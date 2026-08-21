"""Shared helpers for pgverdict tests."""

from __future__ import annotations

from pgverdict import AlterTableAction, MigrationScript, ParsedStatement, parse_migration


def parse(sql: str) -> MigrationScript:
    return parse_migration(sql)


def only_statement(sql: str) -> ParsedStatement:
    """Parse SQL that must contain exactly one statement and return it."""
    script = parse_migration(sql)
    assert len(script.statements) == 1, [s.kind for s in script.statements]
    return script.statements[0]


def only_action(sql: str) -> AlterTableAction:
    """Parse a one-statement ALTER with exactly one subcommand and return it."""
    statement = only_statement(sql)
    assert len(statement.alter_actions) == 1, [a.kind for a in statement.alter_actions]
    return statement.alter_actions[0]
