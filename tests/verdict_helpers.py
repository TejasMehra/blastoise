"""Builders for hand-made snapshots the verdict tests feed the engine.

Every builder fills a complete, valid ``LiveSnapshot`` so tests only state
what matters to them (a table's row count, a proving constraint, a lock
waiter) and everything else is a healthy default.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from blastoise.live.model import (
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
    ReplicationFacts,
    RoleFacts,
    ServerFacts,
    TypeChangeFacts,
    TypeFacts,
)

NOW = "2026-08-21T12:00:00+00:00"


def _iso_hours_ago(hours: int) -> str:
    stamp = datetime.fromisoformat(NOW).astimezone(UTC) - timedelta(hours=hours)
    return stamp.isoformat()


def column(
    name: str,
    *,
    data_type: str = "text",
    not_null: bool = False,
    generated: str = "",
) -> ColumnFacts:
    return ColumnFacts(
        name=name,
        attnum=1,
        data_type=data_type,
        type_oid=25,
        typmod=-1,
        not_null=not_null,
        has_default=False,
        default_expression=None,
        identity="",
        generated=generated,
        is_domain=False,
        domain_base_type=None,
        domain_not_null=False,
        domain_constraint_count=0,
    )


def check_constraint(
    name: str, expression: str, *, validated: bool = True, columns: tuple[str, ...] = ()
) -> ConstraintFacts:
    return ConstraintFacts(
        name=name,
        contype="c",
        validated=validated,
        columns=columns,
        definition=f"CHECK ({expression})",
        check_expression=expression,
        referenced_table=None,
    )


def fk_constraint(
    name: str, referenced: str, *, validated: bool = True
) -> ConstraintFacts:
    return ConstraintFacts(
        name=name,
        contype="f",
        validated=validated,
        columns=("ref_id",),
        definition=f"FOREIGN KEY (ref_id) REFERENCES {referenced}(id)",
        check_expression=None,
        referenced_table=referenced,
    )


def index_facts(
    name: str,
    *,
    depends_on: tuple[str, ...] = (),
    method: str = "btree",
    partial: bool = False,
    has_expressions: bool = False,
    default_opclasses: bool = True,
    size_bytes: int = 8192,
    valid: bool = True,
) -> IndexFacts:
    return IndexFacts(
        name=name,
        valid=valid,
        size_bytes=Fact.of(size_bytes),
        method=method,
        partial=partial,
        has_expressions=has_expressions,
        default_opclasses=default_opclasses,
        depends_on_columns=depends_on,
    )


def relation(
    requested: str,
    *,
    rows: int | None = 1000,
    exists: bool = True,
    analyzed_hours_ago: int | None = 1,
    n_mod: int = 0,
    size_bytes: int = 8192,
    columns_facts: tuple[ColumnFacts, ...] = (),
    constraints: tuple[ConstraintFacts, ...] = (),
    indexes: tuple[IndexFacts, ...] = (),
    indexes_unavailable: str | None = None,
    invalid_indexes: tuple[str, ...] = (),
    schema: str = "public",
) -> RelationFacts:
    if not exists:
        missing: Fact[Any] = Fact.unavailable("relation does not exist")
        return RelationFacts(
            requested=requested,
            exists=Fact.of(False),
            schema=missing,
            name=missing,
            relkind=missing,
            is_partitioned=missing,
            reltuples=missing,
            relpages=missing,
            relation_size_bytes=missing,
            total_relation_size_bytes=missing,
            last_analyze=missing,
            last_autoanalyze=missing,
            n_mod_since_analyze=missing,
            partition_count=missing,
            partitions_total_size_bytes=missing,
            index_count=missing,
            indexes=missing,
            invalid_indexes=missing,
            columns=missing,
            dropped_column_count=missing,
            constraints=missing,
        )
    name = requested.split(".")[-1]
    analyze_fact: Fact[str | None] = (
        Fact.of(None)
        if analyzed_hours_ago is None
        else Fact.of(_iso_hours_ago(analyzed_hours_ago))
    )
    return RelationFacts(
        requested=requested,
        exists=Fact.of(True),
        schema=Fact.of(schema),
        name=Fact.of(name),
        relkind=Fact.of("r"),
        is_partitioned=Fact.of(False),
        reltuples=Fact.of(rows) if rows is not None else Fact.unavailable("no estimate"),
        relpages=Fact.of(max(1, (rows or 0) // 100)),
        relation_size_bytes=Fact.of(size_bytes),
        total_relation_size_bytes=Fact.of(size_bytes * 2),
        last_analyze=Fact.of(None),
        last_autoanalyze=analyze_fact,
        n_mod_since_analyze=Fact.of(n_mod),
        partition_count=Fact.of(0),
        partitions_total_size_bytes=Fact.of(0),
        index_count=(
            Fact.unavailable(indexes_unavailable)
            if indexes_unavailable is not None
            else Fact.of(len(indexes))
        ),
        indexes=(
            Fact.unavailable(indexes_unavailable)
            if indexes_unavailable is not None
            else Fact.of(indexes)
        ),
        invalid_indexes=Fact.of(invalid_indexes),
        columns=Fact.of(columns_facts),
        dropped_column_count=Fact.of(0),
        constraints=Fact.of(constraints),
    )


def function_fact(requested: str, volatility: str) -> FunctionFacts:
    return FunctionFacts(
        requested=requested,
        exists=Fact.of(True),
        overloads=Fact.of(1),
        volatility=Fact.of(volatility),
        prokind=Fact.of("f"),
    )


def type_fact(
    requested: str,
    *,
    is_domain: bool = False,
    domain_constraints: int = 0,
    domain_not_null: bool = False,
) -> TypeFacts:
    return TypeFacts(
        requested=requested,
        exists=Fact.of(True),
        formatted=Fact.of(requested),
        typtype=Fact.of("d" if is_domain else "b"),
        is_domain=Fact.of(is_domain),
        domain_base_type=Fact.of("text" if is_domain else None),
        domain_not_null=Fact.of(domain_not_null),
        domain_constraint_count=Fact.of(domain_constraints),
    )


def type_change(
    rel: str,
    col: str,
    new_type: str,
    *,
    current_type: str = "integer",
    same_base: bool = False,
    cast_method: str | None = "f",
) -> TypeChangeFacts:
    return TypeChangeFacts(
        relation=rel,
        column=col,
        new_type_requested=new_type,
        current_type=Fact.of(current_type),
        current_typmod=Fact.of(-1),
        current_base_type=Fact.of(current_type),
        current_is_domain=Fact.of(False),
        current_domain_not_null=Fact.of(False),
        current_domain_constraint_count=Fact.of(0),
        new_type=Fact.of(new_type),
        new_base_type=Fact.of(new_type),
        new_is_domain=Fact.of(False),
        new_domain_not_null=Fact.of(False),
        new_domain_constraint_count=Fact.of(0),
        new_domain_has_typmod=Fact.of(False),
        same_type=Fact.of(False),
        bases_same=Fact.of(same_base),
        cast_method=Fact.of(cast_method),
        cast_context=Fact.of("i"),
    )


def waiter(
    rel: str, *, blocking_modes: tuple[str, ...] = ("AccessExclusiveLock",)
) -> LockWaiter:
    return LockWaiter(
        relation=rel,
        blocked_pid=4242,
        blocked_mode="AccessShareLock",
        waiting_for_ms=Fact.of(1500),
        blocking_pids=(4100,),
        blocking_modes=blocking_modes,
    )


def snapshot(
    *,
    relations: tuple[RelationFacts, ...] = (),
    functions: tuple[FunctionFacts, ...] = (),
    types: tuple[TypeFacts, ...] = (),
    type_changes: tuple[TypeChangeFacts, ...] = (),
    waiters: tuple[LockWaiter, ...] = (),
    long_transactions: tuple[LongTransaction, ...] = (),
    waiters_unavailable: str | None = None,
) -> LiveSnapshot:
    waiters_fact: Fact[tuple[LockWaiter, ...]] = (
        Fact.unavailable(waiters_unavailable)
        if waiters_unavailable is not None
        else Fact.of(waiters)
    )
    return LiveSnapshot(
        snapshot_format=SNAPSHOT_FORMAT,
        captured_at=NOW,
        target=ConnectionTarget(host="db", port="5432", dbname="app", user="ro"),
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
            warnings=(),
        ),
        server=ServerFacts(
            server_version_num=Fact.of(170010),
            server_version=Fact.of("17.10"),
            pg_major=Fact.of(17),
            in_recovery=Fact.of(False),
            server_now=Fact.of(NOW),
            lock_timeout_ms=Fact.of(0),
            statement_timeout_ms=Fact.of(0),
            timezone=Fact.of("UTC"),
        ),
        relations=relations,
        functions=functions,
        types=types,
        type_changes=type_changes,
        concurrency=ConcurrencyFacts(
            lock_waiters=waiters_fact,
            long_transactions=Fact.of(long_transactions),
            current_connections=Fact.of(10),
            max_connections=Fact.of(100),
        ),
        replication=ReplicationFacts(
            has_replicas=Fact.of(False),
            replicas=Fact.of(()),
            synchronous=Fact.of(False),
            synchronous_standby_names=Fact.of(""),
            synchronous_commit=Fact.of("on"),
        ),
    )
