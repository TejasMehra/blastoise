"""Typed model of a live database snapshot.

A snapshot records production context the parsed migration cannot know:
table sizes and their staleness, lock waiters, replication topology, the
server version. Every field that can independently fail to be gathered is a
:class:`Fact` — either an available value or an explicit ``unavailable``
marker carrying the reason. Downstream must be able to distinguish "false"
from "unknown", so nothing here is ever a silent ``None``.

Serialization is canonical and deterministic: sorted keys, tuples as JSON
arrays sorted at capture time, no floats anywhere (sizes are integer bytes,
durations integer milliseconds, timestamps ISO-8601 UTC strings). The
canonical form is what gets hashed into the evidence bundle.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from enum import StrEnum

from blastoise.ir import QualifiedName

type JsonValue = bool | int | str | list[JsonValue] | dict[str, JsonValue] | None

SNAPSHOT_FORMAT = 5
"""Bump when the serialized shape changes; the evidence bundle records it.

Format 3 extends ``IndexFacts`` with the index's access method, its
partial/expression shape, opclass defaultness, and the set of table
columns it depends on (via ``pg_depend``) — the facts the ALTER COLUMN
TYPE narrowing needs to rule dependent-index rebuilds in or out.

Format 4 adds ``LockWaiter.blockers_all_idle`` — whether the backends
holding the conflicting lock are idle in a transaction rather than
actively running — so the contention escalation can tell a transient
queue behind an idle holder (a timing problem) from sustained active
contention (a block).

Format 5 adds ``LiveSnapshot.calibration`` — the calibration probe's
reading on the target (:class:`CalibrationFacts`): a bounded, read-only,
catalog-free operation of known cost, timed on the target, so the
duration model can scale its estimates to the hardware the migration
will actually run on instead of assuming the machine the constants were
measured on.
"""


@dataclass(frozen=True, slots=True)
class Fact[T]:
    """One introspected fact: an available value or an explicit absence.

    ``Fact.of(None)`` is a *known* null (e.g. ``last_analyze`` on a table
    that has genuinely never been analyzed); ``Fact.unavailable(reason)``
    means the fact could not be gathered and says why. The two serialize
    differently and downstream relies on the difference.
    """

    available: bool
    value: T | None = None
    reason: str | None = None

    @classmethod
    def of(cls, value: T | None) -> Fact[T]:
        return cls(available=True, value=value)

    @classmethod
    def unavailable(cls, reason: str) -> Fact[T]:
        return cls(available=False, reason=reason)


@dataclass(frozen=True, slots=True)
class ConnectionTarget:
    """The connection target with credentials redacted.

    Only host, port, database, and role name survive; the password (and any
    other libpq parameter) is dropped before the snapshot object is built,
    so no code path can serialize it.
    """

    host: str | None
    port: str | None
    dbname: str | None
    user: str | None


@dataclass(frozen=True, slots=True)
class CaptureLimits:
    """The bounds the introspection ran under.

    Recorded so that ``unavailable: statement timeout`` markers in the same
    snapshot can be interpreted, and so two snapshots hashing differently
    because of different bounds is visible.
    """

    connect_timeout_s: int
    statement_timeout_ms: int
    lock_timeout_ms: int
    long_transaction_threshold_ms: int
    max_listed_transactions: int


@dataclass(frozen=True, slots=True)
class RoleFacts:
    """What the read-only gate verified about the connected role."""

    role: str
    transaction_read_only: bool
    superuser: bool
    can_read_all_stats: bool  # pg_read_all_stats (directly or via pg_monitor)
    warnings: tuple[str, ...]  # non-fatal write-adjacent capabilities found


@dataclass(frozen=True, slots=True)
class ServerFacts:
    server_version_num: Fact[int]  # e.g. 170010
    server_version: Fact[str]
    pg_major: Fact[int]  # server_version_num // 10000; what the catalog keys on
    in_recovery: Fact[bool]
    server_now: Fact[str]  # server clock; stat/lock ages are relative to this
    lock_timeout_ms: Fact[int]  # configured value (reset_val), 0 = disabled
    statement_timeout_ms: Fact[int]  # configured value (reset_val), 0 = disabled
    timezone: Fact[str]  # configured TimeZone (reset_val), not this session's UTC override


@dataclass(frozen=True, slots=True)
class IndexFacts:
    """One index on a captured relation.

    ``depends_on_columns`` is read from ``pg_depend`` (column-level entries
    of the index on its table), so it covers key columns *and* columns
    referenced only by index expressions or a partial predicate — the same
    dependency walk ``ALTER TABLE`` uses to find indexes a column change
    forces it to touch. ``default_opclasses`` is true when every key column
    uses its type's default operator class; a non-default opclass may stop
    ``CheckIndexCompatible`` from reusing the index across a type change.
    """

    name: str
    valid: bool  # pg_index.indisvalid; false = failed CONCURRENTLY leftover
    size_bytes: Fact[int]
    method: str = "btree"  # pg_am.amname
    partial: bool = False  # pg_index.indpred is set
    has_expressions: bool = False  # pg_index.indexprs is set
    default_opclasses: bool = True  # every key column uses the type's default opclass
    depends_on_columns: tuple[str, ...] = ()  # via pg_depend, sorted, deduplicated


@dataclass(frozen=True, slots=True)
class ColumnFacts:
    """One live column of a target relation (dropped columns excluded).

    All leaf values come from a single pg_attribute/pg_type/pg_attrdef
    query, so they succeed or fail together: the containing
    ``RelationFacts.columns`` is the :class:`Fact`. ``default_expression``
    is ``None`` exactly when ``has_default`` is false — a known state, not
    a gathering failure.
    """

    name: str
    attnum: int  # ordinal position; gaps where columns were dropped
    data_type: str  # format_type(atttypid, atttypmod): current type, typmod included
    type_oid: int
    typmod: int  # raw atttypmod; -1 = no modifier
    not_null: bool  # attnotnull
    has_default: bool  # atthasdef
    default_expression: str | None  # pg_get_expr(adbin); None iff no default
    identity: str  # attidentity: '' none, 'a' GENERATED ALWAYS, 'd' BY DEFAULT
    generated: str  # attgenerated: '' none, 's' STORED, 'v' VIRTUAL (PG 18); '' before PG 12
    is_domain: bool  # the column's declared type is a domain
    domain_base_type: str | None  # the domain's base type; None when not a domain
    domain_not_null: bool  # the domain itself is declared NOT NULL
    domain_constraint_count: int  # CHECK constraints on the domain; 0 when not a domain


@dataclass(frozen=True, slots=True)
class ConstraintFacts:
    """One pg_constraint row on a target relation.

    ``contype`` and ``validated`` are what VALIDATE/DROP CONSTRAINT rows in
    the lock catalog declare missing (an FK validation/drop also locks the
    referenced table, which the statement does not name); a validated CHECK
    whose ``check_expression`` proves a column non-null is what lets
    SET NOT NULL skip its scan on PG 12+.
    """

    name: str
    contype: str  # c=check f=fk p=pk u=unique t=trigger x=exclusion n=not-null (PG 18)
    validated: bool  # convalidated; False = NOT VALID
    columns: tuple[str, ...]  # constrained columns, in conkey (index) order
    definition: str  # pg_get_constraintdef()
    check_expression: str | None  # bare expression for CHECK rows, else None
    referenced_table: str | None  # resolved schema.name of confrelid for FK rows, else None


@dataclass(frozen=True, slots=True)
class FunctionFacts:
    """Volatility of one function a migration expression references.

    Looked up by name (schema-qualified when the migration wrote it that
    way). An unqualified name matches across all schemas — the migration
    session's search_path is unknowable here — so a decided ``volatility``
    requires every same-named function to agree; a disagreement degrades
    the fact with the reason rather than guessing which overload runs.
    """

    requested: str
    exists: Fact[bool]
    overloads: Fact[int]  # matching pg_proc rows
    volatility: Fact[str]  # provolatile 'i'/'s'/'v'; available only when all overloads agree
    prokind: Fact[str]  # 'f' function, 'p' procedure, 'a' aggregate, 'w' window


@dataclass(frozen=True, slots=True)
class TypeFacts:
    """One type named by the migration (e.g. the type of an added column).

    Answers the domain question the parsed statement cannot: ADD COLUMN
    with a constrained-domain type loses the fast path and rewrites, and
    only the catalog knows whether a type name is a domain.
    """

    requested: str
    exists: Fact[bool]
    formatted: Fact[str]  # format_type(oid, NULL): canonical name
    typtype: Fact[str]  # b base, c composite, d domain, e enum, p pseudo, r range, m multirange
    is_domain: Fact[bool]
    domain_base_type: Fact[str | None]  # None when not a domain
    domain_not_null: Fact[bool]
    domain_constraint_count: Fact[int]  # CHECK constraints on the domain chain


@dataclass(frozen=True, slots=True)
class TypeChangeProbe:
    """Input to ``capture_snapshot``: one ALTER COLUMN TYPE to gather facts for.

    ``new_type`` is the target type exactly as the migration wrote it
    (the IR's deparsed ``column_type``); its typmod is judged client-side by
    :mod:`blastoise.live.typechange`, because ``to_regtype`` discards it.
    """

    relation: QualifiedName | str
    column: str
    new_type: str


@dataclass(frozen=True, slots=True)
class TypeChangeFacts:
    """Facts deciding whether an ALTER COLUMN TYPE rewrites the table.

    ``same_type`` compares raw type OIDs; ``cast_method`` is
    pg_cast.castmethod for (current type reduced through domains → new
    type) — 'b' binary coercion, 'f' cast function, 'i' I/O conversion,
    value ``None`` = no cast row exists at all. The judgment combining
    these with the cited typmod rules is
    :func:`blastoise.live.typechange.assess_type_change`.
    """

    relation: str  # requested relation label
    column: str
    new_type_requested: str  # exactly as the migration wrote it
    current_type: Fact[str]  # format_type with typmod
    current_typmod: Fact[int]  # raw atttypmod; -1 = none
    current_base_type: Fact[str]  # after reducing domains; = current type when not a domain
    current_is_domain: Fact[bool]
    current_domain_not_null: Fact[bool]  # any domain in the chain is NOT NULL
    current_domain_constraint_count: Fact[int]  # CHECK constraints along the domain chain
    new_type: Fact[str]  # format_type(oid, NULL); the typmod lives in new_type_requested
    new_base_type: Fact[str]  # target reduced through domains
    new_is_domain: Fact[bool]
    new_domain_not_null: Fact[bool]  # any domain in the target chain is NOT NULL
    new_domain_constraint_count: Fact[int]  # CHECK constraints along the target chain
    new_domain_has_typmod: Fact[bool]  # a domain in the target chain carries a typmod
    same_type: Fact[bool]  # raw OIDs equal
    bases_same: Fact[bool]  # both sides reduce to the same base type OID
    cast_method: Fact[str | None]  # for (current base → target base); None = no cast row
    cast_context: Fact[str | None]  # 'i' implicit, 'a' assignment, 'e' explicit


@dataclass(frozen=True, slots=True)
class RelationFacts:
    """Everything gathered about one target relation.

    ``requested`` is the name exactly as the migration wrote it; ``schema``
    and ``name`` are the resolution the server performed (search_path
    included). When ``exists`` is false every other fact is marked
    unavailable with that reason rather than omitted, so the shape is
    uniform for hashing.
    """

    requested: str
    exists: Fact[bool]
    schema: Fact[str]
    name: Fact[str]
    relkind: Fact[str]  # r=table, p=partitioned, m=matview, v=view, f=foreign
    is_partitioned: Fact[bool]
    reltuples: Fact[int]  # unavailable when the estimate is unusable (-1)
    relpages: Fact[int]
    relation_size_bytes: Fact[int]
    total_relation_size_bytes: Fact[int]
    last_analyze: Fact[str | None]  # value None = never manually analyzed
    last_autoanalyze: Fact[str | None]  # value None = never autoanalyzed
    n_mod_since_analyze: Fact[int]  # rows modified since last analyze
    partition_count: Fact[int]  # all descendants; 0 for plain tables
    partitions_total_size_bytes: Fact[int]  # sum over descendants; 0 if none
    index_count: Fact[int]
    indexes: Fact[tuple[IndexFacts, ...]]  # sorted by index name
    invalid_indexes: Fact[tuple[str, ...]]  # names with indisvalid = false
    columns: Fact[tuple[ColumnFacts, ...]]  # in attnum order, dropped columns excluded
    dropped_column_count: Fact[int]  # attisdropped columns still occupying tuple slots
    constraints: Fact[tuple[ConstraintFacts, ...]]  # sorted by constraint name


@dataclass(frozen=True, slots=True)
class LockWaiter:
    """One backend currently waiting for a lock on a target relation."""

    relation: str  # resolved schema.name of the target relation
    blocked_pid: int
    blocked_mode: str  # lock mode being requested
    waiting_for_ms: Fact[int]  # from pg_locks.waitstart (PG 14+)
    blocking_pids: tuple[int, ...]  # sorted
    blocking_modes: tuple[str, ...]  # granted modes those pids hold, sorted
    # Whether every backend holding the conflicting lock is idle in a
    # transaction (holding the lock, running nothing). An idle holder is a
    # transient wait a lock_timeout + retry clears; an actively-running
    # holder is sustained contention. Needs pg_read_all_stats to read other
    # roles' state, so it degrades to unavailable when stats are masked.
    blockers_all_idle: Fact[bool] = field(
        default_factory=lambda: Fact.unavailable("not gathered")
    )


@dataclass(frozen=True, slots=True)
class LongTransaction:
    """An open transaction old enough to matter for lock acquisition.

    Any open transaction holding a snapshot or a lock makes an ACCESS
    EXCLUSIVE acquisition queue behind it; ``idle_in_transaction`` marks the
    dangerous kind (holding locks, doing nothing, no query running it
    down). ``first_keyword`` is the only part of the query text captured —
    never the statement itself, which may contain literals.
    """

    pid: int
    state: str  # pg_stat_activity.state verbatim
    idle_in_transaction: bool
    xact_age_ms: int
    query_age_ms: Fact[int]
    first_keyword: Fact[str]  # lowercased first word of the current query
    waiting_on_lock: bool  # wait_event_type = 'Lock'


@dataclass(frozen=True, slots=True)
class ConcurrencyFacts:
    lock_waiters: Fact[tuple[LockWaiter, ...]]
    long_transactions: Fact[tuple[LongTransaction, ...]]
    current_connections: Fact[int]  # client backends only
    max_connections: Fact[int]


@dataclass(frozen=True, slots=True)
class ReplicaFacts:
    """One row of pg_stat_replication.

    Lag interval fields are ``Fact.of(None)`` when the standby is fully
    caught up and idle (Postgres reports NULL then) — that is a known state,
    not a gathering failure.
    """

    name: str  # application_name; may be ''
    client_addr: Fact[str | None]
    state: Fact[str]  # streaming, catchup, ...
    sync_state: Fact[str]  # async, sync, quorum, potential
    replay_lag_bytes: Fact[int]
    write_lag_ms: Fact[int | None]
    flush_lag_ms: Fact[int | None]
    replay_lag_ms: Fact[int | None]


@dataclass(frozen=True, slots=True)
class ReplicationFacts:
    has_replicas: Fact[bool]  # visible to any role; false is a real "no"
    replicas: Fact[tuple[ReplicaFacts, ...]]  # details need pg_read_all_stats
    synchronous: Fact[bool]  # synchronous_standby_names is non-empty
    synchronous_standby_names: Fact[str]
    synchronous_commit: Fact[str]


@dataclass(frozen=True, slots=True)
class CalibrationFacts:
    """The calibration probe's reading on the target (format 5).

    ``compute_ms`` is the minimum of ``repeats`` runs of the compute probe
    (a fixed-size sort of ``compute_rows`` generated rows — backend CPU
    and sort machinery; no table, no lock, no user data, so it runs under
    the minimum-privilege role). The SQL and the anchor reading live in
    :mod:`blastoise.live.calibrate`. The reading is a :class:`Fact`
    because it can fail (a statement timeout on an overloaded server),
    and an unavailable reading degrades the estimate to "unscaled, and
    here is why" rather than to a guess.
    """

    compute_ms: Fact[int]
    repeats: int
    compute_rows: int


def unavailable_calibration(reason: str) -> CalibrationFacts:
    """A calibration block whose every reading is unavailable for ``reason``."""
    from blastoise.live import calibrate as _cb

    return CalibrationFacts(
        compute_ms=Fact.unavailable(reason),
        repeats=_cb.PROBE_REPEATS,
        compute_rows=_cb.COMPUTE_PROBE_ROWS,
    )


@dataclass(frozen=True, slots=True)
class LiveSnapshot:
    """The full introspection result. JSON-serializable and deterministic."""

    snapshot_format: int
    captured_at: str  # client clock, UTC ISO-8601
    target: ConnectionTarget
    limits: CaptureLimits
    role: RoleFacts
    server: ServerFacts
    relations: tuple[RelationFacts, ...]  # sorted by requested name
    functions: tuple[FunctionFacts, ...]  # sorted by requested name
    types: tuple[TypeFacts, ...]  # sorted by requested name
    type_changes: tuple[TypeChangeFacts, ...]  # sorted by (relation, column, new type)
    concurrency: ConcurrencyFacts
    replication: ReplicationFacts
    # Format 5. Defaulted so snapshots built by older callers (and the
    # tests' hand-made snapshots) still construct; an absent probe reads
    # as "not gathered" and the duration model stays unscaled, saying so.
    calibration: CalibrationFacts = field(
        default_factory=lambda: unavailable_calibration("not gathered")
    )

    def to_json_value(self) -> JsonValue:
        return _normalize(dataclasses.asdict(self))

    def to_canonical_json(self) -> str:
        """Stable serialization: sorted keys, compact, ASCII, no floats.

        Two calls on the same snapshot yield identical bytes; this string is
        what the evidence bundle hashes.
        """
        return json.dumps(
            self.to_json_value(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )


def _normalize(value: object) -> JsonValue:
    """Reduce to plain JSON types, rejecting anything nondeterministic.

    Floats are banned outright: sizes are integer bytes, durations integer
    milliseconds, and estimates integers — a float sneaking in would make
    the canonical form platform- and precision-dependent.
    """
    if isinstance(value, float):
        raise TypeError(f"floats are banned from snapshots; got {value!r}")
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, StrEnum):
        return str(value)
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        out: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"non-string dict key {key!r} in snapshot")
            out[key] = _normalize(item)
        return out
    if isinstance(value, list | tuple):
        return [_normalize(item) for item in value]
    raise TypeError(f"unserializable value in snapshot: {value!r} ({type(value).__name__})")
