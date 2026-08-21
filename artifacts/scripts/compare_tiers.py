"""Per-statement old-tier -> new-tier movement matrix, plus the safety check.

Usage: python compare_tiers.py <baseline.json> <new.json>

The check the restructure has to pass: no individual statement may move
out of UNSAFE into a safe tier (SAFE or SAFE_IRREVERSIBLE). Reported per
statement, keyed by (file, statement index), never by totals -- totals can
cancel two opposite errors out.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

SAFE_TIERS = {"safe", "safe_irreversible"}


def load(path: str) -> dict[tuple[str, int], tuple[str, str, str | None]]:
    data = json.loads(Path(path).read_text(encoding="utf8"))
    out: dict[tuple[str, int], tuple[str, str, str | None]] = {}
    for file_name, index, kind, tier, band, _method in data["rows"]:
        out[(file_name, index)] = (tier, kind, band)
    return out


def main() -> None:
    old = load(sys.argv[1])
    new = load(sys.argv[2])
    shared = old.keys() & new.keys()
    print(f"statements: old={len(old)} new={len(new)} compared={len(shared)}")
    if old.keys() != new.keys():
        print(f"  !! key mismatch: only-old={len(old.keys() - new.keys())} "
              f"only-new={len(new.keys() - old.keys())}")

    matrix: Counter[tuple[str, str]] = Counter()
    kinds_by_move: dict[tuple[str, str], Counter[str]] = {}
    for key in shared:
        old_tier, kind, _ = old[key]
        new_tier, _, _ = new[key]
        matrix[(old_tier, new_tier)] += 1
        if old_tier != new_tier:
            kinds_by_move.setdefault((old_tier, new_tier), Counter())[kind] += 1

    print("\nmovement matrix (old -> new):")
    for (old_tier, new_tier), count in sorted(
        matrix.items(), key=lambda item: -item[1]
    ):
        mark = "  (unchanged)" if old_tier == new_tier else ""
        print(f"  {old_tier:20s} -> {new_tier:20s} {count:6d}{mark}")

    print("\nkinds behind each move:")
    for move, counter in sorted(kinds_by_move.items(), key=lambda i: -sum(i[1].values())):
        print(f"  {move[0]} -> {move[1]} ({sum(counter.values())}):")
        for kind, count in counter.most_common(8):
            print(f"      {kind:44s} {count}")

    # --- the mandated check -------------------------------------------
    violations = [
        (key, old[key], new[key])
        for key in shared
        if old[key][0] == "unsafe" and new[key][0] in SAFE_TIERS
    ]
    softened = [
        (key, old[key], new[key])
        for key in shared
        if old[key][0] == "unsafe" and new[key][0] != "unsafe"
    ]
    hardened = [
        (key, old[key], new[key])
        for key in shared
        if new[key][0] == "unsafe" and old[key][0] != "unsafe"
    ]
    print(f"\nold UNSAFE statements: {sum(1 for k in shared if old[k][0] == 'unsafe')}")
    print(f"new UNSAFE statements: {sum(1 for k in shared if new[k][0] == 'unsafe')}")
    print(f"UNSAFE -> safe tier (must be 0): {len(violations)}")
    for key, before, after in violations[:20]:
        print(f"    !! {key} {before} -> {after}")
    print(f"UNSAFE -> anything else: {len(softened)}")
    for key, before, after in softened[:20]:
        print(f"    -  {key} {before[1]} {before[0]} -> {after[0]}")
    print(f"anything -> UNSAFE (newly unsafe): {len(hardened)}")
    for key, before, after in hardened[:20]:
        print(f"    +  {key} {before[1]} {before[0]} -> {after[0]}")


if __name__ == "__main__":
    main()
