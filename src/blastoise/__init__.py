"""Blastoise: know the blast radius before you migrate.

Blastoise reads a migration and reports what it will actually do to
production -- which locks it takes, what they block, and for how long --
checked against a live database rather than just the SQL. When it cannot
be sure, it says so instead of guessing.

This module is **Torrent**, the parser and IR: pglast-based statement
parsing, a locking-aware classification taxonomy, transaction structure,
and DO block extraction. Everything else is built on this IR --
``blastoise.catalog`` (Shell Armour), ``blastoise.live`` (Hydro Scan),
``blastoise.verdict`` (Pressure Levels).

Those names are for people. Nothing in the machine contract carries
them: enum values, JSON keys and exit codes are the plain names.
"""

from blastoise.ir import (
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
from blastoise.parser import MigrationParseError, parse_migration, parse_migration_file

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
