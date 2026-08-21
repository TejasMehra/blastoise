"""Resolving parsed statements against the lock catalog."""

from __future__ import annotations

import pytest
from helpers import only_statement, parse

from pgverdict import (
    AlterTableActionKind,
    ParsedStatement,
    QualifiedName,
    SourceSpan,
    StatementKind,
)
from pgverdict.catalog import (
    DurationModel,
    LockMode,
    TransactionBlock,
    ir_attrs,
    kinds_in,
    load_catalog,
    resolve,
    statement_lock_mode,
)

CATALOG = load_catalog()
PG = 16


def relations(sql: str, pg: int = PG) -> dict[str, tuple[QualifiedName | None, LockMode]]:
    (lock,) = resolve(CATALOG, only_statement(sql), pg)
    return {r.role: (r.name, r.lock_mode) for r in lock.relations}


# --------------------------------------------------------------------------
# Foreign keys: both tables
# --------------------------------------------------------------------------


def test_add_foreign_key_names_referenced_table_as_second_relation() -> None:
    statement = only_statement(
        "ALTER TABLE orders ADD CONSTRAINT fk FOREIGN KEY (cid) REFERENCES app.customers (id);"
    )
    (lock,) = resolve(CATALOG, statement, PG)
    assert lock.action is not None
    assert lock.action.kind is AlterTableActionKind.ADD_FOREIGN_KEY
    by_role = {r.role: r for r in lock.relations}
    assert by_role["target"].name == QualifiedName("orders")
    assert by_role["referenced_table"].name == QualifiedName("customers", schema="app")
    assert by_role["referenced_table"].lock_mode is LockMode.SHARE_ROW_EXCLUSIVE
    referenced = by_role["referenced_table"]
    assert referenced.blocks_writes and not referenced.blocks_reads
    assert by_role["referenced_table"].certain


def test_not_valid_foreign_key_still_locks_referenced_table_but_is_constant() -> None:
    statement = only_statement(
        "ALTER TABLE orders ADD FOREIGN KEY (cid) REFERENCES customers (id) NOT VALID;"
    )
    (lock,) = resolve(CATALOG, statement, PG)
    assert lock.entry.duration_model is DurationModel.CONSTANT
    assert {r.role for r in lock.relations} == {"target", "referenced_table"}


def test_create_table_with_inline_references_locks_referenced_tables() -> None:
    statement = only_statement(
        "CREATE TABLE o (id int PRIMARY KEY, u int REFERENCES users(id), g int,"
        " FOREIGN KEY (g) REFERENCES app.groups(id), u2 int REFERENCES users(id));"
    )
    (lock,) = resolve(CATALOG, statement, PG)
    assert lock.entry.lock_mode is LockMode.NONE  # the new table itself
    named = [(r.role, r.name, r.lock_mode) for r in lock.relations]
    assert named == [
        ("referenced_tables", QualifiedName("users"), LockMode.SHARE_ROW_EXCLUSIVE),
        ("referenced_tables", QualifiedName("groups", schema="app"), LockMode.SHARE_ROW_EXCLUSIVE),
    ]


def test_create_table_without_foreign_keys_locks_nothing() -> None:
    (lock,) = resolve(CATALOG, only_statement("CREATE TABLE t (id int);"), PG)
    assert lock.relations == ()


def test_add_column_with_inline_references_locks_referenced_table() -> None:
    statement = only_statement("ALTER TABLE o ADD COLUMN u int REFERENCES users(id);")
    (lock,) = resolve(CATALOG, statement, PG)
    assert lock.action is not None and lock.action.kind is AlterTableActionKind.ADD_COLUMN
    by_role = {r.role: r for r in lock.relations}
    assert by_role["referenced_table"].name == QualifiedName("users")
    assert by_role["referenced_table"].lock_mode is LockMode.SHARE_ROW_EXCLUSIVE


# --------------------------------------------------------------------------
# ALTER TABLE: one row per subcommand, statement lock = strongest
# --------------------------------------------------------------------------


def test_multi_action_alter_table_resolves_per_action_and_takes_max_lock() -> None:
    statement = only_statement(
        "ALTER TABLE t ALTER COLUMN a SET STATISTICS 200, VALIDATE CONSTRAINT c,"
        " ADD COLUMN b int;"
    )
    locks = resolve(CATALOG, statement, PG)
    assert [lock.entry.kind for lock in locks] == [
        AlterTableActionKind.SET_STATISTICS,
        AlterTableActionKind.VALIDATE_CONSTRAINT,
        AlterTableActionKind.ADD_COLUMN,
    ]
    assert [lock.entry.lock_mode for lock in locks] == [
        LockMode.SHARE_UPDATE_EXCLUSIVE,
        LockMode.SHARE_UPDATE_EXCLUSIVE,
        LockMode.ACCESS_EXCLUSIVE,
    ]
    assert statement_lock_mode(locks) is LockMode.ACCESS_EXCLUSIVE
    assert all(lock.action is not None for lock in locks)


def test_statement_lock_mode_of_nothing_is_none() -> None:
    assert statement_lock_mode(()) is LockMode.NONE


def test_version_changes_the_row_for_the_same_statement() -> None:
    statement = only_statement("ALTER TABLE t ADD COLUMN flag boolean NOT NULL DEFAULT false;")
    (pg10,) = resolve(CATALOG, statement, 10)
    (pg11,) = resolve(CATALOG, statement, 11)
    assert pg10.entry.kind is AlterTableActionKind.ADD_COLUMN_DEFAULT_NONVOLATILE
    assert pg10.entry.requires_table_rewrite and not pg11.entry.requires_table_rewrite


# --------------------------------------------------------------------------
# DO blocks
# --------------------------------------------------------------------------


def test_do_block_resolves_inner_statements_with_conditional_flag() -> None:
    statement = only_statement(
        """
        DO $$ BEGIN
          IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                         WHERE table_name = 't' AND column_name = 'c') THEN
            ALTER TABLE t ADD COLUMN c text;
            CREATE INDEX t_c_idx ON t (c);
          END IF;
        END $$;
        """
    )
    locks = resolve(CATALOG, statement, PG)
    assert [lock.entry.kind for lock in locks] == [
        AlterTableActionKind.ADD_COLUMN,
        StatementKind.CREATE_INDEX,
    ]
    assert all(lock.in_do_block and lock.conditional for lock in locks)
    assert kinds_in(statement) == (
        StatementKind.DO_BLOCK,
        StatementKind.ALTER_TABLE,
        StatementKind.CREATE_INDEX,
    )


def test_unguarded_do_block_is_not_conditional() -> None:
    statement = only_statement("DO $$ BEGIN UPDATE t SET a = 1; END $$;")
    (lock,) = resolve(CATALOG, statement, PG)
    assert lock.in_do_block and not lock.conditional
    assert lock.entry.kind is StatementKind.UPDATE_WITHOUT_WHERE


def test_opaque_do_block_yields_no_rows() -> None:
    statement = only_statement("DO LANGUAGE plpython3u $$ pass $$;")
    assert resolve(CATALOG, statement, PG) == ()


# --------------------------------------------------------------------------
# `when` variants
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sql", "mode"),
    [
        ("LOCK TABLE t IN ACCESS SHARE MODE;", LockMode.ACCESS_SHARE),
        ("LOCK TABLE t IN ROW SHARE MODE;", LockMode.ROW_SHARE),
        ("LOCK TABLE t IN ROW EXCLUSIVE MODE;", LockMode.ROW_EXCLUSIVE),
        ("LOCK TABLE t IN SHARE UPDATE EXCLUSIVE MODE;", LockMode.SHARE_UPDATE_EXCLUSIVE),
        ("LOCK TABLE t IN SHARE MODE;", LockMode.SHARE),
        ("LOCK TABLE t IN SHARE ROW EXCLUSIVE MODE;", LockMode.SHARE_ROW_EXCLUSIVE),
        ("LOCK TABLE t IN EXCLUSIVE MODE;", LockMode.EXCLUSIVE),
        ("LOCK TABLE t IN ACCESS EXCLUSIVE MODE;", LockMode.ACCESS_EXCLUSIVE),
        ("LOCK TABLE t;", LockMode.ACCESS_EXCLUSIVE),
        ("LOCK TABLE t, u NOWAIT;", LockMode.ACCESS_EXCLUSIVE),
    ],
)
def test_lock_table_mode_comes_from_the_statement(sql: str, mode: LockMode) -> None:
    (lock,) = resolve(CATALOG, only_statement(sql), PG)
    assert lock.entry.lock_mode is mode
    assert lock.entry.duration_model is DurationModel.CONSTANT
    assert lock.relations[0].name == QualifiedName("t")


def test_lock_table_additional_targets_are_locked_too() -> None:
    (lock,) = resolve(CATALOG, only_statement("LOCK TABLE t, u IN SHARE MODE;"), PG)
    assert [(r.role, r.name) for r in lock.relations] == [
        ("target", QualifiedName("t")),
        ("additional_targets", QualifiedName("u")),
    ]


def test_insert_values_is_constant_but_insert_select_is_not() -> None:
    (values,) = resolve(CATALOG, only_statement("INSERT INTO t (a) VALUES (1), (2);"), PG)
    (defaults,) = resolve(CATALOG, only_statement("INSERT INTO t DEFAULT VALUES;"), PG)
    (select,) = resolve(CATALOG, only_statement("INSERT INTO t SELECT a FROM s;"), PG)
    assert values.entry.duration_model is DurationModel.CONSTANT
    assert defaults.entry.duration_model is DurationModel.CONSTANT
    assert select.entry.duration_model is DurationModel.PROPORTIONAL_TO_ROWS_MATCHED
    assert select.entry.requires_live_context
    assert [r.role for r in select.relations] == ["target", "source_relations"]
    assert select.relations[1].name is None


def test_reindex_scope_variants() -> None:
    (index,) = resolve(CATALOG, only_statement("REINDEX INDEX i;"), PG)
    (table,) = resolve(CATALOG, only_statement("REINDEX TABLE t;"), PG)
    (schema,) = resolve(CATALOG, only_statement("REINDEX SCHEMA s;"), PG)
    assert index.entry.lock_mode is LockMode.ACCESS_EXCLUSIVE
    assert index.entry.duration_model is DurationModel.PROPORTIONAL_TO_INDEX_SIZE
    assert {r.role: r.lock_mode for r in index.relations} == {
        "target": LockMode.ACCESS_EXCLUSIVE,
        "owning_table": LockMode.SHARE,
    }
    assert table.entry.lock_mode is LockMode.SHARE
    assert table.entry.duration_model is DurationModel.PROPORTIONAL_TO_ROWS
    assert schema.entry.requires_live_context
    assert schema.entry.transaction_block is TransactionBlock.FORBIDDEN


def test_rename_other_variants() -> None:
    (seq,) = resolve(CATALOG, only_statement("ALTER SEQUENCE s RENAME TO s2;"), PG)
    (trig,) = resolve(CATALOG, only_statement("ALTER TRIGGER tg ON t RENAME TO tg2;"), PG)
    (func,) = resolve(CATALOG, only_statement("ALTER FUNCTION f() RENAME TO g;"), PG)
    assert seq.entry.lock_mode is LockMode.ACCESS_EXCLUSIVE
    assert seq.relations[0].name == QualifiedName("s")
    assert trig.entry.lock_mode is LockMode.ACCESS_EXCLUSIVE
    assert trig.relations[0].name == QualifiedName("t")
    assert func.entry.lock_mode is LockMode.NONE
    assert func.relations == ()


def test_create_table_as_no_data_is_constant() -> None:
    (populated,) = resolve(CATALOG, only_statement("CREATE TABLE c AS SELECT * FROM s;"), PG)
    (empty,) = resolve(
        CATALOG, only_statement("CREATE TABLE c AS SELECT * FROM s WITH NO DATA;"), PG
    )
    assert populated.entry.duration_model is DurationModel.PROPORTIONAL_TO_ROWS_MATCHED
    assert empty.entry.duration_model is DurationModel.CONSTANT


# --------------------------------------------------------------------------
# Unresolvable and uncertain relations
# --------------------------------------------------------------------------


def test_drop_index_reports_unnamed_owning_table() -> None:
    (lock,) = resolve(CATALOG, only_statement("DROP INDEX i;"), PG)
    assert [(r.role, r.name, r.lock_mode, r.certain) for r in lock.relations] == [
        ("target", QualifiedName("i"), LockMode.ACCESS_EXCLUSIVE, True),
        ("owning_table", None, LockMode.ACCESS_EXCLUSIVE, True),
    ]


def test_cascade_dependents_are_reported_as_uncertain() -> None:
    (lock,) = resolve(CATALOG, only_statement("DROP TABLE t CASCADE;"), PG)
    by_role = {r.role: r for r in lock.relations}
    assert by_role["target"].certain
    assert not by_role["referencing_tables"].certain
    assert not by_role["dependent_relations"].certain
    assert by_role["referencing_tables"].name is None


def test_comment_on_without_a_decoded_target_still_resolves() -> None:
    (lock,) = resolve(CATALOG, only_statement("COMMENT ON TABLE t IS 'x';"), PG)
    assert lock.entry.lock_mode is LockMode.SHARE_UPDATE_EXCLUSIVE
    assert lock.relations == ()  # optional target, not supplied by the IR


def test_required_role_missing_from_ir_fails_loudly() -> None:
    orphan = ParsedStatement(
        kind=StatementKind.CREATE_TRIGGER,
        sql="CREATE TRIGGER ...",
        span=SourceSpan(0, 1, 1),
        node_tag="CreateTrigStmt",
        targets=(),
    )
    with pytest.raises(LookupError, match="role 'target' is required"):
        resolve(CATALOG, orphan, PG)


# --------------------------------------------------------------------------
# A realistic migration end to end
# --------------------------------------------------------------------------

MIGRATION = """
BEGIN;
CREATE TABLE orders (id bigserial PRIMARY KEY, customer_id int REFERENCES customers (id));
COMMENT ON TABLE orders IS 'customer orders';
COMMENT ON COLUMN orders.customer_id IS 'fk';
CREATE FUNCTION touch() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END $$;
CREATE TRIGGER touch_orders BEFORE UPDATE ON orders FOR EACH ROW EXECUTE FUNCTION touch();
CREATE INDEX orders_customer_idx ON orders (customer_id);
ALTER TABLE orders ADD COLUMN status text NOT NULL DEFAULT 'new';
UPDATE orders SET status = 'legacy' WHERE created_at < '2020-01-01';
COMMIT;
CREATE INDEX CONCURRENTLY orders_status_idx ON orders (status);
"""


def test_realistic_migration_resolves_and_separates_harmless_from_risky() -> None:
    script = parse(MIGRATION)
    summary = []
    for statement in script.statements:
        for lock in resolve(CATALOG, statement, PG):
            entry = lock.entry
            summary.append(
                (
                    str(entry.kind),
                    entry.lock_mode.value,
                    entry.duration_model.value,
                    entry.blocks_reads,
                    entry.blocks_writes,
                )
            )
    assert summary == [
        ("begin", "NONE", "CONSTANT", False, False),
        ("create_table", "NONE", "CONSTANT", False, False),
        ("comment_on", "SHARE UPDATE EXCLUSIVE", "CONSTANT", False, False),
        ("comment_on", "SHARE UPDATE EXCLUSIVE", "CONSTANT", False, False),
        ("create_function", "NONE", "CONSTANT", False, False),
        ("create_trigger", "SHARE ROW EXCLUSIVE", "CONSTANT", False, True),
        ("create_index", "SHARE", "PROPORTIONAL_TO_ROWS", False, True),
        ("add_column_default_nonvolatile", "ACCESS EXCLUSIVE", "CONSTANT", True, True),
        ("update", "ROW EXCLUSIVE", "PROPORTIONAL_TO_ROWS_MATCHED", False, True),
        ("commit", "NONE", "CONSTANT", False, False),
        (
            "create_index_concurrently",
            "SHARE UPDATE EXCLUSIVE",
            "PROPORTIONAL_TO_ROWS",
            False,
            False,
        ),
    ]
    harmless = [s for s in summary if s[2] == "CONSTANT" and not s[3] and not s[4]]
    assert len(harmless) == 6  # begin, create_table, 2x comment, create_function, commit


def test_ir_attrs_exposes_statement_details_and_action_fields() -> None:
    statement = only_statement("ALTER TABLE t ADD COLUMN c int REFERENCES u(id);")
    attrs = ir_attrs(statement, statement.alter_actions[0])
    # later sources win on clashes: the action's kind shadows the statement's
    assert attrs["kind"] is AlterTableActionKind.ADD_COLUMN
    assert attrs["referenced_table"] == QualifiedName("u")
    attrs = ir_attrs(only_statement("LOCK TABLE t IN SHARE MODE;"))
    assert attrs["mode"] == 5 and attrs["nowait"] is False


# --------------------------------------------------------------------------
# Partition roles
# --------------------------------------------------------------------------


def test_attach_partition_names_both_parent_and_partition() -> None:
    statement = only_statement(
        "ALTER TABLE measurements ATTACH PARTITION measurements_2024"
        " FOR VALUES FROM ('2024-01-01') TO ('2025-01-01');"
    )
    (lock,) = resolve(CATALOG, statement, 16)
    assert lock.entry.lock_mode is LockMode.SHARE_UPDATE_EXCLUSIVE
    assert {r.role: (r.name, r.lock_mode) for r in lock.relations} == {
        "target": (QualifiedName("measurements"), LockMode.SHARE_UPDATE_EXCLUSIVE),
        "partition": (QualifiedName("measurements_2024"), LockMode.ACCESS_EXCLUSIVE),
    }
    (pg11,) = resolve(CATALOG, statement, 11)
    assert pg11.entry.lock_mode is LockMode.ACCESS_EXCLUSIVE


def test_create_table_partition_of_locks_the_parent() -> None:
    statement = only_statement(
        "CREATE TABLE measurements_2024 PARTITION OF measurements"
        " FOR VALUES FROM ('2024-01-01') TO ('2025-01-01');"
    )
    (lock,) = resolve(CATALOG, statement, 16)
    assert lock.lock_mode is LockMode.ACCESS_EXCLUSIVE  # via the property
    by_role = {r.role: r.name for r in lock.relations}
    assert by_role["parent"] == QualifiedName("measurements")


def test_plain_create_table_supplies_no_parent_for_the_partition_stub() -> None:
    # The parent role resolves to nothing on a non-partition CREATE TABLE;
    # exercised through the referenced_tables path which shares the details.
    statement = only_statement("CREATE TABLE t (a int REFERENCES u(id));")
    (lock,) = resolve(CATALOG, statement, 16)
    assert [r.role for r in lock.relations] == ["referenced_tables"]


def test_do_block_kind_without_do_details_yields_nothing() -> None:
    fake = ParsedStatement(
        kind=StatementKind.DO_BLOCK,
        sql="DO $$ $$",
        span=SourceSpan(0, 1, 1),
        node_tag="DoStmt",
    )
    assert resolve(CATALOG, fake, PG) == ()
