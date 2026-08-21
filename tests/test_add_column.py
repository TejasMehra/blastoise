"""ALTER TABLE ... ADD COLUMN / DROP COLUMN classification, one test per locking shape.

Every example is written the way real Alembic/Flyway/Rails/hand-rolled migrations
look: multi-line, commented, sometimes schema-qualified or quoted. The assertions
pin the exact classification: action kind, DefaultInfo volatility, deparsed default
expression, and the locking-relevant flags (not_null, missing_ok, cascade,
inline_constraints).
"""

from __future__ import annotations

import pytest
from helpers import only_action, only_statement, parse

from blastoise import (
    AlterTableActionKind,
    QualifiedName,
    StatementKind,
    Volatility,
)

# ---------------------------------------------------------------------------
# Catalog-only adds: nullable, and NOT NULL without a default
# ---------------------------------------------------------------------------


def test_plain_nullable_add_is_catalog_only() -> None:
    statement = only_statement(
        """
        -- migration 0042: track optional display names
        ALTER TABLE users
            ADD COLUMN nickname text;
        """
    )
    assert statement.kind is StatementKind.ALTER_TABLE
    assert statement.node_tag == "AlterTableStmt"
    assert statement.targets == (QualifiedName(name="users"),)
    (action,) = statement.alter_actions
    assert action.kind is AlterTableActionKind.ADD_COLUMN
    assert action.column == "nickname"
    assert action.column_type == "text"
    assert action.default is None
    assert action.not_null is False
    assert action.missing_ok is False
    assert action.inline_constraints == ()


def test_not_null_without_default_fails_on_populated_tables() -> None:
    action = only_action(
        """
        ALTER TABLE orders
            ADD COLUMN currency_code char(3) NOT NULL;  -- breaks if orders has rows
        """
    )
    assert action.kind is AlterTableActionKind.ADD_COLUMN_NOT_NULL_NO_DEFAULT
    assert action.column == "currency_code"
    assert action.not_null is True
    assert action.default is None


# ---------------------------------------------------------------------------
# Constant defaults (PG11+ fast path, no rewrite)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sql", "column", "expression"),
    [
        pytest.param(
            "ALTER TABLE orders ADD COLUMN retry_count integer DEFAULT 0;",
            "retry_count",
            "0",
            id="int-literal",
        ),
        pytest.param(
            "ALTER TABLE orders\n    ADD COLUMN status text DEFAULT 'pending';",
            "status",
            "'pending'",
            id="text-literal",
        ),
        pytest.param(
            "ALTER TABLE users ADD COLUMN is_active boolean DEFAULT true;",
            "is_active",
            "TRUE",
            id="boolean-literal",
        ),
        pytest.param(
            "ALTER TABLE users ADD COLUMN referral_code text DEFAULT NULL;",
            "referral_code",
            "NULL",
            id="explicit-null",
        ),
    ],
)
def test_constant_default_is_nonvolatile(sql: str, column: str, expression: str) -> None:
    action = only_action(sql)
    assert action.kind is AlterTableActionKind.ADD_COLUMN_DEFAULT_NONVOLATILE
    assert action.column == column
    assert action.default is not None
    assert action.default.volatility is Volatility.CONSTANT
    assert action.default.expression == expression
    assert action.not_null is False


@pytest.mark.parametrize(
    ("sql", "column", "expression"),
    [
        pytest.param(
            """
            -- jsonb column with an empty-object default, Alembic style
            ALTER TABLE audit_log
                ADD COLUMN context jsonb DEFAULT '{}'::jsonb;
            """,
            "context",
            "CAST('{}' AS jsonb)",
            id="jsonb-empty-object",
        ),
        pytest.param(
            "ALTER TABLE audit_log ADD COLUMN source text DEFAULT 'now'::text;",
            "source",
            "CAST('now' AS text)",
            id="text-cast-of-now-string",
        ),
    ],
)
def test_casted_constant_default_stays_constant(sql: str, column: str, expression: str) -> None:
    """A cast applied to a literal is evaluated once; no rewrite risk."""
    action = only_action(sql)
    assert action.kind is AlterTableActionKind.ADD_COLUMN_DEFAULT_NONVOLATILE
    assert action.column == column
    assert action.default is not None
    assert action.default.volatility is Volatility.CONSTANT
    assert action.default.expression == expression


# ---------------------------------------------------------------------------
# Stable defaults (still the PG11+ fast path)
# ---------------------------------------------------------------------------


def test_now_function_default_is_stable() -> None:
    action = only_action(
        """
        ALTER TABLE invoices
            ADD COLUMN issued_at timestamptz DEFAULT now();
        """
    )
    assert action.kind is AlterTableActionKind.ADD_COLUMN_DEFAULT_NONVOLATILE
    assert action.column == "issued_at"
    assert action.column_type == "timestamptz"
    assert action.default is not None
    assert action.default.volatility is Volatility.STABLE
    assert action.default.expression == "now()"


def test_current_timestamp_keyword_parses_as_sql_value_function() -> None:
    # CURRENT_TIMESTAMP is not a FuncCall in the parse tree; it must still
    # be recognized as stable via SQLValueFunction.
    action = only_action(
        "ALTER TABLE invoices ADD COLUMN updated_at timestamptz DEFAULT CURRENT_TIMESTAMP;"
    )
    assert action.kind is AlterTableActionKind.ADD_COLUMN_DEFAULT_NONVOLATILE
    assert action.default is not None
    assert action.default.volatility is Volatility.STABLE
    assert action.default.expression == "CURRENT_TIMESTAMP"


def test_arithmetic_over_stable_function_stays_stable() -> None:
    action = only_action(
        """
        -- trial accounts expire a day after signup
        ALTER TABLE users
            ADD COLUMN trial_expires_at timestamptz
                DEFAULT now() + interval '1 day';
        """
    )
    assert action.kind is AlterTableActionKind.ADD_COLUMN_DEFAULT_NONVOLATILE
    assert action.column == "trial_expires_at"
    assert action.default is not None
    assert action.default.volatility is Volatility.STABLE
    assert action.default.expression == "now() + CAST('1 day' AS interval)"


# ---------------------------------------------------------------------------
# Volatile and unknown defaults (rewrite / unprovable)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sql", "column", "expression"),
    [
        pytest.param(
            """
            ALTER TABLE users
                ADD COLUMN public_id uuid DEFAULT gen_random_uuid();
            """,
            "public_id",
            "gen_random_uuid()",
            id="gen_random_uuid",
        ),
        pytest.param(
            "ALTER TABLE experiments ADD COLUMN bucket_seed double precision DEFAULT random();",
            "bucket_seed",
            "random()",
            id="random",
        ),
        pytest.param(
            """
            -- uuid-ossp flavor, common in older Rails apps
            ALTER TABLE orders
                ADD COLUMN external_ref uuid DEFAULT uuid_generate_v4();
            """,
            "external_ref",
            "uuid_generate_v4()",
            id="uuid_generate_v4",
        ),
        pytest.param(
            """
            ALTER TABLE billing.invoices
                ADD COLUMN invoice_number bigint DEFAULT nextval('billing.invoice_number_seq');
            """,
            "invoice_number",
            "nextval('billing.invoice_number_seq')",
            id="nextval",
        ),
    ],
)
def test_volatile_default_forces_table_rewrite(sql: str, column: str, expression: str) -> None:
    action = only_action(sql)
    assert action.kind is AlterTableActionKind.ADD_COLUMN_DEFAULT_VOLATILE
    assert action.column == column
    assert action.default is not None
    assert action.default.volatility is Volatility.VOLATILE
    assert action.default.expression == expression


def test_unrecognized_function_default_has_unknown_volatility() -> None:
    action = only_action(
        """
        ALTER TABLE orders
            ADD COLUMN risk_score integer DEFAULT my_custom_fn();
        """
    )
    assert action.kind is AlterTableActionKind.ADD_COLUMN_DEFAULT_UNKNOWN_VOLATILITY
    assert action.column == "risk_score"
    assert action.default is not None
    assert action.default.volatility is Volatility.UNKNOWN
    assert action.default.expression == "my_custom_fn()"


# ---------------------------------------------------------------------------
# Serial, identity, and generated columns
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sql", "column", "column_type"),
    [
        pytest.param(
            "ALTER TABLE audit_log ADD COLUMN entry_id serial;",
            "entry_id",
            "serial",
            id="serial",
        ),
        pytest.param(
            "ALTER TABLE audit_log ADD COLUMN entry_id bigserial;",
            "entry_id",
            "bigserial",
            id="bigserial",
        ),
    ],
)
def test_serial_pseudo_type_implies_volatile_nextval(
    sql: str, column: str, column_type: str
) -> None:
    action = only_action(sql)
    assert action.kind is AlterTableActionKind.ADD_COLUMN_SERIAL
    assert action.column == column
    assert action.column_type == column_type
    assert action.default is None  # nextval() is implied by the pseudo-type


@pytest.mark.parametrize(
    ("sql", "not_null"),
    [
        pytest.param(
            """
            ALTER TABLE orders
                ADD COLUMN row_id bigint GENERATED ALWAYS AS IDENTITY;
            """,
            False,
            id="always",
        ),
        pytest.param(
            """
            ALTER TABLE orders
                ADD COLUMN row_id bigint NOT NULL GENERATED BY DEFAULT AS IDENTITY;
            """,
            True,
            id="by-default-not-null",
        ),
    ],
)
def test_identity_column_is_backfilled_with_nextval(sql: str, not_null: bool) -> None:
    action = only_action(sql)
    assert action.kind is AlterTableActionKind.ADD_COLUMN_IDENTITY
    assert action.column == "row_id"
    assert action.column_type == "bigint"
    assert action.not_null is not_null


def test_generated_stored_column_rewrites_the_table() -> None:
    action = only_action(
        """
        ALTER TABLE invoices
            ADD COLUMN total_cents numeric(12, 2)
                GENERATED ALWAYS AS (subtotal_cents + tax_cents) STORED;
        """
    )
    assert action.kind is AlterTableActionKind.ADD_COLUMN_GENERATED_STORED
    assert action.column == "total_cents"
    assert action.column_type == "numeric(12, 2)"
    assert action.default is None  # generation expression is not a DEFAULT


# ---------------------------------------------------------------------------
# DROP COLUMN and IF [NOT] EXISTS variants
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sql", "cascade", "missing_ok"),
    [
        pytest.param(
            "ALTER TABLE users DROP COLUMN legacy_flags;",
            False,
            False,
            id="plain",
        ),
        pytest.param(
            "ALTER TABLE users DROP COLUMN IF EXISTS legacy_flags;",
            False,
            True,
            id="if-exists",
        ),
        pytest.param(
            """
            -- also drops the dependent view over this column
            ALTER TABLE users
                DROP COLUMN legacy_flags CASCADE;
            """,
            True,
            False,
            id="cascade",
        ),
    ],
)
def test_drop_column_variants(sql: str, cascade: bool, missing_ok: bool) -> None:
    action = only_action(sql)
    assert action.kind is AlterTableActionKind.DROP_COLUMN
    assert action.column == "legacy_flags"
    assert action.cascade is cascade
    assert action.missing_ok is missing_ok


def test_add_column_if_not_exists_sets_missing_ok() -> None:
    action = only_action(
        """
        ALTER TABLE orders
            ADD COLUMN IF NOT EXISTS tracking_code text;
        """
    )
    assert action.kind is AlterTableActionKind.ADD_COLUMN
    assert action.column == "tracking_code"
    assert action.missing_ok is True
    assert action.default is None


# ---------------------------------------------------------------------------
# Inline column constraints on ADD COLUMN
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sql", "kind", "column", "inline", "not_null"),
    [
        pytest.param(
            "ALTER TABLE audit_log ADD COLUMN id bigint PRIMARY KEY;",
            AlterTableActionKind.ADD_COLUMN,
            "id",
            ("primary_key",),
            False,
            id="primary-key",
        ),
        pytest.param(
            """
            ALTER TABLE users
                ADD COLUMN email citext UNIQUE NOT NULL;
            """,
            AlterTableActionKind.ADD_COLUMN_NOT_NULL_NO_DEFAULT,
            "email",
            ("unique",),
            True,
            id="unique-not-null",
        ),
        pytest.param(
            """
            ALTER TABLE public.orders
                ADD COLUMN customer_id bigint REFERENCES public.customers (id);
            """,
            AlterTableActionKind.ADD_COLUMN,
            "customer_id",
            ("references",),
            False,
            id="references",
        ),
        pytest.param(
            "ALTER TABLE invoices ADD COLUMN line_count integer CHECK (line_count >= 0);",
            AlterTableActionKind.ADD_COLUMN,
            "line_count",
            ("check",),
            False,
            id="check",
        ),
    ],
)
def test_inline_constraints_are_captured(
    sql: str,
    kind: AlterTableActionKind,
    column: str,
    inline: tuple[str, ...],
    not_null: bool,
) -> None:
    action = only_action(sql)
    assert action.kind is kind
    assert action.column == column
    assert action.inline_constraints == inline
    assert action.not_null is not_null


# ---------------------------------------------------------------------------
# Combined forms
# ---------------------------------------------------------------------------


def test_not_null_with_volatile_default_keeps_both_facts() -> None:
    # Kind comes from the default's volatility; NOT NULL is carried separately.
    action = only_action(
        """
        ALTER TABLE users
            ADD COLUMN api_key uuid NOT NULL DEFAULT gen_random_uuid();
        """
    )
    assert action.kind is AlterTableActionKind.ADD_COLUMN_DEFAULT_VOLATILE
    assert action.column == "api_key"
    assert action.not_null is True
    assert action.default is not None
    assert action.default.volatility is Volatility.VOLATILE
    assert action.default.expression == "gen_random_uuid()"


def test_multiple_add_column_subcommands_stay_in_order() -> None:
    statement = only_statement(
        """
        -- one lock acquisition, several subcommands (hand-written style)
        ALTER TABLE billing.invoices
            ADD COLUMN po_number text,
            ADD COLUMN paid_at timestamptz DEFAULT now(),
            ADD COLUMN audit_token uuid DEFAULT gen_random_uuid(),
            DROP COLUMN IF EXISTS legacy_total;
        """
    )
    assert statement.kind is StatementKind.ALTER_TABLE
    assert statement.targets == (QualifiedName(name="invoices", schema="billing"),)
    kinds = [action.kind for action in statement.alter_actions]
    assert kinds == [
        AlterTableActionKind.ADD_COLUMN,
        AlterTableActionKind.ADD_COLUMN_DEFAULT_NONVOLATILE,
        AlterTableActionKind.ADD_COLUMN_DEFAULT_VOLATILE,
        AlterTableActionKind.DROP_COLUMN,
    ]
    columns = [action.column for action in statement.alter_actions]
    assert columns == ["po_number", "paid_at", "audit_token", "legacy_total"]
    paid_at = statement.alter_actions[1]
    assert paid_at.default is not None
    assert paid_at.default.volatility is Volatility.STABLE
    dropped = statement.alter_actions[3]
    assert dropped.missing_ok is True


def test_quoted_identifiers_preserve_case() -> None:
    action = only_action(
        """
        ALTER TABLE "UserAccounts"
            ADD COLUMN "lastLoginAt" timestamptz;
        """
    )
    assert action.kind is AlterTableActionKind.ADD_COLUMN
    assert action.column == "lastLoginAt"
    assert action.column_type == "timestamptz"


def test_flyway_style_migration_with_transaction_wrapper() -> None:
    # V7__add_soft_delete.sql: explicit transaction plus two ALTERs.
    script = parse(
        """
        -- V7__add_soft_delete.sql
        -- Adds soft-delete bookkeeping to users and orders.
        BEGIN;

        ALTER TABLE users
            ADD COLUMN deleted_at timestamptz;  -- NULL means live

        ALTER TABLE orders
            ADD COLUMN deleted_at timestamptz,
            ADD COLUMN deleted_by text DEFAULT 'system';

        COMMIT;
        """
    )
    kinds = [statement.kind for statement in script.statements]
    assert kinds == [
        StatementKind.BEGIN,
        StatementKind.ALTER_TABLE,
        StatementKind.ALTER_TABLE,
        StatementKind.COMMIT,
    ]
    assert script.has_explicit_transactions
    (group,) = script.transaction_groups
    assert group.explicit is True
    assert group.statement_indices == (1, 2)
    assert group.opened_by == 0
    assert group.closed_by == 3

    users_alter = script.statements[1]
    assert users_alter.span.line == 6  # 1-based, counting the leading blank line
    assert users_alter.sql.startswith("ALTER TABLE users")
    (users_action,) = users_alter.alter_actions
    assert users_action.kind is AlterTableActionKind.ADD_COLUMN
    assert users_action.column == "deleted_at"

    orders_alter = script.statements[2]
    assert [action.kind for action in orders_alter.alter_actions] == [
        AlterTableActionKind.ADD_COLUMN,
        AlterTableActionKind.ADD_COLUMN_DEFAULT_NONVOLATILE,
    ]
    deleted_by = orders_alter.alter_actions[1]
    assert deleted_by.default is not None
    assert deleted_by.default.volatility is Volatility.CONSTANT
    assert deleted_by.default.expression == "'system'"
