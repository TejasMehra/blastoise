"""Re-assess the scale harness's captured snapshots with the OLD engine.

The harness pickles the exact LiveSnapshot it fed the new engine for every
(case, size). Replaying those identical inputs through the reconstructed
pre-restructure engine gives an exact old-vs-new tier comparison without
paying for a second three-hour measured run -- and since the inputs are
literally the same objects, any difference is attributable to the engine
change alone.

Usage: python reassess_baseline.py [tree] [out.json]

``tree`` names a reconstructed engine under the scratchpad --
``baseline`` (pre-tier-restructure) or ``baseline_ael``
(pre-ACCESS-EXCLUSIVE-floor); it defaults to ``baseline``.
"""

from __future__ import annotations

import json
import pickle
import sys
from pathlib import Path

SCRATCH = Path(__file__).parent
TREE = sys.argv[1] if len(sys.argv) > 1 else "baseline"
sys.path.insert(0, str(SCRATCH / TREE))

from blastoise.catalog.loader import load_catalog  # noqa: E402
from blastoise.parser import parse_migration  # noqa: E402
from blastoise.verdict import assess_script  # noqa: E402
from blastoise.verdict.model import DurationEstimate  # noqa: E402
import blastoise  # noqa: E402

OUT = Path(sys.argv[2]) if len(sys.argv) > 2 else SCRATCH / f"scale_{TREE}_predictions.json"


def main() -> None:
    assert TREE in blastoise.__file__, f"wrong tree: {blastoise.__file__}"
    print("reconstructed engine:", blastoise.__file__)
    catalog = load_catalog()
    records = []
    files = sorted((SCRATCH / "snapshots").glob("*.pkl"))
    for path in files:
        with path.open("rb") as fh:
            case_id, table, rows, sql, snapshot = pickle.load(fh)
        script = parse_migration(sql + ";\n")
        statement = assess_script(script, catalog, 17, snapshot).statements[0]
        highs = [
            row.duration.high_ms
            for row in statement.rows
            if isinstance(row.duration, DurationEstimate)
        ]
        records.append(
            {
                "case": case_id,
                "table": table,
                "rows": rows,
                "classification": statement.verdict.classification.value,
                "band": statement.verdict.band.value if statement.verdict.band else None,
                "high_ms": max(highs) if highs else None,
                "constant_keys": sorted(
                    {
                        row.duration.constant_key
                        for row in statement.rows
                        if isinstance(row.duration, DurationEstimate)
                        and row.duration.constant_key
                    }
                ),
            }
        )
    OUT.write_text(json.dumps(records, indent=1), encoding="utf8")
    print(f"re-assessed {len(records)} snapshots -> {OUT}")


if __name__ == "__main__":
    main()
