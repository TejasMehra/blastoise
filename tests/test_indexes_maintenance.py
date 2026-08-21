"""Index DDL and maintenance commands on realistic migration snippets.

Every example is shaped like a real Alembic/Flyway/Rails/hand-written
migration: multi-line, commented, sometimes schema-qualified or quoted.
Assertions pin the exact classification — statement kind plus every
locking-relevant detail field (concurrency, uniqueness, partial/expression
form, IF [NOT] EXISTS, CASCADE, WITH NO DATA).
"""

from __future__ import annotations

import pytest
from helpers import only_statement, parse

from blastoise import (
    CreateTableAsDetails,
    DropDetails,
    IndexDetails,
    QualifiedName,
    ReindexDetails,
    StatementKind,
    TransactionGroup,
)

# ---------------------------------------------------------------------------
# CREATE INDEX
# ---------------------------------------------------------------------------


def test_alembic_style_create_index_on_orders() -> None:
    stmt = only_statement(
        """
        -- migration 0042: speed up the customer orders dashboard
        CREATE INDEX ix_orders_customer_id
            ON orders (customer_id, created_at DESC);
        """
    )
    assert stmt.kind is StatementKind.CREATE_INDEX
    assert stmt.node_tag == "IndexStmt"
    assert stmt.targets == (QualifiedName(name="orders"),)
    assert stmt.details == IndexDetails(
        index_name="ix_orders_customer_id",
        unique=False,
        method="btree",
        partial=False,
        if_not_exists=False,
    )


@pytest.mark.parametrize(
    ("sql", "kind", "unique"),
    [
        (
            "CREATE INDEX ix_users_email ON users (email)",
            StatementKind.CREATE_INDEX,
            False,
        ),
        (
            "CREATE INDEX CONCURRENTLY ix_users_email ON users (email)",
            StatementKind.CREATE_INDEX_CONCURRENTLY,
            False,
        ),
        (
            "CREATE UNIQUE INDEX uq_users_email ON users (email)",
            StatementKind.CREATE_INDEX,
            True,
        ),
        (
            "CREATE UNIQUE INDEX CONCURRENTLY uq_users_email ON users (email)",
            StatementKind.CREATE_INDEX_CONCURRENTLY,
            True,
        ),
    ],
)
def test_concurrency_is_the_kind_and_uniqueness_is_a_detail(
    sql: str, kind: StatementKind, unique: bool
) -> None:
    stmt = only_statement(sql)
    assert stmt.kind is kind
    assert stmt.targets == (QualifiedName(name="users"),)
    assert isinstance(stmt.details, IndexDetails)
    assert stmt.details.unique is unique
    assert stmt.details.method == "btree"
    assert stmt.details.partial is False


def test_partial_index_skipping_soft_deleted_invoices() -> None:
    stmt = only_statement(
        """
        -- V57__add_active_invoices_index.sql
        CREATE INDEX CONCURRENTLY ix_invoices_active
            ON billing.invoices (customer_id, issued_at DESC)
            WHERE deleted_at IS NULL;  -- partial: soft-deleted rows excluded
        """
    )
    assert stmt.kind is StatementKind.CREATE_INDEX_CONCURRENTLY
    assert stmt.targets == (QualifiedName(name="invoices", schema="billing"),)
    assert stmt.details == IndexDetails(
        index_name="ix_invoices_active",
        unique=False,
        method="btree",
        partial=True,
        if_not_exists=False,
    )


def test_expression_index_for_case_insensitive_login() -> None:
    stmt = only_statement(
        """
        CREATE UNIQUE INDEX uq_users_lower_email
            ON users (lower(email));
        """
    )
    assert stmt.kind is StatementKind.CREATE_INDEX
    assert stmt.targets == (QualifiedName(name="users"),)
    assert stmt.details == IndexDetails(
        index_name="uq_users_lower_email",
        unique=True,
        method="btree",
        partial=False,
        has_expression=True,
        if_not_exists=False,
    )


@pytest.mark.parametrize(
    ("sql", "method", "target"),
    [
        (
            "CREATE INDEX ix_audit_log_payload ON audit_log USING gin (payload jsonb_path_ops)",
            "gin",
            QualifiedName(name="audit_log"),
        ),
        (
            "CREATE INDEX ix_bookings_room_during ON bookings USING gist (room_id, during)",
            "gist",
            QualifiedName(name="bookings"),
        ),
        (
            "CREATE INDEX ix_sessions_token ON sessions USING hash (token)",
            "hash",
            QualifiedName(name="sessions"),
        ),
        (
            "CREATE INDEX ix_events_created_at ON analytics.events USING brin (created_at)",
            "brin",
            QualifiedName(name="events", schema="analytics"),
        ),
    ],
)
def test_non_btree_access_methods(sql: str, method: str, target: QualifiedName) -> None:
    stmt = only_statement(sql)
    assert stmt.kind is StatementKind.CREATE_INDEX
    assert stmt.targets == (target,)
    assert isinstance(stmt.details, IndexDetails)
    assert stmt.details.method == method
    assert stmt.details.unique is False


def test_covering_unique_index_if_not_exists() -> None:
    stmt = only_statement(
        """
        -- retryable: IF NOT EXISTS lets the deploy pipeline re-run this file
        CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS uq_invoices_number
            ON billing.invoices (invoice_number)
            INCLUDE (total_cents, currency);
        """
    )
    assert stmt.kind is StatementKind.CREATE_INDEX_CONCURRENTLY
    assert stmt.targets == (QualifiedName(name="invoices", schema="billing"),)
    assert stmt.details == IndexDetails(
        index_name="uq_invoices_number",
        unique=True,
        method="btree",
        partial=False,
        if_not_exists=True,
    )


def test_unnamed_index_lets_postgres_pick_the_name() -> None:
    stmt = only_statement("CREATE INDEX ON audit_log (created_at);")
    assert stmt.kind is StatementKind.CREATE_INDEX
    assert stmt.targets == (QualifiedName(name="audit_log"),)
    assert isinstance(stmt.details, IndexDetails)
    assert stmt.details.index_name is None
    assert stmt.details.unique is False


def test_rails_style_quoted_identifiers_are_preserved() -> None:
    stmt = only_statement(
        'CREATE UNIQUE INDEX "index_users_on_reset_password_token"'
        ' ON "users" ("reset_password_token");'
    )
    assert stmt.kind is StatementKind.CREATE_INDEX
    assert stmt.targets == (QualifiedName(name="users"),)
    assert isinstance(stmt.details, IndexDetails)
    assert stmt.details.index_name == "index_users_on_reset_password_token"
    assert stmt.details.unique is True


def test_quoted_mixed_case_identifiers_keep_their_case() -> None:
    stmt = only_statement('CREATE INDEX "IX_Orders_Status" ON public."Orders" ("Status");')
    assert stmt.kind is StatementKind.CREATE_INDEX
    assert stmt.targets == (QualifiedName(name="Orders", schema="public"),)
    assert isinstance(stmt.details, IndexDetails)
    assert stmt.details.index_name == "IX_Orders_Status"


# ---------------------------------------------------------------------------
# DROP INDEX
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sql", "kind", "names", "cascade", "missing_ok", "targets"),
    [
        (
            "DROP INDEX ix_orders_customer_id;",
            StatementKind.DROP_INDEX,
            ("ix_orders_customer_id",),
            False,
            False,
            (QualifiedName(name="ix_orders_customer_id"),),
        ),
        (
            "DROP INDEX IF EXISTS reporting.ix_daily_totals_day CASCADE;",
            StatementKind.DROP_INDEX,
            ("reporting.ix_daily_totals_day",),
            True,
            True,
            (QualifiedName(name="ix_daily_totals_day", schema="reporting"),),
        ),
        (
            "DROP INDEX CONCURRENTLY ix_users_legacy_email;",
            StatementKind.DROP_INDEX_CONCURRENTLY,
            ("ix_users_legacy_email",),
            False,
            False,
            (QualifiedName(name="ix_users_legacy_email"),),
        ),
        (
            "DROP INDEX CONCURRENTLY IF EXISTS ix_users_legacy_email;",
            StatementKind.DROP_INDEX_CONCURRENTLY,
            ("ix_users_legacy_email",),
            False,
            True,
            (QualifiedName(name="ix_users_legacy_email"),),
        ),
        (
            "DROP INDEX ix_orders_old_status, billing.ix_invoices_old_number;",
            StatementKind.DROP_INDEX,
            ("ix_orders_old_status", "billing.ix_invoices_old_number"),
            False,
            False,
            (
                QualifiedName(name="ix_orders_old_status"),
                QualifiedName(name="ix_invoices_old_number", schema="billing"),
            ),
        ),
    ],
)
def test_drop_index_variants(
    sql: str,
    kind: StatementKind,
    names: tuple[str, ...],
    cascade: bool,
    missing_ok: bool,
    targets: tuple[QualifiedName, ...],
) -> None:
    stmt = only_statement(sql)
    assert stmt.kind is kind
    assert stmt.node_tag == "DropStmt"
    assert stmt.targets == targets
    assert stmt.details == DropDetails(object_names=names, cascade=cascade, missing_ok=missing_ok)


# ---------------------------------------------------------------------------
# REINDEX / VACUUM / ANALYZE / CLUSTER
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sql", "kind", "target", "scope"),
    [
        (
            "REINDEX INDEX ix_users_email",
            StatementKind.REINDEX,
            QualifiedName(name="ix_users_email"),
            "index",
        ),
        (
            "REINDEX TABLE users",
            StatementKind.REINDEX,
            QualifiedName(name="users"),
            "table",
        ),
        (
            "REINDEX INDEX CONCURRENTLY billing.ix_invoices_active",
            StatementKind.REINDEX_CONCURRENTLY,
            QualifiedName(name="ix_invoices_active", schema="billing"),
            "index",
        ),
        (
            "REINDEX (VERBOSE) TABLE CONCURRENTLY public.orders",
            StatementKind.REINDEX_CONCURRENTLY,
            QualifiedName(name="orders", schema="public"),
            "table",
        ),
    ],
)
def test_reindex_forms(
    sql: str, kind: StatementKind, target: QualifiedName, scope: str
) -> None:
    stmt = only_statement(sql)
    assert stmt.kind is kind
    assert stmt.node_tag == "ReindexStmt"
    assert stmt.targets == (target,)
    assert stmt.details == ReindexDetails(scope=scope)


def test_reindex_schema_records_scope_and_name() -> None:
    stmt = only_statement("REINDEX SCHEMA reporting;")
    assert stmt.kind is StatementKind.REINDEX
    assert stmt.targets == ()
    assert stmt.details == ReindexDetails(scope="schema", object_name="reporting")


def test_reindex_concurrently_explicitly_disabled_is_plain_reindex() -> None:
    for option in ("FALSE", "OFF", "0"):
        stmt = only_statement(f"REINDEX (CONCURRENTLY {option}) TABLE orders;")
        assert stmt.kind is StatementKind.REINDEX, option
    stmt = only_statement("REINDEX (CONCURRENTLY ON) TABLE orders;")
    assert stmt.kind is StatementKind.REINDEX_CONCURRENTLY


@pytest.mark.parametrize(
    ("sql", "kind"),
    [
        ("VACUUM users", StatementKind.VACUUM),
        ("VACUUM FULL users", StatementKind.VACUUM_FULL),
        ("VACUUM (FULL, ANALYZE) users", StatementKind.VACUUM_FULL),
        ("VACUUM (ANALYZE, VERBOSE) users", StatementKind.VACUUM),
        ("VACUUM (FULL FALSE) users", StatementKind.VACUUM),
        ("VACUUM (FULL OFF) users", StatementKind.VACUUM),
        ("VACUUM (FULL TRUE) users", StatementKind.VACUUM_FULL),
        ("ANALYZE users", StatementKind.ANALYZE),
        ("ANALYZE users (email, created_at)", StatementKind.ANALYZE),
    ],
)
def test_vacuum_and_analyze_forms(sql: str, kind: StatementKind) -> None:
    stmt = only_statement(sql)
    assert stmt.kind is kind
    assert stmt.node_tag == "VacuumStmt"
    assert stmt.targets == (QualifiedName(name="users"),)
    assert stmt.details is None


def test_multi_table_vacuum_targets_every_relation() -> None:
    stmt = only_statement(
        """
        -- weekly maintenance window
        VACUUM ANALYZE users, orders, audit_log;
        """
    )
    assert stmt.kind is StatementKind.VACUUM
    assert stmt.targets == (
        QualifiedName(name="users"),
        QualifiedName(name="orders"),
        QualifiedName(name="audit_log"),
    )


def test_cluster_with_and_without_using_index() -> None:
    with_index = only_statement("CLUSTER orders USING ix_orders_created_at;")
    assert with_index.kind is StatementKind.CLUSTER
    assert with_index.node_tag == "ClusterStmt"
    assert with_index.targets == (QualifiedName(name="orders"),)
    assert with_index.details is None

    bare = only_statement("CLUSTER;")  # recluster everything previously clustered
    assert bare.kind is StatementKind.CLUSTER
    assert bare.targets == ()


# ---------------------------------------------------------------------------
# REFRESH MATERIALIZED VIEW
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sql", "kind", "no_data"),
    [
        (
            "REFRESH MATERIALIZED VIEW reporting.daily_totals",
            StatementKind.REFRESH_MATVIEW,
            False,
        ),
        (
            "REFRESH MATERIALIZED VIEW CONCURRENTLY reporting.daily_totals",
            StatementKind.REFRESH_MATVIEW_CONCURRENTLY,
            False,
        ),
        (
            "REFRESH MATERIALIZED VIEW reporting.daily_totals WITH NO DATA",
            StatementKind.REFRESH_MATVIEW,
            True,
        ),
    ],
)
def test_refresh_materialized_view_forms(sql: str, kind: StatementKind, no_data: bool) -> None:
    stmt = only_statement(sql)
    assert stmt.kind is kind
    assert stmt.node_tag == "RefreshMatViewStmt"
    assert stmt.targets == (QualifiedName(name="daily_totals", schema="reporting"),)
    assert stmt.details == CreateTableAsDetails(no_data=no_data)


# ---------------------------------------------------------------------------
# Whole-file migrations
# ---------------------------------------------------------------------------


def test_zero_downtime_index_swap_migration() -> None:
    script = parse(
        """
        -- V61__swap_orders_status_index.sql
        -- Build the replacement first; CONCURRENTLY must run outside a
        -- transaction, so this file has no BEGIN/COMMIT.
        CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_orders_status_v2
            ON public.orders (status)
            WHERE status <> 'archived';

        DROP INDEX CONCURRENTLY IF EXISTS public.ix_orders_status;

        ANALYZE public.orders;
        """
    )
    assert [s.kind for s in script.statements] == [
        StatementKind.CREATE_INDEX_CONCURRENTLY,
        StatementKind.DROP_INDEX_CONCURRENTLY,
        StatementKind.ANALYZE,
    ]
    assert not script.has_explicit_transactions
    assert script.transaction_groups == (
        TransactionGroup(explicit=False, statement_indices=(0, 1, 2)),
    )

    create, drop, analyze = script.statements
    assert create.details == IndexDetails(
        index_name="ix_orders_status_v2",
        unique=False,
        method="btree",
        partial=True,
        if_not_exists=True,
    )
    assert drop.details == DropDetails(
        object_names=("public.ix_orders_status",), cascade=False, missing_ok=True
    )
    assert drop.targets == (QualifiedName(name="ix_orders_status", schema="public"),)
    assert analyze.targets == (QualifiedName(name="orders", schema="public"),)


def test_maintenance_window_script_with_explicit_transaction() -> None:
    script = parse(
        """
        BEGIN;
        DROP INDEX IF EXISTS ix_audit_log_old;  -- superseded by the brin index
        COMMIT;

        -- VACUUM cannot run inside a transaction block; single-relation
        -- REINDEX can (only CONCURRENTLY and SCHEMA/DATABASE/SYSTEM cannot).
        VACUUM FULL audit_log;
        REINDEX TABLE audit_log;
        """
    )
    assert [s.kind for s in script.statements] == [
        StatementKind.BEGIN,
        StatementKind.DROP_INDEX,
        StatementKind.COMMIT,
        StatementKind.VACUUM_FULL,
        StatementKind.REINDEX,
    ]
    assert script.has_explicit_transactions
    explicit, implicit = script.transaction_groups
    assert explicit.explicit is True
    assert explicit.statement_indices == (1,)
    assert explicit.opened_by == 0
    assert explicit.closed_by == 2
    assert explicit.rolled_back is False
    assert implicit.explicit is False
    assert implicit.statement_indices == (3, 4)

    drop = script.statements[1]
    assert drop.details == DropDetails(
        object_names=("ix_audit_log_old",), cascade=False, missing_ok=True
    )


def test_statement_sql_is_verbatim_without_semicolon() -> None:
    stmt = only_statement("DROP INDEX CONCURRENTLY ix_users_tmp;\n")
    assert stmt.sql == "DROP INDEX CONCURRENTLY ix_users_tmp"
    assert stmt.span.line == 1
