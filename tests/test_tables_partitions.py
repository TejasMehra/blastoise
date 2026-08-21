"""Table lifecycle and whole-table changes, exercised on realistic migrations."""

from __future__ import annotations

import pytest
from helpers import only_action, only_statement, parse

from pgverdict import (
    AlterTableActionKind,
    CreateTableAsDetails,
    CreateTableDetails,
    DropDetails,
    QualifiedName,
    RenameDetails,
    StatementKind,
    TruncateDetails,
)

# ---------------------------------------------------------------------------
# CREATE TABLE
# ---------------------------------------------------------------------------


def test_create_table_plain_alembic_style() -> None:
    statement = only_statement(
        """
        -- migration 0001: initial users table
        CREATE TABLE users (
            id bigserial PRIMARY KEY,
            email text NOT NULL,
            full_name text,
            created_at timestamptz NOT NULL DEFAULT now()
        );
        """
    )
    assert statement.kind is StatementKind.CREATE_TABLE
    assert statement.node_tag == "CreateStmt"
    assert statement.targets == (QualifiedName("users"),)
    assert statement.details == CreateTableDetails(persistence="permanent")
    # stmt_location points at the statement itself, past the leading comment.
    assert statement.span.line == 3
    assert statement.sql.startswith("CREATE TABLE users")
    assert not statement.sql.endswith(";")


def test_create_table_if_not_exists_schema_qualified() -> None:
    statement = only_statement(
        """
        CREATE TABLE IF NOT EXISTS billing.invoices (
            id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            customer_id bigint NOT NULL,
            total_cents integer NOT NULL,
            issued_on date NOT NULL
        );
        """
    )
    assert statement.kind is StatementKind.CREATE_TABLE
    assert statement.targets == (QualifiedName("invoices", schema="billing"),)
    assert statement.details == CreateTableDetails(persistence="permanent", if_not_exists=True)


@pytest.mark.parametrize(
    ("sql", "persistence", "target"),
    [
        (
            "CREATE UNLOGGED TABLE staging_events (payload jsonb NOT NULL);",
            "unlogged",
            QualifiedName("staging_events"),
        ),
        (
            "CREATE TEMPORARY TABLE tmp_user_ids (user_id bigint) ON COMMIT DROP;",
            "temporary",
            QualifiedName("tmp_user_ids"),
        ),
        (
            "CREATE TEMP TABLE tmp_order_ids (order_id bigint);",
            "temporary",
            QualifiedName("tmp_order_ids"),
        ),
    ],
)
def test_create_table_persistence(sql: str, persistence: str, target: QualifiedName) -> None:
    statement = only_statement(sql)
    assert statement.kind is StatementKind.CREATE_TABLE
    assert statement.targets == (target,)
    assert statement.details == CreateTableDetails(persistence=persistence)


@pytest.mark.parametrize(
    ("sql", "target", "parent"),
    [
        (
            """
            CREATE TABLE events_p2026_02
                PARTITION OF app.events
                FOR VALUES FROM ('2026-02-01') TO ('2026-03-01');
            """,
            QualifiedName("events_p2026_02"),
            QualifiedName("events", schema="app"),
        ),
        (
            "CREATE TABLE app.events_default PARTITION OF app.events DEFAULT;",
            QualifiedName("events_default", schema="app"),
            QualifiedName("events", schema="app"),
        ),
    ],
)
def test_create_table_partition_of(sql: str, target: QualifiedName, parent: QualifiedName) -> None:
    statement = only_statement(sql)
    assert statement.kind is StatementKind.CREATE_TABLE_PARTITION_OF
    assert statement.node_tag == "CreateStmt"
    assert statement.targets == (target,)
    assert statement.details == CreateTableDetails(persistence="permanent", partition_of=parent)


def test_create_table_inherits() -> None:
    statement = only_statement(
        "CREATE TABLE audit_log_2023 () INHERITS (public.audit_log);"
    )
    assert statement.kind is StatementKind.CREATE_TABLE
    assert statement.targets == (QualifiedName("audit_log_2023"),)
    assert statement.details == CreateTableDetails(persistence="permanent", inherits=True)


def test_create_table_as_select() -> None:
    statement = only_statement(
        """
        CREATE TABLE reporting.user_signup_counts AS
        SELECT date_trunc('day', created_at) AS day, count(*) AS signups
        FROM users
        GROUP BY 1;
        """
    )
    assert statement.kind is StatementKind.CREATE_TABLE_AS
    assert statement.node_tag == "CreateTableAsStmt"
    assert statement.targets == (QualifiedName("user_signup_counts", schema="reporting"),)
    assert statement.details == CreateTableAsDetails(no_data=False)


def test_create_table_as_select_with_no_data() -> None:
    statement = only_statement(
        """
        CREATE TABLE reporting.orders_snapshot AS
        SELECT * FROM orders
        WITH NO DATA;
        """
    )
    assert statement.kind is StatementKind.CREATE_TABLE_AS
    assert statement.details == CreateTableAsDetails(no_data=True)


def test_select_into() -> None:
    statement = only_statement(
        """
        SELECT id, email
        INTO archived_users
        FROM users
        WHERE deleted_at IS NOT NULL;
        """
    )
    assert statement.kind is StatementKind.SELECT_INTO
    assert statement.node_tag == "SelectStmt"
    assert statement.targets == (QualifiedName("archived_users"),)


# ---------------------------------------------------------------------------
# DROP TABLE / TRUNCATE
# ---------------------------------------------------------------------------


def test_drop_table_single() -> None:
    statement = only_statement("DROP TABLE order_items;")
    assert statement.kind is StatementKind.DROP_TABLE
    assert statement.node_tag == "DropStmt"
    assert statement.targets == (QualifiedName("order_items"),)
    assert statement.details == DropDetails(object_names=("order_items",))


def test_drop_table_multiple_if_exists_cascade() -> None:
    statement = only_statement(
        "DROP TABLE IF EXISTS billing.invoices, billing.invoice_lines CASCADE;"
    )
    assert statement.kind is StatementKind.DROP_TABLE
    assert statement.targets == (
        QualifiedName("invoices", schema="billing"),
        QualifiedName("invoice_lines", schema="billing"),
    )
    assert statement.details == DropDetails(
        object_names=("billing.invoices", "billing.invoice_lines"),
        cascade=True,
        missing_ok=True,
    )


def test_drop_table_quoted_identifiers_preserved() -> None:
    statement = only_statement('DROP TABLE "Billing"."InvoiceLines", legacy_orders;')
    assert statement.kind is StatementKind.DROP_TABLE
    assert statement.targets == (
        QualifiedName("InvoiceLines", schema="Billing"),
        QualifiedName("legacy_orders"),
    )
    assert statement.details == DropDetails(
        object_names=("Billing.InvoiceLines", "legacy_orders")
    )


def test_truncate_single_table() -> None:
    statement = only_statement("TRUNCATE audit_log;")
    assert statement.kind is StatementKind.TRUNCATE
    assert statement.node_tag == "TruncateStmt"
    assert statement.targets == (QualifiedName("audit_log"),)
    assert statement.details == TruncateDetails(cascade=False, restart_identity=False)


def test_truncate_multiple_restart_identity_cascade() -> None:
    statement = only_statement(
        "TRUNCATE TABLE orders, order_items, shop.shipments RESTART IDENTITY CASCADE;"
    )
    assert statement.kind is StatementKind.TRUNCATE
    assert statement.targets == (
        QualifiedName("orders"),
        QualifiedName("order_items"),
        QualifiedName("shipments", schema="shop"),
    )
    # CASCADE widens the blast radius to every FK-referencing table.
    assert statement.details == TruncateDetails(cascade=True, restart_identity=True)


# ---------------------------------------------------------------------------
# Renames
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sql", "kind", "target", "details"),
    [
        (
            "ALTER TABLE users RENAME TO customers;",
            StatementKind.RENAME_TABLE,
            QualifiedName("users"),
            RenameDetails(old_name=None, new_name="customers", object_type="OBJECT_TABLE"),
        ),
        (
            "ALTER TABLE app.users RENAME COLUMN email TO email_address;",
            StatementKind.RENAME_COLUMN,
            QualifiedName("users", schema="app"),
            RenameDetails(
                old_name="email", new_name="email_address", object_type="OBJECT_COLUMN"
            ),
        ),
        (
            "ALTER INDEX idx_users_email RENAME TO users_email_idx;",
            StatementKind.RENAME_INDEX,
            QualifiedName("idx_users_email"),
            RenameDetails(old_name=None, new_name="users_email_idx", object_type="OBJECT_INDEX"),
        ),
        (
            "ALTER TABLE app.users RENAME CONSTRAINT users_email_key TO users_email_unique;",
            StatementKind.RENAME_CONSTRAINT,
            QualifiedName("users", schema="app"),
            RenameDetails(
                old_name="users_email_key",
                new_name="users_email_unique",
                object_type="OBJECT_TABCONSTRAINT",
            ),
        ),
        (
            "ALTER VIEW reporting.active_users RENAME TO current_users;",
            StatementKind.RENAME_VIEW,
            QualifiedName("active_users", schema="reporting"),
            RenameDetails(old_name=None, new_name="current_users", object_type="OBJECT_VIEW"),
        ),
    ],
)
def test_rename_forms(
    sql: str, kind: StatementKind, target: QualifiedName, details: RenameDetails
) -> None:
    statement = only_statement(sql)
    assert statement.kind is kind
    assert statement.node_tag == "RenameStmt"
    assert statement.targets == (target,)
    assert statement.details == details


# ---------------------------------------------------------------------------
# SET SCHEMA / OWNER TO
# ---------------------------------------------------------------------------


def test_alter_table_set_schema() -> None:
    statement = only_statement("ALTER TABLE legacy.users SET SCHEMA archive;")
    assert statement.kind is StatementKind.ALTER_OBJECT_SCHEMA
    assert statement.node_tag == "AlterObjectSchemaStmt"
    assert statement.targets == (QualifiedName("users", schema="legacy"),)
    assert statement.alter_actions == ()


def test_alter_table_owner_to() -> None:
    statement = only_statement("ALTER TABLE billing.invoices OWNER TO app_owner;")
    assert statement.kind is StatementKind.ALTER_TABLE
    assert statement.targets == (QualifiedName("invoices", schema="billing"),)
    action = only_action("ALTER TABLE billing.invoices OWNER TO app_owner;")
    assert action.kind is AlterTableActionKind.CHANGE_OWNER
    assert not action.cascade


# ---------------------------------------------------------------------------
# Partitions: ATTACH / DETACH
# ---------------------------------------------------------------------------


def test_attach_partition() -> None:
    statement = only_statement(
        """
        ALTER TABLE app.events
            ATTACH PARTITION app.events_p2026_02
            FOR VALUES FROM ('2026-02-01') TO ('2026-03-01');
        """
    )
    assert statement.kind is StatementKind.ALTER_TABLE
    assert statement.targets == (QualifiedName("events", schema="app"),)
    assert len(statement.alter_actions) == 1
    action = statement.alter_actions[0]
    assert action.kind is AlterTableActionKind.ATTACH_PARTITION
    assert action.partition == QualifiedName("events_p2026_02", schema="app")
    assert not action.not_valid


def test_detach_partition_plain() -> None:
    action = only_action("ALTER TABLE measurements DETACH PARTITION measurements_2024;")
    assert action.kind is AlterTableActionKind.DETACH_PARTITION
    assert action.partition == QualifiedName("measurements_2024")


def test_detach_partition_concurrently() -> None:
    action = only_action(
        "ALTER TABLE measurements DETACH PARTITION measurements_2024 CONCURRENTLY;"
    )
    assert action.kind is AlterTableActionKind.DETACH_PARTITION_CONCURRENTLY
    assert action.partition == QualifiedName("measurements_2024")


def test_detach_partition_finalize() -> None:
    statement = only_statement(
        "ALTER TABLE measurements DETACH PARTITION measurements_2024 FINALIZE;"
    )
    assert statement.kind is StatementKind.ALTER_TABLE
    assert statement.targets == (QualifiedName("measurements"),)
    assert len(statement.alter_actions) == 1
    action = statement.alter_actions[0]
    assert action.kind is AlterTableActionKind.DETACH_PARTITION_FINALIZE
    # FINALIZE is the second half of the concurrent-detach protocol; the
    # analyzer pairs it with the earlier DETACH CONCURRENTLY by this name.
    assert action.partition == QualifiedName("measurements_2024")


# ---------------------------------------------------------------------------
# Whole-table storage changes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sql", "kind"),
    [
        ("ALTER TABLE audit_log SET LOGGED;", AlterTableActionKind.SET_LOGGED),
        ("ALTER TABLE staging_events SET UNLOGGED;", AlterTableActionKind.SET_UNLOGGED),
    ],
)
def test_set_logged_unlogged(sql: str, kind: AlterTableActionKind) -> None:
    action = only_action(sql)
    assert action.kind is kind
    assert action.column is None


def test_set_tablespace() -> None:
    action = only_action("ALTER TABLE archive.events SET TABLESPACE fast_ssd;")
    assert action.kind is AlterTableActionKind.SET_TABLESPACE
    assert action.column is None
    assert action.detail == "fast_ssd"


def test_set_access_method() -> None:
    action = only_action("ALTER TABLE big_facts SET ACCESS METHOD columnar;")
    assert action.kind is AlterTableActionKind.SET_ACCESS_METHOD
    assert action.column is None
    assert action.detail == "columnar"


# ---------------------------------------------------------------------------
# Trigger toggles and row-level security
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sql", "kind", "name", "detail"),
    [
        (
            "ALTER TABLE orders ENABLE TRIGGER orders_audit;",
            AlterTableActionKind.ENABLE_TRIGGER,
            "orders_audit",
            None,
        ),
        (
            "ALTER TABLE orders ENABLE ALWAYS TRIGGER orders_audit;",
            AlterTableActionKind.ENABLE_TRIGGER,
            "orders_audit",
            "always",
        ),
        (
            "ALTER TABLE orders ENABLE REPLICA TRIGGER orders_sync;",
            AlterTableActionKind.ENABLE_TRIGGER,
            "orders_sync",
            "replica",
        ),
        (
            "ALTER TABLE orders DISABLE TRIGGER orders_audit;",
            AlterTableActionKind.DISABLE_TRIGGER,
            "orders_audit",
            None,
        ),
        (
            "ALTER TABLE orders ENABLE TRIGGER ALL;",
            AlterTableActionKind.ENABLE_TRIGGER,
            None,
            "all",
        ),
        (
            "ALTER TABLE orders DISABLE TRIGGER ALL;",
            AlterTableActionKind.DISABLE_TRIGGER,
            None,
            "all",
        ),
        (
            "ALTER TABLE orders ENABLE TRIGGER USER;",
            AlterTableActionKind.ENABLE_TRIGGER,
            None,
            "user",
        ),
        (
            "ALTER TABLE orders DISABLE TRIGGER USER;",
            AlterTableActionKind.DISABLE_TRIGGER,
            None,
            "user",
        ),
    ],
)
def test_trigger_toggles(
    sql: str, kind: AlterTableActionKind, name: str | None, detail: str | None
) -> None:
    action = only_action(sql)
    assert action.kind is kind
    assert action.constraint_name == name
    assert action.detail == detail


def test_row_level_security_enable_and_force() -> None:
    statement = only_statement(
        """
        -- lock tenants down before the policies land
        ALTER TABLE tenants
            ENABLE ROW LEVEL SECURITY,
            FORCE ROW LEVEL SECURITY;
        """
    )
    assert statement.kind is StatementKind.ALTER_TABLE
    assert statement.targets == (QualifiedName("tenants"),)
    assert tuple(action.kind for action in statement.alter_actions) == (
        AlterTableActionKind.ENABLE_ROW_SECURITY,
        AlterTableActionKind.FORCE_ROW_SECURITY,
    )


@pytest.mark.parametrize(
    ("sql", "kind"),
    [
        (
            "ALTER TABLE tenants DISABLE ROW LEVEL SECURITY;",
            AlterTableActionKind.DISABLE_ROW_SECURITY,
        ),
        (
            "ALTER TABLE tenants NO FORCE ROW LEVEL SECURITY;",
            AlterTableActionKind.NO_FORCE_ROW_SECURITY,
        ),
    ],
)
def test_row_level_security_disable_variants(sql: str, kind: AlterTableActionKind) -> None:
    assert only_action(sql).kind is kind


# ---------------------------------------------------------------------------
# Replica identity, clustering, ALTER INDEX
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "ALTER TABLE users REPLICA IDENTITY FULL;",
        "ALTER TABLE users REPLICA IDENTITY USING INDEX users_pkey;",
    ],
)
def test_replica_identity(sql: str) -> None:
    statement = only_statement(sql)
    assert statement.kind is StatementKind.ALTER_TABLE
    assert statement.targets == (QualifiedName("users"),)
    assert len(statement.alter_actions) == 1
    assert statement.alter_actions[0].kind is AlterTableActionKind.REPLICA_IDENTITY


def test_cluster_on_named_index() -> None:
    action = only_action("ALTER TABLE users CLUSTER ON users_pkey;")
    assert action.kind is AlterTableActionKind.CLUSTER_ON
    assert action.column is None
    assert action.detail == "users_pkey"


def test_set_without_cluster() -> None:
    action = only_action("ALTER TABLE users SET WITHOUT CLUSTER;")
    assert action.kind is AlterTableActionKind.DROP_CLUSTER


def test_alter_index_set_storage_params() -> None:
    statement = only_statement("ALTER INDEX idx_orders_created SET (fillfactor = 90);")
    assert statement.kind is StatementKind.ALTER_INDEX
    assert statement.node_tag == "AlterTableStmt"
    assert statement.targets == (QualifiedName("idx_orders_created"),)
    assert len(statement.alter_actions) == 1
    assert statement.alter_actions[0].kind is AlterTableActionKind.SET_STORAGE_PARAMS


def test_quoted_create_table_targets_preserved() -> None:
    statement = only_statement(
        """
        CREATE TABLE "Billing"."InvoiceLines" (
            "Id" bigint PRIMARY KEY,
            "InvoiceId" bigint NOT NULL
        );
        """
    )
    assert statement.kind is StatementKind.CREATE_TABLE
    assert statement.targets == (QualifiedName("InvoiceLines", schema="Billing"),)
    assert statement.details == CreateTableDetails(persistence="permanent")


# ---------------------------------------------------------------------------
# A full migration script, Flyway style
# ---------------------------------------------------------------------------


def test_partition_rotation_migration_script() -> None:
    script = parse(
        """
        -- V42__rotate_event_partitions.sql
        BEGIN;

        CREATE TABLE IF NOT EXISTS app.events_p2026_09
            PARTITION OF app.events
            FOR VALUES FROM ('2026-09-01') TO ('2026-10-01');

        ALTER TABLE app.events
            DETACH PARTITION app.events_p2024_01;  -- rotated out

        DROP TABLE IF EXISTS app.events_p2024_01;

        COMMIT;
        """
    )
    assert [s.kind for s in script.statements] == [
        StatementKind.BEGIN,
        StatementKind.CREATE_TABLE_PARTITION_OF,
        StatementKind.ALTER_TABLE,
        StatementKind.DROP_TABLE,
        StatementKind.COMMIT,
    ]

    create = script.statements[1]
    assert create.details == CreateTableDetails(
        persistence="permanent",
        if_not_exists=True,
        partition_of=QualifiedName("events", schema="app"),
    )

    detach = script.statements[2]
    assert len(detach.alter_actions) == 1
    assert detach.alter_actions[0].kind is AlterTableActionKind.DETACH_PARTITION
    assert detach.alter_actions[0].partition == QualifiedName("events_p2024_01", schema="app")

    drop = script.statements[3]
    assert drop.details == DropDetails(
        object_names=("app.events_p2024_01",), missing_ok=True
    )

    assert script.has_explicit_transactions
    assert len(script.transaction_groups) == 1
    group = script.transaction_groups[0]
    assert group.explicit
    assert group.statement_indices == (1, 2, 3)
    assert group.opened_by == 0
    assert group.closed_by == 4
    assert not group.rolled_back
    assert script.warnings == ()
