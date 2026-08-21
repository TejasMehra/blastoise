"""Compare the post-fix scale run against the 2026-08-21 pre-fix run.

Answers the two mandated questions — are the six dangerous misses gone,
and did the false UNSAFEs on ADD FK at 10M clear — plus flags any NEW
dangerous misses the changes introduced.
"""

from __future__ import annotations

import json
from pathlib import Path

SCRATCH = Path(__file__).parent
OLD = json.loads((SCRATCH / "scale_results_prev.json").read_text(encoding="utf8"))
NEW = json.loads((SCRATCH / "scale_results.json").read_text(encoding="utf8"))


def best_duration(record: dict) -> dict | None:
    durations = [d for d in record["predicted"]["durations"] if "high_ms" in d]
    if not durations:
        return None
    return max(durations, key=lambda d: d["high_ms"])


def status(record: dict) -> tuple[str, str]:
    """(coverage status, detail) for one case record."""
    d = best_duration(record)
    m = record["measured"]["ms"]
    if record["measured"]["error"]:
        return "ERR", record["measured"]["error"]
    if d is None or m is None:
        return "-", "no interval"
    lo, hi = d["low_ms"], d["high_ms"]
    tag = "IN" if lo <= m <= hi else ("LOW" if m < lo else "HIGH")
    return tag, f"meas {m}ms vs [{lo}..{d['point_ms']}..{hi}] ({d['constant_key']})"


def index_of(data: dict) -> dict[tuple[str, str], dict]:
    return {(r["case"], r["table"]): r for r in data["results"]}


old_by = index_of(OLD)
new_by = index_of(NEW)

print("== 1. the six dangerous misses of the pre-fix run ==")
DANGEROUS = [
    ("reindex_index", "t_100k"),
    ("reindex_index", "t_1m"),
    ("reindex_index", "t_10m"),
    ("alter_type_varchar_widen", "t_100k"),
    ("alter_type_varchar_widen", "t_1m"),
    ("alter_type_varchar_widen", "t_10m"),
]
for key in DANGEROUS:
    old_tag, old_detail = status(old_by[key])
    new_tag, new_detail = status(new_by[key])
    cls = new_by[key]["predicted"]["classification"]
    print(f"  {key[0]:26s} {key[1]:7s} was {old_tag:4s} ({old_detail})")
    print(f"  {'':26s} {'':7s} now {new_tag:4s} ({new_detail}) cls={cls}")

print("\n== 2. ADD FK at 10M: false UNSAFEs ==")
for case in ("add_fk_plain", "add_fk_cascade", "validate_fk"):
    o = old_by[(case, "t_10m")]
    n = new_by[(case, "t_10m")]
    print(
        f"  {case:16s} was {o['predicted']['classification']:18s} "
        f"now {n['predicted']['classification']:18s} "
        f"(measured {n['measured']['ms']}ms)"
    )

print("\n== 3. every dangerous (HIGH) miss in the NEW run ==")
any_high = False
for r in NEW["results"]:
    tag, detail = status(r)
    if tag == "HIGH":
        any_high = True
        print(f"  {r['table']:7s} {r['case']:34s} {detail}")
if not any_high:
    print("  none")

print("\n== 4. coverage old vs new ==")
for label, data in (("old", OLD), ("new", NEW)):
    tags = [status(r)[0] for r in data["results"]]
    counted = [t for t in tags if t in ("IN", "LOW", "HIGH")]
    print(
        f"  {label}: {counted.count('IN')}/{len(counted)} inside, "
        f"{counted.count('LOW')} below low (safe), "
        f"{counted.count('HIGH')} above high (dangerous)"
    )

print("\n== 5. classification distribution per size (new run) ==")
from collections import Counter, defaultdict

dist: dict[str, Counter] = defaultdict(Counter)
for r in NEW["results"]:
    dist[r["table"]][r["predicted"]["classification"]] += 1
for t in ("t_1k", "t_100k", "t_1m", "t_10m"):
    print(f"  {t}: {dict(dist[t])}")

print("\n== 6. classification changes at 10M (old -> new) ==")
for r in NEW["results"]:
    if r["table"] != "t_10m":
        continue
    key = (r["case"], "t_10m")
    if key not in old_by:
        print(f"  {r['case']:34s} NEW CASE -> {r['predicted']['classification']}")
        continue
    old_cls = old_by[key]["predicted"]["classification"]
    new_cls = r["predicted"]["classification"]
    if old_cls != new_cls:
        print(f"  {r['case']:34s} {old_cls} -> {new_cls} (measured {r['measured']['ms']}ms)")
