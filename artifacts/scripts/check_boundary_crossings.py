"""Do any interval misses cross a classification boundary, and which way?

An interval miss only matters if the *tier* would have differed had the
model predicted the measured value. Each miss is re-banded under the
thresholds the engine uses: an ACCESS EXCLUSIVE hold blocks reads (2s /
20s), a weaker write-blocking lock uses 5s / 60s. Crossings are reported
by direction -- "engine stricter" is conservative and acceptable, "engine
more lenient" is the dangerous one and must be zero.

Only the statement-level lock mode is recorded, so REINDEX (whose ACCESS
EXCLUSIVE is on the index, under a SHARE on the table) is not recognised
as floored here and reads as "engine stricter".

Usage: python check_boundary_crossings.py [results.json]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

SCRATCH = Path(__file__).parent
PATH = Path(sys.argv[1]) if len(sys.argv) > 1 else SCRATCH / "scale_results.json"

FULL_SHORT, FULL_LONG = 2_000, 20_000
WRITE_SHORT, WRITE_LONG = 5_000, 60_000
RANK = {"safe": 0, "safe_irreversible": 0, "needs_timing": 1, "unsafe": 2}


def band(ms: int, blocks_reads: bool) -> str:
    short, long = (FULL_SHORT, FULL_LONG) if blocks_reads else (WRITE_SHORT, WRITE_LONG)
    if ms < short:
        return "safe"
    if ms < long:
        return "needs_timing"
    return "unsafe"


def main() -> None:
    results = json.loads(PATH.read_text(encoding="utf8"))["results"]
    misses = crossings = 0
    dangerous: list[tuple[str, str]] = []
    for record in results:
        predicted = record["predicted"]
        durations = predicted["durations"]
        highs = [d["high_ms"] for d in durations if d.get("high_ms") is not None]
        lows = [d["low_ms"] for d in durations if d.get("low_ms") is not None]
        measured = record["measured"]["ms"]
        if not highs or measured is None:
            continue
        high, low = max(highs), min(lows)
        if low <= measured <= high:
            continue
        misses += 1
        direction = "HIGH" if measured > high else "LOW "
        reads = predicted.get("statement_lock_mode") == "ACCESS EXCLUSIVE"
        predicted_tier = predicted["classification"]
        measured_tier = band(measured, reads)
        # The floor is not a duration judgment: an ACCESS EXCLUSIVE
        # statement cannot fall below NEEDS_TIMING however fast it runs.
        if reads and measured_tier == "safe":
            measured_tier = "needs_timing"
        delta = RANK[measured_tier] - RANK[predicted_tier]
        if delta == 0:
            mark = ""
        elif delta < 0:
            mark = "   CROSSES (engine stricter than measurement)"
            crossings += 1
        else:
            mark = "   !! CROSSES DANGEROUSLY (engine more lenient)"
            crossings += 1
            dangerous.append((record["table"], record["case"]))
        print(
            f"  {direction} {record['table']:7s} {record['case']:34s} "
            f"meas={measured:7d}ms interval=[{low}..{high}] "
            f"pred={predicted_tier:18s} bands-to={measured_tier}{mark}"
        )
    print("")
    print(f"misses: {misses}, crossing a classification boundary: {crossings}")
    print(f"crossings in the dangerous direction (must be 0): {len(dangerous)}")
    for entry in dangerous:
        print(f"  !! {entry}")


if __name__ == "__main__":
    main()
