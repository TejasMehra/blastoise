"""The duration model's constants — part guess, part measured.

This is the single named table the calibration loop (prompt 8) will
overwrite with fitted values. Entries marked ``Calibration.MEASURED``
were measured by the 2026-08-21 scale harness and the 2026-08-22
re-measurement (PG 17.10, 1k-10M-row tables, local NVMe, single
uncontended session, hardware-normalized across passes); their bases say
what was measured, over how many runs, and how the value was chosen.
Everything else remains ``Calibration.UNCALIBRATED`` — an admitted guess
whose basis starts with "guess:" — and every estimate built on one says so
through a widened confidence interval.

Measured-on-one-machine is not calibrated-across-environments: the
harness rates are ceilings (uncontended local NVMe), so production can
only be slower, which the upper side of the band absorbs. The band itself
is not a flat per-tier multiplier — it is derived from each constant's
measurement provenance (runs and observed spread) by
``base_widen_tenths``, so a constant measured twice and accurate both
times carries a tighter band than a guess.

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
    """One throughput (or fixed-time) constant of the duration model.

    ``runs`` and ``spread_tenths`` are the measurement provenance the
    widening reads (see :func:`base_widen_tenths`): how many independent
    measurement runs stand behind the value, and the observed spread of the
    normalized rate across those runs and the representative shapes it
    covers (max/min, in tenths — 12 means the fastest reading was 1.2x the
    slowest). They are 0 for an ``UNCALIBRATED`` guess, which has no
    measured spread to speak of. The band a measured constant carries is
    derived from these, not from a flat per-tier multiplier: a constant
    measured twice with tight agreement earns a tighter band than one
    measured once or one whose shapes scatter.
    """

    key: str
    unit: ConstantUnit
    value: int
    calibration: Calibration
    basis: str
    runs: int = 0
    spread_tenths: int = 0
    # Hardware provenance. ``profiles`` is how many distinct hardware
    # profiles measured this constant (one laptop = 1, however many runs);
    # ``per_profile`` records the probe-scaled at-scale value observed on
    # each, ``(profile label, value)``, so the reader can see the spread
    # the band is derived from rather than take it on faith. The
    # cross-profile spread is what ``boundary_spread_tenths`` reads: it is
    # the part of the variance the calibration probe could *not* remove,
    # and therefore the width of the strip around a threshold inside which
    # the engine refuses to decide.
    profiles: int = 1
    per_profile: tuple[tuple[str, int], ...] = ()
    cross_profile_spread_tenths: int = 0


def _c(key: str, unit: ConstantUnit, value: int, basis: str) -> tuple[str, DurationConstant]:
    return key, DurationConstant(
        key=key, unit=unit, value=value, calibration=Calibration.UNCALIBRATED, basis=basis
    )


def _m(
    key: str,
    unit: ConstantUnit,
    value: int,
    basis: str,
    *,
    runs: int,
    spread_tenths: int,
    profiles: int = 1,
    per_profile: tuple[tuple[str, int], ...] = (),
    cross_profile_spread_tenths: int = 0,
) -> tuple[str, DurationConstant]:
    return key, DurationConstant(
        key=key,
        unit=unit,
        value=value,
        calibration=Calibration.MEASURED,
        basis=basis,
        runs=runs,
        spread_tenths=spread_tenths,
        profiles=profiles,
        per_profile=per_profile,
        cross_profile_spread_tenths=cross_profile_spread_tenths,
    )


# The table. Keys are operation families; the engine maps each catalog
# row's kind onto exactly one family (no fallback: an unmapped kind is a
# "cannot estimate", not a default).
#
# Anchor. Every measured value below is expressed at the ``laptop-nvme``
# profile as measured on 2026-08-23 by ``artifacts/scripts/measure_profiles.py``
# (``artifacts/profiles/laptop-nvme.json``): PG 17.10, the scale schema at
# 1k/100k/1M/10M, two passes, the calibration probe read before and after
# each pass (median 215 ms — ``blastoise.live.calibrate.REFERENCE_COMPUTE_MS``).
# A value is the slowest at-scale reading on the anchor (1M rows or more;
# 100k or more for DML), the convention the earlier sessions used — with
# one recorded exception, heap_rewrite, whose basis explains it; the
# duration model multiplies it by the target's probe ratio. The values
# differ from the 2026-08-21/22 ones because the anchor does: the same
# laptop read 1.2-2x slower this run than in the state the earlier
# constants were fitted in (heap_rewrite at 10M was the extreme), and a
# constant can only be paired with a probe reading taken in the same run.
# ``profiles`` is 1 everywhere: the cross-profile measurement the band is
# supposed to come from has not run (see the 2026-08-23 DECISIONS section),
# so every band sits at the single-profile floor and the boundary rule
# refuses on that floor.
_LAPTOP = "laptop-nvme"

DURATION_CONSTANTS: dict[str, DurationConstant] = dict(
    (
        _m(
            "heap_rewrite",
            ConstantUnit.ROWS_PER_SECOND,
            74_000,
            "measured 2026-08-23 on the anchor profile (two passes): a plain "
            "relabel/format rewrite (ALTER COLUMN TYPE, SET LOGGED/UNLOGGED, "
            "SET TABLESPACE/ACCESS METHOD, VACUUM FULL, CLUSTER) copies the heap and "
            "rebuilds indexes with no per-row computation. int->bigint and "
            "text->varchar ran at 74-84k rows/s at 1M; the 1M reading is the value "
            "so that the split against add_column_rewrite (measured at 1M only) "
            "keeps its meaning — a plain relabel is faster than a compute-per-row "
            "add. At 10M the same rewrite read 45-52k: a 3 GB heap copy is "
            "write-path-bound and the compute probe cannot see the write path, so "
            "that 1.6x drop, not the pass-to-pass noise, sets the spread (1.9x). The "
            "2026-08-21/22 runs read 87-105k on the same laptop in a faster state. "
            "The exception to slowest-at-scale is deliberate and recorded here",
            runs=2,
            spread_tenths=19,
            per_profile=((_LAPTOP, 74_041),),
        ),
        _m(
            "add_column_rewrite",
            ConstantUnit.ROWS_PER_SECOND,
            55_000,
            "measured 2026-08-23 on the anchor profile (two passes): an ADD COLUMN "
            "that rewrites the heap AND computes a value per row — gen_random_uuid() "
            "for a volatile default, nextval() for serial/identity, an expression for "
            "GENERATED STORED — ran at 55-79k rows/s at 1M, gen_random_uuid the "
            "slowest. Split out of heap_rewrite (2026-08-22) because one constant "
            "cannot be both accurate for the fast relabel and safe for these; the "
            "2026-08-21/22 runs read 28-64k in a faster laptop state",
            runs=2,
            spread_tenths=15,
            per_profile=((_LAPTOP, 55_444),),
        ),
        _m(
            "validation_scan",
            ConstantUnit.ROWS_PER_SECOND,
            790_000,
            "measured 2026-08-23 on the anchor profile (two passes): a read-only "
            "sequential scan with one predicate per row (ADD CHECK, SET NOT NULL "
            "without a proving CHECK) ran at 0.80-1.73M rows/s at 1M/10M; slowest "
            "(ADD CHECK at 10M, 796k) chosen. First measured 2026-08-22 at 0.9-2.3M "
            "(then set to a round 1M); before that a 500k guess with a 4x band that "
            "produced false UNSAFEs on 5M-row scans",
            runs=2,
            spread_tenths=22,
            per_profile=((_LAPTOP, 796_241),),
        ),
        _m(
            "fk_validation",
            ConstantUnit.ROWS_PER_SECOND,
            1_000_000,
            "measured 2026-08-23 on the anchor profile (two passes): RI_Initial_Check "
            "validates the whole constraint as one join, not per-row index probes — "
            "ADD FOREIGN KEY at 1M/10M ran at 1.0-3.8M rows/s; the slowest (the first "
            "1M pass, cold) chosen. 2026-08-21 measured 1.2-4.5M. The prior 100k "
            "guess was 12-45x pessimistic and produced false UNSAFEs at 10M. Shapes "
            "scatter 3.9x, so the band is held at the 2x measured cap",
            runs=2,
            spread_tenths=39,
            per_profile=((_LAPTOP, 1_009_081),),
        ),
        _m(
            "index_build_btree",
            ConstantUnit.ROWS_PER_SECOND,
            740_000,
            "measured 2026-08-23 on the anchor profile (two passes): a plain-key "
            "btree build ran at 0.74-1.43M rows/s at 1M/10M; slowest chosen. "
            "2026-08-21 measured 0.84-1.4M for single-column, composite, partial, "
            "and unique builds; rebuilds of dependent indexes during ALTER COLUMN "
            "TYPE measured the same rate",
            runs=2,
            spread_tenths=20,
            per_profile=((_LAPTOP, 738_552),),
        ),
        _m(
            "index_build_expression",
            ConstantUnit.ROWS_PER_SECOND,
            170_000,
            "measured 2026-08-23 on the anchor profile (two passes): an expression "
            "(lower(title)) btree build ran at 169-249k rows/s at 1M/10M; slowest "
            "chosen. 2026-08-21 measured 176-282k for expression and GIN builds "
            "(per-row expression evaluation / posting-tree assembly dominates), and "
            "the same whole-table rate for REINDEX TABLE, which the constant also "
            "covers",
            runs=2,
            spread_tenths=15,
            per_profile=((_LAPTOP, 169_462),),
        ),
        _m(
            "dml_update",
            ConstantUnit.ROWS_PER_SECOND,
            13_000,
            "measured 2026-08-23 on the anchor profile (two passes): the unbatched "
            "backfill family (UPDATE with no WHERE, rewriting every row) ran at "
            "13.2-15.2k rows/s at 100k/1M; slowest chosen. 2026-08-21 measured "
            "22-29k in a faster laptop state; the original 50k guess was ~2x "
            "optimistic — the dangerous direction. Known sub-family this constant "
            "does not split: scattered partial updates (matched WHERE, index-driven, "
            "random heap access) measured 8.7-16k at 1M/10M — slower still; not "
            "split because the model cannot see selectivity statically, and "
            "matched-row DML is bounded, never predicted",
            runs=2,
            spread_tenths=12,
            per_profile=((_LAPTOP, 13_248),),
        ),
        _m(
            "dml_delete",
            ConstantUnit.ROWS_PER_SECOND,
            630_000,
            "measured 2026-08-23 on the anchor profile (two passes): DELETE with no "
            "WHERE ran at 632k-1.02M rows/s at 100k/1M; slowest chosen. Until this "
            "run a guess (100k rows/s, 'a delete marks tuples dead without writing "
            "new versions; ~2x the update rate') that was ~6x pessimistic and, with "
            "its 4x band, put a 5M-row delete's upper bound at 200 s — the "
            "del_nowhere_5m false BLOCK of the 2026-08-22 runs. Measured by the same "
            "script and method as every other family here, on the same anchor",
            runs=2,
            spread_tenths=17,
            per_profile=((_LAPTOP, 632_511),),
        ),
        _m(
            "index_bytes",
            ConstantUnit.BYTES_PER_SECOND,
            7_800_000,
            "measured 2026-08-23 on the anchor profile (two passes): REINDEX INDEX "
            "ran at 7.8-9.7 MB/s of index size at 1M/10M; slowest chosen. 2026-08-21 "
            "measured 6.9-10 MB/s. The original 50 MB/s guess was 5x optimistic: "
            "btree deduplication makes the final index small relative to the "
            "heap-scan-and-sort work, so per-byte rates are low",
            runs=2,
            spread_tenths=13,
            per_profile=((_LAPTOP, 7_806_648),),
        ),
        _c(
            "constant_op",
            ConstantUnit.FIXED_MILLISECONDS,
            10,
            "guess: a catalog-only operation touches a handful of catalog rows and "
            "fsyncs one WAL flush; ~10 ms point, up to ~100 ms on slow commit paths "
            "(the interval carries the spread; the harness measured median 3 ms, "
            "honest max 41 ms; the 2026-08-23 anchor run read 1-3 ms). Left a guess: "
            "commit latency is a property of the storage path, which no read-only "
            "probe can measure, and it is never banded",
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
#
# The band a throughput constant carries is not a flat per-tier multiplier;
# it is derived from how well the value is actually known — its calibration
# status, how many independent runs measured it, and how far those runs and
# the shapes they cover scattered. A constant measured twice and accurate
# both times must not carry the same uncertainty band as a guess.
# ``base_widen_tenths`` is the one place that derivation lives.

WIDEN_UNCALIBRATED_TENTHS = 40
"""An uncalibrated throughput constant is an order-of-magnitude guess:
the interval spans 4x each way around the point. Calibration (prompt 8)
is expected to shrink this."""

WIDEN_MEASURED_SINGLE_RUN_TENTHS = 30
"""Measured, but only once: the value is anchored, yet its run-to-run
variance is unknown (the AEL-floor session found the same laptop
disagreeing with itself ~1.5x hours apart, so a single run cannot bound
that). A guess with better manners — 3x, between the 4x guess and the
replicated-measurement band — until a second run makes the spread real."""

WIDEN_MEASURED_DRIFT_FLOOR_TENTHS = 15
"""No measured band sits below 1.5x, however tightly the runs agreed: a
measurement on one quiet laptop bounds production only from above, and the
same machine measured twice drifts ~1.46x (DECISIONS, the AEL floor). The
band must cover at least that."""

WIDEN_MEASURED_CAP_TENTHS = 20
"""No measured band exceeds 2x either: past that the value is too poorly
pinned to call MEASURED rather than a guess, and 2x already absorbs a
2x-slower production disk. A constant whose shapes scatter wider than this
(fk_validation, add_column_rewrite) is capped here — the scatter is a known
sub-family split the calibration loop owns, not licence for a guess band."""

WIDEN_CALIBRATED_TENTHS = 15
"""Once a constant is calibrated across environments, residual variance
still warrants 1.5x."""

WIDEN_CONSTANT_OP_TENTHS = 100
"""Fixed-time catalog operations: 10 ms point, 1-100 ms plausible range
(10x each way) — commit latency dominates and varies wildly."""


def base_widen_tenths(constant: DurationConstant) -> int:
    """The interval-widening factor (tenths) a constant earns from its provenance.

    Guess -> 4x. Measured once -> 3x (variance unknown). Measured twice or
    more -> the observed spread, floored at the documented same-machine/
    production drift (1.5x) and capped at 2x. Calibrated across environments
    -> 1.5x. This is what ties the band to calibration status *and* measured
    variance instead of a flat multiplier: heap_rewrite, three runs agreeing
    within ~1.3x, earns 1.5x, while fk_validation, whose shapes scatter 3.8x,
    is held at the 2x cap.
    """
    if constant.calibration is Calibration.UNCALIBRATED:
        return WIDEN_UNCALIBRATED_TENTHS
    if constant.calibration is Calibration.CALIBRATED:
        return WIDEN_CALIBRATED_TENTHS
    if constant.runs < 2:
        return WIDEN_MEASURED_SINGLE_RUN_TENTHS
    return min(
        max(constant.spread_tenths, WIDEN_MEASURED_DRIFT_FLOOR_TENTHS),
        WIDEN_MEASURED_CAP_TENTHS,
    )


# --- Boundary proximity ----------------------------------------------------
#
# A tier threshold is a line; an estimate is a strip. When the strip the
# constant's *known* spread draws around the point estimate contains a
# threshold, the side the upper bound lands on is decided by hardware the
# probe could not characterize, not by the migration — the 2026-08-22
# validation runs flipped exactly such cases run-to-run on one machine.
# Inside that strip the engine refuses to decide (UNKNOWN, with the
# reason) rather than return a coin-flip between NEEDS_TIMING and UNSAFE.

BOUNDARY_SPREAD_FLOOR_TENTHS = 15
"""The narrowest strip any constant earns, measured cross-profile or not:
the same machine measured hours apart drifted ~1.5x (DECISIONS, the AEL
floor), and a single profile cannot claim to have bounded what it never
varied. Constants measured on three or more profiles use their observed
cross-profile spread when it is wider; one-profile constants sit at the
floor, which is the honest statement that their spread is unknown."""

MIN_PROFILES_FOR_CROSS_SPREAD = 3
"""Below this many hardware profiles, an observed cross-profile spread is
two points and a line, not a spread; the floor applies."""


def boundary_spread_tenths(constant: DurationConstant) -> int:
    """The half-width (as a factor, in tenths) of the strip around a threshold
    inside which a verdict resting on this constant is a coin-flip.

    Uncalibrated guesses use their full guess band (4x): a guess straddling
    a threshold anywhere within its band is undecidable by construction.
    Measured constants use the cross-profile spread when at least
    ``MIN_PROFILES_FOR_CROSS_SPREAD`` profiles stand behind it, floored at
    ``BOUNDARY_SPREAD_FLOOR_TENTHS``; otherwise the floor alone.
    """
    if constant.calibration is Calibration.UNCALIBRATED:
        return WIDEN_UNCALIBRATED_TENTHS
    observed = (
        constant.cross_profile_spread_tenths
        if constant.profiles >= MIN_PROFILES_FOR_CROSS_SPREAD
        else 0
    )
    return max(observed, BOUNDARY_SPREAD_FLOOR_TENTHS)

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
