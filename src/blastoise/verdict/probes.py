"""Derive the snapshot probe lists a parsed script needs.

``capture_snapshot`` gathers facts only for what it is asked about; this
module walks the IR (DO-block inner statements included) and lists every
relation, function, type, and type-change the verdict layer will want
facts for. Pure IR walking — no judgment.
"""

from __future__ import annotations

import re

from blastoise.ir import (
    AlterTableActionKind,
    CreateTableDetails,
    DoBlockDetails,
    MigrationScript,
    ParsedStatement,
    QualifiedName,
)
from blastoise.verdict.model import SnapshotProbes

_ADD_COLUMN_KINDS = frozenset(
    kind for kind in AlterTableActionKind if kind.value.startswith("add_column")
)

_SAFE_IDENTIFIER = re.compile(r"[a-z_][a-z0-9_$]*")


def _quote_part(part: str) -> str:
    if _SAFE_IDENTIFIER.fullmatch(part):
        return part
    return '"' + part.replace('"', '""') + '"'


def probe_name(name: QualifiedName) -> str:
    """The server-resolvable spelling of a relation name.

    ``str(QualifiedName)`` loses the quoting the migration wrote: an
    unquoted ``User`` would be case-folded by the server and miss the
    ``"User"`` table Prisma-style migrations create. Identifiers that are
    not already lower-case-safe are quoted, so ``to_regclass`` resolves
    them as written. The engine looks snapshot facts up by this same
    spelling.
    """
    if name.schema is not None:
        return f"{_quote_part(name.schema)}.{_quote_part(name.name)}"
    return _quote_part(name.name)


def _walk(statement: ParsedStatement) -> tuple[ParsedStatement, ...]:
    out = [statement]
    if isinstance(statement.details, DoBlockDetails):
        for inner in statement.details.statements:
            out.extend(_walk(inner))
    return tuple(out)


def snapshot_probes(script: MigrationScript) -> SnapshotProbes:
    relations: set[str] = set()
    functions: set[str] = set()
    types: set[str] = set()
    type_changes: set[tuple[str, str, str]] = set()

    for top in script.statements:
        for statement in _walk(top):
            for target in statement.targets:
                relations.add(probe_name(target))
            details = statement.details
            if isinstance(details, CreateTableDetails):
                relations.update(probe_name(name) for name in details.referenced_tables)
                if details.partition_of is not None:
                    relations.add(probe_name(details.partition_of))
            for action in statement.alter_actions:
                if action.referenced_table is not None:
                    relations.add(probe_name(action.referenced_table))
                if action.partition is not None:
                    relations.add(probe_name(action.partition))
                if action.default is not None:
                    functions.update(action.default.unknown_functions)
                if action.kind in _ADD_COLUMN_KINDS and action.column_type is not None:
                    types.add(action.column_type)
                if (
                    action.kind is AlterTableActionKind.ALTER_COLUMN_TYPE
                    and statement.targets
                    and action.column is not None
                    and action.column_type is not None
                ):
                    type_changes.add(
                        (probe_name(statement.targets[0]), action.column, action.column_type)
                    )

    return SnapshotProbes(
        relations=tuple(sorted(relations)),
        functions=tuple(sorted(functions)),
        types=tuple(sorted(types)),
        type_changes=tuple(sorted(type_changes)),
    )
