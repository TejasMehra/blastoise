"""Offline wild-corpus run emitting per-statement tiers, for old-vs-new diffing.

Usage:  python corpus_tiers.py <baseline|new> <out.json>

Any mode other than ``new`` names a reconstructed tree under the
scratchpad (``baseline`` = pre-tier-restructure, ``baseline_ael`` =
pre-ACCESS-EXCLUSIVE-floor) and prepends it to sys.path so that engine is
imported instead of the working tree's. Records
one row per statement -- (file, index, kind, tier, band, method) -- so the
comparison can answer "did any individual statement move from UNSAFE into
a safe tier", not merely "did the totals move".
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

SCRATCH = Path(__file__).parent
MODE = sys.argv[1] if len(sys.argv) > 1 else "new"
OUT = Path(sys.argv[2]) if len(sys.argv) > 2 else SCRATCH / f"corpus_offline_{MODE}.json"

if MODE != "new":
    # Any reconstructed tree: "baseline" (pre-tier-restructure) or
    # "baseline_ael" (pre-ACCESS-EXCLUSIVE-floor). Prepending it makes the
    # reconstructed engine win the import over the working tree's.
    sys.path.insert(0, str(SCRATCH / MODE))

from blastoise.catalog.loader import load_catalog  # noqa: E402
from blastoise.parser import MigrationParseError, parse_migration_file  # noqa: E402
from blastoise.verdict import assess_script  # noqa: E402
import blastoise  # noqa: E402

CORPUS = SCRATCH / "corpus"


def main() -> None:
    print(f"mode={MODE} using {blastoise.__file__}", flush=True)
    catalog = load_catalog()
    files = sorted(CORPUS.glob("*.sql"))
    tiers: Counter[str] = Counter()
    methods: Counter[str] = Counter()
    by_kind: dict[str, Counter[str]] = {}
    rows: list[list[object]] = []
    parse_failures: list[str] = []
    resolve_errors: list[str] = []
    statements = 0

    for path in files:
        try:
            script = parse_migration_file(str(path))
        except MigrationParseError:
            parse_failures.append(path.name)
            continue
        try:
            result = assess_script(script, catalog, 17)
        except Exception as exc:  # noqa: BLE001 - report, don't die
            resolve_errors.append(f"{path.name}: {exc}")
            continue
        for statement in result.statements:
            statements += 1
            verdict = statement.verdict
            tier = verdict.classification.value
            tiers[tier] += 1
            methods[verdict.method.value] += 1
            by_kind.setdefault(tier, Counter())[statement.kind.value] += 1
            rows.append(
                [
                    path.name,
                    statement.statement_index,
                    statement.kind.value,
                    tier,
                    verdict.band.value if verdict.band else None,
                    verdict.method.value,
                ]
            )

    report = {
        "mode": MODE,
        "pg_version": 17,
        "files": len(files),
        "parsed": len(files) - len(parse_failures),
        "parse_failures": parse_failures,
        "resolve_errors": resolve_errors,
        "statements": statements,
        "tiers": dict(tiers.most_common()),
        "methods": dict(methods.most_common()),
        "by_kind": {t: dict(c.most_common(20)) for t, c in by_kind.items()},
        "rows": rows,
    }
    OUT.write_text(json.dumps(report, indent=1), encoding="utf8")
    total = statements or 1
    print(f"files={len(files)} parsed={report['parsed']} statements={statements}")
    for tier, count in tiers.most_common():
        print(f"  {tier:20s} {count:6d}  {100 * count / total:5.1f}%")
    print("methods:", dict(methods.most_common()))
    print("resolve_errors:", len(resolve_errors), resolve_errors[:3])
    print("wrote", OUT)


if __name__ == "__main__":
    main()
