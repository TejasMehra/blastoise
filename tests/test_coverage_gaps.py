"""Dispatch arms and volatility branches not exercised by the main suite.

Volatility cases are driven through ``ADD COLUMN ... DEFAULT <expr>`` so the
whole classify pipeline runs; the few nodes a DEFAULT clause cannot hold
syntactically are fed to ``expression_volatility`` directly.
"""

from __future__ import annotations

import pytest
from helpers import only_action, only_statement, parse
from pglast import ast

from pgverdict import (
    AlterTableActionKind,
    DoBlockDetails,
    DropDetails,
    MigrationParseError,
    QualifiedName,
    RenameDetails,
    StatementKind,
    TransactionDetails,
    Volatility,
    parse_migration,
)
from pgverdict.volatility import expression_volatility

# ---------------------------------------------------------------------------
# Volatility of DEFAULT expressions, through the full parse pipeline
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("default_sql", "volatility"),
    [
        # CASE: searched form (no operand), constant arms
        ("CASE WHEN true THEN 1 ELSE 2 END", Volatility.IMMUTABLE),
        # CASE: operand form with a volatile result arm
        ("CASE length('xy') WHEN 2 THEN random() ELSE 0 END", Volatility.VOLATILE),
        ("COALESCE(NULL, now())", Volatility.STABLE),
        ("GREATEST(1, 2)", Volatility.IMMUTABLE),
        ("LEAST(1, random())", Volatility.VOLATILE),
        ("NULLIF('a', 'b')", Volatility.IMMUTABLE),
        ("(true AND false)", Volatility.IMMUTABLE),
        ("(random() > 0.5 OR false)", Volatility.VOLATILE),
        ("(1 IS NULL)", Volatility.IMMUTABLE),
        ("(now() IS NULL)", Volatility.STABLE),
        ("(true IS TRUE)", Volatility.IMMUTABLE),
        ("ROW(1, now())", Volatility.STABLE),
        ("ARRAY[1, 2, 3]", Volatility.IMMUTABLE),
        ("ARRAY[random()]", Volatility.VOLATILE),
        # cast of a non-constant argument keeps the payload's volatility
        ("(now()::date)", Volatility.STABLE),
        # volatile argument inside a stable/immutable function wins
        ("lower(gen_random_uuid()::text)", Volatility.VOLATILE),
        # schema qualification is ignored when matching the allowlists
        ("pg_catalog.now()", Volatility.STABLE),
        # SQLValueFunction, not a FuncCall
        ("CURRENT_TIMESTAMP", Volatility.STABLE),
        ("abs(2)", Volatility.IMMUTABLE),
        # IN-list: the right-hand side is a bare tuple of expressions
        ("(1 IN (1, 2))", Volatility.IMMUTABLE),
        # a column reference parses in a DEFAULT even though it is rejected
        # later at analysis time; it adds no volatility of its own
        ("(other_col + 1)", Volatility.IMMUTABLE),
        # a scalar subquery also parses in a DEFAULT; SubLink is unrecognized
        ("(SELECT 1)", Volatility.UNKNOWN),
    ],
)
def test_default_expression_volatility(default_sql: str, volatility: Volatility) -> None:
    action = only_action(f"ALTER TABLE t ADD COLUMN c integer DEFAULT {default_sql};")
    assert action.default is not None, default_sql
    assert action.default.volatility is volatility
    expected_kind = {
        Volatility.VOLATILE: AlterTableActionKind.ADD_COLUMN_DEFAULT_VOLATILE,
        Volatility.UNKNOWN: AlterTableActionKind.ADD_COLUMN_DEFAULT_UNKNOWN_VOLATILITY,
    }.get(volatility, AlterTableActionKind.ADD_COLUMN_DEFAULT_NONVOLATILE)
    assert action.kind is expected_kind


def test_expression_volatility_of_missing_node_is_constant() -> None:
    assert expression_volatility(None) is Volatility.CONSTANT


def test_function_call_without_a_name_is_unknown() -> None:
    # No SQL text can produce a FuncCall with an empty name list; the guard
    # exists for robustness against hand-built or partially copied trees.
    assert expression_volatility(ast.FuncCall(funcname=())) is Volatility.UNKNOWN


def test_sequence_of_expressions_combines_worst_member() -> None:
    volatile_call = ast.FuncCall(funcname=(ast.String(sval="random"),))
    assert expression_volatility([None, volatile_call]) is Volatility.VOLATILE


# ---------------------------------------------------------------------------
# Session, server, and database-level statements
# ---------------------------------------------------------------------------


def test_show_statement() -> None:
    statement = only_statement("SHOW search_path;")
    assert statement.kind is StatementKind.SHOW
    assert statement.node_tag == "VariableShowStmt"


def test_alter_system_set() -> None:
    statement = only_statement("ALTER SYSTEM SET work_mem = '64MB';")
    assert statement.kind is StatementKind.ALTER_SYSTEM
    assert statement.node_tag == "AlterSystemStmt"


def test_database_and_tablespace_statements() -> None:
    script = parse(
        """
        CREATE DATABASE analytics;
        ALTER DATABASE analytics ALLOW_CONNECTIONS false;
        ALTER DATABASE analytics SET work_mem TO '64MB';
        DROP DATABASE analytics;
        CREATE TABLESPACE fast_disk LOCATION '/mnt/nvme';
        DROP TABLESPACE fast_disk;
        """
    )
    assert [s.kind for s in script.statements] == [
        StatementKind.CREATE_DATABASE,
        StatementKind.ALTER_DATABASE,
        StatementKind.ALTER_DATABASE,
        StatementKind.DROP_DATABASE,
        StatementKind.CREATE_TABLESPACE,
        StatementKind.DROP_TABLESPACE,
    ]


# ---------------------------------------------------------------------------
# Transaction forms: savepoints and two-phase commit
# ---------------------------------------------------------------------------


def test_two_phase_commit_forms_are_transaction_other() -> None:
    script = parse(
        """
        PREPARE TRANSACTION 'push-42';
        COMMIT PREPARED 'push-42';
        ROLLBACK PREPARED 'push-42';
        """
    )
    assert [s.kind for s in script.statements] == [StatementKind.TRANSACTION_OTHER] * 3
    for statement in script.statements:
        assert statement.details == TransactionDetails(savepoint_name=None, chain=False)


def test_savepoint_forms() -> None:
    script = parse(
        """
        BEGIN;
        SAVEPOINT before_backfill;
        ROLLBACK TO SAVEPOINT before_backfill;
        RELEASE SAVEPOINT before_backfill;
        COMMIT;
        """
    )
    assert [s.kind for s in script.statements] == [
        StatementKind.BEGIN,
        StatementKind.SAVEPOINT,
        StatementKind.ROLLBACK_TO_SAVEPOINT,
        StatementKind.RELEASE_SAVEPOINT,
        StatementKind.COMMIT,
    ]
    assert script.statements[1].details == TransactionDetails(savepoint_name="before_backfill")


# ---------------------------------------------------------------------------
# Fallback dispatch arms
# ---------------------------------------------------------------------------


def test_create_aggregate_has_its_own_kind() -> None:
    statement = only_statement(
        "CREATE AGGREGATE array_accum (anycompatible) "
        "(sfunc = array_append, stype = anycompatiblearray, initcond = '{}');"
    )
    assert statement.kind is StatementKind.CREATE_AGGREGATE
    assert statement.node_tag == "DefineStmt"
    assert statement.targets == (QualifiedName("array_accum"),)


def test_non_aggregate_define_stmt_is_other() -> None:
    statement = only_statement("CREATE COLLATION german (locale = 'de_DE');")
    assert statement.kind is StatementKind.OTHER
    assert statement.node_tag == "DefineStmt"


def test_rename_sequence_is_rename_other() -> None:
    statement = only_statement("ALTER SEQUENCE order_id_seq RENAME TO orders_id_seq;")
    assert statement.kind is StatementKind.RENAME_OTHER
    assert statement.targets == (QualifiedName("order_id_seq"),)
    assert statement.details == RenameDetails(
        old_name=None, new_name="orders_id_seq", object_type="OBJECT_SEQUENCE"
    )


def test_set_schema_of_function_has_no_relation_target() -> None:
    statement = only_statement("ALTER FUNCTION compute_total(bigint) SET SCHEMA billing;")
    assert statement.kind is StatementKind.ALTER_OBJECT_SCHEMA
    assert statement.targets == ()


def test_alter_owner_of_schema() -> None:
    statement = only_statement("ALTER SCHEMA billing OWNER TO app_owner;")
    assert statement.kind is StatementKind.ALTER_OWNER
    assert statement.node_tag == "AlterOwnerStmt"
    assert statement.targets == ()


def test_create_range_type() -> None:
    statement = only_statement("CREATE TYPE floatrange AS RANGE (subtype = float8);")
    assert statement.kind is StatementKind.CREATE_RANGE_TYPE
    assert statement.targets == (QualifiedName("floatrange"),)


def test_object_management_kinds() -> None:
    script = parse(
        """
        CREATE SCHEMA app;
        CREATE ROLE deploy;
        ALTER ROLE deploy LOGIN;
        DROP ROLE deploy;
        ALTER FUNCTION app.compute_total(bigint) STRICT;
        ALTER POLICY tenant_isolation ON accounts USING (true);
        CREATE RULE protect_audit AS ON DELETE TO audit_log DO INSTEAD NOTHING;
        ALTER EXTENSION hstore UPDATE;
        """
    )
    assert [s.kind for s in script.statements] == [
        StatementKind.CREATE_SCHEMA,
        StatementKind.CREATE_ROLE,
        StatementKind.ALTER_ROLE,
        StatementKind.DROP_ROLE,
        StatementKind.ALTER_FUNCTION,
        StatementKind.ALTER_POLICY,
        StatementKind.CREATE_RULE,
        StatementKind.ALTER_EXTENSION,
    ]


# ---------------------------------------------------------------------------
# DROP entry shapes
# ---------------------------------------------------------------------------


def test_drop_extension_uses_bare_string_object_names() -> None:
    statement = only_statement("DROP EXTENSION IF EXISTS hstore CASCADE;")
    assert statement.kind is StatementKind.DROP_EXTENSION
    assert statement.targets == ()  # extensions are not relations
    assert statement.details == DropDetails(object_names=("hstore",), cascade=True, missing_ok=True)


def test_drop_cast_is_other_with_no_object_names() -> None:
    # a cast is named by a pair of types, which does not flatten to a name
    statement = only_statement("DROP CAST (integer AS bigint);")
    assert statement.kind is StatementKind.OTHER
    assert statement.node_tag == "DropStmt"
    assert statement.targets == ()
    assert statement.details == DropDetails(object_names=())


def test_drop_access_method_is_other() -> None:
    statement = only_statement("DROP ACCESS METHOD heap2;")
    assert statement.kind is StatementKind.OTHER
    assert statement.details == DropDetails(object_names=("heap2",))


# ---------------------------------------------------------------------------
# ALTER TABLE subcommands outside the mapped set
# ---------------------------------------------------------------------------


def test_alter_foreign_table_options_falls_back_to_other_action() -> None:
    statement = only_statement("ALTER FOREIGN TABLE ext.events OPTIONS (ADD fetch_size '100');")
    assert statement.kind is StatementKind.ALTER_FOREIGN_TABLE
    (action,) = statement.alter_actions
    assert action.kind is AlterTableActionKind.OTHER
    assert action.detail == "AT_GenericOptions"


def test_alter_foreign_table_column_options_keeps_column_name() -> None:
    action = only_action(
        "ALTER FOREIGN TABLE events ALTER COLUMN payload OPTIONS (ADD escape '\\');"
    )
    assert action.kind is AlterTableActionKind.OTHER
    assert action.detail == "AT_AlterColumnGenericOptions"
    assert action.column == "payload"


def test_add_column_generated_virtual() -> None:
    action = only_action(
        "ALTER TABLE metrics ADD COLUMN doubled bigint GENERATED ALWAYS AS (value * 2) VIRTUAL;"
    )
    assert action.kind is AlterTableActionKind.ADD_COLUMN_GENERATED_VIRTUAL
    assert action.column == "doubled"
    assert action.default is None


def test_explicit_null_constraint_is_plain_add_column() -> None:
    action = only_action("ALTER TABLE users ADD COLUMN middle_name text NULL;")
    assert action.kind is AlterTableActionKind.ADD_COLUMN
    assert action.not_null is False
    assert action.inline_constraints == ()


def test_inline_references_with_deferrable_attribute() -> None:
    # DEFERRABLE arrives as a separate attribute-only Constraint node that
    # must not disturb the recorded inline constraints
    action = only_action(
        "ALTER TABLE orders ADD COLUMN customer_id bigint REFERENCES customers DEFERRABLE;"
    )
    assert action.kind is AlterTableActionKind.ADD_COLUMN
    assert action.inline_constraints == ("references",)


# ---------------------------------------------------------------------------
# DO blocks
# ---------------------------------------------------------------------------


def test_do_block_in_another_language_is_opaque() -> None:
    statement = only_statement("DO LANGUAGE sql $$ SELECT 1 $$;")
    assert statement.kind is StatementKind.DO_BLOCK
    assert statement.details == DoBlockDetails(
        language="sql", statements=(), dynamic_sql_count=0, fully_parsed=False
    )


def test_do_block_with_unparseable_body_is_flagged() -> None:
    statement = only_statement("DO $$ BEGIN UPDATE t SET x = = 1; END $$;")
    details = statement.details
    assert isinstance(details, DoBlockDetails)
    assert details.language == "plpgsql"
    assert details.fully_parsed is False
    assert details.statements == ()


# ---------------------------------------------------------------------------
# Parser error edges
# ---------------------------------------------------------------------------


def test_parse_error_at_end_of_input_has_no_location() -> None:
    with pytest.raises(MigrationParseError) as excinfo:
        parse_migration("CREATE TABLE half_written (", path="broken.sql")
    error = excinfo.value
    assert error.location is None
    assert error.line is None
    assert str(error).startswith("broken.sql: ")
