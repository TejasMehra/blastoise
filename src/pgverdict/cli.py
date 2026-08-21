"""Command-line interface: parse migrations and print their classification."""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from typing import Any

from pgverdict.ir import MigrationScript, ParsedStatement
from pgverdict.parser import MigrationParseError, parse_migration_file


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


def main(argv: list[str] | None = None) -> int:
    arg_parser = argparse.ArgumentParser(
        prog="pgverdict",
        description="Analyze Postgres schema migrations for production safety.",
    )
    subparsers = arg_parser.add_subparsers(dest="command", required=True)
    parse_cmd = subparsers.add_parser("parse", help="parse and classify migration files")
    parse_cmd.add_argument("files", nargs="+", help="migration .sql files")
    parse_cmd.add_argument("--json", action="store_true", help="emit JSON instead of text")

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
