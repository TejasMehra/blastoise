"""Duration model tests: stated inputs, staleness widening, honest refusals."""

from __future__ import annotations

from verdict_helpers import relation, snapshot

from blastoise.catalog.model import Calibration, DurationModel
from blastoise.ir import AlterTableActionKind, StatementKind
from blastoise.verdict import DURATION_CONSTANTS, CannotEstimate, DurationEstimate, Method
from blastoise.verdict.duration import (
    BYTES_FAMILY_BY_KIND,
    ROWS_FAMILY_BY_KIND,
    constant_op_estimate,
    empty_relation_estimate,
    estimate_from_bytes,
    estimate_from_rows,
    estimate_index_rebuilds,
)

SERVER = snapshot().server

# The constants the 2026-08-21 scale harness measured; everything else is
# still an admitted guess awaiting the calibration loop.
MEASURED = {
    "fk_validation",
    "index_build_btree",
    "index_build_expression",
    "index_bytes",
    "dml_update",
}


def test_calibration_state_matches_the_harness_record() -> None:
    for constant in DURATION_CONSTANTS.values():
        if constant.key in MEASURED:
            assert constant.calibration is Calibration.MEASURED
            assert constant.basis.startswith("measured 2026-08-21")
        else:
            assert constant.calibration is Calibration.UNCALIBRATED
            assert constant.basis.startswith("guess:")


def test_measured_widening_is_tighter_than_uncalibrated() -> None:
    measured = estimate_from_rows(
        relation("t", rows=10_000_000), SERVER, "fk_validation"
    )
    guessed = estimate_from_rows(
        relation("t", rows=10_000_000), SERVER, "dml_delete"
    )
    assert isinstance(measured, DurationEstimate) and isinstance(guessed, DurationEstimate)
    assert measured.high_ms * 10 < measured.point_ms * 25  # ~2x, not 4x
    assert guessed.high_ms * 10 > guessed.point_ms * 35


def test_family_maps_only_name_defined_constants() -> None:
    for family in (*ROWS_FAMILY_BY_KIND.values(), *BYTES_FAMILY_BY_KIND.values()):
        assert family in DURATION_CONSTANTS


def test_every_calibrated_proportional_kind_has_a_family() -> None:
    """Every CALIBRATED PROPORTIONAL_TO_ROWS row must map to a throughput."""
    from blastoise.catalog.loader import load_catalog

    catalog = load_catalog()
    missing: list[str] = []
    for kind in (*StatementKind, *AlterTableActionKind):
        try:
            rows = catalog.rows_for(kind)
        except KeyError:
            continue
        for entry in rows:
            if (
                entry.calibration is Calibration.CALIBRATED
                and entry.available
                and entry.duration_model is DurationModel.PROPORTIONAL_TO_ROWS
                and kind not in ROWS_FAMILY_BY_KIND
            ):
                missing.append(kind.value)
    assert missing == []


def test_point_estimate_is_rows_over_throughput_plus_overhead() -> None:
    rel = relation("users", rows=1_000_000)
    estimate = estimate_from_rows(rel, SERVER, "index_build_btree")
    assert isinstance(estimate, DurationEstimate)
    # 1M rows / 1M rows/s = 1s, plus the 10 ms fixed overhead
    assert estimate.point_ms == 1010
    assert estimate.low_ms < estimate.point_ms < estimate.high_ms
    assert estimate.method is Method.SIMULATED
    assert estimate.constant_key == "index_build_btree"
    assert any("reltuples=1000000" in text for text in estimate.inputs)


def test_tiny_table_floors_at_the_fixed_overhead() -> None:
    # 10 rows of proportional work rounds to zero; what remains is the
    # constant-op overhead (10 ms point, 1-100 ms spread) — the harness
    # measured 2-14 ms on 1k-row cases, inside this interval.
    estimate = estimate_from_rows(relation("t", rows=10), SERVER, "index_build_btree")
    assert isinstance(estimate, DurationEstimate)
    assert estimate.point_ms == 10
    assert estimate.low_ms == 1
    assert estimate.high_ms == 100


def test_stale_stats_widen_the_interval() -> None:
    fresh = estimate_from_rows(
        relation("t", rows=1_000_000, analyzed_hours_ago=1), SERVER, "heap_rewrite"
    )
    stale = estimate_from_rows(
        relation("t", rows=1_000_000, analyzed_hours_ago=24 * 60), SERVER, "heap_rewrite"
    )
    assert isinstance(fresh, DurationEstimate) and isinstance(stale, DurationEstimate)
    assert stale.high_ms > fresh.high_ms
    assert stale.low_ms < fresh.low_ms
    assert fresh.point_ms == stale.point_ms  # staleness widens, never shifts
    assert stale.confidence == "low"
    assert any("stale" in text for text in stale.inputs)


def test_churn_widens_the_interval() -> None:
    calm = estimate_from_rows(relation("t", rows=1_000_000, n_mod=0), SERVER, "heap_rewrite")
    churned = estimate_from_rows(
        relation("t", rows=1_000_000, n_mod=500_000), SERVER, "heap_rewrite"
    )
    assert isinstance(calm, DurationEstimate) and isinstance(churned, DurationEstimate)
    assert churned.high_ms > calm.high_ms


def test_churn_swamping_the_estimate_refuses() -> None:
    result = estimate_from_rows(
        relation("t", rows=100, n_mod=1_000_000), SERVER, "heap_rewrite"
    )
    assert isinstance(result, CannotEstimate)
    assert "statistics unusable" in result.reason
    assert result.method is Method.UNVERIFIED


def test_never_analyzed_zero_refuses() -> None:
    result = estimate_from_rows(
        relation("t", rows=0, analyzed_hours_ago=None), SERVER, "heap_rewrite"
    )
    assert isinstance(result, CannotEstimate)
    assert "never analyzed" in result.reason


def test_analyzed_empty_table_estimates_only_the_overhead() -> None:
    result = estimate_from_rows(
        relation("t", rows=0, analyzed_hours_ago=2), SERVER, "heap_rewrite"
    )
    assert isinstance(result, DurationEstimate)
    assert result.point_ms == 10


def test_missing_relation_refuses_with_reason() -> None:
    result = estimate_from_rows(
        relation("gone", exists=False), SERVER, "heap_rewrite"
    )
    assert isinstance(result, CannotEstimate)
    assert "does not exist" in result.reason


def test_unavailable_rows_refuses_with_reason() -> None:
    result = estimate_from_rows(relation("t", rows=None), SERVER, "heap_rewrite")
    assert isinstance(result, CannotEstimate)
    assert "no usable row-count estimate" in result.reason


def test_bytes_estimate() -> None:
    estimate = estimate_from_bytes(
        500_000_000, "index_bytes", inputs=("size=500MB",)
    )
    # 500 MB / 9.5 MB/s (measured) ≈ 52.6s, plus the 10 ms overhead
    expected = 500_000_000 * 1000 // DURATION_CONSTANTS["index_bytes"].value + 10
    assert estimate.point_ms == expected
    assert estimate.method is Method.SIMULATED


def test_index_rebuild_estimate_sums_per_index_work() -> None:
    rel = relation("t", rows=10_000_000)
    single = estimate_index_rebuilds(rel, SERVER, (("ix_a", "index_build_btree"),))
    both = estimate_index_rebuilds(
        rel,
        SERVER,
        (("ix_a", "index_build_btree"), ("ix_b", "index_build_expression")),
    )
    assert isinstance(single, DurationEstimate) and isinstance(both, DurationEstimate)
    # 10M btree rows at 1M rows/s = 10s; adding an expression rebuild
    # (10M / 250k = 40s) sums, and the slowest family names the estimate.
    assert single.point_ms == 10_010
    assert both.point_ms == 50_010
    assert single.constant_key == "index_build_btree"
    assert both.constant_key == "index_build_expression"
    assert any("rebuild ix_b" in text for text in both.inputs)


def test_index_rebuild_estimate_refuses_like_rows_do() -> None:
    result = estimate_index_rebuilds(
        relation("gone", exists=False), SERVER, (("ix", "index_build_btree"),)
    )
    assert isinstance(result, CannotEstimate)
    assert "does not exist" in result.reason


def test_constant_op_and_empty_relation_estimates() -> None:
    proven = empty_relation_estimate("created in this file")
    assert proven.method is Method.PROVEN
    assert proven.point_ms == DURATION_CONSTANTS["constant_op"].value
    observed = constant_op_estimate(method=Method.OBSERVED, inputs=("narrowed",))
    assert observed.method is Method.OBSERVED
    assert observed.low_ms >= 1
