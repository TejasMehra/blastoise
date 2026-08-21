"""Old-vs-new tier comparison for the scale harness, on identical inputs.

The new tiers come from the live run (scale_results.json); the old tiers
come from re-assessing the very same pickled snapshots with the
reconstructed pre-restructure engine. Same snapshot, same SQL, same
catalog -- so every difference is the engine change and nothing else.

This is where the "nothing left UNSAFE for a safe tier" check has teeth:
unlike the corpus, the harness actually produces UNSAFE verdicts.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

SCRATCH = Path(__file__).parent
SAFE_TIERS = {"safe", "safe_irreversible"}
SIZE_ORDER = ["t_1k", "t_100k", "t_1m", "t_10m"]

# Usage: compare_scale.py [new_results.json] [old_predictions.json]
NEW_PATH = Path(sys.argv[1]) if len(sys.argv) > 1 else SCRATCH / "scale_results.json"
OLD_PATH = (
    Path(sys.argv[2])
    if len(sys.argv) > 2
    else SCRATCH / "scale_baseline_predictions.json"
)

new_run = json.loads(NEW_PATH.read_text(encoding="utf8"))
new = {
    (r["case"], r["table"]): (
        r["predicted"]["classification"],
        r["predicted"].get("band"),
        r["measured"]["ms"],
    )
    for r in new_run["results"]
}
old = {
    (r["case"], r["table"]): (r["classification"], r["band"], None)
    for r in json.loads(OLD_PATH.read_text(encoding="utf8"))
}

shared = old.keys() & new.keys()
print(f"new: {NEW_PATH.name}")
print(f"old: {OLD_PATH.name}")
print(f"cases compared: {len(shared)} (old={len(old)} new={len(new)})")

print("\n== new five-tier distribution per size ==")
for size in SIZE_ORDER:
    dist: Counter[str] = Counter(
        new[k][0] for k in new if k[1] == size
    )
    total = sum(dist.values())
    parts = " / ".join(
        f"{dist.get(t, 0)} {t}"
        for t in ("safe", "safe_irreversible", "needs_timing", "unsafe", "unknown")
    )
    print(f"  {size:7s} ({total} cases): {parts}")

print("\n== old distribution per size (same snapshots, old engine) ==")
for size in SIZE_ORDER:
    dist = Counter(old[k][0] for k in old if k[1] == size)
    total = sum(dist.values())
    parts = " / ".join(f"{count} {tier}" for tier, count in sorted(dist.items()))
    print(f"  {size:7s} ({total} cases): {parts}")

print("\n== movement matrix ==")
matrix: Counter[tuple[str, str]] = Counter()
for key in shared:
    matrix[(old[key][0], new[key][0])] += 1
for (o, n), count in sorted(matrix.items(), key=lambda i: -i[1]):
    print(f"  {o:20s} -> {n:20s} {count:4d}{'  (unchanged)' if o == n else ''}")

print("\n== the mandated check ==")
violations = [k for k in shared if old[k][0] == "unsafe" and new[k][0] in SAFE_TIERS]
softened = [k for k in shared if old[k][0] == "unsafe" and new[k][0] != "unsafe"]
hardened = [k for k in shared if new[k][0] == "unsafe" and old[k][0] != "unsafe"]
print(f"  old UNSAFE: {sum(1 for k in shared if old[k][0] == 'unsafe')}")
print(f"  new UNSAFE: {sum(1 for k in shared if new[k][0] == 'unsafe')}")
print(f"  UNSAFE -> safe tier (must be 0): {len(violations)}")
for key in violations:
    print(f"    !! {key}: {old[key][0]} -> {new[key][0]}")
print(f"  UNSAFE -> anything else: {len(softened)}")
for key in sorted(softened):
    print(f"    -  {key}: {old[key][0]} -> {new[key][0]} (measured {new[key][2]}ms)")
print(f"  anything -> UNSAFE: {len(hardened)}")
for key in sorted(hardened):
    print(f"    +  {key}: {old[key][0]} -> {new[key][0]} (measured {new[key][2]}ms)")

print("\n== monotonicity: does every case get stricter with size? ==")
RANK = {"safe": 0, "safe_irreversible": 1, "needs_timing": 2, "unknown": 3, "unsafe": 4}
bad = []
for case in sorted({k[0] for k in new}):
    seq = [new[(case, s)][0] for s in SIZE_ORDER if (case, s) in new]
    ranks = [RANK[t] for t in seq]
    if any(b < a for a, b in zip(ranks, ranks[1:])):
        bad.append((case, seq))
print(f"  non-monotonic cases: {len(bad)}")
for case, seq in bad:
    print(f"    !! {case}: {seq}")
