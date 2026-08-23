"""Ground truth: the tier a statement *earned* by what it measurably did.

This is the rule the engine is scored against, so it is written to be read
on its own and deliberately consults nothing in ``blastoise.verdict``
except the four threshold numbers that *define* the tiers (what counts as
a brief stall versus an outage). Everything else — the lock conflict
table, the tier ladder, the ACCESS EXCLUSIVE floor — is restated here from
the Postgres documentation and the tier definitions in ``DECISIONS.md``.

Inputs are measurements (which locks the statement held on relations that
existed before the file ran, how long it held them including the wait to
acquire them, whether it errored, how many rows it touched) plus two
declared facts the harness cannot measure: whether the statement is
irreversible, and whether it is DML.

The rule, in order:

1. An error is ``unsafe``: the migration aborts as written.
2. The strongest lock the statement held on a *pre-existing* relation
   decides what it blocked — reads (ACCESS EXCLUSIVE), writes (SHARE, SHARE
   ROW EXCLUSIVE, EXCLUSIVE), or nothing. Relations the file itself created
   are ignored: nothing in production can be waiting on them. Indexes are
   relations: ACCESS EXCLUSIVE on an index parks every query that plans
   against its table.
3. The normalized hold is banded against the thresholds for that block
   type. A statement that held only ROW EXCLUSIVE / ROW SHARE (DML, or a
   DO block full of it) is banded on the write thresholds anyway: every
   row it touches is row-locked for the whole statement, concurrent
   writers to those rows stall, and the dead tuples and WAL are
   proportional — ``NEEDS_TIMING`` exists for exactly that shape.
4. ACCESS EXCLUSIVE on a pre-existing relation is at least
   ``needs_timing`` whatever the hold: the tier is defined on the
   acquisition, not the hold (``DECISIONS.md``, the AEL floor).
5. A safe result that is irreversible is ``safe_irreversible``.

``unknown`` is never ground truth: after the statement has run, the
outcome is known.
"""

from __future__ import annotations

from dataclasses import dataclass

from blastoise.verdict import constants as _k

# Lock conflict table, PostgreSQL docs "Explicit Locking", Table 13.2.
# A mode blocks reads if it conflicts with ACCESS SHARE, writes if it
# conflicts with ROW EXCLUSIVE.
_BLOCKS_READS: frozenset[str] = frozenset({"ACCESS EXCLUSIVE"})
_BLOCKS_WRITES: frozenset[str] = frozenset(
    {"SHARE", "SHARE ROW EXCLUSIVE", "EXCLUSIVE", "ACCESS EXCLUSIVE"}
)

# Modes that mean the statement wrote rows (or locked them FOR UPDATE):
# no table-level block, but every touched row stays locked and every dead
# tuple and WAL byte accrues for as long as the statement runs.
_ROW_LEVEL: frozenset[str] = frozenset({"ROW SHARE", "ROW EXCLUSIVE"})

_MODE_RANK: dict[str, int] = {
    "ACCESS SHARE": 0,
    "ROW SHARE": 1,
    "ROW EXCLUSIVE": 2,
    "SHARE UPDATE EXCLUSIVE": 3,
    "SHARE": 4,
    "SHARE ROW EXCLUSIVE": 5,
    "EXCLUSIVE": 6,
    "ACCESS EXCLUSIVE": 7,
}

_PG_MODE_NAMES: dict[str, str] = {
    "AccessShareLock": "ACCESS SHARE",
    "RowShareLock": "ROW SHARE",
    "RowExclusiveLock": "ROW EXCLUSIVE",
    "ShareUpdateExclusiveLock": "SHARE UPDATE EXCLUSIVE",
    "ShareLock": "SHARE",
    "ShareRowExclusiveLock": "SHARE ROW EXCLUSIVE",
    "ExclusiveLock": "EXCLUSIVE",
    "AccessExclusiveLock": "ACCESS EXCLUSIVE",
}


def catalog_mode(pg_mode: str) -> str:
    """``pg_locks.mode`` spelling to the documentation spelling."""
    return _PG_MODE_NAMES[pg_mode]


def strongest(modes: list[str] | tuple[str, ...] | set[str]) -> str | None:
    best: str | None = None
    for mode in modes:
        if best is None or _MODE_RANK[mode] > _MODE_RANK[best]:
            best = mode
    return best


def block_type(mode: str | None) -> str:
    """'reads', 'writes', or 'none' for the strongest held mode."""
    if mode is None:
        return "none"
    if mode in _BLOCKS_READS:
        return "reads"
    if mode in _BLOCKS_WRITES:
        return "writes"
    return "none"


@dataclass(frozen=True, slots=True)
class Measured:
    """Everything the labeling rule needs about one executed statement."""

    error: bool
    strongest_preexisting_mode: str | None
    hold_ms: int  # normalized: work scaled to the reference machine + the raw lock wait
    is_dml: bool
    rows_touched: int | None
    irreversible: bool


@dataclass(frozen=True, slots=True)
class Label:
    tier: str
    block: str  # reads | writes | none
    basis: str
    thresholds_ms: tuple[int, int] | None  # the (short, long) pair the hold was banded on
    boundary_proximity: float | None  # hold / nearest threshold, for sensitivity reporting


def _thresholds(block: str) -> tuple[int, int]:
    if block == "reads":
        return _k.FULL_BLOCK_SHORT_MS, _k.FULL_BLOCK_LONG_MS
    return _k.WRITE_BLOCK_SHORT_MS, _k.WRITE_BLOCK_LONG_MS


def _band(hold_ms: int, short: int, long: int) -> str:
    if hold_ms < short:
        return "safe"
    if hold_ms < long:
        return "needs_timing"
    return "unsafe"


def _proximity(hold_ms: int, short: int, long: int) -> float:
    """How close the hold sits to the nearest threshold: 1.0 = exactly on it."""
    nearest = short if abs(hold_ms - short) <= abs(hold_ms - long) else long
    return hold_ms / nearest if nearest else 0.0


def label(m: Measured) -> Label:
    if m.error:
        return Label(
            tier="unsafe",
            block="none",
            basis="the statement errored: run as written, the migration aborts",
            thresholds_ms=None,
            boundary_proximity=None,
        )
    block = block_type(m.strongest_preexisting_mode)
    mode = m.strongest_preexisting_mode or "no table-level lock"

    if block == "none":
        row_level = m.strongest_preexisting_mode in _ROW_LEVEL or (
            m.is_dml and (m.rows_touched or 0) > 0
        )
        if row_level:
            short, long = _thresholds("writes")
            tier = _band(m.hold_ms, short, long)
            touched = f"{m.rows_touched} rows" if m.rows_touched is not None else "rows"
            basis = (
                f"row-level writes ({touched}) under {mode} for {m.hold_ms} ms: "
                "row locks, dead tuples and WAL scale with the run; banded on the "
                "write thresholds"
            )
            prox = _proximity(m.hold_ms, short, long)
            thresholds: tuple[int, int] | None = (short, long)
        else:
            tier = "safe"
            basis = f"{mode} on pre-existing relations blocks neither reads nor writes"
            prox = None
            thresholds = None
    else:
        short, long = _thresholds(block)
        tier = _band(m.hold_ms, short, long)
        basis = f"{mode} blocks {block} for {m.hold_ms} ms (thresholds {short}/{long})"
        prox = _proximity(m.hold_ms, short, long)
        thresholds = (short, long)
        if block == "reads" and tier == "safe":
            tier = "needs_timing"
            basis += (
                "; ACCESS EXCLUSIVE on a pre-existing relation needs a window "
                "regardless of hold (the acquisition queues behind every open "
                "transaction and parks every later query)"
            )

    if tier == "safe" and m.irreversible:
        tier = "safe_irreversible"
        basis += "; irreversible once committed"
    return Label(
        tier=tier, block=block, basis=basis, thresholds_ms=thresholds, boundary_proximity=prox
    )


# The action ladder from blastoise.verdict.model, restated: a prediction is
# *lenient* when it sits lower on the ladder than the truth (the dangerous
# direction), *strict* when higher. UNKNOWN is neither — it is a refusal —
# and is reported on its own.
LADDER: dict[str, int] = {
    "safe": 0,
    "safe_irreversible": 1,
    "needs_timing": 2,
    "unsafe": 3,
}


def outcome(predicted: str, truth: str) -> str:
    """'match', 'strict', 'lenient', or 'unknown'."""
    if predicted == "unknown":
        return "unknown"
    if predicted == truth:
        return "match"
    return "lenient" if LADDER[predicted] < LADDER[truth] else "strict"
