"""File-level parsing: statement splitting, spans, and transaction structure."""

from __future__ import annotations

import re
from pathlib import Path

from pglast.parser import ParseError, parse_sql

from pgverdict.classify import classify_statement
from pgverdict.ir import (
    CreateTableDetails,
    DropDetails,
    IndexDetails,
    MigrationScript,
    ParsedStatement,
    RenameDetails,
    SourceSpan,
    StatementKind,
    TransactionDetails,
    TransactionGroup,
)


class MigrationParseError(Exception):
    """The migration file is not valid Postgres SQL."""

    def __init__(
        self,
        message: str,
        *,
        location: int | None = None,
        line: int | None = None,
        path: str | None = None,
    ) -> None:
        where = f"{path or '<string>'}"
        if line is not None:
            where += f":{line}"
        super().__init__(f"{where}: {message}")
        self.message = message
        self.location = location
        self.line = line
        self.path = path


def _line_of(source: str, offset: int) -> int:
    return source.count("\n", 0, offset) + 1


_NEAR_TOKEN = re.compile(r'at or near "([^"]+)"')


def _refine_offset(source: str, offset: int | None, message: str) -> int | None:
    """Recover from pglast's byte/char offset mix-up on multi-byte sources.

    pglast 8.4 re-maps libpg_query's character-based cursor position as if it
    were a byte offset, shifting the reported location backwards whenever
    multi-byte characters precede the error. The offending token quoted in
    the error message lets us find the true position, which is never before
    the reported one.
    """
    if offset is None:
        return None
    match = _NEAR_TOKEN.search(message)
    if match is None:
        return offset
    token = match.group(1)
    if source.startswith(token, offset):
        return offset
    found = source.find(token, offset)
    return found if found != -1 else offset


def parse_migration(source: str, *, path: str | None = None) -> MigrationScript:
    """Parse and classify a migration script.

    Raises :class:`MigrationParseError` if the file is not valid SQL for the
    Postgres version libpg_query is built against.
    """
    nul = source.find("\x00")
    if nul != -1:
        # libpg_query is a C parser: it stops at the first NUL, which would
        # silently drop every later statement from the analysis.
        raise MigrationParseError(
            "source contains a NUL (0x00) byte",
            location=nul,
            line=_line_of(source, nul),
            path=path,
        )
    try:
        raw_statements = parse_sql(source)
    except ParseError as exc:
        # pglast raises ParseError(message, offset) with a 0-based char offset.
        message = str(exc.args[0]) if exc.args else "syntax error"
        offset = exc.args[1] if len(exc.args) > 1 and isinstance(exc.args[1], int) else None
        if offset is not None and not 0 <= offset < len(source):
            offset = None
        offset = _refine_offset(source, offset, message)
        line = _line_of(source, offset) if offset is not None else None
        raise MigrationParseError(message, location=offset, line=line, path=path) from exc

    statements: list[ParsedStatement] = []
    for raw in raw_statements:
        if raw.stmt is None:
            continue
        start = raw.stmt_location or 0
        length = raw.stmt_len or (len(source) - start)
        end = min(start + length, len(source))
        span = SourceSpan(start=start, end=end, line=_line_of(source, start))
        statements.append(classify_statement(raw.stmt, source[start:end].strip(), span))

    groups, warnings = _group_transactions(statements)
    return MigrationScript(
        source=source,
        statements=tuple(statements),
        transaction_groups=groups,
        warnings=warnings,
        path=path,
        baseline_shaped=_baseline_shaped(statements),
    )


def parse_migration_file(path: str | Path) -> MigrationScript:
    """Read (UTF-8, BOM tolerated) and parse a migration file."""
    file_path = Path(path)
    try:
        source = file_path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise MigrationParseError(
            f"file is not valid UTF-8: {exc}", path=str(file_path)
        ) from exc
    return parse_migration(source, path=str(file_path))


# A file must be at least this many statements to read as a squash/baseline
# script rather than an ordinary create-only incremental migration.
BASELINE_MIN_STATEMENTS = 50

_CREATES_RELATION = frozenset(
    {
        StatementKind.CREATE_TABLE,
        StatementKind.CREATE_TABLE_PARTITION_OF,
        StatementKind.CREATE_TABLE_AS,
        StatementKind.SELECT_INTO,
        StatementKind.CREATE_VIEW,
        StatementKind.CREATE_MATVIEW,
        StatementKind.CREATE_SEQUENCE,
        StatementKind.CREATE_FOREIGN_TABLE,
    }
)

# Statement kinds whose targets reference a relation that must already exist.
_REFERENCES_RELATION = frozenset(
    {
        StatementKind.ALTER_TABLE,
        StatementKind.ALTER_INDEX,
        StatementKind.ALTER_VIEW,
        StatementKind.ALTER_MATVIEW,
        StatementKind.ALTER_FOREIGN_TABLE,
        StatementKind.ALTER_SEQUENCE,
        StatementKind.CREATE_INDEX,
        StatementKind.CREATE_INDEX_CONCURRENTLY,
        StatementKind.CREATE_TRIGGER,
        StatementKind.CREATE_POLICY,
        StatementKind.TRUNCATE,
        StatementKind.INSERT,
        StatementKind.UPDATE,
        StatementKind.UPDATE_WITHOUT_WHERE,
        StatementKind.UPDATE_BATCHED,
        StatementKind.DELETE,
        StatementKind.DELETE_WITHOUT_WHERE,
        StatementKind.DELETE_BATCHED,
        StatementKind.MERGE,
        StatementKind.COPY_FROM,
        StatementKind.COPY_TO,
        StatementKind.CLUSTER,
        StatementKind.VACUUM,
        StatementKind.VACUUM_FULL,
        StatementKind.ANALYZE,
        StatementKind.REFRESH_MATVIEW,
        StatementKind.REFRESH_MATVIEW_CONCURRENTLY,
        StatementKind.LOCK_TABLE,
        StatementKind.RENAME_TABLE,
        StatementKind.RENAME_COLUMN,
    }
)

_DROPS_RELATION = frozenset(
    {
        StatementKind.DROP_TABLE,
        StatementKind.DROP_VIEW,
        StatementKind.DROP_MATVIEW,
        StatementKind.DROP_SEQUENCE,
        StatementKind.DROP_INDEX,
        StatementKind.DROP_INDEX_CONCURRENTLY,
    }
)


def _baseline_shaped(statements: list[ParsedStatement]) -> bool:
    """Detect squash/baseline files: self-contained object creation at scale.

    True when the file is large and every relation it alters, indexes, drops
    (without IF EXISTS), or fills was created earlier in the same file — the
    shape of a pg_dump-style squash that runs against an empty database.
    Names are compared unqualified to tolerate schema-qualification drift.
    """
    if len(statements) < BASELINE_MIN_STATEMENTS:
        return False
    created: set[str] = set()
    for statement in statements:
        kind = statement.kind
        if kind in _CREATES_RELATION:
            created.update(target.name for target in statement.targets)
            details = statement.details
            if isinstance(details, CreateTableDetails):
                parent = details.partition_of
                if parent is not None and parent.name not in created:
                    return False
            continue
        if kind in _REFERENCES_RELATION:
            for target in statement.targets:
                if target.name not in created:
                    return False
            details = statement.details
            if isinstance(details, IndexDetails) and details.index_name:
                created.add(details.index_name)
            elif isinstance(details, RenameDetails) and details.new_name:
                created.add(details.new_name)
            continue
        if kind in _DROPS_RELATION:
            details = statement.details
            if isinstance(details, DropDetails) and details.missing_ok:
                continue  # DROP IF EXISTS is the classic squash preamble
            for target in statement.targets:
                if target.name not in created:
                    return False
    return True


def _group_transactions(
    statements: list[ParsedStatement],
) -> tuple[tuple[TransactionGroup, ...], tuple[str, ...]]:
    """Group statements by explicit BEGIN/COMMIT/ROLLBACK boundaries.

    Statements outside any explicit block form implicit groups; whether the
    migration runner wraps those in a transaction of its own is deliberately
    not decided here.
    """
    groups: list[TransactionGroup] = []
    warnings: list[str] = []
    current: list[int] = []
    opened_by: int | None = None
    in_explicit = False

    def flush(closed_by: int | None = None, rolled_back: bool = False) -> None:
        nonlocal current, opened_by, in_explicit
        if in_explicit or current:
            groups.append(
                TransactionGroup(
                    explicit=in_explicit,
                    statement_indices=tuple(current),
                    opened_by=opened_by,
                    closed_by=closed_by,
                    rolled_back=rolled_back,
                )
            )
        current = []
        opened_by = None
        in_explicit = False

    for index, statement in enumerate(statements):
        kind = statement.kind
        if kind is StatementKind.BEGIN:
            if in_explicit:
                warnings.append(
                    f"line {statement.span.line}: BEGIN inside an already-open "
                    "transaction block has no effect"
                )
            else:
                flush()
                in_explicit = True
                opened_by = index
        elif kind in (StatementKind.COMMIT, StatementKind.ROLLBACK):
            if in_explicit:
                chained = (
                    isinstance(statement.details, TransactionDetails)
                    and statement.details.chain
                )
                flush(closed_by=index, rolled_back=kind is StatementKind.ROLLBACK)
                if chained:
                    # COMMIT/ROLLBACK AND CHAIN immediately starts a new
                    # transaction with the same characteristics.
                    in_explicit = True
                    opened_by = index
            else:
                warnings.append(
                    f"line {statement.span.line}: {statement.sql.split()[0].upper()} "
                    "without a matching BEGIN"
                )
        else:
            current.append(index)

    if in_explicit:
        opened_line = statements[opened_by].span.line if opened_by is not None else 0
        warnings.append(f"line {opened_line}: transaction block is never closed")
    flush()
    return tuple(groups), tuple(warnings)
