"""Hardware normalization: how fast is this machine, right now, relative to
the machine the duration constants were measured on?

The scale harness (``artifacts/scale/``) measured the same statements on
the same schema at the same sizes; the 2026-08-21 AEL-floor session then
showed the *same laptop* disagreeing with itself by ~1.5x hours apart. A
tier label banded on a raw millisecond reading therefore depends on the
laptop. So the harness seeds the reference schema, re-runs a probe set of
reference cases at sizes the artifacts measured, and divides: the ratio
``today_ms / reference_ms`` per probe is the machine factor for that
probe's duration family, and the median over probes is the global factor.
Measured work is divided by the factor before it is banded, so ground
truth is expressed in *reference-machine milliseconds* — the units the
constants were fitted in. Lock waits are not scaled: a 10-second idle
holder is 10 seconds on any hardware.

The reference is ``results_pre_ael_floor_34case.json`` — the run the
``dml_update`` constant was fitted against, on the faster of the two
documented machine states. The factor is also computed against
``results_current_34case.json`` (the 1.46x slower state) and reported, so
the reader sees that the reference itself has a spread.

The full probe set runs at harness start and again at the end; a lighter
set (one probe per family, ~20 s) runs every ``PROBE_EVERY`` cases in
between. Each pass is stamped with its elapsed time, and every case's
factor is interpolated from the passes around it — the first full run
showed the same machine moving from 1.44x to 0.69x *within one 25-minute
run*, which no single factor can describe.
"""

from __future__ import annotations

import itertools
import json
import statistics
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg

ARTIFACTS = Path(__file__).resolve().parent.parent.parent / "artifacts" / "scale"
REFERENCE_PRIMARY = ARTIFACTS / "results_pre_ael_floor_34case.json"
REFERENCE_SECONDARY = ARTIFACTS / "results_current_34case.json"


@dataclass(frozen=True, slots=True)
class Probe:
    """One reference case: its scale-harness id, the fixture size, the duration
    family it stands for, and the SQL (verbatim from the scale harness)."""

    case: str
    table: str
    family: str
    sql: str
    vacuum_after: bool = False


# Verbatim scale-harness statements, chosen so every duration family the
# engine uses has at least one probe with a reference reading well above
# the fixed-overhead floor (>= ~200 ms in the reference run).
PROBES: tuple[Probe, ...] = (
    Probe("create_index_single", "t_1m", "index_build_btree",
          "CREATE INDEX {t}_user_id_idx ON {t} (user_id)"),
    Probe("create_index_expr", "t_1m", "index_build_expression",
          "CREATE INDEX {t}_title_lower_idx ON {t} (lower(title))"),
    Probe("alter_type_int_bigint", "t_1m", "heap_rewrite",
          "ALTER TABLE {t} ALTER COLUMN user_id TYPE bigint"),
    Probe("add_check", "t_1m", "validation_scan",
          "ALTER TABLE {t} ADD CONSTRAINT {t}_note_not_empty CHECK (length(trim(note)) > 0)"),
    Probe("add_fk_plain", "t_1m", "fk_validation",
          "ALTER TABLE {t} ADD CONSTRAINT {t}_account_fk FOREIGN KEY "
          "(account_id) REFERENCES accounts(id)"),
    Probe("reindex_index", "t_1m", "index_bytes", "REINDEX INDEX {t}_account_id_idx"),
    Probe("alter_type_varchar_widen", "t_1m", "index_rebuild",
          "ALTER TABLE {t} ALTER COLUMN status TYPE varchar(64)"),
    Probe("update_without_where", "t_100k", "dml_update",
          "UPDATE {t} SET score = score + 1", vacuum_after=True),
    Probe("delete_without_where", "t_1m", "dml_delete", "DELETE FROM {t}", vacuum_after=True),
)

# The light set: one probe per family, cheap enough to repeat every few
# cases. Sizes chosen so each reference reading is well above the fixed
# overhead and the whole pass stays near twenty seconds.
LIGHT_PROBES: tuple[Probe, ...] = (
    Probe("create_index_single", "t_1m", "index_build_btree",
          "CREATE INDEX {t}_user_id_idx ON {t} (user_id)"),
    Probe("add_check", "t_1m", "validation_scan",
          "ALTER TABLE {t} ADD CONSTRAINT {t}_note_not_empty CHECK (length(trim(note)) > 0)"),
    Probe("reindex_index", "t_1m", "index_bytes", "REINDEX INDEX {t}_account_id_idx"),
    Probe("alter_type_varchar_widen", "t_1m", "index_rebuild",
          "ALTER TABLE {t} ALTER COLUMN status TYPE varchar(64)"),
)
# All at 1M rows on purpose. The first light set mixed in two 100k-row
# probes, and the reference run's 100k readings are slow relative to its
# own 1M readings (cold start per size group), so those probes read ~0.4x
# while the 1M probes read ~0.5-0.8x in the same pass - composition, not
# the machine. The comparable series below is built from the 1M probes
# present in every pass.

PROBE_EVERY = 20  # cases between light probe passes

# Which probe family normalizes which engine constant_key.
FAMILY_OF_CONSTANT: dict[str, str] = {
    "index_build_btree": "index_build_btree",
    "index_build_expression": "index_build_expression",
    "heap_rewrite": "heap_rewrite",
    "validation_scan": "validation_scan",
    "fk_validation": "fk_validation",
    "index_bytes": "index_bytes",
    "dml_update": "dml_update",
    "dml_delete": "dml_delete",
    "constant_op": "constant_op",  # no probe: catalog-only work is not scaled
}


@dataclass(frozen=True, slots=True)
class ProbeReading:
    case: str
    table: str
    family: str
    today_ms: int
    reference_ms: int | None
    secondary_ms: int | None

    @property
    def factor(self) -> float | None:
        if self.reference_ms is None or self.reference_ms <= 0:
            return None
        return self.today_ms / self.reference_ms

    @property
    def factor_secondary(self) -> float | None:
        if self.secondary_ms is None or self.secondary_ms <= 0:
            return None
        return self.today_ms / self.secondary_ms


@dataclass(frozen=True, slots=True)
class Calibration:
    """The machine factors for one probe pass."""

    label: str  # "start" | "end" | "mid<n>"
    readings: tuple[ProbeReading, ...]
    global_factor: float
    global_factor_secondary: float | None
    per_family: dict[str, float]
    at_s: float = 0.0  # seconds since the run started, when the pass began

    def factor_for(self, constant_key: str | None) -> float:
        """The factor to divide a measurement by, given the engine's constant."""
        if constant_key is None:
            return self.global_factor
        family = FAMILY_OF_CONSTANT.get(constant_key)
        if family == "constant_op":
            return 1.0
        if family is not None and family in self.per_family:
            return self.per_family[family]
        return self.global_factor

    def to_json(self) -> dict[str, object]:
        return {
            "label": self.label,
            "at_s": round(self.at_s, 1),
            "global_factor": round(self.global_factor, 3),
            "global_factor_vs_secondary_reference": (
                None if self.global_factor_secondary is None
                else round(self.global_factor_secondary, 3)
            ),
            "per_family": {k: round(v, 3) for k, v in sorted(self.per_family.items())},
            "readings": [
                {
                    "case": r.case,
                    "table": r.table,
                    "family": r.family,
                    "today_ms": r.today_ms,
                    "reference_ms": r.reference_ms,
                    "secondary_reference_ms": r.secondary_ms,
                    "factor": None if r.factor is None else round(r.factor, 3),
                }
                for r in self.readings
            ],
        }


def _reference_ms(path: Path) -> dict[tuple[str, str], int]:
    if not path.exists():
        return {}
    with path.open(encoding="utf8") as fh:
        doc = json.load(fh)
    out: dict[tuple[str, str], int] = {}
    for row in doc["results"]:
        ms = row["measured"]["ms"]
        if ms is not None:
            out[(row["case"], row["table"])] = int(ms)
    return out


def run_probes(
    dsn: str,
    *,
    label: str,
    sizes_present: set[str],
    log: object = None,
    light: bool = False,
    at_s: float = 0.0,
) -> Calibration:
    """Execute every probe whose table is seeded, inside BEGIN...ROLLBACK."""
    primary = _reference_ms(REFERENCE_PRIMARY)
    secondary = _reference_ms(REFERENCE_SECONDARY)
    readings: list[ProbeReading] = []
    with psycopg.connect(dsn) as conn:
        conn.execute("SET TIME ZONE 'UTC'")
        conn.execute("SET statement_timeout = '600s'")
        conn.commit()
        for probe in LIGHT_PROBES if light else PROBES:
            if probe.table not in sizes_present:
                continue
            sql = probe.sql.format(t=probe.table)
            t0 = time.perf_counter()
            conn.execute(sql)
            today_ms = int((time.perf_counter() - t0) * 1000)
            conn.rollback()
            if probe.vacuum_after:
                with psycopg.connect(dsn, autocommit=True) as vac:
                    vac.execute(f"VACUUM (ANALYZE) {probe.table}")
            reading = ProbeReading(
                case=probe.case,
                table=probe.table,
                family=probe.family,
                today_ms=today_ms,
                reference_ms=primary.get((probe.case, probe.table)),
                secondary_ms=secondary.get((probe.case, probe.table)),
            )
            readings.append(reading)
            if log is not None:
                print(
                    f"  probe[{label}] {probe.case:30s} {probe.table:7s} "
                    f"{today_ms:>7} ms  ref={reading.reference_ms}  "
                    f"factor={'n/a' if reading.factor is None else f'{reading.factor:.2f}'}",
                    flush=True,
                )
    factors = [r.factor for r in readings if r.factor is not None]
    secondary_factors = [r.factor_secondary for r in readings if r.factor_secondary is not None]
    global_factor = statistics.median(factors) if factors else 1.0
    per_family: dict[str, float] = {}
    for r in readings:
        if r.factor is not None:
            per_family[r.family] = r.factor
    return Calibration(
        label=label,
        readings=tuple(readings),
        global_factor=global_factor,
        global_factor_secondary=(
            statistics.median(secondary_factors) if secondary_factors else None
        ),
        per_family=per_family,
        at_s=at_s,
    )


def comparable_factor(pass_json: dict[str, Any]) -> float:
    """The pass's factor over the 1M-row probes every pass runs.

    Full and light passes have different probe mixes, and the mix shifts the
    median by itself; interpolating a series that alternates between the two
    would read composition as drift. Computed from the pass's own readings,
    so it applies to results recorded before this function existed.
    """
    comparable = {(p.case, p.table) for p in LIGHT_PROBES} | {
        ("create_index_single", "t_1m"),
        ("add_check", "t_1m"),
        ("reindex_index", "t_1m"),
    }
    factors = [
        float(r["factor"])
        for r in pass_json.get("readings", [])
        if (r["case"], r["table"]) in comparable and r.get("factor") is not None
    ]
    if factors:
        return statistics.median(factors)
    return float(pass_json["global_factor"])


def factor_at(passes: list[dict[str, Any]], at_s: float) -> float:
    """Piecewise-linear interpolation of the comparable factor over probe passes.

    Before the first pass the first factor applies, after the last the last;
    between two passes the factor moves linearly with elapsed time.
    """
    points = sorted((float(p["at_s"]), comparable_factor(p)) for p in passes)
    if not points:
        return 1.0
    if at_s <= points[0][0]:
        return points[0][1]
    for (t0, f0), (t1, f1) in itertools.pairwise(points):
        if t0 <= at_s <= t1:
            if t1 == t0:
                return f1
            return f0 + (f1 - f0) * (at_s - t0) / (t1 - t0)
    return points[-1][1]


def normalize(work_ms: int, wait_ms: int, factor: float) -> int:
    """Reference-machine milliseconds: scaled work plus the unscaled lock wait."""
    if factor <= 0:
        factor = 1.0
    return round(work_ms / factor) + wait_ms
