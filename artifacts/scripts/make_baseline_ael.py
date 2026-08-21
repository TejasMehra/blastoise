"""Reconstruct the pre-AEL-floor engine, by reversing the one wiring edit.

Same discipline as ``make_baseline.py``: copy the working tree, then
reverse each edit that introduced the change under test, asserting every
reversal matched exactly once -- which is what makes the old-vs-new diff
evidence rather than argument. Here that is a single line, the call to
``_floor_for_access_exclusive`` in ``_assess_row``. The floor function and
its helper stay in the reconstructed tree (dead code once the call is
removed), so the diff is the behavior change alone and nothing incidental.

Usage: python make_baseline_ael.py [src/pgverdict] [dest_dir]

Defaults: the pgverdict package of the repo this script is committed in,
copied to ``<cwd>/baseline_ael/pgverdict``. Point ``corpus_tiers.py
baseline_ael`` / ``corpus_replay.py baseline_ael`` / ``reassess_baseline.py
baseline_ael`` at the result.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
DEFAULT_SRC = HERE.parents[2] / "src" / "pgverdict"

SRC = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_SRC
DEST = (Path(sys.argv[2]) if len(sys.argv) > 2 else Path.cwd() / "baseline_ael") / "pgverdict"

if not (SRC / "verdict" / "engine.py").exists():
    raise SystemExit(f"no pgverdict package at {SRC}")
if DEST.parent.exists():
    shutil.rmtree(DEST.parent)
shutil.copytree(SRC, DEST, ignore=shutil.ignore_patterns("__pycache__"))
print(f"copied {SRC} -> {DEST}")

path = DEST / "verdict" / "engine.py"
src = path.read_text(encoding="utf8")
new = (
    "    verdict = _escalate_for_contention(verdict, contention)\n"
    "    verdict = _floor_for_access_exclusive(ctx, verdict, relations)\n"
)
old = "    verdict = _escalate_for_contention(verdict, contention)\n"
assert src.count(new) == 1, f"matched {src.count(new)}"
path.write_text(src.replace(new, old), encoding="utf8")
print("reverted verdict/engine.py: 1 edit (the floor call)")
