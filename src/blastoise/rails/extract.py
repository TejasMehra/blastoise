"""Render a Rails migration's SQL by running it against a throwaway database.

The extraction itself lives in ``harness.rb``; this decides whether it is
allowed to run at all, assembles what it needs, and turns what comes back
into SQL the ordinary parser can read.

Three things here are refusals rather than options, because each of them
turns a safe tool into an unsafe one and none of them is a judgement call a
run should be making on its own:

* **Extraction is opt-in.** Running a migration means executing Ruby that
  came from the pull request. Every other part of Blastoise only ever
  *reads* the branch. A repository that has not asked for this in
  ``.blastoise.yml`` keeps the honest "not supported" message instead.

* **Never under ``pull_request_target``.** That event runs with the base
  repository's secrets and a writable token, against code from the fork. It
  is the one context where executing a contributor's Ruby hands them the
  repository, and no configuration turns it back on.

* **Never on the server being assessed.** Extraction creates and drops
  databases. If the scratch server is the same host and port as the
  database under assessment, that is close enough to production to refuse.

The pre-state comes from the *base* commit, not the branch, and the reason
is a real failure mode rather than caution: Rails regenerates ``schema.rb``
when a developer runs the migration locally, and they commit it. Loading
the branch's schema and then migrating would apply a change the schema
already contains, and the run would die on "column already exists" for
every migration that had ever been run by its author.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

__all__ = [
    "HARNESS",
    "RailsExtraction",
    "RailsExtractionError",
    "extract_rails_sql",
    "rails_refusal",
]

HARNESS = Path(__file__).with_name("harness.rb")

# A migration whose pre-state has to be rebuilt by replaying this many
# earlier migrations is not being assessed, it is being re-run. Mature
# Rails apps have thousands; past this the honest answer is that the
# committed schema file is the supported path.
REPLAY_LIMIT = 200

_VERSION = re.compile(r"^(\d+)_")


class RailsExtractionError(Exception):
    """Extraction did not produce SQL that can be trusted.

    Always carries a reason fit to show a reviewer, because the fallback is
    to tell them this file was not assessed and why -- never to guess at
    the SQL and issue a verdict on the guess.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class RailsExtraction:
    """The SQL a migration ran, and how it was obtained."""

    sql: str
    statements: tuple[str, ...]
    rails_version: str | None
    migration_class: str | None
    disable_ddl_transaction: bool
    schema_source: str
    strong_migrations: bool


# --------------------------------------------------------------------------
# refusals
# --------------------------------------------------------------------------


def _same_server(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    try:
        a, b = urlsplit(left), urlsplit(right)
    except ValueError:
        return False
    return (a.hostname, a.port or 5432) == (b.hostname, b.port or 5432)


def rails_refusal(
    *,
    enabled: bool,
    scratch_url: str | None,
    assessed_url: str | None,
    environ: dict[str, str],
    scratch_url_env: str,
) -> str | None:
    """Why extraction must not run, or ``None`` if it may.

    Checked once per run rather than per file: every reason here is a
    property of the environment, and reporting it once is what makes it
    read as a setting rather than as a fault in the migration.
    """
    if not enabled:
        return (
            "Rails SQL extraction is off. It runs the migration to see what "
            "SQL it emits, which means executing Ruby from the pull request, "
            "so it is opt-in: set 'rails.extract: true' in .blastoise.yml"
        )
    event = environ.get("GITHUB_EVENT_NAME", "")
    if event == "pull_request_target":
        return (
            "refusing to extract under pull_request_target: that event runs "
            "with the base repository's secrets and a writable token, and "
            "extraction would execute the fork's Ruby with them. Use a "
            "'pull_request' workflow for migration checks"
        )
    if not scratch_url:
        return (
            f"no scratch database in ${scratch_url_env}. Extraction creates "
            "and drops a throwaway database; point this at a disposable "
            "Postgres in your CI job, never at a real one"
        )
    if _same_server(scratch_url, assessed_url):
        return (
            "refusing to extract: the scratch database is on the same server "
            "as the database being assessed. Extraction creates and drops "
            "databases, so it needs a server nothing depends on"
        )
    return None


# --------------------------------------------------------------------------
# what the migration expects to find
# --------------------------------------------------------------------------


def _app_root(repo_root: Path, migration: str) -> tuple[Path, str]:
    """The Rails app directory containing ``db/migrate/...``, repo-relative."""
    parts = migration.split("/")
    try:
        index = len(parts) - 1 - parts[::-1].index("db")
    except ValueError as exc:  # pragma: no cover - detection guarantees db/
        raise RailsExtractionError(f"{migration} is not under a db/ directory") from exc
    prefix = "/".join(parts[:index])
    return (repo_root / prefix if prefix else repo_root), prefix


def _git_show(repo_root: Path, ref: str, path: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "show", f"{ref}:{path}"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def _schema_candidates(db_prefix: str) -> tuple[tuple[str, str], ...]:
    """(path, kind) pairs, most preferred first.

    ``structure.sql`` is preferred over ``schema.rb`` when a project has
    both: it is what the project chose when its schema outgrew the Ruby
    dumper, and it carries the constraints and extensions ``schema.rb``
    silently drops.
    """
    prefix = f"{db_prefix}/" if db_prefix else ""
    return (
        (f"{prefix}db/structure.sql", "sql"),
        (f"{prefix}db/schema.rb", "ruby"),
    )


def _resolve_schema(
    repo_root: Path, db_prefix: str, base_ref: str | None, workdir: Path
) -> tuple[Path | None, str | None, str]:
    """The pre-migration schema, written to ``workdir``.

    Returns ``(file, kind, description)``. ``(None, None, ...)`` means no
    committed schema file was found and the caller should replay instead.
    """
    for path, kind in _schema_candidates(db_prefix):
        text: str | None = None
        source = ""
        if base_ref:
            text = _git_show(repo_root, base_ref, path)
            if text is not None:
                source = f"{path} at {base_ref}"
        if text is None:
            candidate = repo_root / path
            if candidate.is_file():
                text = candidate.read_text(encoding="utf-8", errors="replace")
                source = f"{path} (working tree)"
        if text is None or not text.strip():
            continue
        target = workdir / Path(path).name
        target.write_text(text, encoding="utf-8")
        return target, kind, source
    return None, None, ""


def _replay_set(repo_root: Path, migration: str) -> tuple[str, ...]:
    """Earlier migrations in the same directory, oldest first."""
    directory = (repo_root / migration).parent
    target = _VERSION.match(Path(migration).name)
    if not directory.is_dir() or target is None:
        return ()
    ceiling = target.group(1)
    earlier = []
    for entry in sorted(directory.iterdir()):
        if entry.suffix != ".rb" or entry.name == Path(migration).name:
            continue
        match = _VERSION.match(entry.name)
        if match and match.group(1) < ceiling:
            earlier.append(str(entry))
    return tuple(earlier)


# --------------------------------------------------------------------------
# running the harness
# --------------------------------------------------------------------------


def _admin_url(scratch_url: str) -> str:
    parts = urlsplit(scratch_url)
    return urlunsplit((parts.scheme, parts.netloc, "/postgres", parts.query, parts.fragment))


def _scratch_url(scratch_url: str, dbname: str) -> str:
    parts = urlsplit(scratch_url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{dbname}", parts.query, parts.fragment))


def _ruby_command(app_root: Path, ruby: str | None) -> list[str]:
    """``bundle exec ruby`` when the app has a bundle, else plain ruby.

    The bundle is not a nicety. A migration declaring
    ``ActiveRecord::Migration[8.1]`` raises "Unknown migration version" on
    any older ActiveRecord, and ``safety_assured`` is a method that only
    exists if the app's strong_migrations is loaded -- so the Rails that
    renders the SQL has to be the app's own, not one this tool chose.
    """
    if ruby:
        return [ruby]
    if (app_root / "Gemfile").is_file() and shutil.which("bundle"):
        return ["bundle", "exec", "ruby"]
    found = shutil.which("ruby")
    if not found:
        raise RailsExtractionError(
            "no ruby on PATH: extraction runs the migration with the app's "
            "own Rails, so the CI job needs Ruby and a completed bundle install"
        )
    return [found]


def extract_rails_sql(
    migration: str,
    *,
    repo_root: Path,
    scratch_url: str,
    base_ref: str | None = None,
    ruby: str | None = None,
    timeout: int = 300,
    replay_limit: int = REPLAY_LIMIT,
    preceding: tuple[str, ...] = (),
    environ: dict[str, str] | None = None,
) -> RailsExtraction:
    """The SQL ``migration`` runs, or :class:`RailsExtractionError`.

    ``preceding`` are migrations from the *same change* that run before this
    one. A pull request that adds a column in one migration and indexes it
    in the next is ordinary Rails, and assessing the second against the
    base commit's schema alone would fail on a column that the branch
    creates. They are applied without being recorded: each file's report is
    about that file.
    """
    app_root, db_prefix = _app_root(repo_root, migration)
    command = _ruby_command(app_root, ruby)

    with tempfile.TemporaryDirectory(prefix="blastoise-rails-") as tmp:
        workdir = Path(tmp)
        schema_file, schema_kind, schema_source = _resolve_schema(
            repo_root, db_prefix, base_ref, workdir
        )

        replay: tuple[str, ...] = ()
        if schema_file is None:
            replay = _replay_set(repo_root, migration)
            if not replay:
                raise RailsExtractionError(
                    "no committed db/schema.rb or db/structure.sql to build "
                    "the pre-migration state from, and no earlier migrations "
                    "to replay"
                )
            if len(replay) > replay_limit:
                raise RailsExtractionError(
                    "no committed schema file, and rebuilding the state would "
                    f"mean replaying {len(replay)} earlier migrations (limit "
                    f"{replay_limit}). Commit db/schema.rb or db/structure.sql "
                    "to make this assessable"
                )
            schema_source = f"replayed {len(replay)} earlier migration(s)"

        env = dict(os.environ if environ is None else environ)
        env.setdefault("RAILS_ENV", "test")
        branch = tuple(str(repo_root / path) for path in preceding)

        def attempt(
            schema: Path | None, kind: str | None, history: tuple[str, ...]
        ) -> dict[str, Any]:
            dbname = f"blastoise_rails_{secrets.token_hex(6)}"
            request = {
                "admin_url": _admin_url(scratch_url),
                "scratch_url": _scratch_url(scratch_url, dbname),
                "scratch_dbname": dbname,
                "schema_file": str(schema) if schema else None,
                "schema_kind": kind,
                # History first, then the branch's own earlier migrations:
                # the order they would really run in.
                "replay": [*history, *branch],
                "migration": str(repo_root / migration),
                "drop_when_done": True,
            }
            request_path = workdir / "request.json"
            response_path = workdir / "response.json"
            response_path.unlink(missing_ok=True)
            request_path.write_text(json.dumps(request), encoding="utf-8")
            try:
                completed = subprocess.run(
                    [*command, str(HARNESS), str(request_path), str(response_path)],
                    cwd=app_root,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=timeout,
                    env=env,
                )
            except subprocess.TimeoutExpired as exc:
                raise RailsExtractionError(
                    f"the migration did not finish within {timeout}s"
                ) from exc
            except OSError as exc:
                raise RailsExtractionError(f"could not run ruby ({exc})") from exc

            if not response_path.is_file():
                detail = (completed.stderr or completed.stdout or "").strip().splitlines()
                tail = detail[-1] if detail else f"ruby exited {completed.returncode}"
                raise RailsExtractionError(f"the extraction harness did not run ({tail})")
            try:
                loaded = json.loads(response_path.read_text(encoding="utf-8"))
            except ValueError as exc:
                raise RailsExtractionError(
                    "the extraction harness produced no result"
                ) from exc
            return dict(loaded)

        payload = attempt(schema_file, schema_kind, replay)

        # A committed schema file that will not load is not the end of it.
        # `db/schema.rb` cannot express a function, a trigger or a custom
        # type, so an application that creates one in a migration and then
        # uses it as a column default has a schema file that fails on its
        # own. Replaying the migrations builds the same state the long way.
        if not payload.get("ok") and payload.get("stage") == "schema" and schema_file:
            history = _replay_set(repo_root, migration)
            if history and len(history) <= replay_limit:
                schema_source = (
                    f"{schema_source} would not load "
                    f"({payload.get('error', 'unknown error')}); "
                    f"replayed {len(history)} earlier migration(s) instead"
                )
                payload = attempt(None, None, history)
            elif history:
                raise RailsExtractionError(
                    f"the committed schema file would not load "
                    f"({payload.get('error', 'unknown error')}), and rebuilding "
                    f"the state would mean replaying {len(history)} earlier "
                    f"migrations (limit {replay_limit})"
                )

    if not payload.get("ok"):
        error = payload.get("error") or "the migration did not run"
        raise RailsExtractionError(f"{payload.get('error_class', 'error')}: {error}")

    statements = tuple(
        entry["sql"].strip() for entry in payload.get("statements", []) if entry.get("sql")
    )
    if not statements:
        raise RailsExtractionError(
            "the migration ran but emitted no SQL (nothing to assess)"
        )

    sql = ";\n".join(statement.rstrip(";") for statement in statements) + ";\n"
    return RailsExtraction(
        sql=sql,
        statements=statements,
        rails_version=payload.get("rails_version"),
        migration_class=payload.get("migration_class"),
        disable_ddl_transaction=bool(payload.get("disable_ddl_transaction")),
        schema_source=schema_source,
        strong_migrations=bool(payload.get("strong_migrations")),
    )
