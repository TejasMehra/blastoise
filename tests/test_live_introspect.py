"""Integration tests for the live introspection layer.

These run against a disposable Postgres server (see ``live_harness.py``) and
deliberately cover the degraded paths: a role without pg_monitor, a server
with no replicas, a never-analyzed table, a missing relation, and a staged
live lock conflict. Each must produce a marked-unavailable field, never an
exception.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator

import psycopg
import pytest
from live_harness import unique_name

from pgverdict.ir import QualifiedName
from pgverdict.live import (
    LiveSnapshot,
    RelationFacts,
    WritableRoleError,
    capture_snapshot,
)


def relation(snapshot: LiveSnapshot, requested: str) -> RelationFacts:
    [facts] = [r for r in snapshot.relations if r.requested == requested]
    return facts


@pytest.fixture
def admin(admin_dsn: str) -> Iterator[psycopg.Connection]:
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        yield conn


class TestPrivilegeGate:
    def test_superuser_is_rejected(self, admin_dsn: str) -> None:
        with pytest.raises(WritableRoleError, match="superuser"):
            capture_snapshot(admin_dsn, ["public.anything"])

    def test_role_with_dml_privilege_is_rejected(
        self, admin: psycopg.Connection, admin_dsn: str
    ) -> None:
        table = unique_name("t_writable")
        role = unique_name("r_writer")
        admin.execute(f"CREATE TABLE public.{table} (id int)")
        admin.execute(f"CREATE ROLE {role} LOGIN PASSWORD 'pgverdict-test'")
        admin.execute(f"GRANT INSERT ON public.{table} TO {role}")
        try:
            dsn = psycopg.conninfo.make_conninfo(
                admin_dsn, user=role, password="pgverdict-test"
            )
            with pytest.raises(WritableRoleError, match=table):
                capture_snapshot(dsn, [f"public.{table}"])
        finally:
            admin.execute(f"DROP TABLE public.{table}")
            admin.execute(f"DROP ROLE {role}")

    def test_createdb_role_is_rejected(
        self, admin: psycopg.Connection, admin_dsn: str
    ) -> None:
        role = unique_name("r_createdb")
        admin.execute(f"CREATE ROLE {role} LOGIN PASSWORD 'pgverdict-test' CREATEDB")
        try:
            dsn = psycopg.conninfo.make_conninfo(
                admin_dsn, user=role, password="pgverdict-test"
            )
            with pytest.raises(WritableRoleError, match="CREATEDB"):
                capture_snapshot(dsn, [])
        finally:
            admin.execute(f"DROP ROLE {role}")

    def test_minimum_privilege_role_passes(self, ro_dsn: str) -> None:
        snapshot = capture_snapshot(ro_dsn, [])
        assert snapshot.role.transaction_read_only
        assert not snapshot.role.superuser
        assert snapshot.role.can_read_all_stats


class TestTableFacts:
    def test_analyzed_table_with_index(
        self, admin: psycopg.Connection, ro_dsn: str
    ) -> None:
        table = unique_name("t_happy")
        admin.execute(f"CREATE TABLE public.{table} (id int PRIMARY KEY, body text)")
        admin.execute(
            f"INSERT INTO public.{table} SELECT g, repeat('x', 100) "
            "FROM generate_series(1, 500) g"
        )
        admin.execute(f"ANALYZE public.{table}")
        try:
            snapshot = capture_snapshot(ro_dsn, [f"public.{table}"])
            facts = relation(snapshot, f"public.{table}")
            assert facts.exists.value is True
            assert facts.relkind.value == "r"
            assert facts.schema.value == "public"
            assert facts.is_partitioned.value is False
            assert facts.reltuples.available and facts.reltuples.value == 500
            assert facts.relpages.available and (facts.relpages.value or 0) >= 1
            assert (facts.relation_size_bytes.value or 0) > 0
            total = facts.total_relation_size_bytes.value or 0
            assert total >= (facts.relation_size_bytes.value or 0)
            assert facts.last_analyze.available and facts.last_analyze.value is not None
            assert facts.n_mod_since_analyze.available
            assert facts.index_count.value == 1
            assert facts.indexes.value is not None
            [index] = facts.indexes.value
            assert index.name == f"{table}_pkey"
            assert index.valid
            assert (index.size_bytes.value or 0) > 0
            assert index.method == "btree"
            assert not index.partial
            assert not index.has_expressions
            assert index.default_opclasses
            assert index.depends_on_columns == ("id",)
            assert facts.invalid_indexes.value == ()
        finally:
            admin.execute(f"DROP TABLE public.{table}")

    def test_index_shape_facts_cover_partial_expression_and_opclass(
        self, admin: psycopg.Connection, ro_dsn: str
    ) -> None:
        table = unique_name("t_ix_shapes")
        admin.execute(
            f"CREATE TABLE public.{table} "
            "(id int, status varchar(32), title varchar(64), flag boolean)"
        )
        admin.execute(
            f"CREATE INDEX {table}_partial ON public.{table} (status) WHERE flag"
        )
        admin.execute(
            f"CREATE INDEX {table}_expr ON public.{table} (lower(title))"
        )
        admin.execute(
            f"CREATE INDEX {table}_pattern ON public.{table} "
            "(status varchar_pattern_ops)"
        )
        try:
            snapshot = capture_snapshot(ro_dsn, [f"public.{table}"])
            facts = relation(snapshot, f"public.{table}")
            assert facts.indexes.value is not None
            by_name = {ix.name: ix for ix in facts.indexes.value}
            partial = by_name[f"{table}_partial"]
            # depends_on comes from pg_depend, so the predicate-only column
            # (flag) counts as a dependency alongside the key column.
            assert partial.partial and not partial.has_expressions
            assert partial.depends_on_columns == ("flag", "status")
            expr = by_name[f"{table}_expr"]
            assert expr.has_expressions and not expr.partial
            assert expr.depends_on_columns == ("title",)
            pattern = by_name[f"{table}_pattern"]
            assert not pattern.default_opclasses
            assert pattern.depends_on_columns == ("status",)
            assert all(ix.method == "btree" for ix in by_name.values())
        finally:
            admin.execute(f"DROP TABLE public.{table}")

    def test_missing_relation_is_a_fact_not_an_error(self, ro_dsn: str) -> None:
        snapshot = capture_snapshot(ro_dsn, ["public.definitely_not_there_xyz"])
        facts = relation(snapshot, "public.definitely_not_there_xyz")
        assert facts.exists.available and facts.exists.value is False
        assert not facts.reltuples.available
        assert facts.reltuples.reason == "relation does not exist"
        assert not facts.indexes.available

    def test_unparsable_relation_name_degrades(self, ro_dsn: str) -> None:
        snapshot = capture_snapshot(ro_dsn, ["this is (not) a name"])
        facts = relation(snapshot, "this is (not) a name")
        if facts.exists.available:
            # PG 16+: to_regclass returns NULL for syntactically invalid
            # names, so garbage resolves to a clean "does not exist".
            assert facts.exists.value is False
        else:
            # PG <= 15 raises on invalid syntax; the error is preserved.
            assert "could not resolve relation" in (facts.exists.reason or "")

    def test_never_analyzed_table_marks_reltuples_unusable(
        self, admin: psycopg.Connection, ro_dsn: str
    ) -> None:
        table = unique_name("t_unanalyzed")
        admin.execute(
            f"CREATE TABLE public.{table} (id int) WITH (autovacuum_enabled = off)"
        )
        admin.execute(f"INSERT INTO public.{table} SELECT generate_series(1, 100)")
        try:
            snapshot = capture_snapshot(ro_dsn, [f"public.{table}"])
            facts = relation(snapshot, f"public.{table}")
            assert facts.exists.value is True
            if (snapshot.server.pg_major.value or 0) >= 14:
                assert not facts.reltuples.available
                assert "never been vacuumed or analyzed" in (facts.reltuples.reason or "")
            # "never analyzed" is a known state, not a gathering failure:
            assert facts.last_analyze.available and facts.last_analyze.value is None
            assert facts.last_autoanalyze.available and facts.last_autoanalyze.value is None
            assert (facts.relation_size_bytes.value or 0) > 0
        finally:
            admin.execute(f"DROP TABLE public.{table}")

    def test_partitioned_table_counts_and_sizes_partitions(
        self, admin: psycopg.Connection, ro_dsn: str
    ) -> None:
        table = unique_name("t_parted")
        admin.execute(
            f"CREATE TABLE public.{table} (id int, at date) PARTITION BY RANGE (at)"
        )
        admin.execute(
            f"CREATE TABLE public.{table}_a PARTITION OF public.{table} "
            "FOR VALUES FROM ('2026-01-01') TO ('2026-02-01')"
        )
        admin.execute(
            f"CREATE TABLE public.{table}_b PARTITION OF public.{table} "
            "FOR VALUES FROM ('2026-02-01') TO ('2026-03-01')"
        )
        admin.execute(
            f"INSERT INTO public.{table} SELECT g, '2026-01-15' "
            "FROM generate_series(1, 200) g"
        )
        try:
            snapshot = capture_snapshot(ro_dsn, [f"public.{table}"])
            facts = relation(snapshot, f"public.{table}")
            assert facts.relkind.value == "p"
            assert facts.is_partitioned.value is True
            assert facts.partition_count.value == 2
            assert (facts.partitions_total_size_bytes.value or 0) > 0
            assert facts.relation_size_bytes.value == 0  # parents hold no data
        finally:
            admin.execute(f"DROP TABLE public.{table}")

    def test_invalid_index_is_reported(
        self, admin: psycopg.Connection, ro_dsn: str
    ) -> None:
        table = unique_name("t_invalid_ix")
        admin.execute(f"CREATE TABLE public.{table} (id int)")
        admin.execute(f"CREATE INDEX {table}_broken ON public.{table} (id)")
        # Simulate a failed CREATE INDEX CONCURRENTLY leftover.
        admin.execute(
            "UPDATE pg_index SET indisvalid = false WHERE indexrelid = %s::regclass",
            (f"public.{table}_broken",),
        )
        try:
            snapshot = capture_snapshot(ro_dsn, [f"public.{table}"])
            facts = relation(snapshot, f"public.{table}")
            assert facts.invalid_indexes.value == (f"{table}_broken",)
            assert facts.indexes.value is not None
            [index] = facts.indexes.value
            assert not index.valid
        finally:
            admin.execute(f"DROP TABLE public.{table}")

    def test_qualified_name_object_resolves_with_case_preserved(
        self, admin: psycopg.Connection, ro_dsn: str
    ) -> None:
        table = "T_" + unique_name("Mixed")
        admin.execute(f'CREATE TABLE public."{table}" (id int)')
        try:
            snapshot = capture_snapshot(
                ro_dsn, [QualifiedName(name=table, schema="public")]
            )
            facts = relation(snapshot, f"public.{table}")
            assert facts.exists.value is True
            assert facts.name.value == table
        finally:
            admin.execute(f'DROP TABLE public."{table}"')


class TestConcurrencyFacts:
    def test_no_pg_monitor_degrades_explicitly(
        self, admin_dsn: str, nomon_dsn: str
    ) -> None:
        with psycopg.connect(admin_dsn) as idle:
            idle.execute("SELECT 1")  # open transaction, then go idle in it
            snapshot = capture_snapshot(
                nomon_dsn, [], long_transaction_threshold_ms=0
            )
        assert not snapshot.role.can_read_all_stats
        long_txns = snapshot.concurrency.long_transactions
        assert not long_txns.available
        assert "pg_read_all_stats" in (long_txns.reason or "")
        connections = snapshot.concurrency.current_connections
        assert not connections.available
        assert "pg_read_all_stats" in (connections.reason or "")
        # pg_locks is not masked: waiters stay available even without pg_monitor.
        assert snapshot.concurrency.lock_waiters.available
        assert snapshot.concurrency.max_connections.available

    def test_idle_in_transaction_is_distinguished(
        self, admin_dsn: str, ro_dsn: str
    ) -> None:
        with psycopg.connect(admin_dsn) as idle:
            idle.execute("SELECT 1")
            idle_pid = idle.info.backend_pid
            time.sleep(0.1)
            snapshot = capture_snapshot(ro_dsn, [], long_transaction_threshold_ms=0)
        txns = snapshot.concurrency.long_transactions
        assert txns.available and txns.value is not None
        [mine] = [t for t in txns.value if t.pid == idle_pid]
        assert mine.state == "idle in transaction"
        assert mine.idle_in_transaction
        assert mine.xact_age_ms >= 0
        # Only the first keyword of the query is ever captured — never literals.
        assert mine.first_keyword.value == "select"

    def test_connection_count_against_max(self, ro_dsn: str) -> None:
        snapshot = capture_snapshot(ro_dsn, [])
        assert (snapshot.concurrency.current_connections.value or 0) >= 1
        assert (snapshot.concurrency.max_connections.value or 0) >= 1

    def test_live_lock_conflict_is_reported_and_sizes_degrade(
        self, admin_dsn: str, ro_dsn: str
    ) -> None:
        table = unique_name("t_conflict")
        with psycopg.connect(admin_dsn, autocommit=True) as setup:
            setup.execute(f"CREATE TABLE public.{table} (id int)")
        holder = psycopg.connect(admin_dsn)
        waiter = psycopg.connect(admin_dsn)
        waiter_pid = waiter.info.backend_pid
        holder_pid = holder.info.backend_pid
        thread: threading.Thread | None = None
        try:
            holder.execute(f"LOCK TABLE public.{table} IN ACCESS EXCLUSIVE MODE")

            def block() -> None:
                try:
                    waiter.execute("SET lock_timeout = '30s'")
                    waiter.execute(f"LOCK TABLE public.{table} IN ACCESS SHARE MODE")
                except psycopg.Error:
                    pass

            thread = threading.Thread(target=block)
            thread.start()
            with psycopg.connect(admin_dsn, autocommit=True) as poll:
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    row = poll.execute(
                        "SELECT count(*) FROM pg_locks WHERE NOT granted AND pid = %s",
                        (waiter_pid,),
                    ).fetchone()
                    if row is not None and row[0] >= 1:
                        break
                    time.sleep(0.05)
                else:
                    pytest.fail("waiter never started waiting")

            snapshot = capture_snapshot(
                ro_dsn,
                [f"public.{table}"],
                lock_timeout_ms=500,
                long_transaction_threshold_ms=0,
            )
        finally:
            holder.rollback()
            if thread is not None:
                thread.join(timeout=30)
            holder.close()
            waiter.close()
            with psycopg.connect(admin_dsn, autocommit=True) as teardown:
                teardown.execute(f"DROP TABLE public.{table}")

        waiters = snapshot.concurrency.lock_waiters
        assert waiters.available and waiters.value is not None
        [conflict] = [w for w in waiters.value if w.blocked_pid == waiter_pid]
        assert conflict.relation == f"public.{table}"
        assert conflict.blocked_mode == "AccessShareLock"
        assert holder_pid in conflict.blocking_pids
        assert "AccessExclusiveLock" in conflict.blocking_modes
        if (snapshot.server.pg_major.value or 0) >= 14:
            assert conflict.waiting_for_ms.available

        # The size functions open the relation and queue behind the ACCESS
        # EXCLUSIVE lock; bounded execution turns that into an explicit
        # per-field marker instead of a hung introspection.
        facts = relation(snapshot, f"public.{table}")
        assert facts.exists.value is True  # pg_class facts take no relation lock
        assert facts.reltuples.reason is not None or facts.reltuples.available
        assert not facts.relation_size_bytes.available
        assert "lock" in (facts.relation_size_bytes.reason or "").lower()
        # The holder's open transaction is also visible as a long transaction.
        txns = snapshot.concurrency.long_transactions
        assert txns.available and txns.value is not None
        assert any(t.pid == holder_pid for t in txns.value)


class TestReplicationFacts:
    def test_no_replicas_is_a_known_false(self, ro_dsn: str) -> None:
        snapshot = capture_snapshot(ro_dsn, [])
        assert snapshot.replication.has_replicas.available
        assert snapshot.replication.has_replicas.value is False
        assert snapshot.replication.replicas.available
        assert snapshot.replication.replicas.value == ()
        assert snapshot.replication.synchronous.available
        assert snapshot.replication.synchronous.value is False
        # The value is whatever the server runs with (the harness turns it
        # off for speed); the fact just has to be available and sensible.
        assert snapshot.replication.synchronous_commit.value in (
            "on", "off", "local", "remote_write", "remote_apply"
        )


class TestServerFacts:
    def test_version_and_configured_timeouts(self, ro_dsn: str) -> None:
        snapshot = capture_snapshot(ro_dsn, [], statement_timeout_ms=4321)
        version_num = snapshot.server.server_version_num.value or 0
        assert version_num >= 100000
        assert snapshot.server.pg_major.value == version_num // 10000
        assert snapshot.server.in_recovery.value is False
        assert snapshot.server.server_now.available
        # The *configured* timeouts, not this session's introspection bounds:
        assert snapshot.server.statement_timeout_ms.value == 0
        assert snapshot.server.lock_timeout_ms.value == 0


class TestSnapshotSerialization:
    def test_live_snapshot_serializes_canonically(
        self, admin: psycopg.Connection, ro_dsn: str
    ) -> None:
        table = unique_name("t_serialize")
        admin.execute(f"CREATE TABLE public.{table} (id int)")
        try:
            snapshot = capture_snapshot(ro_dsn, [f"public.{table}"])
            first = snapshot.to_canonical_json()
            second = snapshot.to_canonical_json()
            assert first == second
            parsed = json.loads(first)
            assert parsed["snapshot_format"] == 3
            assert parsed["target"]["user"] == "pgverdict_ro"
            assert "password" not in first
        finally:
            admin.execute(f"DROP TABLE public.{table}")


class TestConnectionFailureMidCapture:
    def test_driver_errors_are_wrapped_in_our_exception(
        self, ro_dsn: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import pgverdict.live.introspect as introspect
        from pgverdict.live import LiveIntrospectionError

        def explode(conn: object, limits: object) -> object:
            raise psycopg.errors.AdminShutdown()

        monkeypatch.setattr(introspect, "_guard_role", explode)
        with pytest.raises(LiveIntrospectionError, match="introspection failed"):
            capture_snapshot(ro_dsn, [])



class TestColumnAndConstraintFacts:
    def test_column_facts_cover_types_defaults_identity_and_drops(
        self, admin: psycopg.Connection, ro_dsn: str
    ) -> None:
        table = unique_name("t_cols")
        domain = unique_name("dom_email")
        admin.execute(f"CREATE DOMAIN {domain} AS text CHECK (VALUE LIKE '%@%')")
        admin.execute(
            f"""
            CREATE TABLE public.{table} (
                id bigint GENERATED ALWAYS AS IDENTITY,
                name varchar(50) NOT NULL,
                note text DEFAULT 'x',
                created timestamptz DEFAULT now(),
                shadow text,
                lowered text GENERATED ALWAYS AS (lower(name)) STORED,
                email {domain}
            )
            """
        )
        admin.execute(f"ALTER TABLE public.{table} DROP COLUMN shadow")
        try:
            snapshot = capture_snapshot(ro_dsn, [f"public.{table}"])
            facts = relation(snapshot, f"public.{table}")
            assert facts.columns.available and facts.columns.value is not None
            by_name = {c.name: c for c in facts.columns.value}
            assert list(by_name) == ["id", "name", "note", "created", "lowered", "email"]

            assert by_name["id"].identity == "a"
            assert by_name["id"].not_null
            assert by_name["id"].data_type == "bigint"

            assert by_name["name"].data_type == "character varying(50)"
            assert by_name["name"].not_null
            assert not by_name["name"].has_default
            assert by_name["name"].default_expression is None

            assert by_name["note"].has_default
            assert by_name["note"].default_expression == "'x'::text"

            assert by_name["created"].default_expression == "now()"

            assert by_name["lowered"].generated == "s"

            email = by_name["email"]
            assert email.is_domain
            assert email.domain_base_type == "text"
            assert email.domain_constraint_count == 1
            assert not email.domain_not_null

            # The dropped column still occupies a tuple slot: rewrite cost.
            assert facts.dropped_column_count.value == 1
            assert by_name["lowered"].attnum == 6  # gap where shadow was
        finally:
            admin.execute(f"DROP TABLE public.{table}")
            admin.execute(f"DROP DOMAIN {domain}")

    def test_constraint_facts_distinguish_fk_check_and_validation(
        self, admin: psycopg.Connection, ro_dsn: str
    ) -> None:
        parent = unique_name("t_parent")
        child = unique_name("t_child")
        admin.execute(f"CREATE TABLE public.{parent} (id int PRIMARY KEY)")
        admin.execute(
            f"""
            CREATE TABLE public.{child} (
                id int PRIMARY KEY,
                parent_id int,
                qty int
            )
            """
        )
        admin.execute(
            f"ALTER TABLE public.{child} ADD CONSTRAINT c_fk "
            f"FOREIGN KEY (parent_id) REFERENCES public.{parent} (id) NOT VALID"
        )
        admin.execute(
            f"ALTER TABLE public.{child} ADD CONSTRAINT c_qty_nn "
            f"CHECK (qty IS NOT NULL) NOT VALID"
        )
        admin.execute(f"ALTER TABLE public.{child} VALIDATE CONSTRAINT c_qty_nn")
        try:
            snapshot = capture_snapshot(ro_dsn, [f"public.{child}"])
            facts = relation(snapshot, f"public.{child}")
            assert facts.constraints.available and facts.constraints.value is not None
            by_name = {c.name: c for c in facts.constraints.value}

            fk = by_name["c_fk"]
            assert fk.contype == "f"
            assert not fk.validated  # NOT VALID is exactly what VALIDATE needs
            assert fk.columns == ("parent_id",)
            # The referenced table is the fact the catalog's validate_constraint
            # and drop_constraint rows declare missing: it is locked too.
            assert fk.referenced_table == f"public.{parent}"
            assert fk.check_expression is None

            check = by_name["c_qty_nn"]
            assert check.contype == "c"
            assert check.validated  # a valid CHECK: the SET NOT NULL scan skip
            assert check.columns == ("qty",)
            assert check.check_expression == "(qty IS NOT NULL)"
            assert "CHECK" in check.definition

            pkey = by_name[f"{child}_pkey"]
            assert pkey.contype == "p"
            assert pkey.validated
        finally:
            admin.execute(f"DROP TABLE public.{child}")
            admin.execute(f"DROP TABLE public.{parent}")


class TestFunctionFacts:
    def test_custom_function_volatility_is_decided(
        self, admin: psycopg.Connection, ro_dsn: str
    ) -> None:
        fn_v = unique_name("f_vol")
        fn_i = unique_name("f_imm")
        admin.execute(
            f"CREATE FUNCTION public.{fn_v}() RETURNS int "
            f"LANGUAGE sql AS 'SELECT 1'"  # default volatility: VOLATILE
        )
        admin.execute(
            f"CREATE FUNCTION public.{fn_i}(int) RETURNS int IMMUTABLE "
            f"LANGUAGE sql AS 'SELECT $1'"
        )
        try:
            snapshot = capture_snapshot(ro_dsn, [], functions=[fn_v, fn_i, "no_such_fn"])
            by_name = {f.requested: f for f in snapshot.functions}
            assert list(by_name) == sorted([fn_v, fn_i, "no_such_fn"])

            assert by_name[fn_v].exists.value is True
            assert by_name[fn_v].volatility.value == "v"
            assert by_name[fn_v].prokind.value == "f"

            assert by_name[fn_i].volatility.value == "i"

            missing = by_name["no_such_fn"]
            assert missing.exists.value is False
            assert missing.overloads.value == 0
            assert not missing.volatility.available
        finally:
            admin.execute(f"DROP FUNCTION public.{fn_v}()")
            admin.execute(f"DROP FUNCTION public.{fn_i}(int)")

    def test_cross_schema_disagreement_stays_undecided(
        self, admin: psycopg.Connection, ro_dsn: str
    ) -> None:
        schema = unique_name("s_other")
        fn = unique_name("f_shadow")
        admin.execute(f"CREATE SCHEMA {schema}")
        admin.execute(
            f"CREATE FUNCTION public.{fn}() RETURNS int IMMUTABLE "
            f"LANGUAGE sql AS 'SELECT 1'"
        )
        admin.execute(
            f"CREATE FUNCTION {schema}.{fn}() RETURNS int VOLATILE "
            f"LANGUAGE sql AS 'SELECT 2'"
        )
        try:
            snapshot = capture_snapshot(ro_dsn, [], functions=[fn, f"{schema}.{fn}"])
            by_name = {f.requested: f for f in snapshot.functions}
            # Unqualified: the migration's search_path is unknowable, and the
            # two candidates disagree — no guess.
            unqualified = by_name[fn]
            assert unqualified.exists.value is True
            assert unqualified.overloads.value == 2
            assert not unqualified.volatility.available
            assert "disagree" in (unqualified.volatility.reason or "")
            # Qualified: decided.
            qualified = by_name[f"{schema}.{fn}"]
            assert qualified.overloads.value == 1
            assert qualified.volatility.value == "v"
        finally:
            admin.execute(f"DROP SCHEMA {schema} CASCADE")
            admin.execute(f"DROP FUNCTION public.{fn}()")

    def test_unknown_default_becomes_decided_only_with_live_facts(
        self, admin: psycopg.Connection, ro_dsn: str
    ) -> None:
        from pgverdict import parse_migration
        from pgverdict.ir import Volatility
        from pgverdict.live import decide_default_volatility

        fn = unique_name("f_gen")
        admin.execute(
            f"CREATE FUNCTION public.{fn}() RETURNS uuid "
            f"LANGUAGE sql AS 'SELECT gen_random_uuid()'"  # VOLATILE by default
        )
        try:
            script = parse_migration(f"ALTER TABLE t ADD COLUMN c uuid DEFAULT {fn}();")
            [statement] = script.statements
            [action] = statement.alter_actions
            assert action.default is not None
            # Offline: the static allowlists cannot know a user function.
            assert action.default.volatility is Volatility.UNKNOWN
            assert action.default.unknown_functions == (fn,)

            snapshot = capture_snapshot(
                ro_dsn, [], functions=action.default.unknown_functions
            )
            decided = decide_default_volatility(action.default, snapshot)
            assert decided is Volatility.VOLATILE

            # A snapshot without the function keeps the honest UNKNOWN.
            empty = capture_snapshot(ro_dsn, [])
            assert decide_default_volatility(action.default, empty) is Volatility.UNKNOWN
        finally:
            admin.execute(f"DROP FUNCTION public.{fn}()")


class TestTypeFacts:
    def test_domain_and_plain_types_resolve(
        self, admin: psycopg.Connection, ro_dsn: str
    ) -> None:
        domain = unique_name("dom_status")
        admin.execute(
            f"CREATE DOMAIN {domain} AS text NOT NULL "
            f"CHECK (VALUE IN ('active', 'gone'))"
        )
        try:
            snapshot = capture_snapshot(
                ro_dsn, [], types=[domain, "varchar(20)", "no_such_type"]
            )
            by_name = {t.requested: t for t in snapshot.types}

            dom = by_name[domain]
            assert dom.exists.value is True
            assert dom.is_domain.value is True
            assert dom.domain_base_type.value == "text"
            assert dom.domain_not_null.value is True
            assert dom.domain_constraint_count.value == 1

            plain = by_name["varchar(20)"]
            assert plain.exists.value is True
            assert plain.formatted.value == "character varying"
            assert plain.is_domain.value is False

            missing = by_name["no_such_type"]
            assert missing.exists.value is False or not missing.exists.available
        finally:
            admin.execute(f"DROP DOMAIN {domain}")


class TestTypeChangeFactsLive:
    def test_facts_and_assessments_for_common_pairs(
        self, admin: psycopg.Connection, ro_dsn: str
    ) -> None:
        from pgverdict.live import RewriteVerdict, TypeChangeProbe, assess_type_change

        table = unique_name("t_types")
        admin.execute(
            f"""
            CREATE TABLE public.{table} (
                vc varchar(10),
                num numeric(10,2),
                n int,
                ts timestamp,
                body text
            )
            """
        )
        try:
            probes = [
                TypeChangeProbe(f"public.{table}", "vc", "varchar(20)"),
                TypeChangeProbe(f"public.{table}", "vc", "varchar(5)"),
                TypeChangeProbe(f"public.{table}", "vc", "text"),
                TypeChangeProbe(f"public.{table}", "body", "varchar(10)"),
                TypeChangeProbe(f"public.{table}", "n", "bigint"),
                TypeChangeProbe(f"public.{table}", "num", "numeric(12,2)"),
                TypeChangeProbe(f"public.{table}", "ts", "timestamptz"),
                TypeChangeProbe(f"public.{table}", "ghost", "text"),
            ]
            snapshot = capture_snapshot(ro_dsn, [], type_changes=probes)
            pg_major = snapshot.server.pg_major.value or 0
            by_key = {(c.column, c.new_type_requested): c for c in snapshot.type_changes}

            def verdict(column: str, new_type: str) -> RewriteVerdict:
                return assess_type_change(
                    by_key[(column, new_type)], pg_major=pg_major
                ).verdict

            grow = by_key[("vc", "varchar(20)")]
            assert grow.current_type.value == "character varying(10)"
            assert grow.same_type.value is True
            assert verdict("vc", "varchar(20)") is RewriteVerdict.NO_REWRITE

            assert verdict("vc", "varchar(5)") is RewriteVerdict.REWRITE

            widen = by_key[("vc", "text")]
            assert widen.cast_method.value == "b"
            assert verdict("vc", "text") is RewriteVerdict.NO_REWRITE

            assert verdict("body", "varchar(10)") is RewriteVerdict.REWRITE

            int_widen = by_key[("n", "bigint")]
            assert int_widen.cast_method.value == "f"
            assert verdict("n", "bigint") is RewriteVerdict.REWRITE

            assert verdict("num", "numeric(12,2)") is RewriteVerdict.NO_REWRITE

            assert (
                verdict("ts", "timestamptz")
                is RewriteVerdict.NO_REWRITE_IF_SESSION_TZ_UTC
            )

            ghost = by_key[("ghost", "text")]
            assert not ghost.current_type.available
            assert "not found" in (ghost.current_type.reason or "")
            assessment = assess_type_change(ghost, pg_major=pg_major)
            assert assessment.verdict is RewriteVerdict.UNKNOWN
        finally:
            admin.execute(f"DROP TABLE public.{table}")

    def test_assessments_match_relfilenode_ground_truth(
        self, admin: psycopg.Connection, ro_dsn: str
    ) -> None:
        from pgverdict.live import RewriteVerdict, TypeChangeProbe, assess_type_change

        cases = [
            ("varchar(10)", "varchar(20)", False),
            ("varchar(20)", "varchar(10)", True),
            ("varchar(10)", "text", False),
            ("text", "varchar(10)", True),
            ("integer", "bigint", True),
            ("numeric(10,2)", "numeric(12,2)", False),
            ("timestamp(3)", "timestamp(6)", False),
            ("xml", "text", False),
        ]
        table = unique_name("t_truth")
        admin.execute("SET TimeZone = 'UTC'")
        for current, target, expect_rewrite in cases:
            admin.execute(f"CREATE TABLE public.{table} (c {current})")
            try:
                probe = TypeChangeProbe(f"public.{table}", "c", target)
                snapshot = capture_snapshot(ro_dsn, [], type_changes=[probe])
                [facts] = snapshot.type_changes
                assessment = assess_type_change(
                    facts, pg_major=snapshot.server.pg_major.value or 0
                )

                row = admin.execute(
                    "SELECT relfilenode FROM pg_class WHERE relname = %s", (table,)
                ).fetchone()
                assert row is not None
                before = row[0]
                admin.execute(f"ALTER TABLE public.{table} ALTER COLUMN c TYPE {target}")
                row = admin.execute(
                    "SELECT relfilenode FROM pg_class WHERE relname = %s", (table,)
                ).fetchone()
                assert row is not None
                rewrote = before != row[0]

                assert rewrote is expect_rewrite, f"{current} -> {target}"
                expected_verdict = (
                    RewriteVerdict.REWRITE
                    if expect_rewrite
                    else RewriteVerdict.NO_REWRITE
                )
                assert assessment.verdict is expected_verdict, (
                    f"{current} -> {target}: assessed {assessment.verdict}, "
                    f"server {'rewrote' if rewrote else 'did not rewrite'} "
                    f"({assessment.reason})"
                )
            finally:
                admin.execute(f"DROP TABLE public.{table}")


class TestServerTimezoneFact:
    def test_reset_val_is_the_configured_zone_not_our_utc_override(
        self, admin: psycopg.Connection, ro_dsn: str
    ) -> None:
        # The introspection session runs under SET TimeZone = 'UTC'; the
        # captured fact must still be the *configured* zone — the same
        # startup-packet trap the timeouts hit in the first increment.
        admin.execute("ALTER DATABASE postgres SET TimeZone = 'America/Chicago'")
        try:
            snapshot = capture_snapshot(ro_dsn, [])
            assert snapshot.server.timezone.value == "America/Chicago"
        finally:
            admin.execute("ALTER DATABASE postgres RESET TimeZone")


class TestCaptureOrdering:
    def test_introspection_never_reports_its_own_lock_conflict(
        self, admin_dsn: str, ro_dsn: str
    ) -> None:
        # An ACCESS EXCLUSIVE holder and nobody else: the only backend that
        # could possibly queue on the relation is the introspection itself
        # (its size queries). The snapshot must show the sizes as degraded
        # while reporting ZERO waiters — a waiter entry here would be the
        # introspection observing itself.
        table = unique_name("t_selfwait")
        with psycopg.connect(admin_dsn, autocommit=True) as setup:
            setup.execute(f"CREATE TABLE public.{table} (id int)")
        holder = psycopg.connect(admin_dsn)
        try:
            holder.execute(f"LOCK TABLE public.{table} IN ACCESS EXCLUSIVE MODE")
            snapshot = capture_snapshot(
                ro_dsn,
                [f"public.{table}"],
                lock_timeout_ms=300,
                long_transaction_threshold_ms=0,
            )
        finally:
            holder.rollback()
            holder.close()
            with psycopg.connect(admin_dsn, autocommit=True) as teardown:
                teardown.execute(f"DROP TABLE public.{table}")

        facts = relation(snapshot, f"public.{table}")
        # Size facts degraded: they queue behind the ACCESS EXCLUSIVE lock.
        assert not facts.relation_size_bytes.available
        # Lock-free facts survived: gathered before any lock was requested.
        assert facts.exists.value is True
        assert facts.columns.available and facts.columns.value is not None
        assert facts.constraints.available
        # And the waiter list is empty: the concurrency capture ran before
        # the introspection's own lock-taking queries, and its own pid is
        # excluded — the conflict the sizes hit is not reported as a waiter.
        waiters = snapshot.concurrency.lock_waiters
        assert waiters.available
        assert waiters.value == ()


class TestNoNewPrivilegesRequired:
    def test_second_increment_facts_survive_without_pg_monitor(
        self, admin: psycopg.Connection, nomon_dsn: str
    ) -> None:
        # The three-statement role grant is the sales pitch: this increment
        # must not widen it. Columns, constraints, function volatility, type
        # facts, and type-change facts are plain catalog reads — they work
        # even WITHOUT pg_monitor (which only unmasks activity/replication).
        from pgverdict.live import TypeChangeProbe

        table = unique_name("t_priv")
        fn = unique_name("f_priv")
        admin.execute(
            f"CREATE TABLE public.{table} "
            f"(id int PRIMARY KEY, body varchar(10) NOT NULL)"
        )
        admin.execute(
            f"CREATE FUNCTION public.{fn}() RETURNS int IMMUTABLE "
            f"LANGUAGE sql AS 'SELECT 1'"
        )
        try:
            snapshot = capture_snapshot(
                nomon_dsn,
                [f"public.{table}"],
                functions=[fn],
                types=["varchar(20)"],
                type_changes=[TypeChangeProbe(f"public.{table}", "body", "text")],
            )
            facts = relation(snapshot, f"public.{table}")
            assert facts.columns.available and facts.columns.value is not None
            assert {c.name for c in facts.columns.value} == {"id", "body"}
            assert facts.constraints.available and facts.constraints.value is not None
            assert facts.dropped_column_count.value == 0

            [fn_facts] = snapshot.functions
            assert fn_facts.volatility.value == "i"

            [type_facts] = snapshot.types
            assert type_facts.exists.value is True

            [change] = snapshot.type_changes
            assert change.cast_method.value == "b"
            assert snapshot.server.timezone.available
        finally:
            admin.execute(f"DROP TABLE public.{table}")
            admin.execute(f"DROP FUNCTION public.{fn}()")
