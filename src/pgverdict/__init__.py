"""pgverdict: analyze Postgres schema migrations for production safety.

This package currently provides the parsing layer: pglast-based statement
parsing, a locking-aware classification taxonomy, transaction structure, and
DO block extraction. Lock analysis is built on top of this IR.
"""

from pgverdict.ir import (
    AlterTableAction,
    AlterTableActionKind,
    CreateTableAsDetails,
    CreateTableDetails,
    DefaultInfo,
    DmlDetails,
    DoBlockDetails,
    DropDetails,
    IndexDetails,
    InsertDetails,
    LockDetails,
    MigrationScript,
    ParsedStatement,
    QualifiedName,
    ReindexDetails,
    RenameDetails,
    SetDetails,
    SourceSpan,
    StatementDetails,
    StatementKind,
    TransactionDetails,
    TransactionGroup,
    TruncateDetails,
    Volatility,
)
from pgverdict.parser import MigrationParseError, parse_migration, parse_migration_file

__all__ = [
    "AlterTableAction",
    "AlterTableActionKind",
    "CreateTableAsDetails",
    "CreateTableDetails",
    "DefaultInfo",
    "DmlDetails",
    "DoBlockDetails",
    "DropDetails",
    "IndexDetails",
    "InsertDetails",
    "LockDetails",
    "MigrationParseError",
    "MigrationScript",
    "ParsedStatement",
    "QualifiedName",
    "ReindexDetails",
    "RenameDetails",
    "SetDetails",
    "SourceSpan",
    "StatementDetails",
    "StatementKind",
    "TransactionDetails",
    "TransactionGroup",
    "TruncateDetails",
    "Volatility",
    "parse_migration",
    "parse_migration_file",
]
