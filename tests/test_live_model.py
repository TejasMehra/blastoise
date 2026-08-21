"""Snapshot model tests: serialization determinism, Fact semantics, redaction.

These run everywhere — no database needed.
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from pgverdict.live import (
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
    TypeFacts,
    redact_conninfo,
)
from pgverdict.live.introspect import _regclass_text
from pgverdict.live.model import _normalize


def make_snapshot() -> LiveSnapshot:
    return LiveSnapshot(
        snapshot_format=SNAPSHOT_FORMAT,
        captured_at="2026-08-20T12:00:00+00:00",
        target=ConnectionTarget(host="db.example", port="5432", dbname="app", user="ro"),
        limits=CaptureLimits(
            connect_timeout_s=10,
            statement_timeout_ms=5000,
            lock_timeout_ms=2000,
            long_transaction_threshold_ms=60_000,
            max_listed_transactions=100,
        ),
        role=RoleFacts(
            role="ro",
            transaction_read_only=True,
            superuser=False,
            can_read_all_stats=True,
            warnings=("role can CREATE schemas in this database",),
        ),
        server=ServerFacts(
            server_version_num=Fact.of(170010),
            server_version=Fact.of("17.10"),
            pg_major=Fact.of(17),
            in_recovery=Fact.of(False),
            server_now=Fact.of("2026-08-20T12:00:00.123456+00:00"),
            lock_timeout_ms=Fact.of(0),
            statement_timeout_ms=Fact.of(0),
            timezone=Fact.of("UTC"),
        ),
        relations=(
            RelationFacts(
                requested="public.users",
                exists=Fact.of(True),
                schema=Fact.of("public"),
                name=Fact.of("users"),
                relkind=Fact.of("r"),
                is_partitioned=Fact.of(False),
                reltuples=Fact.of(40_000_000),
                relpages=Fact.of(500_000),
                relation_size_bytes=Fact.of(4_096_000_000),
                total_relation_size_bytes=Fact.of(6_000_000_000),
                last_analyze=Fact.of(None),
                last_autoanalyze=Fact.of("2026-08-20T11:00:00+00:00"),
                n_mod_since_analyze=Fact.of(1234),
                partition_count=Fact.of(0),
                partitions_total_size_bytes=Fact.of(0),
                index_count=Fact.of(1),
                indexes=Fact.of(
                    (IndexFacts(name="users_pkey", valid=True, size_bytes=Fact.of(900_000_000)),)
                ),
                invalid_indexes=Fact.of(()),
                columns=Fact.of(
                    (
                        ColumnFacts(
                            name="id",
                            attnum=1,
                            data_type="bigint",
                            type_oid=20,
                            typmod=-1,
                            not_null=True,
                            has_default=True,
                            default_expression="nextval('users_id_seq'::regclass)",
                            identity="",
                            generated="",
                            is_domain=False,
                            domain_base_type=None,
                            domain_not_null=False,
                            domain_constraint_count=0,
                        ),
                        ColumnFacts(
                            name="email",
                            attnum=3,
                            data_type="email_address",
                            type_oid=90001,
                            typmod=-1,
                            not_null=False,
                            has_default=False,
                            default_expression=None,
                            identity="",
                            generated="",
                            is_domain=True,
                            domain_base_type="text",
                            domain_not_null=False,
                            domain_constraint_count=1,
                        ),
                    )
                ),
                dropped_column_count=Fact.of(1),
                constraints=Fact.of(
                    (
                        ConstraintFacts(
                            name="users_email_check",
                            contype="c",
                            validated=False,
                            columns=("email",),
                            definition="CHECK ((email IS NOT NULL)) NOT VALID",
                            check_expression="(email IS NOT NULL)",
                            referenced_table=None,
                        ),
                        ConstraintFacts(
                            name="users_org_fkey",
                            contype="f",
                            validated=True,
                            columns=("org_id",),
                            definition="FOREIGN KEY (org_id) REFERENCES orgs(id)",
                            check_expression=None,
                            referenced_table="public.orgs",
                        ),
                    )
                ),
            ),
        ),
        functions=(
            FunctionFacts(
                requested="my_schema.gen_id",
                exists=Fact.of(True),
                overloads=Fact.of(1),
                volatility=Fact.of("v"),
                prokind=Fact.of("f"),
            ),
        ),
        types=(
            TypeFacts(
                requested="email_address",
                exists=Fact.of(True),
                formatted=Fact.of("email_address"),
                typtype=Fact.of("d"),
                is_domain=Fact.of(True),
                domain_base_type=Fact.of("text"),
                domain_not_null=Fact.of(False),
                domain_constraint_count=Fact.of(1),
            ),
        ),
        type_changes=(
            TypeChangeFacts(
                relation="public.users",
                column="body",
                new_type_requested="varchar(200)",
                current_type=Fact.of("character varying(100)"),
                current_typmod=Fact.of(104),
                current_base_type=Fact.of("character varying"),
                current_is_domain=Fact.of(False),
                current_domain_not_null=Fact.of(False),
                current_domain_constraint_count=Fact.of(0),
                new_type=Fact.of("character varying"),
                new_base_type=Fact.of("character varying"),
                new_is_domain=Fact.of(False),
                new_domain_not_null=Fact.of(False),
                new_domain_constraint_count=Fact.of(0),
                new_domain_has_typmod=Fact.of(False),
                same_type=Fact.of(True),
                bases_same=Fact.of(True),
                cast_method=Fact.of(None),
                cast_context=Fact.of(None),
            ),
        ),
        concurrency=ConcurrencyFacts(
            lock_waiters=Fact.of(
                (
                    LockWaiter(
                        relation="public.users",
                        blocked_pid=101,
                        blocked_mode="AccessExclusiveLock",
                        waiting_for_ms=Fact.of(1500),
                        blocking_pids=(99,),
                        blocking_modes=("AccessShareLock",),
                    ),
                )
            ),
            long_transactions=Fact.of(
                (
                    LongTransaction(
                        pid=99,
                        state="idle in transaction",
                        idle_in_transaction=True,
                        xact_age_ms=600_000,
                        query_age_ms=Fact.of(590_000),
                        first_keyword=Fact.of("select"),
                        waiting_on_lock=False,
                    ),
                )
            ),
            current_connections=Fact.of(42),
            max_connections=Fact.of(100),
        ),
        replication=ReplicationFacts(
            has_replicas=Fact.of(True),
            replicas=Fact.of(
                (
                    ReplicaFacts(
                        name="replica-1",
                        client_addr=Fact.of("10.0.0.5"),
                        state=Fact.of("streaming"),
                        sync_state=Fact.of("async"),
                        replay_lag_bytes=Fact.of(1024),
                        write_lag_ms=Fact.of(12),
                        flush_lag_ms=Fact.of(None),
                        replay_lag_ms=Fact.of(15),
                    ),
                )
            ),
            synchronous=Fact.of(False),
            synchronous_standby_names=Fact.of(""),
            synchronous_commit=Fact.of("on"),
        ),
    )


class TestDecideDefaultVolatility:
    def test_already_decided_defaults_pass_through(self) -> None:
        from pgverdict.ir import DefaultInfo, Volatility
        from pgverdict.live import decide_default_volatility

        decided = DefaultInfo(volatility=Volatility.VOLATILE, expression="f()")
        assert (
            decide_default_volatility(decided, make_snapshot()) is Volatility.VOLATILE
        )

    def test_unknown_resolves_via_snapshot_functions(self) -> None:
        from pgverdict.ir import DefaultInfo, Volatility
        from pgverdict.live import decide_default_volatility

        # make_snapshot carries my_schema.gen_id with provolatile 'v'.
        unknown = DefaultInfo(
            volatility=Volatility.UNKNOWN,
            expression="my_schema.gen_id(7)",
            unknown_functions=("my_schema.gen_id",),
        )
        assert (
            decide_default_volatility(unknown, make_snapshot()) is Volatility.VOLATILE
        )

    def test_unknown_without_matching_facts_stays_unknown(self) -> None:
        from pgverdict.ir import DefaultInfo, Volatility
        from pgverdict.live import decide_default_volatility

        unknown = DefaultInfo(
            volatility=Volatility.UNKNOWN,
            expression="other_fn()",
            unknown_functions=("other_fn",),
        )
        assert (
            decide_default_volatility(unknown, make_snapshot()) is Volatility.UNKNOWN
        )

    def test_undecided_function_facts_are_not_in_the_mapping(self) -> None:
        from pgverdict.live import FunctionFacts, resolved_function_volatilities

        snapshot = dataclasses.replace(
            make_snapshot(),
            functions=(
                FunctionFacts(
                    requested="ambiguous_fn",
                    exists=Fact.of(True),
                    overloads=Fact.of(2),
                    volatility=Fact.unavailable("2 overloads disagree"),
                    prokind=Fact.of("f"),
                ),
            ),
        )
        assert resolved_function_volatilities(snapshot) == {}


class TestFact:
    def test_of_none_is_a_known_null_not_an_absence(self) -> None:
        fact: Fact[str | None] = Fact.of(None)
        assert fact.available
        assert fact.value is None
        assert fact.reason is None

    def test_unavailable_carries_the_reason(self) -> None:
        fact: Fact[int] = Fact.unavailable("relation does not exist")
        assert not fact.available
        assert fact.value is None
        assert fact.reason == "relation does not exist"

    def test_known_false_and_unknown_serialize_differently(self) -> None:
        known = dataclasses.asdict(Fact.of(False))
        unknown = dataclasses.asdict(Fact[bool].unavailable("no privilege"))
        assert known != unknown
        assert known["available"] and known["value"] is False
        assert not unknown["available"] and unknown["value"] is None


class TestSerialization:
    def test_canonical_json_is_stable(self) -> None:
        a = make_snapshot().to_canonical_json()
        b = make_snapshot().to_canonical_json()
        assert a == b

    def test_canonical_json_has_sorted_keys_everywhere(self) -> None:
        def check(value: object) -> None:
            if isinstance(value, dict):
                assert list(value.keys()) == sorted(value.keys())
                for item in value.values():
                    check(item)
            elif isinstance(value, list):
                for item in value:
                    check(item)

        check(json.loads(make_snapshot().to_canonical_json()))

    def test_canonical_json_round_trips(self) -> None:
        snapshot = make_snapshot()
        assert json.loads(snapshot.to_canonical_json()) == snapshot.to_json_value()

    def test_no_floats_anywhere(self) -> None:
        def check(value: object) -> None:
            assert not isinstance(value, float), value
            if isinstance(value, dict):
                for item in value.values():
                    check(item)
            if isinstance(value, list):
                for item in value:
                    check(item)

        check(make_snapshot().to_json_value())

    def test_floats_are_rejected_loudly(self) -> None:
        with pytest.raises(TypeError, match="floats are banned"):
            _normalize({"reltuples": 1.5})

    def test_non_string_keys_are_rejected(self) -> None:
        with pytest.raises(TypeError, match="non-string dict key"):
            _normalize({1: "x"})

    def test_unserializable_values_are_rejected(self) -> None:
        with pytest.raises(TypeError, match="unserializable"):
            _normalize({"x": object()})


class TestRedaction:
    def test_url_password_is_dropped(self) -> None:
        target = redact_conninfo("postgresql://ro:s3cret@db.example:5433/app?sslmode=require")
        assert target.host == "db.example"
        assert target.port == "5433"
        assert target.dbname == "app"
        assert target.user == "ro"
        assert "s3cret" not in repr(target)

    def test_keyword_password_is_dropped(self) -> None:
        target = redact_conninfo("host=h dbname=d user=u password=hunter2")
        assert target.dbname == "d"
        assert "hunter2" not in repr(target)

    def test_redacted_target_serializes_without_the_password(self) -> None:
        snapshot = make_snapshot()
        replaced = dataclasses.replace(
            snapshot,
            target=redact_conninfo("postgresql://ro:supersecret@db.example/app"),
        )
        assert "supersecret" not in replaced.to_canonical_json()


class TestRegclassText:
    def test_plain_string_passes_verbatim(self) -> None:
        assert _regclass_text("public.users") == ("public.users", "public.users")

    def test_qualified_name_is_quoted_to_preserve_case(self) -> None:
        from pgverdict.ir import QualifiedName

        requested, lookup = _regclass_text(QualifiedName(name="MyTable", schema="app"))
        assert requested == "app.MyTable"
        assert lookup == '"app"."MyTable"'

    def test_embedded_quotes_are_doubled(self) -> None:
        from pgverdict.ir import QualifiedName

        _, lookup = _regclass_text(QualifiedName(name='we"ird'))
        assert lookup == '"we""ird"'


class TestNormalizeExtras:
    def test_str_enums_flatten_to_plain_strings(self) -> None:
        from pgverdict.catalog.model import LockMode

        assert _normalize({"mode": LockMode.ACCESS_EXCLUSIVE}) == {"mode": "ACCESS EXCLUSIVE"}


class TestConnectionErrors:
    def test_invalid_conninfo_raises_cleanly(self) -> None:
        from pgverdict.live import LiveIntrospectionError

        with pytest.raises(LiveIntrospectionError, match="invalid connection string"):
            redact_conninfo("=this is not a conninfo=")

    def test_unreachable_server_raises_without_leaking_credentials(self) -> None:
        from pgverdict.live import LiveIntrospectionError, capture_snapshot

        with pytest.raises(LiveIntrospectionError, match="could not connect") as info:
            capture_snapshot(
                "postgresql://u:secretpw@127.0.0.1:9/db", [], connect_timeout_s=2
            )
        assert "secretpw" not in str(info.value)
