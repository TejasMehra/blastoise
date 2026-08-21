"""Part 3 analysis: turn scale_results.json into the report tables."""

from __future__ import annotations

import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path

SCRATCH = Path(__file__).parent
DATA = json.loads((SCRATCH / "scale_results.json").read_text(encoding="utf8"))

SIZE_ORDER = ["t_1k", "t_100k", "t_1m", "t_10m"]
GUESSES = {
    "heap_rewrite": ("rows/s", 100_000),
    "validation_scan": ("rows/s", 500_000),
    "fk_validation": ("rows/s", 100_000),
    "index_build": ("rows/s", 250_000),
    "dml_update": ("rows/s", 50_000),
    "dml_delete": ("rows/s", 100_000),
    "index_bytes": ("bytes/s", 50_000_000),
    "constant_op": ("ms", 10),
}


def best_duration(record) -> dict | None:
    """The row duration the classification banded on: max high_ms estimate."""
    durations = [d for d in record["predicted"]["durations"] if "high_ms" in d]
    if not durations:
        return None
    return max(durations, key=lambda d: d["high_ms"])


def main() -> None:
    results = DATA["results"]

    # ---- 1. classification distribution per size ----------------------
    print("== classification distribution per size ==")
    dist: dict[str, Counter] = defaultdict(Counter)
    for r in results:
        dist[r["table"]][r["predicted"]["classification"]] += 1
    for t in SIZE_ORDER:
        if t in dist:
            print(f"  {t}: {dict(dist[t])}")

    print("\n== per-size classification of each case ==")
    by_case: dict[str, dict[str, str]] = defaultdict(dict)
    for r in results:
        by_case[r["case"]][r["table"]] = r["predicted"]["classification"]
    for case, row in by_case.items():
        cells = [row.get(t, "-")[:4] for t in SIZE_ORDER]
        print(f"  {case:32s} {' '.join(c.ljust(4) for c in cells)}")

    # ---- 2. predicted interval vs measured -----------------------------
    print("\n== predicted vs measured (cases with an interval) ==")
    inside = 0
    outside_lo = 0
    outside_hi = 0
    width_ratios = []
    high_over_measured = []
    rows_out = []
    for r in results:
        d = best_duration(r)
        m = r["measured"]["ms"]
        if d is None or m is None or r["measured"]["error"]:
            continue
        lo, hi, pt = d["low_ms"], d["high_ms"], d["point_ms"]
        ok = lo <= m <= hi
        if ok:
            inside += 1
        elif m < lo:
            outside_lo += 1
        else:
            outside_hi += 1
        width_ratios.append(hi / max(lo, 1))
        high_over_measured.append(hi / max(m, 1))
        rows_out.append(
            f"  {r['table']:7s} {r['case']:32s} pred [{lo:>8}..{pt:>9}..{hi:>9}]ms "
            f"meas {m:>8}ms {'IN ' if ok else ('LOW' if m < lo else 'HIGH')} "
            f"({d['constant_key']})"
        )
    for line in rows_out:
        print(line)
    total = inside + outside_lo + outside_hi
    print(
        f"\n  coverage: {inside}/{total} inside "
        f"({outside_lo} below low, {outside_hi} above high)"
    )
    print(
        f"  interval width (high/low): median {statistics.median(width_ratios):.0f}x"
    )
    print(
        f"  high/measured: median {statistics.median(high_over_measured):.1f}x, "
        f"p90 {sorted(high_over_measured)[int(len(high_over_measured) * 0.9)]:.1f}x"
    )

    # ---- 3. constants: measured throughput per family ------------------
    print("\n== duration constants: guess vs measured ==")
    fam_measurements: dict[str, list[tuple[str, str, float]]] = defaultdict(list)
    for r in results:
        d = best_duration(r)
        m = r["measured"]["ms"]
        if d is None or m is None or r["measured"]["error"]:
            continue
        key = d["constant_key"]
        if key == "constant_op":
            fam_measurements[key].append((r["table"], r["case"], float(m)))
            continue
        if key == "index_bytes":
            size = r.get("index_bytes")
            if size and m > 0:
                fam_measurements[key].append(
                    (r["table"], r["case"], size / (m / 1000))
                )
            continue
        rows = r["measured"]["rowcount"] or r["rows"]
        if m > 0:
            fam_measurements[key].append((r["table"], r["case"], rows / (m / 1000)))
    for fam, (unit, guess) in GUESSES.items():
        ms = fam_measurements.get(fam, [])
        if not ms:
            print(f"  {fam:16s} guess {guess:>12,} {unit}: no measurements")
            continue
        # constants are throughputs: small-table runs are dominated by fixed
        # cost, so report per-size and a large-table (>=1m) median.
        large = [v for (t, _c, v) in ms if t in ("t_1m", "t_10m")]
        med = statistics.median(large if large else [v for (_t, _c, v) in ms])
        print(
            f"  {fam:16s} guess {guess:>12,} {unit}: "
            f"measured median {med:>14,.0f} ({len(ms)} samples)"
        )
        for t in SIZE_ORDER:
            vals = [v for (tt, _c, v) in ms if tt == t]
            if vals:
                print(
                    f"      {t:7s}: "
                    + ", ".join(f"{v:,.0f}" for v in sorted(vals))
                )

    # ---- 4. lock mode check ---------------------------------------------
    print("\n== lock-mode mismatches (predicted statement lock vs measured) ==")
    mismatches = 0
    for r in results:
        pred = r["predicted"]["statement_lock_mode"]
        meas = r["measured"]["strongest_table_mode"]
        if meas is None:
            continue
        # REINDEX INDEX locks the index AEL, the table SHARE: statement lock
        # mode names the strongest anywhere, which is on the index.
        if r["case"] == "reindex_index":
            continue
        if pred != meas:
            mismatches += 1
            print(f"  {r['table']:7s} {r['case']:32s} pred={pred} measured={meas}")
    if mismatches == 0:
        print("  none")

    # ---- 5. rewrite ground truth ----------------------------------------
    print("\n== rewrite ground truth (relfilenode) ==")
    for r in results:
        if "rewrote_table" in r and r["table"] == "t_1m":
            print(f"  {r['case']:32s} rewrote={r['rewrote_table']}")

    # ---- 6. errors -------------------------------------------------------
    errs = [r for r in results if r["measured"]["error"]]
    if errs:
        print("\n== errors ==")
        for r in errs:
            print(f"  {r['table']} {r['case']}: {r['measured']['error']}")


if __name__ == "__main__":
    main()
