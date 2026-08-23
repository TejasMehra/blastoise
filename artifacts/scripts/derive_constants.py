"""Derive the duration constants' values, anchors, and bands from profile runs.

Input: one or more results files from ``measure_profiles.py``, each a
different hardware profile. Output: for every constant family, the
at-scale rate observed on each profile, the calibration probe on each
profile, the rate *as the probe predicts it on the anchor profile*, and
the spread of those probe-scaled rates across profiles — the residual the
probe does not explain, which is the band the constant must carry and
the half-width of the strip inside which the engine refuses to decide.

Usage:
    python artifacts/scripts/derive_constants.py --anchor laptop-nvme \
        scratch/profiles/*.json [--out artifacts/profiles/derived.json]

The "slowest at scale" convention of the earlier sessions is kept per
profile: a family's rate on a profile is the slowest reading among its
representative shapes at 1M rows or more (100k or more for the DML
families, which the scale harness measured at 100k/1M/10M), across both
passes. Within-profile spread (max/min over passes and shapes at scale) is
reported beside the cross-profile spread so the reader can see which one
dominates.

``index_bytes`` is bytes/s: ``REINDEX INDEX`` on the account_id index,
bytes from ``pg_relation_size`` recorded by the measurement. ``constant_op``
is a fixed cost and is reported as milliseconds, not a rate.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))

# The live probe is compute-only (the privilege contract forbids reading
# user tables); the scan probe the measurement records is informational —
# it shows how much of the cross-profile spread a disk probe *would* have
# explained, had one been allowed.
SCAN_WEIGHTED_FAMILIES: frozenset[str] = frozenset()

AT_SCALE_ROWS = {
    "dml_update": 100_000,
    "dml_delete": 100_000,
}
DEFAULT_AT_SCALE = 1_000_000


def _rate(rec: dict[str, Any], index_bytes: dict[str, int]) -> int | None:
    if rec["ms"] is None or rec["error"]:
        return None
    if rec["family"] == "index_bytes":
        size = index_bytes.get(rec["table"], 0)
        return size * 1000 // rec["ms"] if rec["ms"] else None
    if rec["family"] == "constant_op":
        return rec["ms"]  # milliseconds, not a rate
    return int(rec["rate_raw"]) if rec["rate_raw"] else None


def load(path: Path) -> dict[str, Any]:
    doc = json.loads(path.read_text(encoding="utf8"))
    probes = doc.get("probes") or []
    compute = [p["compute_ms"] for p in probes if p.get("compute_ms") is not None]
    scan = [p["scan_ms"] for p in probes if p.get("scan_ms") is not None]
    families: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for rec in doc["records"]:
        threshold = AT_SCALE_ROWS.get(rec["family"], DEFAULT_AT_SCALE)
        if rec["rows"] < threshold:
            continue
        rate = _rate(rec, doc.get("index_bytes") or {})
        if rate is None:
            continue
        families[rec["family"]].append(
            {"case": rec["case"], "table": rec["table"], "pass": rec["pass_"], "rate": rate,
             "ms": rec["ms"]}
        )
    return {
        "profile": doc["profile"],
        "machine": doc.get("machine"),
        "compute_ms": statistics.median(compute) if compute else None,
        "compute_ms_all": compute,
        "scan_ms": statistics.median(scan) if scan else None,
        "scan_ms_all": scan,
        "families": families,
    }


def _spread_tenths(values: list[float]) -> int:
    vals = [v for v in values if v > 0]
    if len(vals) < 2:
        return 0
    return math.ceil(10 * max(vals) / min(vals))


def derive(profiles: list[dict[str, Any]], anchor_name: str) -> dict[str, Any]:
    by_name = {p["profile"]: p for p in profiles}
    if anchor_name not in by_name:
        raise SystemExit(f"anchor profile {anchor_name!r} not among {sorted(by_name)}")
    anchor = by_name[anchor_name]
    out: dict[str, Any] = {
        "anchor": anchor_name,
        "anchor_compute_ms": anchor["compute_ms"],
        "anchor_scan_ms": anchor["scan_ms"],
        "profiles": {
            p["profile"]: {
                "compute_ms": p["compute_ms"], "scan_ms": p["scan_ms"],
                "compute_ms_all": p["compute_ms_all"], "scan_ms_all": p["scan_ms_all"],
                "machine": p["machine"],
            }
            for p in profiles
        },
        "constants": {},
    }
    all_families = sorted({f for p in profiles for f in p["families"]})
    for family in all_families:
        rows: dict[str, Any] = {}
        scaled_compute: list[float] = []
        scaled_scan: list[float] = []
        raw_rates: list[float] = []
        for p in profiles:
            readings = p["families"].get(family) or []
            if not readings:
                continue
            rates = [r["rate"] for r in readings]
            if family == "constant_op":
                # A fixed cost: the representative value is the median ms.
                value = int(statistics.median(rates))
                slowest = max(rates)
            else:
                value = min(rates)  # slowest at scale
                slowest = value
            within = _spread_tenths([float(r) for r in rates])
            f_compute = (
                p["compute_ms"] / anchor["compute_ms"]
                if p["compute_ms"] and anchor["compute_ms"] else None
            )
            f_scan = (
                p["scan_ms"] / anchor["scan_ms"] if p["scan_ms"] and anchor["scan_ms"] else None
            )
            # A rate on a profile f times slower than the anchor corresponds
            # to rate * f on the anchor (ms for constant_op: ms / f).
            if family == "constant_op":
                sc = value / f_compute if f_compute else None
                ss = value / f_scan if f_scan else None
            else:
                sc = value * f_compute if f_compute else None
                ss = value * f_scan if f_scan else None
            rows[p["profile"]] = {
                "value": value,
                "slowest_at_scale": slowest,
                "readings": readings,
                "within_profile_spread_tenths": within,
                "factor_compute": None if f_compute is None else round(f_compute, 3),
                "factor_scan": None if f_scan is None else round(f_scan, 3),
                "scaled_to_anchor_by_compute": None if sc is None else int(sc),
                "scaled_to_anchor_by_scan": None if ss is None else int(ss),
            }
            raw_rates.append(float(value))
            if sc is not None:
                scaled_compute.append(sc)
            if ss is not None:
                scaled_scan.append(ss)
        n_profiles = len(rows)
        cross_raw = _spread_tenths(raw_rates)
        cross_compute = _spread_tenths(scaled_compute)
        cross_scan = _spread_tenths(scaled_scan)
        probe = "scan" if family in SCAN_WEIGHTED_FAMILIES else "compute"
        chosen = cross_scan if probe == "scan" and scaled_scan else cross_compute
        # The recommended value: the anchor profile's own slowest-at-scale
        # reading (the constants are expressed at the anchor's probe).
        anchor_row = rows.get(anchor_name)
        out["constants"][family] = {
            "probe": probe,
            "profiles": n_profiles,
            "per_profile": rows,
            "anchor_value": None if anchor_row is None else anchor_row["value"],
            "cross_profile_spread_raw_tenths": cross_raw,
            "cross_profile_spread_scaled_by_compute_tenths": cross_compute,
            "cross_profile_spread_scaled_by_scan_tenths": cross_scan,
            "cross_profile_spread_tenths": chosen,
            "within_profile_spread_tenths_max": max(
                (r["within_profile_spread_tenths"] for r in rows.values()), default=0
            ),
        }
    return out


def render(d: dict[str, Any]) -> str:
    lines = [f"anchor: {d['anchor']}  compute {d['anchor_compute_ms']} ms  "
             f"scan {d['anchor_scan_ms']} ms", ""]
    lines.append("profiles:")
    for name, p in d["profiles"].items():
        m = p["machine"] or {}
        lines.append(
            f"  {name:18s} compute {p['compute_ms']!s:>6} ms  scan {p['scan_ms']!s:>6} ms  "
            f"cpu={m.get('cpu_count')} {str(m.get('cpu_model'))[:40]}  disk={m.get('disk_label')}"
        )
    lines.append("")
    lines.append(f"{'family':24s} {'probe':7s} {'n':>2} {'anchor':>10} {'raw x':>6} "
                 f"{'cmp x':>6} {'scan x':>6} {'within':>6}   per-profile (value->scaled)")
    for fam, c in d["constants"].items():
        key = "scaled_to_anchor_by_scan" if c["probe"] == "scan" else "scaled_to_anchor_by_compute"
        per = "  ".join(f"{n}={r['value']}->{r[key]}" for n, r in c["per_profile"].items())
        lines.append(
            f"{fam:24s} {c['probe']:7s} {c['profiles']:>2} {c['anchor_value']!s:>10} "
            f"{c['cross_profile_spread_raw_tenths'] / 10:>6.1f} "
            f"{c['cross_profile_spread_scaled_by_compute_tenths'] / 10:>6.1f} "
            f"{c['cross_profile_spread_scaled_by_scan_tenths'] / 10:>6.1f} "
            f"{c['within_profile_spread_tenths_max'] / 10:>6.1f}   {per}"
        )
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("--anchor", required=True)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    profiles = [load(Path(f)) for f in args.files]
    d = derive(profiles, args.anchor)
    text = render(d)
    sys.stdout.write(text)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(d, indent=1), encoding="utf8")
        Path(args.out).with_suffix(".txt").write_text(text, encoding="utf8")


if __name__ == "__main__":
    main()
