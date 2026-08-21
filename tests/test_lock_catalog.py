"""Invariants of the lock semantics catalog data and its validating loader.

Three things the catalog must guarantee, each pinned here: the conflict
matrix is the documented one (symmetric, complete), every classification has
a row (real or an explicit UNCALIBRATED stub), and no row silently spans a
Postgres version where behavior is known to differ.
"""

from __future__ import annotations

import copy
from importlib import resources
from typing import Any

import pytest
import yaml

from pgverdict import AlterTableActionKind, StatementKind
from pgverdict.catalog import (
    CATALOG_RESOURCE,
    TABLE_LOCK_MODES,
    Calibration,
    CatalogError,
    DurationModel,
    LockMode,
    Resolution,
    TransactionBlock,
    load_catalog,
    parse_catalog,
)

CATALOG = load_catalog()
VERSIONS = range(CATALOG.version_domain.min, CATALOG.version_domain.max + 1)


@pytest.fixture
def raw_catalog() -> dict[str, Any]:
    text = resources.files("pgverdict.catalog").joinpath(CATALOG_RESOURCE).read_text("utf-8")
    data: dict[str, Any] = yaml.safe_load(text)
    return data


# --------------------------------------------------------------------------
# Conflict matrix
# --------------------------------------------------------------------------

# doc:18/explicit-locking.html Table 13.2, transcribed independently of the YAML.
_AS, _RS, _RX, _SUE, _S, _SRX, _X, _AX = (
    LockMode.ACCESS_SHARE,
    LockMode.ROW_SHARE,
    LockMode.ROW_EXCLUSIVE,
    LockMode.SHARE_UPDATE_EXCLUSIVE,
    LockMode.SHARE,
    LockMode.SHARE_ROW_EXCLUSIVE,
    LockMode.EXCLUSIVE,
    LockMode.ACCESS_EXCLUSIVE,
)
DOCUMENTED_MATRIX: dict[LockMode, frozenset[LockMode]] = {
    _AS: frozenset({_AX}),
    _RS: frozenset({_X, _AX}),
    _RX: frozenset({_S, _SRX, _X, _AX}),
    _SUE: frozenset({_SUE, _S, _SRX, _X, _AX}),
    _S: frozenset({_RX, _SUE, _SRX, _X, _AX}),
    _SRX: frozenset({_RX, _SUE, _S, _SRX, _X, _AX}),
    _X: frozenset({_RS, _RX, _SUE, _S, _SRX, _X, _AX}),
    _AX: frozenset({_AS, _RS, _RX, _SUE, _S, _SRX, _X, _AX}),
}


def test_conflict_matrix_is_complete() -> None:
    assert set(CATALOG.conflict_matrix) == set(TABLE_LOCK_MODES)
    assert len(TABLE_LOCK_MODES) == 8


def test_conflict_matrix_is_symmetric() -> None:
    for mode, conflicts in CATALOG.conflict_matrix.items():
        for other in conflicts:
            assert mode in CATALOG.conflict_matrix[other], (mode, other)


def test_conflict_matrix_matches_postgres_documentation() -> None:
    assert CATALOG.conflict_matrix == DOCUMENTED_MATRIX


def test_conflict_matrix_structural_facts() -> None:
    # ACCESS EXCLUSIVE conflicts with everything; ACCESS SHARE only with it;
    # strictly stronger modes conflict with a superset of modes.
    assert CATALOG.conflict_matrix[_AX] == frozenset(TABLE_LOCK_MODES)
    assert CATALOG.conflict_matrix[_AS] == frozenset({_AX})
    assert CATALOG.conflicts_with(LockMode.NONE) == frozenset()
    assert CATALOG.conflicts_with(LockMode.UNKNOWN) == frozenset(TABLE_LOCK_MODES)
    # every entry's conflicts_with is the matrix row for its lock mode
    for rows in list(CATALOG.statements.values()) + list(CATALOG.alter_table_actions.values()):
        for entry in rows:
            assert entry.conflicts_with == CATALOG.conflicts_with(entry.lock_mode)


# --------------------------------------------------------------------------
# Coverage: every classification, every version
# --------------------------------------------------------------------------


def test_every_statement_kind_has_rows_or_a_resolution() -> None:
    for kind in StatementKind:
        resolution = CATALOG.resolution_of(kind)
        if resolution is Resolution.DIRECT:
            assert kind in CATALOG.statements, kind
            for version in VERSIONS:
                entry = CATALOG.entry_for(kind, version)
                assert entry.is_default_row
        else:
            assert kind not in CATALOG.statements, kind


def test_every_alter_table_action_kind_has_rows() -> None:
    for kind in AlterTableActionKind:
        assert kind in CATALOG.alter_table_actions, kind
        for version in VERSIONS:
            assert CATALOG.entry_for(kind, version).is_default_row


def test_only_alter_table_and_do_block_resolve_indirectly() -> None:
    assert CATALOG.statement_resolution == {
        StatementKind.ALTER_TABLE: Resolution.PER_ACTION,
        StatementKind.DO_BLOCK: Resolution.INNER_STATEMENTS,
    }


# The 41 kinds that never occurred in the 3,081-file wild corpus (top-level or
# inside DO blocks), per corpus_report.json. These — and only these — are
# UNCALIBRATED stubs at the statement level.
NEVER_SEEN_IN_WILD = frozenset(
    StatementKind(name)
    for name in [
        "alter_composite_type", "alter_database", "alter_extension", "alter_foreign_table",
        "alter_index", "alter_matview", "alter_object_schema", "alter_owner", "alter_policy",
        "alter_system", "alter_view", "cluster", "copy_from", "copy_to", "create_database",
        "create_foreign_table", "create_range_type", "create_rule", "create_table_partition_of",
        "create_tablespace", "delete_batched", "drop_database", "drop_domain", "drop_role",
        "drop_tablespace", "merge", "other", "refresh_matview", "refresh_matview_concurrently",
        "reindex_concurrently", "release_savepoint", "rename_matview", "reset", "rollback",
        "rollback_to_savepoint", "savepoint", "select_into", "show", "transaction_other",
        "vacuum", "vacuum_full",
    ]
)  # fmt: skip


def test_uncalibrated_statement_stubs_are_exactly_the_never_seen_kinds() -> None:
    assert len(NEVER_SEEN_IN_WILD) == 41
    uncalibrated = {
        kind
        for kind, rows in CATALOG.statements.items()
        if any(e.calibration is Calibration.UNCALIBRATED for e in rows)
    }
    assert uncalibrated == NEVER_SEEN_IN_WILD
    # and a never-seen kind is a stub on every version, not a mixed bag
    for kind in NEVER_SEEN_IN_WILD:
        for entry in CATALOG.statements[kind]:
            assert entry.calibration is Calibration.UNCALIBRATED, (kind, entry.pg_versions)


# ALTER TABLE subcommands observed in the wild (alter_action_distribution)
# must all be calibrated; the rest may be stubs.
WILD_ACTIONS = frozenset(
    AlterTableActionKind(name)
    for name in [
        "add_column", "add_column_default_nonvolatile", "add_foreign_key", "drop_column",
        "alter_column_type", "drop_constraint", "set_column_default", "add_primary_key",
        "set_not_null", "add_unique", "add_check", "drop_not_null", "add_column_generated_stored",
        "drop_column_default", "alter_constraint", "add_column_not_null_no_default",
        "enable_row_security", "disable_trigger", "enable_trigger", "add_column_default_volatile",
        "add_column_default_unknown_volatility", "add_column_serial", "add_column_identity",
        "set_unlogged", "set_storage_params", "set_statistics", "add_exclusion",
        "add_unique_using_index", "disable_row_security", "add_primary_key_using_index",
        "add_foreign_key_not_valid", "validate_constraint", "drop_identity", "add_identity",
    ]
)  # fmt: skip


def test_wild_alter_table_actions_are_calibrated() -> None:
    for kind in WILD_ACTIONS:
        for entry in CATALOG.alter_table_actions[kind]:
            assert entry.calibration is Calibration.CALIBRATED, (kind, entry.pg_versions)


def test_uncalibrated_actions_are_the_expected_stubs() -> None:
    uncalibrated = {
        kind
        for kind, rows in CATALOG.alter_table_actions.items()
        if any(e.calibration is Calibration.UNCALIBRATED for e in rows)
    }
    assert uncalibrated == {
        AlterTableActionKind.ATTACH_PARTITION,
        AlterTableActionKind.DETACH_PARTITION,
        AlterTableActionKind.DETACH_PARTITION_CONCURRENTLY,
        AlterTableActionKind.DETACH_PARTITION_FINALIZE,
        AlterTableActionKind.INHERIT,
        AlterTableActionKind.NO_INHERIT,
        AlterTableActionKind.OF_TYPE,
        AlterTableActionKind.NOT_OF,
        AlterTableActionKind.REPLACE_STORAGE_PARAMS,
        AlterTableActionKind.ADD_CONSTRAINT_OTHER,
        AlterTableActionKind.OTHER,
    }


def test_unknown_marker_is_confined_to_grab_bag_kinds() -> None:
    unknown_kinds = set()
    for rows in list(CATALOG.statements.values()) + list(CATALOG.alter_table_actions.values()):
        for entry in rows:
            if entry.lock_mode is LockMode.UNKNOWN or entry.duration_model is DurationModel.UNKNOWN:
                unknown_kinds.add(entry.kind)
                assert entry.lock_mode is LockMode.UNKNOWN
                assert entry.duration_model is DurationModel.UNKNOWN
                assert entry.calibration is Calibration.UNCALIBRATED
    assert unknown_kinds == {
        StatementKind.OTHER,
        AlterTableActionKind.OTHER,
        AlterTableActionKind.ADD_CONSTRAINT_OTHER,
    }


def test_every_calibrated_row_cites_docs_or_source() -> None:
    for rows in list(CATALOG.statements.values()) + list(CATALOG.alter_table_actions.values()):
        for entry in rows:
            assert entry.source
            if entry.calibration is Calibration.CALIBRATED:
                assert any(
                    s.startswith(("doc:", "src:", "release:")) for s in entry.source
                ), (entry.kind, entry.pg_versions)


# --------------------------------------------------------------------------
# Version awareness
# --------------------------------------------------------------------------


def test_no_row_spans_a_registered_breakpoint() -> None:
    assert CATALOG.breakpoints
    for bp in CATALOG.breakpoints:
        rows = CATALOG.rows_for(bp.kind)
        for entry in rows:
            assert not (entry.pg_versions.min < bp.version <= entry.pg_versions.max), (
                bp.kind,
                bp.version,
                entry.pg_versions,
            )
        # the breakpoint really separates two rows, i.e. it is not dead data
        before = {e.pg_versions for e in rows if e.pg_versions.covers(bp.version - 1)}
        after = {e.pg_versions for e in rows if e.pg_versions.covers(bp.version)}
        assert before and after and before.isdisjoint(after), (bp.kind, bp.version)


@pytest.mark.parametrize(
    ("kind", "version"),
    [
        (AlterTableActionKind.ADD_COLUMN_DEFAULT_NONVOLATILE, 11),
        (AlterTableActionKind.SET_NOT_NULL, 12),
        (AlterTableActionKind.ATTACH_PARTITION, 12),
        (StatementKind.RENAME_INDEX, 12),
        (StatementKind.ALTER_ENUM_ADD_VALUE, 12),
        (StatementKind.ALTER_ENUM_ADD_VALUE, 17),
        (StatementKind.CREATE_INDEX_CONCURRENTLY, 14),
        (StatementKind.GRANT, 18),
        (StatementKind.REVOKE, 18),
        (AlterTableActionKind.ADD_COLUMN_GENERATED_VIRTUAL, 18),
        (AlterTableActionKind.ADD_NOT_NULL_CONSTRAINT_NOT_VALID, 18),
        (StatementKind.MERGE, 15),
    ],
)
def test_known_behavior_changes_are_registered_breakpoints(
    kind: StatementKind | AlterTableActionKind, version: int
) -> None:
    assert any(bp.kind is kind and bp.version == version for bp in CATALOG.breakpoints)


def test_add_column_nonvolatile_default_rewrites_only_before_11() -> None:
    kind = AlterTableActionKind.ADD_COLUMN_DEFAULT_NONVOLATILE
    pg10 = CATALOG.entry_for(kind, 10)
    pg11 = CATALOG.entry_for(kind, 11)
    assert pg10.requires_table_rewrite and pg10.duration_model is DurationModel.PROPORTIONAL_TO_ROWS
    assert not pg11.requires_table_rewrite and pg11.duration_model is DurationModel.CONSTANT
    assert pg10.lock_mode is pg11.lock_mode is LockMode.ACCESS_EXCLUSIVE
    for version in range(11, CATALOG.version_domain.max + 1):
        assert not CATALOG.entry_for(kind, version).requires_table_rewrite


def test_volatile_default_rewrites_on_every_version() -> None:
    for kind in (
        AlterTableActionKind.ADD_COLUMN_DEFAULT_VOLATILE,
        AlterTableActionKind.ADD_COLUMN_SERIAL,
        AlterTableActionKind.ADD_COLUMN_IDENTITY,
    ):
        for version in VERSIONS:
            entry = CATALOG.entry_for(kind, version)
            assert entry.requires_table_rewrite, (kind, version)
            assert entry.duration_model is DurationModel.PROPORTIONAL_TO_ROWS


def test_attach_partition_parent_lock_drops_in_12() -> None:
    kind = AlterTableActionKind.ATTACH_PARTITION
    assert CATALOG.entry_for(kind, 11).lock_mode is LockMode.ACCESS_EXCLUSIVE
    assert CATALOG.entry_for(kind, 12).lock_mode is LockMode.SHARE_UPDATE_EXCLUSIVE
    # the partition itself stays ACCESS EXCLUSIVE in both
    for version in (11, 12):
        by_role = {r.role: r for r in CATALOG.entry_for(kind, version).affected_relations}
        assert by_role["partition"].lock_mode is LockMode.ACCESS_EXCLUSIVE


def test_rename_index_lock_drops_in_12() -> None:
    assert CATALOG.entry_for(StatementKind.RENAME_INDEX, 11).lock_mode is LockMode.ACCESS_EXCLUSIVE
    assert (
        CATALOG.entry_for(StatementKind.RENAME_INDEX, 12).lock_mode
        is LockMode.SHARE_UPDATE_EXCLUSIVE
    )


def test_enum_add_value_transaction_rules_by_version() -> None:
    kind = StatementKind.ALTER_ENUM_ADD_VALUE
    assert CATALOG.entry_for(kind, 11).transaction_block is TransactionBlock.FORBIDDEN
    assert CATALOG.entry_for(kind, 12).transaction_block is TransactionBlock.RESTRICTED
    assert CATALOG.entry_for(kind, 16).failure_mode != CATALOG.entry_for(kind, 17).failure_mode
    for version in VERSIONS:
        assert CATALOG.entry_for(kind, version).lock_mode is LockMode.NONE


def test_set_not_null_needs_live_context_only_from_12() -> None:
    kind = AlterTableActionKind.SET_NOT_NULL
    assert not CATALOG.entry_for(kind, 11).requires_live_context
    assert CATALOG.entry_for(kind, 12).requires_live_context
    for version in VERSIONS:
        entry = CATALOG.entry_for(kind, version)
        assert entry.lock_mode is LockMode.ACCESS_EXCLUSIVE
        assert entry.duration_model is DurationModel.PROPORTIONAL_TO_ROWS


def test_forms_unavailable_before_their_version_are_marked() -> None:
    cases: list[tuple[StatementKind | AlterTableActionKind, int]] = [
        (AlterTableActionKind.ADD_COLUMN_GENERATED_STORED, 12),
        (AlterTableActionKind.ADD_COLUMN_GENERATED_VIRTUAL, 18),
        (AlterTableActionKind.SET_EXPRESSION, 17),
        (AlterTableActionKind.DROP_EXPRESSION, 13),
        (AlterTableActionKind.SET_COMPRESSION, 14),
        (AlterTableActionKind.SET_ACCESS_METHOD, 15),
        (AlterTableActionKind.ADD_NOT_NULL_CONSTRAINT, 18),
        (AlterTableActionKind.DETACH_PARTITION_CONCURRENTLY, 14),
        (StatementKind.MERGE, 15),
        (StatementKind.REINDEX_CONCURRENTLY, 12),
        (StatementKind.CREATE_PROCEDURE, 11),
        (StatementKind.CALL, 11),
    ]
    for kind, introduced in cases:
        before = CATALOG.entry_for(kind, introduced - 1)
        after = CATALOG.entry_for(kind, introduced)
        assert not before.available and before.failure_mode, (kind, introduced)
        assert before.lock_mode is LockMode.NONE
        assert after.available, (kind, introduced)


def test_version_outside_domain_is_rejected() -> None:
    with pytest.raises(ValueError, match="outside the catalog's version domain"):
        CATALOG.entry_for(StatementKind.CREATE_INDEX, 9)
    with pytest.raises(ValueError):
        CATALOG.entries_for(StatementKind.CREATE_INDEX, 99)


def test_indirect_kinds_have_no_direct_rows() -> None:
    with pytest.raises(KeyError):
        CATALOG.rows_for(StatementKind.ALTER_TABLE)
    with pytest.raises(KeyError):
        CATALOG.rows_for(StatementKind.DO_BLOCK)


# --------------------------------------------------------------------------
# The high-frequency entries the user called out
# --------------------------------------------------------------------------


def test_create_index_plain_versus_concurrently() -> None:
    plain = CATALOG.entry_for(StatementKind.CREATE_INDEX, 16)
    assert plain.lock_mode is LockMode.SHARE
    assert plain.blocks_writes and not plain.blocks_reads
    assert plain.duration_model is DurationModel.PROPORTIONAL_TO_ROWS
    assert plain.transaction_block is TransactionBlock.ALLOWED
    assert plain.failure_mode is None

    cic = CATALOG.entry_for(StatementKind.CREATE_INDEX_CONCURRENTLY, 16)
    assert cic.lock_mode is LockMode.SHARE_UPDATE_EXCLUSIVE
    assert not cic.blocks_writes and not cic.blocks_reads
    assert cic.duration_model is DurationModel.PROPORTIONAL_TO_ROWS
    assert cic.transaction_block is TransactionBlock.FORBIDDEN
    assert cic.failure_mode is not None and "invalid" in cic.failure_mode.lower()
    assert cic.lock_mode.level < plain.lock_mode.level


def test_foreign_key_locks_both_tables() -> None:
    for kind in (
        AlterTableActionKind.ADD_FOREIGN_KEY,
        AlterTableActionKind.ADD_FOREIGN_KEY_NOT_VALID,
    ):
        entry = CATALOG.entry_for(kind, 16)
        by_role = {r.role: r for r in entry.affected_relations}
        assert entry.lock_mode is LockMode.SHARE_ROW_EXCLUSIVE
        assert by_role["referenced_table"].lock_mode is LockMode.SHARE_ROW_EXCLUSIVE
        assert not by_role["referenced_table"].optional
    assert (
        CATALOG.entry_for(AlterTableActionKind.ADD_FOREIGN_KEY, 16).duration_model
        is DurationModel.PROPORTIONAL_TO_ROWS
    )
    assert (
        CATALOG.entry_for(AlterTableActionKind.ADD_FOREIGN_KEY_NOT_VALID, 16).duration_model
        is DurationModel.CONSTANT
    )


def test_dml_backfills_require_live_context() -> None:
    for kind in (
        StatementKind.UPDATE,
        StatementKind.DELETE,
        StatementKind.UPDATE_BATCHED,
        StatementKind.DELETE_BATCHED,
    ):
        entry = CATALOG.entry_for(kind, 16)
        assert entry.lock_mode is LockMode.ROW_EXCLUSIVE
        assert entry.duration_model is DurationModel.PROPORTIONAL_TO_ROWS_MATCHED
        assert entry.requires_live_context and entry.live_context
        assert entry.blocks_writes and entry.row_locks and not entry.blocks_reads
    for kind in (StatementKind.UPDATE_WITHOUT_WHERE, StatementKind.DELETE_WITHOUT_WHERE):
        entry = CATALOG.entry_for(kind, 16)
        assert entry.duration_model is DurationModel.PROPORTIONAL_TO_ROWS


def test_harmless_majority_resolves_clean() -> None:
    for kind in (
        StatementKind.COMMENT_ON,
        StatementKind.CREATE_FUNCTION,
        StatementKind.CREATE_PROCEDURE,
        StatementKind.CREATE_ENUM_TYPE,
        StatementKind.CREATE_TABLE,
        StatementKind.CREATE_SEQUENCE,
        StatementKind.BEGIN,
        StatementKind.COMMIT,
    ):
        entry = CATALOG.entry_for(kind, 16)
        assert entry.calibration is Calibration.CALIBRATED, kind
        assert entry.duration_model is DurationModel.CONSTANT, kind
        assert not entry.blocks_reads and not entry.blocks_writes, kind
        assert not entry.requires_table_rewrite
    trigger = CATALOG.entry_for(StatementKind.CREATE_TRIGGER, 16)
    assert trigger.lock_mode is LockMode.SHARE_ROW_EXCLUSIVE
    assert trigger.duration_model is DurationModel.CONSTANT
    assert not trigger.blocks_reads


# --------------------------------------------------------------------------
# Loader: every inconsistency fails loudly
# --------------------------------------------------------------------------


def test_bundled_catalog_round_trips_through_parse(raw_catalog: dict[str, Any]) -> None:
    assert parse_catalog(raw_catalog).version_domain == CATALOG.version_domain


def _expect_error(data: dict[str, Any], match: str) -> None:
    with pytest.raises(CatalogError, match=match):
        parse_catalog(data)


def test_missing_statement_kind_fails(raw_catalog: dict[str, Any]) -> None:
    data = copy.deepcopy(raw_catalog)
    del data["statements"]["create_index"]
    _expect_error(data, "no catalog rows for StatementKinds \\['create_index'\\]")


def test_missing_action_kind_fails(raw_catalog: dict[str, Any]) -> None:
    data = copy.deepcopy(raw_catalog)
    del data["alter_table_actions"]["drop_column"]
    _expect_error(data, "no catalog rows for AlterTableActionKinds \\['drop_column'\\]")


def test_unknown_kind_name_fails(raw_catalog: dict[str, Any]) -> None:
    data = copy.deepcopy(raw_catalog)
    data["statements"]["create_indexx"] = data["statements"]["create_index"]
    _expect_error(data, "kind 'create_indexx' is not one of")


def test_version_gap_fails(raw_catalog: dict[str, Any]) -> None:
    data = copy.deepcopy(raw_catalog)
    data["statements"]["create_index"][0]["pg_versions"] = [11, 18]
    _expect_error(data, "create_index: no default row covers PG 10")


def test_overlapping_default_rows_fail(raw_catalog: dict[str, Any]) -> None:
    data = copy.deepcopy(raw_catalog)
    rows = data["statements"]["create_index"]
    rows.append(copy.deepcopy(rows[0]))
    _expect_error(data, "2 default rows overlap on PG 10")


def test_row_spanning_a_breakpoint_fails(raw_catalog: dict[str, Any]) -> None:
    data = copy.deepcopy(raw_catalog)
    rows = data["alter_table_actions"]["add_column_default_nonvolatile"]
    merged = copy.deepcopy(rows[1])
    merged["pg_versions"] = [10, 18]
    data["alter_table_actions"]["add_column_default_nonvolatile"] = [merged]
    _expect_error(data, "spans the 11 breakpoint")


def test_blocks_reads_contradicting_matrix_fails(raw_catalog: dict[str, Any]) -> None:
    data = copy.deepcopy(raw_catalog)
    data["statements"]["create_index"][0]["blocks_reads"] = True
    _expect_error(data, "blocks_reads=True contradicts the matrix for SHARE")


def test_share_lock_claiming_no_write_block_fails(raw_catalog: dict[str, Any]) -> None:
    data = copy.deepcopy(raw_catalog)
    data["statements"]["create_index"][0]["blocks_writes"] = False
    _expect_error(data, "SHARE blocks writes but blocks_writes is false")


def test_unknown_when_key_fails(raw_catalog: dict[str, Any]) -> None:
    data = copy.deepcopy(raw_catalog)
    data["statements"]["create_index"][0]["when"] = {"nonexistent_field": True}
    _expect_error(data, "when references unknown IR attribute 'nonexistent_field'")


def test_unknown_lock_mode_outside_grab_bag_fails(raw_catalog: dict[str, Any]) -> None:
    data = copy.deepcopy(raw_catalog)
    row = data["statements"]["create_index"][0]
    row["lock_mode"] = "UNKNOWN"
    row["blocks_reads"] = row["blocks_writes"] = True
    row["calibration"] = "UNCALIBRATED"
    _expect_error(data, "UNKNOWN lock_mode/duration_model is only allowed for OTHER kinds")


def test_calibrated_row_without_citation_fails(raw_catalog: dict[str, Any]) -> None:
    data = copy.deepcopy(raw_catalog)
    data["statements"]["create_index"][0]["source"] = ["I remember it this way"]
    _expect_error(data, "CALIBRATED rows need a doc:/src:/release: citation")


def test_unknown_field_fails(raw_catalog: dict[str, Any]) -> None:
    data = copy.deepcopy(raw_catalog)
    data["statements"]["create_index"][0]["lockmode"] = "SHARE"
    _expect_error(data, "unknown fields \\['lockmode'\\]")


def test_missing_required_field_fails(raw_catalog: dict[str, Any]) -> None:
    data = copy.deepcopy(raw_catalog)
    del data["statements"]["create_index"][0]["duration_model"]
    _expect_error(data, "missing required fields \\['duration_model'\\]")


def test_unavailable_row_must_take_no_lock(raw_catalog: dict[str, Any]) -> None:
    data = copy.deepcopy(raw_catalog)
    row = data["statements"]["merge"][0]
    assert row["available"] is False
    row["lock_mode"] = "ROW EXCLUSIVE"
    _expect_error(data, "available: false rows take no lock")


def test_asymmetric_conflict_matrix_fails(raw_catalog: dict[str, Any]) -> None:
    data = copy.deepcopy(raw_catalog)
    data["conflict_matrix"]["ACCESS SHARE"] = []
    _expect_error(data, "asymmetric")


def test_incomplete_conflict_matrix_fails(raw_catalog: dict[str, Any]) -> None:
    data = copy.deepcopy(raw_catalog)
    del data["conflict_matrix"]["ROW SHARE"]
    _expect_error(data, "missing rows for \\['ROW SHARE'\\]")


def test_direct_rows_for_per_action_kind_fail(raw_catalog: dict[str, Any]) -> None:
    data = copy.deepcopy(raw_catalog)
    data["statements"]["alter_table"] = data["statements"]["create_index"]
    _expect_error(data, "alter_table resolves via per_action and must not have direct rows")


def test_primary_lock_must_match_target_role(raw_catalog: dict[str, Any]) -> None:
    data = copy.deepcopy(raw_catalog)
    row = data["alter_table_actions"]["add_foreign_key"][0]
    row["affected_relations"][0]["lock_mode"] = "ACCESS EXCLUSIVE"
    _expect_error(data, "lock_mode SHARE ROW EXCLUSIVE does not match the primary affected")


def test_live_context_flag_and_text_must_agree(raw_catalog: dict[str, Any]) -> None:
    data = copy.deepcopy(raw_catalog)
    data["statements"]["update"][0]["live_context"] = None
    _expect_error(data, "requires_live_context needs live_context")


def test_unknown_affected_role_fails(raw_catalog: dict[str, Any]) -> None:
    data = copy.deepcopy(raw_catalog)
    data["statements"]["create_index"][0]["affected_relations"] = [
        {"role": "victim", "lock_mode": "SHARE"}
    ]
    _expect_error(data, "unknown affected-relation role 'victim'")


def test_breakpoint_for_unknown_kind_fails(raw_catalog: dict[str, Any]) -> None:
    data = copy.deepcopy(raw_catalog)
    data["version_breakpoints"].append(
        {"kind": "statement/nope", "version": 12, "what_changed": "x", "source": "y"}
    )
    _expect_error(data, "unknown kind 'statement/nope'")


def test_wrong_schema_version_fails(raw_catalog: dict[str, Any]) -> None:
    data = copy.deepcopy(raw_catalog)
    data["schema_version"] = 99
    _expect_error(data, "schema_version 99")


# --------------------------------------------------------------------------
# Loader rejections: structural details
# --------------------------------------------------------------------------


def test_invalid_pg_versions_shapes_fail(raw_catalog: dict[str, Any]) -> None:
    for bad in ([12], [12, 11], [9, 18], "12-18", [12, True]):
        data = copy.deepcopy(raw_catalog)
        data["statements"]["create_index"][0]["pg_versions"] = bad
        with pytest.raises(CatalogError, match="pg_versions"):
            parse_catalog(data)


def test_non_boolean_flag_fails(raw_catalog: dict[str, Any]) -> None:
    data = copy.deepcopy(raw_catalog)
    data["statements"]["create_index"][0]["blocks_writes"] = "yes"
    _expect_error(data, "blocks_writes must be a boolean")


def test_empty_string_fields_fail(raw_catalog: dict[str, Any]) -> None:
    data = copy.deepcopy(raw_catalog)
    data["statements"]["create_index"][0]["notes"] = "   "
    _expect_error(data, "notes must be a non-empty string or null")


def test_invalid_enum_values_fail(raw_catalog: dict[str, Any]) -> None:
    data = copy.deepcopy(raw_catalog)
    data["statements"]["create_index"][0]["lock_mode"] = "SUPER EXCLUSIVE"
    _expect_error(data, "lock_mode 'SUPER EXCLUSIVE' is not one of")
    data = copy.deepcopy(raw_catalog)
    data["statements"]["create_index"][0]["duration_model"] = "FOREVER"
    _expect_error(data, "duration_model 'FOREVER' is not one of")


def test_empty_source_fails(raw_catalog: dict[str, Any]) -> None:
    data = copy.deepcopy(raw_catalog)
    data["statements"]["create_index"][0]["source"] = []
    _expect_error(data, "source must be a non-empty string or list")


def test_bare_string_source_is_accepted(raw_catalog: dict[str, Any]) -> None:
    data = copy.deepcopy(raw_catalog)
    data["statements"]["create_index"][0]["source"] = "doc:18/sql-createindex.html"
    parse_catalog(data)


def test_none_lock_mode_cannot_block(raw_catalog: dict[str, Any]) -> None:
    data = copy.deepcopy(raw_catalog)
    row = data["statements"]["commit"][0]
    row["blocks_reads"] = True
    _expect_error(data, "lock_mode NONE cannot block reads or writes")


def test_row_locks_require_matched_rows_scope(raw_catalog: dict[str, Any]) -> None:
    data = copy.deepcopy(raw_catalog)
    data["statements"]["create_index"][0]["row_locks"] = "FOR UPDATE"
    _expect_error(data, "row_locks is only meaningful with write_block_scope MATCHED_ROWS")


def test_matched_rows_scope_requires_row_locks(raw_catalog: dict[str, Any]) -> None:
    data = copy.deepcopy(raw_catalog)
    row = data["statements"]["update"][0]
    del row["row_locks"]
    _expect_error(data, "MATCHED_ROWS scope must name the row_locks taken")


def test_write_block_scope_none_contradicting_blocks_writes_fails(
    raw_catalog: dict[str, Any],
) -> None:
    data = copy.deepcopy(raw_catalog)
    data["statements"]["create_index"][0]["write_block_scope"] = "NONE"
    _expect_error(data, "write_block_scope NONE contradicts blocks_writes true")


def test_row_lock_write_block_needs_explicit_scope(raw_catalog: dict[str, Any]) -> None:
    data = copy.deepcopy(raw_catalog)
    row = data["statements"]["update"][0]
    del row["write_block_scope"]
    _expect_error(data, "blocks_writes is true but the lock does not imply it")


def test_transaction_restriction_needs_explanation(raw_catalog: dict[str, Any]) -> None:
    data = copy.deepcopy(raw_catalog)
    row = data["statements"]["create_index_concurrently"][0]
    row["failure_mode"] = None
    row["notes"] = None
    _expect_error(data, "transaction_block restriction needs notes or failure_mode")


def test_unavailable_row_needs_failure_mode(raw_catalog: dict[str, Any]) -> None:
    data = copy.deepcopy(raw_catalog)
    data["statements"]["merge"][0]["failure_mode"] = None
    _expect_error(data, "available: false rows must state the failure_mode")


def test_duplicate_affected_role_fails(raw_catalog: dict[str, Any]) -> None:
    data = copy.deepcopy(raw_catalog)
    rels = data["alter_table_actions"]["add_foreign_key"][0]["affected_relations"]
    rels.append(dict(rels[0]))
    _expect_error(data, "duplicate affected-relation role 'target'")


def test_wrong_statically_resolvable_declaration_fails(raw_catalog: dict[str, Any]) -> None:
    data = copy.deepcopy(raw_catalog)
    data["statements"]["drop_index"][0]["affected_relations"][2]["statically_resolvable"] = True
    _expect_error(data, "role 'owning_table' must declare statically_resolvable=False")


def test_overlapping_when_variants_fail(raw_catalog: dict[str, Any]) -> None:
    data = copy.deepcopy(raw_catalog)
    rows = data["statements"]["lock_table"]
    rows.append(copy.deepcopy(rows[0]))  # duplicate the {mode: 1} variant
    _expect_error(data, "identical when=")


def test_unknown_top_level_key_fails(raw_catalog: dict[str, Any]) -> None:
    data = copy.deepcopy(raw_catalog)
    data["extra"] = {}
    _expect_error(data, "top-level keys must be exactly")


def test_breakpoint_outside_domain_fails(raw_catalog: dict[str, Any]) -> None:
    data = copy.deepcopy(raw_catalog)
    data["version_breakpoints"].append(
        {"kind": "statement/merge", "version": 10, "what_changed": "x", "source": "y"}
    )
    _expect_error(data, "version 10 must lie in")


def test_malformed_breakpoint_fails(raw_catalog: dict[str, Any]) -> None:
    data = copy.deepcopy(raw_catalog)
    data["version_breakpoints"].append({"kind": "statement/merge", "version": 15})
    _expect_error(data, "each breakpoint needs exactly")


def test_bad_kind_reference_shapes_fail(raw_catalog: dict[str, Any]) -> None:
    for bad in ("merge", "dml/merge", 12):
        data = copy.deepcopy(raw_catalog)
        data["version_breakpoints"].append(
            {"kind": bad, "version": 15, "what_changed": "x", "source": "y"}
        )
        with pytest.raises(CatalogError):
            parse_catalog(data)


def test_direct_resolution_must_be_omitted(raw_catalog: dict[str, Any]) -> None:
    data = copy.deepcopy(raw_catalog)
    data["statement_resolution"]["create_index"] = "direct"
    _expect_error(data, "direct is the default; omit it")


def test_alter_table_must_resolve_per_action(raw_catalog: dict[str, Any]) -> None:
    data = copy.deepcopy(raw_catalog)
    del data["statement_resolution"]["alter_table"]
    _expect_error(data, "alter_table must resolve per_action")


def test_unknown_marker_requires_uncalibrated(raw_catalog: dict[str, Any]) -> None:
    data = copy.deepcopy(raw_catalog)
    data["statements"]["other"][0]["calibration"] = "CALIBRATED"
    _expect_error(data, "UNKNOWN rows must be UNCALIBRATED")


def test_unknown_marker_requires_worst_case_blocking(raw_catalog: dict[str, Any]) -> None:
    data = copy.deepcopy(raw_catalog)
    data["statements"]["other"][0]["blocks_reads"] = False
    _expect_error(data, "UNKNOWN lock_mode must assume the worst case")


def test_load_catalog_file_round_trips(tmp_path: Any, raw_catalog: dict[str, Any]) -> None:
    from pgverdict.catalog import load_catalog_file

    path = tmp_path / "catalog.yaml"
    path.write_text(yaml.safe_dump(raw_catalog), encoding="utf-8")
    catalog = load_catalog_file(path)
    assert catalog.version_domain == CATALOG.version_domain
    assert catalog.conflict_matrix == CATALOG.conflict_matrix


def test_conflict_matrix_shape_errors(raw_catalog: dict[str, Any]) -> None:
    data = copy.deepcopy(raw_catalog)
    data["conflict_matrix"] = []
    _expect_error(data, "must be a mapping of lock mode")
    data = copy.deepcopy(raw_catalog)
    data["conflict_matrix"]["NONE"] = []
    _expect_error(data, "NONE is not a table lock mode")
    data = copy.deepcopy(raw_catalog)
    data["conflict_matrix"]["SHARE"] = "ACCESS EXCLUSIVE"
    _expect_error(data, "SHARE: conflicts must be a list")


def test_affected_relations_shape_errors(raw_catalog: dict[str, Any]) -> None:
    data = copy.deepcopy(raw_catalog)
    data["statements"]["create_index"][0]["affected_relations"] = "target"
    _expect_error(data, "affected_relations must be a list")
    data = copy.deepcopy(raw_catalog)
    data["statements"]["create_index"][0]["affected_relations"] = ["target"]
    _expect_error(data, "each affected relation must be a mapping")
    data = copy.deepcopy(raw_catalog)
    data["statements"]["create_index"][0]["affected_relations"] = [
        {"role": "target", "lock_mode": "SHARE", "surprise": 1}
    ]
    _expect_error(data, "affected relation has unknown keys")


def test_when_must_be_a_non_empty_mapping(raw_catalog: dict[str, Any]) -> None:
    data = copy.deepcopy(raw_catalog)
    data["statements"]["create_index"][0]["when"] = []
    _expect_error(data, "when must be a non-empty mapping or omitted")


def test_entry_must_be_a_mapping(raw_catalog: dict[str, Any]) -> None:
    data = copy.deepcopy(raw_catalog)
    data["statements"]["create_index"] = ["SHARE"]
    _expect_error(data, "entry must be a mapping")
    data = copy.deepcopy(raw_catalog)
    data["statements"]["create_index"] = []
    _expect_error(data, "rows must be a non-empty list")


def test_live_context_without_flag_fails(raw_catalog: dict[str, Any]) -> None:
    data = copy.deepcopy(raw_catalog)
    data["statements"]["create_index"][0]["live_context"] = "table size"
    _expect_error(data, "live_context given but requires_live_context is false")


def test_unavailable_row_must_be_constant(raw_catalog: dict[str, Any]) -> None:
    data = copy.deepcopy(raw_catalog)
    data["statements"]["merge"][0]["duration_model"] = "PROPORTIONAL_TO_ROWS"
    _expect_error(data, "available: false rows must be CONSTANT and not rewrite")
