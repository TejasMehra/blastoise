"""Deterministic tests for the degraded introspection paths.

The live integration tests stage real failures (lock conflicts, missing
privileges); these drive the remaining per-section failure branches with a
scripted fake connection, because a healthy single-node test server cannot
produce them on demand: replica rows, query timeouts in specific sections,
pre-PG14 waiter output, masked query text. No database needed.
"""

from __future__ import annotations

from typing import Any, cast

import psycopg
import pytest

from blastoise.live.introspect import (
    WritableRoleError,
    _describe_error,
    _finish_relation,
    _gather_columns,
    _gather_concurrency,
    _gather_constraints,
    _gather_function,
    _gather_long_transactions,
    _gather_relation_static,
    _gather_replica_details,
    _gather_replication,
    _gather_server,
    _gather_type,
    _gather_type_change,
    _gather_waiters,
    _guard_role,
    _resolve_relation,
    _setting_ms,
    _setting_text,
)
from blastoise.live.model import (
    CaptureLimits,
    Fact,
    RelationFacts,
    RoleFacts,
    ServerFacts,
    TypeChangeProbe,
)

LIMITS = CaptureLimits(
    connect_timeout_s=10,
    statement_timeout_ms=5000,
    lock_timeout_ms=2000,
    long_transaction_threshold_ms=60_000,
    max_listed_transactions=100,
)

MONITOR_ROLE = RoleFacts(
    role="ro",
    transaction_read_only=True,
    superuser=False,
    can_read_all_stats=True,
    warnings=(),
)

Row = dict[str, Any]
Step = tuple[str, list[Row] | Exception]


class _FakeCursor:
    def __init__(self, rows: list[Row]) -> None:
        self._rows = rows

    def fetchall(self) -> list[Row]:
        return self._rows

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *args: object) -> None:
        return None


class _FakeTransaction:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *args: object) -> None:
        return None


class FakeConnection:
    """Replays a script of (sql-substring, rows-or-exception) steps.

    Each executed query must match the next remaining step containing its
    substring; unmatched queries fail the test, so a section under test
    cannot silently run more (or different) SQL than the script expects.
    """

    def __init__(self, script: list[Step]) -> None:
        self.script = list(script)

    def transaction(self) -> _FakeTransaction:
        return _FakeTransaction()

    def execute(self, sql: str, params: object = None) -> _FakeCursor:
        for i, (fragment, result) in enumerate(self.script):
            if fragment in sql:
                del self.script[i]
                if isinstance(result, Exception):
                    raise result
                return _FakeCursor(result)
        raise AssertionError(f"unexpected query: {sql!r}")


def conn_of(script: list[Step]) -> psycopg.Connection[Row]:
    return cast("psycopg.Connection[Row]", FakeConnection(script))


def server_facts(*, version_num: int = 170010, in_recovery: bool = False) -> ServerFacts:
    return ServerFacts(
        server_version_num=Fact.of(version_num),
        server_version=Fact.of("17.10"),
        pg_major=Fact.of(version_num // 10000),
        in_recovery=Fact.of(in_recovery),
        server_now=Fact.of("2026-08-20T12:00:00+00:00"),
        lock_timeout_ms=Fact.of(0),
        statement_timeout_ms=Fact.of(0),
        timezone=Fact.of("UTC"),
    )


class TestDescribeError:
    def test_statement_timeout_names_the_bound(self) -> None:
        reason = _describe_error(psycopg.errors.QueryCanceled(), LIMITS)
        assert "statement_timeout" in reason
        assert "5000 ms" in reason

    def test_lock_timeout_names_the_bound(self) -> None:
        reason = _describe_error(psycopg.errors.LockNotAvailable(), LIMITS)
        assert "lock_timeout" in reason
        assert "2000 ms" in reason

    def test_insufficient_privilege(self) -> None:
        reason = _describe_error(psycopg.errors.InsufficientPrivilege(), LIMITS)
        assert reason.startswith("insufficient privilege")

    def test_non_psycopg_exception(self) -> None:
        assert _describe_error(ValueError("boom"), LIMITS) == "ValueError: boom"


class TestGuardRoleDegraded:
    def test_session_not_read_only_is_fatal(self) -> None:
        conn = conn_of(
            [
                (
                    "transaction_read_only",
                    [
                        {
                            "role": "ro",
                            "txn_ro": "off",
                            "is_super": "off",
                            "can_stats": True,
                        }
                    ],
                )
            ]
        )
        with pytest.raises(WritableRoleError, match="READ ONLY transaction"):
            _guard_role(conn, LIMITS)

    def test_createrole_is_rejected(self) -> None:
        conn = conn_of(
            [
                (
                    "transaction_read_only",
                    [{"role": "ro", "txn_ro": "on", "is_super": "off", "can_stats": True}],
                ),
                (
                    "rolcreatedb",
                    [
                        {
                            "rolcreatedb": False,
                            "rolcreaterole": True,
                            "rolreplication": False,
                            "rolbypassrls": False,
                        }
                    ],
                ),
            ]
        )
        with pytest.raises(WritableRoleError, match="CREATEROLE"):
            _guard_role(conn, LIMITS)

    def test_write_adjacent_capabilities_become_warnings(self) -> None:
        conn = conn_of(
            [
                (
                    "transaction_read_only",
                    [{"role": "ro", "txn_ro": "on", "is_super": "off", "can_stats": False}],
                ),
                (
                    "rolcreatedb",
                    [
                        {
                            "rolcreatedb": False,
                            "rolcreaterole": False,
                            "rolreplication": True,
                            "rolbypassrls": True,
                        }
                    ],
                ),
                ("has_table_privilege", []),
                ("has_database_privilege", [{"c": True}]),
                ("has_schema_privilege", [{"schema": "public"}]),
            ]
        )
        role = _guard_role(conn, LIMITS)
        assert not role.can_read_all_stats
        joined = " | ".join(role.warnings)
        assert "REPLICATION" in joined
        assert "BYPASSRLS" in joined
        assert "CREATE schemas" in joined
        assert "schema(s): public" in joined


class TestServerDegraded:
    def test_server_query_failure_marks_every_field(self) -> None:
        conn = conn_of(
            [
                ("server_version_num", psycopg.errors.QueryCanceled()),
                ("pg_settings", []),
                ("pg_settings", []),
                ("pg_settings", []),
            ]
        )
        facts = _gather_server(conn, LIMITS)
        for fact in (
            facts.server_version_num,
            facts.server_version,
            facts.pg_major,
            facts.in_recovery,
            facts.server_now,
        ):
            assert not fact.available
            assert "statement_timeout" in (fact.reason or "")
        assert not facts.lock_timeout_ms.available
        assert facts.lock_timeout_ms.reason == "no pg_settings row for lock_timeout"

    def test_setting_in_unexpected_unit_is_not_guessed(self) -> None:
        conn = conn_of([("pg_settings", [{"reset_val": "1", "unit": "min"}])])
        fact = _setting_ms(conn, "statement_timeout", LIMITS)
        assert not fact.available
        assert "unexpected unit 'min'" in (fact.reason or "")

    def test_setting_query_failure_degrades(self) -> None:
        conn = conn_of([("pg_settings", psycopg.errors.InsufficientPrivilege())])
        fact = _setting_ms(conn, "lock_timeout", LIMITS)
        assert not fact.available


def full_relation(conn: psycopg.Connection[Row], requested: str) -> RelationFacts:
    """Resolve + static + finish, the way capture_snapshot sequences them."""
    res = _resolve_relation(conn, requested, requested, LIMITS)
    static = _gather_relation_static(conn, res, LIMITS, 170010)
    return _finish_relation(conn, res, static, LIMITS)


class TestRelationDegraded:
    def test_resolution_failure_marks_exists_unknown(self) -> None:
        conn = conn_of([("to_regclass", psycopg.errors.QueryCanceled())])
        res = _resolve_relation(conn, "public.t", "public.t", LIMITS)
        assert res.oid is None
        assert not res.exists.available
        assert "could not resolve relation" in (res.exists.reason or "")
        facts = _finish_relation(
            conn, res, _gather_relation_static(conn, res, LIMITS, 170010), LIMITS
        )
        assert not facts.exists.available
        assert not facts.columns.available
        assert not facts.constraints.available

    def test_each_section_degrades_independently(self) -> None:
        # Scripted in capture order: static facts (stats, partition count,
        # index list, columns, dropped count, constraints) run before the
        # size queries — the ordering contract.
        conn = conn_of(
            [
                (
                    "to_regclass",
                    [
                        {
                            "oid": 16384,
                            "schema": "public",
                            "name": "events",
                            "relkind": "p",
                            "relpages": 10,
                            "reltuples": 1000,
                        }
                    ],
                ),
                ("pg_stat_all_tables", psycopg.errors.QueryCanceled()),
                ("count(*)::bigint AS n FROM parts", psycopg.errors.QueryCanceled()),
                ("pg_index", psycopg.errors.InsufficientPrivilege()),
                ("pg_attrdef", psycopg.errors.QueryCanceled()),
                ("pg_get_constraintdef", psycopg.errors.QueryCanceled()),
                ("pg_total_relation_size", psycopg.errors.LockNotAvailable()),
                ("sum(pg_total_relation_size", psycopg.errors.QueryCanceled()),
            ]
        )
        facts = full_relation(conn, "public.events")
        assert facts.exists.value is True
        assert facts.reltuples.value == 1000  # pg_class facts survived
        assert not facts.relation_size_bytes.available
        assert "lock_timeout" in (facts.relation_size_bytes.reason or "")
        assert not facts.last_analyze.available
        assert not facts.partition_count.available
        assert not facts.partitions_total_size_bytes.available
        assert "could not size partitions" in (facts.partitions_total_size_bytes.reason or "")
        assert not facts.indexes.available
        assert not facts.invalid_indexes.available
        assert not facts.columns.available
        assert not facts.dropped_column_count.available
        assert not facts.constraints.available

    def test_one_unreadable_index_size_does_not_take_down_the_rest(self) -> None:
        conn = conn_of(
            [
                (
                    "to_regclass",
                    [
                        {
                            "oid": 16384,
                            "schema": "public",
                            "name": "t",
                            "relkind": "r",
                            "relpages": 1,
                            "reltuples": 10,
                        }
                    ],
                ),
                (
                    "pg_stat_all_tables",
                    [{"last_analyze": None, "last_autoanalyze": None, "n_mod": 0}],
                ),
                (
                    "pg_index",
                    [
                        {
                            "name": "t_a_idx",
                            "valid": True,
                            "oid": 1,
                            "method": "btree",
                            "partial": False,
                            "has_expressions": False,
                            "default_opclasses": True,
                            "depends_on": ["a"],
                        },
                        {
                            "name": "t_b_idx",
                            "valid": False,
                            "oid": 2,
                            "method": "btree",
                            "partial": False,
                            "has_expressions": False,
                            "default_opclasses": True,
                            "depends_on": ["b"],
                        },
                    ],
                ),
                ("pg_attrdef", []),
                ("AND attisdropped", [{"n": 0}]),
                ("pg_get_constraintdef", []),
                ("pg_total_relation_size", [{"rel": 8192, "total": 16384}]),
                ("pg_relation_size", psycopg.errors.LockNotAvailable()),
                ("pg_relation_size", [{"size": 4096}]),
            ]
        )
        facts = full_relation(conn, "public.t")
        assert facts.indexes.value is not None
        first, second = facts.indexes.value
        assert not first.size_bytes.available
        assert second.size_bytes.value == 4096
        assert facts.invalid_indexes.value == ("t_b_idx",)
        assert facts.columns.value == ()
        assert facts.dropped_column_count.value == 0
        assert facts.constraints.value == ()

    def test_columns_failure_leaves_constraints_standing(self) -> None:
        conn = conn_of(
            [
                ("pg_attrdef", psycopg.errors.QueryCanceled()),
                (
                    "pg_get_constraintdef",
                    [
                        {
                            "name": "t_pkey",
                            "contype": "p",
                            "validated": True,
                            "definition": "PRIMARY KEY (id)",
                            "check_expression": None,
                            "referenced_table": None,
                            "columns": ["id"],
                        }
                    ],
                ),
            ]
        )
        columns, dropped = _gather_columns(conn, 16384, LIMITS, 170010)
        assert not columns.available
        assert not dropped.available
        constraints = _gather_constraints(conn, 16384, LIMITS)
        assert constraints.value is not None
        [pkey] = constraints.value
        assert pkey.contype == "p"
        assert pkey.columns == ("id",)

    def test_function_overload_disagreement_degrades(self) -> None:
        conn = conn_of(
            [
                (
                    "pg_proc",
                    [
                        {"schema": "public", "provolatile": "v", "prokind": "f"},
                        {"schema": "app", "provolatile": "i", "prokind": "f"},
                    ],
                )
            ]
        )
        facts = _gather_function(conn, "gen_id", LIMITS, 170010)
        assert facts.exists.value is True
        assert facts.overloads.value == 2
        assert not facts.volatility.available
        assert "disagree" in (facts.volatility.reason or "")
        assert "public" in (facts.volatility.reason or "")

    def test_function_lookup_failure_degrades(self) -> None:
        conn = conn_of([("pg_proc", psycopg.errors.QueryCanceled())])
        facts = _gather_function(conn, "gen_id", LIMITS, 170010)
        assert not facts.exists.available
        assert not facts.volatility.available

    def test_dropped_count_failure_leaves_columns_standing(self) -> None:
        conn = conn_of(
            [
                ("pg_attrdef", []),
                ("AND attisdropped", psycopg.errors.QueryCanceled()),
            ]
        )
        columns, dropped = _gather_columns(conn, 16384, LIMITS, 170010)
        assert columns.available and columns.value == ()
        assert not dropped.available
        assert "statement_timeout" in (dropped.reason or "")

    def test_type_lookup_failure_degrades(self) -> None:
        # Pre-PG16 servers raise on syntactically invalid names passed to
        # to_regtype; the savepoint turns that into unavailable markers.
        conn = conn_of([("to_regtype", psycopg.errors.SyntaxError())])
        facts = _gather_type(conn, "not (a) type", LIMITS)
        assert not facts.exists.available
        assert not facts.is_domain.available

    def test_type_change_probe_failure_degrades(self) -> None:
        conn = conn_of([("pg_cast", psycopg.errors.SyntaxError())])
        facts = _gather_type_change(
            conn, TypeChangeProbe("public.t", "c", "not (a) type"), LIMITS
        )
        assert not facts.current_type.available
        assert not facts.cast_method.available
        assert facts.new_type_requested == "not (a) type"

    def test_setting_text_failure_degrades(self) -> None:
        conn = conn_of([("pg_settings", psycopg.errors.QueryCanceled())])
        fact = _setting_text(conn, "TimeZone", LIMITS)
        assert not fact.available

    def test_setting_text_missing_row(self) -> None:
        conn = conn_of([("pg_settings", [])])
        fact = _setting_text(conn, "TimeZone", LIMITS)
        assert not fact.available
        assert fact.reason == "no pg_settings row for TimeZone"


class TestConcurrencyDegraded:
    def test_pre_pg14_wait_duration_is_marked(self) -> None:
        conn = conn_of(
            [
                (
                    "NOT l.granted",
                    [
                        {
                            "blocked_pid": 200,
                            "blocked_mode": "AccessExclusiveLock",
                            "reloid": 16384,
                            "waiting_for_ms": None,
                            "blocking_pids": [100],
                        }
                    ],
                ),
                (
                    "l.granted",
                    [{"pid": 100, "mode": "AccessShareLock", "reloid": 16384}],
                ),
            ]
        )
        waiters = _gather_waiters(conn, {16384: "public.t"}, LIMITS, 130000, None)
        assert waiters.value is not None
        [waiter] = waiters.value
        assert not waiter.waiting_for_ms.available
        assert "PG 14+" in (waiter.waiting_for_ms.reason or "")
        assert waiter.blocking_modes == ("AccessShareLock",)

    def test_own_backend_pid_is_stripped_from_blocking_pids(self) -> None:
        # The SQL filter excludes our own rows server-side; the Python-side
        # strip covers pg_blocking_pids output, which the filter cannot reach.
        conn = conn_of(
            [
                (
                    "NOT l.granted",
                    [
                        {
                            "blocked_pid": 200,
                            "blocked_mode": "AccessExclusiveLock",
                            "reloid": 16384,
                            "waiting_for_ms": 100,
                            "blocking_pids": [100, 999],
                        }
                    ],
                ),
                (
                    "l.granted",
                    [{"pid": 100, "mode": "AccessShareLock", "reloid": 16384}],
                ),
            ]
        )
        waiters = _gather_waiters(conn, {16384: "public.t"}, LIMITS, 170010, 999)
        assert waiters.value is not None
        [waiter] = waiters.value
        assert waiter.blocking_pids == (100,)

    def test_waiter_query_failure_degrades(self) -> None:
        conn = conn_of([("NOT l.granted", psycopg.errors.QueryCanceled())])
        waiters = _gather_waiters(conn, {16384: "public.t"}, LIMITS, 170010, None)
        assert not waiters.available

    def test_masked_and_missing_query_text(self) -> None:
        conn = conn_of(
            [
                (
                    "pg_stat_activity",
                    [
                        {
                            "pid": 1,
                            "state": "active",
                            "xact_age_ms": 90_000,
                            "query_age_ms": 80_000,
                            "first_keyword": None,
                            "no_query": False,
                            "masked_query": True,
                            "waiting_on_lock": True,
                        },
                        {
                            "pid": 2,
                            "state": None,
                            "xact_age_ms": 70_000,
                            "query_age_ms": None,
                            "first_keyword": None,
                            "no_query": True,
                            "masked_query": False,
                            "waiting_on_lock": False,
                        },
                    ],
                )
            ]
        )
        txns = _gather_long_transactions(conn, LIMITS, MONITOR_ROLE)
        assert txns.value is not None
        masked, missing = txns.value
        assert not masked.first_keyword.available
        assert "masked" in (masked.first_keyword.reason or "")
        assert masked.waiting_on_lock
        assert missing.state == "unknown"
        assert not missing.first_keyword.available
        assert "no query text" in (missing.first_keyword.reason or "")
        assert not missing.query_age_ms.available

    def test_activity_query_failure_degrades(self) -> None:
        conn = conn_of([("pg_stat_activity", psycopg.errors.QueryCanceled())])
        txns = _gather_long_transactions(conn, LIMITS, MONITOR_ROLE)
        assert not txns.available

    def test_connection_count_failure_degrades(self) -> None:
        conn = conn_of(
            [
                ("NOT l.granted", []),
                ("l.granted", []),
                ("pg_stat_activity", []),
                ("client backend", psycopg.errors.QueryCanceled()),
                ("max_connections", [{"n": 100}]),
            ]
        )
        facts = _gather_concurrency(conn, {16384: "public.t"}, LIMITS, 170010, MONITOR_ROLE)
        assert not facts.current_connections.available
        assert facts.max_connections.value == 100


class TestReplicationDegraded:
    def test_count_failure_degrades_everything_downstream(self) -> None:
        conn = conn_of(
            [
                ("pg_stat_replication", psycopg.errors.InsufficientPrivilege()),
                ("synchronous_standby_names", psycopg.errors.QueryCanceled()),
            ]
        )
        facts = _gather_replication(conn, LIMITS, server_facts(), MONITOR_ROLE)
        assert not facts.has_replicas.available
        assert not facts.replicas.available
        assert not facts.synchronous.available
        assert not facts.synchronous_commit.available

    def test_replicas_present_but_role_cannot_read_details(self) -> None:
        nomon = RoleFacts(
            role="ro",
            transaction_read_only=True,
            superuser=False,
            can_read_all_stats=False,
            warnings=(),
        )
        conn = conn_of(
            [
                ("pg_stat_replication", [{"n": 2}]),
                ("synchronous_standby_names", [{"names": "ANY 1 (r1, r2)", "commit": "on"}]),
            ]
        )
        facts = _gather_replication(conn, LIMITS, server_facts(), nomon)
        assert facts.has_replicas.value is True
        assert not facts.replicas.available
        assert "pg_read_all_stats" in (facts.replicas.reason or "")
        assert facts.synchronous.value is True

    def test_replica_details_parse_lag_and_known_nulls(self) -> None:
        conn = conn_of(
            [
                (
                    "pg_stat_replication",
                    [
                        {
                            "name": "replica-1",
                            "client_addr": "10.0.0.5",
                            "state": "streaming",
                            "sync_state": "async",
                            "replay_lag_bytes": 1024,
                            "write_lag_ms": 12,
                            "flush_lag_ms": None,
                            "replay_lag_ms": 15,
                        },
                        {
                            "name": "",
                            "client_addr": None,
                            "state": None,
                            "sync_state": None,
                            "replay_lag_bytes": None,
                            "write_lag_ms": None,
                            "flush_lag_ms": None,
                            "replay_lag_ms": None,
                        },
                    ],
                )
            ]
        )
        replicas = _gather_replica_details(conn, LIMITS, server_facts())
        assert replicas.value is not None
        healthy, starting = replicas.value
        assert healthy.replay_lag_bytes.value == 1024
        assert healthy.write_lag_ms.value == 12
        # Caught-up standbys report NULL lag: a known state, not a failure.
        assert healthy.flush_lag_ms.available and healthy.flush_lag_ms.value is None
        assert not starting.replay_lag_bytes.available
        assert "not yet reported" in (starting.replay_lag_bytes.reason or "")
        assert not starting.state.available
        assert starting.client_addr.available and starting.client_addr.value is None

    def test_in_recovery_marks_byte_lag_not_applicable(self) -> None:
        conn = conn_of(
            [
                (
                    "pg_stat_replication",
                    [
                        {
                            "name": "cascade",
                            "client_addr": None,
                            "state": "streaming",
                            "sync_state": "async",
                            "replay_lag_bytes": None,
                            "write_lag_ms": None,
                            "flush_lag_ms": None,
                            "replay_lag_ms": None,
                        }
                    ],
                )
            ]
        )
        replicas = _gather_replica_details(conn, LIMITS, server_facts(in_recovery=True))
        assert replicas.value is not None
        [replica] = replicas.value
        assert not replica.replay_lag_bytes.available
        assert "in recovery" in (replica.replay_lag_bytes.reason or "")

    def test_details_query_failure_degrades(self) -> None:
        conn = conn_of([("pg_stat_replication", psycopg.errors.QueryCanceled())])
        replicas = _gather_replica_details(conn, LIMITS, server_facts())
        assert not replicas.available


class TestRemainingBranches:
    def test_generic_sqlstate_falls_through_with_code(self) -> None:
        reason = _describe_error(psycopg.errors.UndefinedTable(), LIMITS)
        assert reason.startswith("42P01: ")

    def test_view_has_no_statistics_row(self) -> None:
        conn = conn_of(
            [
                (
                    "to_regclass",
                    [
                        {
                            "oid": 16400,
                            "schema": "public",
                            "name": "v",
                            "relkind": "v",
                            "relpages": 0,
                            "reltuples": -1,
                        }
                    ],
                ),
                ("pg_stat_all_tables", []),
                ("pg_index", []),
                ("pg_attrdef", []),
                ("AND attisdropped", [{"n": 0}]),
                ("pg_get_constraintdef", []),
                ("pg_total_relation_size", [{"rel": 0, "total": 0}]),
            ]
        )
        facts = full_relation(conn, "public.v")
        assert not facts.last_analyze.available
        assert "no statistics row" in (facts.last_analyze.reason or "")
        assert "relkind v" in (facts.last_analyze.reason or "")

    def test_max_connections_failure_degrades(self) -> None:
        conn = conn_of(
            [
                ("client backend", [{"n": 3}]),
                ("max_connections", psycopg.errors.QueryCanceled()),
            ]
        )
        facts = _gather_concurrency(conn, {}, LIMITS, 170010, MONITOR_ROLE)
        assert facts.current_connections.value == 3
        assert not facts.max_connections.available

    def test_null_waitstart_on_pg14_is_marked(self) -> None:
        conn = conn_of(
            [
                (
                    "NOT l.granted",
                    [
                        {
                            "blocked_pid": 5,
                            "blocked_mode": "ShareLock",
                            "reloid": 1,
                            "waiting_for_ms": None,
                            "blocking_pids": [],
                        }
                    ],
                ),
                ("l.granted", []),
            ]
        )
        waiters = _gather_waiters(conn, {1: "public.t"}, LIMITS, 170010, None)
        assert waiters.value is not None
        [waiter] = waiters.value
        assert not waiter.waiting_for_ms.available
        assert "was null" in (waiter.waiting_for_ms.reason or "")
        assert waiter.blocking_modes == ()

    def test_replication_details_gathered_when_privileged(self) -> None:
        conn = conn_of(
            [
                ("pg_stat_replication", [{"n": 1}]),
                ("synchronous_standby_names", [{"names": "", "commit": "on"}]),
                (
                    "pg_stat_replication",
                    [
                        {
                            "name": "r1",
                            "client_addr": "10.0.0.9",
                            "state": "streaming",
                            "sync_state": "async",
                            "replay_lag_bytes": 0,
                            "write_lag_ms": None,
                            "flush_lag_ms": None,
                            "replay_lag_ms": None,
                        }
                    ],
                ),
            ]
        )
        facts = _gather_replication(conn, LIMITS, server_facts(), MONITOR_ROLE)
        assert facts.has_replicas.value is True
        assert facts.replicas.value is not None
        [replica] = facts.replicas.value
        assert replica.name == "r1"
        assert replica.replay_lag_bytes.value == 0
