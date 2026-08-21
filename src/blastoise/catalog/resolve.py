"""Resolve parsed statements against the lock catalog.

``resolve()`` turns one :class:`ParsedStatement` into the catalog rows that
apply on a given Postgres major version, with every affected relation named
where the IR allows it: one row per ALTER TABLE subcommand, recursion into
DO blocks, and per-relation lock modes (e.g. the referenced table of a
foreign key). No judgment is made here; this is the lookup the analyzer
builds verdicts on.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from blastoise.catalog.model import CatalogEntry, LockCatalog, LockMode, Resolution
from blastoise.ir import (
    AlterTableAction,
    CreateTableDetails,
    DoBlockDetails,
    ParsedStatement,
    QualifiedName,
    StatementKind,
)


@dataclass(frozen=True, slots=True)
class RelationLock:
    """One relation-level lock the statement takes.

    ``name`` is ``None`` when the role cannot be named statically (the table
    owning a dropped index, tables referencing a truncated table, ...).
    """

    role: str
    name: QualifiedName | None
    lock_mode: LockMode
    blocks_reads: bool
    blocks_writes: bool
    # False for an optional role the IR cannot name (CASCADE dependents, FK
    # referencers of a truncated table): the lock is taken only if such
    # relations exist, which static analysis cannot tell.
    certain: bool = True


@dataclass(frozen=True, slots=True)
class ResolvedLock:
    """A catalog row applied to one statement (or one ALTER TABLE action)."""

    entry: CatalogEntry
    relations: tuple[RelationLock, ...]
    statement: ParsedStatement
    action: AlterTableAction | None = None
    in_do_block: bool = False
    conditional: bool = False  # inside an existence-guarded DO block

    @property
    def lock_mode(self) -> LockMode:
        return self.entry.lock_mode


def ir_attrs(statement: ParsedStatement, action: AlterTableAction | None = None) -> dict[str, Any]:
    """Flatten the IR fields a ``when`` predicate can reference.

    Later sources win on name clashes (statement < details < action), which
    matches how the catalog's ``when`` keys are written: ``kind``/``only`` on
    the statement, ``scope``/``mode``/``no_data``/``source``/``object_type``
    on details, and action fields for ALTER TABLE subcommands.
    """
    attrs: dict[str, Any] = {}
    for field in dataclasses.fields(statement):
        attrs[field.name] = getattr(statement, field.name)
    details = statement.details
    if details is not None:
        for field in dataclasses.fields(details):
            attrs[field.name] = getattr(details, field.name)
    if action is not None:
        for field in dataclasses.fields(action):
            attrs[field.name] = getattr(action, field.name)
    return attrs


def _names_for_role(
    role: str, statement: ParsedStatement, action: AlterTableAction | None
) -> tuple[QualifiedName | None, ...]:
    """Relation names the IR supplies for a role; () when it supplies none."""
    if role == "target":
        return statement.targets[:1]
    if role == "additional_targets":
        return statement.targets[1:]
    if role == "referenced_table":
        return (action.referenced_table,) if action is not None and action.referenced_table else ()
    if role == "referenced_tables":
        details = statement.details
        if isinstance(details, CreateTableDetails):
            return details.referenced_tables
        return ()
    if role == "partition":
        return (action.partition,) if action is not None and action.partition else ()
    if role == "parent":
        details = statement.details
        if isinstance(details, CreateTableDetails) and details.partition_of is not None:
            return (details.partition_of,)
        return ()
    # Unresolvable roles: one unnamed relation lock.
    return (None,)


def _relations(
    catalog: LockCatalog,
    entry: CatalogEntry,
    statement: ParsedStatement,
    action: AlterTableAction | None,
) -> tuple[RelationLock, ...]:
    locks: list[RelationLock] = []
    for rel in entry.affected_relations:
        if not rel.statically_resolvable:
            names: tuple[QualifiedName | None, ...] = (None,)
        else:
            names = _names_for_role(rel.role, statement, action)
        if not names:
            if rel.optional:
                continue
            raise LookupError(
                f"{entry.kind.value}: affected relation role {rel.role!r} is required "
                f"but the IR supplies no relation for it"
            )
        conflicts = catalog.conflicts_with(rel.lock_mode)
        for name in names:
            locks.append(
                RelationLock(
                    role=rel.role,
                    name=name,
                    lock_mode=rel.lock_mode,
                    blocks_reads=LockMode.ACCESS_SHARE in conflicts,
                    blocks_writes=LockMode.ROW_EXCLUSIVE in conflicts,
                    certain=rel.statically_resolvable or not rel.optional,
                )
            )
    return tuple(locks)


def resolve(
    catalog: LockCatalog,
    statement: ParsedStatement,
    pg_version: int,
    *,
    in_do_block: bool = False,
    conditional: bool = False,
) -> tuple[ResolvedLock, ...]:
    """Catalog rows applying to ``statement`` on ``pg_version``.

    ALTER TABLE yields one row per subcommand (in order); DO blocks yield the
    rows of their statically extracted inner statements (tagged
    ``in_do_block``, and ``conditional`` when the block is existence-guarded);
    everything else yields exactly one row. Raises ``LookupError`` if the IR
    lacks a relation the catalog row requires.
    """
    resolution = catalog.resolution_of(statement.kind)
    if resolution is Resolution.PER_ACTION:
        resolved: list[ResolvedLock] = []
        for action in statement.alter_actions:
            entry = catalog.entry_for(action.kind, pg_version, ir_attrs(statement, action))
            resolved.append(
                ResolvedLock(
                    entry=entry,
                    relations=_relations(catalog, entry, statement, action),
                    statement=statement,
                    action=action,
                    in_do_block=in_do_block,
                    conditional=conditional,
                )
            )
        return tuple(resolved)
    if resolution is Resolution.INNER_STATEMENTS:
        details = statement.details
        if not isinstance(details, DoBlockDetails):
            return ()
        inner: list[ResolvedLock] = []
        for embedded in details.statements:
            inner.extend(
                resolve(
                    catalog,
                    embedded,
                    pg_version,
                    in_do_block=True,
                    conditional=conditional or details.existence_guarded,
                )
            )
        return tuple(inner)
    entry = catalog.entry_for(statement.kind, pg_version, ir_attrs(statement))
    return (
        ResolvedLock(
            entry=entry,
            relations=_relations(catalog, entry, statement, None),
            statement=statement,
            in_do_block=in_do_block,
            conditional=conditional,
        ),
    )


def statement_lock_mode(resolved: Iterable[ResolvedLock]) -> LockMode:
    """Strongest lock across the rows of one statement.

    For ALTER TABLE this is what Postgres actually takes for the whole
    statement: ``AlterTableGetLockLevel`` picks the strictest mode any
    subcommand needs and acquires it up front, so a SHARE UPDATE EXCLUSIVE
    subcommand bundled with an ACCESS EXCLUSIVE one runs under ACCESS
    EXCLUSIVE for the whole statement.
    """
    modes = [r.entry.lock_mode for r in resolved]
    if not modes:
        return LockMode.NONE
    return max(modes, key=lambda m: m.level)


def kinds_in(statement: ParsedStatement) -> tuple[StatementKind, ...]:
    """Statement kinds reachable from a statement, including DO inner ones."""
    kinds = [statement.kind]
    if isinstance(statement.details, DoBlockDetails):
        for inner in statement.details.statements:
            kinds.extend(kinds_in(inner))
    return tuple(kinds)
