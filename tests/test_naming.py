"""The naming principle, pinned: cute in the wrapper, boring in the payload.

Blastoise's theme lives in things a person reads -- CLI help, the README,
the docs, rationale prose. It must never reach a machine-readable surface:
JSON keys, schema field names, enum values, or exit codes. Someone wiring
this into CI reads ``classification: needs_timing`` and should never need
to know what a Hydro Pump is.

These tests are the guardrail. They exist because the theme is a *working
codename* (see the NOTICE in README.md) and will very likely be replaced:
if a rename can only touch prose, it stays a find-and-replace instead of a
breaking API change for everyone consuming the output.
"""

from __future__ import annotations

import dataclasses
import enum
import importlib
import json
import pkgutil
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from verdict_helpers import relation, snapshot

import blastoise
from blastoise.parser import parse_migration
from blastoise.verdict import PRESSURE_LEVELS, Classification, pressure_level

# Words that are unmistakably ours, matched as whole tokens. Substring
# matching was tried first and is wrong: "concurrently" contains "current",
# "constraint" contains "rain". Identifiers are split on separators and on
# camelCase boundaries before comparison.
THEMED_TOKENS = frozenset(
    {
        "blastoise",
        "squirtle",
        "wartortle",
        "pokemon",
        "pokeball",
        "torrent",
        "hydro",
        "shell",
        "armour",
        "armor",
        "pressure",
        "calm",
        "rain",
        "pump",
        "fog",
        "training",
        "ground",
        "evolution",
        "seal",
        "blast",
    }
)

# Ordinary SQL and English that collides with the list above, and predates
# the codename: CHECK constraints (``add_check``), the pre-change type of a
# column (``current_type``). Only the multi-word phrases below can flag
# these.
ALLOWED_TOKENS = frozenset({"check", "current", "water", "way", "one"})

# The display names themselves, as phrases -- the bare tokens "calm" and
# "water" would be too coarse on their own, but "calm water" never occurs
# by accident.
THEMED_PHRASES = (
    "calm water",
    "one way current",
    "rain check",
    "hydro pump",
    "hydro scan",
    "shell report",
    "shell armour",
    "shell seal",
    "pressure level",
    "training ground",
)


def _tokens(text: str) -> list[str]:
    """Identifier split on separators and camelCase, lowercased."""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text)
    return [token.lower() for token in re.split(r"[^A-Za-z0-9]+", spaced) if token]


def _themed_hits(text: str) -> list[str]:
    """Themed vocabulary in ``text``, ignoring genuine SQL/domain words."""
    hits = [
        token
        for token in _tokens(text)
        if token in THEMED_TOKENS and token not in ALLOWED_TOKENS
    ]
    normalized = " ".join(_tokens(text))
    hits.extend(phrase for phrase in THEMED_PHRASES if phrase in normalized)
    return sorted(set(hits))


def _package_modules() -> list[str]:
    names = ["blastoise"]
    names.extend(
        info.name for info in pkgutil.walk_packages(blastoise.__path__, prefix="blastoise.")
    )
    return names


def _walk_json_keys(value: Any) -> list[str]:
    if isinstance(value, dict):
        keys = list(value)
        for item in value.values():
            keys.extend(_walk_json_keys(item))
        return keys
    if isinstance(value, list):
        return [key for item in value for key in _walk_json_keys(item)]
    return []


class TestMachineValuesCarryNoTheme:
    def test_every_enum_value_is_plain(self) -> None:
        offenders: list[str] = []
        for module_name in _package_modules():
            module = importlib.import_module(module_name)
            for attr, value in vars(module).items():
                if attr.startswith("_") or not isinstance(value, type):
                    continue
                if not issubclass(value, enum.Enum) or not value.__module__.startswith("blastoise"):
                    continue
                for member in value:
                    hits = _themed_hits(str(member.value))
                    if hits:
                        offenders.append(
                            f"{value.__qualname__}.{member.name}={member.value!r} {hits}"
                        )
        assert offenders == []

    def test_every_dataclass_field_name_is_plain(self) -> None:
        offenders: list[str] = []
        for module_name in _package_modules():
            module = importlib.import_module(module_name)
            for attr, value in vars(module).items():
                if attr.startswith("_") or not isinstance(value, type):
                    continue
                if not dataclasses.is_dataclass(value):
                    continue
                if not value.__module__.startswith("blastoise"):
                    continue
                for field in dataclasses.fields(value):
                    hits = _themed_hits(field.name)
                    if hits:
                        offenders.append(f"{value.__qualname__}.{field.name} {hits}")
        assert offenders == []

    def test_snapshot_json_keys_are_plain(self) -> None:
        snap = snapshot(relations=(relation("users", rows=1000),))
        payload = json.loads(snap.to_canonical_json())
        offenders = [key for key in _walk_json_keys(payload) if _themed_hits(key)]
        assert offenders == []

    def test_cli_json_keys_are_plain(self, tmp_path: Path) -> None:
        sql = tmp_path / "m.sql"
        sql.write_text(
            "CREATE TABLE t (id int);\nALTER TABLE users ADD COLUMN e text;\n",
            encoding="utf8",
        )
        run = subprocess.run(
            [sys.executable, "-m", "blastoise.cli", "parse", "--json", str(sql)],
            capture_output=True,
            text=True,
            check=True,
        )
        payload = json.loads(run.stdout)
        offenders = [key for key in _walk_json_keys(payload) if _themed_hits(key)]
        assert offenders == []

    def test_statement_kind_values_of_a_real_file_are_plain(self) -> None:
        script = parse_migration(
            "CREATE INDEX CONCURRENTLY i ON users (email);\n"
            "ALTER TABLE users ADD CONSTRAINT c CHECK (id > 0) NOT VALID;\n"
        )
        for statement in script.statements:
            assert _themed_hits(statement.kind.value) == []


class TestPressureLevels:
    """The one sanctioned bridge from machine value to display name."""

    def test_classification_values_are_pinned(self) -> None:
        # This is the contract other people's CI reads. Changing any string
        # here is a breaking change, not a copy edit.
        assert {member.name: member.value for member in Classification} == {
            "SAFE": "safe",
            "SAFE_IRREVERSIBLE": "safe_irreversible",
            "NEEDS_TIMING": "needs_timing",
            "UNSAFE": "unsafe",
            "UNKNOWN": "unknown",
        }

    def test_every_tier_has_a_display_name(self) -> None:
        assert set(PRESSURE_LEVELS) == set(Classification)
        assert [pressure_level(member) for member in Classification] == [
            "Calm Water",
            "One-Way Current",
            "Rain Check",
            "Hydro Pump",
            "Fog",
        ]

    def test_display_names_are_not_machine_values(self) -> None:
        # A display name must never be mistakable for the thing it labels:
        # no round-trip, no accidental use as a key.
        machine = {member.value for member in Classification}
        for display in PRESSURE_LEVELS.values():
            assert display not in machine
            assert display.lower().replace(" ", "_") not in machine

    def test_display_names_live_only_in_the_lookup(self) -> None:
        # Every themed tier name must be reachable only through
        # PRESSURE_LEVELS -- never baked into a member value.
        for member, display in PRESSURE_LEVELS.items():
            assert display.lower() not in member.value

    @pytest.mark.parametrize("member", list(Classification))
    def test_str_of_a_tier_is_the_machine_value(self, member: Classification) -> None:
        # StrEnum: formatting a tier anywhere gives the machine value, so a
        # careless f-string in reporting code cannot leak the theme, and a
        # careless one in machine output cannot leak the display name.
        assert f"{member}" == member.value
