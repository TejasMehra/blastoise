"""Column alterations and table constraints, exercised on realistic migrations.

Every example is shaped like something Alembic/Flyway/Rails or a hand-written
migration would emit: multi-line statements, comments, schema-qualified names,
and the occasional quoted identifier. Assertions pin the exact classification,
including the locking-relevant flags (not_valid, using_index, cascade, ...).
"""

from __future__ import annotations

import pytest
from helpers import only_action, only_statement, parse

from blastoise import AlterTableActionKind, QualifiedName, StatementKind, Volatility

# ---------------------------------------------------------------------------
# NOT NULL toggles
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sql", "expected_kind", "column"),
    [
        (
            "-- 0043_users_email_required.sql\n"
            "ALTER TABLE public.users\n"
            "    ALTER COLUMN email SET NOT NULL;",
            AlterTableActionKind.SET_NOT_NULL,
            "email",
        ),
        (
            "-- soften the column kept around from the CRM import\n"
            "ALTER TABLE public.users\n"
            "    ALTER COLUMN legacy_crm_id DROP NOT NULL;",
            AlterTableActionKind.DROP_NOT_NULL,
            "legacy_crm_id",
        ),
    ],
)
def test_not_null_toggles_capture_the_column(
    sql: str, expected_kind: AlterTableActionKind, column: str
) -> None:
    statement = only_statement(sql)
    assert statement.kind is StatementKind.ALTER_TABLE
    assert statement.node_tag == "AlterTableStmt"
    assert statement.targets == (QualifiedName(name="users", schema="public"),)
    assert len(statement.alter_actions) == 1
    action = statement.alter_actions[0]
    assert action.kind is expected_kind
    assert action.column == column
    assert action.constraint_name is None
    assert action.not_valid is False


# ---------------------------------------------------------------------------
# Column defaults
# ---------------------------------------------------------------------------


def test_set_default_constant() -> None:
    action = only_action(
        """
        -- Rails: change_column_default :invoices, :status, from: nil, to: "draft"
        ALTER TABLE invoices
            ALTER COLUMN status SET DEFAULT 'draft';
        """
    )
    assert action.kind is AlterTableActionKind.SET_COLUMN_DEFAULT
    assert action.column == "status"
    assert action.default is not None
    assert action.default.volatility is Volatility.CONSTANT
    assert action.default.expression == "'draft'"


@pytest.mark.parametrize(
    ("sql", "volatility", "expression"),
    [
        (
            "ALTER TABLE users ALTER COLUMN external_ref SET DEFAULT gen_random_uuid();",
            Volatility.VOLATILE,
            "gen_random_uuid()",
        ),
        (
            "ALTER TABLE audit_log ALTER COLUMN recorded_at SET DEFAULT now();",
            Volatility.STABLE,
            "now()",
        ),
    ],
)
def test_set_default_records_volatility_without_changing_kind(
    sql: str, volatility: Volatility, expression: str
) -> None:
    # SET DEFAULT only affects future inserts, so even a volatile default
    # keeps the set_column_default kind; volatility is recorded on the side.
    action = only_action(sql)
    assert action.kind is AlterTableActionKind.SET_COLUMN_DEFAULT
    assert action.default is not None
    assert action.default.volatility is volatility
    assert action.default.expression == expression


def test_drop_default_carries_no_default_info() -> None:
    action = only_action("ALTER TABLE invoices ALTER COLUMN status DROP DEFAULT;")
    assert action.kind is AlterTableActionKind.DROP_COLUMN_DEFAULT
    assert action.column == "status"
    assert action.default is None


# ---------------------------------------------------------------------------
# ALTER COLUMN TYPE
# ---------------------------------------------------------------------------


def test_alter_column_type_with_using_expression() -> None:
    statement = only_statement(
        """
        -- Alembic: op.alter_column("users", "email", type_=sa.String(255))
        ALTER TABLE public.users
            ALTER COLUMN email TYPE varchar(255)
            USING email::varchar(255);
        """
    )
    assert statement.kind is StatementKind.ALTER_TABLE
    assert statement.targets == (QualifiedName(name="users", schema="public"),)
    assert len(statement.alter_actions) == 1
    action = statement.alter_actions[0]
    assert action.kind is AlterTableActionKind.ALTER_COLUMN_TYPE
    assert action.column == "email"
    assert action.column_type == "varchar(255)"
    assert action.has_using_expression is True


def test_alter_column_type_without_using_expression() -> None:
    action = only_action("ALTER TABLE invoices ALTER COLUMN amount TYPE numeric(12, 2);")
    assert action.kind is AlterTableActionKind.ALTER_COLUMN_TYPE
    assert action.column == "amount"
    assert action.column_type == "numeric(12, 2)"
    assert action.has_using_expression is False


# ---------------------------------------------------------------------------
# PRIMARY KEY and UNIQUE
# ---------------------------------------------------------------------------


def test_add_primary_key_builds_index_in_place() -> None:
    action = only_action(
        """
        -- restore the primary key dropped during the id bigint swap
        ALTER TABLE users ADD PRIMARY KEY (id);
        """
    )
    assert action.kind is AlterTableActionKind.ADD_PRIMARY_KEY
    assert action.constraint_name is None  # Postgres names it users_pkey itself
    assert action.using_index is None
    assert action.not_valid is False


def test_add_primary_key_using_index_adopts_existing_index() -> None:
    action = only_action(
        """
        -- step 2 of the zero-downtime PK swap; users_pkey_new was built CONCURRENTLY
        ALTER TABLE public.users
            ADD CONSTRAINT users_pkey PRIMARY KEY USING INDEX users_pkey_new;
        """
    )
    assert action.kind is AlterTableActionKind.ADD_PRIMARY_KEY_USING_INDEX
    assert action.constraint_name == "users_pkey"
    assert action.using_index == "users_pkey_new"


@pytest.mark.parametrize(
    ("sql", "expected_kind", "using_index"),
    [
        (
            "ALTER TABLE users ADD CONSTRAINT users_email_key UNIQUE (email);",
            AlterTableActionKind.ADD_UNIQUE,
            None,
        ),
        (
            "ALTER TABLE users\n"
            "    ADD CONSTRAINT users_email_key UNIQUE USING INDEX users_email_uidx;",
            AlterTableActionKind.ADD_UNIQUE_USING_INDEX,
            "users_email_uidx",
        ),
    ],
)
def test_add_unique_with_and_without_using_index(
    sql: str, expected_kind: AlterTableActionKind, using_index: str | None
) -> None:
    action = only_action(sql)
    assert action.kind is expected_kind
    assert action.constraint_name == "users_email_key"
    assert action.using_index == using_index
    assert action.not_valid is False


# ---------------------------------------------------------------------------
# FOREIGN KEY, CHECK, VALIDATE
# ---------------------------------------------------------------------------

TWO_STEP_FK_MIGRATION = """-- 0102_orders_customer_fk.sql
BEGIN;

ALTER TABLE orders
    ADD CONSTRAINT orders_customer_id_fkey
    FOREIGN KEY (customer_id) REFERENCES customers (id)
    NOT VALID;

COMMIT;

-- validation scans under SHARE UPDATE EXCLUSIVE, outside the transaction
ALTER TABLE orders VALIDATE CONSTRAINT orders_customer_id_fkey;
"""


def test_two_step_foreign_key_rollout() -> None:
    script = parse(TWO_STEP_FK_MIGRATION)
    assert [statement.kind for statement in script.statements] == [
        StatementKind.BEGIN,
        StatementKind.ALTER_TABLE,
        StatementKind.COMMIT,
        StatementKind.ALTER_TABLE,
    ]

    add_fk = script.statements[1].alter_actions[0]
    assert add_fk.kind is AlterTableActionKind.ADD_FOREIGN_KEY_NOT_VALID
    assert add_fk.constraint_name == "orders_customer_id_fkey"
    assert add_fk.not_valid is True

    validate = script.statements[3]
    assert validate.span.line == 12
    assert validate.sql == "ALTER TABLE orders VALIDATE CONSTRAINT orders_customer_id_fkey"
    validate_action = validate.alter_actions[0]
    assert validate_action.kind is AlterTableActionKind.VALIDATE_CONSTRAINT
    assert validate_action.constraint_name == "orders_customer_id_fkey"
    assert validate_action.column is None  # the name is a constraint, not a column

    assert script.has_explicit_transactions
    explicit, implicit = script.transaction_groups
    assert explicit.explicit is True
    assert explicit.statement_indices == (1,)
    assert explicit.opened_by == 0
    assert explicit.closed_by == 2
    assert implicit.explicit is False
    assert implicit.statement_indices == (3,)


def test_plain_foreign_key_validates_and_locks_both_tables() -> None:
    action = only_action(
        """
        ALTER TABLE order_items
            ADD CONSTRAINT order_items_order_fkey
            FOREIGN KEY (order_id, line_no)
            REFERENCES order_lines (order_id, line_no)
            ON DELETE CASCADE;
        """
    )
    assert action.kind is AlterTableActionKind.ADD_FOREIGN_KEY
    assert action.constraint_name == "order_items_order_fkey"
    assert action.not_valid is False
    # ON DELETE CASCADE is a referential action, not the subcommand's CASCADE
    assert action.cascade is False


def test_add_check_anonymous_scans_immediately() -> None:
    action = only_action("ALTER TABLE invoices ADD CHECK (amount_cents > 0);")
    assert action.kind is AlterTableActionKind.ADD_CHECK
    assert action.constraint_name is None
    assert action.not_valid is False


def test_add_check_not_valid_skips_the_scan() -> None:
    action = only_action(
        """
        ALTER TABLE invoices
            ADD CONSTRAINT invoices_amount_positive
            CHECK (amount_cents > 0) NOT VALID;
        """
    )
    assert action.kind is AlterTableActionKind.ADD_CHECK_NOT_VALID
    assert action.constraint_name == "invoices_amount_positive"
    assert action.not_valid is True


# ---------------------------------------------------------------------------
# DROP CONSTRAINT, PG18 NOT NULL, EXCLUDE, ALTER CONSTRAINT
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sql", "cascade", "missing_ok"),
    [
        (
            "ALTER TABLE invoices DROP CONSTRAINT invoices_amount_check;",
            False,
            False,
        ),
        (
            "ALTER TABLE invoices DROP CONSTRAINT IF EXISTS invoices_amount_check;",
            False,
            True,
        ),
        (
            "ALTER TABLE invoices DROP CONSTRAINT invoices_amount_check CASCADE;",
            True,
            False,
        ),
    ],
)
def test_drop_constraint_variants(sql: str, cascade: bool, missing_ok: bool) -> None:
    action = only_action(sql)
    assert action.kind is AlterTableActionKind.DROP_CONSTRAINT
    assert action.constraint_name == "invoices_amount_check"
    assert action.column is None
    assert action.cascade is cascade
    assert action.missing_ok is missing_ok


def test_pg18_named_not_null_constraint() -> None:
    action = only_action(
        """
        -- PG18: named NOT NULL constraints can later be dropped by name
        ALTER TABLE users
            ADD CONSTRAINT users_email_not_null NOT NULL email;
        """
    )
    assert action.kind is AlterTableActionKind.ADD_NOT_NULL_CONSTRAINT
    assert action.column == "email"
    assert action.constraint_name == "users_email_not_null"


def test_exclusion_constraint_using_gist() -> None:
    action = only_action(
        """
        ALTER TABLE room_bookings
            ADD CONSTRAINT room_bookings_no_overlap
            EXCLUDE USING gist (room_id WITH =, during WITH &&);
        """
    )
    assert action.kind is AlterTableActionKind.ADD_EXCLUSION
    assert action.constraint_name == "room_bookings_no_overlap"
    assert action.not_valid is False
    assert action.using_index is None


def test_alter_constraint_deferrable() -> None:
    action = only_action(
        "ALTER TABLE orders ALTER CONSTRAINT orders_customer_id_fkey "
        "DEFERRABLE INITIALLY DEFERRED;"
    )
    assert action.kind is AlterTableActionKind.ALTER_CONSTRAINT
    assert action.constraint_name == "orders_customer_id_fkey"
    assert action.column is None


# ---------------------------------------------------------------------------
# Multi-subcommand ALTER TABLE and the ONLY / quoted-identifier forms
# ---------------------------------------------------------------------------


def test_checkout_hardening_mixes_subcommands_in_order() -> None:
    statement = only_statement(
        """
        -- 0087_orders_checkout_constraints.sql (hand-written, applied via psql)
        ALTER TABLE public.orders
            ADD COLUMN fulfilled_at timestamptz,
            ALTER COLUMN status SET DEFAULT 'pending',
            ALTER COLUMN status SET NOT NULL,
            ADD CONSTRAINT orders_customer_id_fkey
                FOREIGN KEY (customer_id) REFERENCES public.customers (id) NOT VALID,
            DROP CONSTRAINT IF EXISTS orders_status_check_old;
        """
    )
    assert statement.kind is StatementKind.ALTER_TABLE
    assert statement.targets == (QualifiedName(name="orders", schema="public"),)
    assert tuple(action.kind for action in statement.alter_actions) == (
        AlterTableActionKind.ADD_COLUMN,
        AlterTableActionKind.SET_COLUMN_DEFAULT,
        AlterTableActionKind.SET_NOT_NULL,
        AlterTableActionKind.ADD_FOREIGN_KEY_NOT_VALID,
        AlterTableActionKind.DROP_CONSTRAINT,
    )

    add_column, set_default, set_not_null, add_fk, drop_constraint = statement.alter_actions
    assert add_column.column == "fulfilled_at"
    assert add_column.column_type == "timestamptz"
    assert set_default.column == "status"
    assert set_default.default is not None
    assert set_default.default.volatility is Volatility.CONSTANT
    assert set_not_null.column == "status"
    assert add_fk.constraint_name == "orders_customer_id_fkey"
    assert add_fk.not_valid is True
    assert drop_constraint.constraint_name == "orders_status_check_old"
    assert drop_constraint.missing_ok is True


def test_alter_table_only_with_quoted_identifiers() -> None:
    statement = only_statement(
        'ALTER TABLE ONLY billing."Invoices"\n    ALTER COLUMN "dueDate" SET NOT NULL;'
    )
    assert statement.kind is StatementKind.ALTER_TABLE
    assert statement.node_tag == "AlterTableStmt"
    assert statement.sql.startswith("ALTER TABLE ONLY")
    assert statement.targets == (QualifiedName(name="Invoices", schema="billing"),)
    assert len(statement.alter_actions) == 1
    action = statement.alter_actions[0]
    assert action.kind is AlterTableActionKind.SET_NOT_NULL
    assert action.column == "dueDate"


# ---------------------------------------------------------------------------
# Statistics, storage, and reloption forms
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sql", "expected_kind"),
    [
        (
            "ALTER TABLE audit_log ALTER COLUMN payload SET STATISTICS 1000;",
            AlterTableActionKind.SET_STATISTICS,
        ),
        (
            "ALTER TABLE audit_log ALTER COLUMN payload SET STORAGE EXTERNAL;",
            AlterTableActionKind.SET_STORAGE,
        ),
    ],
)
def test_per_column_statistics_and_storage(
    sql: str, expected_kind: AlterTableActionKind
) -> None:
    action = only_action(sql)
    assert action.kind is expected_kind
    assert action.column == "payload"
    assert action.constraint_name is None


@pytest.mark.parametrize(
    ("sql", "expected_kind", "column"),
    [
        (
            "ALTER TABLE users ALTER COLUMN email SET (n_distinct = -1);",
            AlterTableActionKind.SET_COLUMN_OPTIONS,
            "email",
        ),
        (
            "ALTER TABLE users ALTER COLUMN email RESET (n_distinct);",
            AlterTableActionKind.RESET_COLUMN_OPTIONS,
            "email",
        ),
        (
            "ALTER TABLE audit_log SET (fillfactor = 70);",
            AlterTableActionKind.SET_STORAGE_PARAMS,
            None,
        ),
        (
            "ALTER TABLE audit_log RESET (fillfactor);",
            AlterTableActionKind.RESET_STORAGE_PARAMS,
            None,
        ),
    ],
)
def test_column_options_are_distinct_from_table_storage_params(
    sql: str, expected_kind: AlterTableActionKind, column: str | None
) -> None:
    action = only_action(sql)
    assert action.kind is expected_kind
    assert action.column == column
