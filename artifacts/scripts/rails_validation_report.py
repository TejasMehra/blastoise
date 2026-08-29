"""Turn the Rails extraction validation results into a report.

Written to the same standard as the wild-corpus reports: every case is
accounted for, failures are named individually rather than folded into a
percentage, and the denominator never quietly shrinks. A case that could
not be extracted is a result about the extractor, not a case to drop.
"""

from __future__ import annotations

import argparse
import collections
import json
from pathlib import Path

ORDER = (
    "match",
    "mismatch",
    "extract_failed",
    "parse_failed",
    "replay_failed",
    "setup_failed",
    "no_sql",
    "timeout",
    "skipped",
    "error",
)

MEANING = {
    "match": "extracted SQL produced an identical schema",
    "mismatch": "extracted SQL produced a DIFFERENT schema",
    "extract_failed": "the migration could not be run (falls back to not-assessed)",
    "parse_failed": "extracted SQL did not parse (falls back to not-assessed)",
    "replay_failed": "extracted SQL would not re-apply to the same pre-state",
    "setup_failed": "the pre-state could not be rebuilt (validation harness)",
    "no_sql": "the migration ran but emitted no SQL",
    "timeout": "exceeded the per-case time limit",
    "skipped": "no usable pre-state in the repository's history",
    "error": "the validator itself failed on this case",
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--results", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    cases = json.loads(Path(args.results).read_text(encoding="utf-8"))
    lines: list[str] = []
    add = lines.append

    add("RAILS EXTRACTION VALIDATION")
    add("=" * 72)
    add(
        "Method: for each migration, build its pre-migration schema twice from "
        "the base commit."
    )
    add(
        "  A = pre-state + the real migration (run through the shipped harness)"
    )
    add("  B = pre-state + the SQL that harness extracted, applied statement by statement")
    add("A and B are then compared column by column, index by index, constraint by")
    add("constraint. Extraction is faithful only where they are identical.")
    add("")

    by_repo = collections.Counter(c["repo"] for c in cases)
    add(f"cases: {len(cases)}   applications: {len(by_repo)}")
    add("  " + "   ".join(f"{repo}={count}" for repo, count in sorted(by_repo.items())))
    versions = sorted({c["rails_version"] for c in cases if c.get("rails_version")})
    add(f"  ActiveRecord versions exercised: {', '.join(versions) or 'none reached'}")
    add("")

    counts = collections.Counter(c["status"] for c in cases)
    add("OUTCOMES")
    add("-" * 72)
    for status in ORDER:
        if counts[status]:
            share = 100 * counts[status] / len(cases)
            add(f"  {status:16s} {counts[status]:3d}  ({share:4.1f}%)  {MEANING[status]}")
    unknown = set(counts) - set(ORDER)
    for status in sorted(unknown):
        add(f"  {status:16s} {counts[status]:3d}  (unclassified)")
    add("")

    verified = counts["match"]
    attempted = len(cases)
    add(
        f"Extraction verified faithful on {verified} of {attempted} real migrations "
        f"({100 * verified / attempted:.0f}%)."
    )
    wrong = counts["mismatch"] + counts["replay_failed"]
    add(
        f"Extraction produced WRONG SQL on {wrong} of {attempted} "
        f"({100 * wrong / attempted:.0f}%) -- the number that would matter for trust."
    )
    add("")

    # Which risky constructs actually survived, evidenced by the statements.
    add("CONSTRUCTS RECOVERED (from the extracted statements of matching cases)")
    add("-" * 72)
    probes = {
        "CREATE INDEX CONCURRENTLY": lambda s: "CONCURRENTLY" in s.upper(),
        "DROP INDEX CONCURRENTLY": lambda s: "DROP INDEX CONCURRENTLY" in s.upper(),
        "explicit BEGIN/COMMIT": lambda s: s.strip().upper() in ("BEGIN", "COMMIT"),
        "backfill UPDATE": lambda s: s.strip().upper().startswith("UPDATE"),
        "DELETE": lambda s: s.strip().upper().startswith("DELETE"),
        "ALTER TABLE ... VALIDATE": lambda s: "VALIDATE CONSTRAINT" in s.upper(),
        "ADD CONSTRAINT ... NOT VALID": lambda s: "NOT VALID" in s.upper(),
        "SET NOT NULL": lambda s: "SET NOT NULL" in s.upper(),
        "DROP COLUMN": lambda s: "DROP COLUMN" in s.upper(),
        "CREATE TABLE": lambda s: s.strip().upper().startswith("CREATE TABLE"),
    }
    for label, test in probes.items():
        hits = [
            c
            for c in cases
            if c["status"] == "match" and any(test(s) for s in c["statements"])
        ]
        if hits:
            add(f"  {label:28s} {len(hits):3d} case(s)")
    add("")

    failures = [c for c in cases if c["status"] not in ("match",)]
    if failures:
        add("EVERY CASE THAT DID NOT VERIFY, WITH ITS REASON")
        add("-" * 72)
        for case in failures:
            name = case["path"].rsplit("/", 1)[-1]
            add(f"  [{case['status']}] {case['repo']}/{name}")
            if case.get("reason"):
                add(f"      {case['reason'][:200]}")
            for difference in case.get("differences", [])[:8]:
                add(f"      DIFF {difference[:180]}")
        add("")

    adapted = [c for c in cases if c.get("adaptations")]
    if adapted:
        add("PRE-STATE ADAPTATIONS (validation machine, applied to A and B alike)")
        add("-" * 72)
        removed: collections.Counter[str] = collections.Counter()
        for case in adapted:
            for line in case["adaptations"]:
                removed[line[:110]] += 1
        add(f"  {len(adapted)} case(s) had lines removed from the pre-state schema.")
        add("  These are objects this machine cannot create (pgvector has no Windows")
        add("  build). Both databases are built from the same adapted file, so the")
        add("  comparison is unaffected; it is disclosed because the input changed.")
        for line, count in removed.most_common(12):
            add(f"    x{count:<3d} {line}")
        add("")

    add("STATEMENTS EXTRACTED PER CASE")
    add("-" * 72)
    for case in cases:
        if case["status"] != "match":
            continue
        name = case["path"].rsplit("/", 1)[-1][:48]
        add(f"  {case['repo']:10s} {name:50s} {len(case['statements']):2d} stmt")
        for statement in case["statements"]:
            add(f"      {' '.join(statement.split())[:150]}")
    add("")

    Path(args.out).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
