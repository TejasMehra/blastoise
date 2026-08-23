"""The explicit lock-hold duration model.

The model is deliberately simple and fully stated: for a row-proportional
operation the point estimate is ``rows / throughput``, where ``rows`` is
the snapshot's ``reltuples`` and ``throughput`` is one named constant from
:mod:`blastoise.verdict.constants`. The confidence interval starts at the
constant's calibration width and widens with the staleness of the
statistics — analyze age and rows modified since — so a stale estimate is
still SIMULATED, just with less confidence; the staleness never disappears
into the point value.

Where an input is missing, the answer is :class:`CannotEstimate` with the
specific missing input. There is no fallback path that produces a number
anyway.

Every proportional estimate also carries the fixed constant-op overhead
additively (10 ms point, 1-100 ms spread): the scale harness showed that
at small sizes the fixed cost of locking, catalog updates, and commit
dominates the proportional term, and without the floor a measured 3 ms
would fall "dangerously above" a computed 1 ms interval.

The target's hardware is an input, not an assumption. The constants are
rates on the anchor profile (:mod:`blastoise.live.calibrate` records its
probe readings); the snapshot carries the same probe timed on the
target, and every proportional estimate is multiplied by the ratio
before it is widened. A snapshot without a usable probe reading leaves
the estimate unscaled and says so in its inputs — the same assumption
the model always made, now visible.

Integer arithmetic throughout (milliseconds, tenths for factors): the
project bans floats so results serialize and hash stably.
"""

from __future__ import annotations

from datetime import datetime

from blastoise.ir import AlterTableActionKind, StatementKind
from blastoise.live.calibrate import hardware_factor_tenths
from blastoise.live.model import CalibrationFacts, RelationFacts, ServerFacts
from blastoise.verdict import constants as k
from blastoise.verdict.constants import DURATION_CONSTANTS, ConstantUnit, DurationConstant
from blastoise.verdict.model import CannotEstimate, DurationEstimate, Method

_SK = StatementKind
_AK = AlterTableActionKind

# Which throughput family a row-proportional kind draws on. No fallback:
# a PROPORTIONAL_TO_ROWS row whose kind is absent here yields an explicit
# CannotEstimate naming the gap. CREATE INDEX maps to the plain-btree
# constant here; the engine upgrades it to index_build_expression when the
# parse tree shows a non-btree access method or an expression column
# (measured 4-5x slower). Table-scope REINDEX rebuilds *every* index, and
# the harness measured its whole-table rate at the expression constant.
ROWS_FAMILY_BY_KIND: dict[StatementKind | AlterTableActionKind, str] = {
    _SK.CREATE_INDEX: "index_build_btree",
    _SK.CREATE_INDEX_CONCURRENTLY: "index_build_btree",
    _SK.REINDEX: "index_build_expression",
    _SK.UPDATE: "dml_update",
    _SK.UPDATE_BATCHED: "dml_update",
    _SK.UPDATE_WITHOUT_WHERE: "dml_update",
    _SK.DELETE: "dml_delete",
    _SK.DELETE_BATCHED: "dml_delete",
    _SK.DELETE_WITHOUT_WHERE: "dml_delete",
    _SK.VACUUM: "validation_scan",
    _SK.VACUUM_FULL: "heap_rewrite",
    _SK.CLUSTER: "heap_rewrite",
    _SK.COPY_TO: "validation_scan",
    _SK.ALTER_DOMAIN: "validation_scan",
    _AK.ADD_CHECK: "validation_scan",
    _AK.SET_NOT_NULL: "validation_scan",
    _AK.ADD_NOT_NULL_CONSTRAINT: "validation_scan",
    _AK.VALIDATE_CONSTRAINT: "validation_scan",
    _AK.ATTACH_PARTITION: "validation_scan",
    _AK.ADD_FOREIGN_KEY: "fk_validation",
    _AK.ADD_PRIMARY_KEY: "index_build_btree",
    _AK.ADD_UNIQUE: "index_build_btree",
    _AK.ADD_EXCLUSION: "index_build_expression",  # exclusion = GiST/expression-shaped
    _AK.ALTER_COLUMN_TYPE: "heap_rewrite",
    # A constant/domain default writes one fixed value per row: plain
    # relabel speed (measured 82-125k rows/s), so it stays on heap_rewrite.
    _AK.ADD_COLUMN_DEFAULT_NONVOLATILE: "heap_rewrite",  # PG 10 row / constrained domain
    # These rewrite the heap AND compute a value per row (gen_random_uuid,
    # nextval, a generated expression): measured 1.5-2x slower, their own
    # constant so the fast relabel's tight band cannot under-predict them.
    _AK.ADD_COLUMN_DEFAULT_VOLATILE: "add_column_rewrite",
    _AK.ADD_COLUMN_DEFAULT_UNKNOWN_VOLATILITY: "add_column_rewrite",
    _AK.ADD_COLUMN_SERIAL: "add_column_rewrite",
    _AK.ADD_COLUMN_IDENTITY: "add_column_rewrite",
    _AK.ADD_COLUMN_GENERATED_STORED: "add_column_rewrite",
    _AK.SET_LOGGED: "heap_rewrite",
    _AK.SET_UNLOGGED: "heap_rewrite",
    _AK.SET_TABLESPACE: "heap_rewrite",
    _AK.SET_ACCESS_METHOD: "heap_rewrite",
    _AK.SET_EXPRESSION: "heap_rewrite",
}

# PROPORTIONAL_TO_INDEX_SIZE kinds.
BYTES_FAMILY_BY_KIND: dict[StatementKind | AlterTableActionKind, str] = {
    _SK.REINDEX: "index_bytes",
    _SK.REINDEX_CONCURRENTLY: "index_bytes",
}


def _confidence(total_widen_tenths: int) -> str:
    if total_widen_tenths <= k.CONFIDENCE_HIGH_MAX_TENTHS:
        return "high"
    if total_widen_tenths <= k.CONFIDENCE_MEDIUM_MAX_TENTHS:
        return "medium"
    return "low"


def _interval(point_ms: int, widen_tenths: int) -> tuple[int, int]:
    low = point_ms * 10 // widen_tenths
    high = point_ms * widen_tenths // 10
    return low, max(high, point_ms)


def _base_tenths(constant: DurationConstant) -> int:
    """The constant's base widening, derived from its measurement provenance.

    See :func:`blastoise.verdict.constants.base_widen_tenths` — the band is
    tied to calibration status and measured variance, not a flat per-tier
    multiplier.
    """
    return k.base_widen_tenths(constant)


def _with_overhead(prop_point: int, widen_tenths: int) -> tuple[int, int, int]:
    """(low, point, high): the widened proportional term plus fixed overhead.

    The overhead triple is constant_op's point and interval — even a
    zero-row operation opens relations, locks, and commits, so no estimate
    may fall below it.
    """
    low, high = _interval(prop_point, widen_tenths)
    return (
        low + k.FIXED_OVERHEAD_LOW_MS,
        prop_point + k.FIXED_OVERHEAD_POINT_MS,
        high + k.FIXED_OVERHEAD_HIGH_MS,
    )


def _parse_iso(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _age_widen(
    relation: RelationFacts, server: ServerFacts
) -> tuple[int, str]:
    """Widening factor (tenths) from the age of the statistics, with the input note."""
    newest: datetime | None = None
    for fact in (relation.last_analyze, relation.last_autoanalyze):
        if fact.available and isinstance(fact.value, str):
            stamp = _parse_iso(fact.value)
            if stamp is not None and (newest is None or stamp > newest):
                newest = stamp
    if newest is None:
        return k.WIDEN_AGE_UNKNOWN_TENTHS, "analyze time unknown (never analyzed or masked)"
    if not server.server_now.available or not isinstance(server.server_now.value, str):
        return k.WIDEN_AGE_UNKNOWN_TENTHS, "server clock unavailable; analyze age unknown"
    now = _parse_iso(server.server_now.value)
    if now is None:
        return k.WIDEN_AGE_UNKNOWN_TENTHS, "server clock unparsable; analyze age unknown"
    age_s = max(0, int((now - newest).total_seconds()))
    if age_s <= 86_400:
        return k.WIDEN_AGE_FRESH_TENTHS, f"analyzed {age_s // 3600}h ago"
    age_days = age_s // 86_400
    if age_days <= 7:
        return k.WIDEN_AGE_WEEK_TENTHS, f"analyzed {age_days}d ago"
    if age_days <= 30:
        return k.WIDEN_AGE_MONTH_TENTHS, f"analyzed {age_days}d ago"
    return k.WIDEN_AGE_OLD_TENTHS, f"analyzed {age_days}d ago (stale)"


def _mods_widen(relation: RelationFacts, rows: int) -> tuple[int, str] | CannotEstimate:
    """Widening from rows modified since analyze; refuses when churn swamps the estimate."""
    fact = relation.n_mod_since_analyze
    if not fact.available or not isinstance(fact.value, int):
        reason = fact.reason or "not gathered"
        return k.WIDEN_MODS_UNKNOWN_TENTHS, f"n_mod_since_analyze unavailable ({reason})"
    n_mod = fact.value
    if n_mod > 0 and rows < n_mod // 10:
        return CannotEstimate(
            reason=(
                f"statistics unusable: {n_mod} rows modified since the last analyze "
                f"against a row-count estimate of {rows}; the estimate no longer "
                "describes the table"
            ),
            method=Method.UNVERIFIED,
        )
    pct = 100 * n_mod // max(rows, 1)
    tenths = min(k.WIDEN_MODS_CAP_TENTHS, 10 + 10 * n_mod // max(rows, 1))
    return tenths, f"n_mod_since_analyze={n_mod} ({pct}% of estimate)"


def _usable_rows(relation: RelationFacts) -> int | CannotEstimate:
    """The relation's row-count estimate, or the refusal explaining why not."""
    if not relation.exists.available:
        return CannotEstimate(
            reason=f"existence of {relation.requested} unknown: {relation.exists.reason}",
            method=Method.UNVERIFIED,
        )
    if relation.exists.value is False:
        return CannotEstimate(
            reason=f"{relation.requested} does not exist on the target database",
            method=Method.UNVERIFIED,
        )
    rows_fact = relation.reltuples
    if not rows_fact.available or not isinstance(rows_fact.value, int):
        return CannotEstimate(
            reason=(
                f"no usable row-count estimate for {relation.requested}: "
                f"{rows_fact.reason or 'reltuples not gathered'}"
            ),
            method=Method.UNVERIFIED,
        )
    rows = max(0, rows_fact.value)
    never_analyzed = (
        relation.last_analyze.available
        and relation.last_analyze.value is None
        and relation.last_autoanalyze.available
        and relation.last_autoanalyze.value is None
    )
    if rows == 0 and never_analyzed:
        return CannotEstimate(
            reason=(
                f"row-count estimate for {relation.requested} is 0 but the table was "
                "never analyzed: emptiness cannot be told from a never-set estimate "
                "(pre-14 servers report 0 for both)"
            ),
            method=Method.UNVERIFIED,
        )
    return rows


def _staleness(
    relation: RelationFacts, server: ServerFacts, rows: int
) -> tuple[int, str, str] | CannotEstimate:
    """(staleness widening tenths, age note, mods note), or a refusal."""
    mods = _mods_widen(relation, rows)
    if isinstance(mods, CannotEstimate):
        return mods
    mods_tenths, mods_note = mods
    age_tenths, age_note = _age_widen(relation, server)
    return age_tenths * mods_tenths // 10, age_note, mods_note


def _calibration_note(constant: DurationConstant) -> str:
    return constant.calibration.value.lower()


def _hardware(calibration: CalibrationFacts | None) -> tuple[int, str]:
    """(factor tenths, input note) for scaling an estimate to the target."""
    return hardware_factor_tenths(calibration)


def _scaled(prop_point_ms: int, hardware_tenths: int) -> int:
    return prop_point_ms * hardware_tenths // 10


def estimate_from_rows(
    relation: RelationFacts,
    server: ServerFacts,
    family: str,
    *,
    extra_inputs: tuple[str, ...] = (),
    calibration: CalibrationFacts | None = None,
) -> DurationEstimate | CannotEstimate:
    """Rows / throughput, scaled to the target's hardware, with the interval
    widened by calibration and staleness.

    Every estimate additionally carries the fixed constant-op overhead
    (10 ms point, 1-100 ms spread): even a zero-row operation locks,
    updates catalogs, and commits. ``calibration`` is the snapshot's probe
    reading; ``None`` (or an unavailable reading) leaves the estimate
    unscaled, and the inputs say so.
    """
    constant = DURATION_CONSTANTS[family]
    if constant.unit is not ConstantUnit.ROWS_PER_SECOND:  # pragma: no cover - table error
        raise ValueError(f"{family} is not a rows/second constant")
    rows = _usable_rows(relation)
    if isinstance(rows, CannotEstimate):
        return rows
    staleness = _staleness(relation, server, rows)
    if isinstance(staleness, CannotEstimate):
        return staleness
    staleness_tenths, age_note, mods_note = staleness
    total_tenths = _base_tenths(constant) * staleness_tenths // 10
    hw_tenths, hw_note = _hardware(calibration)

    low, point, high = _with_overhead(
        _scaled(rows * 1000 // constant.value, hw_tenths), total_tenths
    )
    return DurationEstimate(
        point_ms=point,
        low_ms=low,
        high_ms=high,
        confidence=_confidence(total_tenths),
        method=Method.SIMULATED,
        inputs=(
            f"reltuples={rows} ({age_note})",
            mods_note,
            f"throughput={family} {constant.value} rows/s "
            f"({_calibration_note(constant)})",
            hw_note,
            *extra_inputs,
        ),
        constant_key=family,
    )


def estimate_index_rebuilds(
    relation: RelationFacts,
    server: ServerFacts,
    rebuilds: tuple[tuple[str, str], ...],
    *,
    extra_inputs: tuple[str, ...] = (),
    calibration: CalibrationFacts | None = None,
) -> DurationEstimate | CannotEstimate:
    """Rebuild cost of specific dependent indexes: one heap-scan-and-build per index.

    ``rebuilds`` pairs each index name with its throughput family
    (``index_build_btree`` / ``index_build_expression``). The cost is
    row-proportional, not index-bytes-proportional, because each rebuild
    scans the whole heap and sorts the entries — btree deduplication makes
    the final index's byte size a poor proxy for that work. The weakest
    calibration among the families sets the base widening; ``constant_key``
    names the family of the largest contribution.
    """
    rows = _usable_rows(relation)
    if isinstance(rows, CannotEstimate):
        return rows
    staleness = _staleness(relation, server, rows)
    if isinstance(staleness, CannotEstimate):
        return staleness
    staleness_tenths, age_note, mods_note = staleness

    prop_point = 0
    worst_base = 0
    slowest_key = rebuilds[0][1]
    slowest_part = -1
    parts: list[str] = []
    for index_name, family in rebuilds:
        constant = DURATION_CONSTANTS[family]
        if constant.unit is not ConstantUnit.ROWS_PER_SECOND:  # pragma: no cover
            raise ValueError(f"{family} is not a rows/second constant")
        part = rows * 1000 // constant.value
        prop_point += part
        worst_base = max(worst_base, _base_tenths(constant))
        if part > slowest_part:
            slowest_part = part
            slowest_key = family
        parts.append(
            f"rebuild {index_name}: {family} {constant.value} rows/s "
            f"({_calibration_note(constant)})"
        )
    total_tenths = worst_base * staleness_tenths // 10
    hw_tenths, hw_note = _hardware(calibration)

    low, point, high = _with_overhead(_scaled(prop_point, hw_tenths), total_tenths)
    return DurationEstimate(
        point_ms=point,
        low_ms=low,
        high_ms=high,
        confidence=_confidence(total_tenths),
        method=Method.SIMULATED,
        inputs=(
            f"reltuples={rows} ({age_note})",
            mods_note,
            *parts,
            hw_note,
            *extra_inputs,
        ),
        constant_key=slowest_key,
    )


def estimate_from_bytes(
    size_bytes: int,
    family: str,
    *,
    inputs: tuple[str, ...],
    calibration: CalibrationFacts | None = None,
) -> DurationEstimate:
    """Bytes / throughput for index-sized operations (size is a live fact)."""
    constant = DURATION_CONSTANTS[family]
    if constant.unit is not ConstantUnit.BYTES_PER_SECOND:  # pragma: no cover - table error
        raise ValueError(f"{family} is not a bytes/second constant")
    base_tenths = _base_tenths(constant)
    hw_tenths, hw_note = _hardware(calibration)
    low, point, high = _with_overhead(
        _scaled(size_bytes * 1000 // constant.value, hw_tenths), base_tenths
    )
    return DurationEstimate(
        point_ms=point,
        low_ms=low,
        high_ms=high,
        confidence=_confidence(base_tenths),
        method=Method.SIMULATED,
        inputs=(
            *inputs,
            f"throughput={family} {constant.value} bytes/s "
            f"({_calibration_note(constant)})",
            hw_note,
        ),
        constant_key=family,
    )


def constant_op_estimate(*, method: Method, inputs: tuple[str, ...]) -> DurationEstimate:
    """The fixed-time estimate for catalog-only operations.

    ``method`` is the caller's evidence class for *why* the operation is
    constant-time: PROVEN when the catalog row says CONSTANT outright,
    OBSERVED when a live fact narrowed a worst case down to it.
    """
    constant = DURATION_CONSTANTS["constant_op"]
    low, high = _interval(constant.value, k.WIDEN_CONSTANT_OP_TENTHS)
    return DurationEstimate(
        point_ms=constant.value,
        low_ms=low,
        high_ms=high,
        confidence="medium",
        method=method,
        inputs=(
            *inputs,
            f"fixed constant_op {constant.value} ms ({_calibration_note(constant)})",
        ),
        constant_key="constant_op",
    )


def empty_relation_estimate(basis: str) -> DurationEstimate:
    """Estimate for a relation proven empty (created earlier in this file).

    Even a full rewrite of zero rows is a catalog-time operation, so the
    constant-op spread applies; the method is PROVEN because the emptiness
    follows from the file's own grammar, not from statistics.
    """
    return constant_op_estimate(method=Method.PROVEN, inputs=(basis,))
