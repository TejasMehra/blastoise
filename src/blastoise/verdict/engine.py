"""The risk engine: parsed statements + catalog rows + live snapshot -> verdicts.

Per statement it produces the acquired locks and what they block, a
duration estimate with a confidence interval (or an explicit refusal),
the currently observable contention, reversibility, and a classification —
every piece tagged with its evidence class (:class:`Method`).

Three rules shape everything here:

1. A catalog worst case whose declared live context is missing maps to
   UNKNOWN, never to UNSAFE. Offline runs must not declare everything
   dangerous.
2. UNKNOWN and UNVERIFIED are intended outcomes. There is no fallback
   severity, no default heuristic, and no confidence floor.
3. Statement form alone never drives severity: the same statement is SAFE
   on a small table, UNSAFE on a huge one, and UNKNOWN with no size fact.
4. A tier names the action the reviewer must take, not how alarming the
   statement sounds. Irreversibility alone therefore never leaves the safe
   tiers — it selects SAFE_IRREVERSIBLE over SAFE; only a lock that is
   disruptive at the wrong moment reaches NEEDS_TIMING.
5. The NEEDS_TIMING boundary is drawn on the *lock*, not on which code
   path estimated the duration: acquiring ACCESS EXCLUSIVE on a relation
   that existed before this file needs a window whatever the hold costs
   (:func:`_floor_for_access_exclusive`). Duration decides everything
   above that floor, nothing below it.
6. A threshold inside the estimate's hardware strip is not decided. When
   the strip the constant's known cross-hardware spread draws around the
   point estimate contains a tier threshold that would change the verdict,
   which side the upper bound lands on is a property of the target's
   hardware that the calibration probe could not characterize, not of the
   migration — and the engine says UNKNOWN with that reason rather than
   returning a coin-flip (:func:`_boundary_refusal`). A refused verdict
   costs a reviewer a look; a false BLOCK costs an uninstall.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from blastoise.catalog.model import (
    Calibration,
    DurationModel,
    LockCatalog,
    LockMode,
    TransactionBlock,
)
from blastoise.catalog.resolve import ResolvedLock, resolve
from blastoise.ir import (
    AlterTableActionKind,
    DmlDetails,
    DoBlockDetails,
    IndexDetails,
    InsertDetails,
    MigrationScript,
    ParsedStatement,
    QualifiedName,
    RenameDetails,
    StatementKind,
)
from blastoise.live.model import LiveSnapshot
from blastoise.verdict import constants as k
from blastoise.verdict import reversibility as rev
from blastoise.verdict.constants import DURATION_CONSTANTS
from blastoise.verdict.duration import (
    BYTES_FAMILY_BY_KIND,
    ROWS_FAMILY_BY_KIND,
    constant_op_estimate,
    empty_relation_estimate,
    estimate_from_bytes,
    estimate_from_rows,
    estimate_index_rebuilds,
)
from blastoise.verdict.model import (
    SAFE_TIERS,
    CannotEstimate,
    Classification,
    ContentionAssessment,
    DurationBand,
    DurationEstimate,
    HeldLock,
    Method,
    RelationLockAssessment,
    Reversibility,
    ReversibilityAssessment,
    RowAssessment,
    ScriptAssessment,
    StatementAssessment,
    TransactionWarning,
    Tristate,
    Verdict,
    weakest_method,
    worse_classification,
)
from blastoise.verdict.narrow import (
    NarrowOutcome,
    NarrowResult,
    narrow_action,
    relation_facts_for,
)
from blastoise.verdict.probes import probe_name

_SK = StatementKind
_AK = AlterTableActionKind

_DML_MATCHED = frozenset(
    {_SK.UPDATE, _SK.UPDATE_BATCHED, _SK.DELETE, _SK.DELETE_BATCHED}
)
_DML_ALL_ROWS = frozenset({_SK.UPDATE_WITHOUT_WHERE, _SK.DELETE_WITHOUT_WHERE})
_PURE_READS = frozenset({_SK.SELECT, _SK.SHOW})
_LOADING_CREATES = frozenset({_SK.CREATE_TABLE_AS, _SK.CREATE_MATVIEW})

# pg_locks.mode spellings -> catalog lock modes.
_PG_LOCK_MODES: dict[str, LockMode] = {
    "AccessShareLock": LockMode.ACCESS_SHARE,
    "RowShareLock": LockMode.ROW_SHARE,
    "RowExclusiveLock": LockMode.ROW_EXCLUSIVE,
    "ShareUpdateExclusiveLock": LockMode.SHARE_UPDATE_EXCLUSIVE,
    "ShareLock": LockMode.SHARE,
    "ShareRowExclusiveLock": LockMode.SHARE_ROW_EXCLUSIVE,
    "ExclusiveLock": LockMode.EXCLUSIVE,
    "AccessExclusiveLock": LockMode.ACCESS_EXCLUSIVE,
}


def _blocks_description(conflicts: frozenset[LockMode]) -> str:
    parts: list[str] = []
    if LockMode.ACCESS_SHARE in conflicts:
        parts.append("all reads (SELECT)")
    if LockMode.ROW_EXCLUSIVE in conflicts:
        parts.append("all writes (INSERT/UPDATE/DELETE)")
    if LockMode.SHARE_UPDATE_EXCLUSIVE in conflicts:
        parts.append("VACUUM / ANALYZE / CONCURRENTLY index builds and most DDL")
    if not parts:
        return "nothing but ACCESS EXCLUSIVE DDL"
    return ", ".join(parts)


@dataclass
class _FileState:
    """What the file itself has created so far, keyed by unqualified name.

    Values: ``"clean"`` (created empty; only bounded VALUES inserts since)
    or ``"loaded"`` (bulk-loaded by a query — row volume unknown, but the
    relation is still invisible to production traffic).
    """

    created: dict[str, str] = field(default_factory=dict)
    baseline: bool = False

    def state_of(self, name: QualifiedName | None) -> str | None:
        if self.baseline:
            return "clean"
        if name is None:
            return None
        return self.created.get(name.name)

    def state_of_label(self, label: str | None) -> str | None:
        """Same lookup from a rendered ``schema.name`` string.

        Relations reach the verdict layer as strings once a narrowing has
        added them (the FK referenced table). Names are compared
        unqualified, as everywhere else in the file-local reasoning.
        """
        if self.baseline:
            return "clean"
        if label is None:
            return None
        return self.created.get(label.rsplit(".", 1)[-1])

    def observe(self, statement: ParsedStatement) -> None:
        kind = statement.kind
        target = statement.targets[0].name if statement.targets else None
        if kind in (_SK.CREATE_TABLE, _SK.CREATE_TABLE_PARTITION_OF):
            if target is not None:
                self.created[target] = "clean"
        elif kind in (_SK.CREATE_TABLE_AS, _SK.SELECT_INTO, _SK.CREATE_MATVIEW):
            if target is not None:
                self.created[target] = "loaded"
        elif kind is _SK.DROP_TABLE:
            for name in statement.targets:
                self.created.pop(name.name, None)
        elif kind is _SK.RENAME_TABLE:
            details = statement.details
            if (
                target is not None
                and isinstance(details, RenameDetails)
                and details.new_name is not None
                and target in self.created
            ):
                self.created[details.new_name] = self.created.pop(target)
        elif kind in (_SK.INSERT, _SK.COPY_FROM, _SK.MERGE) and target in self.created:
            details = statement.details
            bounded = isinstance(details, InsertDetails) and details.source in (
                "values",
                "default_values",
            )
            if not bounded:
                self.created[target] = "loaded"
        elif isinstance(statement.details, DoBlockDetails):
            for inner in statement.details.statements:
                self.observe(inner)


@dataclass
class _Context:
    catalog: LockCatalog
    pg_version: int
    snapshot: LiveSnapshot | None
    file_state: _FileState
    resolved_names: dict[str, str]  # snapshot requested -> "schema.name"
    in_explicit_txn: bool


def _resolved_name_map(snapshot: LiveSnapshot | None) -> dict[str, str]:
    if snapshot is None:
        return {}
    out: dict[str, str] = {}
    for relation in snapshot.relations:
        if (
            relation.schema.available
            and relation.name.available
            and isinstance(relation.schema.value, str)
            and isinstance(relation.name.value, str)
        ):
            out[relation.requested] = f"{relation.schema.value}.{relation.name.value}"
    return out


def _relation_assessments(
    ctx: _Context, resolved: ResolvedLock
) -> tuple[RelationLockAssessment, ...]:
    calibrated = resolved.entry.calibration is Calibration.CALIBRATED
    method = Method.PROVEN if calibrated else Method.UNVERIFIED
    basis = (
        f"lock catalog: {resolved.entry.kind.value} on PG {ctx.pg_version} "
        f"({resolved.entry.calibration.value})"
    )
    out: list[RelationLockAssessment] = []
    for relation in resolved.relations:
        conflicts = ctx.catalog.conflicts_with(relation.lock_mode)
        out.append(
            RelationLockAssessment(
                relation=str(relation.name) if relation.name is not None else None,
                role=relation.role,
                lock_mode=relation.lock_mode,
                blocks_reads=relation.blocks_reads,
                blocks_writes=relation.blocks_writes,
                conflicting_modes=tuple(sorted(conflicts, key=lambda m: m.level)),
                certain=relation.certain,
                method=method,
                basis=basis,
            )
        )
    return tuple(out)


def _extra_relation_assessment(
    ctx: _Context, name: str, mode: LockMode, narrative: str
) -> RelationLockAssessment:
    conflicts = ctx.catalog.conflicts_with(mode)
    return RelationLockAssessment(
        relation=name,
        role="referenced_table",
        lock_mode=mode,
        blocks_reads=LockMode.ACCESS_SHARE in conflicts,
        blocks_writes=LockMode.ROW_EXCLUSIVE in conflicts,
        conflicting_modes=tuple(sorted(conflicts, key=lambda m: m.level)),
        certain=True,
        method=Method.OBSERVED,
        basis=narrative,
    )


def _contention_for(
    ctx: _Context, relations: tuple[RelationLockAssessment, ...]
) -> tuple[ContentionAssessment, ...]:
    snapshot = ctx.snapshot
    out: list[ContentionAssessment] = []
    for relation in relations:
        if relation.relation is None or not relation.lock_mode.is_table_lock:
            continue
        conflicts = ctx.catalog.conflicts_with(relation.lock_mode)
        queued = _blocks_description(conflicts)
        if snapshot is None:
            out.append(
                ContentionAssessment(
                    relation=relation.relation,
                    conflicting_lock_held=Tristate(
                        value=None, method=Method.UNVERIFIED, basis="no live snapshot"
                    ),
                    held_conflicting_modes=(),
                    waiting_pids=(),
                    queued_behind_us=queued,
                    queued_behind_us_method=Method.PROVEN,
                )
            )
            continue
        waiters_fact = snapshot.concurrency.lock_waiters
        if not waiters_fact.available or waiters_fact.value is None:
            out.append(
                ContentionAssessment(
                    relation=relation.relation,
                    conflicting_lock_held=Tristate(
                        value=None,
                        method=Method.UNVERIFIED,
                        basis=f"pg_locks facts unavailable: {waiters_fact.reason}",
                    ),
                    held_conflicting_modes=(),
                    waiting_pids=(),
                    queued_behind_us=queued,
                    queued_behind_us_method=Method.PROVEN,
                )
            )
            continue
        resolved_name = ctx.resolved_names.get(relation.relation, relation.relation)
        held_modes: list[str] = []
        pids: list[int] = []
        # Whether the observed conflict is sustained active work or just a
        # queue behind idle-in-transaction holders. It stays False only if
        # every contributing waiter proves its holders are all idle; an
        # actively-running holder, or one whose state could not be read,
        # makes it active (the conservative reading).
        active = False
        for waiter in waiters_fact.value:
            if waiter.relation != resolved_name:
                continue
            contributes = False
            for mode_name in (*waiter.blocking_modes, waiter.blocked_mode):
                mode = _PG_LOCK_MODES.get(mode_name)
                if mode is None or mode in conflicts:
                    held_modes.append(mode_name)
                    contributes = True
            if not contributes:
                continue
            pids.append(waiter.blocked_pid)
            idle = waiter.blockers_all_idle
            if not (idle.available and idle.value is True):
                active = True
        if held_modes:
            idle_note = (
                ""
                if active
                else " — the holders are idle in a transaction, so this is a "
                "queue a lock_timeout and retry clears, not active contention"
            )
            state = Tristate(
                value=True,
                method=Method.OBSERVED,
                basis=(
                    f"pg_locks shows lock traffic on {resolved_name} conflicting "
                    f"with {relation.lock_mode.value}: "
                    f"{', '.join(sorted(set(held_modes)))}{idle_note}"
                ),
            )
        else:
            state = Tristate(
                value=None,
                method=Method.OBSERVED,
                basis=(
                    f"no waiters observed on {resolved_name}; lock holders are only "
                    "visible in pg_locks when someone waits, so an idle holder "
                    "cannot be ruled out"
                ),
            )
        out.append(
            ContentionAssessment(
                relation=relation.relation,
                conflicting_lock_held=state,
                held_conflicting_modes=tuple(sorted(set(held_modes))),
                waiting_pids=tuple(sorted(set(pids))),
                queued_behind_us=queued,
                queued_behind_us_method=Method.PROVEN,
                active_conflict=active,
            )
        )
    return tuple(out)


def _block_type(relations: tuple[RelationLockAssessment, ...]) -> str | None:
    """'reads', 'writes', or None — strongest certain table-level block."""
    blocks_reads = any(r.certain and r.blocks_reads for r in relations)
    if blocks_reads:
        return "reads"
    if any(r.certain and r.blocks_writes for r in relations):
        return "writes"
    return None


def _band(high_ms: int, block: str) -> Classification:
    short, long = (
        (k.FULL_BLOCK_SHORT_MS, k.FULL_BLOCK_LONG_MS)
        if block == "reads"
        else (k.WRITE_BLOCK_SHORT_MS, k.WRITE_BLOCK_LONG_MS)
    )
    if high_ms < short:
        return Classification.SAFE
    if high_ms < long:
        return Classification.NEEDS_TIMING
    return Classification.UNSAFE


def _thresholds(block: str) -> tuple[int, int]:
    if block == "reads":
        return k.FULL_BLOCK_SHORT_MS, k.FULL_BLOCK_LONG_MS
    return k.WRITE_BLOCK_SHORT_MS, k.WRITE_BLOCK_LONG_MS


@dataclass(frozen=True, slots=True)
class _Boundary:
    """A threshold the estimate's hardware strip contains."""

    threshold_ms: int
    spread_tenths: int
    faster: Classification  # the tier on the fast side of the line
    slower: Classification  # the tier on the slow side
    would_be: Classification  # what the upper-bound rule returns today


def _boundary_refusal(
    duration: DurationEstimate, block: str, *, short_matters: bool
) -> _Boundary | None:
    """The threshold inside the estimate's hardware strip, if any.

    The strip is ``[point / S, point * S]`` where ``S`` is the constant's
    known spread (:func:`blastoise.verdict.constants.boundary_spread_tenths`)
    — the residual a hardware-calibrated estimate still carries because
    the probe cannot characterize everything (disk write path, cache
    state, contention). The upper bound ``high_ms`` already includes that
    spread *and* the statistics-staleness widening; what this asks is
    narrower: would the same statement, on a target the probe reads the
    same, land on the other side of a threshold within the spread the
    measured profiles actually showed? If so, the verdict is not the
    migration's property, and the engine must not pretend it is.

    ``short_matters`` is False when the SAFE/NEEDS_TIMING line cannot
    change the outcome (the ACCESS EXCLUSIVE floor lifts SAFE to
    NEEDS_TIMING anyway) — refusing there would be a spurious UNKNOWN.
    A refusal is only ever issued where the two sides of the line are
    different verdicts.
    """
    if duration.constant_key is None:
        return None
    constant = DURATION_CONSTANTS.get(duration.constant_key)
    if constant is None:
        return None
    spread = k.boundary_spread_tenths(constant)
    point = duration.point_ms
    # The fixed overhead is not hardware-proportional and is tiny against
    # any threshold; the strip is drawn around the proportional part.
    point = max(0, point - k.FIXED_OVERHEAD_POINT_MS)
    if point <= 0:
        return None
    lo = point * 10 // spread
    hi = point * spread // 10
    short, long = _thresholds(block)
    would_be = _band(duration.high_ms, block)
    candidates: list[tuple[int, Classification, Classification]] = [
        (long, Classification.NEEDS_TIMING, Classification.UNSAFE)
    ]
    if short_matters:
        candidates.append((short, Classification.SAFE, Classification.NEEDS_TIMING))
    for threshold, faster, slower in candidates:
        if lo < threshold <= hi:
            return _Boundary(
                threshold_ms=threshold,
                spread_tenths=spread,
                faster=faster,
                slower=slower,
                would_be=would_be,
            )
    return None


def _seconds(ms: int) -> str:
    if ms % 1000 == 0:
        return f"{ms // 1000} s"
    return f"{ms / 1000:.1f} s"


def _refused_verdict(
    boundary: _Boundary,
    duration: DurationEstimate,
    *,
    method: Method,
    what: str,
    blocked: str,
) -> Verdict:
    line = (
        "outage line" if boundary.threshold_ms in (k.FULL_BLOCK_LONG_MS, k.WRITE_BLOCK_LONG_MS)
        else "brief-stall line"
    )
    spread = boundary.spread_tenths / 10
    hw_notes = [i for i in duration.inputs if i.startswith("hardware:")]
    hw = hw_notes[0] if hw_notes else "hardware: unscaled"
    return Verdict(
        classification=Classification.UNKNOWN,
        method=method,
        rationale=(
            f"refused at the boundary: {what} blocks {blocked} for an estimated "
            f"{_seconds(duration.point_ms)} (interval {_seconds(duration.low_ms)}-"
            f"{_seconds(duration.high_ms)}), and the {_seconds(boundary.threshold_ms)} "
            f"{line} sits inside the x{spread:.1f} spread this constant showed across the "
            f"hardware profiles that measured it — which side of the line this target "
            f"lands on is decided by hardware the calibration probe cannot characterize "
            f"({hw}), not by the migration; the upper-bound rule would have said "
            f"{boundary.would_be.value}, and on a faster target this is "
            f"{boundary.faster.value}, on a slower one {boundary.slower.value}"
        ),
        conditions=(
            f"time this statement on a production-sized copy of the target before "
            f"deciding between {boundary.faster.value} and {boundary.slower.value}; "
            f"with no measurement, treat it as {boundary.slower.value}",
        ),
        band=duration.band,
        refusal="boundary",
        refused_from=boundary.would_be,
        refused_alternatives=(boundary.faster, boundary.slower),
    )


_HOLD_PHRASE: dict[DurationBand, str] = {
    DurationBand.SUB_SECOND: "a sub-second hold at worst",
    DurationBand.SECONDS: "a hold measured in seconds at worst",
    DurationBand.MINUTES: "a hold measured in minutes at worst",
    DurationBand.LONG: "a hold of an hour or more at worst",
}


def _hold_phrase(band: DurationBand) -> str:
    return _HOLD_PHRASE[band]


def _duration_relation(resolved: ResolvedLock) -> QualifiedName | None:
    if resolved.action is not None and resolved.action.kind is _AK.ATTACH_PARTITION:
        return resolved.action.partition
    return resolved.statement.targets[0] if resolved.statement.targets else None


def _rows_family(resolved: ResolvedLock, override: str | None) -> str | None:
    if override is not None:
        return override
    family = ROWS_FAMILY_BY_KIND.get(resolved.entry.kind)
    if family == "index_build_btree":
        details = resolved.statement.details
        if isinstance(details, IndexDetails) and (
            details.method != "btree" or details.has_expression
        ):
            # Expression and non-btree builds measured 4-5x slower than
            # plain-key btree sorts; the parse tree tells them apart.
            return "index_build_expression"
    return family


def _rows_estimate(
    ctx: _Context, resolved: ResolvedLock, family_override: str | None = None
) -> DurationEstimate | CannotEstimate:
    name = _duration_relation(resolved)
    if name is None:
        return CannotEstimate(
            reason="the statement names no relation to size the work by",
            method=Method.UNVERIFIED,
        )
    state = ctx.file_state.state_of(name)
    if state == "clean":
        return empty_relation_estimate(
            f"{name} was created earlier in this file and only bounded VALUES "
            "rows were inserted: effectively empty"
        )
    if state == "loaded":
        return CannotEstimate(
            reason=(
                f"{name} was created and bulk-loaded earlier in this file; its row "
                "volume follows the loading query"
            ),
            method=Method.UNVERIFIED,
        )
    if ctx.snapshot is None:
        return CannotEstimate(
            reason=f"no live snapshot: the row count of {name} is unknown",
            method=Method.UNVERIFIED,
        )
    family = _rows_family(resolved, family_override)
    if family is None:
        return CannotEstimate(
            reason=(
                f"no throughput constant is defined for {resolved.entry.kind.value}"
            ),
            method=Method.UNVERIFIED,
        )
    facts = relation_facts_for(ctx.snapshot, probe_name(name))
    if facts is None:
        return CannotEstimate(
            reason=f"{name} was not captured in the snapshot",
            method=Method.UNVERIFIED,
        )
    return estimate_from_rows(
        facts, ctx.snapshot.server, family, calibration=ctx.snapshot.calibration
    )


def _bytes_estimate(
    ctx: _Context, resolved: ResolvedLock
) -> DurationEstimate | CannotEstimate:
    name = _duration_relation(resolved)
    family = BYTES_FAMILY_BY_KIND.get(resolved.entry.kind)
    if name is None or family is None:
        return CannotEstimate(
            reason="the statement names no index to size the work by",
            method=Method.UNVERIFIED,
        )
    if ctx.snapshot is None:
        return CannotEstimate(
            reason=f"no live snapshot: the size of {name} is unknown",
            method=Method.UNVERIFIED,
        )
    facts = relation_facts_for(ctx.snapshot, probe_name(name))
    if facts is None:
        return CannotEstimate(
            reason=f"{name} was not captured in the snapshot", method=Method.UNVERIFIED
        )
    size = facts.relation_size_bytes
    if not size.available or not isinstance(size.value, int):
        return CannotEstimate(
            reason=f"size of {name} unavailable: {size.reason}", method=Method.UNVERIFIED
        )
    return estimate_from_bytes(
        size.value,
        family,
        inputs=(f"pg_relation_size({name})={size.value} bytes",),
        calibration=ctx.snapshot.calibration,
    )


def _proportional_verdict(
    ctx: _Context,
    resolved: ResolvedLock,
    relations: tuple[RelationLockAssessment, ...],
    duration: DurationEstimate | CannotEstimate,
    *,
    base_method: Method,
    what: str,
) -> Verdict:
    block = _block_type(relations)
    if block is None:
        band = duration.band if isinstance(duration, DurationEstimate) else None
        note = f" (the work itself runs in the {band.value} band)" if band else ""
        return Verdict(
            classification=Classification.SAFE,
            method=base_method,
            rationale=(
                f"{what} holds no lock that blocks reads or writes on a "
                f"pre-existing relation{note}"
            ),
            band=band,
        )
    if isinstance(duration, CannotEstimate):
        return Verdict(
            classification=Classification.UNKNOWN,
            method=Method.UNVERIFIED,
            rationale=(
                f"{what} blocks {block} for a duration that could not be "
                f"estimated: {duration.reason}"
            ),
        )
    blocked = "reads and writes" if block == "reads" else "writes"
    cls = _band(duration.high_ms, block)
    band = duration.band
    method = weakest_method(base_method, duration.method)
    # The SAFE/NEEDS_TIMING line changes nothing under the ACCESS
    # EXCLUSIVE floor, so a straddle there is not a refusal.
    short_matters = not (block == "reads" and _live_ael_relations(ctx, relations))
    boundary = _boundary_refusal(duration, block, short_matters=short_matters)
    if boundary is not None:
        return _refused_verdict(
            boundary, duration, method=method, what=what, blocked=blocked
        )
    if cls is Classification.SAFE:
        return Verdict(
            classification=cls,
            method=method,
            rationale=(
                f"{what} blocks {blocked} for {_hold_phrase(band)}: a brief stall"
            ),
            band=band,
        )
    if cls is Classification.NEEDS_TIMING:
        return Verdict(
            classification=cls,
            method=method,
            rationale=f"{what} blocks {blocked} for {_hold_phrase(band)}",
            conditions=(
                "needs a low-traffic window or an aggressive lock_timeout with "
                f"retries: {blocked} on the relation stall for the whole run",
            ),
            band=band,
        )
    return Verdict(
        classification=cls,
        method=method,
        rationale=(
            f"{what} blocks {blocked} for {_hold_phrase(band)}: an "
            "outage-length stall"
        ),
        band=band,
    )


def _constant_verdict(
    relations: tuple[RelationLockAssessment, ...],
    *,
    method: Method,
    what: str,
) -> tuple[Verdict, tuple[str, ...]]:
    block = _block_type(relations)
    if block == "reads":
        locked = ", ".join(
            sorted({r.relation for r in relations if r.certain and r.blocks_reads and r.relation})
        ) or "the relation"
        return (
            Verdict(
                classification=Classification.NEEDS_TIMING,
                method=method,
                rationale=(
                    f"{what} is catalog-only but takes a read-and-write-blocking "
                    f"lock on {locked}; the work is brief, the wait for the lock "
                    "may not be"
                ),
                conditions=(
                    "acquisition must be prompt: set lock_timeout with retries or "
                    "run in a low-traffic window — while this statement waits for "
                    "its lock, every later query on the relation queues behind it",
                ),
                band=DurationBand.SUB_SECOND,
            ),
            (),
        )
    if block == "writes":
        return (
            Verdict(
                classification=Classification.SAFE,
                method=method,
                rationale=f"{what} is catalog-only; writers stall only briefly",
                band=DurationBand.SUB_SECOND,
            ),
            (
                "waits for in-flight writers before acquiring its lock; writers "
                "arriving meanwhile queue briefly",
            ),
        )
    return (
        Verdict(
            classification=Classification.SAFE,
            method=method,
            rationale=f"{what} is catalog-only and blocks neither reads nor writes",
            band=DurationBand.SUB_SECOND,
        ),
        (),
    )


def _live_context_reason(resolved: ResolvedLock, *, offline: bool) -> str:
    context = resolved.entry.live_context or "state the statement cannot show"
    prefix = "no live snapshot" if offline else "the snapshot could not supply"
    return f"{prefix}: the catalog row is a worst case needing live context ({context})"


def _assess_row(ctx: _Context, resolved: ResolvedLock) -> RowAssessment:
    entry = resolved.entry
    relations = _relation_assessments(ctx, resolved)
    narrowings: list[str] = []
    notes: list[str] = []
    conditions_extra: tuple[str, ...] = ()
    what = entry.kind.value

    duration: DurationEstimate | CannotEstimate
    verdict: Verdict

    if not entry.available:
        duration = CannotEstimate(
            reason="the form does not exist on this Postgres version", method=Method.PROVEN
        )
        verdict = Verdict(
            classification=Classification.UNSAFE,
            method=Method.PROVEN,
            rationale=(
                f"{what} does not exist on PG {ctx.pg_version}: the statement "
                "fails and aborts the migration"
            ),
        )
    elif (
        entry.lock_mode is LockMode.UNKNOWN
        or entry.duration_model is DurationModel.UNKNOWN
        or entry.calibration is Calibration.UNCALIBRATED
    ):
        why = (
            "the statement form is not modeled"
            if entry.lock_mode is LockMode.UNKNOWN
            else "the catalog row is an uncalibrated stub: its values are provisional"
        )
        duration = CannotEstimate(reason=why, method=Method.UNVERIFIED)
        verdict = Verdict(
            classification=Classification.UNKNOWN,
            method=Method.UNVERIFIED,
            rationale=f"cannot assess {what}: {why}",
        )
    elif entry.transaction_block is TransactionBlock.FORBIDDEN and (
        ctx.in_explicit_txn or resolved.in_do_block
    ):
        where = "a DO block" if resolved.in_do_block else "an explicit transaction"
        duration = CannotEstimate(
            reason="the statement errors before acquiring locks", method=Method.PROVEN
        )
        verdict = Verdict(
            classification=Classification.UNSAFE,
            method=Method.PROVEN,
            rationale=(
                f"{what} cannot run inside a transaction block, but it sits inside "
                f"{where}: it fails and aborts the migration"
            ),
        )
    elif _file_local(ctx, resolved, relations):
        duration = _file_local_duration(ctx, resolved)
        basis = (
            "baseline/squash-shaped file: runs against an empty database"
            if ctx.file_state.baseline
            else "every relation this statement locks was created earlier in this file"
        )
        verdict = Verdict(
            classification=Classification.SAFE,
            method=Method.PROVEN,
            rationale=(
                f"{what} touches only relations this file itself creates ({basis}); "
                "no production traffic can be blocked"
            ),
            band=duration.band if isinstance(duration, DurationEstimate) else None,
        )
        notes.append(basis)
    elif entry.kind in _PURE_READS:
        duration = CannotEstimate(
            reason="rows scanned follow the query; irrelevant to safety — the "
            "statement only reads",
            method=Method.PROVEN,
        )
        verdict = Verdict(
            classification=Classification.SAFE,
            method=Method.PROVEN,
            rationale=f"{what} is read-only under ACCESS SHARE: it blocks nothing",
        )
    elif entry.kind in _LOADING_CREATES:
        duration = CannotEstimate(
            reason="row volume follows the defining query, which is not statically "
            "bounded",
            method=Method.UNVERIFIED,
        )
        verdict = Verdict(
            classification=Classification.NEEDS_TIMING,
            method=Method.PROVEN,
            rationale=(
                f"{what} creates a new object (blocking nothing) but reads its "
                "source for an unbounded duration"
            ),
            conditions=(
                "the defining query's volume bounds the runtime: the transaction "
                "stays open (holding back vacuum) and the WAL burst replicates "
                "for as long as it runs",
            ),
        )
    elif (
        entry.kind is _SK.INSERT
        and entry.duration_model is DurationModel.PROPORTIONAL_TO_ROWS_MATCHED
    ):
        duration = CannotEstimate(
            reason="rows produced by the source query are not statically knowable",
            method=Method.UNVERIFIED,
        )
        verdict = Verdict(
            classification=Classification.NEEDS_TIMING,
            method=Method.PROVEN,
            rationale=(
                "INSERT ... SELECT blocks no reads or writes, but its volume "
                "follows the source query"
            ),
            conditions=(
                "the source query's row volume bounds the runtime, transaction "
                "length, WAL burst, and replication lag",
            ),
        )
    elif entry.kind in _DML_MATCHED:
        duration = CannotEstimate(
            reason="rows matched by the WHERE clause are not statically knowable "
            "(the catalog row says: do not guess)",
            method=Method.UNVERIFIED,
        )
        verdict = _matched_dml_verdict(ctx, resolved)
    elif entry.kind in _DML_ALL_ROWS:
        duration = _rows_estimate(ctx, resolved)
        verdict = _all_rows_dml_verdict(resolved, duration)
    elif entry.requires_live_context:
        narrowed = (
            narrow_action(resolved.action, resolved.statement, ctx.snapshot, ctx.pg_version)
            if ctx.snapshot is not None and resolved.action is not None
            else None
        )
        if ctx.snapshot is None:
            duration = CannotEstimate(
                reason=_live_context_reason(resolved, offline=True),
                method=Method.UNVERIFIED,
            )
            verdict = Verdict(
                classification=Classification.UNKNOWN,
                method=Method.UNVERIFIED,
                rationale=_live_context_reason(resolved, offline=True),
            )
        elif narrowed is None:
            duration = CannotEstimate(
                reason=_live_context_reason(resolved, offline=False),
                method=Method.UNVERIFIED,
            )
            verdict = Verdict(
                classification=Classification.UNKNOWN,
                method=Method.UNVERIFIED,
                rationale=_live_context_reason(resolved, offline=False),
            )
        else:
            relations, duration, verdict, extra = _apply_narrowing(
                ctx, resolved, relations, narrowed
            )
            narrowings.extend(extra[0])
            notes.extend(extra[1])
            conditions_extra = narrowed.conditions
    else:
        duration, verdict = _generic_verdict(ctx, resolved, relations)

    # Conditional-branch cap: a narrowing that attached conditions (the
    # timestamp/timestamptz TimeZone case) means the expensive branch is
    # avoidable — the statement is safe run the right way, which is
    # exactly NEEDS_TIMING, so a worse outcome softens to it.
    if conditions_extra and verdict.classification in (
        Classification.NEEDS_TIMING,
        Classification.UNSAFE,
    ):
        verdict = Verdict(
            classification=Classification.NEEDS_TIMING,
            method=verdict.method,
            rationale=verdict.rationale,
            conditions=(*conditions_extra, *verdict.conditions),
            band=verdict.band,
        )

    contention = _contention_for(ctx, relations)
    verdict = _escalate_for_contention(verdict, contention)
    verdict = _floor_for_access_exclusive(ctx, verdict, relations)

    if resolved.conditional:
        verdict = _cap_for_guard(verdict)

    if entry.failure_mode:
        notes.append(f"failure mode: {entry.failure_mode}")
    if entry.transaction_block is TransactionBlock.RESTRICTED and entry.notes:
        notes.append("transaction caveat applies (see catalog notes)")
    _invalid_index_note(ctx, resolved, notes)

    return RowAssessment(
        kind=entry.kind,
        lock_mode=entry.lock_mode,
        relations=relations,
        duration=duration,
        contention=contention,
        verdict=verdict,
        narrowings=tuple(narrowings),
        notes=tuple(notes),
    )


def _file_local(
    ctx: _Context, resolved: ResolvedLock, relations: tuple[RelationLockAssessment, ...]
) -> bool:
    if ctx.file_state.baseline:
        return True
    certain = [r for r in relations if r.certain and r.lock_mode.is_table_lock]
    if not certain:
        return False
    for relation in certain:
        if relation.relation is None:
            return False
    names = _relation_qnames(resolved)
    return all(ctx.file_state.state_of(name) is not None for name in names)


def _relation_qnames(resolved: ResolvedLock) -> tuple[QualifiedName, ...]:
    return tuple(
        r.name for r in resolved.relations if r.certain and r.name is not None
    )


def _file_local_duration(
    ctx: _Context, resolved: ResolvedLock
) -> DurationEstimate | CannotEstimate:
    if resolved.entry.duration_model is DurationModel.CONSTANT:
        return constant_op_estimate(
            method=Method.PROVEN,
            inputs=("catalog duration model CONSTANT",),
        )
    name = _duration_relation(resolved)
    state = ctx.file_state.state_of(name)
    if state == "loaded":
        return CannotEstimate(
            reason=(
                f"{name} was bulk-loaded earlier in this file; volume follows the "
                "loading query (harmless: the relation is invisible to production)"
            ),
            method=Method.UNVERIFIED,
        )
    return empty_relation_estimate(
        f"{name} is created in this file and effectively empty"
        if name is not None
        else "the file runs against relations it creates itself"
    )


def _matched_dml_verdict(ctx: _Context, resolved: ResolvedLock) -> Verdict:
    name = _duration_relation(resolved)
    worst = _rows_estimate(ctx, resolved)
    details = resolved.statement.details
    batched = (
        isinstance(details, DmlDetails) and bool(details.batch_signals)
    ) or resolved.entry.kind in (_SK.UPDATE_BATCHED, _SK.DELETE_BATCHED)
    if isinstance(worst, CannotEstimate):
        return Verdict(
            classification=Classification.UNKNOWN,
            method=Method.UNVERIFIED,
            rationale=(
                f"row locks only, but neither the matched-row count nor the size "
                f"of {name} is available: {worst.reason}"
            ),
        )
    if worst.high_ms < k.WRITE_BLOCK_SHORT_MS:
        return Verdict(
            classification=Classification.SAFE,
            method=weakest_method(Method.PROVEN, worst.method),
            rationale=(
                f"row locks only, and even if every row of {name} matched, the "
                f"run is {_hold_phrase(worst.band)}"
            ),
            band=worst.band,
        )
    conditions = [
        "the WHERE clause must match a bounded number of rows: matched rows "
        "hold row locks, produce dead tuples, and extend the transaction — "
        f"the worst case (every row of {name}) is {_hold_phrase(worst.band)}"
    ]
    if not batched:
        conditions.append("consider batching (LIMIT-bounded loops or key windows)")
    return Verdict(
        classification=Classification.NEEDS_TIMING,
        method=weakest_method(Method.PROVEN, worst.method),
        rationale=(
            "row locks only — no table-wide block — but the matched-row count is "
            "not statically knowable"
        ),
        conditions=tuple(conditions),
        band=worst.band,
    )


def _all_rows_dml_verdict(
    resolved: ResolvedLock, duration: DurationEstimate | CannotEstimate
) -> Verdict:
    name = resolved.statement.targets[0] if resolved.statement.targets else None
    verb = "rewrites" if resolved.entry.kind is _SK.UPDATE_WITHOUT_WHERE else "deletes"
    if isinstance(duration, CannotEstimate):
        return Verdict(
            classification=Classification.UNKNOWN,
            method=Method.UNVERIFIED,
            rationale=(
                f"the statement {verb} every row of {name}, and its size is "
                f"unavailable: {duration.reason}"
            ),
        )
    cls = _band(duration.high_ms, "writes")
    band = duration.band
    boundary = _boundary_refusal(duration, "writes", short_matters=True)
    if boundary is not None:
        return _refused_verdict(
            boundary,
            duration,
            method=duration.method,
            what=f"the statement ({verb} every row of {name})",
            blocked="writers to every row",
        )
    if cls is Classification.SAFE:
        return Verdict(
            classification=cls,
            method=duration.method,
            rationale=(
                f"{verb} every row of {name}, but the table is small: "
                f"{_hold_phrase(band)}"
            ),
            band=band,
        )
    rationale = (
        f"{verb} every row of {name} in one transaction: every row is locked "
        f"for {_hold_phrase(band)}, dead tuples pile up for the whole table, "
        "and the WAL/replication burst matches"
    )
    if cls is Classification.NEEDS_TIMING:
        return Verdict(
            classification=cls,
            method=duration.method,
            rationale=rationale,
            conditions=("acceptable only in a low-traffic window; prefer batching",),
            band=band,
        )
    return Verdict(
        classification=cls, method=duration.method, rationale=rationale, band=band
    )


def _apply_narrowing(
    ctx: _Context,
    resolved: ResolvedLock,
    relations: tuple[RelationLockAssessment, ...],
    narrowed: NarrowResult,
) -> tuple[
    tuple[RelationLockAssessment, ...],
    DurationEstimate | CannotEstimate,
    Verdict,
    tuple[tuple[str, ...], tuple[str, ...]],
]:
    narrowings: list[str] = []
    notes = list(narrowed.notes)
    if narrowed.narrative:
        narrowings.append(narrowed.narrative)
    if narrowed.extra_relation is not None:
        name, mode = narrowed.extra_relation
        relations = (
            *relations,
            _extra_relation_assessment(ctx, name, mode, narrowed.narrative),
        )
    what = resolved.entry.kind.value

    if narrowed.outcome is NarrowOutcome.UNRESOLVED:
        reason = (
            f"the snapshot could not supply the declared live context: "
            f"{narrowed.reason}"
        )
        duration: DurationEstimate | CannotEstimate = CannotEstimate(
            reason=reason, method=Method.UNVERIFIED
        )
        verdict = Verdict(
            classification=Classification.UNKNOWN,
            method=Method.UNVERIFIED,
            rationale=reason,
        )
        return relations, duration, verdict, (tuple(narrowings), tuple(notes))

    if narrowed.outcome is NarrowOutcome.CONSTANT:
        duration = constant_op_estimate(
            method=narrowed.method,
            inputs=(narrowed.narrative or "narrowed to catalog-time by live facts",),
        )
        verdict, extra_notes = _constant_verdict(
            relations,
            method=weakest_method(Method.PROVEN, narrowed.method),
            what=what,
        )
        notes.extend(extra_notes)
        return relations, duration, verdict, (tuple(narrowings), tuple(notes))

    if narrowed.outcome is NarrowOutcome.KEEP_MODEL:
        duration, verdict = _generic_verdict(ctx, resolved, relations)
        return relations, duration, verdict, (tuple(narrowings), tuple(notes))

    if narrowed.rebuild_indexes:
        duration = _rebuild_estimate(ctx, resolved, narrowed.rebuild_indexes)
    else:
        duration = _rows_estimate(ctx, resolved, narrowed.family_override)
    verdict = _proportional_verdict(
        ctx,
        resolved,
        relations,
        duration,
        base_method=weakest_method(Method.PROVEN, narrowed.method),
        what=what,
    )
    return relations, duration, verdict, (tuple(narrowings), tuple(notes))


def _rebuild_estimate(
    ctx: _Context,
    resolved: ResolvedLock,
    rebuilds: tuple[tuple[str, str], ...],
) -> DurationEstimate | CannotEstimate:
    """Duration of the dependent-index rebuilds a no-rewrite type change forces."""
    name = _duration_relation(resolved)
    if name is None or ctx.snapshot is None:  # pragma: no cover - narrowing needs both
        return CannotEstimate(
            reason="the statement names no relation to size the rebuild by",
            method=Method.UNVERIFIED,
        )
    if ctx.file_state.state_of(name) == "clean":
        return empty_relation_estimate(
            f"{name} was created earlier in this file: rebuilding its indexes "
            "is catalog-time work"
        )
    facts = relation_facts_for(ctx.snapshot, probe_name(name))
    if facts is None:
        return CannotEstimate(
            reason=f"{name} was not captured in the snapshot",
            method=Method.UNVERIFIED,
        )
    return estimate_index_rebuilds(
        facts, ctx.snapshot.server, rebuilds, calibration=ctx.snapshot.calibration
    )


def _generic_verdict(
    ctx: _Context,
    resolved: ResolvedLock,
    relations: tuple[RelationLockAssessment, ...],
) -> tuple[DurationEstimate | CannotEstimate, Verdict]:
    entry = resolved.entry
    what = entry.kind.value
    if entry.duration_model is DurationModel.CONSTANT:
        duration: DurationEstimate | CannotEstimate = constant_op_estimate(
            method=Method.PROVEN, inputs=("catalog duration model CONSTANT",)
        )
        verdict, _extra = _constant_verdict(relations, method=Method.PROVEN, what=what)
        return duration, verdict
    if entry.duration_model is DurationModel.PROPORTIONAL_TO_INDEX_SIZE:
        duration = _bytes_estimate(ctx, resolved)
        return duration, _proportional_verdict(
            ctx, resolved, relations, duration, base_method=Method.PROVEN, what=what
        )
    if entry.duration_model is DurationModel.PROPORTIONAL_TO_ROWS:
        duration = _rows_estimate(ctx, resolved)
        return duration, _proportional_verdict(
            ctx, resolved, relations, duration, base_method=Method.PROVEN, what=what
        )
    # PROPORTIONAL_TO_ROWS_MATCHED that no special branch claimed.
    duration = CannotEstimate(
        reason="rows matched by the statement are not statically knowable",
        method=Method.UNVERIFIED,
    )
    return duration, Verdict(
        classification=Classification.UNKNOWN,
        method=Method.UNVERIFIED,
        rationale=f"{what}: the work scales with matched rows, which cannot be known",
    )


def _live_ael_relations(
    ctx: _Context, relations: tuple[RelationLockAssessment, ...]
) -> tuple[str, ...]:
    """Certain ACCESS EXCLUSIVE locks on relations the file did not create.

    Whether such a lock is *held* briefly is a duration question; whether
    it must be *acquired* is not. Acquiring ACCESS EXCLUSIVE on a relation
    that existed before this file ran means queueing behind every open
    transaction touching it, and parking every later query behind that
    wait — the cost is the queue, and the queue does not shrink because
    the work is short.

    Relations this file created are exempt: nothing else can hold a lock
    on a relation nothing else has seen. A baseline/squash-shaped file
    counts as creating everything it touches.
    """
    if ctx.file_state.baseline:
        return ()
    names: list[str] = []
    for relation in relations:
        if not relation.certain or relation.lock_mode is not LockMode.ACCESS_EXCLUSIVE:
            continue
        if ctx.file_state.state_of_label(relation.relation) is not None:
            continue
        names.append(relation.relation or "an unnamed relation")
    return tuple(dict.fromkeys(names))


def _floor_for_access_exclusive(
    ctx: _Context, verdict: Verdict, relations: tuple[RelationLockAssessment, ...]
) -> Verdict:
    """Floor any safe-tier verdict that takes a live ACCESS EXCLUSIVE lock.

    The tier boundary is drawn on the *lock*, not on which code path
    estimated the duration. Before this floor existed, a brief hold
    reached through :func:`_constant_verdict` became NEEDS_TIMING while
    the identical lock reached through :func:`_proportional_verdict` on a
    small table stayed SAFE — so at 1k rows a pure relabel read
    NEEDS_TIMING and an actual heap rewrite read SAFE, both holding
    ACCESS EXCLUSIVE. Duration still decides everything above this floor
    (seconds and minutes still reach UNSAFE); it no longer decides
    whether an ACCESS EXCLUSIVE acquisition needs a window at all.

    This is a floor, never an escalation: it runs after
    :func:`_escalate_for_contention` so it can lift SAFE and
    SAFE_IRREVERSIBLE to NEEDS_TIMING and can move nothing into UNSAFE.
    """
    if verdict.classification not in SAFE_TIERS:
        return verdict
    locked = _live_ael_relations(ctx, relations)
    if not locked:
        return verdict
    names = ", ".join(locked)
    return Verdict(
        classification=Classification.NEEDS_TIMING,
        method=verdict.method,
        rationale=(
            f"{verdict.rationale}; it takes ACCESS EXCLUSIVE on {names}, which "
            "existed before this file — the wait for that lock queues behind "
            "every open transaction on the relation, and every later query "
            "queues behind the wait"
        ),
        conditions=(
            "acquisition must be prompt: set lock_timeout with retries or run "
            f"in a low-traffic window — ACCESS EXCLUSIVE on {names} blocks "
            "reads and writes from the moment it is requested",
            *verdict.conditions,
        ),
        band=verdict.band,
    )


def _escalate_for_contention(
    verdict: Verdict, contention: tuple[ContentionAssessment, ...]
) -> Verdict:
    observed = [c for c in contention if c.conflicting_lock_held.value is True]
    if not observed or verdict.classification in (
        Classification.UNKNOWN,
        Classification.UNSAFE,
    ):
        return verdict
    # Observed contention is a timing problem: it lifts either safe tier to
    # NEEDS_TIMING (irreversibility is orthogonal to who else holds a lock,
    # so SAFE_IRREVERSIBLE escalates the same way SAFE does). It pushes an
    # already-timed statement to UNSAFE only when the conflict is *active* —
    # a backend running work behind the lock, sustained contention a
    # lock_timeout and retry will keep losing to. When the observed conflict
    # is only idle-in-transaction holders, the wait is transient (it clears
    # the moment they commit or roll back), which is exactly what the
    # NEEDS_TIMING remedy already prescribes, so one-tier escalation on an
    # idle holder — the acquisition-queue risk the statement is already
    # flagged for — must not compound to UNSAFE. The engine cannot see how
    # long an idle holder will hold, so a long idle hold that happens to
    # exceed the outage threshold is missed here; that is inherent to a
    # snapshot, not a tuning choice.
    active = any(c.active_conflict for c in observed)
    if verdict.classification in SAFE_TIERS:
        escalated = Classification.NEEDS_TIMING
    elif active:
        escalated = Classification.UNSAFE
    else:
        return verdict
    names = ", ".join(sorted(c.relation for c in observed))
    kind = "conflicting lock traffic" if active else "an idle-in-transaction holder"
    tail = (
        "running now queues behind it, and everything else queues behind this statement"
        if active
        else "running now queues behind it until it commits or rolls back — set "
        "lock_timeout and retry, or run in a low-traffic window"
    )
    return Verdict(
        classification=escalated,
        method=weakest_method(verdict.method, Method.OBSERVED),
        rationale=(
            f"{verdict.rationale}; escalated: pg_locks currently shows {kind} on "
            f"{names} — {tail}"
        ),
        conditions=verdict.conditions,
        band=verdict.band,
    )


def _cap_for_guard(verdict: Verdict) -> Verdict:
    """Cap an existence-guarded statement at NEEDS_TIMING, both ways.

    A guarded statement's *whether* is undetermined until the deploy runs,
    so the reviewer has something to check either way: a guarded UNSAFE
    must be shown not to fire, and a guarded SAFE may silently not happen
    at all. Retargeting the pre-existing cap from CONDITIONALLY_SAFE to
    NEEDS_TIMING keeps that behavior unchanged under the new tiers.

    Judgment call: the strict reading of "what must the reviewer DO" would
    let a guarded SAFE stay SAFE (it needs no action whichever way the
    guard falls) and leave a guarded UNSAFE at UNSAFE. That would be a
    behavior change beyond the tier split, so the cap is preserved; the
    guarded population is small (23 of 129 wild DO blocks).
    """
    if verdict.classification is Classification.UNKNOWN:
        return verdict
    guard = (
        "guarded by an existence check (information_schema / pg_catalog probe): "
        "the statement may not execute at all"
    )
    if verdict.classification is Classification.UNSAFE:
        return Verdict(
            classification=Classification.NEEDS_TIMING,
            method=verdict.method,
            rationale=verdict.rationale,
            conditions=(
                guard,
                f"if the guard passes and it executes: UNSAFE — {verdict.rationale}",
                *verdict.conditions,
            ),
            band=verdict.band,
        )
    return Verdict(
        classification=Classification.NEEDS_TIMING,
        method=verdict.method,
        rationale=verdict.rationale,
        conditions=(guard, *verdict.conditions),
        band=verdict.band,
    )


def _invalid_index_note(
    ctx: _Context, resolved: ResolvedLock, notes: list[str]
) -> None:
    if resolved.entry.kind not in (_SK.CREATE_INDEX, _SK.CREATE_INDEX_CONCURRENTLY):
        return
    if ctx.snapshot is None or not resolved.statement.targets:
        return
    facts = relation_facts_for(ctx.snapshot, probe_name(resolved.statement.targets[0]))
    if facts is None or not facts.invalid_indexes.available:
        return
    invalid = facts.invalid_indexes.value or ()
    details = resolved.statement.details
    wanted = details.index_name if isinstance(details, IndexDetails) else None
    if wanted is not None and wanted in invalid:
        notes.append(
            f"observed: an INVALID index named {wanted!r} already exists on the "
            "table (a failed CONCURRENTLY build?) — creation fails until it is "
            "dropped"
        )
    elif invalid:
        notes.append(
            f"observed: the table carries INVALID index(es) {', '.join(invalid)} "
            "from failed CONCURRENTLY builds"
        )


def _statement_reversibility(
    statement: ParsedStatement,
    rows: tuple[RowAssessment, ...],
    resolved_rows: tuple[ResolvedLock, ...],
    relabels: set[int],
) -> ReversibilityAssessment:
    parts: list[ReversibilityAssessment] = []
    for index, resolved in enumerate(resolved_rows):
        if resolved.action is not None:
            part = rev.action_reversibility(resolved.action.kind)
            if index in relabels:
                part = ReversibilityAssessment(
                    reversibility=Reversibility.REVERSIBLE,
                    method=Method.OBSERVED,
                    basis=(
                        "live: the type change is a pure relabel (no rewrite, "
                        "values unchanged) — altering back restores the exact state"
                    ),
                )
            parts.append(part)
        else:
            parts.append(rev.statement_reversibility(resolved.statement.kind))
    if not parts:
        return rev.statement_reversibility(statement.kind)
    return rev.combine(tuple(parts))


def assess_script(
    script: MigrationScript,
    catalog: LockCatalog,
    pg_version: int,
    snapshot: LiveSnapshot | None = None,
) -> ScriptAssessment:
    """Assess every statement of a parsed migration file.

    ``snapshot`` is optional: offline, every size- or state-dependent
    judgment degrades to UNKNOWN (never to UNSAFE).
    """
    file_state = _FileState(baseline=script.baseline_shaped)
    resolved_names = _resolved_name_map(snapshot)

    explicit_group_of: dict[int, int] = {}
    for explicit_index, group in enumerate(script.transaction_groups):
        if group.explicit:
            for statement_index in group.statement_indices:
                explicit_group_of[statement_index] = explicit_index
    group_locks: dict[int, list[HeldLock]] = {}

    assessments: list[StatementAssessment] = []
    all_ael_relations: dict[str, int] = {}
    has_forbidden_outside_txn = False

    for index, statement in enumerate(script.statements):
        group_index = explicit_group_of.get(index)
        ctx = _Context(
            catalog=catalog,
            pg_version=pg_version,
            snapshot=snapshot,
            file_state=file_state,
            resolved_names=resolved_names,
            in_explicit_txn=group_index is not None,
        )
        resolved_rows = resolve(catalog, statement, pg_version)
        held = tuple(group_locks.get(group_index, ())) if group_index is not None else ()

        rows: list[RowAssessment] = []
        relabels: set[int] = set()
        for row_index, resolved in enumerate(resolved_rows):
            assessed = _assess_row(ctx, resolved)
            if _narrow_relabel(assessed):
                relabels.add(row_index)
            rows.append(assessed)

        notes: list[str] = []
        if rows:
            deciding = rows[0]
            combined = deciding.verdict
            for row in rows[1:]:
                worse = worse_classification(
                    combined.classification, row.verdict.classification
                )
                if worse is not combined.classification:
                    deciding = row
                    combined = row.verdict
        else:
            combined = _no_rows_verdict(statement)

        details = statement.details
        if isinstance(details, DoBlockDetails) and (
            details.dynamic_sql_count > 0 or not details.fully_parsed
        ):
            opaque = Verdict(
                classification=Classification.UNKNOWN,
                method=Method.UNVERIFIED,
                rationale=(
                    "the DO block builds SQL at runtime or its body could not be "
                    "fully parsed; its complete effect is unknown"
                ),
            )
            if (
                worse_classification(
                    combined.classification, opaque.classification
                )
                is Classification.UNKNOWN
                and combined.classification is not Classification.UNSAFE
            ):
                combined = opaque
            notes.append(opaque.rationale)

        reversibility = _statement_reversibility(statement, tuple(rows), resolved_rows, relabels)

        # Irreversibility selects *which safe tier* a safe statement lands
        # in; it never pushes one out of the safe tiers. This is why the
        # tier exists: an enum label addition or a drop of a relation this
        # file created costs the reviewer a note, not a maintenance window.
        # A statement that already needs timing (or worse) keeps that tier —
        # the stronger required action owns the headline — and the loss
        # stays on the statement's ReversibilityAssessment either way.
        #
        # Note there is deliberately no file-local exemption any more. The
        # old engine suppressed the irreversibility floor for relations the
        # file itself created, because CONDITIONALLY_SAFE was too heavy a
        # penalty for dropping a table nobody could see. SAFE_IRREVERSIBLE
        # costs nothing to say, so the honest fact ("this has no undo") is
        # recorded instead of hidden.
        if (
            combined.classification is Classification.SAFE
            and reversibility.reversibility is Reversibility.IRREVERSIBLE
        ):
            combined = Verdict(
                classification=Classification.SAFE_IRREVERSIBLE,
                method=weakest_method(combined.method, reversibility.method),
                rationale=combined.rationale,
                conditions=(
                    "irreversible once committed: "
                    f"{reversibility.what_is_lost or 'state is lost'} — record "
                    "it; there is no undo",
                    *combined.conditions,
                ),
                band=combined.band,
            )

        blocking_held = [
            lock for lock in held if lock.lock_mode.level >= LockMode.SHARE.level
        ]
        if blocking_held:
            held_desc = ", ".join(
                f"{lock.lock_mode.value} on {lock.relation or '<unnamed>'} "
                f"(statement {lock.acquired_by_index + 1})"
                for lock in blocking_held
            )
            notes.append(
                f"runs while the transaction still holds: {held_desc} — those "
                "locks stay held until COMMIT, so this statement's runtime "
                "extends theirs"
            )
            long_running = any(
                not (
                    isinstance(row.duration, DurationEstimate)
                    and row.duration.constant_key == "constant_op"
                )
                for row in rows
            )
            if long_running and combined.classification in SAFE_TIERS:
                combined = Verdict(
                    classification=Classification.NEEDS_TIMING,
                    method=combined.method,
                    rationale=combined.rationale,
                    conditions=(
                        "this statement itself is safe, but while it runs the "
                        f"transaction keeps holding {held_desc}",
                        *combined.conditions,
                    ),
                    band=combined.band,
                )

        statement_mode = LockMode.NONE
        for row in rows:
            if row.lock_mode.is_table_lock and row.lock_mode.level > statement_mode.level:
                statement_mode = row.lock_mode

        assessments.append(
            StatementAssessment(
                statement_index=index,
                kind=statement.kind,
                line=statement.span.line,
                sql=statement.sql,
                statement_lock_mode=statement_mode,
                rows=tuple(rows),
                reversibility=reversibility,
                verdict=combined,
                held_locks_before=held,
                notes=tuple(notes),
            )
        )

        # Bookkeeping for later statements.
        if group_index is not None:
            bucket = group_locks.setdefault(group_index, [])
            for resolved in resolved_rows:
                for relation in resolved.relations:
                    if (
                        relation.certain
                        and relation.lock_mode.is_table_lock
                        and relation.lock_mode.level >= LockMode.ROW_EXCLUSIVE.level
                    ):
                        bucket.append(
                            HeldLock(
                                relation=str(relation.name)
                                if relation.name is not None
                                else None,
                                lock_mode=relation.lock_mode,
                                acquired_by_index=index,
                            )
                        )
        for resolved in resolved_rows:
            entry = resolved.entry
            if (
                entry.transaction_block is TransactionBlock.FORBIDDEN
                and group_index is None
                and not resolved.in_do_block
            ):
                has_forbidden_outside_txn = True
            for relation in resolved.relations:
                if (
                    relation.certain
                    and relation.name is not None
                    and relation.lock_mode is LockMode.ACCESS_EXCLUSIVE
                    and not resolved.conditional
                ):
                    all_ael_relations.setdefault(str(relation.name), index)
        file_state.observe(statement)

    warnings = _transaction_warnings(
        script, catalog, pg_version, all_ael_relations
    )
    script_notes: list[str] = []
    if script.baseline_shaped:
        script_notes.append(
            "baseline/squash-shaped file: assessed as running against an empty "
            "database; size-based hazards do not apply"
        )
    if has_forbidden_outside_txn and not script.has_explicit_transactions:
        script_notes.append(
            "contains statements that fail inside a transaction block: the "
            "migration runner must not wrap this file in one"
        )

    return ScriptAssessment(
        statements=tuple(assessments),
        transaction_warnings=warnings,
        pg_version=pg_version,
        online=snapshot is not None,
        baseline_shaped=script.baseline_shaped,
        notes=tuple(script_notes),
    )


def _narrow_relabel(assessed: RowAssessment) -> bool:
    return any("no table rewrite" in n for n in assessed.narrowings)


def _no_rows_verdict(statement: ParsedStatement) -> Verdict:
    if isinstance(statement.details, DoBlockDetails):
        return Verdict(
            classification=Classification.UNKNOWN,
            method=Method.UNVERIFIED,
            rationale="the DO block yielded no analyzable inner statements",
        )
    return Verdict(  # pragma: no cover - catalog covers every kind
        classification=Classification.UNKNOWN,
        method=Method.UNVERIFIED,
        rationale="no catalog rows resolved for this statement",
    )


def _transaction_warnings(
    script: MigrationScript,
    catalog: LockCatalog,
    pg_version: int,
    all_ael_relations: dict[str, int],
) -> tuple[TransactionWarning, ...]:
    warnings: list[TransactionWarning] = []
    for group_index, group in enumerate(script.transaction_groups):
        if not group.explicit:
            continue
        ael: dict[str, int] = {}
        for statement_index in group.statement_indices:
            statement = script.statements[statement_index]
            for resolved in resolve(catalog, statement, pg_version):
                for relation in resolved.relations:
                    if (
                        relation.certain
                        and relation.name is not None
                        and relation.lock_mode is LockMode.ACCESS_EXCLUSIVE
                        and not resolved.conditional
                    ):
                        ael.setdefault(str(relation.name), statement_index)
        if len(ael) >= 2:
            names = tuple(sorted(ael))
            warnings.append(
                TransactionWarning(
                    relations=names,
                    description=(
                        "one transaction accumulates ACCESS EXCLUSIVE on "
                        f"{len(names)} relations ({', '.join(names)}): all of them "
                        "stay fully blocked until COMMIT, and multi-relation lock "
                        "acquisition in one transaction invites deadlock with "
                        "concurrent workloads"
                    ),
                    method=Method.PROVEN,
                    group_index=group_index,
                )
            )
    if not script.has_explicit_transactions and len(all_ael_relations) >= 2:
        names = tuple(sorted(all_ael_relations))
        warnings.append(
            TransactionWarning(
                relations=names,
                description=(
                    "if the migration runner wraps this file in a single "
                    f"transaction, it accumulates ACCESS EXCLUSIVE on {len(names)} "
                    f"relations ({', '.join(names)}) held until COMMIT"
                ),
                method=Method.PROVEN,
                hypothetical=True,
            )
        )
    return tuple(warnings)
