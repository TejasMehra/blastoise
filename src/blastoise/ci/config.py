"""``.blastoise.yml`` -- the config for teams whose layout no convention describes.

Everything here has a working default, so the file is optional and the
common case is no file at all. What it exists for is the two things
detection cannot guess: where a team keeps its migrations when they are not
where any framework puts them, and which environment variable holds the
connection string.

Validation is strict. An unknown key is an error, not a warning, because the
failure mode of a silently ignored key is a team believing their exclusion
list is in force when it is not -- and the whole file is optional, so the
only way to have one is to have written it on purpose.

One key is refused rather than merely unknown: a connection string spelled
into the config. ``url_env`` names an environment variable; a ``url`` would
be a credential in a file that gets committed, and the error says so.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

__all__ = [
    "CONFIG_FILENAME",
    "DEFAULT_DATABASE_URL_ENV",
    "DEFAULT_SCRATCH_URL_ENV",
    "CiConfig",
    "ConfigError",
    "FailOn",
    "RailsConfig",
    "load_config",
    "parse_config",
]

CONFIG_FILENAME = ".blastoise.yml"
CONFIG_FILENAMES = (".blastoise.yml", ".blastoise.yaml")
DEFAULT_DATABASE_URL_ENV = "BLASTOISE_DATABASE_URL"
DEFAULT_SCRATCH_URL_ENV = "BLASTOISE_SCRATCH_DATABASE_URL"
CONFIG_VERSION = 1


class ConfigError(Exception):
    """The config file exists and is wrong. Never silently tolerated."""


class FailOn(StrEnum):
    """How far the verdict has to go before the CI step itself fails.

    The verdict, the comment and the check status do not depend on this: it
    decides only the process exit code, which is the crude signal that turns
    a pull request red.
    """

    BLOCK = "block"
    REQUIRES_APPROVAL = "requires_approval"
    NEVER = "never"


@dataclass(frozen=True, slots=True)
class RailsConfig:
    """Whether, and how, to render a Rails migration's SQL by running it.

    ``extract`` defaults to off, and that default is a security boundary
    rather than caution. Every other part of Blastoise reads the branch;
    extraction *executes* it, because the SQL a Rails migration runs does
    not exist until ActiveRecord renders it. A repository has to say so.
    """

    extract: bool = False
    scratch_url_env: str = DEFAULT_SCRATCH_URL_ENV
    """Name of the environment variable holding the throwaway database's
    connection string. A name, never a value, for the same reason
    ``database.url_env`` is."""

    timeout: int = 300
    ruby: str | None = None
    """Interpreter to run the harness with. Left unset, the app's own
    ``bundle exec ruby`` is used, which is what makes a migration declaring
    a newer schema version than this tool knows about still render."""


@dataclass(frozen=True, slots=True)
class CiConfig:
    """Resolved configuration. Every field has a default that works."""

    paths: tuple[str, ...] = ()
    """Globs that override detection entirely when non-empty."""

    exclude: tuple[str, ...] = ()
    """Globs removed from the result, whether detected or configured."""

    database_url_env: str = DEFAULT_DATABASE_URL_ENV
    """Name of the environment variable holding the connection string.
    A name, never a value: see the module docstring."""

    database_label: str | None = None
    """Human label for the database that was read, shown in the comment
    ('staging', 'staging-replica'). Free text chosen by the team, so it can
    say which environment answered without naming a host."""

    pg_version: int | None = None
    fail_on: FailOn = FailOn.BLOCK
    comment: bool = True
    check_run: bool = True
    rails: RailsConfig = field(default_factory=RailsConfig)
    source: str | None = field(default=None, compare=False)
    """Where this config came from, for the message that reports it."""

    @property
    def overrides_detection(self) -> bool:
        return bool(self.paths)


_TOP_LEVEL = frozenset({"version", "migrations", "database", "ci", "rails"})
_MIGRATIONS_KEYS = frozenset({"paths", "exclude"})
_DATABASE_KEYS = frozenset({"url_env", "label", "pg_version"})
_CI_KEYS = frozenset({"fail_on", "comment", "check_run"})
_RAILS_KEYS = frozenset({"extract", "scratch_url_env", "timeout", "ruby"})

_REFUSED = {
    ("database", "url"): (
        "database.url would put a connection string in a committed file. "
        "Set database.url_env to the NAME of an environment variable and "
        "supply the value from your CI secret store"
    ),
    ("database", "password"): (
        "database.password would put a credential in a committed file; "
        "supply the whole connection string through database.url_env"
    ),
    ("rails", "scratch_url"): (
        "rails.scratch_url would put a connection string in a committed "
        "file. Set rails.scratch_url_env to the NAME of an environment "
        "variable and supply the value from your CI job"
    ),
    ("database", "dsn"): (
        "database.dsn would put a connection string in a committed file. "
        "Set database.url_env to the NAME of an environment variable"
    ),
}


def _section(
    data: dict[str, Any], name: str, allowed: frozenset[str], where: str
) -> dict[str, Any]:
    value = data.get(name)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{where}: '{name}' must be a mapping, not {type(value).__name__}")
    for key in value:
        if (name, key) in _REFUSED:
            raise ConfigError(f"{where}: {_REFUSED[(name, key)]}")
        if key not in allowed:
            raise ConfigError(
                f"{where}: unknown key '{name}.{key}' "
                f"(known: {', '.join(sorted(allowed))})"
            )
    return value


def _string_list(value: Any, key: str, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        raise ConfigError(f"{where}: '{key}' must be a list of globs, not a single string")
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"{where}: '{key}' must be a list of strings")
    entries = tuple(item.strip() for item in value if item.strip())
    return entries


def _bool(value: Any, key: str, where: str, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ConfigError(f"{where}: '{key}' must be true or false, not {value!r}")
    return value


def parse_config(data: Any, *, where: str) -> CiConfig:
    """Validate a parsed YAML document into a :class:`CiConfig`."""
    if data is None:
        return CiConfig(source=where)
    if not isinstance(data, dict):
        raise ConfigError(f"{where}: the config must be a mapping, not {type(data).__name__}")

    for key in data:
        if key not in _TOP_LEVEL:
            raise ConfigError(
                f"{where}: unknown key '{key}' (known: {', '.join(sorted(_TOP_LEVEL))})"
            )

    version = data.get("version", CONFIG_VERSION)
    if not isinstance(version, int) or isinstance(version, bool):
        raise ConfigError(f"{where}: 'version' must be an integer")
    if version != CONFIG_VERSION:
        raise ConfigError(
            f"{where}: config version {version} is not supported by this "
            f"release (expected {CONFIG_VERSION})"
        )

    migrations = _section(data, "migrations", _MIGRATIONS_KEYS, where)
    database = _section(data, "database", _DATABASE_KEYS, where)
    ci_section = _section(data, "ci", _CI_KEYS, where)
    rails_section = _section(data, "rails", _RAILS_KEYS, where)

    url_env = database.get("url_env", DEFAULT_DATABASE_URL_ENV)
    if not isinstance(url_env, str) or not url_env.strip():
        raise ConfigError(f"{where}: 'database.url_env' must be a non-empty string")
    url_env = url_env.strip()
    _refuse_value_as_name(url_env, where)

    label = database.get("label")
    if label is not None and not isinstance(label, str):
        raise ConfigError(f"{where}: 'database.label' must be a string")

    pg_version = database.get("pg_version")
    if pg_version is not None and (not isinstance(pg_version, int) or isinstance(pg_version, bool)):
        raise ConfigError(f"{where}: 'database.pg_version' must be an integer")

    fail_on_raw = ci_section.get("fail_on", FailOn.BLOCK.value)
    if not isinstance(fail_on_raw, str):
        raise ConfigError(f"{where}: 'ci.fail_on' must be a string")
    try:
        fail_on = FailOn(fail_on_raw.strip().lower())
    except ValueError as exc:
        known = ", ".join(member.value for member in FailOn)
        raise ConfigError(
            f"{where}: 'ci.fail_on' must be one of {known}, not {fail_on_raw!r}"
        ) from exc

    scratch_env = rails_section.get("scratch_url_env", DEFAULT_SCRATCH_URL_ENV)
    if not isinstance(scratch_env, str) or not scratch_env.strip():
        raise ConfigError(f"{where}: 'rails.scratch_url_env' must be a non-empty string")
    scratch_env = scratch_env.strip()
    if "://" in scratch_env or " " in scratch_env or "=" in scratch_env:
        raise ConfigError(
            f"{where}: 'rails.scratch_url_env' takes the NAME of an "
            "environment variable, not a connection string"
        )

    timeout = rails_section.get("timeout", 300)
    if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
        raise ConfigError(f"{where}: 'rails.timeout' must be a positive integer (seconds)")

    ruby = rails_section.get("ruby")
    if ruby is not None and (not isinstance(ruby, str) or not ruby.strip()):
        raise ConfigError(f"{where}: 'rails.ruby' must be a non-empty string")

    rails = RailsConfig(
        extract=_bool(rails_section.get("extract"), "rails.extract", where, False),
        scratch_url_env=scratch_env,
        timeout=timeout,
        ruby=ruby.strip() if isinstance(ruby, str) else None,
    )

    return CiConfig(
        paths=_string_list(migrations.get("paths"), "migrations.paths", where),
        exclude=_string_list(migrations.get("exclude"), "migrations.exclude", where),
        database_url_env=url_env,
        database_label=label,
        pg_version=pg_version,
        fail_on=fail_on,
        comment=_bool(ci_section.get("comment"), "ci.comment", where, True),
        check_run=_bool(ci_section.get("check_run"), "ci.check_run", where, True),
        rails=rails,
        source=where,
    )


def _refuse_value_as_name(name: str, where: str) -> None:
    """Catch a connection string written where the variable NAME belongs.

    The mistake is easy to make and the consequence is a credential in the
    repository, so it is refused with the reason rather than treated as a
    variable that happens not to exist.
    """
    if "://" in name or " " in name or "=" in name:
        raise ConfigError(
            f"{where}: 'database.url_env' takes the NAME of an environment "
            "variable, not a connection string. The value must come from "
            "your CI secret store, never from a file or a workflow input"
        )


def find_config(root: Path) -> Path | None:
    for name in CONFIG_FILENAMES:
        candidate = root / name
        if candidate.is_file():
            return candidate
    return None


def load_config(path: Path | None, *, root: Path | None = None) -> CiConfig:
    """Load a config file, or return defaults when there is none.

    ``path`` explicitly named and missing is an error; the file merely not
    existing at its conventional location is not.
    """
    if path is None:
        found = find_config(root if root is not None else Path())
        if found is None:
            return CiConfig(source=None)
        path = found
    elif not path.is_file():
        raise ConfigError(f"{path}: no such config file")

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"{path}: cannot read config ({exc.strerror or exc})") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: not valid YAML ({exc})") from exc
    return parse_config(data, where=str(path))
