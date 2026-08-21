"""Typed model of the lock semantics catalog.

The catalog itself is data (``lock_catalog.yaml``); these types are what the
validating loader produces. Nothing here decides anything about a migration —
an entry records what Postgres does for one statement classification on one
range of major versions, with a citation, so each row can be audited on its
own.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from blastoise.ir import AlterTableActionKind, StatementKind


class LockMode(StrEnum):
    """Table-level lock modes, weakest to strongest, plus two markers.

    ``NONE`` means the statement takes no relation-level lock at all (pure
    catalog work: CREATE FUNCTION, COMMIT, SET ...). ``UNKNOWN`` is permitted
    only for the grab-bag ``OTHER`` classifications and signals "not modeled;
    surface the statement as unanalyzed" — it is never a default.
    """

    ACCESS_SHARE = "ACCESS SHARE"
    ROW_SHARE = "ROW SHARE"
    ROW_EXCLUSIVE = "ROW EXCLUSIVE"
    SHARE_UPDATE_EXCLUSIVE = "SHARE UPDATE EXCLUSIVE"
    SHARE = "SHARE"
    SHARE_ROW_EXCLUSIVE = "SHARE ROW EXCLUSIVE"
    EXCLUSIVE = "EXCLUSIVE"
    ACCESS_EXCLUSIVE = "ACCESS EXCLUSIVE"
    NONE = "NONE"
    UNKNOWN = "UNKNOWN"

    @property
    def level(self) -> int:
        """Numeric strength: Postgres' 1..8, with NONE=0 and UNKNOWN=9."""
        return _LOCK_LEVELS[self]

    @property
    def is_table_lock(self) -> bool:
        return self not in (LockMode.NONE, LockMode.UNKNOWN)


_LOCK_LEVELS: dict[LockMode, int] = {
    LockMode.NONE: 0,
    LockMode.ACCESS_SHARE: 1,
    LockMode.ROW_SHARE: 2,
    LockMode.ROW_EXCLUSIVE: 3,
    LockMode.SHARE_UPDATE_EXCLUSIVE: 4,
    LockMode.SHARE: 5,
    LockMode.SHARE_ROW_EXCLUSIVE: 6,
    LockMode.EXCLUSIVE: 7,
    LockMode.ACCESS_EXCLUSIVE: 8,
    LockMode.UNKNOWN: 9,
}

TABLE_LOCK_MODES: tuple[LockMode, ...] = tuple(mode for mode in LockMode if mode.is_table_lock)


class DurationModel(StrEnum):
    """How long the statement holds its locks.

    ``UNKNOWN`` is permitted only for the ``OTHER`` classifications.
    """

    CONSTANT = "CONSTANT"  # catalog-only / O(1): lock is held briefly
    PROPORTIONAL_TO_ROWS = "PROPORTIONAL_TO_ROWS"  # full scan or rewrite of the table
    PROPORTIONAL_TO_INDEX_SIZE = "PROPORTIONAL_TO_INDEX_SIZE"  # rebuild of one index
    PROPORTIONAL_TO_ROWS_MATCHED = "PROPORTIONAL_TO_ROWS_MATCHED"  # depends on a predicate
    UNKNOWN = "UNKNOWN"


class Calibration(StrEnum):
    """Whether the row was researched and checked against the wild corpus.

    ``UNCALIBRATED`` rows are explicit stubs: provisional values for kinds
    that never occurred in the 3,081-file corpus (or, for ALTER TABLE actions,
    that were not verified against docs/source). They exist so that lookup
    never falls through to a default, and so consumers can hedge.

    ``MEASURED`` marks a value measured directly by a harness on real
    hardware (the duration constants use it; catalog rows do not) —
    stronger than a researched guess, weaker than a calibration fitted
    across environments, and its basis must say when and on what it was
    measured.
    """

    CALIBRATED = "CALIBRATED"
    UNCALIBRATED = "UNCALIBRATED"
    MEASURED = "MEASURED"


class TransactionBlock(StrEnum):
    ALLOWED = "ALLOWED"
    FORBIDDEN = "FORBIDDEN"  # errors inside BEGIN ... COMMIT
    RESTRICTED = "RESTRICTED"  # runs, but with a caveat described in notes


class WriteBlockScope(StrEnum):
    NONE = "NONE"
    TABLE = "TABLE"  # the table-level lock conflicts with ROW EXCLUSIVE
    MATCHED_ROWS = "MATCHED_ROWS"  # row locks on the rows the statement touches


class Resolution(StrEnum):
    """How a StatementKind is mapped onto catalog entries."""

    DIRECT = "direct"  # one entry per (kind, version, when-variant)
    PER_ACTION = "per_action"  # ALTER TABLE: one entry per subcommand
    INNER_STATEMENTS = "inner_statements"  # DO: recurse into embedded statements


@dataclass(frozen=True, slots=True)
class VersionRange:
    """Inclusive range of Postgres major versions."""

    min: int
    max: int

    def covers(self, version: int) -> bool:
        return self.min <= version <= self.max

    def overlaps(self, other: VersionRange) -> bool:
        return self.min <= other.max and other.min <= self.max

    def __str__(self) -> str:
        return f"{self.min}" if self.min == self.max else f"{self.min}-{self.max}"


@dataclass(frozen=True, slots=True)
class AffectedRelation:
    """One relation the statement locks, by role.

    ``role`` names how the resolver finds the relation in the IR (``target``,
    ``referenced_table``, ...). ``optional`` roles are skipped when the IR
    does not supply them; non-optional roles must resolve. Roles with
    ``statically_resolvable=False`` (``owning_table`` of an index, FK
    referencers of a truncated table, ...) are reported with no name.
    """

    role: str
    lock_mode: LockMode
    optional: bool = False
    statically_resolvable: bool = True


@dataclass(frozen=True, slots=True)
class VersionBreakpoint:
    """A known change in locking behavior for one kind at one major version.

    The registry of breakpoints is the audit trail for version splits: no
    entry for ``kind`` may span ``version - 1`` and ``version``.
    """

    kind: StatementKind | AlterTableActionKind
    version: int
    what_changed: str
    source: str


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    """Lock semantics of one classification on one version range."""

    kind: StatementKind | AlterTableActionKind
    pg_versions: VersionRange
    calibration: Calibration
    available: bool  # False: the form does not exist in these versions
    lock_mode: LockMode  # strongest lock taken on any pre-existing relation
    conflicts_with: frozenset[LockMode]  # from the conflict matrix
    blocks_reads: bool
    blocks_writes: bool
    write_block_scope: WriteBlockScope
    row_locks: str | None  # row-level lock mode for DML, if any
    requires_table_rewrite: bool
    duration_model: DurationModel
    requires_live_context: bool  # catalog worst case; live schema/data narrows it
    live_context: str | None  # what live context would narrow it
    transaction_block: TransactionBlock
    failure_mode: str | None
    affected_relations: tuple[AffectedRelation, ...]
    when: tuple[tuple[str, Any], ...]  # IR-field predicate; empty = default row
    source: tuple[str, ...]  # doc:/src:/release: citations
    notes: str | None

    @property
    def is_default_row(self) -> bool:
        return not self.when

    @property
    def when_mapping(self) -> dict[str, Any]:
        return dict(self.when)


_MISSING = object()


def when_matches(when: tuple[tuple[str, Any], ...], attrs: dict[str, Any]) -> bool:
    """Evaluate an entry's ``when`` predicate against IR attributes.

    Each key must be present in ``attrs``; a list value means membership,
    anything else is equality (StrEnum members compare equal to their value).
    """
    for key, expected in when:
        actual = attrs.get(key, _MISSING)
        if actual is _MISSING:
            return False
        if isinstance(expected, list):
            if actual not in expected:
                return False
        elif actual != expected:
            return False
    return True


@dataclass(frozen=True, slots=True)
class LockCatalog:
    """The loaded, validated catalog.

    ``statements`` holds entries for every ``StatementKind`` whose resolution
    is ``DIRECT``; ``alter_table_actions`` holds entries for every
    ``AlterTableActionKind``. Kinds resolved ``PER_ACTION`` or via
    ``INNER_STATEMENTS`` deliberately have no direct entries.
    """

    version_domain: VersionRange
    conflict_matrix: dict[LockMode, frozenset[LockMode]]
    breakpoints: tuple[VersionBreakpoint, ...]
    statement_resolution: dict[StatementKind, Resolution]
    statements: dict[StatementKind, tuple[CatalogEntry, ...]]
    alter_table_actions: dict[AlterTableActionKind, tuple[CatalogEntry, ...]]

    def resolution_of(self, kind: StatementKind) -> Resolution:
        return self.statement_resolution.get(kind, Resolution.DIRECT)

    def conflicts_with(self, mode: LockMode) -> frozenset[LockMode]:
        """Lock modes that ``mode`` blocks (and is blocked by)."""
        if mode is LockMode.NONE:
            return frozenset()
        if mode is LockMode.UNKNOWN:
            return frozenset(TABLE_LOCK_MODES)
        return self.conflict_matrix[mode]

    def rows_for(self, kind: StatementKind | AlterTableActionKind) -> tuple[CatalogEntry, ...]:
        if isinstance(kind, StatementKind):
            if self.resolution_of(kind) is not Resolution.DIRECT:
                raise KeyError(
                    f"{kind} resolves via {self.resolution_of(kind).value}; "
                    "it has no direct catalog rows"
                )
            return self.statements[kind]
        return self.alter_table_actions[kind]

    def entries_for(
        self, kind: StatementKind | AlterTableActionKind, pg_version: int
    ) -> tuple[CatalogEntry, ...]:
        """Every row (default and ``when`` variants) covering ``pg_version``."""
        self._check_version(pg_version)
        return tuple(e for e in self.rows_for(kind) if e.pg_versions.covers(pg_version))

    def entry_for(
        self,
        kind: StatementKind | AlterTableActionKind,
        pg_version: int,
        attrs: dict[str, Any] | None = None,
    ) -> CatalogEntry:
        """The single row that applies: first matching ``when`` row, else default.

        ``attrs`` are the IR attributes (statement, details, action fields) the
        ``when`` predicates are evaluated against; see ``resolve.ir_attrs``.
        """
        candidates = self.entries_for(kind, pg_version)
        attrs = attrs or {}
        for entry in candidates:
            if not entry.is_default_row and when_matches(entry.when, attrs):
                return entry
        for entry in candidates:
            if entry.is_default_row:
                return entry
        raise LookupError(f"no catalog row for {kind} on PG {pg_version}")  # pragma: no cover

    def _check_version(self, pg_version: int) -> None:
        if not self.version_domain.covers(pg_version):
            raise ValueError(
                f"PG {pg_version} is outside the catalog's version domain {self.version_domain}"
            )
