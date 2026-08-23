"""The validation harness's own invariants: the corpus loads and is shaped
as the brief requires, the ground-truth labeling rule behaves as written,
and the hardware normalization arithmetic is right. No database needed."""

from __future__ import annotations

from collections import Counter

import pytest

from validation.harness import calibration as cal
from validation.harness.corpus import (
    ADVERSARIAL,
    FAMILIES,
    FIXTURE_TABLES,
    Case,
    load_corpus,
)
from validation.harness.labeling import Measured, block_type, label, outcome, strongest

# --- corpus ------------------------------------------------------------------


@pytest.fixture(scope="module")
def corpus() -> tuple[Case, ...]:
    return load_corpus()


def test_corpus_has_at_least_120_cases(corpus: tuple[Case, ...]) -> None:
    assert len(corpus) >= 120
    assert len({c.id for c in corpus}) == len(corpus)


def test_every_family_is_represented(corpus: tuple[Case, ...]) -> None:
    present = {c.family for c in corpus}
    assert present == set(FAMILIES)


def test_weighted_by_wild_frequency_not_evenly(corpus: tuple[Case, ...]) -> None:
    """The four families the brief names must outweigh the long tail."""
    counts = Counter(c.family for c in corpus)
    heavy = counts["index_creation"] + counts["foreign_keys"] + counts["add_column"]
    heavy += counts["dml_backfills"]
    assert heavy >= len(corpus) * 0.4
    assert counts["transactions"] < counts["add_column"]


def test_adversarial_cases_in_both_directions(corpus: tuple[Case, ...]) -> None:
    counts = Counter(c.adversarial for c in corpus if c.adversarial)
    assert set(counts) == set(ADVERSARIAL)
    assert counts["looks_dangerous"] >= 10
    assert counts["looks_benign"] >= 10
    assert counts["judgment_call"] >= 5


def test_named_adversarial_scenarios_present(corpus: tuple[Case, ...]) -> None:
    ids = {c.id for c in corpus}
    for required in (
        "idx_btree_tiny",  # CREATE INDEX on a tiny table
        "type_varchar_widen_plain_idx_5m",  # ALTER TYPE on a binary-coercible pair
        "drop_column_file_created",  # DROP COLUMN on a table the file created
        "addcol_const_default_5m",  # non-volatile default on PG 11+
        "type_varchar_widen_partial_idx_5m",  # the 13.5 s case
        "upd_matched_narrow_looking_5m",  # narrow WHERE on a huge table
        "conc_addcol_idle_holder_long",  # brief AEL behind an idle holder
        "setnn_plain_5m",  # SET NOT NULL with no proving CHECK
        "enum_add_value",  # irreversible-but-harmless -> safe_irreversible
        "addcol_bare_tiny",  # the AEL floor on a short hold
    ):
        assert required in ids, required


def test_expectations_align_with_statements(corpus: tuple[Case, ...]) -> None:
    for case in corpus:
        statements = [s for s in case.sql().split(";") if s.strip()]
        if len(case.expect) > 1:
            assert len(case.expect) == len(statements), case.id


def test_rendering_binds_table_and_escapes_braces(corpus: tuple[Case, ...]) -> None:
    for case in corpus:
        sql = case.sql()
        assert "{t}" not in sql, case.id
        if case.table is not None:
            assert case.table in FIXTURE_TABLES
            if "{t}" in case.migration:
                assert case.table in sql, case.id


# --- labeling ----------------------------------------------------------------


def _m(**kw: object) -> Measured:
    base: dict[str, object] = {
        "error": False,
        "strongest_preexisting_mode": None,
        "hold_ms": 5,
        "is_dml": False,
        "rows_touched": None,
        "irreversible": False,
    }
    base.update(kw)
    return Measured(**base)  # type: ignore[arg-type]


def test_error_is_unsafe() -> None:
    assert label(_m(error=True)).tier == "unsafe"


def test_no_lock_is_safe_and_irreversible_selects_tier() -> None:
    assert label(_m()).tier == "safe"
    assert label(_m(irreversible=True)).tier == "safe_irreversible"


def test_access_exclusive_floor_on_short_hold() -> None:
    lab = label(_m(strongest_preexisting_mode="ACCESS EXCLUSIVE", hold_ms=3))
    assert lab.tier == "needs_timing"
    assert lab.block == "reads"


def test_full_block_thresholds() -> None:
    ael = "ACCESS EXCLUSIVE"
    assert label(_m(strongest_preexisting_mode=ael, hold_ms=1_999)).tier == "needs_timing"
    assert label(_m(strongest_preexisting_mode=ael, hold_ms=19_999)).tier == "needs_timing"
    assert label(_m(strongest_preexisting_mode=ael, hold_ms=20_000)).tier == "unsafe"


def test_write_block_thresholds() -> None:
    assert label(_m(strongest_preexisting_mode="SHARE", hold_ms=4_999)).tier == "safe"
    assert label(_m(strongest_preexisting_mode="SHARE", hold_ms=5_000)).tier == "needs_timing"
    assert label(_m(strongest_preexisting_mode="SHARE", hold_ms=60_000)).tier == "unsafe"
    assert label(_m(strongest_preexisting_mode="SHARE ROW EXCLUSIVE", hold_ms=10)).tier == "safe"


def test_dml_bands_on_write_thresholds_even_without_table_block() -> None:
    rx = "ROW EXCLUSIVE"
    assert (
        label(_m(strongest_preexisting_mode=rx, is_dml=True, rows_touched=10, hold_ms=5,
                 irreversible=True)).tier
        == "safe_irreversible"
    )
    assert (
        label(_m(strongest_preexisting_mode=rx, is_dml=True, rows_touched=10**6, hold_ms=30_000,
                 irreversible=True)).tier
        == "needs_timing"
    )
    assert (
        label(_m(strongest_preexisting_mode=rx, is_dml=True, rows_touched=10**6, hold_ms=90_000,
                 irreversible=True)).tier
        == "unsafe"
    )
    # ROW EXCLUSIVE held for 90 s is write exposure whether or not a rowcount
    # was reported (a DO block full of UPDATEs reports none).
    do_block = _m(strongest_preexisting_mode=rx, is_dml=False, rows_touched=None, hold_ms=90_000)
    assert label(do_block).tier == "unsafe"
    # No row-level lock sampled and no rows: nothing to band.
    assert label(_m(is_dml=True, rows_touched=0, hold_ms=90_000)).tier == "safe"


def test_non_blocking_long_work_is_safe() -> None:
    sue = "SHARE UPDATE EXCLUSIVE"
    assert label(_m(strongest_preexisting_mode=sue, hold_ms=120_000)).tier == "safe"
    assert block_type(sue) == "none"
    assert block_type("EXCLUSIVE") == "writes"


def test_strongest_and_outcome() -> None:
    assert strongest(["ACCESS SHARE", "SHARE", "ROW EXCLUSIVE"]) == "SHARE"
    assert strongest([]) is None
    assert outcome("safe", "safe") == "match"
    assert outcome("safe", "needs_timing") == "lenient"
    assert outcome("unsafe", "needs_timing") == "strict"
    assert outcome("unknown", "safe") == "unknown"


def test_boundary_proximity_reported() -> None:
    lab = label(_m(strongest_preexisting_mode="SHARE", hold_ms=4_500))
    assert lab.boundary_proximity == pytest.approx(0.9)
    assert lab.thresholds_ms == (5_000, 60_000)


# --- calibration ---------------------------------------------------------------


def test_normalize_scales_work_not_wait() -> None:
    assert cal.normalize(work_ms=3_000, wait_ms=10_000, factor=1.5) == 12_000
    assert cal.normalize(work_ms=3_000, wait_ms=0, factor=0.5) == 6_000
    assert cal.normalize(work_ms=100, wait_ms=0, factor=0) == 100  # degenerate factor -> 1


def test_factor_for_uses_family_then_global() -> None:
    c = cal.Calibration(
        label="start",
        readings=(),
        global_factor=1.4,
        global_factor_secondary=None,
        per_family={"heap_rewrite": 1.8},
    )
    assert c.factor_for("heap_rewrite") == 1.8
    assert c.factor_for("dml_update") == 1.4
    assert c.factor_for("constant_op") == 1.0
    assert c.factor_for(None) == 1.4


def test_probe_reference_readings_exist() -> None:
    """Every probe must have a reference reading in the committed artifact."""
    refs = cal._reference_ms(cal.REFERENCE_PRIMARY)
    assert refs, "artifacts/scale/results_pre_ael_floor_34case.json missing"
    for probe in cal.PROBES:
        assert (probe.case, probe.table) in refs, probe.case
        assert refs[(probe.case, probe.table)] >= 200, probe.case


def test_factor_at_interpolates_between_passes() -> None:
    passes = [
        {"at_s": 0.0, "global_factor": 1.4},
        {"at_s": 100.0, "global_factor": 0.7},
        {"at_s": 200.0, "global_factor": 0.9},
    ]
    assert cal.factor_at(passes, -5) == 1.4
    assert cal.factor_at(passes, 50) == pytest.approx(1.05)
    assert cal.factor_at(passes, 150) == pytest.approx(0.8)
    assert cal.factor_at(passes, 999) == 0.9
    assert cal.factor_at([], 10) == 1.0
