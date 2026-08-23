"""Precision and recall per tier, the confusion matrix, and the error
breakdowns by statement classification and by duration constant.

Per tier T: precision = |predicted T and truth T| / |predicted T|, recall =
|predicted T and truth T| / |truth T|. UNKNOWN predictions are never
counted as a hit or a miss for any other tier — they are a refusal, and
they are reported as their own row ("unknown where the truth was …") —
but a truth that the engine called UNKNOWN *does* count against that
tier's recall, because the engine failed to claim it.

Errors carry a direction: *lenient* when the engine sat lower on the
action ladder than the truth (the dangerous kind), *strict* when higher
(the false-alarm kind). Every error is attributed to the statement kind,
the deciding catalog row's kind (for ALTER TABLE, the action), and the
duration constant the deciding row leaned on, so the reader can see which
rows or constants are responsible.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from validation.harness.corpus import TIERS

SCORED_TIERS: tuple[str, ...] = ("safe", "safe_irreversible", "needs_timing", "unsafe")


def executed_statements(results: dict[str, Any]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    out: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for record in results["results"]:
        for srec in record.get("statements", []):
            if srec.get("truth"):
                out.append((record, srec))
    return out


def _ratio(num: int, den: int) -> float | None:
    return None if den == 0 else num / den


def per_tier(results: dict[str, Any], truth_key: str = "tier") -> dict[str, dict[str, Any]]:
    rows = executed_statements(results)
    out: dict[str, dict[str, Any]] = {}
    for tier in SCORED_TIERS:
        predicted = [(r, s) for r, s in rows if s["predicted"]["tier"] == tier]
        truth = [(r, s) for r, s in rows if s["truth"][truth_key] == tier]
        tp = [(r, s) for r, s in predicted if s["truth"][truth_key] == tier]
        fp = [(r, s) for r, s in predicted if s["truth"][truth_key] != tier]
        fn = [(r, s) for r, s in truth if s["predicted"]["tier"] != tier]
        fn_unknown = [(r, s) for r, s in fn if s["predicted"]["tier"] == "unknown"]
        out[tier] = {
            "predicted": len(predicted),
            "truth": len(truth),
            "tp": len(tp),
            "fp": len(fp),
            "fn": len(fn),
            "fn_as_unknown": len(fn_unknown),
            "precision": _ratio(len(tp), len(predicted)),
            "recall": _ratio(len(tp), len(truth)),
            "fp_cases": [f"{r['case']}#{s['index']}->{s['truth'][truth_key]}" for r, s in fp],
            "fn_cases": [f"{r['case']}#{s['index']}<-{s['predicted']['tier']}" for r, s in fn],
        }
    unknown = [(r, s) for r, s in rows if s["predicted"]["tier"] == "unknown"]
    out["unknown"] = {
        "predicted": len(unknown),
        "boundary_refusals": sum(
            1 for _, s in unknown if s["predicted"].get("refusal") == "boundary"
        ),
        "truth_breakdown": dict(Counter(s["truth"][truth_key] for _, s in unknown)),
        "cases": [f"{r['case']}#{s['index']}->{s['truth'][truth_key]}" for r, s in unknown],
    }
    return out


def confusion(results: dict[str, Any]) -> dict[str, dict[str, int]]:
    rows = executed_statements(results)
    matrix: dict[str, dict[str, int]] = {t: dict.fromkeys(SCORED_TIERS, 0) for t in TIERS}
    for _, s in rows:
        matrix[s["predicted"]["tier"]][s["truth"]["tier"]] += 1
    return matrix


def directions(results: dict[str, Any]) -> dict[str, int]:
    rows = executed_statements(results)
    return dict(Counter(s["outcome"] for _, s in rows))


def breakdown(results: dict[str, Any], key_fn: Any, title: str) -> list[dict[str, Any]]:
    """Errors grouped by a key: count, lenient/strict/unknown split, the cases."""
    rows = executed_statements(results)
    groups: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"total": 0, "match": 0, "strict": 0, "lenient": 0, "unknown": 0, "cases": []}
    )
    for r, s in rows:
        key = key_fn(r, s)
        if key is None:
            continue
        g = groups[str(key)]
        g["total"] += 1
        g[s["outcome"]] += 1
        if s["outcome"] != "match":
            g["cases"].append(
                f"{r['case']}#{s['index']} {s['predicted']['tier']}->{s['truth']['tier']} "
                f"({s['outcome']})"
            )
    out = [{"key": k, **v} for k, v in groups.items()]
    out.sort(key=lambda g: (-(g["lenient"] + g["strict"] + g["unknown"]), g["key"]))
    return out


def by_statement_kind(results: dict[str, Any]) -> list[dict[str, Any]]:
    return breakdown(results, lambda r, s: s["kind"], "statement kind")


def by_deciding_row(results: dict[str, Any]) -> list[dict[str, Any]]:
    return breakdown(results, lambda r, s: s["predicted"]["deciding_kind"], "deciding catalog row")


def by_constant(results: dict[str, Any]) -> list[dict[str, Any]]:
    return breakdown(
        results,
        lambda r, s: s["predicted"]["deciding_constant_key"] or "(no duration constant)",
        "duration constant",
    )


def by_family(results: dict[str, Any]) -> list[dict[str, Any]]:
    return breakdown(results, lambda r, s: r["family"], "corpus family")


def by_adversarial(results: dict[str, Any]) -> list[dict[str, Any]]:
    return breakdown(results, lambda r, s: r.get("adversarial") or "(plain)", "adversarial class")


def file_level(results: dict[str, Any]) -> dict[str, Any]:
    files = [r["file"] for r in results["results"] if r.get("file")]
    out: dict[str, Any] = {"files": len(files)}
    for verdict in ("proceed", "requires_approval", "block"):
        predicted = [f for f in files if f["predicted"] == verdict]
        truth = [f for f in files if f["truth"] == verdict]
        tp = [f for f in predicted if f["truth"] == verdict]
        out[verdict] = {
            "predicted": len(predicted),
            "truth": len(truth),
            "tp": len(tp),
            "precision": _ratio(len(tp), len(predicted)),
            "recall": _ratio(len(tp), len(truth)),
        }
    out["directions"] = dict(Counter(f["direction"] for f in files))
    out["block_errors"] = [
        f"{r['case']}: predicted {r['file']['predicted']}, truth {r['file']['truth']}"
        for r in results["results"]
        if r.get("file") and not r["file"]["match"]
        and "block" in (r["file"]["predicted"], r["file"]["truth"])
    ]
    return out


def label_mismatches(results: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for r, s in executed_statements(results):
        for m in s.get("label_mismatches", []):
            out.append(f"{r['case']}#{s['index']}: {m}")
    return out


def boundary_cases(results: dict[str, Any], window: float = 0.25) -> list[dict[str, Any]]:
    """Truth labels within ``window`` of a threshold — where hardware decides."""
    out: list[dict[str, Any]] = []
    for r, s in executed_statements(results):
        prox = s["truth"].get("boundary_proximity")
        if prox is None:
            continue
        if abs(prox - 1.0) <= window:
            out.append(
                {
                    "case": f"{r['case']}#{s['index']}",
                    "hold_ms": s["truth"]["hold_ms_normalized"],
                    "thresholds_ms": s["truth"]["thresholds_ms"],
                    "proximity": prox,
                    "truth": s["truth"]["tier"],
                    "predicted": s["predicted"]["tier"],
                    "outcome": s["outcome"],
                }
            )
    out.sort(key=lambda x: abs(x["proximity"] - 1.0))
    return out


def boundary_refusals(results: dict[str, Any]) -> dict[str, Any]:
    """The UNKNOWNs the boundary-proximity rule issued, and what each replaced.

    ``refused_from`` is the tier the upper-bound rule would have returned;
    ``would_have_been`` scores that tier against the truth, so the reader
    sees how many refusals absorbed a false BLOCK (``strict``), how many
    gave up a correct call (``match``), and how many hid a lenient miss
    (``lenient``). The cost the brief accepts is the ``match`` row.
    """
    rows = executed_statements(results)
    refused = [
        (r, s) for r, s in rows
        if s["predicted"]["tier"] == "unknown" and s["predicted"].get("refusal") == "boundary"
    ]
    from validation.harness.labeling import outcome as _outcome

    detail = []
    for r, s in refused:
        frm = s["predicted"].get("refused_from")
        detail.append(
            {
                "case": f"{r['case']}#{s['index']}",
                "refused_from": frm,
                "truth": s["truth"]["tier"],
                "would_have_been": _outcome(frm, s["truth"]["tier"]) if frm else None,
                "hold_ms": s["truth"]["hold_ms"],
                "thresholds_ms": s["truth"]["thresholds_ms"],
                "deciding_high_ms": s["predicted"].get("deciding_high_ms"),
                "constant": s["predicted"].get("deciding_constant_key"),
            }
        )
    return {
        "count": len(refused),
        "refused_from": dict(Counter(d["refused_from"] for d in detail)),
        "truth_breakdown": dict(Counter(d["truth"] for d in detail)),
        "would_have_been": dict(Counter(d["would_have_been"] for d in detail)),
        "cases": detail,
    }


def hardware_probe(results: dict[str, Any]) -> dict[str, Any]:
    """The calibration probe readings the snapshots carried, over the run."""
    compute = [
        int(r["snapshot_calibration"]["compute_ms"])
        for r in results["results"]
        if (r.get("snapshot_calibration") or {}).get("compute_ms") is not None
    ]

    def stats(xs: list[int]) -> dict[str, Any]:
        if not xs:
            return {"n": 0}
        xs = sorted(xs)
        return {
            "n": len(xs), "min": xs[0], "median": xs[len(xs) // 2], "max": xs[-1],
        }

    return {"compute_ms": stats(compute), "truth_basis": results.get("truth_basis")}


def normalization_sensitivity(results: dict[str, Any]) -> dict[str, Any]:
    rows = executed_statements(results)
    flips_raw = [
        f"{r['case']}#{s['index']}: raw {s['truth']['tier_raw']} vs normalized "
        f"{s['truth'].get('tier_normalized', s['truth']['tier'])}"
        for r, s in rows
        if s["truth"]["tier_raw"] != s["truth"].get("tier_normalized", s["truth"]["tier"])
    ]
    flips_family = [
        f"{r['case']}#{s['index']}: per-family {s['truth']['tier_per_family']} vs "
        f"normalized {s['truth']['tier']}"
        for r, s in rows
        if s["truth"].get("tier_per_family") and s["truth"]["tier_per_family"] != s["truth"]["tier"]
    ]
    # Outcome counts under each labeling variant.
    def outcomes(key: str) -> dict[str, int]:
        from validation.harness.labeling import outcome as _outcome

        return dict(
            Counter(
                _outcome(s["predicted"]["tier"], s["truth"][key])
                for _, s in rows
                if s["truth"].get(key)
            )
        )

    return {
        "statements": len(rows),
        "labels_flipped_by_normalization": flips_raw,
        "labels_flipped_by_per_family_factor": flips_family,
        "outcomes_used": outcomes("tier"),
        "outcomes_normalized": outcomes("tier_normalized")
        if any(s["truth"].get("tier_normalized") for _, s in rows) else outcomes("tier"),
        "outcomes_raw": outcomes("tier_raw"),
        "outcomes_per_family": outcomes("tier_per_family"),
        "unsafe_precision_raw": per_tier(results, "tier_raw")["unsafe"]["precision"],
        "unsafe_precision_normalized": (
            per_tier(results, "tier_normalized")["unsafe"]["precision"]
            if any(s["truth"].get("tier_normalized") for _, s in rows)
            else per_tier(results)["unsafe"]["precision"]
        ),
        "unsafe_precision_per_family": (
            per_tier(results, "tier_per_family")["unsafe"]["precision"]
            if any(s["truth"].get("tier_per_family") for _, s in rows) else None
        ),
    }


def summarize(results: dict[str, Any]) -> dict[str, Any]:
    rows = executed_statements(results)
    harness_errors = [
        f"{r['case']}: {r['harness_error']}" for r in results["results"] if r.get("harness_error")
    ]
    not_executed = [
        f"{r['case']}#{s['index']}"
        for r in results["results"]
        for s in r.get("statements", [])
        if s.get("outcome") == "not_executed"
    ]
    return {
        "cases": len(results["results"]),
        "statements_scored": len(rows),
        "harness_errors": harness_errors,
        "not_executed": not_executed,
        "factor_used": results.get("factor_used"),
        "calibration": results.get("calibration"),
        "directions": directions(results),
        "per_tier": per_tier(results),
        "confusion": confusion(results),
        "file_level": file_level(results),
        "by_statement_kind": by_statement_kind(results),
        "by_deciding_row": by_deciding_row(results),
        "by_constant": by_constant(results),
        "by_family": by_family(results),
        "by_adversarial": by_adversarial(results),
        "label_mismatches": label_mismatches(results),
        "boundary_cases": boundary_cases(results),
        "boundary_refusals": boundary_refusals(results),
        "hardware_probe": hardware_probe(results),
        "normalization": normalization_sensitivity(results),
    }
