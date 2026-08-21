"""Tests for the gaps surfaced by the real-world corpus run.

Covers: RENAME_TYPE and the five previously-OTHER statement mappings, range
constructors in the volatility allowlist, UPDATE/DELETE backfill shapes,
squash/baseline file detection, and catalog-guarded DO blocks.
"""

from __future__ import annotations

import pytest
from helpers import only_action, only_statement, parse

from blastoise import (
    AlterTableActionKind,
    DmlDetails,
    DoBlockDetails,
    QualifiedName,
    StatementKind,
    Volatility,
)

# ---------------------------------------------------------------------------
# Taxonomy: previously-OTHER statements from the wild
# ---------------------------------------------------------------------------


def test_alter_type_rename_is_rename_type() -> None:
    # Prisma's enum-change recipe, step one.
    statement = only_statement('ALTER TYPE "BookingStatus" RENAME TO "BookingStatus_old";')
    assert statement.kind is StatementKind.RENAME_TYPE
    assert statement.targets == (QualifiedName("BookingStatus"),)
    assert statement.details is not None


def test_alter_type_rename_schema_qualified_target() -> None:
    statement = only_statement(
        "ALTER TYPE billing.invoice_state RENAME TO invoice_state_old;"
    )
    assert statement.kind is StatementKind.RENAME_TYPE
    assert statement.targets == (QualifiedName("invoice_state", schema="billing"),)


def test_create_statistics_targets_the_table() -> None:
    statement = only_statement(
        "CREATE STATISTICS stat_orders (dependencies) ON customer_id, region FROM orders;"
    )
    assert statement.kind is StatementKind.CREATE_STATISTICS
    assert statement.node_tag == "CreateStatsStmt"
    assert statement.targets == (QualifiedName("orders"),)


def test_alter_statistics() -> None:
    statement = only_statement("ALTER STATISTICS stat_orders SET STATISTICS 1500;")
    assert statement.kind is StatementKind.ALTER_STATISTICS
    assert statement.targets == (QualifiedName("stat_orders"),)


def test_alter_default_privileges() -> None:
    statement = only_statement(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO app_user;"
    )
    assert statement.kind is StatementKind.ALTER_DEFAULT_PRIVILEGES
    assert statement.node_tag == "AlterDefaultPrivilegesStmt"


def test_drop_aggregate() -> None:
    statement = only_statement("DROP AGGREGATE IF EXISTS sg_jsonb_concat_agg(jsonb);")
    assert statement.kind is StatementKind.DROP_AGGREGATE


def test_alter_role_set_variant_maps_to_alter_role() -> None:
    statement = only_statement("ALTER ROLE CURRENT_USER SET search_path TO zulip,public;")
    assert statement.kind is StatementKind.ALTER_ROLE
    assert statement.node_tag == "AlterRoleSetStmt"


def test_rename_function_carries_target_name() -> None:
    statement = only_statement("ALTER FUNCTION wh_dim_id(int) RENAME TO wh_dim_key;")
    assert statement.kind is StatementKind.RENAME_OTHER
    assert statement.targets == (QualifiedName("wh_dim_id"),)


def test_rename_publication_carries_target_name() -> None:
    statement = only_statement("ALTER PUBLICATION events_pub RENAME TO events_pub_v2;")
    assert statement.kind is StatementKind.RENAME_OTHER
    assert statement.targets == (QualifiedName("events_pub"),)


# ---------------------------------------------------------------------------
# Volatility: range constructors
# ---------------------------------------------------------------------------


def _default_volatility(expr: str) -> Volatility:
    action = only_action(f"ALTER TABLE t ADD COLUMN c tstzrange DEFAULT {expr};")
    assert action.default is not None
    return action.default.volatility


def test_range_constructors_are_immutable() -> None:
    assert _default_volatility("numrange(1, 10)") is Volatility.IMMUTABLE
    assert _default_volatility("int8range(0, 100)") is Volatility.IMMUTABLE
    # Arguments still dominate: now() keeps the boundary fix nonvolatile
    # but stable, exactly like the bare now() default.
    assert _default_volatility("tstzrange(now(), NULL, '[]')") is Volatility.STABLE
    action = only_action(
        "ALTER TABLE session_state ADD COLUMN active_time_range tstzrange"
        " NOT NULL DEFAULT tstzrange(now(), NULL, '[]');"
    )
    assert action.kind is AlterTableActionKind.ADD_COLUMN_DEFAULT_NONVOLATILE


# ---------------------------------------------------------------------------
# DML backfill shapes
# ---------------------------------------------------------------------------


def test_update_without_where_is_flagged() -> None:
    statement = only_statement("UPDATE orders SET migrated = true;")
    assert statement.kind is StatementKind.UPDATE_WITHOUT_WHERE
    assert statement.targets == (QualifiedName("orders"),)
    assert statement.details == DmlDetails(has_where=False)


def test_update_with_where_but_unbatched_stays_update() -> None:
    statement = only_statement("UPDATE orders SET total = 0 WHERE total IS NULL;")
    assert statement.kind is StatementKind.UPDATE
    assert statement.details == DmlDetails(has_where=True)


def test_update_batched_by_limited_subselect() -> None:
    statement = only_statement(
        "UPDATE orders SET migrated = true\n"
        " WHERE id IN (SELECT id FROM orders WHERE NOT migrated LIMIT 1000);"
    )
    assert statement.kind is StatementKind.UPDATE_BATCHED
    assert statement.details == DmlDetails(has_where=True, batch_signals=("limit",))


def test_update_batched_by_ctid_and_limit() -> None:
    statement = only_statement(
        "UPDATE orders SET migrated = true"
        " WHERE ctid = ANY (ARRAY(SELECT ctid FROM orders LIMIT 500));"
    )
    assert statement.kind is StatementKind.UPDATE_BATCHED
    assert statement.details == DmlDetails(has_where=True, batch_signals=("limit", "ctid"))


def test_update_batched_by_key_window() -> None:
    statement = only_statement(
        "UPDATE orders SET migrated = true WHERE id >= 10000 AND id < 20000;"
    )
    assert statement.kind is StatementKind.UPDATE_BATCHED
    assert statement.details == DmlDetails(has_where=True, batch_signals=("key_window",))


def test_update_batched_by_cte_limit() -> None:
    statement = only_statement(
        "WITH batch AS (SELECT id FROM orders WHERE NOT migrated LIMIT 100)\n"
        "UPDATE orders SET migrated = true FROM batch WHERE orders.id = batch.id;"
    )
    assert statement.kind is StatementKind.UPDATE_BATCHED


def test_delete_without_where_is_flagged() -> None:
    statement = only_statement("DELETE FROM stale_sessions;")
    assert statement.kind is StatementKind.DELETE_WITHOUT_WHERE
    assert statement.details == DmlDetails(has_where=False)


def test_delete_batched_by_between_window() -> None:
    statement = only_statement("DELETE FROM audit_log WHERE id BETWEEN 1 AND 100000;")
    assert statement.kind is StatementKind.DELETE_BATCHED
    assert statement.details == DmlDetails(has_where=True, batch_signals=("key_window",))


def test_one_sided_retention_cutoff_is_not_a_window() -> None:
    statement = only_statement(
        "DELETE FROM audit_log WHERE created_at < now() - interval '90 days';"
    )
    assert statement.kind is StatementKind.DELETE
    assert statement.details == DmlDetails(has_where=True)


def test_reversed_operand_window_is_detected() -> None:
    statement = only_statement(
        "UPDATE orders SET migrated = true WHERE 10000 <= id AND id < 20000;"
    )
    assert statement.kind is StatementKind.UPDATE_BATCHED


def test_bounds_on_different_columns_are_not_a_window() -> None:
    statement = only_statement(
        "DELETE FROM audit_log WHERE id >= 10 AND created_at <= now();"
    )
    assert statement.kind is StatementKind.DELETE


# ---------------------------------------------------------------------------
# Squash/baseline file detection
# ---------------------------------------------------------------------------


def _baseline_source(tables: int) -> str:
    parts = ["DROP TABLE IF EXISTS legacy_leftover;"]
    for i in range(tables):
        parts.append(
            f"CREATE TABLE t{i} (id bigserial PRIMARY KEY, name text, val int);"
        )
        parts.append(f"CREATE INDEX ix_t{i}_name ON t{i} (name);")
        parts.append(f"ALTER TABLE t{i} ADD CONSTRAINT ck_t{i} CHECK (val >= 0);")
        parts.append(f"COMMENT ON TABLE t{i} IS 'table {i}';")
    parts.append("INSERT INTO t0 (name, val) VALUES ('seed', 1);")
    return "\n".join(parts)


def test_large_self_contained_file_is_baseline_shaped() -> None:
    script = parse(_baseline_source(tables=15))  # 62 statements
    assert len(script.statements) >= 50
    assert script.baseline_shaped is True


def test_alter_against_preexisting_table_defeats_baseline_shape() -> None:
    source = _baseline_source(tables=15) + "\nALTER TABLE users ADD COLUMN age int;"
    script = parse(source)
    assert script.baseline_shaped is False


def test_small_create_only_migration_is_not_baseline_shaped() -> None:
    script = parse(
        "CREATE TABLE web_hooks (id bigserial PRIMARY KEY, url text);\n"
        "CREATE INDEX ix_web_hooks_url ON web_hooks (url);"
    )
    assert script.baseline_shaped is False


def test_backfill_against_preexisting_table_defeats_baseline_shape() -> None:
    source = _baseline_source(tables=15) + "\nUPDATE users SET migrated = true;"
    script = parse(source)
    assert script.baseline_shaped is False


def test_baseline_tracks_partitions_of_in_file_parents() -> None:
    partition_ddl = (
        "\nCREATE TABLE events (id bigint, at timestamptz) PARTITION BY RANGE (at);"
        "\nCREATE TABLE events_2026 PARTITION OF events"
        " FOR VALUES FROM ('2026-01-01') TO ('2027-01-01');"
    )
    assert parse(_baseline_source(tables=15) + partition_ddl).baseline_shaped is True
    orphan = (
        "\nCREATE TABLE events_2026 PARTITION OF preexisting_events"
        " FOR VALUES FROM ('2026-01-01') TO ('2027-01-01');"
    )
    assert parse(_baseline_source(tables=15) + orphan).baseline_shaped is False


def test_baseline_tracks_renames_and_plain_drops_of_own_objects() -> None:
    tail = (
        "\nALTER TABLE t0 RENAME TO t0_final;"
        "\nALTER TABLE t0_final ADD COLUMN extra int;"
        "\nDROP TABLE t1;"
    )
    assert parse(_baseline_source(tables=15) + tail).baseline_shaped is True
    assert (
        parse(_baseline_source(tables=15) + "\nDROP TABLE preexisting;").baseline_shaped
        is False
    )


# ---------------------------------------------------------------------------
# Catalog-guarded DO blocks
# ---------------------------------------------------------------------------


def _do_details(sql: str) -> DoBlockDetails:
    statement = only_statement(sql)
    assert isinstance(statement.details, DoBlockDetails)
    return statement.details


def test_information_schema_guard_is_flagged() -> None:
    details = _do_details(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                           WHERE table_name = 'users' AND column_name = 'age') THEN
                ALTER TABLE users ADD COLUMN age int;
            END IF;
        END
        $$;
        """
    )
    assert details.existence_guarded is True
    assert [s.kind for s in details.statements] == [StatementKind.ALTER_TABLE]


def test_to_regclass_guard_is_flagged() -> None:
    details = _do_details(
        """
        DO $$
        BEGIN
            IF to_regclass('public.users_backup') IS NULL THEN
                CREATE TABLE users_backup (id int);
            END IF;
        END
        $$;
        """
    )
    assert details.existence_guarded is True


def test_unqualified_pg_catalog_table_guard_is_flagged() -> None:
    details = _do_details(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_class WHERE relname = 'old_index') THEN
                DROP INDEX old_index;
            END IF;
        END
        $$;
        """
    )
    assert details.existence_guarded is True


def test_elsif_catalog_guard_flags_the_whole_chain() -> None:
    details = _do_details(
        """
        DO $$
        BEGIN
            IF false THEN
                UPDATE users SET age = 0 WHERE age IS NULL;
            ELSIF EXISTS (SELECT 1 FROM pg_type WHERE typname = 'mood') THEN
                ALTER TYPE mood ADD VALUE 'weary';
            ELSE
                CREATE TYPE mood AS ENUM ('happy', 'weary');
            END IF;
        END
        $$;
        """
    )
    assert details.existence_guarded is True
    assert len(details.statements) == 3


def test_plain_condition_is_not_flagged() -> None:
    details = _do_details(
        """
        DO $$
        DECLARE n int;
        BEGIN
            SELECT count(*) INTO n FROM users;
            IF n > 5 THEN
                UPDATE users SET flagged = true WHERE flagged IS NULL;
            END IF;
        END
        $$;
        """
    )
    assert details.existence_guarded is False


def test_unguarded_statement_outside_if_does_not_flag() -> None:
    details = _do_details(
        "DO $$ BEGIN UPDATE users SET age = 0 WHERE age IS NULL; END $$;"
    )
    assert details.existence_guarded is False


@pytest.mark.parametrize(
    ("sql", "kind"),
    [
        ("UPDATE t SET a = 1 WHERE b;", StatementKind.UPDATE),
        ("DELETE FROM t WHERE b;", StatementKind.DELETE),
    ],
)
def test_bare_boolean_where_is_plain_dml(sql: str, kind: StatementKind) -> None:
    assert only_statement(sql).kind is kind
