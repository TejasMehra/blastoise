"""A compact digest of one results file for the write-up: every
disagreement with its predicted upper bound beside the measured hold, the
judgment-call cases, and what the traffic probe saw. ``python -m
validation.harness.digest results.json``."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def _rows(results: dict[str, Any]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    return [
        (r, s)
        for r in results["results"]
        for s in r.get("statements", [])
        if s.get("truth")
    ]


def digest(results: dict[str, Any]) -> str:
    out: list[str] = []
    add = out.append
    rows = _rows(results)

    add("DISAGREEMENTS (predicted -> truth | engine upper bound vs measured hold, "
        "on the truth basis)")
    for r, s in rows:
        if s["outcome"] == "match":
            continue
        m, p, t = s["measured"], s["predicted"], s["truth"]
        stall = m.get("traffic") or {}
        add(
            f"  {s['outcome']:8s} {r['case']}#{s['index']:<2} {p['tier']:>17s} -> "
            f"{t['tier']:<17s} | high={p['deciding_high_ms']!s:>7} ms  "
            f"hold={t.get('hold_ms', t['hold_ms_normalized']):>7} ms "
            f"(raw {m['wall_ms']}, wait {m['wait_ms']})  mode={m['strongest_preexisting_mode']}  "
            f"const={p['deciding_constant_key']}  stalls r/w={stall.get('max_read_stall_ms')}/"
            f"{stall.get('max_write_stall_ms')}  err={'yes' if m['error'] else 'no'}"
        )
        add(f"             {p['rationale'][:150]}")

    add("")
    add("JUDGMENT-CALL AND ADVERSARIAL CASES")
    for r, s in rows:
        if not r.get("adversarial"):
            continue
        m, p, t = s["measured"], s["predicted"], s["truth"]
        add(
            f"  {r['adversarial']:15s} {s['outcome']:8s} {r['case']}#{s['index']:<2} "
            f"{p['tier']:>17s} -> {t['tier']:<17s} "
            f"hold={t.get('hold_ms', t['hold_ms_normalized'])} ms "
            f"mode={m['strongest_preexisting_mode']}"
        )

    add("")
    add("CONCURRENCY: what the snapshot saw and what the engine did")
    for r in results["results"]:
        if r.get("family") != "concurrency":
            continue
        for s in r.get("statements", []):
            if not s.get("truth"):
                continue
            m, p, t = s["measured"], s["predicted"], s["truth"]
            add(
                f"  {r['case']:42s} holder={r.get('holder', {}).get('hold_s')}s "
                f"pre_age={r.get('holder', {}).get('pre_age_s')} long_txns="
                f"{r.get('snapshot_long_transactions')} waiters={r.get('snapshot_lock_waiters')} "
                f"wait={m['wait_ms']} ms stalls r/w="
                f"{(m.get('traffic') or {}).get('max_read_stall_ms')}/"
                f"{(m.get('traffic') or {}).get('max_write_stall_ms')} {p['tier']} -> {t['tier']} "
                f"({s['outcome']})"
            )

    add("")
    add("TRAFFIC PROBE vs LOCK MODE (AEL holds >= 1 s: did readers actually stall?)")
    for r, s in rows:
        m = s["measured"]
        tr = m.get("traffic") or {}
        if m["strongest_preexisting_mode"] == "ACCESS EXCLUSIVE" and m["wall_ms"] >= 1000:
            add(
                f"  {r['case']}#{s['index']:<2} wall={m['wall_ms']:>7} ms  read stall="
                f"{tr.get('max_read_stall_ms')!s:>7}  "
                f"write stall={tr.get('max_write_stall_ms')!s:>7}"
            )
    return "\n".join(out) + "\n"


def main(argv: list[str]) -> int:
    results = json.loads(Path(argv[1]).read_text(encoding="utf8"))
    sys.stdout.write(digest(results))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
