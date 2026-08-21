"""Print the actual SQL behind selected old->new tier moves, to eyeball them."""

from __future__ import annotations

import json
import sys
from pathlib import Path

SCRATCH = Path(__file__).parent
OLD = json.loads((SCRATCH / "corpus_offline_baseline.json").read_text(encoding="utf8"))
NEW = json.loads((SCRATCH / "corpus_offline_new.json").read_text(encoding="utf8"))

WANT_KINDS = set(sys.argv[1:]) or {
    "drop_extension",
    "drop_schema",
    "drop_table",
    "do_block",
    "update",
    "delete",
    "drop_policy",
    "alter_table",
}

old = {(f, i): (t, k) for f, i, k, t, _b, _m in OLD["rows"]}
new = {(f, i): (t, k) for f, i, k, t, _b, _m in NEW["rows"]}

from blastoise.catalog.loader import load_catalog  # noqa: E402
from blastoise.parser import parse_migration_file  # noqa: E402
from blastoise.verdict import assess_script  # noqa: E402

catalog = load_catalog()
targets: dict[str, list[int]] = {}
for key in old:
    old_tier, kind = old[key]
    new_tier, _ = new[key]
    if new_tier == "safe_irreversible" and kind in WANT_KINDS:
        targets.setdefault(key[0], []).append(key[1])

shown = 0
for file_name, indices in sorted(targets.items()):
    script = parse_migration_file(str(SCRATCH / "corpus" / file_name))
    result = assess_script(script, catalog, 17)
    for index in sorted(indices):
        statement = result.statements[index]
        old_tier, kind = old[(file_name, index)]
        print(f"--- {file_name}[{index}] {kind}: {old_tier} -> safe_irreversible")
        print(f"    sql: {statement.sql.strip()[:150]}")
        print(f"    rationale: {statement.verdict.rationale[:170]}")
        for condition in statement.verdict.conditions[:2]:
            print(f"    cond: {condition[:170]}")
        print(f"    locks: {[(r.relation, r.lock_mode.value, r.blocks_reads) for row in statement.rows for r in row.relations][:4]}")
        shown += 1
        if shown >= 24:
            sys.exit(0)
