"""Typed internal representation of parsed migration scripts.

The taxonomy here is deliberately fine-grained: two statements get different
kinds whenever their locking or rewrite behavior in production can differ,
even if they look superficially similar (e.g. ``ADD COLUMN`` with a volatile
default vs a constant one, ``CREATE INDEX`` vs ``CREATE INDEX CONCURRENTLY``).
No lock analysis happens in this layer; it only produces the classification.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum, auto


class StatementKind(StrEnum):
    """Top-level statement forms, one per locking-relevant shape."""

    # Transaction control
    BEGIN = auto()
    COMMIT = auto()
    ROLLBACK = auto()
    SAVEPOINT = auto()
    RELEASE_SAVEPOINT = auto()
    ROLLBACK_TO_SAVEPOINT = auto()
    TRANSACTION_OTHER = auto()  # two-phase commit forms

    # Tables
    CREATE_TABLE = auto()
    CREATE_TABLE_PARTITION_OF = auto()  # locks the parent partitioned table
    CREATE_TABLE_AS = auto()  # scans the source query while holding locks
    SELECT_INTO = auto()
    DROP_TABLE = auto()
    TRUNCATE = auto()

    # ALTER on relations (pglast uses one node for all of these; the lock
    # profile differs by object class, so they get distinct kinds)
    ALTER_TABLE = auto()
    ALTER_INDEX = auto()
    ALTER_VIEW = auto()
    ALTER_MATVIEW = auto()
    CREATE_FOREIGN_TABLE = auto()
    ALTER_FOREIGN_TABLE = auto()
    ALTER_COMPOSITE_TYPE = auto()  # ALTER TYPE ... ADD/DROP ATTRIBUTE

    # Renames (ALTER INDEX RENAME takes a weaker lock than table renames)
    RENAME_TABLE = auto()
    RENAME_COLUMN = auto()
    RENAME_INDEX = auto()
    RENAME_CONSTRAINT = auto()
    RENAME_VIEW = auto()
    RENAME_MATVIEW = auto()
    RENAME_TYPE = auto()  # first step of the Prisma enum-change recipe
    RENAME_OTHER = auto()

    ALTER_OBJECT_SCHEMA = auto()  # SET SCHEMA
    ALTER_OWNER = auto()

    # Indexes
    CREATE_INDEX = auto()
    CREATE_INDEX_CONCURRENTLY = auto()
    DROP_INDEX = auto()
    DROP_INDEX_CONCURRENTLY = auto()
    REINDEX = auto()
    REINDEX_CONCURRENTLY = auto()

    # Maintenance / whole-table rewrites
    VACUUM = auto()
    VACUUM_FULL = auto()
    ANALYZE = auto()
    CLUSTER = auto()
    REFRESH_MATVIEW = auto()
    REFRESH_MATVIEW_CONCURRENTLY = auto()

    # Views and sequences
    CREATE_VIEW = auto()
    CREATE_MATVIEW = auto()
    DROP_VIEW = auto()
    DROP_MATVIEW = auto()
    CREATE_SEQUENCE = auto()
    ALTER_SEQUENCE = auto()
    DROP_SEQUENCE = auto()

    # Types and domains
    CREATE_ENUM_TYPE = auto()
    CREATE_COMPOSITE_TYPE = auto()
    CREATE_RANGE_TYPE = auto()
    ALTER_ENUM_ADD_VALUE = auto()  # transaction restrictions differ by PG version
    ALTER_ENUM_RENAME_VALUE = auto()
    CREATE_DOMAIN = auto()
    ALTER_DOMAIN = auto()
    DROP_TYPE = auto()
    DROP_DOMAIN = auto()

    # Extended statistics and aggregates
    CREATE_STATISTICS = auto()  # brief SHARE UPDATE EXCLUSIVE on the table
    ALTER_STATISTICS = auto()
    CREATE_AGGREGATE = auto()
    DROP_AGGREGATE = auto()

    # Functions, triggers, policies, rules
    CREATE_FUNCTION = auto()
    CREATE_PROCEDURE = auto()
    ALTER_FUNCTION = auto()
    DROP_FUNCTION = auto()
    DROP_PROCEDURE = auto()
    CREATE_TRIGGER = auto()
    DROP_TRIGGER = auto()
    CREATE_POLICY = auto()
    ALTER_POLICY = auto()
    DROP_POLICY = auto()
    CREATE_RULE = auto()

    # Extensions, schemas, roles, ACLs
    CREATE_EXTENSION = auto()
    ALTER_EXTENSION = auto()
    DROP_EXTENSION = auto()
    CREATE_SCHEMA = auto()
    DROP_SCHEMA = auto()
    CREATE_ROLE = auto()
    ALTER_ROLE = auto()
    DROP_ROLE = auto()
    GRANT = auto()
    REVOKE = auto()
    ALTER_DEFAULT_PRIVILEGES = auto()
    COMMENT_ON = auto()

    # DML (backfills and data fixes inside migrations). UPDATE/DELETE split
    # by shape: an unbatched, WHERE-less statement on a large table holds
    # row locks and generates bloat for its whole runtime.
    INSERT = auto()
    UPDATE = auto()  # has WHERE, no recognized batching idiom
    UPDATE_WITHOUT_WHERE = auto()  # touches every row
    UPDATE_BATCHED = auto()  # LIMIT-bounded subselect/CTE, ctid, or key window
    DELETE = auto()
    DELETE_WITHOUT_WHERE = auto()
    DELETE_BATCHED = auto()
    MERGE = auto()
    SELECT = auto()
    COPY_FROM = auto()  # bulk write: ROW EXCLUSIVE, fires triggers
    COPY_TO = auto()  # read-only: ACCESS SHARE
    CALL = auto()

    # Session / server state
    SET = auto()
    RESET = auto()
    SHOW = auto()
    LOCK_TABLE = auto()
    ALTER_SYSTEM = auto()  # cannot run inside a transaction block
    CREATE_DATABASE = auto()
    ALTER_DATABASE = auto()
    DROP_DATABASE = auto()
    CREATE_TABLESPACE = auto()
    DROP_TABLESPACE = auto()

    DO_BLOCK = auto()
    OTHER = auto()


class AlterTableActionKind(StrEnum):
    """One ALTER TABLE subcommand, split by locking/rewrite-relevant form."""

    # ADD COLUMN: the default expression decides whether the table is rewritten
    ADD_COLUMN = auto()  # nullable, no default: catalog-only
    ADD_COLUMN_NOT_NULL_NO_DEFAULT = auto()  # catalog-only, fails on non-empty tables
    ADD_COLUMN_DEFAULT_NONVOLATILE = auto()  # PG11+ catalog-only fast path
    ADD_COLUMN_DEFAULT_VOLATILE = auto()  # forces a full table rewrite
    ADD_COLUMN_DEFAULT_UNKNOWN_VOLATILITY = auto()  # can't prove either way
    ADD_COLUMN_SERIAL = auto()  # serial pseudo-type implies volatile nextval()
    ADD_COLUMN_IDENTITY = auto()  # backfilled with nextval(): rewrite
    ADD_COLUMN_GENERATED_STORED = auto()  # expression is computed and stored: rewrite
    ADD_COLUMN_GENERATED_VIRTUAL = auto()  # PG18 virtual: not stored, no rewrite
    DROP_COLUMN = auto()

    # Column alterations
    ALTER_COLUMN_TYPE = auto()
    SET_COLUMN_DEFAULT = auto()  # applies to future inserts only
    DROP_COLUMN_DEFAULT = auto()
    SET_NOT_NULL = auto()  # full validation scan unless a proving CHECK exists
    DROP_NOT_NULL = auto()
    ADD_IDENTITY = auto()
    SET_IDENTITY = auto()
    DROP_IDENTITY = auto()
    SET_EXPRESSION = auto()
    DROP_EXPRESSION = auto()
    SET_STATISTICS = auto()
    SET_STORAGE = auto()
    SET_COMPRESSION = auto()
    SET_COLUMN_OPTIONS = auto()
    RESET_COLUMN_OPTIONS = auto()

    # Constraints: NOT VALID / USING INDEX variants skip the blocking scan
    ADD_PRIMARY_KEY = auto()  # builds a unique index under ACCESS EXCLUSIVE
    ADD_PRIMARY_KEY_USING_INDEX = auto()  # adopts an existing index
    ADD_UNIQUE = auto()
    ADD_UNIQUE_USING_INDEX = auto()
    ADD_FOREIGN_KEY = auto()  # validates existing rows, locks both tables
    ADD_FOREIGN_KEY_NOT_VALID = auto()
    ADD_CHECK = auto()  # validation scan
    ADD_CHECK_NOT_VALID = auto()
    ADD_NOT_NULL_CONSTRAINT = auto()  # PG18 named NOT NULL constraint
    ADD_NOT_NULL_CONSTRAINT_NOT_VALID = auto()  # PG18: skips the validation scan
    ADD_EXCLUSION = auto()
    ADD_CONSTRAINT_OTHER = auto()
    VALIDATE_CONSTRAINT = auto()  # scan under SHARE UPDATE EXCLUSIVE (online)
    DROP_CONSTRAINT = auto()
    ALTER_CONSTRAINT = auto()

    # Storage parameters
    SET_STORAGE_PARAMS = auto()
    RESET_STORAGE_PARAMS = auto()
    # AT_ReplaceRelOptions is only generated internally (CREATE OR REPLACE
    # VIEW transformation), never by parsed SQL; kept for enum completeness.
    REPLACE_STORAGE_PARAMS = auto()

    # Whole-table changes
    SET_LOGGED = auto()  # rewrite
    SET_UNLOGGED = auto()  # rewrite
    SET_TABLESPACE = auto()  # physically moves the table
    SET_ACCESS_METHOD = auto()  # rewrite

    # Partitions
    ATTACH_PARTITION = auto()  # scans partition unless constraint proves bounds
    DETACH_PARTITION = auto()
    DETACH_PARTITION_CONCURRENTLY = auto()
    DETACH_PARTITION_FINALIZE = auto()

    # Triggers, rules, row security
    ENABLE_TRIGGER = auto()
    DISABLE_TRIGGER = auto()
    ENABLE_RULE = auto()
    DISABLE_RULE = auto()
    ENABLE_ROW_SECURITY = auto()
    DISABLE_ROW_SECURITY = auto()
    FORCE_ROW_SECURITY = auto()
    NO_FORCE_ROW_SECURITY = auto()

    # Misc
    CLUSTER_ON = auto()
    DROP_CLUSTER = auto()
    CHANGE_OWNER = auto()
    INHERIT = auto()
    NO_INHERIT = auto()
    OF_TYPE = auto()
    NOT_OF = auto()
    REPLICA_IDENTITY = auto()
    OTHER = auto()


class Volatility(StrEnum):
    """Volatility of a default/generation expression.

    Ordered from safest to most dangerous; ``UNKNOWN`` ranks below
    ``VOLATILE`` but must be treated conservatively by later analysis.
    """

    CONSTANT = auto()  # bare literal or NULL
    IMMUTABLE = auto()
    STABLE = auto()
    UNKNOWN = auto()  # unrecognized function or construct
    VOLATILE = auto()


@dataclass(frozen=True, slots=True)
class SourceSpan:
    """Character range of a statement in the original file text."""

    start: int
    end: int
    line: int  # 1-based line number of ``start``


@dataclass(frozen=True, slots=True)
class QualifiedName:
    """A possibly schema-qualified object name."""

    name: str
    schema: str | None = None

    def __str__(self) -> str:
        return f"{self.schema}.{self.name}" if self.schema else self.name


@dataclass(frozen=True, slots=True)
class DefaultInfo:
    """A DEFAULT clause expression and its volatility.

    Only populated for explicit DEFAULT clauses; identity, serial, and
    generated columns encode their implied expression in the action kind.
    """

    volatility: Volatility
    expression: str | None = None  # deparsed SQL; None if deparse failed
    # Function names (dotted when schema-qualified) whose unrecognized
    # volatility is why the whole expression is UNKNOWN. Populated only when
    # ``volatility`` is UNKNOWN: these are exactly the names a live
    # pg_proc.provolatile lookup can decide; offline they stay undecided.
    unknown_functions: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AlterTableAction:
    """A single ALTER TABLE subcommand.

    ``not_null`` records that an explicit NOT NULL clause was written; the
    NOT NULL that Postgres implies for identity and primary-key columns is
    the analyzer's inference to make. ``constraint_name`` also carries the
    trigger or rule name for the ENABLE/DISABLE trigger and rule kinds.
    """

    kind: AlterTableActionKind
    column: str | None = None
    constraint_name: str | None = None
    column_type: str | None = None  # new/added column type, deparsed
    default: DefaultInfo | None = None
    not_null: bool = False
    not_valid: bool = False
    has_using_expression: bool = False  # ALTER COLUMN TYPE ... USING
    using_index: str | None = None  # ADD constraint ... USING INDEX
    inline_constraints: tuple[str, ...] = ()  # extra column-level constraints
    partition: QualifiedName | None = None
    # Table named by a FOREIGN KEY / REFERENCES clause: it is locked too
    # (SHARE ROW EXCLUSIVE for ADD FOREIGN KEY). Set for the ADD_FOREIGN_KEY
    # kinds and for ADD COLUMN with an inline REFERENCES constraint.
    referenced_table: QualifiedName | None = None
    cascade: bool = False
    missing_ok: bool = False  # IF [NOT] EXISTS on the subcommand
    detail: str | None = None  # variant info / raw pglast subtype for OTHER


@dataclass(frozen=True, slots=True)
class CreateTableDetails:
    """CREATE TABLE shape.

    ``referenced_tables`` lists the pre-existing tables named by inline
    ``REFERENCES`` / ``FOREIGN KEY`` clauses: creating the FK adds triggers to
    them, so they are locked even though the created table is brand new.
    """

    persistence: str  # "permanent" | "unlogged" | "temporary"
    if_not_exists: bool = False
    partition_of: QualifiedName | None = None
    inherits: bool = False
    referenced_tables: tuple[QualifiedName, ...] = ()


@dataclass(frozen=True, slots=True)
class CreateTableAsDetails:
    """CREATE TABLE AS / CREATE MATERIALIZED VIEW / REFRESH details."""

    no_data: bool = False  # WITH NO DATA skips the populating scan


@dataclass(frozen=True, slots=True)
class IndexDetails:
    index_name: str | None  # None: Postgres picks the name
    unique: bool = False
    method: str = "btree"
    partial: bool = False  # has a WHERE clause
    has_expression: bool = False  # any index column is an expression, not a name
    if_not_exists: bool = False


@dataclass(frozen=True, slots=True)
class DropDetails:
    object_names: tuple[str, ...] = ()
    cascade: bool = False  # CASCADE also drops dependent objects
    missing_ok: bool = False


@dataclass(frozen=True, slots=True)
class DmlDetails:
    """Shape of an UPDATE/DELETE backfill.

    ``batch_signals`` lists the static batching idioms found: ``"limit"``
    (a LIMIT-bounded subselect or CTE feeds the statement), ``"ctid"``
    (physical ctid targeting), ``"key_window"`` (both a lower and an upper
    bound on the same column). Detection is syntactic and errs toward NOT
    claiming a batch; absence of a signal is not proof the statement is
    unbatched at runtime.
    """

    has_where: bool
    batch_signals: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class InsertDetails:
    """Where an INSERT's rows come from.

    ``source`` is ``"values"`` (a VALUES list: bounded, constant cost),
    ``"default_values"`` (one row), or ``"select"`` (query-fed: cost follows
    the source query, so an ``INSERT ... SELECT`` backfill can run for as long
    as the query does).
    """

    source: str


@dataclass(frozen=True, slots=True)
class TruncateDetails:
    cascade: bool = False  # also truncates every FK-referencing table
    restart_identity: bool = False  # resets owned sequences


@dataclass(frozen=True, slots=True)
class ReindexDetails:
    scope: str  # "index" | "table" | "schema" | "system" | "database"
    object_name: str | None = None  # for schema/system/database scopes


@dataclass(frozen=True, slots=True)
class RenameDetails:
    old_name: str | None
    new_name: str | None
    object_type: str  # pglast ObjectType name, e.g. "OBJECT_COLUMN"


@dataclass(frozen=True, slots=True)
class SetDetails:
    name: str | None  # None for RESET ALL / SET TRANSACTION forms
    is_local: bool = False  # SET LOCAL scopes to the enclosing transaction


@dataclass(frozen=True, slots=True)
class LockDetails:
    mode: int  # numeric Postgres lock level 1..8
    mode_name: str  # e.g. "ACCESS EXCLUSIVE"
    nowait: bool = False


@dataclass(frozen=True, slots=True)
class TransactionDetails:
    savepoint_name: str | None = None
    chain: bool = False  # COMMIT AND CHAIN


@dataclass(frozen=True, slots=True)
class DoBlockDetails:
    """A DO block with any statically extractable inner statements.

    Inner statements inherit the span of the enclosing DO statement because
    plpgsql does not report reliable source offsets. ``dynamic_sql_count``
    counts EXECUTE statements whose SQL is built at runtime and therefore
    cannot be classified statically.
    """

    language: str
    statements: tuple[ParsedStatement, ...] = ()
    dynamic_sql_count: int = 0
    fully_parsed: bool = True  # False: body opaque or partially unparseable
    # True when at least one embedded statement sits inside an IF whose
    # condition probes information_schema, pg_catalog, or to_reg*(): the
    # statement may not execute at all, so no unconditional verdict applies.
    existence_guarded: bool = False


StatementDetails = (
    CreateTableDetails
    | CreateTableAsDetails
    | IndexDetails
    | DropDetails
    | TruncateDetails
    | ReindexDetails
    | RenameDetails
    | SetDetails
    | LockDetails
    | TransactionDetails
    | DoBlockDetails
    | DmlDetails
    | InsertDetails
)


@dataclass(frozen=True, slots=True)
class ParsedStatement:
    """One classified SQL statement.

    ``only`` records the ONLY keyword on the primary relation of ALTER TABLE
    and CREATE INDEX statements — the statically visible discriminator for
    whether the operation recurses into partitions/children (``CREATE INDEX
    ... ON ONLY parent`` is the non-blocking partitioned-index pattern).
    """

    kind: StatementKind
    sql: str  # verbatim statement text (no trailing semicolon)
    span: SourceSpan
    node_tag: str  # pglast AST class name, e.g. "AlterTableStmt"
    targets: tuple[QualifiedName, ...] = ()  # affected objects, primary first
    alter_actions: tuple[AlterTableAction, ...] = ()
    details: StatementDetails | None = None
    only: bool = False


@dataclass(frozen=True, slots=True)
class TransactionGroup:
    """A run of statements sharing one (explicit or implicit) transaction.

    ``statement_indices`` index into ``MigrationScript.statements`` and
    exclude the BEGIN/COMMIT/ROLLBACK statements themselves, which are
    referenced by ``opened_by``/``closed_by``. Implicit groups are the
    statements outside any explicit block; whether the migration runner
    wraps them in a transaction is the analyzer's concern, not recorded here.
    A group opened by ``COMMIT AND CHAIN``/``ROLLBACK AND CHAIN`` is explicit
    with ``opened_by`` pointing at that chaining statement.
    """

    explicit: bool
    statement_indices: tuple[int, ...]
    opened_by: int | None = None
    closed_by: int | None = None
    rolled_back: bool = False


@dataclass(frozen=True, slots=True)
class MigrationScript:
    """A fully parsed and classified migration file.

    ``baseline_shaped`` flags squash/baseline-style files: many statements
    that exclusively create objects and then only reference objects created
    earlier in the same file. Such files run against empty databases; the
    flag records the shape only — risk judgment belongs to the analyzer.
    """

    source: str
    statements: tuple[ParsedStatement, ...]
    transaction_groups: tuple[TransactionGroup, ...]
    warnings: tuple[str, ...] = ()
    path: str | None = None
    baseline_shaped: bool = False

    @property
    def has_explicit_transactions(self) -> bool:
        return any(group.explicit for group in self.transaction_groups)
