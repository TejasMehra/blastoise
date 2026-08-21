"""Real-world migration examples: non-table objects, session state, and DML.

Each test parses a realistic migration snippet (formatted the way Alembic,
Flyway, Rails, or hand-written migrations look) and asserts the exact
pgverdict classification, including the locking-relevant detail fields.
"""

from __future__ import annotations

import pytest
from helpers import only_statement, parse

from pgverdict import (
    CreateTableAsDetails,
    DropDetails,
    LockDetails,
    QualifiedName,
    SetDetails,
    StatementKind,
)

# ---------------------------------------------------------------------------
# Views and materialized views
# ---------------------------------------------------------------------------


def test_create_view_schema_qualified() -> None:
    stmt = only_statement(
        """
        -- reporting rollup consumed by the BI dashboard
        CREATE VIEW reporting.open_invoices AS
        SELECT i.id, i.customer_id, i.total_cents
        FROM billing.invoices i
        WHERE i.paid_at IS NULL;
        """
    )
    assert stmt.kind is StatementKind.CREATE_VIEW
    assert stmt.node_tag == "ViewStmt"
    assert stmt.targets == (QualifiedName(name="open_invoices", schema="reporting"),)
    assert stmt.details is None


def test_create_or_replace_view_with_quoted_identifier() -> None:
    stmt = only_statement(
        """
        CREATE OR REPLACE VIEW "activeUsers" AS
        SELECT id, email, last_seen_at
        FROM users
        WHERE deleted_at IS NULL;
        """
    )
    assert stmt.kind is StatementKind.CREATE_VIEW
    assert stmt.node_tag == "ViewStmt"
    assert stmt.targets == (QualifiedName(name="activeUsers"),)


@pytest.mark.parametrize(("with_clause", "no_data"), [("WITH NO DATA", True), ("WITH DATA", False)])
def test_create_materialized_view(with_clause: str, no_data: bool) -> None:
    stmt = only_statement(
        "CREATE MATERIALIZED VIEW reporting.daily_order_totals AS\n"
        "SELECT order_date, sum(total_cents) AS total_cents\n"
        "FROM orders\n"
        "GROUP BY order_date\n"
        f"{with_clause};"
    )
    assert stmt.kind is StatementKind.CREATE_MATVIEW
    assert stmt.node_tag == "CreateTableAsStmt"
    assert stmt.targets == (QualifiedName(name="daily_order_totals", schema="reporting"),)
    assert isinstance(stmt.details, CreateTableAsDetails)
    assert stmt.details.no_data is no_data


def test_drop_view_multiple_names() -> None:
    stmt = only_statement('DROP VIEW IF EXISTS active_users, "activeUsers";')
    assert stmt.kind is StatementKind.DROP_VIEW
    assert stmt.node_tag == "DropStmt"
    assert isinstance(stmt.details, DropDetails)
    assert stmt.details.object_names == ("active_users", "activeUsers")
    assert stmt.details.missing_ok is True
    assert stmt.details.cascade is False
    assert stmt.targets == (QualifiedName(name="active_users"), QualifiedName(name="activeUsers"))


def test_drop_materialized_view_cascade() -> None:
    stmt = only_statement("DROP MATERIALIZED VIEW IF EXISTS reporting.daily_order_totals CASCADE;")
    assert stmt.kind is StatementKind.DROP_MATVIEW
    assert isinstance(stmt.details, DropDetails)
    assert stmt.details.object_names == ("reporting.daily_order_totals",)
    assert stmt.details.cascade is True
    assert stmt.details.missing_ok is True
    assert stmt.targets == (QualifiedName(name="daily_order_totals", schema="reporting"),)


# ---------------------------------------------------------------------------
# Sequences
# ---------------------------------------------------------------------------


def test_sequence_lifecycle_migration() -> None:
    script = parse(
        """
        -- move invoice numbering onto its own sequence
        CREATE SEQUENCE billing.invoice_number_seq
            START WITH 1000
            INCREMENT BY 1
            OWNED BY billing.invoices.invoice_number;

        ALTER SEQUENCE billing.invoice_number_seq RESTART WITH 5000;

        DROP SEQUENCE IF EXISTS billing.legacy_invoice_seq;
        """
    )
    create, alter, drop = script.statements
    assert create.kind is StatementKind.CREATE_SEQUENCE
    assert create.node_tag == "CreateSeqStmt"
    assert create.targets == (QualifiedName(name="invoice_number_seq", schema="billing"),)
    assert alter.kind is StatementKind.ALTER_SEQUENCE
    assert alter.node_tag == "AlterSeqStmt"
    assert alter.targets == (QualifiedName(name="invoice_number_seq", schema="billing"),)
    assert drop.kind is StatementKind.DROP_SEQUENCE
    assert isinstance(drop.details, DropDetails)
    assert drop.details.object_names == ("billing.legacy_invoice_seq",)
    assert drop.details.missing_ok is True
    assert drop.details.cascade is False
    assert drop.targets == (QualifiedName(name="legacy_invoice_seq", schema="billing"),)


# ---------------------------------------------------------------------------
# Enum, composite, and domain types
# ---------------------------------------------------------------------------


def test_enum_type_creation_and_value_addition() -> None:
    script = parse(
        """
        CREATE TYPE order_status AS ENUM ('pending', 'paid', 'shipped', 'cancelled');

        -- new refund flow (PG < 12 cannot do this inside a transaction block)
        ALTER TYPE order_status ADD VALUE IF NOT EXISTS 'refund_pending' BEFORE 'cancelled';
        ALTER TYPE order_status ADD VALUE 'refunded' AFTER 'refund_pending';
        """
    )
    create, add_before, add_after = script.statements
    assert create.kind is StatementKind.CREATE_ENUM_TYPE
    assert create.node_tag == "CreateEnumStmt"
    assert create.targets == (QualifiedName(name="order_status"),)
    for stmt in (add_before, add_after):
        assert stmt.kind is StatementKind.ALTER_ENUM_ADD_VALUE
        assert stmt.node_tag == "AlterEnumStmt"
        assert stmt.targets == (QualifiedName(name="order_status"),)


def test_enum_rename_value_and_schema_qualified_add() -> None:
    script = parse(
        """
        ALTER TYPE order_status RENAME VALUE 'shiped' TO 'shipped';
        ALTER TYPE billing.invoice_state ADD VALUE 'voided';
        """
    )
    rename, add = script.statements
    assert rename.kind is StatementKind.ALTER_ENUM_RENAME_VALUE
    assert rename.node_tag == "AlterEnumStmt"
    assert rename.targets == (QualifiedName(name="order_status"),)
    assert add.kind is StatementKind.ALTER_ENUM_ADD_VALUE
    assert add.targets == (QualifiedName(name="invoice_state", schema="billing"),)


def test_domain_lifecycle_migration() -> None:
    script = parse(
        """
        CREATE DOMAIN email_address AS text
            CHECK (VALUE ~* '^[^@]+@[^@]+$');

        ALTER DOMAIN email_address SET NOT NULL;

        DROP DOMAIN IF EXISTS legacy_email CASCADE;
        """
    )
    create, alter, drop = script.statements
    assert create.kind is StatementKind.CREATE_DOMAIN
    assert create.node_tag == "CreateDomainStmt"
    assert create.targets == (QualifiedName(name="email_address"),)
    assert alter.kind is StatementKind.ALTER_DOMAIN
    assert alter.node_tag == "AlterDomainStmt"
    assert alter.targets == (QualifiedName(name="email_address"),)
    assert drop.kind is StatementKind.DROP_DOMAIN
    assert isinstance(drop.details, DropDetails)
    assert drop.details.object_names == ("legacy_email",)
    assert drop.details.cascade is True
    assert drop.details.missing_ok is True
    assert drop.targets == ()


def test_composite_type_and_drop_type() -> None:
    script = parse(
        """
        CREATE TYPE money_amount AS (
            amount   numeric(12, 2),
            currency char(3)
        );
        DROP TYPE IF EXISTS legacy_money;
        """
    )
    create, drop = script.statements
    assert create.kind is StatementKind.CREATE_COMPOSITE_TYPE
    assert create.node_tag == "CompositeTypeStmt"
    assert create.targets == (QualifiedName(name="money_amount"),)
    assert drop.kind is StatementKind.DROP_TYPE
    assert isinstance(drop.details, DropDetails)
    assert drop.details.object_names == ("legacy_money",)
    assert drop.details.missing_ok is True
    assert drop.targets == ()


# ---------------------------------------------------------------------------
# Extensions, functions, procedures, triggers, policies
# ---------------------------------------------------------------------------


def test_create_extension_if_not_exists() -> None:
    stmt = only_statement("CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public;")
    assert stmt.kind is StatementKind.CREATE_EXTENSION
    assert stmt.node_tag == "CreateExtensionStmt"
    assert stmt.targets == ()


def test_create_function_vs_create_procedure() -> None:
    script = parse(
        """
        CREATE OR REPLACE FUNCTION public.touch_updated_at() RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            NEW.updated_at := now();
            RETURN NEW;
        END;
        $$;

        CREATE PROCEDURE archive_old_orders(cutoff date)
        LANGUAGE sql
        AS $body$
            DELETE FROM orders WHERE created_at < cutoff;
        $body$;
        """
    )
    function, procedure = script.statements
    assert function.kind is StatementKind.CREATE_FUNCTION
    assert function.node_tag == "CreateFunctionStmt"
    assert function.targets == (QualifiedName(name="touch_updated_at", schema="public"),)
    assert procedure.kind is StatementKind.CREATE_PROCEDURE
    assert procedure.node_tag == "CreateFunctionStmt"
    assert procedure.targets == (QualifiedName(name="archive_old_orders"),)


def test_drop_function_with_args_vs_drop_procedure() -> None:
    script = parse(
        """
        DROP FUNCTION billing.compute_total(integer, text);
        DROP PROCEDURE IF EXISTS archive_old_orders();
        """
    )
    drop_function, drop_procedure = script.statements
    assert drop_function.kind is StatementKind.DROP_FUNCTION
    assert drop_function.node_tag == "DropStmt"
    assert isinstance(drop_function.details, DropDetails)
    assert drop_function.details.object_names == ("billing.compute_total",)
    assert drop_function.details.missing_ok is False
    assert drop_function.targets == ()
    assert drop_procedure.kind is StatementKind.DROP_PROCEDURE
    assert isinstance(drop_procedure.details, DropDetails)
    assert drop_procedure.details.object_names == ("archive_old_orders",)
    assert drop_procedure.details.missing_ok is True
    assert drop_procedure.targets == ()


def test_create_trigger_targets_the_table() -> None:
    stmt = only_statement(
        """
        CREATE TRIGGER trg_touch_updated_at
        BEFORE UPDATE ON users
        FOR EACH ROW
        EXECUTE FUNCTION public.touch_updated_at();
        """
    )
    assert stmt.kind is StatementKind.CREATE_TRIGGER
    assert stmt.node_tag == "CreateTrigStmt"
    assert stmt.targets == (QualifiedName(name="users"),)


def test_drop_trigger_uses_dotted_object_name() -> None:
    stmt = only_statement("DROP TRIGGER trg_touch_updated_at ON users;")
    assert stmt.kind is StatementKind.DROP_TRIGGER
    assert stmt.sql == "DROP TRIGGER trg_touch_updated_at ON users"
    assert stmt.span.line == 1
    assert isinstance(stmt.details, DropDetails)
    assert stmt.details.object_names == ("users.trg_touch_updated_at",)
    assert stmt.targets == ()


def test_row_level_security_policies() -> None:
    script = parse(
        """
        CREATE POLICY tenant_isolation ON app.accounts
            USING (tenant_id = current_setting('app.tenant_id')::uuid);

        DROP POLICY IF EXISTS legacy_tenant_isolation ON app.accounts;
        """
    )
    create, drop = script.statements
    assert create.kind is StatementKind.CREATE_POLICY
    assert create.node_tag == "CreatePolicyStmt"
    assert create.targets == (QualifiedName(name="accounts", schema="app"),)
    assert drop.kind is StatementKind.DROP_POLICY
    assert isinstance(drop.details, DropDetails)
    assert drop.details.object_names == ("app.accounts.legacy_tenant_isolation",)
    assert drop.details.missing_ok is True
    assert drop.targets == ()


# ---------------------------------------------------------------------------
# ACLs and comments
# ---------------------------------------------------------------------------


def test_grant_and_revoke_are_distinct_kinds() -> None:
    script = parse(
        """
        GRANT SELECT ON orders, invoices TO reporting_ro;
        REVOKE INSERT, UPDATE ON orders FROM app_writer;
        GRANT reporting_ro TO alice;
        """
    )
    grant, revoke, grant_role = script.statements
    assert grant.kind is StatementKind.GRANT
    assert grant.node_tag == "GrantStmt"
    assert revoke.kind is StatementKind.REVOKE
    assert revoke.node_tag == "GrantStmt"
    assert grant_role.kind is StatementKind.GRANT
    assert grant_role.node_tag == "GrantRoleStmt"


def test_comment_on_column() -> None:
    stmt = only_statement("COMMENT ON COLUMN users.deleted_at IS 'soft delete marker';")
    assert stmt.kind is StatementKind.COMMENT_ON
    assert stmt.node_tag == "CommentStmt"
    assert stmt.targets == ()


# ---------------------------------------------------------------------------
# DML backfills
# ---------------------------------------------------------------------------


def test_backfill_update_inside_explicit_transaction() -> None:
    script = parse(
        """
        -- 0042_backfill_customer_emails.sql
        BEGIN;

        SET LOCAL lock_timeout = '5s';

        UPDATE orders o
        SET customer_email = c.email
        FROM customers c
        WHERE o.customer_id = c.id
          AND o.customer_email IS NULL;

        COMMIT;
        """
    )
    assert [s.kind for s in script.statements] == [
        StatementKind.BEGIN,
        StatementKind.SET,
        StatementKind.UPDATE,
        StatementKind.COMMIT,
    ]
    set_stmt = script.statements[1]
    assert isinstance(set_stmt.details, SetDetails)
    assert set_stmt.details.name == "lock_timeout"
    assert set_stmt.details.is_local is True
    update = script.statements[2]
    assert update.node_tag == "UpdateStmt"
    assert update.targets == (QualifiedName(name="orders"),)
    assert script.has_explicit_transactions is True
    (group,) = script.transaction_groups
    assert group.explicit is True
    assert group.statement_indices == (1, 2)
    assert group.opened_by == 0
    assert group.closed_by == 3
    assert group.rolled_back is False


def test_insert_on_conflict_upsert() -> None:
    stmt = only_statement(
        """
        INSERT INTO app_settings (key, value)
        VALUES ('feature.dark_mode', 'on')
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;
        """
    )
    assert stmt.kind is StatementKind.INSERT
    assert stmt.node_tag == "InsertStmt"
    assert stmt.targets == (QualifiedName(name="app_settings"),)


def test_multi_row_reference_data_insert() -> None:
    stmt = only_statement(
        """
        INSERT INTO countries (code, name) VALUES
            ('IN', 'India'),
            ('US', 'United States'),
            ('DE', 'Germany');
        """
    )
    assert stmt.kind is StatementKind.INSERT
    assert stmt.node_tag == "InsertStmt"
    assert stmt.targets == (QualifiedName(name="countries"),)


def test_delete_using_targets_primary_table() -> None:
    stmt = only_statement(
        """
        DELETE FROM sessions s
        USING users u
        WHERE s.user_id = u.id
          AND u.deleted_at IS NOT NULL;
        """
    )
    assert stmt.kind is StatementKind.DELETE
    assert stmt.node_tag == "DeleteStmt"
    assert stmt.targets == (QualifiedName(name="sessions"),)


def test_merge_targets_the_merged_into_table() -> None:
    stmt = only_statement(
        """
        MERGE INTO customer_balances cb
        USING staged_payments sp ON cb.customer_id = sp.customer_id
        WHEN MATCHED THEN
            UPDATE SET balance = cb.balance - sp.amount
        WHEN NOT MATCHED THEN
            INSERT (customer_id, balance) VALUES (sp.customer_id, -sp.amount);
        """
    )
    assert stmt.kind is StatementKind.MERGE
    assert stmt.node_tag == "MergeStmt"
    assert stmt.targets == (QualifiedName(name="customer_balances"),)


def test_bare_select_copy_and_call() -> None:
    script = parse(
        """
        SELECT count(*) FROM orders WHERE customer_email IS NULL;
        COPY audit_log (event, actor, occurred_at) FROM STDIN WITH (FORMAT csv);
        CALL maintenance.rebuild_rollups(7);
        """
    )
    select, copy, call = script.statements
    assert select.kind is StatementKind.SELECT
    assert select.node_tag == "SelectStmt"
    assert select.targets == ()
    assert copy.kind is StatementKind.COPY_FROM
    assert copy.node_tag == "CopyStmt"
    assert copy.targets == (QualifiedName(name="audit_log"),)
    assert call.kind is StatementKind.CALL
    assert call.node_tag == "CallStmt"
    assert call.targets == ()


# ---------------------------------------------------------------------------
# Session and server state
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sql", "kind", "name", "is_local"),
    [
        ("SET statement_timeout = '600s'", StatementKind.SET, "statement_timeout", False),
        ("SET LOCAL lock_timeout = '2s'", StatementKind.SET, "lock_timeout", True),
        ("SET search_path TO DEFAULT", StatementKind.SET, "search_path", False),
        ("RESET statement_timeout", StatementKind.RESET, "statement_timeout", False),
        ("RESET ALL", StatementKind.RESET, None, False),
    ],
)
def test_set_and_reset_session_state(
    sql: str, kind: StatementKind, name: str | None, is_local: bool
) -> None:
    stmt = only_statement(sql)
    assert stmt.kind is kind
    assert stmt.node_tag == "VariableSetStmt"
    assert isinstance(stmt.details, SetDetails)
    assert stmt.details.name == name
    assert stmt.details.is_local is is_local


@pytest.mark.parametrize(
    ("sql", "mode", "mode_name", "nowait", "target"),
    [
        (
            "LOCK TABLE orders IN SHARE ROW EXCLUSIVE MODE",
            6,
            "SHARE ROW EXCLUSIVE",
            False,
            QualifiedName(name="orders"),
        ),
        (
            "LOCK TABLE accounting.invoices IN ACCESS EXCLUSIVE MODE NOWAIT",
            8,
            "ACCESS EXCLUSIVE",
            True,
            QualifiedName(name="invoices", schema="accounting"),
        ),
        # LOCK TABLE with no mode defaults to ACCESS EXCLUSIVE
        ("LOCK TABLE users", 8, "ACCESS EXCLUSIVE", False, QualifiedName(name="users")),
    ],
)
def test_lock_table_modes(
    sql: str, mode: int, mode_name: str, nowait: bool, target: QualifiedName
) -> None:
    stmt = only_statement(sql)
    assert stmt.kind is StatementKind.LOCK_TABLE
    assert stmt.node_tag == "LockStmt"
    assert stmt.targets == (target,)
    assert isinstance(stmt.details, LockDetails)
    assert stmt.details.mode == mode
    assert stmt.details.mode_name == mode_name
    assert stmt.details.nowait is nowait


def test_alter_system_set() -> None:
    stmt = only_statement("ALTER SYSTEM SET wal_level = 'logical';")
    assert stmt.kind is StatementKind.ALTER_SYSTEM
    assert stmt.node_tag == "AlterSystemStmt"
    assert stmt.details is None
    assert stmt.targets == ()


@pytest.mark.parametrize(
    ("sql", "node_tag"),
    [("CHECKPOINT", "CheckPointStmt"), ("LISTEN cache_invalidation", "ListenStmt")],
)
def test_unhandled_statements_fall_back_to_other(sql: str, node_tag: str) -> None:
    stmt = only_statement(sql)
    assert stmt.kind is StatementKind.OTHER
    assert stmt.node_tag == node_tag
    assert stmt.sql == sql
