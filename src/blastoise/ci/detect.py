"""Which of a pull request's changed files are migrations.

This is the part that decides whether the CI integration works without
being configured. It matches paths against the layouts the major migration
frameworks impose, and it records *which* framework matched, because that
decides whether the file can be assessed at all: Prisma, Flyway and
golang-migrate write SQL, while Rails, Django and Alembic write a DSL that
the parser cannot read.

Detection is over paths, never over file contents. A pull request's changed
files are a list of strings; reading them to sniff their type would mean the
tool behaves differently depending on whether a file survived the diff, and
a renamed-away migration would classify by whatever replaced it.

The rules are ordered most-specific-first and the first match wins, so
``prisma/migrations/20240101/migration.sql`` is Prisma rather than the
generic ``migrations/`` catch-all, and ``migrations/0001_init.up.sql`` is
golang-migrate rather than generic. Ordering, not scoring: a scored match
would be one more thing to explain when it surprises someone.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

__all__ = [
    "DetectedFile",
    "Framework",
    "SourceKind",
    "compile_globs",
    "detect_migrations",
    "framework_of",
    "glob_to_regex",
    "normalize_path",
]


class Framework(StrEnum):
    """The migration tool whose layout a path matched."""

    PRISMA = "prisma"
    RAILS = "rails"
    DJANGO = "django"
    ALEMBIC = "alembic"
    FLYWAY = "flyway"
    GOLANG_MIGRATE = "golang_migrate"
    GENERIC = "generic"
    CONFIGURED = "configured"


class SourceKind(StrEnum):
    """Whether the file holds SQL the parser can read.

    ``SQL`` -- plain DDL, assessable today.
    ``DSL`` -- a Ruby/Python migration whose SQL only exists once the
    framework renders it. Recognized, reported, and not assessed.
    """

    SQL = "sql"
    DSL = "dsl"


# Display names for the message a DSL file produces. Machine values stay in
# the enum above; this is prose, so it says what the reader needs to do.
FRAMEWORK_NAMES: dict[Framework, str] = {
    Framework.PRISMA: "Prisma",
    Framework.RAILS: "Rails",
    Framework.DJANGO: "Django",
    Framework.ALEMBIC: "Alembic",
    Framework.FLYWAY: "Flyway",
    Framework.GOLANG_MIGRATE: "golang-migrate",
    Framework.GENERIC: "plain SQL migrations directory",
    Framework.CONFIGURED: "configured path",
}

# What it would take to render each DSL framework's SQL. Recorded here
# rather than only in DECISIONS.md so the message a user sees names the
# command that would fix it.
DSL_ADAPTER_HINT: dict[Framework, str] = {
    Framework.RAILS: (
        "rails db:migrate emits the SQL it runs, but only while running it; "
        "an adapter would need a dry-run that renders without applying"
    ),
    Framework.DJANGO: (
        "django-admin sqlmigrate <app> <name> prints the exact SQL, and needs "
        "the project's settings and an importable app registry"
    ),
    Framework.ALEMBIC: (
        "alembic upgrade <rev> --sql prints the SQL offline, and needs the "
        "project's alembic.ini and env.py"
    ),
}


@dataclass(frozen=True, slots=True)
class DetectedFile:
    """One changed path that is a migration, and what kind."""

    path: str
    framework: Framework
    source_kind: SourceKind

    @property
    def assessable(self) -> bool:
        return self.source_kind is SourceKind.SQL


@dataclass(frozen=True, slots=True)
class _Rule:
    framework: Framework
    source_kind: SourceKind
    pattern: re.Pattern[str]


def _rule(framework: Framework, source_kind: SourceKind, pattern: str) -> _Rule:
    return _Rule(framework, source_kind, re.compile(pattern))


# Ordered most-specific-first; first match wins.
#
# Every pattern is anchored with ``(?:^|/)`` so it matches at any depth: a
# monorepo keeps its Rails app in ``services/billing/db/migrate/`` and the
# layout inside the app is unchanged.
_RULES: tuple[_Rule, ...] = (
    # Prisma: one directory per migration, always this file name.
    _rule(
        Framework.PRISMA,
        SourceKind.SQL,
        r"(?:^|/)prisma/migrations/[^/]+/migration\.sql$",
    ),
    # Rails: db/migrate/, and db/<name>/migrate/ for multi-database setups.
    _rule(
        Framework.RAILS,
        SourceKind.DSL,
        r"(?:^|/)db/(?:[^/]+/)?migrate/[^/]+\.rb$",
    ),
    # Alembic: a versions/ directory, whatever its parent is called
    # (alembic/, migrations/, db/migrations/ are all in the wild). Checked
    # before Django because Django's rule matches .py directly inside
    # migrations/, which never collides, but the intent is clearer stated.
    _rule(
        Framework.ALEMBIC,
        SourceKind.DSL,
        r"(?:^|/)versions/(?!__init__\.py$)[^/]+\.py$",
    ),
    # Django: <app>/migrations/0001_initial.py. __init__.py is package
    # scaffolding, never a migration.
    _rule(
        Framework.DJANGO,
        SourceKind.DSL,
        r"(?:^|/)migrations/(?!__init__\.py$)[^/]+\.py$",
    ),
    # Flyway: V<version>__<description>.sql, U for undo, R for repeatable.
    # Matched by file name at any depth, because Flyway's location is
    # configurable and most projects move it. The version must start with a
    # digit, which is what keeps this from matching Views__old.sql.
    _rule(
        Framework.FLYWAY,
        SourceKind.SQL,
        r"(?:^|/)[VU][0-9][0-9._]*__[^/]*\.sql$",
    ),
    _rule(Framework.FLYWAY, SourceKind.SQL, r"(?:^|/)R__[^/]+\.sql$"),
    # golang-migrate: <version>_<name>.up.sql / .down.sql. Both directions
    # are migrations; a down file is SQL that will run on a rollback.
    _rule(
        Framework.GOLANG_MIGRATE,
        SourceKind.SQL,
        r"(?:^|/)[^/]+\.(?:up|down)\.sql$",
    ),
    # The plain case: a migrations/ (or migration/) directory of .sql files,
    # at any depth and with any nesting under it.
    _rule(
        Framework.GENERIC,
        SourceKind.SQL,
        r"(?:^|/)migrations?/(?:[^/]+/)*[^/]+\.sql$",
    ),
)

_EXTENSION_KIND: dict[str, SourceKind] = {
    ".sql": SourceKind.SQL,
    ".rb": SourceKind.DSL,
    ".py": SourceKind.DSL,
    ".ex": SourceKind.DSL,
    ".exs": SourceKind.DSL,
    ".ts": SourceKind.DSL,
    ".js": SourceKind.DSL,
    ".go": SourceKind.DSL,
}


def normalize_path(path: str) -> str:
    """Repo-relative POSIX form: backslashes flipped, ``./`` prefixes gone.

    Changed-file lists arrive from git, from the GitHub API, and from a file
    someone wrote by hand on Windows. They are compared against patterns
    written in one form, so they are normalized into it first.
    """
    text = path.strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text.lstrip("/")


def framework_of(path: str) -> tuple[Framework, SourceKind] | None:
    """The framework whose layout ``path`` matches, or ``None``."""
    normalized = normalize_path(path)
    if not normalized:
        return None
    for rule in _RULES:
        if rule.pattern.search(normalized):
            return rule.framework, rule.source_kind
    return None


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Translate a path glob to a regex over POSIX-normalized paths.

    ``fnmatch`` is not usable here: its ``*`` crosses ``/``, so
    ``migrations/*.sql`` would match ``migrations/2024/01.sql`` and
    ``*.sql`` would match every SQL file in the tree. This implements the
    conventional path-glob semantics instead -- ``*`` within one segment,
    ``**`` across segments, ``?`` one character, ``[...]`` a class -- which
    is what a team writing ``paths:`` in a config file will expect.
    """
    normalized = normalize_path(pattern)
    parts: list[str] = []
    index = 0
    length = len(normalized)
    while index < length:
        char = normalized[index]
        if char == "*":
            if normalized.startswith("**", index):
                index += 2
                # A trailing or separator-flanked ** spans whole segments,
                # including none at all: a/**/b matches a/b.
                if normalized.startswith("/", index):
                    index += 1
                    parts.append(r"(?:[^/]+/)*")
                else:
                    parts.append(r".*")
            else:
                index += 1
                parts.append(r"[^/]*")
        elif char == "?":
            index += 1
            parts.append(r"[^/]")
        elif char == "[":
            close = normalized.find("]", index + 1)
            if close == -1:
                index += 1
                parts.append(re.escape("["))
            else:
                body = normalized[index + 1 : close]
                index = close + 1
                if body.startswith(("!", "^")):
                    body = "^" + body[1:]
                parts.append(f"[{body}]")
        else:
            index += 1
            parts.append(re.escape(char))
    return re.compile("^" + "".join(parts) + "$")


def compile_globs(patterns: tuple[str, ...]) -> tuple[re.Pattern[str], ...]:
    return tuple(glob_to_regex(pattern) for pattern in patterns)


def _matches_any(path: str, patterns: tuple[re.Pattern[str], ...]) -> bool:
    return any(pattern.match(path) for pattern in patterns)


def detect_migrations(
    paths: tuple[str, ...],
    *,
    include: tuple[str, ...] = (),
    exclude: tuple[str, ...] = (),
) -> tuple[DetectedFile, ...]:
    """The migrations among ``paths``, deduplicated and sorted by path.

    ``include`` **overrides detection entirely**: when it is non-empty, a
    path is a migration if and only if it matches one of those globs. That
    is the escape hatch for a team whose layout no convention describes, and
    it has to be total -- a config that only added to the conventions would
    leave them unable to turn a false positive off.

    ``exclude`` applies either way, and is checked last.
    """
    include_patterns = compile_globs(include)
    exclude_patterns = compile_globs(exclude)
    seen: dict[str, DetectedFile] = {}
    for raw in paths:
        path = normalize_path(raw)
        if not path or path in seen:
            continue
        matched = framework_of(path)
        if include_patterns:
            if not _matches_any(path, include_patterns):
                continue
            # An explicitly configured path still reports the framework its
            # layout matches, when one does: the team overrode *which* files
            # are checked, not what a Rails migration is.
            framework, source_kind = (
                matched if matched is not None else _configured_kind(path)
            )
        else:
            if matched is None:
                continue
            framework, source_kind = matched
        if _matches_any(path, exclude_patterns):
            continue
        seen[path] = DetectedFile(path=path, framework=framework, source_kind=source_kind)
    return tuple(seen[path] for path in sorted(seen))


def _configured_kind(path: str) -> tuple[Framework, SourceKind]:
    """Framework and kind for a configured path no convention describes."""
    suffix = path[path.rfind(".") :].lower() if "." in path.rsplit("/", 1)[-1] else ""
    return Framework.CONFIGURED, _EXTENSION_KIND.get(suffix, SourceKind.SQL)
