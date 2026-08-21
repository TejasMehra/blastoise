"""Regression tests for defects found during adversarial verification.

Each test pins a fix for a confirmed bug or taxonomy gap: chained
transactions, boolean utility options, NUL/encoding handling, multi-byte
parse-error offsets, DEFAULT NULL semantics, foreign tables, COPY direction,
the ONLY keyword, and volatility-table corrections.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from helpers import only_action, only_statement, parse

from pgverdict import (
    AlterTableActionKind,
    CreateTableDetails,
    MigrationParseError,
    QualifiedName,
    StatementKind,
    TransactionGroup,
    Volatility,
    parse_migration,
    parse_migration_file,
)

# ---------------------------------------------------------------------------
# COMMIT/ROLLBACK AND CHAIN open a new explicit transaction
# ---------------------------------------------------------------------------


def test_commit_and_chain_opens_a_new_explicit_group() -> None:
    script = parse(
        """
        BEGIN;
        CREATE TABLE a (i int);
        COMMIT AND CHAIN;
        CREATE TABLE b (i int);
        COMMIT;
        """
    )
    assert script.warnings == ()
    assert script.transaction_groups == (
        TransactionGroup(explicit=True, statement_indices=(1,), opened_by=0, closed_by=2),
        TransactionGroup(explicit=True, statement_indices=(3,), opened_by=2, closed_by=4),
    )


def test_rollback_and_chain_opens_a_new_explicit_group() -> None:
    script = parse(
        """
        BEGIN;
        UPDATE flags SET enabled = false;
        ROLLBACK AND CHAIN;
        UPDATE flags SET enabled = true;
        COMMIT;
        """
    )
    assert script.warnings == ()
    first, second = script.transaction_groups
    assert first.rolled_back is True
    assert second.explicit is True
    assert second.opened_by == 2
    assert second.closed_by == 4


# ---------------------------------------------------------------------------
# Hostile input handling
# ---------------------------------------------------------------------------


def test_nul_byte_raises_instead_of_silently_truncating() -> None:
    source = "SELECT 1;\x00DROP TABLE users;"
    with pytest.raises(MigrationParseError) as excinfo:
        parse_migration(source, path="0001_bad.sql")
    assert excinfo.value.location == 9
    assert excinfo.value.line == 1
    assert "NUL" in excinfo.value.message


def test_non_utf8_file_raises_migration_parse_error(tmp_path: Path) -> None:
    bad = tmp_path / "latin1.sql"
    bad.write_bytes(b"-- caf\xe9\nSELECT 1;\n")
    with pytest.raises(MigrationParseError) as excinfo:
        parse_migration_file(bad)
    assert "not valid UTF-8" in excinfo.value.message
    assert excinfo.value.path == str(bad)


def test_parse_error_offset_survives_multibyte_characters() -> None:
    source = "SELECT '\U0001f642\U0001f642\U0001f642';\nSELEC 2;"
    with pytest.raises(MigrationParseError) as excinfo:
        parse_migration(source)
    location = excinfo.value.location
    assert location is not None
    assert source[location:].startswith("SELEC 2")
    assert excinfo.value.line == 2


# ---------------------------------------------------------------------------
# Boolean utility options: an explicit FALSE/OFF must not read as enabled
# (REINDEX/VACUUM variants are pinned in test_indexes_maintenance.py)
# ---------------------------------------------------------------------------


def test_add_column_default_null_with_not_null_is_the_failing_form() -> None:
    action = only_action(
        "ALTER TABLE orders ADD COLUMN priority integer DEFAULT NULL NOT NULL;"
    )
    # A NULL default provides no fill value: this fails on non-empty tables
    # exactly like NOT NULL without a default, despite the DEFAULT clause.
    assert action.kind is AlterTableActionKind.ADD_COLUMN_NOT_NULL_NO_DEFAULT
    assert action.not_null is True
    assert action.default is not None
    assert action.default.volatility is Volatility.CONSTANT


def test_add_column_casted_null_default_with_not_null_is_the_failing_form() -> None:
    action = only_action(
        "ALTER TABLE orders ADD COLUMN note text DEFAULT NULL::text NOT NULL;"
    )
    assert action.kind is AlterTableActionKind.ADD_COLUMN_NOT_NULL_NO_DEFAULT


def test_add_column_default_null_without_not_null_stays_nonvolatile() -> None:
    action = only_action("ALTER TABLE orders ADD COLUMN note text DEFAULT NULL;")
    assert action.kind is AlterTableActionKind.ADD_COLUMN_DEFAULT_NONVOLATILE


# ---------------------------------------------------------------------------
# Foreign tables and COPY direction
# ---------------------------------------------------------------------------


def test_create_foreign_table_is_classified_with_its_target() -> None:
    statement = only_statement(
        "CREATE FOREIGN TABLE staging.remote_orders (id bigint, total numeric)\n"
        "    SERVER analytics_fdw OPTIONS (table_name 'orders');"
    )
    assert statement.kind is StatementKind.CREATE_FOREIGN_TABLE
    assert statement.node_tag == "CreateForeignTableStmt"
    assert statement.targets == (QualifiedName("remote_orders", schema="staging"),)
    assert statement.details == CreateTableDetails(persistence="permanent")


def test_copy_from_and_copy_to_get_distinct_kinds() -> None:
    copy_in = only_statement("COPY audit_log FROM STDIN WITH (FORMAT csv);")
    assert copy_in.kind is StatementKind.COPY_FROM
    copy_out = only_statement("COPY audit_log TO STDOUT;")
    assert copy_out.kind is StatementKind.COPY_TO
    assert copy_out.targets == (QualifiedName("audit_log"),)
    query_out = only_statement("COPY (SELECT id FROM users) TO STDOUT;")
    assert query_out.kind is StatementKind.COPY_TO
    assert query_out.targets == ()


# ---------------------------------------------------------------------------
# The ONLY keyword: recursion into partitions/children is lock-relevant
# ---------------------------------------------------------------------------


def test_alter_table_only_is_recorded() -> None:
    statement = only_statement("ALTER TABLE ONLY events ADD CONSTRAINT ck CHECK (n > 0);")
    assert statement.only is True
    plain = only_statement("ALTER TABLE events ADD CONSTRAINT ck CHECK (n > 0);")
    assert plain.only is False


def test_create_index_on_only_parent_is_recorded() -> None:
    statement = only_statement("CREATE INDEX ix_events_at ON ONLY events (occurred_at);")
    assert statement.only is True
    plain = only_statement("CREATE INDEX ix_events_at ON events (occurred_at);")
    assert plain.only is False


# ---------------------------------------------------------------------------
# Misc classification corrections
# ---------------------------------------------------------------------------


def test_schema_qualified_serial_is_a_real_type_not_a_serial_column() -> None:
    # Postgres expands serial pseudo-types only for unqualified names.
    action = only_action("ALTER TABLE t ADD COLUMN c myschema.serial;")
    assert action.kind is AlterTableActionKind.ADD_COLUMN


def test_pg18_not_null_constraint_not_valid_gets_its_own_kind() -> None:
    action = only_action("ALTER TABLE users ADD CONSTRAINT nn NOT NULL email NOT VALID;")
    assert action.kind is AlterTableActionKind.ADD_NOT_NULL_CONSTRAINT_NOT_VALID
    assert action.column == "email"
    assert action.not_valid is True


# ---------------------------------------------------------------------------
# Volatility corrections
# ---------------------------------------------------------------------------


def _default_volatility(expr: str) -> Volatility:
    action = only_action(f"ALTER TABLE t ADD COLUMN c text DEFAULT {expr};")
    assert action.default is not None
    return action.default.volatility


def test_array_to_string_is_stable_not_immutable() -> None:
    assert _default_volatility("array_to_string(ARRAY['a', 'b'], ',')") is Volatility.STABLE


def test_crypt_with_fixed_salt_is_immutable() -> None:
    # gen_salt is the volatile half of the pgcrypto pair.
    assert _default_volatility("crypt('seed', 'fixed-salt')") is Volatility.IMMUTABLE
    assert _default_volatility("crypt('seed', gen_salt('bf'))") is Volatility.VOLATILE


def test_at_time_zone_default_is_nonvolatile() -> None:
    action = only_action(
        "ALTER TABLE orders ADD COLUMN created_utc timestamp"
        " DEFAULT (now() AT TIME ZONE 'utc');"
    )
    assert action.kind is AlterTableActionKind.ADD_COLUMN_DEFAULT_NONVOLATILE
    assert action.default is not None
    assert action.default.volatility is Volatility.STABLE


def test_double_cast_of_now_literal_is_not_constant() -> None:
    # ('now'::text)::timestamptz is the documented idiom for run-time
    # evaluation; only the single cast of the untyped literal is folded
    # at ALTER time.
    assert _default_volatility("'now'::timestamptz") is Volatility.CONSTANT
    assert _default_volatility("('now'::text)::timestamptz") is not Volatility.CONSTANT


def test_unknown_operators_are_unknown_volatility() -> None:
    # User-defined operators can wrap volatile functions.
    assert _default_volatility("(1 === 2)") is Volatility.UNKNOWN
    assert _default_volatility("(1 OPERATOR(myschema.+) 2)") is Volatility.UNKNOWN
    assert _default_volatility("(1 + 2)") is Volatility.IMMUTABLE


# ---------------------------------------------------------------------------
# Direct unit tests for defensive branches of private helpers
# ---------------------------------------------------------------------------


def test_refine_offset_keeps_offset_when_message_names_no_token() -> None:
    from pgverdict.parser import _refine_offset

    assert _refine_offset("SELECT 1", 3, "syntax error at end of input") == 3


def test_defelem_enabled_handles_every_argument_shape() -> None:
    # Utility options reach us as String args from parsed SQL; the Boolean
    # and fallback arms guard against other pglast representations.
    from pglast import ast

    from pgverdict.classify import _defelem_enabled

    assert _defelem_enabled(ast.DefElem(defname="full", arg=ast.Boolean(boolval=True)))
    assert not _defelem_enabled(ast.DefElem(defname="full", arg=ast.Boolean(boolval=False)))
    assert _defelem_enabled(ast.DefElem(defname="full", arg=ast.Float(fval="1.0")))
