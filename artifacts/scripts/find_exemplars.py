"""Scan the wild corpus for exemplar statements of the scale-harness forms."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from blastoise.parser import MigrationParseError, parse_migration_file

SCRATCH = Path(__file__).parent
CORPUS = SCRATCH / "corpus"
OUT = SCRATCH / "exemplars.json"

STATEMENT_KINDS = {
    "create_index",
    "create_unique_index",
    "update_without_where",
    "delete_without_where",
    "update",
    "reindex",
}
ACTION_KINDS = {
    "add_foreign_key",
    "add_foreign_key_not_valid",
    "validate_constraint",
    "alter_column_type",
    "set_not_null",
    "add_check",
    "add_unique",
    "add_column",
    "add_column_default_nonvolatile",
    "add_column_default_volatile",
    "add_column_serial",
    "add_column_identity",
    "add_column_generated_stored",
}
PER_KIND = 6

def main() -> None:
    found: dict[str, list[dict[str, str]]] = defaultdict(list)
    for path in sorted(CORPUS.glob("*.sql")):
        try:
            script = parse_migration_file(str(path))
        except MigrationParseError:
            continue
        for statement in script.statements:
            kind = statement.kind.value
            if kind in STATEMENT_KINDS and len(found[kind]) < PER_KIND:
                found[kind].append({"file": path.name, "sql": statement.sql[:400]})
            for action in statement.alter_actions:
                ak = action.kind.value
                if ak in ACTION_KINDS and len(found[ak]) < PER_KIND:
                    found[ak].append({"file": path.name, "sql": statement.sql[:400]})
    OUT.write_text(json.dumps(found, indent=1), encoding="utf8")
    for kind in sorted(found):
        print(f"{kind}: {len(found[kind])}")


if __name__ == "__main__":
    main()
