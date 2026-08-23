"""CLI: ``python -m validation.harness run|score|corpus``.

``run``    executes the corpus (``--smoke`` for a quick harness check at
           1k/20k rows with shortened holds and no calibration probe).
``score``  re-labels and re-summarizes an existing results JSON without
           touching a database — useful after editing the labeling rule.
``corpus`` validates the YAML and prints the family weights.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from validation.harness import fixtures as fx
from validation.harness import metrics, report
from validation.harness.corpus import load_corpus
from validation.harness.runner import RunOptions, run, score

RESULTS_DIR = Path(__file__).resolve().parent.parent / "results"


def _stamp() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H%M%SZ")


def _score_and_write(results: dict[str, Any], out_json: Path) -> None:
    score(results)
    summary = metrics.summarize(results)
    results["summary"] = summary
    out_json.write_text(json.dumps(results, indent=1), encoding="utf8")
    text = report.render(summary, results)
    out_txt = out_json.with_suffix(".txt")
    out_txt.write_text(text, encoding="utf8")
    sys.stdout.write(text)
    print(f"\nwrote {out_json}\nwrote {out_txt}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="validation.harness")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run")
    p_run.add_argument("--pg-bin", default=None)
    p_run.add_argument("--out", default=None)
    p_run.add_argument("--smoke", action="store_true")
    p_run.add_argument("--only", default="", help="comma-separated case ids")
    p_run.add_argument("--family", default="", help="comma-separated families")
    p_run.add_argument("--sizes", default="", help="comma-separated fixture tables")
    p_run.add_argument("--skip-probes", action="store_true")
    p_run.add_argument("--max-hold", type=int, default=None, help="cap holder seconds")

    p_score = sub.add_parser("score")
    p_score.add_argument("results")

    sub.add_parser("corpus")

    args = parser.parse_args(argv)
    if args.command == "corpus":
        cases = load_corpus()
        fam = Counter(c.family for c in cases)
        adv = Counter(c.adversarial or "(plain)" for c in cases)
        stmts = sum(len(c.expect) for c in cases)
        print(f"{len(cases)} cases, {stmts} labeled statements")
        for k, v in fam.most_common():
            print(f"  {k:22s} {v:4d}  {100 * v / len(cases):5.1f}%")
        print("adversarial:", dict(adv))
        return 0
    if args.command == "score":
        path = Path(args.results)
        results = json.loads(path.read_text(encoding="utf8"))
        _score_and_write(results, path)
        return 0

    out = Path(args.out) if args.out else RESULTS_DIR / (
        f"validation_{'smoke_' if args.smoke else ''}{_stamp()}.json"
    )
    opts = RunOptions(
        pg_bin=fx.find_pg_bin(args.pg_bin),
        out=out,
        smoke=args.smoke,
        only=frozenset(s for s in args.only.split(",") if s),
        families=frozenset(s for s in args.family.split(",") if s),
        sizes=frozenset(s for s in args.sizes.split(",") if s),
        skip_probes=args.skip_probes,
        max_hold_s=(2 if args.smoke else args.max_hold),
    )
    results = run(opts)
    _score_and_write(results, out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
