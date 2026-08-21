"""Offline corpus run: assess all 3,081 wild files with no snapshot at PG 17."""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

from pgverdict.catalog.loader import load_catalog
from pgverdict.parser import MigrationParseError, parse_migration_file
from pgverdict.verdict import Classification, assess_script

CORPUS = Path(__file__).parent / "corpus"
OUT = Path(__file__).parent / "corpus_offline_results.json"


def main() -> None:
    catalog = load_catalog()
    files = sorted(CORPUS.glob("*.sql"))
    class_counts: Counter[str] = Counter()
    method_counts: Counter[str] = Counter()
    unknown_by_kind: Counter[str] = Counter()
    unsafe_by_kind: Counter[str] = Counter()
    per_file: dict[str, dict[str, int]] = {}
    parse_failures: list[str] = []
    statements = 0
    resolve_errors: list[str] = []

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
        counts = result.classification_counts()
        per_file[path.name] = {k.value: v for k, v in counts.items() if v}
        for statement in result.statements:
            statements += 1
            cls = statement.verdict.classification
            class_counts[cls.value] += 1
            method_counts[statement.verdict.method.value] += 1
            if cls is Classification.UNKNOWN:
                unknown_by_kind[statement.kind.value] += 1
            elif cls is Classification.UNSAFE:
                unsafe_by_kind[statement.kind.value] += 1

    report = {
        "mode": "offline",
        "pg_version": 17,
        "files": len(files),
        "parsed": len(files) - len(parse_failures),
        "parse_failures": parse_failures,
        "resolve_errors": resolve_errors,
        "statements": statements,
        "classification": dict(class_counts.most_common()),
        "methods": dict(method_counts.most_common()),
        "unknown_by_kind": dict(unknown_by_kind.most_common(25)),
        "unsafe_by_kind": dict(unsafe_by_kind.most_common(25)),
        "per_file": per_file,
    }
    OUT.write_text(json.dumps(report, indent=1), encoding="utf8")
    for key in ("files", "parsed", "statements"):
        print(key, report[key])
    print("classification:", report["classification"])
    print("methods:", report["methods"])
    print("unknown_by_kind:", dict(list(report["unknown_by_kind"].items())[:15]))
    print("unsafe_by_kind:", report["unsafe_by_kind"])
    print("resolve_errors:", len(resolve_errors), resolve_errors[:5])


if __name__ == "__main__":
    sys.exit(main())
