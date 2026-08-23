"""The target's hardware as an input, and the boundary-proximity refusal.

Two things the 2026-08-23 session added, tested from the outside:

* the snapshot's calibration probe scales every proportional duration
  estimate to the target (a probe reading twice the anchor's doubles the
  estimate; an unavailable probe leaves it unscaled and says so), and
* when the strip the constant's known spread draws around the point
  estimate contains a tier threshold that would change the verdict, the
  engine refuses (UNKNOWN, ``refusal="boundary"``) instead of returning a
  coin-flip — but never where the ACCESS EXCLUSIVE floor makes the line
  irrelevant.
"""

from __future__ import annotations

from verdict_helpers import calibration, relation, snapshot

from blastoise.catalog.loader import load_catalog
from blastoise.live import calibrate as cb
from blastoise.live.model import CalibrationFacts, LiveSnapshot
from blastoise.parser import parse_migration
from blastoise.report import build_report
from blastoise.verdict import Classification, assess_script
from blastoise.verdict import constants as k
from blastoise.verdict.duration import estimate_from_rows
from blastoise.verdict.model import DurationEstimate, Method, StatementAssessment

CATALOG = load_catalog()
SERVER = snapshot().server


def rows_for(family: str, seconds: float) -> int:
    """The row count whose point estimate on the anchor is ``seconds``."""
    return int(k.DURATION_CONSTANTS[family].value * seconds)


def _one(sql: str, snap: LiveSnapshot) -> StatementAssessment:
    return assess_script(parse_migration(sql), CATALOG, 17, snap).statements[0]


# --- the probe -> factor --------------------------------------------------


def test_no_probe_means_unscaled_and_says_so() -> None:
    tenths, note = cb.hardware_factor_tenths(None)
    assert tenths == 10
    assert "unscaled" in note
    tenths, note = cb.hardware_factor_tenths(
        calibration(compute_reason="statement timeout"))
    assert tenths == 10
    assert "statement timeout" in note


def test_probe_ratio_is_the_factor() -> None:
    twice = calibration(compute_ms=2 * cb.REFERENCE_COMPUTE_MS)
    tenths, note = cb.hardware_factor_tenths(twice)
    assert tenths == 20
    assert "x2.0" in note
    half = calibration(compute_ms=max(cb.PROBE_FLOOR_MS, cb.REFERENCE_COMPUTE_MS // 2))
    tenths, _ = cb.hardware_factor_tenths(half)
    assert tenths == 5


def test_factor_is_clamped_to_the_measured_range() -> None:
    absurd = calibration(compute_ms=100 * cb.REFERENCE_COMPUTE_MS)
    tenths, note = cb.hardware_factor_tenths(absurd)
    assert tenths == cb.FACTOR_MAX_TENTHS
    assert "clamped" in note


# --- the factor -> estimate -----------------------------------------------


def test_estimate_scales_with_the_probe_and_records_it() -> None:
    rel = relation("t", rows=rows_for("heap_rewrite", 10))
    base = estimate_from_rows(rel, SERVER, "heap_rewrite")
    slow = estimate_from_rows(
        rel, SERVER, "heap_rewrite", calibration=calibration(compute_ms=2 * cb.REFERENCE_COMPUTE_MS)
    )
    assert isinstance(base, DurationEstimate) and isinstance(slow, DurationEstimate)
    assert base.point_ms == 10_010
    assert slow.point_ms == 20_010
    assert slow.high_ms > base.high_ms
    assert any(i.startswith("hardware: compute probe") for i in slow.inputs)
    assert any(i.startswith("hardware: unscaled") for i in base.inputs)


def test_snapshot_without_probe_constructs_and_is_unscaled() -> None:
    snap = snapshot(relations=(relation("t", rows=1_000_000),))
    assert isinstance(snap.calibration, CalibrationFacts)
    assert not snap.calibration.compute_ms.available
    assert "calibration" in snap.to_canonical_json()


# --- the boundary rule ----------------------------------------------------


def _rewrite_at(rows: int, facts: CalibrationFacts | None = None) -> StatementAssessment:
    snap = snapshot(relations=(relation("t", rows=rows),), calibration_facts=facts)
    # SET UNLOGGED: a plain heap rewrite on heap_rewrite with no live
    # narrowing (ALTER COLUMN TYPE would need type-change facts).
    return _one("ALTER TABLE t SET UNLOGGED;", snap)


def test_straddling_the_outage_line_is_refused_not_decided() -> None:
    # An 18 s point on heap_rewrite. The 1.5x strip [12 s, 27 s] contains
    # the 20 s line; the upper bound would have said UNSAFE. Refuse, and
    # say from what.
    st = _rewrite_at(rows_for("heap_rewrite", 18))
    v = st.verdict
    assert v.classification is Classification.UNKNOWN
    assert v.refusal == "boundary"
    assert v.refused_from is Classification.UNSAFE
    assert v.refused_alternatives == (Classification.NEEDS_TIMING, Classification.UNSAFE)
    assert v.method is Method.SIMULATED
    assert "refused at the boundary" in v.rationale
    assert "20 s" in v.rationale
    assert any("production-sized copy" in c for c in v.conditions)


def test_clear_of_the_line_is_decided() -> None:
    # 50 s point, strip [33 s, 75 s]: no line inside -> UNSAFE.
    big = _rewrite_at(rows_for("heap_rewrite", 50)).verdict
    assert big.classification is Classification.UNSAFE
    # 5 s point, strip [3.3 s, 7.5 s]: no line -> NEEDS_TIMING.
    small = _rewrite_at(rows_for("heap_rewrite", 5)).verdict
    assert small.classification is Classification.NEEDS_TIMING


def test_the_probe_moves_a_case_across_the_strip() -> None:
    # A table that is 10 s on the anchor: clear of the line (NEEDS_TIMING).
    # On a target whose probe reads 1.8x slower the same table is 18 s:
    # inside the strip, refused. The verdict is a property of the target.
    rows = rows_for("heap_rewrite", 10)
    assert _rewrite_at(rows).verdict.classification is Classification.NEEDS_TIMING
    slow = _rewrite_at(rows, calibration(compute_ms=cb.REFERENCE_COMPUTE_MS * 18 // 10))
    assert slow.verdict.refusal == "boundary"
    assert slow.verdict.refused_from is Classification.UNSAFE


def test_the_short_line_under_the_ael_floor_is_not_a_refusal() -> None:
    # A 2 s point: strip [1.3 s, 3 s] contains the 2 s line — but ACCESS
    # EXCLUSIVE on a live table is NEEDS_TIMING on either side of it (the
    # floor), so there is nothing to refuse.
    v = _rewrite_at(rows_for("heap_rewrite", 2)).verdict
    assert v.classification is Classification.NEEDS_TIMING
    assert v.refusal is None


def test_the_short_line_on_a_write_block_is_refused() -> None:
    # CREATE INDEX (SHARE: blocks writes) with a 5 s point: the strip
    # [3.3 s, 7.5 s] contains the 5 s write-stall line, and SAFE vs
    # NEEDS_TIMING is a real difference for the reviewer.
    snap = snapshot(relations=(relation("t", rows=rows_for("index_build_btree", 5)),))
    v = _one("CREATE INDEX t_x ON t (x);", snap).verdict
    assert v.classification is Classification.UNKNOWN
    assert v.refusal == "boundary"
    assert v.refused_alternatives == (Classification.SAFE, Classification.NEEDS_TIMING)


def test_full_table_dml_is_refused_at_the_write_outage_line() -> None:
    # A 55 s full-table UPDATE: strip [36 s, 82 s] contains the 60 s line.
    snap = snapshot(relations=(relation("t", rows=rows_for("dml_update", 55)),))
    v = _one("UPDATE t SET x = 1;", snap).verdict
    assert v.classification is Classification.UNKNOWN
    assert v.refusal == "boundary"
    assert v.refused_from is Classification.UNSAFE


def test_uncalibrated_constants_use_their_guess_band_as_the_strip() -> None:
    c = k.DURATION_CONSTANTS["constant_op"]
    assert k.boundary_spread_tenths(c) == k.WIDEN_UNCALIBRATED_TENTHS
    # One profile, however many runs, sits at the floor.
    assert k.boundary_spread_tenths(k.DURATION_CONSTANTS["heap_rewrite"]) == (
        k.BOUNDARY_SPREAD_FLOOR_TENTHS
    )


def test_refusal_reaches_the_report_payload() -> None:
    sql = "ALTER TABLE t SET UNLOGGED;"
    snap = snapshot(relations=(relation("t", rows=rows_for("heap_rewrite", 18)),))
    script = parse_migration(sql)
    assessment = assess_script(script, CATALOG, 17, snap)
    payload, _bundle = build_report(
        script,
        assessment,
        catalog=CATALOG,
        snapshot=snap,
        evaluated_at="2026-08-23T00:00:00+00:00")
    st = payload["statements"][0]
    assert st["classification"] == "unknown"
    assert st["refusal"] == "boundary"
    assert st["refused_from"] == "unsafe"
    assert payload["verdict"] == "requires_approval"
