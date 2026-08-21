"""Command-line interface.

Named surfaces (help text, docs, README) carry the product's vocabulary:
Torrent is the parser and IR, Shell Armour the lock semantics catalog,
Hydro Scan the live introspection layer, Pressure Levels the five
classification tiers. None of that reaches the payload — ``--json`` keys,
enum values and exit codes are the plain machine names, and a test pins
them.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from typing import Any

from blastoise.ir import MigrationScript, ParsedStatement
from blastoise.parser import MigrationParseError, parse_migration_file


def _to_jsonable(value: object) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _to_jsonable(getattr(value, field.name))
            for field in dataclasses.fields(value)
        }
    if isinstance(value, tuple | list):
        return [_to_jsonable(item) for item in value]
    return value


def _format_statement(statement: ParsedStatement, indent: str = "  ") -> list[str]:
    target = f"  target={statement.targets[0]}" if statement.targets else ""
    lines = [f"{indent}{statement.span.line:>4}: {statement.kind}{target}"]
    for action in statement.alter_actions:
        parts = [str(action.kind)]
        if action.column:
            parts.append(f"column={action.column}")
        if action.constraint_name:
            parts.append(f"constraint={action.constraint_name}")
        if action.column_type:
            parts.append(f"type={action.column_type}")
        if action.default is not None:
            parts.append(f"default={action.default.expression} [{action.default.volatility}]")
        lines.append(f"{indent}      - {' '.join(parts)}")
    return lines


def _print_script(script: MigrationScript) -> None:
    print(script.path or "<stdin>")
    for statement in script.statements:
        print("\n".join(_format_statement(statement)))
    for group in script.transaction_groups:
        label = "explicit" if group.explicit else "implicit"
        print(f"  tx: {label} group of {len(group.statement_indices)} statement(s)")
    for warning in script.warnings:
        print(f"  warning: {warning}")


EPILOG = """Pressure Levels, the five tiers a statement can land in. The names on the
left are what appears in JSON and exit codes; the names on the right are
what a report calls them.

  safe               Calm Water        nothing to do
  safe_irreversible  One-Way Current   proceed, but there is no undo
  needs_timing       Rain Check        safe in itself, wrong at the wrong moment
  unsafe             Hydro Pump        do not run as written
  unknown            Fog               not enough evidence to say

Components: Torrent (parser and IR), Shell Armour (lock semantics
catalog), Hydro Scan (live introspection), Shell Report (the verdict
document), Training Ground (the scale harness), Evolution (the
calibration loop), Shell Seal (signing and attestation).

'bt' is an alias for this command.

Blastoise is a working codename; see the NOTICE in README.md.
"""


def main(argv: list[str] | None = None) -> int:
    arg_parser = argparse.ArgumentParser(
        prog="blastoise",
        description=(
            "Blastoise: know the blast radius before you migrate. Reads a "
            "migration and reports what it will actually do to production: "
            "which locks it takes, what they block, and for how long."
        ),
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = arg_parser.add_subparsers(dest="command", required=True)
    parse_cmd = subparsers.add_parser(
        "parse",
        help="run Torrent over migration files: parse and classify every statement",
        description=(
            "Torrent, the parser and IR. Classifies every statement by its "
            "exact DDL form, using the real Postgres grammar."
        ),
    )
    parse_cmd.add_argument("files", nargs="+", help="migration .sql files")
    parse_cmd.add_argument(
        "--json",
        action="store_true",
        help="emit JSON instead of text (plain machine field names, no flavour)",
    )

    args = arg_parser.parse_args(argv)
    exit_code = 0
    scripts: list[MigrationScript] = []
    for file_name in args.files:
        try:
            scripts.append(parse_migration_file(file_name))
        except OSError as exc:
            print(f"error: cannot read {file_name}: {exc}", file=sys.stderr)
            exit_code = 2
        except MigrationParseError as exc:
            print(f"error: {exc}", file=sys.stderr)
            exit_code = 2

    if args.json:
        payload = [
            {key: value for key, value in _to_jsonable(script).items() if key != "source"}
            for script in scripts
        ]
        print(json.dumps(payload, indent=2))
    else:
        for script in scripts:
            _print_script(script)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
