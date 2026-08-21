"""Read-only live database introspection.

Gathers the production context that turns a catalog lookup into a real
assessment: the facts the lock catalog's ``requires_live_context`` rows and
``duration_model`` fields declare missing — table sizes with their
staleness, invalid indexes, lock waiters, long transactions, replication
lag, and the server version.

Hard rules, enforced here and tested:

* Strictly read-only: the session runs with ``default_transaction_read_only``
  on, inside an explicit ``READ ONLY`` transaction, and refuses to proceed as
  a role that *could* write (superuser, DML privileges on any user relation,
  CREATEROLE/CREATEDB) — not merely one that happens not to.
* Catalogs, statistics, and system views only. No user table is ever read;
  ``reltuples`` with its staleness markers stands in for COUNT(*).
* Query text from ``pg_stat_activity`` is never captured — only the
  statement's first keyword and its duration leave the server.
* Every query is bounded by ``statement_timeout`` and ``lock_timeout``.
* Lock-taking queries run last: everything up to and including the pg_locks
  capture reads catalogs and statistics views only, and the size functions —
  the only calls that open a target relation (ACCESS SHARE) — run strictly
  after it, with this backend's own pid excluded from pg_locks on top, so a
  snapshot can never report a lock conflict the introspection itself
  created.
* Failures degrade per field into :class:`~pgverdict.live.model.Fact`
  ``unavailable`` markers with the reason, never into exceptions and never
  into silent nulls.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pgverdict.ir import QualifiedName
from pgverdict.live.model import (
    SNAPSHOT_FORMAT,
    CaptureLimits,
    ColumnFacts,
    ConcurrencyFacts,
    ConnectionTarget,
    ConstraintFacts,
    Fact,
    FunctionFacts,
    IndexFacts,
    LiveSnapshot,
    LockWaiter,
    LongTransaction,
    RelationFacts,
    ReplicaFacts,
    ReplicationFacts,
    RoleFacts,
    ServerFacts,
    TypeChangeFacts,
    TypeChangeProbe,
    TypeFacts,
)

if TYPE_CHECKING:
    import psycopg
    from psycopg.rows import DictRow


class LiveIntrospectionError(Exception):
    """Connection-level failure: could not connect, or server unsupported."""


class WritableRoleError(LiveIntrospectionError):
    """The connected role can write; introspection refuses to proceed."""


_MIN_SERVER_VERSION_NUM = 100000  # PG 10; the lock catalog's version domain floor

_MASKED_STATS_REASON = (
    "pg_stat_activity rows for other roles are masked; "
    "grant pg_read_all_stats (or pg_monitor) to the introspection role"
)


def _psycopg() -> Any:
    """Import psycopg lazily so the parsing layer works without libpq."""
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - depends on install extras
        raise LiveIntrospectionError(
            "live introspection requires the psycopg driver; "
            "install it with: pip install 'pgverdict[live]'"
        ) from exc
    return psycopg


def redact_conninfo(conninfo: str) -> ConnectionTarget:
    """Reduce a connection string to host/port/dbname/user.

    The password — and every other libpq parameter — is dropped here, before
    any snapshot object exists, so nothing downstream can serialize it.
    """
    psycopg = _psycopg()
    try:
        params = psycopg.conninfo.conninfo_to_dict(conninfo)
    except psycopg.ProgrammingError as exc:
        raise LiveIntrospectionError(f"invalid connection string: {exc}") from exc
    return ConnectionTarget(
        host=params.get("host"),
        port=str(params["port"]) if "port" in params else None,
        dbname=params.get("dbname"),
        user=params.get("user"),
    )


def _regclass_text(relation: QualifiedName | str) -> tuple[str, str]:
    """(requested label, text passed to to_regclass).

    ``QualifiedName`` parts are actual identifiers, so they are quoted for
    the lookup (``to_regclass`` would otherwise down-case them). A plain
    string is passed verbatim: the caller controls its quoting.
    """
    if isinstance(relation, str):
        return relation, relation
    quoted = '"' + relation.name.replace('"', '""') + '"'
    if relation.schema is not None:
        quoted = '"' + relation.schema.replace('"', '""') + '".' + quoted
    return str(relation), quoted


def capture_snapshot(
    conninfo: str,
    relations: Iterable[QualifiedName | str],
    *,
    functions: Iterable[str] = (),
    types: Iterable[str] = (),
    type_changes: Iterable[TypeChangeProbe] = (),
    connect_timeout_s: int = 10,
    statement_timeout_ms: int = 5000,
    lock_timeout_ms: int = 2000,
    long_transaction_threshold_ms: int = 60_000,
    max_listed_transactions: int = 100,
) -> LiveSnapshot:
    """Capture a read-only snapshot of live context for ``relations``.

    ``functions`` are (possibly schema-qualified) function names to look up
    in ``pg_proc`` — the names ``DefaultInfo.unknown_functions`` records for
    a default whose volatility the static allowlists could not decide.
    ``types`` are type names to resolve (the constrained-domain question for
    ADD COLUMN). ``type_changes`` are ALTER COLUMN TYPE probes; the gathered
    facts are judged by :func:`pgverdict.live.typechange.assess_type_change`.

    Raises :class:`WritableRoleError` if the role can write, and
    :class:`LiveIntrospectionError` if the server cannot be reached or is
    older than PG 10. Everything else degrades into per-field
    ``unavailable`` markers inside the returned snapshot.
    """
    psycopg = _psycopg()
    from psycopg.rows import dict_row

    target = redact_conninfo(conninfo)
    limits = CaptureLimits(
        connect_timeout_s=connect_timeout_s,
        statement_timeout_ms=statement_timeout_ms,
        lock_timeout_ms=lock_timeout_ms,
        long_transaction_threshold_ms=long_transaction_threshold_ms,
        max_listed_transactions=max_listed_transactions,
    )
    # statement_timeout/lock_timeout — and TimeZone — are deliberately NOT
    # set here: values in the startup packet become the session's RESET
    # target, which would make pg_settings.reset_val report our own value
    # instead of the server's configured one. (The server's configured
    # TimeZone is itself a captured fact: it decides the
    # timestamp<->timestamptz no-rewrite rule.) They are applied with
    # session-level SET below.
    options = "-c default_transaction_read_only=on"
    try:
        conn = psycopg.Connection.connect(
            conninfo,
            autocommit=False,
            row_factory=dict_row,
            application_name="pgverdict-introspect",
            connect_timeout=connect_timeout_s,
            options=options,
        )
    except psycopg.OperationalError as exc:
        raise LiveIntrospectionError(
            f"could not connect to {target.host or '?'}:{target.port or '5432'}"
            f"/{target.dbname or '?'}: {exc}"
        ) from exc

    captured_at = datetime.now(UTC).isoformat(timespec="seconds")
    try:
        conn.read_only = True
        with conn.transaction():  # the explicit READ ONLY transaction
            # Bound every query. SET takes no bind parameters; the values are
            # ints from our own signature, not user SQL.
            conn.execute(f"SET statement_timeout = {int(statement_timeout_ms)}")
            conn.execute(f"SET lock_timeout = {int(lock_timeout_ms)}")
            conn.execute("SET TimeZone = 'UTC'")  # all serialized timestamps are UTC
            role = _guard_role(conn, limits)
            server = _gather_server(conn, limits)
            if not server.server_version_num.available or server.server_version_num.value is None:
                raise LiveIntrospectionError(
                    "could not determine server version: "
                    f"{server.server_version_num.reason}"
                )
            version_num = server.server_version_num.value
            if version_num < _MIN_SERVER_VERSION_NUM:
                raise LiveIntrospectionError(
                    f"server version {version_num} is older than PG 10; "
                    "the lock catalog does not cover it"
                )
            # Ordering contract (tested): relation resolution, statistics,
            # columns, constraints, functions, and types read catalogs only
            # and take no lock on any target relation. The pg_locks capture
            # runs after them, and the size functions — the only calls that
            # DO open a target relation (ACCESS SHARE) — run strictly after
            # the pg_locks capture, so introspection never observes a
            # waiter or blocking pid it created itself.
            probes = sorted(
                {_regclass_text(rel) for rel in relations}, key=lambda pair: pair[0]
            )
            resolved = [
                _resolve_relation(conn, requested, lookup, limits)
                for requested, lookup in probes
            ]
            oids = {
                res.oid: f"{res.schema}.{res.name}"
                for res in resolved
                if res.oid is not None
            }
            statics = [
                _gather_relation_static(conn, res, limits, version_num)
                for res in resolved
            ]
            function_facts = tuple(
                _gather_function(conn, name, limits, version_num)
                for name in sorted({str(name) for name in functions})
            )
            type_facts = tuple(
                _gather_type(conn, name, limits)
                for name in sorted({str(name) for name in types})
            )
            change_facts = tuple(
                _gather_type_change(conn, probe, limits)
                for probe in _sorted_type_changes(type_changes)
            )
            concurrency = _gather_concurrency(conn, oids, limits, version_num, role)
            relation_facts = [
                _finish_relation(conn, res, static, limits)
                for res, static in zip(resolved, statics, strict=True)
            ]
            replication = _gather_replication(conn, limits, server, role)
    except psycopg.Error as exc:
        # Section gatherers degrade per field; an error surfacing here means
        # the connection itself failed (dropped mid-capture, SET refused, a
        # gate query died). Keep the API contract: our exceptions only.
        raise LiveIntrospectionError(
            f"introspection failed: {_describe_error(exc, limits)}"
        ) from exc
    finally:
        conn.close()

    return LiveSnapshot(
        snapshot_format=SNAPSHOT_FORMAT,
        captured_at=captured_at,
        target=target,
        limits=limits,
        role=role,
        server=server,
        relations=tuple(relation_facts),
        functions=function_facts,
        types=type_facts,
        type_changes=change_facts,
        concurrency=concurrency,
        replication=replication,
    )


# --- plumbing -------------------------------------------------------------


def _query(
    conn: psycopg.Connection[DictRow], sql: str, params: dict[str, Any] | None = None
) -> list[DictRow]:
    """Run one bounded query inside a savepoint.

    The savepoint keeps a failure (timeout, privilege error) from aborting
    the outer READ ONLY transaction, so later sections still run.
    """
    with conn.transaction(), conn.execute(sql, params) as cur:
        return cur.fetchall()


def _describe_error(exc: Exception, limits: CaptureLimits) -> str:
    """A stable, literal-free reason string for an unavailable marker."""
    import psycopg

    if not isinstance(exc, psycopg.Error):
        return f"{type(exc).__name__}: {exc}"
    sqlstate = exc.sqlstate or "?????"
    primary = exc.diag.message_primary if exc.diag else None
    primary = primary or type(exc).__name__
    if sqlstate == "57014":
        return (
            f"introspection query cancelled by statement_timeout "
            f"({limits.statement_timeout_ms} ms)"
        )
    if sqlstate == "55P03":
        return (
            f"lock not acquired within lock_timeout ({limits.lock_timeout_ms} ms); "
            "the relation is exclusively locked by another session"
        )
    if sqlstate == "42501":
        return f"insufficient privilege: {primary}"
    return f"{sqlstate}: {primary}"


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


# --- privilege gate -------------------------------------------------------


def _guard_role(conn: psycopg.Connection[DictRow], limits: CaptureLimits) -> RoleFacts:
    """Verify the session cannot write; fail loudly if the role could.

    Refused outright: superuser, DML privileges (INSERT/UPDATE/DELETE/
    TRUNCATE) on any user relation, CREATEROLE, CREATEDB, and a session that
    is somehow not in a read-only transaction. Recorded as warnings but not
    fatal: CREATE on schemas or the database (the pre-PG15 ``public``
    default), REPLICATION, BYPASSRLS — capabilities a minimal role should
    not have but which cannot modify existing data.
    """
    see_doc = "see docs/minimum-privilege-role.md for the role this tool expects"
    [row] = _query(
        conn,
        """
        SELECT current_user AS role,
               current_setting('transaction_read_only') AS txn_ro,
               current_setting('is_superuser') AS is_super,
               pg_has_role(current_user, 'pg_read_all_stats', 'MEMBER') AS can_stats
        """,
    )
    role = str(row["role"])
    if row["txn_ro"] != "on":
        raise WritableRoleError(
            f"refusing to introspect: the session for role {role!r} is not in a "
            f"READ ONLY transaction (transaction_read_only = {row['txn_ro']!r}); "
            "a server or pooler setting is overriding it"
        )
    if row["is_super"] == "on":
        raise WritableRoleError(
            f"refusing to introspect as superuser role {role!r}: a superuser can "
            f"write anything; {see_doc}"
        )
    [attrs] = _query(
        conn,
        """
        SELECT rolcreatedb, rolcreaterole, rolreplication, rolbypassrls
        FROM pg_roles WHERE rolname = current_user
        """,
    )
    for attr, label in (("rolcreatedb", "CREATEDB"), ("rolcreaterole", "CREATEROLE")):
        if attrs[attr]:
            raise WritableRoleError(
                f"refusing to introspect as role {role!r}: it has {label}, "
                f"which allows creating writable objects or roles; {see_doc}"
            )
    writable = _query(
        conn,
        """
        SELECT c.oid::regclass::text AS rel
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind IN ('r', 'p', 'm', 'f', 'v')
          AND n.nspname NOT IN ('pg_catalog', 'information_schema')
          AND n.nspname NOT LIKE 'pg\\_%'
          AND has_table_privilege(current_user, c.oid,
                                  'INSERT, UPDATE, DELETE, TRUNCATE')
        ORDER BY 1
        LIMIT 5
        """,
    )
    if writable:
        rels = ", ".join(str(r["rel"]) for r in writable)
        raise WritableRoleError(
            f"refusing to introspect as role {role!r}: it holds write privileges "
            f"on user relations (e.g. {rels}); a role that merely happens not to "
            f"write is not read-only — {see_doc}"
        )

    warnings: list[str] = []
    for attr, label in (("rolreplication", "REPLICATION"), ("rolbypassrls", "BYPASSRLS")):
        if attrs[attr]:
            warnings.append(f"role has {label}; the minimal role does not need it")
    [db_create] = _query(
        conn,
        "SELECT has_database_privilege(current_user, current_database(), 'CREATE') AS c",
    )
    if db_create["c"]:
        warnings.append("role can CREATE schemas in this database")
    creatable = _query(
        conn,
        """
        SELECT n.nspname AS schema
        FROM pg_namespace n
        WHERE n.nspname NOT LIKE 'pg\\_%' AND n.nspname <> 'information_schema'
          AND has_schema_privilege(current_user, n.oid, 'CREATE')
        ORDER BY 1
        LIMIT 5
        """,
    )
    if creatable:
        schemas = ", ".join(str(r["schema"]) for r in creatable)
        warnings.append(
            f"role can CREATE objects in schema(s): {schemas} "
            "(the pre-PG15 public-schema default; consider revoking)"
        )
    return RoleFacts(
        role=role,
        transaction_read_only=True,
        superuser=False,
        can_read_all_stats=bool(row["can_stats"]),
        warnings=tuple(warnings),
    )


# --- server ---------------------------------------------------------------


def _gather_server(conn: psycopg.Connection[DictRow], limits: CaptureLimits) -> ServerFacts:
    version_num: Fact[int]
    version: Fact[str]
    pg_major: Fact[int]
    in_recovery: Fact[bool]
    server_now: Fact[str]
    try:
        [row] = _query(
            conn,
            """
            SELECT current_setting('server_version_num')::int AS version_num,
                   current_setting('server_version') AS version,
                   pg_is_in_recovery() AS in_recovery,
                   now() AS server_now
            """,
        )
        version_num = Fact.of(int(row["version_num"]))
        version = Fact.of(str(row["version"]))
        pg_major = Fact.of(int(row["version_num"]) // 10000)
        in_recovery = Fact.of(bool(row["in_recovery"]))
        server_now = Fact.of(str(_iso(row["server_now"])))
    except Exception as exc:
        reason = _describe_error(exc, limits)
        version_num = Fact.unavailable(reason)
        version = Fact.unavailable(reason)
        pg_major = Fact.unavailable(reason)
        in_recovery = Fact.unavailable(reason)
        server_now = Fact.unavailable(reason)

    lock_timeout = _setting_ms(conn, "lock_timeout", limits)
    statement_timeout = _setting_ms(conn, "statement_timeout", limits)
    timezone = _setting_text(conn, "TimeZone", limits)
    return ServerFacts(
        server_version_num=version_num,
        server_version=version,
        pg_major=pg_major,
        in_recovery=in_recovery,
        server_now=server_now,
        lock_timeout_ms=lock_timeout,
        statement_timeout_ms=statement_timeout,
        timezone=timezone,
    )


def _setting_ms(
    conn: psycopg.Connection[DictRow], name: str, limits: CaptureLimits
) -> Fact[int]:
    """The *configured* value of a timeout, not this session's override.

    The introspection session sets its own statement_timeout/lock_timeout,
    so ``current_setting()`` would report our bound, not the server's.
    ``pg_settings.reset_val`` is what the session would reset to — the
    server/database/role-level configuration a migration would run under.
    This works only because our bounds are applied with session-level SET;
    a value in the startup packet would become the RESET target itself.
    """
    try:
        rows = _query(
            conn,
            "SELECT reset_val, unit FROM pg_settings WHERE name = %(name)s",
            {"name": name},
        )
        if not rows:
            return Fact.unavailable(f"no pg_settings row for {name}")
        raw = str(rows[0]["reset_val"])
        unit = rows[0]["unit"]
        if unit not in (None, "ms"):
            return Fact.unavailable(f"{name} reported in unexpected unit {unit!r}: {raw}")
        return Fact.of(int(raw))
    except Exception as exc:
        return Fact.unavailable(_describe_error(exc, limits))


def _setting_text(
    conn: psycopg.Connection[DictRow], name: str, limits: CaptureLimits
) -> Fact[str]:
    """The configured (reset_val) value of a text setting; see _setting_ms."""
    try:
        rows = _query(
            conn,
            "SELECT reset_val FROM pg_settings WHERE name = %(name)s",
            {"name": name},
        )
        if not rows:
            return Fact.unavailable(f"no pg_settings row for {name}")
        return Fact.of(str(rows[0]["reset_val"]))
    except Exception as exc:
        return Fact.unavailable(_describe_error(exc, limits))


# --- relations ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _ResolvedRelation:
    """Phase-one resolution of one requested relation. Takes no lock."""

    requested: str
    lookup: str
    exists: Fact[bool]
    absent_reason: str | None  # why nothing else can be gathered, when set
    oid: int | None = None
    schema: str | None = None
    name: str | None = None
    relkind: str | None = None
    relpages: int | None = None
    reltuples_raw: int | None = None


@dataclass(frozen=True, slots=True)
class _IndexRow:
    """One pg_index row plus the shape facts the verdict layer narrows on."""

    name: str
    valid: bool
    oid: int
    method: str
    partial: bool
    has_expressions: bool
    default_opclasses: bool
    depends_on: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _StaticRelationFacts:
    """Lock-free per-relation facts, gathered before the pg_locks capture."""

    reltuples: Fact[int]
    last_analyze: Fact[str | None]
    last_autoanalyze: Fact[str | None]
    n_mod: Fact[int]
    partition_count: Fact[int]
    index_rows: tuple[_IndexRow, ...] | None
    index_reason: str | None  # set when index_rows is None
    columns: Fact[tuple[ColumnFacts, ...]]
    dropped_column_count: Fact[int]
    constraints: Fact[tuple[ConstraintFacts, ...]]


def _resolve_relation(
    conn: psycopg.Connection[DictRow],
    requested: str,
    lookup: str,
    limits: CaptureLimits,
) -> _ResolvedRelation:
    """Resolve one requested relation via pg_class. Takes no relation lock."""
    try:
        rows = _query(
            conn,
            """
            SELECT c.oid::bigint AS oid, n.nspname AS schema, c.relname AS name,
                   c.relkind::text AS relkind,
                   c.relpages::bigint AS relpages, c.reltuples::bigint AS reltuples
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.oid = to_regclass(%(lookup)s)
            """,
            {"lookup": lookup},
        )
    except Exception as exc:
        reason = f"could not resolve relation: {_describe_error(exc, limits)}"
        return _ResolvedRelation(
            requested=requested,
            lookup=lookup,
            exists=Fact.unavailable(reason),
            absent_reason=reason,
        )
    if not rows:
        return _ResolvedRelation(
            requested=requested,
            lookup=lookup,
            exists=Fact.of(False),
            absent_reason="relation does not exist",
        )
    row = rows[0]
    return _ResolvedRelation(
        requested=requested,
        lookup=lookup,
        exists=Fact.of(True),
        absent_reason=None,
        oid=int(row["oid"]),
        schema=str(row["schema"]),
        name=str(row["name"]),
        relkind=str(row["relkind"]),
        relpages=int(row["relpages"]),
        reltuples_raw=int(row["reltuples"]),
    )


def _gather_relation_static(
    conn: psycopg.Connection[DictRow],
    res: _ResolvedRelation,
    limits: CaptureLimits,
    version_num: int,
) -> _StaticRelationFacts:
    """All lock-free facts for one resolved relation.

    Everything here reads pg_class/pg_attribute/pg_constraint/pg_index/
    pg_inherits/pg_stat_all_tables — no target relation is opened, so this
    runs before the pg_locks capture without polluting it.
    """
    if res.oid is None:
        unavailable: Fact[Any] = Fact.unavailable(str(res.absent_reason))
        return _StaticRelationFacts(
            reltuples=unavailable,
            last_analyze=unavailable,
            last_autoanalyze=unavailable,
            n_mod=unavailable,
            partition_count=unavailable,
            index_rows=None,
            index_reason=str(res.absent_reason),
            columns=unavailable,
            dropped_column_count=unavailable,
            constraints=unavailable,
        )
    oid = res.oid
    reltuples: Fact[int] = (
        Fact.unavailable(
            "reltuples is -1: the table has never been vacuumed or analyzed; "
            "the row count estimate is unusable and this layer will not scan to get one"
        )
        if res.reltuples_raw is not None and res.reltuples_raw < 0
        else Fact.of(int(res.reltuples_raw or 0))
    )

    last_analyze: Fact[str | None]
    last_autoanalyze: Fact[str | None]
    n_mod: Fact[int]
    try:
        stat_rows = _query(
            conn,
            """
            SELECT last_analyze, last_autoanalyze,
                   n_mod_since_analyze::bigint AS n_mod
            FROM pg_stat_all_tables
            WHERE relid = %(oid)s::oid
            """,
            {"oid": oid},
        )
        if stat_rows:
            stat = stat_rows[0]
            last_analyze = Fact.of(_iso(stat["last_analyze"]))
            last_autoanalyze = Fact.of(_iso(stat["last_autoanalyze"]))
            n_mod = Fact.of(int(stat["n_mod"]))
        else:
            reason = f"no statistics row in pg_stat_all_tables (relkind {res.relkind})"
            last_analyze = Fact.unavailable(reason)
            last_autoanalyze = Fact.unavailable(reason)
            n_mod = Fact.unavailable(reason)
    except Exception as exc:
        reason = _describe_error(exc, limits)
        last_analyze = Fact.unavailable(reason)
        last_autoanalyze = Fact.unavailable(reason)
        n_mod = Fact.unavailable(reason)

    partition_count: Fact[int] = Fact.of(0)
    if res.relkind == "p":
        try:
            [pc] = _query(
                conn,
                """
                WITH RECURSIVE parts AS (
                    SELECT i.inhrelid AS relid FROM pg_inherits i
                    WHERE i.inhparent = %(oid)s::oid
                    UNION ALL
                    SELECT i2.inhrelid FROM pg_inherits i2
                    JOIN parts p ON i2.inhparent = p.relid
                )
                SELECT count(*)::bigint AS n FROM parts
                """,
                {"oid": oid},
            )
            partition_count = Fact.of(int(pc["n"]))
        except Exception as exc:
            partition_count = Fact.unavailable(_describe_error(exc, limits))

    index_rows: tuple[_IndexRow, ...] | None
    index_reason: str | None = None
    try:
        # depends_on unions the key columns (indkey) with pg_depend's
        # column-level entries. The union matters twice over: pg_depend
        # alone misses constraint-backed indexes (a PK/UNIQUE index depends
        # on its constraint, not directly on the columns), while indkey
        # alone misses columns referenced only by index expressions or a
        # partial predicate. Together they reproduce the set of columns
        # whose ALTER touches the index. default_opclasses: bool_and over
        # the key columns' opclasses; an empty list coalesces true.
        ix_rows = _query(
            conn,
            """
            SELECT ci.relname AS name, i.indisvalid AS valid,
                   i.indexrelid::bigint AS oid,
                   am.amname AS method,
                   (i.indpred IS NOT NULL) AS partial,
                   (i.indexprs IS NOT NULL) AS has_expressions,
                   (SELECT coalesce(bool_and(op.opcdefault), true)
                    FROM unnest(i.indclass) AS c(opcoid)
                    JOIN pg_opclass op ON op.oid = c.opcoid) AS default_opclasses,
                   ARRAY(
                     SELECT a.attname::text
                     FROM (
                       SELECT k.attnum::int AS attnum
                       FROM unnest(i.indkey) AS k(attnum)
                       WHERE k.attnum > 0
                       UNION
                       SELECT d.refobjsubid AS attnum
                       FROM pg_depend d
                       WHERE d.classid = 'pg_class'::regclass
                         AND d.objid = i.indexrelid
                         AND d.refclassid = 'pg_class'::regclass
                         AND d.refobjid = i.indrelid
                         AND d.refobjsubid > 0
                     ) cols
                     JOIN pg_attribute a
                       ON a.attrelid = i.indrelid AND a.attnum = cols.attnum
                     ORDER BY a.attname::text
                   ) AS depends_on
            FROM pg_index i
            JOIN pg_class ci ON ci.oid = i.indexrelid
            JOIN pg_am am ON am.oid = ci.relam
            WHERE i.indrelid = %(oid)s::oid
            ORDER BY ci.relname
            """,
            {"oid": oid},
        )
        index_rows = tuple(
            _IndexRow(
                name=str(ix["name"]),
                valid=bool(ix["valid"]),
                oid=int(ix["oid"]),
                method=str(ix["method"]),
                partial=bool(ix["partial"]),
                has_expressions=bool(ix["has_expressions"]),
                default_opclasses=bool(ix["default_opclasses"]),
                depends_on=tuple(str(c) for c in ix["depends_on"]),
            )
            for ix in ix_rows
        )
    except Exception as exc:
        index_rows = None
        index_reason = _describe_error(exc, limits)

    columns, dropped = _gather_columns(conn, oid, limits, version_num)
    constraints = _gather_constraints(conn, oid, limits)
    return _StaticRelationFacts(
        reltuples=reltuples,
        last_analyze=last_analyze,
        last_autoanalyze=last_autoanalyze,
        n_mod=n_mod,
        partition_count=partition_count,
        index_rows=index_rows,
        index_reason=index_reason,
        columns=columns,
        dropped_column_count=dropped,
        constraints=constraints,
    )


def _gather_columns(
    conn: psycopg.Connection[DictRow],
    oid: int,
    limits: CaptureLimits,
    version_num: int,
) -> tuple[Fact[tuple[ColumnFacts, ...]], Fact[int]]:
    """Live columns plus the dropped-column count (rewrite cost signal).

    Catalog reads only (pg_attribute/pg_type/pg_attrdef); ``pg_get_expr``
    deparses from the syscache without opening the relation.
    """
    # attgenerated exists from PG 12; before that no column is generated.
    generated_expr = "a.attgenerated::text" if version_num >= 120000 else "''::text"
    columns: Fact[tuple[ColumnFacts, ...]]
    try:
        rows = _query(
            conn,
            f"""
            SELECT a.attname AS name, a.attnum AS attnum,
                   format_type(a.atttypid, a.atttypmod) AS data_type,
                   a.atttypid::bigint AS type_oid, a.atttypmod AS typmod,
                   a.attnotnull AS not_null, a.atthasdef AS has_default,
                   pg_get_expr(d.adbin, d.adrelid) AS default_expression,
                   a.attidentity::text AS identity, {generated_expr} AS generated,
                   (t.typtype = 'd') AS is_domain,
                   CASE WHEN t.typtype = 'd'
                        THEN format_type(t.typbasetype, t.typtypmod)
                   END AS domain_base_type,
                   (t.typtype = 'd' AND t.typnotnull) AS domain_not_null,
                   CASE WHEN t.typtype = 'd'
                        THEN (SELECT count(*)::bigint FROM pg_constraint dc
                              WHERE dc.contypid = t.oid AND dc.contype = 'c')
                        ELSE 0
                   END AS domain_constraint_count
            FROM pg_attribute a
            JOIN pg_type t ON t.oid = a.atttypid
            LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
            WHERE a.attrelid = %(oid)s::oid AND a.attnum > 0 AND NOT a.attisdropped
            ORDER BY a.attnum
            """,
            {"oid": oid},
        )
        columns = Fact.of(
            tuple(
                ColumnFacts(
                    name=str(r["name"]),
                    attnum=int(r["attnum"]),
                    data_type=str(r["data_type"]),
                    type_oid=int(r["type_oid"]),
                    typmod=int(r["typmod"]),
                    not_null=bool(r["not_null"]),
                    has_default=bool(r["has_default"]),
                    default_expression=(
                        str(r["default_expression"])
                        if r["default_expression"] is not None
                        else None
                    ),
                    identity=str(r["identity"]),
                    generated=str(r["generated"]),
                    is_domain=bool(r["is_domain"]),
                    domain_base_type=(
                        str(r["domain_base_type"])
                        if r["domain_base_type"] is not None
                        else None
                    ),
                    domain_not_null=bool(r["domain_not_null"]),
                    domain_constraint_count=int(r["domain_constraint_count"]),
                )
                for r in rows
            )
        )
    except Exception as exc:
        return (
            Fact.unavailable(_describe_error(exc, limits)),
            Fact.unavailable(_describe_error(exc, limits)),
        )

    dropped: Fact[int]
    try:
        [dr] = _query(
            conn,
            """
            SELECT count(*)::bigint AS n FROM pg_attribute
            WHERE attrelid = %(oid)s::oid AND attisdropped
            """,
            {"oid": oid},
        )
        dropped = Fact.of(int(dr["n"]))
    except Exception as exc:
        dropped = Fact.unavailable(_describe_error(exc, limits))
    return columns, dropped


def _gather_constraints(
    conn: psycopg.Connection[DictRow],
    oid: int,
    limits: CaptureLimits,
) -> Fact[tuple[ConstraintFacts, ...]]:
    """Every pg_constraint row on the relation, sorted by name."""
    try:
        rows = _query(
            conn,
            """
            SELECT con.conname AS name, con.contype::text AS contype,
                   con.convalidated AS validated,
                   pg_get_constraintdef(con.oid) AS definition,
                   CASE WHEN con.contype = 'c'
                        THEN pg_get_expr(con.conbin, con.conrelid)
                   END AS check_expression,
                   CASE WHEN con.confrelid <> 0
                        THEN (SELECT n.nspname || '.' || cr.relname
                              FROM pg_class cr
                              JOIN pg_namespace n ON n.oid = cr.relnamespace
                              WHERE cr.oid = con.confrelid)
                   END AS referenced_table,
                   COALESCE(
                       (SELECT array_agg(a.attname ORDER BY k.ord)
                        FROM unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord)
                        JOIN pg_attribute a
                          ON a.attrelid = con.conrelid AND a.attnum = k.attnum),
                       '{}'::name[]) AS columns
            FROM pg_constraint con
            WHERE con.conrelid = %(oid)s::oid
            ORDER BY con.conname
            """,
            {"oid": oid},
        )
    except Exception as exc:
        return Fact.unavailable(_describe_error(exc, limits))
    return Fact.of(
        tuple(
            ConstraintFacts(
                name=str(r["name"]),
                contype=str(r["contype"]),
                validated=bool(r["validated"]),
                columns=tuple(str(col) for col in (r["columns"] or [])),
                definition=str(r["definition"]),
                check_expression=(
                    str(r["check_expression"])
                    if r["check_expression"] is not None
                    else None
                ),
                referenced_table=(
                    str(r["referenced_table"])
                    if r["referenced_table"] is not None
                    else None
                ),
            )
            for r in rows
        )
    )


def _finish_relation(
    conn: psycopg.Connection[DictRow],
    res: _ResolvedRelation,
    static: _StaticRelationFacts,
    limits: CaptureLimits,
) -> RelationFacts:
    """Size facts — the only lock-takers — and final assembly.

    Runs strictly after the pg_locks capture: ``pg_relation_size`` opens the
    relation (ACCESS SHARE) and queues behind an ACCESS EXCLUSIVE holder, so
    these calls sit in their own savepoints, degrade to ``unavailable`` on
    ``lock_timeout``, and must never precede the concurrency section.
    """
    if res.oid is None:
        unavailable: Fact[Any] = Fact.unavailable(str(res.absent_reason))
        return RelationFacts(
            requested=res.requested,
            exists=res.exists,
            schema=unavailable,
            name=unavailable,
            relkind=unavailable,
            is_partitioned=unavailable,
            reltuples=unavailable,
            relpages=unavailable,
            relation_size_bytes=unavailable,
            total_relation_size_bytes=unavailable,
            last_analyze=unavailable,
            last_autoanalyze=unavailable,
            n_mod_since_analyze=unavailable,
            partition_count=unavailable,
            partitions_total_size_bytes=unavailable,
            index_count=unavailable,
            indexes=unavailable,
            invalid_indexes=unavailable,
            columns=unavailable,
            dropped_column_count=unavailable,
            constraints=unavailable,
        )
    oid = res.oid
    is_partitioned = res.relkind == "p"

    rel_size: Fact[int]
    total_size: Fact[int]
    try:
        [sizes] = _query(
            conn,
            """
            SELECT pg_relation_size(%(oid)s::oid)::bigint AS rel,
                   pg_total_relation_size(%(oid)s::oid)::bigint AS total
            """,
            {"oid": oid},
        )
        rel_size = Fact.of(int(sizes["rel"]))
        total_size = Fact.of(int(sizes["total"]))
    except Exception as exc:
        reason = _describe_error(exc, limits)
        rel_size = Fact.unavailable(reason)
        total_size = Fact.unavailable(reason)

    partitions_total: Fact[int] = Fact.of(0)
    if is_partitioned:
        try:
            [ps] = _query(
                conn,
                """
                WITH RECURSIVE parts AS (
                    SELECT i.inhrelid AS relid FROM pg_inherits i
                    WHERE i.inhparent = %(oid)s::oid
                    UNION ALL
                    SELECT i2.inhrelid FROM pg_inherits i2
                    JOIN parts p ON i2.inhparent = p.relid
                )
                SELECT COALESCE(sum(pg_total_relation_size(relid)), 0)::bigint AS total
                FROM parts
                """,
                {"oid": oid},
            )
            partitions_total = Fact.of(int(ps["total"]))
        except Exception as exc:
            partitions_total = Fact.unavailable(
                f"could not size partitions: {_describe_error(exc, limits)}"
            )

    index_count: Fact[int]
    indexes: Fact[tuple[IndexFacts, ...]]
    invalid: Fact[tuple[str, ...]]
    if static.index_rows is None:
        reason = str(static.index_reason)
        index_count = Fact.unavailable(reason)
        indexes = Fact.unavailable(reason)
        invalid = Fact.unavailable(reason)
    else:
        gathered: list[IndexFacts] = []
        for row in static.index_rows:
            size: Fact[int]
            try:
                [sz] = _query(
                    conn,
                    "SELECT pg_relation_size(%(oid)s::oid)::bigint AS size",
                    {"oid": row.oid},
                )
                size = Fact.of(int(sz["size"]))
            except Exception as exc:
                size = Fact.unavailable(_describe_error(exc, limits))
            gathered.append(
                IndexFacts(
                    name=row.name,
                    valid=row.valid,
                    size_bytes=size,
                    method=row.method,
                    partial=row.partial,
                    has_expressions=row.has_expressions,
                    default_opclasses=row.default_opclasses,
                    depends_on_columns=row.depends_on,
                )
            )
        index_count = Fact.of(len(gathered))
        indexes = Fact.of(tuple(gathered))
        invalid = Fact.of(tuple(ix.name for ix in gathered if not ix.valid))

    return RelationFacts(
        requested=res.requested,
        exists=res.exists,
        schema=Fact.of(str(res.schema)),
        name=Fact.of(str(res.name)),
        relkind=Fact.of(str(res.relkind)),
        is_partitioned=Fact.of(is_partitioned),
        reltuples=static.reltuples,
        relpages=Fact.of(int(res.relpages or 0)),
        relation_size_bytes=rel_size,
        total_relation_size_bytes=total_size,
        last_analyze=static.last_analyze,
        last_autoanalyze=static.last_autoanalyze,
        n_mod_since_analyze=static.n_mod,
        partition_count=static.partition_count,
        partitions_total_size_bytes=partitions_total,
        index_count=index_count,
        indexes=indexes,
        invalid_indexes=invalid,
        columns=static.columns,
        dropped_column_count=static.dropped_column_count,
        constraints=static.constraints,
    )


# --- functions, types, and type changes -----------------------------------


def _split_function_name(requested: str) -> tuple[str | None, str]:
    """(schema, name) from a dotted key; unqualified names get schema None.

    Keys come from ``unknown_function_keys`` — raw identifier parts joined
    with dots — so a two-part name is schema.function and a three-part name
    is database.schema.function (the database part is dropped: we are
    already connected to it).
    """
    parts = requested.split(".")
    if len(parts) == 1:
        return None, parts[0]
    return parts[-2], parts[-1]


def _gather_function(
    conn: psycopg.Connection[DictRow],
    requested: str,
    limits: CaptureLimits,
    version_num: int,
) -> FunctionFacts:
    """pg_proc facts for one function name.

    An unqualified name matches every schema: the migration session's
    search_path is unknowable here, so the volatility is decided only when
    all same-named functions agree.
    """
    schema, name = _split_function_name(requested)
    # prokind exists from PG 11; before that aggregate/window are flags.
    prokind_expr = (
        "p.prokind::text"
        if version_num >= 110000
        else "CASE WHEN p.proisagg THEN 'a' WHEN p.proiswindow THEN 'w' ELSE 'f' END"
    )
    try:
        rows = _query(
            conn,
            f"""
            SELECT n.nspname AS schema, p.provolatile::text AS provolatile,
                   {prokind_expr} AS prokind
            FROM pg_proc p
            JOIN pg_namespace n ON n.oid = p.pronamespace
            WHERE p.proname = %(name)s
              AND (%(schema)s::text IS NULL OR n.nspname = %(schema)s::text)
            ORDER BY n.nspname, p.provolatile
            """,
            {"name": name, "schema": schema},
        )
    except Exception as exc:
        reason = _describe_error(exc, limits)
        return FunctionFacts(
            requested=requested,
            exists=Fact.unavailable(reason),
            overloads=Fact.unavailable(reason),
            volatility=Fact.unavailable(reason),
            prokind=Fact.unavailable(reason),
        )
    if not rows:
        reason = f"no function named {requested!r} in any schema"
        return FunctionFacts(
            requested=requested,
            exists=Fact.of(False),
            overloads=Fact.of(0),
            volatility=Fact.unavailable(reason),
            prokind=Fact.unavailable(reason),
        )
    volatilities = sorted({str(r["provolatile"]) for r in rows})
    prokinds = sorted({str(r["prokind"]) for r in rows})
    schemas = sorted({str(r["schema"]) for r in rows})
    volatility: Fact[str] = (
        Fact.of(volatilities[0])
        if len(volatilities) == 1
        else Fact.unavailable(
            f"{len(rows)} overloads (schemas: {', '.join(schemas)}) disagree on "
            f"volatility: {', '.join(volatilities)}"
        )
    )
    prokind: Fact[str] = (
        Fact.of(prokinds[0])
        if len(prokinds) == 1
        else Fact.unavailable(
            f"{len(rows)} overloads disagree on kind: {', '.join(prokinds)}"
        )
    )
    return FunctionFacts(
        requested=requested,
        exists=Fact.of(True),
        overloads=Fact.of(len(rows)),
        volatility=volatility,
        prokind=prokind,
    )


def _gather_type(
    conn: psycopg.Connection[DictRow],
    requested: str,
    limits: CaptureLimits,
) -> TypeFacts:
    """pg_type facts for one type name, domain chain included.

    ``to_regtype`` accepts the full type-name grammar (typmods are parsed
    and discarded). On PG 16+ an invalid name resolves to NULL; older
    servers raise, which the savepoint turns into an unavailable marker.
    """
    try:
        rows = _query(
            conn,
            """
            WITH RECURSIVE chain(oid, depth) AS (
                SELECT t.oid, 0 FROM pg_type t WHERE t.oid = to_regtype(%(name)s)
                UNION ALL
                SELECT t2.typbasetype, c.depth + 1
                FROM chain c
                JOIN pg_type t2 ON t2.oid = c.oid
                WHERE t2.typtype = 'd' AND t2.typbasetype <> 0
            )
            SELECT format_type(t.oid, NULL) AS formatted, t.typtype::text AS typtype,
                   (t.typtype = 'd') AS is_domain,
                   CASE WHEN t.typtype = 'd'
                        THEN format_type(t.typbasetype, t.typtypmod)
                   END AS domain_base_type,
                   (SELECT bool_or(tt.typnotnull) FROM pg_type tt
                    WHERE tt.oid IN (SELECT oid FROM chain)) AS domain_not_null,
                   (SELECT count(*)::bigint FROM pg_constraint dc
                    WHERE dc.contype = 'c'
                      AND dc.contypid IN (SELECT oid FROM chain))
                       AS domain_constraint_count
            FROM pg_type t
            WHERE t.oid = to_regtype(%(name)s)
            """,
            {"name": requested},
        )
    except Exception as exc:
        reason = _describe_error(exc, limits)
        unavailable: Fact[Any] = Fact.unavailable(reason)
        return TypeFacts(
            requested=requested,
            exists=Fact.unavailable(reason),
            formatted=unavailable,
            typtype=unavailable,
            is_domain=unavailable,
            domain_base_type=unavailable,
            domain_not_null=unavailable,
            domain_constraint_count=unavailable,
        )
    if not rows:
        reason = f"type {requested!r} does not exist"
        unavailable = Fact.unavailable(reason)
        return TypeFacts(
            requested=requested,
            exists=Fact.of(False),
            formatted=unavailable,
            typtype=unavailable,
            is_domain=unavailable,
            domain_base_type=unavailable,
            domain_not_null=unavailable,
            domain_constraint_count=unavailable,
        )
    row = rows[0]
    return TypeFacts(
        requested=requested,
        exists=Fact.of(True),
        formatted=Fact.of(str(row["formatted"])),
        typtype=Fact.of(str(row["typtype"])),
        is_domain=Fact.of(bool(row["is_domain"])),
        domain_base_type=Fact.of(
            str(row["domain_base_type"]) if row["domain_base_type"] is not None else None
        ),
        domain_not_null=Fact.of(bool(row["domain_not_null"])),
        domain_constraint_count=Fact.of(int(row["domain_constraint_count"])),
    )


def _sorted_type_changes(
    type_changes: Iterable[TypeChangeProbe],
) -> list[TypeChangeProbe]:
    """Deduplicate and canonically order the probes for stable serialization."""
    seen: dict[tuple[str, str, str], TypeChangeProbe] = {}
    for probe in type_changes:
        key = (str(probe.relation), probe.column, probe.new_type)
        seen.setdefault(key, probe)
    return [seen[key] for key in sorted(seen)]


def _unavailable_type_change(
    requested_rel: str, probe: TypeChangeProbe, reason: str
) -> TypeChangeFacts:
    unavailable: Fact[Any] = Fact.unavailable(reason)
    return TypeChangeFacts(
        relation=requested_rel,
        column=probe.column,
        new_type_requested=probe.new_type,
        current_type=unavailable,
        current_typmod=unavailable,
        current_base_type=unavailable,
        current_is_domain=unavailable,
        current_domain_not_null=unavailable,
        current_domain_constraint_count=unavailable,
        new_type=unavailable,
        new_base_type=unavailable,
        new_is_domain=unavailable,
        new_domain_not_null=unavailable,
        new_domain_constraint_count=unavailable,
        new_domain_has_typmod=unavailable,
        same_type=unavailable,
        bases_same=unavailable,
        cast_method=unavailable,
        cast_context=unavailable,
    )


def _gather_type_change(
    conn: psycopg.Connection[DictRow],
    probe: TypeChangeProbe,
    limits: CaptureLimits,
) -> TypeChangeFacts:
    """The from→to facts for one ALTER COLUMN TYPE.

    The current type comes from pg_attribute, reduced through domains to its
    base; the cast row for (base → target) supplies castmethod. Catalog
    reads only — no relation lock.
    """
    requested_rel, lookup = _regclass_text(probe.relation)
    try:
        rows = _query(
            conn,
            """
            WITH RECURSIVE
            cur AS (
                SELECT a.atttypid AS oid, a.atttypmod AS typmod
                FROM pg_attribute a
                WHERE a.attrelid = to_regclass(%(rel)s)
                  AND a.attname = %(col)s AND a.attnum > 0 AND NOT a.attisdropped
            ),
            tgt AS (SELECT to_regtype(%(new_type)s)::oid AS oid),
            chain(oid, depth) AS (
                SELECT cur.oid, 0 FROM cur
                UNION ALL
                SELECT t.typbasetype, c.depth + 1
                FROM chain c
                JOIN pg_type t ON t.oid = c.oid
                WHERE t.typtype = 'd' AND t.typbasetype <> 0
            ),
            base AS (SELECT oid FROM chain ORDER BY depth DESC LIMIT 1),
            tchain(oid, depth) AS (
                SELECT tgt.oid, 0 FROM tgt WHERE tgt.oid IS NOT NULL
                UNION ALL
                SELECT t.typbasetype, c.depth + 1
                FROM tchain c
                JOIN pg_type t ON t.oid = c.oid
                WHERE t.typtype = 'd' AND t.typbasetype <> 0
            ),
            tbase AS (SELECT oid FROM tchain ORDER BY depth DESC LIMIT 1)
            SELECT format_type(cur.oid, cur.typmod) AS current_type,
                   cur.typmod AS current_typmod,
                   format_type(base.oid, NULL) AS current_base_type,
                   (SELECT t.typtype = 'd' FROM pg_type t WHERE t.oid = cur.oid)
                       AS current_is_domain,
                   (SELECT bool_or(t.typnotnull) FROM pg_type t
                    WHERE t.oid IN (SELECT oid FROM chain)) AS current_domain_not_null,
                   (SELECT count(*)::bigint FROM pg_constraint dc
                    WHERE dc.contype = 'c'
                      AND dc.contypid IN (SELECT oid FROM chain))
                       AS current_domain_constraint_count,
                   CASE WHEN tgt.oid IS NOT NULL
                        THEN format_type(tgt.oid, NULL)
                   END AS new_type,
                   (SELECT format_type(tb.oid, NULL) FROM tbase tb) AS new_base_type,
                   (SELECT t.typtype = 'd' FROM pg_type t WHERE t.oid = tgt.oid)
                       AS new_is_domain,
                   COALESCE((SELECT bool_or(t.typnotnull) FROM pg_type t
                             WHERE t.oid IN (SELECT oid FROM tchain)), false)
                       AS new_domain_not_null,
                   (SELECT count(*)::bigint FROM pg_constraint dc
                    WHERE dc.contype = 'c'
                      AND dc.contypid IN (SELECT oid FROM tchain))
                       AS new_domain_constraint_count,
                   COALESCE((SELECT bool_or(t.typtypmod <> -1) FROM pg_type t
                             WHERE t.oid IN (SELECT oid FROM tchain)
                               AND t.typtype = 'd'), false)
                       AS new_domain_has_typmod,
                   (cur.oid = tgt.oid) AS same_type,
                   (SELECT base.oid = tb.oid FROM tbase tb) AS bases_same,
                   c2.castmethod::text AS cast_method,
                   c2.castcontext::text AS cast_context
            FROM cur
            CROSS JOIN tgt
            CROSS JOIN base
            LEFT JOIN tbase ON true
            LEFT JOIN pg_cast c2
              ON c2.castsource = base.oid AND c2.casttarget = tbase.oid
            """,
            {"rel": lookup, "col": probe.column, "new_type": probe.new_type},
        )
    except Exception as exc:
        return _unavailable_type_change(
            requested_rel, probe, _describe_error(exc, limits)
        )
    if not rows:
        return _unavailable_type_change(
            requested_rel,
            probe,
            f"relation or column not found: {requested_rel}.{probe.column}",
        )
    row = rows[0]
    target_missing = row["new_type"] is None
    target_reason = f"type {probe.new_type!r} does not exist"

    def _target_fact(value: Any) -> Fact[Any]:
        return Fact.unavailable(target_reason) if target_missing else Fact.of(value)

    return TypeChangeFacts(
        relation=requested_rel,
        column=probe.column,
        new_type_requested=probe.new_type,
        current_type=Fact.of(str(row["current_type"])),
        current_typmod=Fact.of(int(row["current_typmod"])),
        current_base_type=Fact.of(str(row["current_base_type"])),
        current_is_domain=Fact.of(bool(row["current_is_domain"])),
        current_domain_not_null=Fact.of(bool(row["current_domain_not_null"])),
        current_domain_constraint_count=Fact.of(
            int(row["current_domain_constraint_count"])
        ),
        new_type=_target_fact(None if target_missing else str(row["new_type"])),
        new_base_type=_target_fact(
            None if target_missing else str(row["new_base_type"])
        ),
        new_is_domain=_target_fact(bool(row["new_is_domain"])),
        new_domain_not_null=_target_fact(bool(row["new_domain_not_null"])),
        new_domain_constraint_count=_target_fact(
            int(row["new_domain_constraint_count"] or 0)
        ),
        new_domain_has_typmod=_target_fact(bool(row["new_domain_has_typmod"])),
        same_type=_target_fact(bool(row["same_type"])),
        bases_same=_target_fact(bool(row["bases_same"])),
        cast_method=_target_fact(
            str(row["cast_method"]) if row["cast_method"] is not None else None
        ),
        cast_context=_target_fact(
            str(row["cast_context"]) if row["cast_context"] is not None else None
        ),
    )


# --- concurrency ----------------------------------------------------------


def _gather_concurrency(
    conn: psycopg.Connection[DictRow],
    oids: dict[int, str],
    limits: CaptureLimits,
    version_num: int,
    role: RoleFacts,
) -> ConcurrencyFacts:
    own_pid: int | None
    try:
        [bp] = _query(conn, "SELECT pg_backend_pid() AS pid")
        own_pid = int(bp["pid"])
    except Exception:
        own_pid = None
    waiters = _gather_waiters(conn, oids, limits, version_num, own_pid)
    long_txns = _gather_long_transactions(conn, limits, role)

    current: Fact[int]
    if role.can_read_all_stats:
        try:
            [cc] = _query(
                conn,
                """
                SELECT count(*)::bigint AS n FROM pg_stat_activity
                WHERE backend_type = 'client backend'
                """,
            )
            current = Fact.of(int(cc["n"]))
        except Exception as exc:
            current = Fact.unavailable(_describe_error(exc, limits))
    else:
        current = Fact.unavailable(_MASKED_STATS_REASON)
    max_conn = _max_connections(conn, limits)
    return ConcurrencyFacts(
        lock_waiters=waiters,
        long_transactions=long_txns,
        current_connections=current,
        max_connections=max_conn,
    )


def _max_connections(conn: psycopg.Connection[DictRow], limits: CaptureLimits) -> Fact[int]:
    try:
        [row] = _query(conn, "SELECT current_setting('max_connections')::int AS n")
        return Fact.of(int(row["n"]))
    except Exception as exc:
        return Fact.unavailable(_describe_error(exc, limits))


def _gather_waiters(
    conn: psycopg.Connection[DictRow],
    oids: dict[int, str],
    limits: CaptureLimits,
    version_num: int,
    own_pid: int | None,
) -> Fact[tuple[LockWaiter, ...]]:
    """Lock waiters on the target relations, from pg_locks.

    pg_locks is not privilege-masked, so this works for any role. Wait
    durations come from pg_locks.waitstart, which exists from PG 14.

    This backend's own pid is excluded on both sides (and stripped from
    ``pg_blocking_pids`` output): the snapshot must never report a conflict
    the introspection created. By the ordering contract nothing lock-taking
    has run yet, so the exclusion is defense in depth.
    """
    if not oids:
        return Fact.of(())
    waitstart_expr = (
        "(EXTRACT(EPOCH FROM (now() - l.waitstart)) * 1000)::bigint"
        if version_num >= 140000
        else "NULL::bigint"
    )
    oid_list = sorted(oids)
    try:
        blocked = _query(
            conn,
            f"""
            SELECT l.pid AS blocked_pid, l.mode AS blocked_mode,
                   l.relation::bigint AS reloid,
                   {waitstart_expr} AS waiting_for_ms,
                   pg_blocking_pids(l.pid) AS blocking_pids
            FROM pg_locks l
            WHERE NOT l.granted AND l.locktype = 'relation'
              AND l.relation::bigint = ANY(%(oids)s)
              AND l.pid <> pg_backend_pid()
            ORDER BY l.pid
            """,
            {"oids": oid_list},
        )
        holders = _query(
            conn,
            """
            SELECT l.pid AS pid, l.mode AS mode, l.relation::bigint AS reloid
            FROM pg_locks l
            WHERE l.granted AND l.locktype = 'relation'
              AND l.relation::bigint = ANY(%(oids)s)
              AND l.pid <> pg_backend_pid()
            """,
            {"oids": oid_list},
        )
    except Exception as exc:
        return Fact.unavailable(_describe_error(exc, limits))

    held: dict[tuple[int, int], set[str]] = {}
    for h in holders:
        held.setdefault((int(h["reloid"]), int(h["pid"])), set()).add(str(h["mode"]))
    waiters: list[LockWaiter] = []
    for w in blocked:
        reloid = int(w["reloid"])
        blocking_pids = tuple(
            sorted(
                int(p)
                for p in (w["blocking_pids"] or [])
                if own_pid is None or int(p) != own_pid
            )
        )
        modes: set[str] = set()
        for pid in blocking_pids:
            modes |= held.get((reloid, pid), set())
        waiting: Fact[int]
        if version_num < 140000:
            waiting = Fact.unavailable("pg_locks.waitstart requires PG 14+")
        elif w["waiting_for_ms"] is None:
            waiting = Fact.unavailable("pg_locks.waitstart was null")
        else:
            waiting = Fact.of(int(w["waiting_for_ms"]))
        waiters.append(
            LockWaiter(
                relation=oids[reloid],
                blocked_pid=int(w["blocked_pid"]),
                blocked_mode=str(w["blocked_mode"]),
                waiting_for_ms=waiting,
                blocking_pids=blocking_pids,
                blocking_modes=tuple(sorted(modes)),
            )
        )
    return Fact.of(tuple(waiters))


def _gather_long_transactions(
    conn: psycopg.Connection[DictRow],
    limits: CaptureLimits,
    role: RoleFacts,
) -> Fact[tuple[LongTransaction, ...]]:
    """Open transactions old enough to block a lock acquisition.

    Restricted to the current database (a transaction elsewhere cannot hold
    locks here). Requires pg_read_all_stats: without it pg_stat_activity
    masks other roles' xact_start, and a listing that silently omitted them
    would look like "no long transactions" — so it degrades explicitly.
    """
    if not role.can_read_all_stats:
        return Fact.unavailable(_MASKED_STATS_REASON)
    try:
        rows = _query(
            conn,
            """
            SELECT a.pid AS pid, a.state AS state,
                   (EXTRACT(EPOCH FROM (now() - a.xact_start)) * 1000)::bigint
                       AS xact_age_ms,
                   CASE WHEN a.query_start IS NULL THEN NULL
                        ELSE (EXTRACT(EPOCH FROM (now() - a.query_start)) * 1000)::bigint
                   END AS query_age_ms,
                   lower(substring(ltrim(a.query) FROM '^[[:alpha:]]+'))
                       AS first_keyword,
                   (a.query IS NULL OR a.query = '') AS no_query,
                   COALESCE(a.query ~ '^<', false) AS masked_query,
                   COALESCE(a.wait_event_type = 'Lock', false) AS waiting_on_lock
            FROM pg_stat_activity a
            WHERE a.pid <> pg_backend_pid()
              AND a.datname = current_database()
              AND a.xact_start IS NOT NULL
              AND (now() - a.xact_start)
                  >= make_interval(secs => %(threshold_ms)s::bigint / 1000.0)
            ORDER BY a.xact_start ASC, a.pid ASC
            LIMIT %(limit)s
            """,
            {
                "threshold_ms": limits.long_transaction_threshold_ms,
                "limit": limits.max_listed_transactions,
            },
        )
    except Exception as exc:
        return Fact.unavailable(_describe_error(exc, limits))
    out: list[LongTransaction] = []
    for r in rows:
        state = str(r["state"]) if r["state"] is not None else "unknown"
        keyword: Fact[str]
        if r["masked_query"]:
            keyword = Fact.unavailable(
                "query text is masked for this role (pg_stat_activity placeholder)"
            )
        elif r["no_query"] or r["first_keyword"] is None:
            keyword = Fact.unavailable("no query text (empty or track_activities off)")
        else:
            keyword = Fact.of(str(r["first_keyword"]))
        query_age: Fact[int] = (
            Fact.of(int(r["query_age_ms"]))
            if r["query_age_ms"] is not None
            else Fact.unavailable("query_start is null")
        )
        out.append(
            LongTransaction(
                pid=int(r["pid"]),
                state=state,
                idle_in_transaction=state.startswith("idle in transaction"),
                xact_age_ms=int(r["xact_age_ms"]),
                query_age_ms=query_age,
                first_keyword=keyword,
                waiting_on_lock=bool(r["waiting_on_lock"]),
            )
        )
    return Fact.of(tuple(out))


# --- replication ----------------------------------------------------------


def _gather_replication(
    conn: psycopg.Connection[DictRow],
    limits: CaptureLimits,
    server: ServerFacts,
    role: RoleFacts,
) -> ReplicationFacts:
    has_replicas: Fact[bool]
    replica_count = 0
    try:
        [count_row] = _query(conn, "SELECT count(*)::bigint AS n FROM pg_stat_replication")
        replica_count = int(count_row["n"])
        has_replicas = Fact.of(replica_count > 0)
    except Exception as exc:
        has_replicas = Fact.unavailable(_describe_error(exc, limits))

    sync_names: Fact[str]
    synchronous: Fact[bool]
    sync_commit: Fact[str]
    try:
        [s] = _query(
            conn,
            """
            SELECT current_setting('synchronous_standby_names') AS names,
                   current_setting('synchronous_commit') AS commit
            """,
        )
        names = str(s["names"])
        sync_names = Fact.of(names)
        synchronous = Fact.of(names.strip() != "")
        sync_commit = Fact.of(str(s["commit"]))
    except Exception as exc:
        reason = _describe_error(exc, limits)
        sync_names = Fact.unavailable(reason)
        synchronous = Fact.unavailable(reason)
        sync_commit = Fact.unavailable(reason)

    replicas: Fact[tuple[ReplicaFacts, ...]]
    if not has_replicas.available:
        replicas = Fact.unavailable(str(has_replicas.reason))
    elif replica_count == 0:
        replicas = Fact.of(())
    elif not role.can_read_all_stats:
        replicas = Fact.unavailable(
            "pg_stat_replication detail columns are masked; "
            "grant pg_read_all_stats (or pg_monitor) to the introspection role"
        )
    else:
        replicas = _gather_replica_details(conn, limits, server)
    return ReplicationFacts(
        has_replicas=has_replicas,
        replicas=replicas,
        synchronous=synchronous,
        synchronous_standby_names=sync_names,
        synchronous_commit=sync_commit,
    )


def _gather_replica_details(
    conn: psycopg.Connection[DictRow],
    limits: CaptureLimits,
    server: ServerFacts,
) -> Fact[tuple[ReplicaFacts, ...]]:
    in_recovery = bool(server.in_recovery.value) if server.in_recovery.available else False
    lag_bytes_expr = (
        "NULL::bigint"
        if in_recovery
        else (
            "CASE WHEN r.replay_lsn IS NULL THEN NULL "
            "ELSE pg_wal_lsn_diff(pg_current_wal_lsn(), r.replay_lsn)::bigint END"
        )
    )
    try:
        rows = _query(
            conn,
            f"""
            SELECT COALESCE(r.application_name, '') AS name,
                   host(r.client_addr) AS client_addr,
                   r.state AS state, r.sync_state AS sync_state,
                   {lag_bytes_expr} AS replay_lag_bytes,
                   (EXTRACT(EPOCH FROM r.write_lag) * 1000)::bigint AS write_lag_ms,
                   (EXTRACT(EPOCH FROM r.flush_lag) * 1000)::bigint AS flush_lag_ms,
                   (EXTRACT(EPOCH FROM r.replay_lag) * 1000)::bigint AS replay_lag_ms
            FROM pg_stat_replication r
            ORDER BY name, client_addr, r.pid
            """,
        )
    except Exception as exc:
        return Fact.unavailable(_describe_error(exc, limits))
    out: list[ReplicaFacts] = []
    for r in rows:
        lag_bytes: Fact[int]
        if in_recovery:
            lag_bytes = Fact.unavailable(
                "server is in recovery; pg_current_wal_lsn() is not applicable"
            )
        elif r["replay_lag_bytes"] is None:
            lag_bytes = Fact.unavailable("replay_lsn not yet reported by this standby")
        else:
            lag_bytes = Fact.of(int(r["replay_lag_bytes"]))

        def _lag(value: Any) -> Fact[int | None]:
            # NULL lag means "caught up / no recent measurement" — a known state.
            return Fact.of(int(value)) if value is not None else Fact.of(None)

        out.append(
            ReplicaFacts(
                name=str(r["name"]),
                client_addr=Fact.of(
                    str(r["client_addr"]) if r["client_addr"] is not None else None
                ),
                state=Fact.of(str(r["state"])) if r["state"] is not None else (
                    Fact.unavailable("state column was null")
                ),
                sync_state=Fact.of(str(r["sync_state"])) if r["sync_state"] is not None else (
                    Fact.unavailable("sync_state column was null")
                ),
                replay_lag_bytes=lag_bytes,
                write_lag_ms=_lag(r["write_lag_ms"]),
                flush_lag_ms=_lag(r["flush_lag_ms"]),
                replay_lag_ms=_lag(r["replay_lag_ms"]),
            )
        )
    return Fact.of(tuple(out))
