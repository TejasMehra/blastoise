"""The duration model's constants — part guess, part measured.

This is the single named table the calibration loop (prompt 8) will
overwrite with fitted values. Entries marked ``Calibration.MEASURED``
were measured by the 2026-08-21 scale harness (PG 17.10, 1k-10M-row
tables, local NVMe, single uncontended session); their bases say what was
measured and how the value was chosen. Everything else remains
``Calibration.UNCALIBRATED`` — an admitted guess whose basis starts with
"guess:" — and every estimate built on one says so through a widened
confidence interval.

Measured-on-one-machine is not calibrated-across-environments: the
harness rates are ceilings (uncontended local NVMe), so production can
only be slower, which the asymmetry note on ``WIDEN_MEASURED_TENTHS``
absorbs into the upper bound.

Also here: the classification thresholds (how long a blocking lock may be
held before the statement stops being safe) and the interval-widening
factors. Factors are expressed in tenths (integer arithmetic only — the
project bans floats end to end so that serialized output hashes stably).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum, auto

from blastoise.catalog.model import Calibration


class ConstantUnit(StrEnum):
    ROWS_PER_SECOND = auto()
    BYTES_PER_SECOND = auto()
    FIXED_MILLISECONDS = auto()


@dataclass(frozen=True, slots=True)
class DurationConstant:
    """One throughput (or fixed-time) constant of the duration model."""

    key: str
    unit: ConstantUnit
    value: int
    calibration: Calibration
    basis: str


def _c(key: str, unit: ConstantUnit, value: int, basis: str) -> tuple[str, DurationConstant]:
    return key, DurationConstant(
        key=key, unit=unit, value=value, calibration=Calibration.UNCALIBRATED, basis=basis
    )


def _m(key: str, unit: ConstantUnit, value: int, basis: str) -> tuple[str, DurationConstant]:
    return key, DurationConstant(
        key=key, unit=unit, value=value, calibration=Calibration.MEASURED, basis=basis
    )


# The table. Keys are operation families; the engine maps each catalog
# row's kind onto exactly one family (no fallback: an unmapped kind is a
# "cannot estimate", not a default).
DURATION_CONSTANTS: dict[str, DurationConstant] = dict(
    (
        _c(
            "heap_rewrite",
            ConstantUnit.ROWS_PER_SECOND,
            100_000,
            "guess: a rewrite sequentially reads the heap, writes a new one, WAL-logs "
            "everything, and rebuilds every index; public war stories put full-table "
            "rewrites at roughly 1-5 GB/min on ordinary cloud disks, which at ~150 "
            "bytes/row is ~100-500k rows/s — the low end chosen to be conservative "
            "(the 2026-08-21 scale harness measured 59-125k rows/s across rewrite "
            "shapes at 1M/10M rows: the guess stands)",
        ),
        _c(
            "validation_scan",
            ConstantUnit.ROWS_PER_SECOND,
            500_000,
            "guess: a read-only sequential scan evaluating one predicate per row; a bare "
            "seqscan runs millions of rows/s from cache and hundreds of thousands from "
            "disk, and the check itself is cheap",
        ),
        _m(
            "fk_validation",
            ConstantUnit.ROWS_PER_SECOND,
            1_200_000,
            "measured 2026-08-21 (scale harness, PG 17.10, local NVMe, uncontended): "
            "RI_Initial_Check validates the whole constraint as one join, not per-row "
            "index probes — ADD/VALIDATE FOREIGN KEY at 1M/10M rows ran at 1.2M-4.5M "
            "rows/s; the slowest at-scale case chosen. The prior 100k rows/s guess "
            "was 12-45x pessimistic and produced false UNSAFEs on ADD FK at 10M",
        ),
        _m(
            "index_build_btree",
            ConstantUnit.ROWS_PER_SECOND,
            1_000_000,
            "measured 2026-08-21 (scale harness, PG 17.10, local NVMe, uncontended): "
            "plain-key btree builds — single-column, composite, partial, and unique — "
            "ran at 0.84-1.4M rows/s at 1M/10M rows; sort-dominated. Rebuilds of "
            "dependent indexes during ALTER COLUMN TYPE measured the same rate",
        ),
        _m(
            "index_build_expression",
            ConstantUnit.ROWS_PER_SECOND,
            250_000,
            "measured 2026-08-21 (scale harness, PG 17.10, local NVMe, uncontended): "
            "expression-column and GIN builds ran at 176-282k rows/s at 100k-10M rows "
            "(per-row expression evaluation / posting-tree assembly dominates). Also "
            "the observed whole-table rate of REINDEX TABLE (42s at 10M rows), whose "
            "work is every index on the table, so the same constant covers it",
        ),
        _m(
            "dml_update",
            ConstantUnit.ROWS_PER_SECOND,
            22_000,
            "measured 2026-08-21 (scale harness, PG 17.10, local NVMe, "
            "uncontended): the unbatched-backfill family (UPDATE with no WHERE, "
            "rewriting every row) ran at 29.2k / 27.7k / 22.2k rows/s at 100k / "
            "1M / 10M rows; the slowest at-scale case chosen, as for "
            "fk_validation. The prior 50k rows/s guess was ~2x optimistic — the "
            "dangerous direction — and was left alone while duration was "
            "secondary and the 4x uncalibrated widening absorbed it; with the "
            "band of the upper bound now the headline, a 2x underestimate can "
            "drop a statement a full band, so it is fixed to the measurement. "
            "Known sub-family this single constant does not split: scattered "
            "partial updates (matched WHERE, index-driven, random heap access) "
            "measured 8.7-16k rows/s at 1M/10M — slower still. They are not "
            "split because the model cannot see selectivity statically, and "
            "because matched-row DML is bounded, never predicted; the "
            "calibration loop owns that split",
        ),        _c(
            "dml_delete",
            ConstantUnit.ROWS_PER_SECOND,
            100_000,
            "guess: a delete marks tuples dead without writing new versions; ~2x the "
            "update rate",
        ),
        _m(
            "index_bytes",
            ConstantUnit.BYTES_PER_SECOND,
            9_500_000,
            "measured 2026-08-21 (scale harness, PG 17.10, local NVMe, uncontended): "
            "REINDEX INDEX ran at 6.9-10 MB/s of index size across 100k-10M-row "
            "tables. The prior 50 MB/s guess was 5x optimistic: btree deduplication "
            "makes the final index small relative to the heap-scan-and-sort work, so "
            "per-byte rates are low",
        ),
        _c(
            "constant_op",
            ConstantUnit.FIXED_MILLISECONDS,
            10,
            "guess: a catalog-only operation touches a handful of catalog rows and "
            "fsyncs one WAL flush; ~10 ms point, up to ~100 ms on slow commit paths "
            "(the interval carries the spread; the harness measured median 3 ms, "
            "honest max 41 ms)",
        ),
    )
)


# --- Classification thresholds -------------------------------------------
#
# How long a table-level blocking lock may plausibly be held before the
# statement stops being SAFE / becomes UNSAFE. Applied to the *upper* bound
# of the duration interval (the worst plausible hold), so wide intervals
# from stale statistics push borderline statements toward the stricter
# class rather than the laxer one.

FULL_BLOCK_SHORT_MS = 2_000
FULL_BLOCK_LONG_MS = 20_000
"""Locks that stop reads too (ACCESS EXCLUSIVE, EXCLUSIVE): a ~2 s stall
hides inside p99 retry budgets; beyond ~20 s health checks fail and the
outage is user-visible. Basis: convention, not measurement — UNCALIBRATED
like the guessed constants above."""

WRITE_BLOCK_SHORT_MS = 5_000
WRITE_BLOCK_LONG_MS = 60_000
"""Locks that stop writes but let reads flow (SHARE, SHARE ROW EXCLUSIVE):
a ~5 s write stall is absorbed by retry logic; a minute of failed writes is
an incident. Basis: convention, not measurement."""


# --- Duration bands --------------------------------------------------------
#
# The primary, coarse reading of a duration interval: which order of
# magnitude the worst plausible hold falls in. The scale harness showed the
# numeric intervals are ~16x wide median — too wide to headline — while the
# band of the upper bound tracked the measured reality; the classification
# plus the band is what the engine presents first, the numeric interval is
# secondary detail.

BAND_SUB_SECOND_MAX_MS = 1_000
BAND_SECONDS_MAX_MS = 60_000
BAND_MINUTES_MAX_MS = 3_600_000


# --- Interval widening (all factors in tenths: 40 = 4.0x) -----------------

WIDEN_UNCALIBRATED_TENTHS = 40
"""An uncalibrated throughput constant is an order-of-magnitude guess:
the interval spans 4x each way around the point. Calibration (prompt 8)
is expected to shrink this."""

WIDEN_MEASURED_TENTHS = 20
"""A constant measured by the scale harness on one uncontended NVMe
machine: 2x each way covers the residual spread the harness observed
across case shapes at the same size. The measured rates are ceilings —
production storage and contention only slow things down — which the upper
bound (the side classification bands on) absorbs."""

WIDEN_CALIBRATED_TENTHS = 15
"""Once a constant is calibrated across environments, residual variance
still warrants 1.5x."""

WIDEN_CONSTANT_OP_TENTHS = 100
"""Fixed-time catalog operations: 10 ms point, 1-100 ms plausible range
(10x each way) — commit latency dominates and varies wildly."""

# Fixed overhead added to every row/byte-proportional estimate: even a
# zero-row operation opens relations, takes its locks, updates catalogs,
# and commits. The triple is constant_op's point and interval (10 ms,
# 1-100 ms); the harness's 1k-row cases (2-14 ms measured where the
# proportional term rounds to zero) sit inside it.
FIXED_OVERHEAD_LOW_MS = 1
FIXED_OVERHEAD_POINT_MS = 10
FIXED_OVERHEAD_HIGH_MS = 100

# Statistics-age widening: reltuples decays as the table changes under it.
WIDEN_AGE_FRESH_TENTHS = 10  # analyzed within a day: x1.0
WIDEN_AGE_WEEK_TENTHS = 15  # within a week: x1.5
WIDEN_AGE_MONTH_TENTHS = 20  # within a month: x2.0
WIDEN_AGE_OLD_TENTHS = 30  # older: x3.0
WIDEN_AGE_UNKNOWN_TENTHS = 30  # reltuples set but analyze times unknowable: x3.0
WIDEN_MODS_UNKNOWN_TENTHS = 20  # n_mod_since_analyze unavailable: x2.0
WIDEN_MODS_CAP_TENTHS = 50  # churn-driven widening caps at x5.0

# Confidence labels from the total widening factor (in tenths).
CONFIDENCE_HIGH_MAX_TENTHS = 20  # ≤2x each way
CONFIDENCE_MEDIUM_MAX_TENTHS = 60  # ≤6x each way
