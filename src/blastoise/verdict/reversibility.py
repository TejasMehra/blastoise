"""Post-commit reversibility of every statement and ALTER TABLE action.

"Reversible" means: after the migration commits, an inverse statement can
restore the previous logical state without loss (recreating a definition
from schema history counts; losing row data or definitions that exist only
in the database does not). Where reversibility depends on facts the
grammar cannot supply (is this cast lossless?), the entry is UNKNOWN with
the reason — the engine may upgrade it when a live fact decides.

The tables are total over both enums — a test asserts coverage — and the
lookups raise on a miss rather than defaulting.
"""

from __future__ import annotations

from blastoise.ir import AlterTableActionKind, StatementKind
from blastoise.verdict.model import Method, Reversibility, ReversibilityAssessment

_SK = StatementKind
_AK = AlterTableActionKind


def _rev(basis: str) -> ReversibilityAssessment:
    return ReversibilityAssessment(
        reversibility=Reversibility.REVERSIBLE, method=Method.PROVEN, basis=basis
    )


def _irrev(what_is_lost: str, basis: str) -> ReversibilityAssessment:
    return ReversibilityAssessment(
        reversibility=Reversibility.IRREVERSIBLE,
        method=Method.PROVEN,
        basis=basis,
        what_is_lost=what_is_lost,
    )


def _unknown(basis: str) -> ReversibilityAssessment:
    return ReversibilityAssessment(
        reversibility=Reversibility.UNKNOWN, method=Method.UNVERIFIED, basis=basis
    )


_NO_CHANGE = _rev("no persistent schema or data change of its own")
_CREATED = _rev("a created object is fully undone by dropping it")
_SETTINGS = _rev(
    "an inverse statement restores the setting; the prior value must come from "
    "schema history, not the database"
)
_RENAME = _rev("renaming back restores the previous state exactly")
_MAINTENANCE = _rev("maintenance only: the logical contents are unchanged")
_READ_ONLY = _rev("read-only")
_CONSTRAINT_ADD = _rev(
    "dropping the constraint undoes it (PRIMARY KEY/UNIQUE drop their backing "
    "index with it — rebuild cost, no data loss)"
)

_STATEMENTS: dict[StatementKind, ReversibilityAssessment] = {
    # Transaction control
    _SK.BEGIN: _NO_CHANGE,
    _SK.COMMIT: _NO_CHANGE,
    _SK.ROLLBACK: _NO_CHANGE,
    _SK.SAVEPOINT: _NO_CHANGE,
    _SK.RELEASE_SAVEPOINT: _NO_CHANGE,
    _SK.ROLLBACK_TO_SAVEPOINT: _NO_CHANGE,
    _SK.TRANSACTION_OTHER: _NO_CHANGE,
    # Creations
    _SK.CREATE_TABLE: _CREATED,
    _SK.CREATE_TABLE_PARTITION_OF: _CREATED,
    _SK.CREATE_TABLE_AS: _CREATED,
    _SK.SELECT_INTO: _CREATED,
    _SK.CREATE_FOREIGN_TABLE: _CREATED,
    _SK.CREATE_VIEW: _CREATED,
    _SK.CREATE_MATVIEW: _CREATED,
    _SK.CREATE_SEQUENCE: _CREATED,
    _SK.CREATE_ENUM_TYPE: _CREATED,
    _SK.CREATE_COMPOSITE_TYPE: _CREATED,
    _SK.CREATE_RANGE_TYPE: _CREATED,
    _SK.CREATE_DOMAIN: _CREATED,
    _SK.CREATE_STATISTICS: _CREATED,
    _SK.CREATE_AGGREGATE: _CREATED,
    _SK.CREATE_FUNCTION: _CREATED,
    _SK.CREATE_PROCEDURE: _CREATED,
    _SK.CREATE_TRIGGER: _CREATED,
    _SK.CREATE_POLICY: _CREATED,
    _SK.CREATE_RULE: _CREATED,
    _SK.CREATE_EXTENSION: _CREATED,
    _SK.CREATE_SCHEMA: _CREATED,
    _SK.CREATE_ROLE: _CREATED,
    _SK.CREATE_INDEX: _CREATED,
    _SK.CREATE_INDEX_CONCURRENTLY: _CREATED,
    _SK.CREATE_DATABASE: _CREATED,
    _SK.CREATE_TABLESPACE: _CREATED,
    # Destructions
    _SK.DROP_TABLE: _irrev(
        "the table's rows, and its indexes, constraints, triggers, policies, "
        "ACLs and comments (plus every dependent object under CASCADE); "
        "recreating the shape does not restore the data",
        "dropped row data exists nowhere else",
    ),
    _SK.TRUNCATE: _irrev(
        "every row of the table (with CASCADE: every row of every table whose "
        "foreign keys reference it; with RESTART IDENTITY: the positions of "
        "owned sequences)",
        "truncated rows exist nowhere else",
    ),
    _SK.DROP_INDEX: _rev("an index is derived data: recreating it restores it (rebuild cost)"),
    _SK.DROP_INDEX_CONCURRENTLY: _rev(
        "an index is derived data: recreating it restores it (rebuild cost)"
    ),
    _SK.DROP_VIEW: _irrev(
        "the view's definition as stored in the database (and dependent views "
        "under CASCADE); recoverable only from schema history",
        "the definition is destroyed with the object",
    ),
    _SK.DROP_MATVIEW: _irrev(
        "the materialized view's definition and its materialized contents; the "
        "contents are recomputable only if the defining query is recorded elsewhere",
        "the definition is destroyed with the object",
    ),
    _SK.DROP_SEQUENCE: _irrev(
        "the sequence's current position: recreating it restarts numbering and "
        "can re-issue already-used values",
        "the position is not recorded anywhere else",
    ),
    _SK.DROP_TYPE: _irrev(
        "the type definition; under CASCADE, dependent columns are dropped with "
        "their data",
        "the definition is destroyed with the object",
    ),
    _SK.DROP_DOMAIN: _irrev(
        "the domain definition; under CASCADE, dependent columns are dropped "
        "with their data",
        "the definition is destroyed with the object",
    ),
    _SK.DROP_FUNCTION: _irrev(
        "the function's body as stored in the database; recoverable only from "
        "schema history",
        "the definition is destroyed with the object",
    ),
    _SK.DROP_PROCEDURE: _irrev(
        "the procedure's body as stored in the database; recoverable only from "
        "schema history",
        "the definition is destroyed with the object",
    ),
    _SK.DROP_AGGREGATE: _irrev(
        "the aggregate's definition as stored in the database",
        "the definition is destroyed with the object",
    ),
    _SK.DROP_TRIGGER: _irrev(
        "the trigger's definition on the table; recoverable only from schema history",
        "the definition is destroyed with the object",
    ),
    _SK.DROP_POLICY: _irrev(
        "the policy's predicate and role list; recoverable only from schema history",
        "the definition is destroyed with the object",
    ),
    _SK.DROP_EXTENSION: _irrev(
        "every object the extension owns, including any data in extension-owned tables",
        "extension objects are dropped with it",
    ),
    _SK.DROP_SCHEMA: _irrev(
        "under CASCADE: every contained object and its data",
        "contained objects are destroyed with the schema",
    ),
    _SK.DROP_ROLE: _irrev(
        "the role's memberships, settings and password",
        "role attributes are not recorded elsewhere",
    ),
    _SK.DROP_DATABASE: _irrev("the entire database", "everything in it is destroyed"),
    _SK.DROP_TABLESPACE: _rev(
        "a tablespace must already be empty to be dropped; nothing is lost"
    ),
    # ALTER on relations — composed per subcommand by the engine; the entry
    # here covers an ALTER TABLE that carries no parsed actions.
    _SK.ALTER_TABLE: _unknown("assessed per subcommand; no subcommand was parsed"),
    _SK.ALTER_INDEX: _SETTINGS,
    _SK.ALTER_VIEW: _SETTINGS,
    _SK.ALTER_MATVIEW: _SETTINGS,
    _SK.ALTER_FOREIGN_TABLE: _unknown("assessed per subcommand; no subcommand was parsed"),
    _SK.ALTER_COMPOSITE_TYPE: _unknown(
        "the subcommand is not recorded: DROP ATTRIBUTE discards that attribute's "
        "value in every column of this composite type; ADD ATTRIBUTE is reversible"
    ),
    # Renames and moves
    _SK.RENAME_TABLE: _RENAME,
    _SK.RENAME_COLUMN: _RENAME,
    _SK.RENAME_INDEX: _RENAME,
    _SK.RENAME_CONSTRAINT: _RENAME,
    _SK.RENAME_VIEW: _RENAME,
    _SK.RENAME_MATVIEW: _RENAME,
    _SK.RENAME_TYPE: _RENAME,
    _SK.RENAME_OTHER: _RENAME,
    _SK.ALTER_OBJECT_SCHEMA: _rev("moving the object back restores the previous state"),
    _SK.ALTER_OWNER: _rev("ownership can be transferred back"),
    # Maintenance
    _SK.REINDEX: _MAINTENANCE,
    _SK.REINDEX_CONCURRENTLY: _MAINTENANCE,
    _SK.VACUUM: _MAINTENANCE,
    _SK.VACUUM_FULL: _MAINTENANCE,
    _SK.ANALYZE: _MAINTENANCE,
    _SK.CLUSTER: _MAINTENANCE,
    _SK.REFRESH_MATVIEW: _rev(
        "contents are derived from the defining query; the pre-refresh snapshot "
        "is discarded but was itself derived data"
    ),
    _SK.REFRESH_MATVIEW_CONCURRENTLY: _rev(
        "contents are derived from the defining query; the pre-refresh snapshot "
        "is discarded but was itself derived data"
    ),
    # Sequences, types, domains
    _SK.ALTER_SEQUENCE: _unknown(
        "which ALTER SEQUENCE form is not recorded: RESTART discards the current "
        "position (already-used values can be re-issued); the other forms are "
        "reversible"
    ),
    _SK.ALTER_ENUM_ADD_VALUE: _irrev(
        "nothing yet — but the added label can never be removed (Postgres has no "
        "ALTER TYPE ... DROP VALUE); it can only be renamed",
        "enum labels are permanent once committed",
    ),
    _SK.ALTER_ENUM_RENAME_VALUE: _RENAME,
    _SK.ALTER_DOMAIN: _unknown("which ALTER DOMAIN form is not recorded"),
    # Routines, triggers, policies
    _SK.ALTER_FUNCTION: _SETTINGS,
    _SK.ALTER_POLICY: _SETTINGS,
    _SK.ALTER_STATISTICS: _SETTINGS,
    _SK.ALTER_EXTENSION: _unknown(
        "which ALTER EXTENSION form is not recorded; UPDATE runs the extension's "
        "upgrade scripts, which have no downgrade path"
    ),
    # ACLs and settings
    _SK.GRANT: _SETTINGS,
    _SK.REVOKE: _SETTINGS,
    _SK.ALTER_DEFAULT_PRIVILEGES: _SETTINGS,
    _SK.ALTER_ROLE: _SETTINGS,
    _SK.ALTER_DATABASE: _SETTINGS,
    _SK.ALTER_SYSTEM: _SETTINGS,
    _SK.COMMENT_ON: _rev(
        "a comment can be set back; the previous comment text must come from history"
    ),
    _SK.SET: _NO_CHANGE,
    _SK.RESET: _NO_CHANGE,
    _SK.SHOW: _READ_ONLY,
    _SK.LOCK_TABLE: _NO_CHANGE,
    # DML
    _SK.INSERT: _rev(
        "inserted rows can be deleted, provided the migration can identify them"
    ),
    _SK.COPY_FROM: _rev(
        "loaded rows can be deleted, provided the migration can identify them"
    ),
    _SK.UPDATE: _irrev(
        "the pre-update values of every matched row",
        "old row versions are dead after commit and vacuumed away",
    ),
    _SK.UPDATE_WITHOUT_WHERE: _irrev(
        "the pre-update values of every row in the table",
        "old row versions are dead after commit and vacuumed away",
    ),
    _SK.UPDATE_BATCHED: _irrev(
        "the pre-update values of every matched row",
        "old row versions are dead after commit and vacuumed away",
    ),
    _SK.DELETE: _irrev("every deleted row", "deleted rows exist nowhere else"),
    _SK.DELETE_WITHOUT_WHERE: _irrev(
        "every row of the table", "deleted rows exist nowhere else"
    ),
    _SK.DELETE_BATCHED: _irrev("every deleted row", "deleted rows exist nowhere else"),
    _SK.MERGE: _irrev(
        "the pre-merge values of updated rows and every deleted row",
        "old row versions are dead after commit",
    ),
    _SK.SELECT: _READ_ONLY,
    _SK.COPY_TO: _READ_ONLY,
    _SK.CALL: _unknown("the procedure body is opaque; what it changes is unknown"),
    # Composed / unknown
    _SK.DO_BLOCK: _unknown("composed from the block's inner statements"),
    _SK.OTHER: _unknown("statement form not modeled"),
}


_ACTIONS: dict[AlterTableActionKind, ReversibilityAssessment] = {
    _AK.ADD_COLUMN: _rev("dropping the added column undoes it; no pre-existing data is touched"),
    _AK.ADD_COLUMN_NOT_NULL_NO_DEFAULT: _rev(
        "dropping the added column undoes it; no pre-existing data is touched"
    ),
    _AK.ADD_COLUMN_DEFAULT_NONVOLATILE: _rev(
        "dropping the added column undoes it; no pre-existing data is touched"
    ),
    _AK.ADD_COLUMN_DEFAULT_VOLATILE: _rev(
        "dropping the added column undoes it; no pre-existing data is touched"
    ),
    _AK.ADD_COLUMN_DEFAULT_UNKNOWN_VOLATILITY: _rev(
        "dropping the added column undoes it; no pre-existing data is touched"
    ),
    _AK.ADD_COLUMN_SERIAL: _rev(
        "dropping the added column (and its sequence) undoes it; no pre-existing "
        "data is touched"
    ),
    _AK.ADD_COLUMN_IDENTITY: _rev(
        "dropping the added column (and its sequence) undoes it; no pre-existing "
        "data is touched"
    ),
    _AK.ADD_COLUMN_GENERATED_STORED: _rev(
        "dropping the added column undoes it; no pre-existing data is touched"
    ),
    _AK.ADD_COLUMN_GENERATED_VIRTUAL: _rev(
        "dropping the added column undoes it; no pre-existing data is touched"
    ),
    _AK.DROP_COLUMN: _irrev(
        "every value in the dropped column, plus its default, its constraints, "
        "and any index built on it",
        "column data exists nowhere else",
    ),
    _AK.ALTER_COLUMN_TYPE: _unknown(
        "whether the cast loses information (precision, timezone, truncation) is "
        "not derivable statically; a lossless change is reversible, a lossy one "
        "destroys the original values"
    ),
    _AK.SET_COLUMN_DEFAULT: _SETTINGS,
    _AK.DROP_COLUMN_DEFAULT: _SETTINGS,
    _AK.SET_NOT_NULL: _rev("DROP NOT NULL restores the previous state exactly"),
    _AK.DROP_NOT_NULL: _rev(
        "SET NOT NULL restores it (it may fail if NULLs were inserted meanwhile, "
        "but no data is lost)"
    ),
    _AK.ADD_IDENTITY: _rev("DROP IDENTITY undoes it; existing values are untouched"),
    _AK.SET_IDENTITY: _SETTINGS,
    _AK.DROP_IDENTITY: _irrev(
        "the backing identity sequence and its current position",
        "the sequence is dropped with the identity",
    ),
    _AK.SET_EXPRESSION: _rev(
        "generated values are derived; setting the previous expression back "
        "recomputes them (the expression must come from history)"
    ),
    _AK.DROP_EXPRESSION: _rev(
        "the column keeps its values; re-adding the expression recomputes them"
    ),
    _AK.SET_STATISTICS: _SETTINGS,
    _AK.SET_STORAGE: _SETTINGS,
    _AK.SET_COMPRESSION: _SETTINGS,
    _AK.SET_COLUMN_OPTIONS: _SETTINGS,
    _AK.RESET_COLUMN_OPTIONS: _SETTINGS,
    _AK.ADD_PRIMARY_KEY: _CONSTRAINT_ADD,
    _AK.ADD_PRIMARY_KEY_USING_INDEX: _CONSTRAINT_ADD,
    _AK.ADD_UNIQUE: _CONSTRAINT_ADD,
    _AK.ADD_UNIQUE_USING_INDEX: _CONSTRAINT_ADD,
    _AK.ADD_FOREIGN_KEY: _CONSTRAINT_ADD,
    _AK.ADD_FOREIGN_KEY_NOT_VALID: _CONSTRAINT_ADD,
    _AK.ADD_CHECK: _CONSTRAINT_ADD,
    _AK.ADD_CHECK_NOT_VALID: _CONSTRAINT_ADD,
    _AK.ADD_NOT_NULL_CONSTRAINT: _CONSTRAINT_ADD,
    _AK.ADD_NOT_NULL_CONSTRAINT_NOT_VALID: _CONSTRAINT_ADD,
    _AK.ADD_EXCLUSION: _CONSTRAINT_ADD,
    _AK.ADD_CONSTRAINT_OTHER: _CONSTRAINT_ADD,
    _AK.VALIDATE_CONSTRAINT: _rev(
        "validation is metadata; the NOT VALID marker cannot be restored "
        "directly, but nothing is lost"
    ),
    _AK.DROP_CONSTRAINT: _rev(
        "re-adding the constraint restores it (definition from history; "
        "revalidation and index-rebuild cost, no data loss)"
    ),
    _AK.ALTER_CONSTRAINT: _SETTINGS,
    _AK.SET_STORAGE_PARAMS: _SETTINGS,
    _AK.RESET_STORAGE_PARAMS: _SETTINGS,
    _AK.REPLACE_STORAGE_PARAMS: _SETTINGS,
    _AK.SET_LOGGED: _rev("SET UNLOGGED flips it back; contents are unchanged"),
    _AK.SET_UNLOGGED: _rev(
        "SET LOGGED flips it back; note that while unlogged the table's contents "
        "do not survive a crash and are not replicated"
    ),
    _AK.SET_TABLESPACE: _rev("moving the table back restores the previous placement"),
    _AK.SET_ACCESS_METHOD: _rev("setting the previous access method rewrites back"),
    _AK.ATTACH_PARTITION: _rev("DETACH PARTITION undoes it; rows stay in the partition"),
    _AK.DETACH_PARTITION: _rev("ATTACH PARTITION undoes it; rows stay in the table"),
    _AK.DETACH_PARTITION_CONCURRENTLY: _rev(
        "ATTACH PARTITION undoes it; rows stay in the table"
    ),
    _AK.DETACH_PARTITION_FINALIZE: _rev("ATTACH PARTITION undoes it; rows stay in the table"),
    _AK.ENABLE_TRIGGER: _rev("DISABLE TRIGGER restores the previous state"),
    _AK.DISABLE_TRIGGER: _rev(
        "ENABLE TRIGGER restores the setting; side effects the trigger would have "
        "produced for writes made while disabled are never recovered"
    ),
    _AK.ENABLE_RULE: _rev("DISABLE RULE restores the previous state"),
    _AK.DISABLE_RULE: _rev("ENABLE RULE restores the setting"),
    _AK.ENABLE_ROW_SECURITY: _rev("DISABLE ROW SECURITY restores the previous state"),
    _AK.DISABLE_ROW_SECURITY: _rev("ENABLE ROW SECURITY restores the previous state"),
    _AK.FORCE_ROW_SECURITY: _rev("NO FORCE ROW SECURITY restores the previous state"),
    _AK.NO_FORCE_ROW_SECURITY: _rev("FORCE ROW SECURITY restores the previous state"),
    _AK.CLUSTER_ON: _SETTINGS,
    _AK.DROP_CLUSTER: _SETTINGS,
    _AK.CHANGE_OWNER: _rev("ownership can be transferred back"),
    _AK.INHERIT: _rev("NO INHERIT undoes it"),
    _AK.NO_INHERIT: _rev("INHERIT undoes it"),
    _AK.OF_TYPE: _rev("NOT OF undoes it"),
    _AK.NOT_OF: _rev("OF <type> undoes it"),
    _AK.REPLICA_IDENTITY: _SETTINGS,
    _AK.OTHER: _unknown("subcommand form not modeled"),
}


def statement_reversibility(kind: StatementKind) -> ReversibilityAssessment:
    return _STATEMENTS[kind]


def action_reversibility(kind: AlterTableActionKind) -> ReversibilityAssessment:
    return _ACTIONS[kind]


def combine(
    parts: tuple[ReversibilityAssessment, ...],
) -> ReversibilityAssessment:
    """A composite is as irreversible as its worst part.

    IRREVERSIBLE dominates (something is definitely lost), then UNKNOWN
    (something might be), then REVERSIBLE.
    """
    rank = {
        Reversibility.REVERSIBLE: 0,
        Reversibility.UNKNOWN: 1,
        Reversibility.IRREVERSIBLE: 2,
    }
    worst = parts[0]
    for part in parts[1:]:
        if rank[part.reversibility] > rank[worst.reversibility]:
            worst = part
    return worst
