"""Plain-text rendering of the validation summary. ASCII only."""

from __future__ import annotations

from typing import Any

from blastoise.verdict import constants as _k


def _pct(x: float | None) -> str:
    return "  n/a" if x is None else f"{100 * x:5.1f}%"


def render(summary: dict[str, Any], results: dict[str, Any]) -> str:
    lines: list[str] = []
    add = lines.append
    add("BLASTOISE VALIDATION HARNESS")
    add("=" * 72)
    add(f"cases: {summary['cases']}   statements scored: {summary['statements_scored']}   "
        f"elapsed: {results.get('elapsed_s', '?')}s   smoke: {results.get('smoke')}")
    add(f"sizes seeded: {results.get('sizes')}")
    add(
        "tier thresholds (ms): full block "
        f"{_k.FULL_BLOCK_SHORT_MS}/{_k.FULL_BLOCK_LONG_MS}, write block "
        f"{_k.WRITE_BLOCK_SHORT_MS}/{_k.WRITE_BLOCK_LONG_MS}"
    )
    if summary["harness_errors"]:
        add("")
        add(f"HARNESS ERRORS ({len(summary['harness_errors'])}) - these cases are not scored:")
        for e in summary["harness_errors"]:
            add(f"  {e}")
    if summary["not_executed"]:
        add(f"not executed (aborted transaction): {', '.join(summary['not_executed'])}")

    add("")
    add("HARDWARE NORMALIZATION")
    add("-" * 72)
    calib = summary.get("calibration") or {}
    if not calib:
        add("no calibration probe ran: measurements are raw (factor 1.0)")
    for phase in ("start", "end"):
        c = calib.get(phase)
        if not c:
            continue
        add(
            f"probe[{phase}]: machine factor {c['global_factor']:.2f}x vs reference run "
            f"(pre-AEL-floor 34-case), {c['global_factor_vs_secondary_reference']}x vs the "
            f"1.46x-slower run of the same machine"
        )
        for r in c["readings"]:
            f = "n/a" if r["factor"] is None else f"{r['factor']:.2f}x"
            add(
                f"    {r['case']:28s} {r['table']:7s} today {r['today_ms']:>7} ms  "
                f"ref {r['reference_ms']!s:>7}  {f:>6}  [{r['family']}]"
            )
    passes = calib.get("passes") or []
    if passes:
        add("factor over the run (light probes every 20 cases; each case uses the factor "
            "interpolated at the time it ran):")
        from validation.harness.calibration import comparable_factor

        for p in passes:
            per = " ".join(f"{k[:10]}={v:.2f}" for k, v in sorted(p["per_family"].items()))
            add(
                f"    t={p['at_s']:>7.0f}s {p['label']:6s} comparable {comparable_factor(p):.2f}x "
                f"(full-set {p['global_factor']:.2f}x)   {per}"
            )
    if calib.get("start") and calib.get("end"):
        s, e = calib["start"]["global_factor"], calib["end"]["global_factor"]
        add(f"drift during the run: start {s:.2f}x -> end {e:.2f}x (ratio {e / s:.2f})")
    add(f"factor used for ground-truth labels: median {summary['factor_used']}, range "
        f"{results.get('factor_range')} (measured work / factor + raw lock wait = "
        "reference-machine ms)")

    hp = summary.get("hardware_probe") or {}
    tb = hp.get("truth_basis") or {}
    add("")
    add("HARDWARE PROBE (snapshot calibration, format 5) AND TRUTH BASIS")
    add("-" * 72)
    if hp.get("compute_ms", {}).get("n"):
        c = hp["compute_ms"]
        add(f"compute probe over {c['n']} snapshots: min {c['min']} ms, median {c['median']} ms, "
            f"max {c['max']} ms")
    else:
        add("no snapshot carried a calibration probe")
    if tb:
        add(f"truth basis: {tb['rule']} ({tb['probe_scaled_cases']}/{tb['cases']} cases "
            "probe-scaled -> labeled on the raw hold)")

    add("")
    add("PER-TIER PRECISION AND RECALL (statement level, truth = measured hold)")
    add("-" * 72)
    add(f"{'tier':18s} {'pred':>5} {'truth':>5} {'tp':>4} {'fp':>4} {'fn':>4} "
        f"{'precision':>10} {'recall':>8}  (fn as unknown)")
    pt = summary["per_tier"]
    for tier in ("unsafe", "needs_timing", "safe_irreversible", "safe"):
        t = pt[tier]
        add(
            f"{tier:18s} {t['predicted']:>5} {t['truth']:>5} {t['tp']:>4} {t['fp']:>4} "
            f"{t['fn']:>4} {_pct(t['precision']):>10} {_pct(t['recall']):>8}  "
            f"({t['fn_as_unknown']})"
        )
    u = pt["unknown"]
    add(f"{'unknown':18s} {u['predicted']:>5}      truth was: {u['truth_breakdown']}  "
        f"(boundary refusals: {u.get('boundary_refusals', 0)})")
    add("")
    add(f"outcome directions: {summary['directions']}  "
        "(lenient = engine said safer than measured: the dangerous kind)")

    add("")
    add("CONFUSION (rows = predicted, columns = truth)")
    add("-" * 72)
    cols = ("safe", "safe_irreversible", "needs_timing", "unsafe")
    add(f"{'':18s}" + "".join(f"{c[:12]:>14}" for c in cols))
    for pred, row in summary["confusion"].items():
        add(f"{pred:18s}" + "".join(f"{row[c]:>14}" for c in cols))

    add("")
    add("UNSAFE IN DETAIL")
    add("-" * 72)
    add("false BLOCKs (predicted unsafe, truth was):")
    for c in pt["unsafe"]["fp_cases"] or ["  (none)"]:
        add(f"  {c}")
    add("missed UNSAFE (truth unsafe, predicted):")
    for c in pt["unsafe"]["fn_cases"] or ["  (none)"]:
        add(f"  {c}")

    fl = summary["file_level"]
    add("")
    add("FILE LEVEL (proceed / requires_approval / block)")
    add("-" * 72)
    for v in ("block", "requires_approval", "proceed"):
        t = fl[v]
        add(f"{v:18s} pred {t['predicted']:>4} truth {t['truth']:>4} tp {t['tp']:>4} "
            f"precision {_pct(t['precision'])} recall {_pct(t['recall'])}")
    add(f"directions: {fl['directions']}")
    if fl["block_errors"]:
        add("BLOCK disagreements:")
        for e in fl["block_errors"]:
            add(f"  {e}")

    for title, key in (
        ("ERRORS BY STATEMENT KIND", "by_statement_kind"),
        ("ERRORS BY DECIDING CATALOG ROW", "by_deciding_row"),
        ("ERRORS BY DURATION CONSTANT", "by_constant"),
        ("ERRORS BY CORPUS FAMILY", "by_family"),
        ("ERRORS BY ADVERSARIAL CLASS", "by_adversarial"),
    ):
        add("")
        add(title)
        add("-" * 72)
        add(f"{'key':36s} {'n':>4} {'match':>6} {'strict':>7} {'lenient':>8} {'unk':>4}")
        for g in summary[key]:
            add(
                f"{g['key'][:36]:36s} {g['total']:>4} {g['match']:>6} {g['strict']:>7} "
                f"{g['lenient']:>8} {g['unknown']:>4}"
            )
            for c in g["cases"]:
                add(f"    {c}")

    add("")
    add("LABEL MISMATCHES (author's expectation vs measurement; truth = measurement)")
    add("-" * 72)
    for m in summary["label_mismatches"] or ["(none)"]:
        add(f"  {m}")

    add("")
    add("BOUNDARY CASES (truth hold within 25% of a tier threshold: hardware decides)")
    add("-" * 72)
    for b in summary["boundary_cases"] or [{"case": "(none)"}]:
        if b["case"] == "(none)":
            add("  (none)")
            continue
        add(
            f"  {b['case']:40s} hold {b['hold_ms']:>8} ms  thresholds {b['thresholds_ms']}  "
            f"x{b['proximity']:.2f}  truth {b['truth']:17s} pred {b['predicted']:17s} "
            f"{b['outcome']}"
        )

    br = summary.get("boundary_refusals") or {"count": 0, "cases": []}
    add("")
    add("BOUNDARY REFUSALS (UNKNOWN because the estimate straddles a threshold by less "
        "than the constant's known spread)")
    add("-" * 72)
    add(f"refused: {br['count']}   refused from: {br.get('refused_from')}   truth was: "
        f"{br.get('truth_breakdown')}")
    add(f"what the refusal replaced (scoring refused_from against truth): "
        f"{br.get('would_have_been')}")
    for d in br["cases"]:
        add(
            f"  {d['case']:40s} from {d['refused_from']!s:13s} truth {d['truth']:13s} "
            f"would-be {d['would_have_been']!s:8s} hold {d['hold_ms']:>8} ms  "
            f"high {d['deciding_high_ms']!s:>8} ms  thresholds {d['thresholds_ms']}  "
            f"[{d['constant']}]"
        )

    n = summary["normalization"]
    add("")
    add("NORMALIZATION SENSITIVITY")
    add("-" * 72)
    add(f"outcomes with the labels used:   {n.get('outcomes_used', n['outcomes_normalized'])}")
    add(f"outcomes with normalized labels: {n['outcomes_normalized']}")
    add(f"outcomes with raw labels:        {n['outcomes_raw']}")
    add(f"outcomes with per-family labels: {n['outcomes_per_family']}")
    add(f"UNSAFE precision: used {_pct(pt['unsafe']['precision'])}  normalized "
        f"{_pct(n.get('unsafe_precision_normalized'))}  raw "
        f"{_pct(n['unsafe_precision_raw'])}  per-family {_pct(n['unsafe_precision_per_family'])}")
    add(f"labels flipped by normalization ({len(n['labels_flipped_by_normalization'])}):")
    for f in n["labels_flipped_by_normalization"]:
        add(f"  {f}")
    add(f"labels flipped by per-family factor ({len(n['labels_flipped_by_per_family_factor'])}):")
    for f in n["labels_flipped_by_per_family_factor"]:
        add(f"  {f}")
    return "\n".join(lines) + "\n"
