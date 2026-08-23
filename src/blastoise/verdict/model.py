"""Typed output model of the risk engine.

Every conclusion the engine emits carries a :class:`Method` tag saying what
kind of evidence produced it. The tag is a required constructor argument on
every conclusion type — there is deliberately no default, so no code path
can produce a conclusion without stating its evidence class.

``UNKNOWN`` classifications and ``UNVERIFIED`` methods are intended,
reachable outcomes: where a fact is missing or a value is not derivable,
the engine says so instead of guessing. Nothing in this module (or the
engine) supplies a fallback severity or a confidence floor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum, auto

from blastoise.catalog.model import LockMode
from blastoise.ir import AlterTableActionKind, StatementKind
from blastoise.verdict import constants as _k


class Method(StrEnum):
    """Evidence class of one conclusion.

    ``PROVEN`` — follows from the grammar and the researched lock catalog
    alone (lock modes, conflict sets, statically decidable rewrites).
    ``OBSERVED`` — read directly from the live snapshot (a constraint row
    exists, a conflicting lock is held, the relation is absent).
    ``SIMULATED`` — produced by a model over live statistics (duration
    estimates from reltuples and a throughput constant). Staleness of the
    inputs reduces confidence but never upgrades the method.
    ``UNVERIFIED`` — asserted without sufficient evidence: uncalibrated
    catalog stubs, opaque bodies, missing facts.
    """

    PROVEN = auto()
    OBSERVED = auto()
    SIMULATED = auto()
    UNVERIFIED = auto()


# Evidentiary strength, strongest first. A conclusion combining several
# pieces of evidence is only as strong as its weakest contributor.
_STRENGTH: dict[Method, int] = {
    Method.PROVEN: 3,
    Method.OBSERVED: 2,
    Method.SIMULATED: 1,
    Method.UNVERIFIED: 0,
}


def weakest_method(first: Method, *rest: Method) -> Method:
    """The weakest evidence class among the contributors."""
    result = first
    for method in rest:
        if _STRENGTH[method] < _STRENGTH[result]:
            result = method
    return result


class Classification(StrEnum):
    """What the reviewer must DO about one statement.

    Called **Pressure Levels** in anything a person reads; the member
    values here are the machine contract and never carry the theme.
    :func:`pressure_level` maps a member to its display name.

    The tiers are ordered by the action they demand, not by how severe
    they sound — placement is decided by the required action alone:

    ``SAFE`` — no reviewer action.
    ``SAFE_IRREVERSIBLE`` — proceed, but there is no undo: record the
    fact. Enum label additions; drops of relations that are empty or
    that this file created. Irreversibility on its own does not make a
    statement unsafe — it decides *which* safe tier the statement lands
    in, never whether it is safe.
    ``NEEDS_TIMING`` — safe in itself, disruptive at the wrong moment:
    run it off-peak, or behind a lock_timeout with retries. *Any*
    ACCESS EXCLUSIVE acquisition on a relation that existed before the
    file (the work may be constant; the queue behind the acquisition is
    not, and it does not shrink because the hold is short), and anything
    whose worst plausible hold bands to seconds or longer on a live
    relation.
    ``UNSAFE`` — do not run as written.
    ``UNKNOWN`` — cannot determine.

    This replaced a single ``CONDITIONALLY_SAFE`` tier that had grown to
    ~27.5% of the wild corpus while holding three unrelated things —
    harmless-but-irreversible operations, brief ACCESS EXCLUSIVE locks,
    and genuinely disruptive multi-second write blocks. A tier that
    broad gets skimmed; splitting it by required action does not.
    """

    SAFE = auto()
    SAFE_IRREVERSIBLE = auto()
    NEEDS_TIMING = auto()
    UNSAFE = auto()
    UNKNOWN = auto()


SAFE_TIERS: frozenset[Classification] = frozenset(
    {Classification.SAFE, Classification.SAFE_IRREVERSIBLE}
)
"""The tiers that clear a statement to run as written, unscheduled.

Both demand nothing of *how* the statement is run; ``SAFE_IRREVERSIBLE``
only asks that the absence of an undo be recorded. Used by the engine's
escalation paths, which must lift a statement out of both together.
"""


PRESSURE_LEVELS: dict[Classification, str] = {
    Classification.SAFE: "Calm Water",
    Classification.SAFE_IRREVERSIBLE: "One-Way Current",
    Classification.NEEDS_TIMING: "Rain Check",
    Classification.UNSAFE: "Hydro Pump",
    Classification.UNKNOWN: "Fog",
}
"""Display names for the five tiers — presentation only.

This is the one place the product's theme touches the verdict layer, and
it is a lookup table pointed *at* the enum rather than anything stored in
it. ``Classification`` values stay ``safe`` / ``safe_irreversible`` /
``needs_timing`` / ``unsafe`` / ``unknown`` in JSON, in exit codes, and in
every comparison; someone wiring this into CI reads the machine value and
never needs to know what a Hydro Pump is. Renaming a level here is a
copy change. Renaming one in the enum would be a breaking API change, and
a test pins the enum values against exactly that.
"""


def pressure_level(classification: Classification) -> str:
    """The display name of a tier, for human-readable output only.

    Never use this as a key, a filename, or a comparison target — use the
    :class:`Classification` member itself.
    """
    return PRESSURE_LEVELS[classification]


class DurationBand(StrEnum):
    """The coarse order-of-magnitude of the worst plausible lock hold.

    Derived from the duration interval's *upper* bound. This is the
    primary duration reading: the 2026-08-21 scale harness showed the
    numeric intervals are ~16x wide at median — too wide to drive a
    decision — while the upper bound's band tracked the measured reality.
    The numeric interval stays available on :class:`DurationEstimate` as
    secondary detail.
    """

    SUB_SECOND = auto()
    SECONDS = auto()
    MINUTES = auto()
    LONG = auto()  # an hour or more


def duration_band(high_ms: int) -> DurationBand:
    if high_ms < _k.BAND_SUB_SECOND_MAX_MS:
        return DurationBand.SUB_SECOND
    if high_ms < _k.BAND_SECONDS_MAX_MS:
        return DurationBand.SECONDS
    if high_ms < _k.BAND_MINUTES_MAX_MS:
        return DurationBand.MINUTES
    return DurationBand.LONG


# Ordering for combining the rows of one statement: a statement is as bad
# as its worst part, and a part we cannot assess (UNKNOWN) outranks one
# that merely needs timing — a statement is never called safe while a
# piece of it is undecided. The ladder is the *action* ladder: nothing,
# then record a fact, then schedule it, then investigate, then stop.
_COMBINE_RANK: dict[Classification, int] = {
    Classification.SAFE: 0,
    Classification.SAFE_IRREVERSIBLE: 1,
    Classification.NEEDS_TIMING: 2,
    Classification.UNKNOWN: 3,
    Classification.UNSAFE: 4,
}


def worse_classification(first: Classification, *rest: Classification) -> Classification:
    result = first
    for cls in rest:
        if _COMBINE_RANK[cls] > _COMBINE_RANK[result]:
            result = cls
    return result


@dataclass(frozen=True, slots=True)
class Verdict:
    """One classification with its evidence class and reasoning.

    The primary assessment is ``classification`` plus ``band`` — the
    coarse order-of-magnitude of the worst plausible lock hold, derived
    from the duration interval's upper bound. ``band`` is ``None`` exactly
    when no bounded duration underlies the verdict (UNKNOWN outcomes,
    proven failures, unbounded source-query volumes); the numeric interval
    itself lives on the row's :class:`DurationEstimate` as secondary
    detail and is deliberately kept out of the headline.

    ``method`` has no default on purpose: constructing a verdict without
    stating its evidence class is a TypeError, and a test pins that.
    ``conditions`` name what must hold, or what must be recorded, for a
    verdict outside ``SAFE``: the timing requirement for NEEDS_TIMING,
    the exact loss for SAFE_IRREVERSIBLE.

    ``refusal`` is set on an UNKNOWN that is a deliberate refusal to
    decide rather than a missing fact — today only ``"boundary"``: the
    estimate straddles a tier threshold by less than the constant's known
    hardware spread (see ``_boundary_refusal`` in the engine). For such a
    verdict ``refused_from`` records the tier the upper-bound rule would
    have returned, so a consumer (and the validation harness) can see
    what the refusal replaced; ``refused_alternatives`` are the two tiers
    the statement would land in on the fast and the slow side of the
    line. Both are ``None``/empty for every other verdict.
    """

    classification: Classification
    method: Method
    rationale: str
    conditions: tuple[str, ...] = ()
    band: DurationBand | None = None
    refusal: str | None = None
    refused_from: Classification | None = None
    refused_alternatives: tuple[Classification, ...] = ()


@dataclass(frozen=True, slots=True)
class RelationLockAssessment:
    """The lock this row takes on one relation, and what that lock stops.

    ``relation`` is ``None`` when the role cannot be named statically (the
    table owning a dropped index, FK referencers of a truncated table).
    ``certain`` is False for locks taken only if such relations exist.
    """

    relation: str | None
    role: str
    lock_mode: LockMode
    blocks_reads: bool
    blocks_writes: bool
    conflicting_modes: tuple[LockMode, ...]
    certain: bool
    method: Method
    basis: str


@dataclass(frozen=True, slots=True)
class DurationEstimate:
    """A modeled lock-hold duration: a coarse band plus a numeric interval.

    ``band`` (a property, derived from ``high_ms``) is the primary
    reading; the numeric interval is secondary detail — the intervals are
    wide by construction (~16x median at the harness), so the numbers
    inform but must not headline. All values are integer milliseconds.
    ``inputs`` states every input the model consumed, including the
    staleness of the statistics; the interval widens with staleness and
    with the calibration state of the throughput constant
    (``constant_key`` names the row in the duration-constants table).
    ``confidence`` is the qualitative reading of the interval width:
    ``high`` (≤2x each way), ``medium`` (≤6x), ``low``.
    """

    point_ms: int
    low_ms: int
    high_ms: int
    confidence: str
    method: Method
    inputs: tuple[str, ...]
    constant_key: str | None

    @property
    def band(self) -> DurationBand:
        return duration_band(self.high_ms)


@dataclass(frozen=True, slots=True)
class CannotEstimate:
    """The explicit refusal to estimate, with the specific missing input."""

    reason: str
    method: Method


@dataclass(frozen=True, slots=True)
class Tristate:
    """A yes/no/unknown answer with evidence. ``value`` None = unknown."""

    value: bool | None
    method: Method
    basis: str


@dataclass(frozen=True, slots=True)
class ContentionAssessment:
    """Current contention on one relation, read from the snapshot.

    ``conflicting_lock_held`` answers "does anything hold a lock right now
    that this statement's lock conflicts with"; unknown offline or when
    ``pg_locks`` facts were unavailable. ``queued_behind_us`` is what would
    queue behind this statement while it waits for or holds its lock —
    derived from the conflict matrix, so it is PROVEN even offline.
    """

    relation: str
    conflicting_lock_held: Tristate
    held_conflicting_modes: tuple[str, ...]
    waiting_pids: tuple[int, ...]
    queued_behind_us: str
    queued_behind_us_method: Method
    # True when an observed conflicting holder is actively running, False
    # when the observed conflict comes only from idle-in-transaction holders
    # (a transient wait a lock_timeout + retry clears). Meaningful only when
    # ``conflicting_lock_held`` is True; defaults True so that an
    # unobserved or stats-masked conflict keeps the stricter escalation.
    active_conflict: bool = True


class Reversibility(StrEnum):
    REVERSIBLE = auto()
    IRREVERSIBLE = auto()
    UNKNOWN = auto()


@dataclass(frozen=True, slots=True)
class ReversibilityAssessment:
    """Whether the committed statement can be undone, and what is lost.

    ``what_is_lost`` is exact and non-empty for IRREVERSIBLE; ``basis``
    carries the reasoning (or, for UNKNOWN, why it is not derivable).
    """

    reversibility: Reversibility
    method: Method
    basis: str
    what_is_lost: str | None = None


@dataclass(frozen=True, slots=True)
class RowAssessment:
    """Assessment of one catalog row: a statement or one ALTER TABLE action."""

    kind: StatementKind | AlterTableActionKind
    lock_mode: LockMode
    relations: tuple[RelationLockAssessment, ...]
    duration: DurationEstimate | CannotEstimate
    contention: tuple[ContentionAssessment, ...]
    verdict: Verdict
    narrowings: tuple[str, ...] = ()  # live facts applied, human-readable
    notes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class HeldLock:
    """A lock an earlier statement of the same transaction still holds."""

    relation: str | None
    lock_mode: LockMode
    acquired_by_index: int  # index into MigrationScript.statements


@dataclass(frozen=True, slots=True)
class StatementAssessment:
    """The full per-statement result.

    ``statement_lock_mode`` is what Postgres takes for the whole statement
    (the strictest of any subcommand, per AlterTableGetLockLevel).
    ``held_locks_before`` are locks earlier statements in the same explicit
    transaction still hold while this one runs.
    """

    statement_index: int
    kind: StatementKind
    line: int
    sql: str
    statement_lock_mode: LockMode
    rows: tuple[RowAssessment, ...]
    reversibility: ReversibilityAssessment
    verdict: Verdict
    held_locks_before: tuple[HeldLock, ...] = ()
    notes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TransactionWarning:
    """One transaction accumulating strong locks across relations.

    ``hypothetical`` marks the whole-file variant computed under the
    assumption that the migration runner wraps the file in one transaction
    (only emitted when the file has no explicit transaction of its own).
    """

    relations: tuple[str, ...]
    description: str
    method: Method
    group_index: int | None = None
    hypothetical: bool = False


@dataclass(frozen=True, slots=True)
class ScriptAssessment:
    """Assessment of one migration file."""

    statements: tuple[StatementAssessment, ...]
    transaction_warnings: tuple[TransactionWarning, ...]
    pg_version: int
    online: bool  # a live snapshot was supplied
    baseline_shaped: bool
    notes: tuple[str, ...] = ()

    def classification_counts(self) -> dict[Classification, int]:
        counts = dict.fromkeys(Classification, 0)
        for statement in self.statements:
            counts[statement.verdict.classification] += 1
        return counts


@dataclass(frozen=True, slots=True)
class SnapshotProbes:
    """What to ask ``capture_snapshot`` for, derived from a parsed script."""

    relations: tuple[str, ...] = field(default=())
    functions: tuple[str, ...] = field(default=())
    types: tuple[str, ...] = field(default=())
    type_changes: tuple[tuple[str, str, str], ...] = field(default=())  # (relation, column, type)
