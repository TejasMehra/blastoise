"""Whole-file behavior: multi-statement files, transactions, DO blocks, spans, errors."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from helpers import only_statement, parse

from pgverdict import (
    AlterTableAction,
    AlterTableActionKind,
    CreateTableDetails,
    DefaultInfo,
    DoBlockDetails,
    IndexDetails,
    MigrationParseError,
    QualifiedName,
    RenameDetails,
    StatementKind,
    TransactionDetails,
    TransactionGroup,
    Volatility,
    parse_migration,
    parse_migration_file,
)

# ---------------------------------------------------------------------------
# Multi-statement files: order, spans, statement text
# ---------------------------------------------------------------------------

ORDERS_MIGRATION = """\
-- 0004_orders.sql
-- Adds the orders table, an index on customer_id, and a NOT VALID FK.

CREATE TABLE orders (
    id bigserial PRIMARY KEY,
    customer_id bigint NOT NULL,
    total_cents integer NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX CONCURRENTLY idx_orders_customer_id
    ON orders (customer_id);

-- validated in a follow-up migration
ALTER TABLE orders
    ADD CONSTRAINT orders_customer_id_fkey
    FOREIGN KEY (customer_id) REFERENCES customers (id) NOT VALID;

UPDATE orders SET total_cents = 0 WHERE total_cents IS NULL;
"""


def test_realistic_migration_statement_order_and_kinds() -> None:
    script = parse(ORDERS_MIGRATION)
    assert [s.kind for s in script.statements] == [
        StatementKind.CREATE_TABLE,
        StatementKind.CREATE_INDEX_CONCURRENTLY,
        StatementKind.ALTER_TABLE,
        StatementKind.UPDATE,
    ]
    create, index, alter, update = script.statements

    assert create.targets == (QualifiedName("orders"),)
    assert create.details == CreateTableDetails(persistence="permanent")

    assert index.targets == (QualifiedName("orders"),)
    assert index.details == IndexDetails(index_name="idx_orders_customer_id")

    assert alter.targets == (QualifiedName("orders"),)
    assert alter.alter_actions == (
        AlterTableAction(
            kind=AlterTableActionKind.ADD_FOREIGN_KEY_NOT_VALID,
            constraint_name="orders_customer_id_fkey",
            not_valid=True,
            referenced_table=QualifiedName("customers"),
        ),
    )

    assert update.targets == (QualifiedName("orders"),)


def test_realistic_migration_spans_and_sql_slices() -> None:
    script = parse(ORDERS_MIGRATION)
    assert [s.span.line for s in script.statements] == [4, 11, 15, 19]
    for statement in script.statements:
        assert not statement.sql.endswith(";")
        # The span slices exactly the statement text: pglast puts the start
        # of the span at the statement's first token, so leading comments and
        # blank lines are excluded.
        assert script.source[statement.span.start : statement.span.end] == statement.sql
    assert script.statements[2].sql == (
        "ALTER TABLE orders\n"
        "    ADD CONSTRAINT orders_customer_id_fkey\n"
        "    FOREIGN KEY (customer_id) REFERENCES customers (id) NOT VALID"
    )


def test_file_without_transaction_statements_is_one_implicit_group() -> None:
    script = parse(ORDERS_MIGRATION)
    assert script.transaction_groups == (
        TransactionGroup(explicit=False, statement_indices=(0, 1, 2, 3)),
    )
    assert script.has_explicit_transactions is False
    assert script.warnings == ()


def test_leading_comments_excluded_from_statement_slice() -> None:
    source = "-- add soft-delete support\nALTER TABLE users ADD COLUMN deleted_at timestamptz;\n"
    statement = only_statement(source)
    assert statement.sql == "ALTER TABLE users ADD COLUMN deleted_at timestamptz"
    assert statement.span.line == 2
    assert source[statement.span.start : statement.span.end] == statement.sql


def test_multiple_statements_on_one_line() -> None:
    source = 'TRUNCATE sessions; ALTER TABLE app."OrderItems" RENAME COLUMN "SKU" TO sku;'
    script = parse(source)
    first, second = script.statements
    assert first.kind is StatementKind.TRUNCATE
    assert first.targets == (QualifiedName("sessions"),)
    assert first.span.line == 1
    assert first.sql == "TRUNCATE sessions"

    assert second.kind is StatementKind.RENAME_COLUMN
    assert second.span.line == 1
    assert second.span.start > first.span.end
    assert second.sql == 'ALTER TABLE app."OrderItems" RENAME COLUMN "SKU" TO sku'
    assert second.targets == (QualifiedName("OrderItems", "app"),)
    assert second.details == RenameDetails(
        old_name="SKU", new_name="sku", object_type="OBJECT_COLUMN"
    )


@pytest.mark.parametrize(
    "source",
    [
        "",
        "   \n\n",
        "-- placeholder migration, nothing to do in this release\n",
        "/* reserved: schema change shipped in 0031 instead */\n",
    ],
)
def test_empty_and_comment_only_files(source: str) -> None:
    script = parse(source)
    assert script.statements == ()
    assert script.transaction_groups == ()
    assert script.warnings == ()


# ---------------------------------------------------------------------------
# Explicit transaction blocks
# ---------------------------------------------------------------------------

INVOICES_MIGRATION = """\
-- V7__tighten_invoices.sql
BEGIN;

ALTER TABLE public.invoices
    ADD COLUMN IF NOT EXISTS reconciled boolean NOT NULL DEFAULT false;

ALTER TABLE public.invoices
    ADD CONSTRAINT invoices_amount_positive CHECK (amount_cents >= 0) NOT VALID;

COMMIT;
"""


def test_explicit_begin_commit_grouping() -> None:
    script = parse(INVOICES_MIGRATION)
    assert [s.kind for s in script.statements] == [
        StatementKind.BEGIN,
        StatementKind.ALTER_TABLE,
        StatementKind.ALTER_TABLE,
        StatementKind.COMMIT,
    ]
    assert [s.span.line for s in script.statements] == [2, 4, 7, 10]
    assert script.transaction_groups == (
        TransactionGroup(explicit=True, statement_indices=(1, 2), opened_by=0, closed_by=3),
    )
    assert script.has_explicit_transactions is True
    assert script.warnings == ()

    add_column = script.statements[1].alter_actions[0]
    assert add_column == AlterTableAction(
        kind=AlterTableActionKind.ADD_COLUMN_DEFAULT_NONVOLATILE,
        column="reconciled",
        column_type="boolean",
        default=DefaultInfo(volatility=Volatility.CONSTANT, expression="FALSE"),
        not_null=True,
        missing_ok=True,
    )
    add_check = script.statements[2].alter_actions[0]
    assert add_check.kind is AlterTableActionKind.ADD_CHECK_NOT_VALID
    assert add_check.constraint_name == "invoices_amount_positive"
    assert add_check.not_valid is True


def test_rolled_back_block_is_marked() -> None:
    script = parse(
        "BEGIN;\nUPDATE accounts SET plan = 'free' WHERE plan IS NULL;\nROLLBACK;\n"
    )
    assert [s.kind for s in script.statements] == [
        StatementKind.BEGIN,
        StatementKind.UPDATE,
        StatementKind.ROLLBACK,
    ]
    assert script.transaction_groups == (
        TransactionGroup(
            explicit=True,
            statement_indices=(1,),
            opened_by=0,
            closed_by=2,
            rolled_back=True,
        ),
    )
    assert script.warnings == ()


CITEXT_MIGRATION = """\
-- 0012_citext_emails.sql
CREATE EXTENSION IF NOT EXISTS citext;

BEGIN;
ALTER TABLE users ALTER COLUMN email TYPE citext USING email::citext;
ALTER TABLE users ADD CONSTRAINT users_email_key UNIQUE USING INDEX users_email_idx;
COMMIT;

ANALYZE users;
"""


def test_statements_around_explicit_block_form_implicit_groups() -> None:
    script = parse(CITEXT_MIGRATION)
    assert [s.kind for s in script.statements] == [
        StatementKind.CREATE_EXTENSION,
        StatementKind.BEGIN,
        StatementKind.ALTER_TABLE,
        StatementKind.ALTER_TABLE,
        StatementKind.COMMIT,
        StatementKind.ANALYZE,
    ]
    assert script.transaction_groups == (
        TransactionGroup(explicit=False, statement_indices=(0,)),
        TransactionGroup(explicit=True, statement_indices=(2, 3), opened_by=1, closed_by=4),
        TransactionGroup(explicit=False, statement_indices=(5,)),
    )
    assert script.has_explicit_transactions is True

    alter_type = script.statements[2].alter_actions[0]
    assert alter_type.kind is AlterTableActionKind.ALTER_COLUMN_TYPE
    assert alter_type.column == "email"
    assert alter_type.column_type == "citext"
    assert alter_type.has_using_expression is True

    add_unique = script.statements[3].alter_actions[0]
    assert add_unique.kind is AlterTableActionKind.ADD_UNIQUE_USING_INDEX
    assert add_unique.using_index == "users_email_idx"
    assert add_unique.constraint_name == "users_email_key"

    assert script.statements[5].targets == (QualifiedName("users"),)


@pytest.mark.parametrize(
    ("open_sql", "close_sql"),
    [
        ("BEGIN", "COMMIT"),
        ("START TRANSACTION", "COMMIT"),
        ("BEGIN", "END"),
    ],
)
def test_transaction_keyword_aliases(open_sql: str, close_sql: str) -> None:
    source = f"{open_sql};\nCREATE SEQUENCE billing.invoice_number_seq;\n{close_sql};\n"
    script = parse(source)
    assert [s.kind for s in script.statements] == [
        StatementKind.BEGIN,
        StatementKind.CREATE_SEQUENCE,
        StatementKind.COMMIT,
    ]
    assert script.transaction_groups == (
        TransactionGroup(explicit=True, statement_indices=(1,), opened_by=0, closed_by=2),
    )
    assert script.has_explicit_transactions is True
    assert script.warnings == ()
    assert script.statements[1].targets == (QualifiedName("invoice_number_seq", "billing"),)


SAVEPOINT_MIGRATION = """\
BEGIN;
SAVEPOINT before_backfill;
UPDATE invoices SET amount_cents = 0 WHERE amount_cents IS NULL;
ROLLBACK TO SAVEPOINT before_backfill;
RELEASE SAVEPOINT before_backfill;
COMMIT;
"""


def test_savepoint_forms_are_distinct_and_stay_in_group_body() -> None:
    script = parse(SAVEPOINT_MIGRATION)
    assert [s.kind for s in script.statements] == [
        StatementKind.BEGIN,
        StatementKind.SAVEPOINT,
        StatementKind.UPDATE,
        StatementKind.ROLLBACK_TO_SAVEPOINT,
        StatementKind.RELEASE_SAVEPOINT,
        StatementKind.COMMIT,
    ]
    assert script.transaction_groups == (
        TransactionGroup(
            explicit=True, statement_indices=(1, 2, 3, 4), opened_by=0, closed_by=5
        ),
    )
    assert script.warnings == ()
    for index in (1, 3, 4):
        assert script.statements[index].details == TransactionDetails(
            savepoint_name="before_backfill"
        )


# ---------------------------------------------------------------------------
# Transaction warnings
# ---------------------------------------------------------------------------


def test_commit_without_begin_warns_with_line() -> None:
    script = parse("ALTER TABLE users ADD COLUMN nickname text;\nCOMMIT;\n")
    assert [s.kind for s in script.statements] == [
        StatementKind.ALTER_TABLE,
        StatementKind.COMMIT,
    ]
    assert len(script.warnings) == 1
    assert "line 2" in script.warnings[0]
    assert "COMMIT without a matching BEGIN" in script.warnings[0]
    # The stray COMMIT belongs to no group.
    assert script.transaction_groups == (
        TransactionGroup(explicit=False, statement_indices=(0,)),
    )


NESTED_BEGIN_MIGRATION = """\
BEGIN;
CREATE TABLE audit_log (id bigserial PRIMARY KEY);
BEGIN;  -- oops: already inside a transaction
CREATE INDEX idx_audit_log_id ON audit_log (id);
COMMIT;
"""


def test_nested_begin_warns_and_is_excluded_from_body() -> None:
    script = parse(NESTED_BEGIN_MIGRATION)
    assert len(script.warnings) == 1
    assert "line 3" in script.warnings[0]
    assert "BEGIN inside an already-open" in script.warnings[0]
    assert script.transaction_groups == (
        TransactionGroup(explicit=True, statement_indices=(1, 3), opened_by=0, closed_by=4),
    )


def test_unclosed_begin_warns_with_its_line() -> None:
    script = parse(
        "CREATE TABLE payments (id bigserial PRIMARY KEY);\n"
        "BEGIN;\n"
        "ALTER TABLE payments ADD COLUMN provider text;\n"
    )
    assert len(script.warnings) == 1
    assert "line 2" in script.warnings[0]
    assert "never closed" in script.warnings[0]
    assert script.transaction_groups == (
        TransactionGroup(explicit=False, statement_indices=(0,)),
        TransactionGroup(explicit=True, statement_indices=(2,), opened_by=1, closed_by=None),
    )


# ---------------------------------------------------------------------------
# Errors and file reading
# ---------------------------------------------------------------------------


def test_parse_error_carries_path_and_line() -> None:
    bad = "CREATE TABLE ok (id int);\n\nALTER TABLE ok FROB COLUMN nope;\n"
    with pytest.raises(MigrationParseError) as excinfo:
        parse_migration(bad, path="migrations/0007_bad.sql")
    error = excinfo.value
    assert error.path == "migrations/0007_bad.sql"
    assert "migrations/0007_bad.sql" in str(error)
    # Documented contract: the error carries the 1-based line of the failure.
    assert error.line == 3
    assert "migrations/0007_bad.sql:3" in str(error)


def test_parse_migration_file_tolerates_utf8_bom(tmp_path: Path) -> None:
    migration = tmp_path / "0001_create_users.sql"
    sql = "CREATE TABLE users (\n    id bigint PRIMARY KEY,\n    email text NOT NULL\n);\n"
    migration.write_bytes(b"\xef\xbb\xbf" + sql.encode("utf-8"))
    script = parse_migration_file(migration)
    assert script.path == str(migration)
    assert [s.kind for s in script.statements] == [StatementKind.CREATE_TABLE]
    assert script.statements[0].span.line == 1
    assert script.statements[0].targets == (QualifiedName("users"),)


# ---------------------------------------------------------------------------
# DO blocks
# ---------------------------------------------------------------------------


def _do_details(source: str) -> DoBlockDetails:
    statement = only_statement(source)
    assert statement.kind is StatementKind.DO_BLOCK
    assert statement.node_tag == "DoStmt"
    details = statement.details
    assert isinstance(details, DoBlockDetails)
    return details


def test_do_block_single_update_is_extracted() -> None:
    source = """\
DO $$
BEGIN
    UPDATE users
       SET email = lower(email)
     WHERE email <> lower(email);
END
$$;
"""
    statement = only_statement(source)
    details = statement.details
    assert isinstance(details, DoBlockDetails)
    assert details.language == "plpgsql"
    assert details.fully_parsed is True
    assert details.dynamic_sql_count == 0
    assert len(details.statements) == 1
    inner = details.statements[0]
    assert inner.kind is StatementKind.UPDATE
    assert inner.targets == (QualifiedName("users"),)
    # Inner statements inherit the span of the enclosing DO statement.
    assert inner.span == statement.span


def test_do_block_with_multiple_statements_including_ddl() -> None:
    details = _do_details(
        """\
DO $$
BEGIN
    ALTER TABLE invoices ADD COLUMN paid_at timestamptz;
    CREATE INDEX idx_invoices_paid_at ON invoices (paid_at);
    UPDATE invoices SET paid_at = created_at WHERE status = 'paid';
END
$$;
"""
    )
    assert details.fully_parsed is True
    assert [s.kind for s in details.statements] == [
        StatementKind.ALTER_TABLE,
        StatementKind.CREATE_INDEX,
        StatementKind.UPDATE,
    ]
    alter, index, update = details.statements
    assert alter.sql == "ALTER TABLE invoices ADD COLUMN paid_at timestamptz"
    assert alter.alter_actions[0].kind is AlterTableActionKind.ADD_COLUMN
    assert alter.alter_actions[0].column == "paid_at"
    assert index.details == IndexDetails(index_name="idx_invoices_paid_at")
    assert {s.targets[0] for s in (alter, index, update)} == {QualifiedName("invoices")}


def test_do_block_dynamic_execute_is_counted_not_classified() -> None:
    details = _do_details(
        """\
DO $$
BEGIN
    EXECUTE format(
        'CREATE INDEX %I ON orders (customer_id)',
        'idx_orders_customer_id'
    );
    UPDATE orders SET status = 'new' WHERE status IS NULL;
END
$$;
"""
    )
    assert details.dynamic_sql_count == 1
    assert details.fully_parsed is True
    assert [s.kind for s in details.statements] == [StatementKind.UPDATE]


def test_do_block_if_else_collects_both_branches() -> None:
    details = _do_details(
        """\
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name = 'invoices' AND column_name = 'paid_at') THEN
        UPDATE invoices SET paid_at = created_at WHERE paid_at IS NULL;
    ELSE
        ALTER TABLE invoices ADD COLUMN paid_at timestamptz;
    END IF;
END
$$;
"""
    )
    assert details.fully_parsed is True
    # Both branch bodies are collected; the IF condition is an expression
    # fragment (parseMode != 0), not a statement, so it is not.
    assert [s.kind for s in details.statements] == [
        StatementKind.UPDATE,
        StatementKind.ALTER_TABLE,
    ]
    assert details.statements[0].targets == (QualifiedName("invoices"),)
    assert details.statements[1].alter_actions[0].kind is AlterTableActionKind.ADD_COLUMN


def test_do_block_for_loop_query_is_collected() -> None:
    details = _do_details(
        """\
DO $$
DECLARE
    r record;
BEGIN
    FOR r IN SELECT id FROM audit_log WHERE archived = false LOOP
        DELETE FROM audit_log WHERE id = r.id;
    END LOOP;
END
$$;
"""
    )
    assert details.fully_parsed is True
    assert len(details.statements) == 2
    kinds = {s.kind for s in details.statements}
    assert kinds == {StatementKind.DELETE, StatementKind.SELECT}
    select = next(s for s in details.statements if s.kind is StatementKind.SELECT)
    assert select.sql == "SELECT id FROM audit_log WHERE archived = false"


def test_do_block_with_explicit_language_clause() -> None:
    details = _do_details(
        "DO LANGUAGE plpgsql $$\n"
        "BEGIN\n"
        "    INSERT INTO audit_log (event) VALUES ('migrated');\n"
        "END\n"
        "$$;"
    )
    assert details.language == "plpgsql"
    assert details.fully_parsed is True
    assert [s.kind for s in details.statements] == [StatementKind.INSERT]
    assert details.statements[0].targets == (QualifiedName("audit_log"),)


def test_do_block_with_invalid_plpgsql_body_does_not_raise() -> None:
    script = parse("DO $$ THIS IS DEFINITELY NOT PLPGSQL $$;")
    assert len(script.statements) == 1
    statement = script.statements[0]
    assert statement.kind is StatementKind.DO_BLOCK
    details = statement.details
    assert isinstance(details, DoBlockDetails)
    assert details.fully_parsed is False
    assert details.statements == ()
    assert details.dynamic_sql_count == 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

CLI_MIGRATION = """\
CREATE TABLE customers (
    id bigserial PRIMARY KEY,
    name text NOT NULL
);
ALTER TABLE customers ADD COLUMN vip boolean NOT NULL DEFAULT false;
"""


def _run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pgverdict.cli", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


def test_cli_parse_prints_kinds(tmp_path: Path) -> None:
    migration = tmp_path / "0002_customers.sql"
    migration.write_text(CLI_MIGRATION, encoding="utf-8")
    proc = _run_cli("parse", str(migration))
    assert proc.returncode == 0, proc.stderr
    assert str(migration) in proc.stdout
    assert "create_table" in proc.stdout
    assert "alter_table" in proc.stdout
    assert "add_column_default_nonvolatile" in proc.stdout
    assert "implicit group of 2 statement(s)" in proc.stdout


def test_cli_parse_json_output(tmp_path: Path) -> None:
    migration = tmp_path / "0002_customers.sql"
    migration.write_text(CLI_MIGRATION, encoding="utf-8")
    proc = _run_cli("parse", "--json", str(migration))
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert isinstance(payload, list)
    assert len(payload) == 1
    script_obj = payload[0]
    assert "source" not in script_obj
    assert script_obj["path"] == str(migration)
    kinds = [s["kind"] for s in script_obj["statements"]]
    assert kinds == ["create_table", "alter_table"]
    action = script_obj["statements"][1]["alter_actions"][0]
    assert action["kind"] == "add_column_default_nonvolatile"
    assert action["default"]["volatility"] == "constant"


def test_cli_parse_error_exits_2(tmp_path: Path) -> None:
    migration = tmp_path / "0003_broken.sql"
    migration.write_text("ALTER TABLE users FROB COLUMN nope;\n", encoding="utf-8")
    proc = _run_cli("parse", str(migration))
    assert proc.returncode == 2
    assert "error:" in proc.stderr
