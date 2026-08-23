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

# The constants the scale harness and the 2026-08-22 re-measurement measured;
# everything else is still an admitted guess awaiting the calibration loop.
MEASURED = {
    "fk_validation",
    "index_build_btree",
    "index_build_expression",
    "index_bytes",
    "dml_update",
    "heap_rewrite",
    "add_column_rewrite",
    "validation_scan",
    "dml_delete",
}


def test_calibration_state_matches_the_harness_record() -> None:
    for constant in DURATION_CONSTANTS.values():
        if constant.key in MEASURED:
            assert constant.calibration is Calibration.MEASURED
            assert constant.basis.startswith("measured")
            # A measured constant states its provenance, and its band is
            # derived from it: at least two runs and an observed spread.
            assert constant.runs >= 2
            assert constant.spread_tenths > 0
        else:
            assert constant.calibration is Calibration.UNCALIBRATED
            assert constant.basis.startswith("guess:")
            assert constant.runs == 0


def test_widening_is_derived_from_provenance_not_a_flat_tier() -> None:
    from blastoise.catalog.model import Calibration
    from blastoise.verdict.constants import (
        ConstantUnit,
        DurationConstant,
        base_widen_tenths,
    )

    def const(cal: Calibration, runs: int, spread: int) -> DurationConstant:
        return DurationConstant(
            key="k", unit=ConstantUnit.ROWS_PER_SECOND, value=1000,
            calibration=cal, basis="b", runs=runs, spread_tenths=spread,
        )

    # A guess is 4x whatever it claims.
    assert base_widen_tenths(const(Calibration.UNCALIBRATED, 0, 0)) == 40
    # Measured once: variance unknown, a guess with better manners (3x).
    assert base_widen_tenths(const(Calibration.MEASURED, 1, 12)) == 30
    # Measured twice with tight agreement: the drift floor (1.5x), not the
    # 2x a flat MEASURED tier would give — this is the heap_rewrite case.
    assert base_widen_tenths(const(Calibration.MEASURED, 3, 13)) == 15
    # Measured, but shapes scatter wide: held at the 2x cap, not wider — the
    # scatter is a sub-family split, not licence for a guess band.
    assert base_widen_tenths(const(Calibration.MEASURED, 2, 38)) == 20
    # A spread between floor and cap is honored exactly.
    assert base_widen_tenths(const(Calibration.MEASURED, 2, 17)) == 17
    # Calibrated across environments: 1.5x.
    assert base_widen_tenths(const(Calibration.CALIBRATED, 5, 10)) == 15


def test_the_split_rewrite_constants_keep_their_order() -> None:
    # The heap_rewrite split exists so a plain relabel is never modeled
    # slower than a compute-per-row add of the same table: at every size
    # the plain estimate sits below the compute one. (Whether a 1M-row
    # rewrite lands under or over the 20 s line is, since 2026-08-23, a
    # boundary refusal on this anchor — see test_hardware_boundary — so the
    # old "plain under, compute over at 1M" assertion is no longer the
    # claim.)
    for rows in (100_000, 1_000_000, 5_000_000):
        rel = relation("t", rows=rows)
        plain = estimate_from_rows(rel, SERVER, "heap_rewrite")
        compute = estimate_from_rows(rel, SERVER, "add_column_rewrite")
        assert isinstance(plain, DurationEstimate) and isinstance(compute, DurationEstimate)
        assert plain.point_ms < compute.point_ms
    big = estimate_from_rows(relation("t", rows=5_000_000), SERVER, "heap_rewrite")
    assert isinstance(big, DurationEstimate)
    assert big.low_ms >= 20_000  # a 5M-row rewrite is over the line on any reading


def test_measured_widening_is_tighter_than_uncalibrated() -> None:
    # Every rows/bytes constant is measured since 2026-08-23 (dml_delete was
    # the last guess), so the uncalibrated side is a synthetic constant.
    measured = estimate_from_rows(
        relation("t", rows=10_000_000), SERVER, "fk_validation"
    )
    assert isinstance(measured, DurationEstimate)
    assert measured.high_ms * 10 < measured.point_ms * 25  # ~2x, not 4x
    from blastoise.verdict.constants import ConstantUnit, DurationConstant, base_widen_tenths

    guess = DurationConstant(
        key="k", unit=ConstantUnit.ROWS_PER_SECOND, value=1000,
        calibration=Calibration.UNCALIBRATED, basis="guess: synthetic",
    )
    assert base_widen_tenths(guess) == 40


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
    rate = DURATION_CONSTANTS["index_build_btree"].value
    rel = relation("users", rows=rate)
    estimate = estimate_from_rows(rel, SERVER, "index_build_btree")
    assert isinstance(estimate, DurationEstimate)
    # rate rows / rate rows/s = 1s, plus the 10 ms fixed overhead
    assert estimate.point_ms == 1010
    assert estimate.low_ms < estimate.point_ms < estimate.high_ms
    assert estimate.method is Method.SIMULATED
    assert estimate.constant_key == "index_build_btree"
    assert any(f"reltuples={rate}" in text for text in estimate.inputs)


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
    # heap_rewrite is now a measured, low-variance constant (base 1.5x), so
    # month-old stats degrade its confidence from high without reaching the
    # "low" floor a 4x guess would — the confidence tracks what is actually
    # known, which is the point.
    assert fresh.confidence == "high"
    assert stale.confidence in ("medium", "low")
    assert stale.confidence != fresh.confidence
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
    btree = DURATION_CONSTANTS["index_build_btree"].value
    expr = DURATION_CONSTANTS["index_build_expression"].value
    rows = 10 * btree
    rel = relation("t", rows=rows)
    single = estimate_index_rebuilds(rel, SERVER, (("ix_a", "index_build_btree"),))
    both = estimate_index_rebuilds(
        rel,
        SERVER,
        (("ix_a", "index_build_btree"), ("ix_b", "index_build_expression")),
    )
    assert isinstance(single, DurationEstimate) and isinstance(both, DurationEstimate)
    # ten seconds of btree rows; adding an expression rebuild of the same
    # rows sums, and the slowest family names the estimate.
    assert single.point_ms == 10_010
    assert both.point_ms == 10_000 + rows * 1000 // expr + 10
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
