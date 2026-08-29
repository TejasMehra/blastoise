"""``blastoise ci``: assess a pull request's migrations and report on it.

The shape of a run:

1. resolve the config (``.blastoise.yml``, optional, strict when present);
2. work out which files the change touched -- from the GitHub API, from
   ``git diff``, or from a list handed in, in that order of preference;
3. detect which of them are migrations, and by which framework's layout;
4. parse them all, capture **one** live snapshot covering every relation
   any of them touches, and assess each against it;
5. write a Shell Report and evidence bundle per file;
6. render the comment, post or update it, set the check status, write the
   job summary, and exit.

Two properties are load-bearing enough to state here.

**The connection string comes from the environment and nothing else.**
There is no ``--database-url`` on this command and the Action declares no
input for it. The config names an environment *variable*; the value is
supplied by the CI secret store. A workflow input would be visible in the
run's logs, in the event payload of a ``workflow_run``, and to anyone who
can open a pull request against a repository with a ``pull_request_target``
workflow.

**One snapshot, not one per file.** Every migration in the pull request is
assessed against the same observed state, so their reports agree with each
other, and a reviewer reading three reports is reading three views of one
database rather than three databases moments apart. It is also one
connection instead of N.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

from blastoise.ci.config import CiConfig, ConfigError, FailOn, load_config
from blastoise.ci.detect import DetectedFile, SourceKind, detect_migrations
from blastoise.ci.github import (
    CONCLUSION_OF_VERDICT,
    GitHubClient,
    GitHubContext,
    GitHubError,
    Transport,
    urllib_transport,
)
from blastoise.ci.markdown import (
    COMMENT_MARKER,
    render_comment,
    render_summary_line,
    unsupported_notice,
)
from blastoise.ci.model import CiRun, FileOutcome, OutcomeStatus
from blastoise.ci.redact import Redactor
from blastoise.ir import MigrationScript
from blastoise.parser import (
    MigrationParseError,
    parse_migration,
    parse_migration_file,
)
from blastoise.rails import (
    RailsExtractionError,
    extract_rails_sql,
    rails_refusal,
)
from blastoise.report import (
    BUNDLE_DIRNAME,
    EXIT_CODES,
    EXIT_TOOL_ERROR,
    REPORT_FILENAME,
    FileVerdict,
    build_report,
    canonical_json,
    tool_version,
    write_bundle,
)

__all__ = ["ChangedSource", "CiOptions", "CiRunError", "run_ci"]

DEFAULT_PG_VERSION = 17
DEFAULT_OUTPUT_DIR = "blastoise-reports"

# Environment variables whose values are registered with the redactor
# unconditionally, whatever the run is configured to read.
_ALWAYS_SECRET = (
    "BLASTOISE_DATABASE_URL",
    "DATABASE_URL",
    "PGPASSWORD",
    "PGPASSFILE",
    "GITHUB_TOKEN",
    "INPUT_GITHUB_TOKEN",
)


class CiRunError(Exception):
    """The run could not be performed at all: exit 3, never exit 2.

    A run that failed is not a migration that is dangerous. The distinction
    is the same one ``check`` makes between exit 3 and exit 2, and it is
    kept here for the same reason: CI reading a 2 must be reading a verdict.
    """


class ChangedSource:
    """Where the list of changed files came from. Plain strings, for logs."""

    AUTO = "auto"
    GITHUB_API = "github_api"
    GIT = "git"
    FILE = "file"
    EXPLICIT = "explicit"


@dataclass(slots=True)
class CiOptions:
    """Everything ``blastoise ci`` was told, before config is merged in."""

    repo_root: Path = field(default_factory=Path)
    config_path: Path | None = None
    changed_source: str = ChangedSource.AUTO
    changed_from: Path | None = None
    explicit_paths: tuple[str, ...] = ()
    base_ref: str | None = None
    head_ref: str = "HEAD"
    output_dir: Path | None = None
    pg_version: int | None = None
    offline: bool = False
    database_url_env: str | None = None
    database_label: str | None = None
    fail_on: FailOn | None = None
    comment: bool | None = None
    check_run: bool | None = None
    comment_output: Path | None = None
    summary_output: Path | None = None
    json_output: Path | None = None
    artifact_name: str | None = None
    emit_json: bool = False
    transport: Transport = urllib_transport


# --------------------------------------------------------------------------
# changed files
# --------------------------------------------------------------------------


def _read_changed_file(path: Path, stdin: IO[str] | None) -> tuple[str, ...]:
    if str(path) == "-":
        if stdin is None:
            raise CiRunError("--changed-from - was given but stdin is not readable")
        text = stdin.read()
    else:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise CiRunError(f"cannot read {path}: {exc.strerror or exc}") from exc
    return tuple(line.strip() for line in text.splitlines() if line.strip())


def _git_changed(root: Path, base: str, head: str) -> tuple[str, ...]:
    """``git diff`` against the base, three-dot first.

    Three-dot is the semantics a reviewer expects -- the changes the branch
    introduced, not the difference between two branch tips -- but it needs
    the merge base, which a shallow clone does not have. Two-dot compares
    the two trees directly and needs only the two commits, so it is the
    fallback rather than a failure.
    """
    attempts = ([f"{base}...{head}"], [base, head])
    last_error = "git produced no output"
    for spec in attempts:
        try:
            result = subprocess.run(
                ["git", "diff", "--name-only", "--diff-filter=d", *spec],
                cwd=root,
                capture_output=True,
                text=True,
                check=False,
                timeout=120,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise CiRunError(f"could not run git: {exc}") from exc
        if result.returncode == 0:
            return tuple(line.strip() for line in result.stdout.splitlines() if line.strip())
        last_error = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else (
            f"git exited {result.returncode}"
        )
    raise CiRunError(
        f"git diff against '{base}' failed ({last_error}). Check out with "
        "fetch-depth: 0, or pass --changed-from with a list of paths."
    )


def _resolve_changed(
    options: CiOptions,
    context: GitHubContext,
    client: GitHubClient | None,
    stdin: IO[str] | None,
    log: _Log,
) -> tuple[tuple[str, ...], str]:
    source = options.changed_source
    if options.explicit_paths:
        return options.explicit_paths, ChangedSource.EXPLICIT
    if source in (ChangedSource.AUTO, ChangedSource.FILE) and options.changed_from is not None:
        return _read_changed_file(options.changed_from, stdin), ChangedSource.FILE
    if source == ChangedSource.FILE:
        raise CiRunError("--changed-source file needs --changed-from PATH")

    if source in (ChangedSource.AUTO, ChangedSource.GITHUB_API):
        if client is not None and context.is_pull_request and context.pull_number is not None:
            try:
                return (
                    client.pull_request_files(context.pull_number),
                    ChangedSource.GITHUB_API,
                )
            except GitHubError as exc:
                if source == ChangedSource.GITHUB_API:
                    raise CiRunError(f"could not list the pull request's files: {exc}") from exc
                log.warn(f"could not list the pull request's files ({exc}); falling back to git")
        elif source == ChangedSource.GITHUB_API:
            raise CiRunError(
                "--changed-source github_api needs a pull request context and a token"
            )

    base = options.base_ref or context.base_sha
    if not base:
        raise CiRunError(
            "no base to compare against: pass --base-ref, or --changed-from "
            "with a list of paths, or run inside a pull request with a token"
        )
    return _git_changed(options.repo_root, base, options.head_ref), ChangedSource.GIT


# --------------------------------------------------------------------------
# assessment
# --------------------------------------------------------------------------


def _slug(path: str, index: int) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]+", "-", path).strip("-").lower()
    return f"{index:02d}-{cleaned[:80] or 'migration'}"


@dataclass(slots=True)
class _Parsed:
    detected: DetectedFile
    script: MigrationScript


def _capture_snapshot(
    database_url: str,
    scripts: list[_Parsed],
    redactor: Redactor,
    log: _Log,
) -> tuple[Any | None, str | None]:
    """One snapshot for the whole run, or a redacted reason it was skipped."""
    from blastoise.live import (
        LiveIntrospectionError,
        TypeChangeProbe,
        WritableRoleError,
        capture_snapshot,
    )
    from blastoise.verdict import snapshot_probes

    relations: set[str] = set()
    functions: set[str] = set()
    types: set[str] = set()
    type_changes: set[tuple[str, str, str]] = set()
    for parsed in scripts:
        probes = snapshot_probes(parsed.script)
        relations.update(probes.relations)
        functions.update(probes.functions)
        types.update(probes.types)
        type_changes.update(probes.type_changes)

    try:
        snapshot = capture_snapshot(
            database_url,
            tuple(sorted(relations)),
            functions=tuple(sorted(functions)),
            types=tuple(sorted(types)),
            type_changes=tuple(
                TypeChangeProbe(relation=relation, column=column, new_type=new_type)
                for relation, column, new_type in sorted(type_changes)
            ),
        )
    except (LiveIntrospectionError, WritableRoleError) as exc:
        reason = redactor.scrub_exception(exc)
        log.warn(
            f"could not capture a live snapshot: {reason}; "
            "degrading to offline - the reports will carry far more in unverified"
        )
        return None, f"live snapshot unavailable ({reason})"
    except Exception as exc:
        reason = redactor.scrub_exception(exc)
        log.warn(f"live snapshot failed unexpectedly: {reason}; degrading to offline")
        log.debug(redactor.scrub_traceback(exc))
        return None, f"live snapshot unavailable ({reason})"
    return snapshot, None


# --------------------------------------------------------------------------
# logging
# --------------------------------------------------------------------------


@dataclass(slots=True)
class _Log:
    """Every write goes through the redactor. That is the whole point."""

    stream: IO[str]
    redactor: Redactor
    verbose: bool = False

    def info(self, message: str) -> None:
        print(self.redactor.scrub(message), file=self.stream)

    def warn(self, message: str) -> None:
        print(f"WARNING: {self.redactor.scrub(message)}", file=self.stream)

    def debug(self, message: str) -> None:
        if self.verbose:
            print(self.redactor.scrub(message), file=self.stream)


# --------------------------------------------------------------------------
# the run
# --------------------------------------------------------------------------


def _resolve_database_url(
    config: CiConfig,
    options: CiOptions,
    environ: dict[str, str],
    redactor: Redactor,
    log: _Log,
) -> str | None:
    """The connection string, from the environment, or ``None``.

    The variable is named, never valued, by config and CLI alike. Two
    refusals are enforced here rather than documented:

    * a name that is itself a connection string (the mistake that puts a
      credential into a committed file or a workflow input);
    * a name in GitHub Actions' ``INPUT_*`` namespace, which is how an
      Action input arrives -- the one channel this must never read from.
    """
    if options.offline:
        return None
    name = (options.database_url_env or config.database_url_env).strip()
    if "://" in name or "=" in name or " " in name:
        raise CiRunError(
            "--database-url-env takes the NAME of an environment variable, "
            "not a connection string. The value must come from your CI "
            "secret store"
        )
    if name.upper().startswith("INPUT_"):
        raise CiRunError(
            f"refusing to read the connection string from '{name}': "
            "INPUT_* is the GitHub Actions workflow-input namespace, and a "
            "connection string must come from a secret, never from an input"
        )
    value = environ.get(name)
    if not value or not value.strip():
        log.info(
            f"no connection string in ${name}: running offline. "
            "Set it from a secret to resolve the size-dependent verdicts."
        )
        return None
    redactor.add_connection_string(value)
    return value.strip()


def _rails_order(
    detected: tuple[DetectedFile, ...],
) -> dict[str, tuple[str, ...]]:
    """For each Rails migration, the ones in this change that precede it.

    Ordered by the timestamp Rails puts at the front of the file name,
    which is the order Rails itself would run them in. Only migrations in
    the same directory count: two Rails apps in a monorepo have separate
    histories and separate databases.
    """
    buckets: dict[str, list[tuple[str, str]]] = {}
    for entry in detected:
        if not entry.extractable:
            continue
        directory, _, name = entry.path.rpartition("/")
        version = name.split("_", 1)[0]
        if not version.isdigit():
            continue
        buckets.setdefault(directory, []).append((version, entry.path))
    order: dict[str, tuple[str, ...]] = {}
    for entries in buckets.values():
        entries.sort()
        for index, (_, path) in enumerate(entries):
            order[path] = tuple(other for _, other in entries[:index])
    return order


def _resolve_scratch_url(
    config: CiConfig, environ: dict[str, str], redactor: Redactor
) -> str | None:
    """The throwaway database's connection string, from the environment.

    Named by config and valued by the CI job, on the same rule as the
    assessed database: a URL in a committed file is a credential in a
    committed file, whichever database it points at.
    """
    value = environ.get(config.rails.scratch_url_env)
    if not value or not value.strip():
        return None
    redactor.add_connection_string(value)
    return value.strip()


def _render_rails(
    entry: DetectedFile,
    options: CiOptions,
    config: CiConfig,
    scratch_url: str,
    base_ref: str | None,
    preceding: tuple[str, ...],
    environ: dict[str, str],
    redactor: Redactor,
    log: _Log,
) -> _Parsed | FileOutcome:
    """Render a Rails migration to SQL and parse it, or say why not.

    The parse is not a formality. The SQL comes from ActiveRecord, so if it
    does not enter the ordinary parser cleanly then something about the
    rendering is wrong, and the right answer is to report the file as
    unassessed -- not to widen the parser until the output fits.
    """
    try:
        extraction = extract_rails_sql(
            entry.path,
            repo_root=options.repo_root,
            scratch_url=scratch_url,
            base_ref=base_ref,
            ruby=config.rails.ruby,
            timeout=config.rails.timeout,
            preceding=preceding,
            environ=environ,
        )
    except RailsExtractionError as exc:
        notice = unsupported_notice(entry.framework, redactor.scrub(exc.reason))
        log.info(f"{entry.path}: {notice}")
        return FileOutcome.of(entry, OutcomeStatus.UNSUPPORTED, detail=notice)

    try:
        script = parse_migration(extraction.sql, path=entry.path)
    except MigrationParseError as exc:
        reason = redactor.scrub(
            f"ActiveRecord emitted SQL this parser could not read ({exc})"
        )
        notice = unsupported_notice(entry.framework, reason)
        log.warn(f"{entry.path}: {notice}")
        return FileOutcome.of(entry, OutcomeStatus.UNSUPPORTED, detail=notice)

    version = extraction.rails_version or "an unknown version"
    log.info(
        f"{entry.path}: rendered {len(extraction.statements)} statement(s) "
        f"with ActiveRecord {version}, pre-state from {extraction.schema_source}"
    )
    return _Parsed(entry, script)


def _load_event(environ: dict[str, str], log: _Log) -> dict[str, Any] | None:
    path = environ.get("GITHUB_EVENT_PATH")
    if not path:
        return None
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warn(f"could not read the workflow event payload: {exc}")
        return None
    return data if isinstance(data, dict) else None


def _github_token(environ: dict[str, str]) -> str | None:
    for name in ("INPUT_GITHUB_TOKEN", "GITHUB_TOKEN", "GH_TOKEN"):
        value = environ.get(name)
        if value and value.strip():
            return value.strip()
    return None


def _write_text(path: Path, text: str, log: _Log) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    except OSError as exc:
        log.warn(f"could not write {path}: {exc.strerror or exc}")


def _append_text(path: Path, text: str, log: _Log) -> None:
    try:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(text)
    except OSError as exc:
        log.warn(f"could not write {path}: {exc.strerror or exc}")


def _write_outputs(environ: dict[str, str], values: dict[str, str], log: _Log) -> None:
    """GitHub Actions step outputs, via $GITHUB_OUTPUT."""
    target = environ.get("GITHUB_OUTPUT")
    if not target:
        return
    lines: list[str] = []
    for key, value in values.items():
        if "\n" in value:
            delimiter = "blastoise-eof"
            lines.append(f"{key}<<{delimiter}\n{value}\n{delimiter}\n")
        else:
            lines.append(f"{key}={value}\n")
    _append_text(Path(target), "".join(lines), log)


def _exit_code(verdict: FileVerdict, fail_on: FailOn) -> int:
    """The process exit code, which is the only thing that turns CI red.

    The verdict is reported in full regardless; this decides only how loud
    it is. ``never`` still reports -- it is for a team introducing the check
    to a repository whose migrations were never gated before.
    """
    if fail_on is FailOn.NEVER:
        return 0
    if verdict is FileVerdict.BLOCK:
        return EXIT_CODES[FileVerdict.BLOCK]
    if verdict is FileVerdict.REQUIRES_APPROVAL and fail_on is FailOn.REQUIRES_APPROVAL:
        return EXIT_CODES[FileVerdict.REQUIRES_APPROVAL]
    return 0


def run_ci(
    options: CiOptions,
    *,
    environ: dict[str, str] | None = None,
    stdout: IO[str] | None = None,
    stderr: IO[str] | None = None,
    stdin: IO[str] | None = None,
) -> int:
    """Run the CI integration. Returns the process exit code."""
    import sys

    environ = dict(os.environ if environ is None else environ)
    out = sys.stdout if stdout is None else stdout
    err = sys.stderr if stderr is None else stderr

    redactor = Redactor()
    redactor.add_environment(environ, *_ALWAYS_SECRET)
    log = _Log(stream=err, redactor=redactor)

    try:
        return _run(options, environ, out, err, stdin, redactor, log)
    except CiRunError as exc:
        log.info(f"error: {redactor.scrub(str(exc))}")
        return EXIT_TOOL_ERROR
    except ConfigError as exc:
        log.info(f"error: {redactor.scrub(str(exc))}")
        return EXIT_TOOL_ERROR
    except Exception as exc:
        log.info(f"error: {redactor.scrub_exception(exc)}")
        log.info(redactor.scrub_traceback(exc))
        return EXIT_TOOL_ERROR


def _run(
    options: CiOptions,
    environ: dict[str, str],
    out: IO[str],
    err: IO[str],
    stdin: IO[str] | None,
    redactor: Redactor,
    log: _Log,
) -> int:
    from blastoise.catalog import load_catalog
    from blastoise.verdict import assess_script

    config = load_config(options.config_path, root=options.repo_root)
    fail_on = options.fail_on or config.fail_on
    want_comment = config.comment if options.comment is None else options.comment
    want_check_run = config.check_run if options.check_run is None else options.check_run

    event = _load_event(environ, log)
    context = GitHubContext.from_environment(environ, event=event)
    token = _github_token(environ)
    if token:
        redactor.add_secret(token, minimum=8)
    client: GitHubClient | None = None
    if token and context.repository:
        client = GitHubClient(
            token=token,
            repository=context.repository,
            api_url=context.api_url,
            transport=options.transport,
        )

    changed, changed_source = _resolve_changed(options, context, client, stdin, log)
    log.info(f"{len(changed)} changed file(s) ({changed_source})")

    detected = detect_migrations(
        changed, include=config.paths, exclude=config.exclude
    )
    if config.overrides_detection:
        log.info(
            f"{len(detected)} migration file(s) matched the {len(config.paths)} "
            f"path glob(s) in {config.source} (framework detection is off)"
        )
    else:
        log.info(f"{len(detected)} migration file(s) detected")

    database_url = _resolve_database_url(config, options, environ, redactor, log)

    # Rendering a Rails migration means running it, so whether that is
    # allowed is decided once for the run, from the environment, and
    # reported once. Deciding per file would print the same refusal N times
    # and read as N problems with the migrations rather than one setting.
    scratch_url = _resolve_scratch_url(config, environ, redactor)
    rails_reason: str | None = None
    if any(entry.extractable for entry in detected):
        rails_reason = rails_refusal(
            enabled=config.rails.extract,
            scratch_url=scratch_url,
            assessed_url=database_url,
            environ=environ,
            scratch_url_env=config.rails.scratch_url_env,
        )
        if rails_reason:
            log.info(f"Rails extraction not run: {rails_reason}")

    base_ref = options.base_ref or context.base_sha
    # Migrations in one pull request run in version order, and a later one
    # routinely depends on an earlier one. Each is rendered with the ones
    # before it already applied.
    rails_order = _rails_order(detected)

    parsed: list[_Parsed] = []
    outcomes: list[FileOutcome] = []
    for entry in detected:
        if entry.source_kind is SourceKind.DSL:
            if entry.extractable and rails_reason is None and scratch_url:
                rendered = _render_rails(
                    entry,
                    options,
                    config,
                    scratch_url,
                    base_ref,
                    rails_order.get(entry.path, ()),
                    environ,
                    redactor,
                    log,
                )
                if isinstance(rendered, _Parsed):
                    parsed.append(rendered)
                else:
                    outcomes.append(rendered)
                continue
            notice = unsupported_notice(
                entry.framework, rails_reason if entry.extractable else None
            )
            log.info(f"{entry.path}: {notice}")
            outcomes.append(
                FileOutcome.of(entry, OutcomeStatus.UNSUPPORTED, detail=notice)
            )
            continue
        full = options.repo_root / entry.path
        try:
            parsed.append(_Parsed(entry, parse_migration_file(str(full))))
        except OSError as exc:
            detail = redactor.scrub(f"cannot read the file ({exc.strerror or exc})")
            log.warn(f"{entry.path}: {detail}")
            outcomes.append(FileOutcome.of(entry, OutcomeStatus.ERROR, detail=detail))
        except MigrationParseError as exc:
            detail = redactor.scrub(f"could not be parsed as SQL ({exc})")
            log.warn(f"{entry.path}: {detail}")
            outcomes.append(FileOutcome.of(entry, OutcomeStatus.ERROR, detail=detail))

    snapshot = None
    degraded_reason: str | None = None
    if database_url and parsed:
        snapshot, degraded_reason = _capture_snapshot(database_url, parsed, redactor, log)

    catalog = load_catalog()
    pg_version = options.pg_version or config.pg_version or DEFAULT_PG_VERSION
    notes: list[str] = []
    if snapshot is not None and snapshot.server.pg_major.available:
        server_major = snapshot.server.pg_major.value
        if pg_version != server_major:
            notes.append(
                f"pg_version {pg_version} was overridden by the server's "
                f"actual version {server_major}"
            )
        pg_version = server_major

    output_root = options.output_dir or (options.repo_root / DEFAULT_OUTPUT_DIR)
    evaluated_at = datetime.now(UTC).isoformat()

    for index, item in enumerate(parsed, start=1):
        assessment = assess_script(item.script, catalog, pg_version, snapshot)
        payload, bundle = build_report(
            item.script,
            assessment,
            catalog=catalog,
            snapshot=snapshot,
            evaluated_at=evaluated_at,
            bundle_dir=BUNDLE_DIRNAME,
            degraded_reason=degraded_reason,
            notes=tuple(notes),
        )
        directory = output_root / _slug(item.detected.path, index)
        try:
            write_bundle(bundle, directory / BUNDLE_DIRNAME)
            (directory / REPORT_FILENAME).write_text(
                canonical_json(payload) + "\n", encoding="ascii"
            )
            report_dir: str | None = str(directory)
        except OSError as exc:
            log.warn(f"could not write the report for {item.detected.path}: {exc}")
            report_dir = None
        outcomes.append(
            FileOutcome.of(
                item.detected,
                OutcomeStatus.ASSESSED,
                verdict=FileVerdict(payload["verdict"]),
                counts=dict(payload["classification_counts"]),
                payload=payload,
                report_dir=report_dir,
            )
        )

    # Detection order, not assessment order: the comment reads as the file
    # list of the pull request, with the DSL files in place among the rest.
    order = {entry.path: position for position, entry in enumerate(detected)}
    outcomes.sort(key=lambda outcome: order.get(outcome.path, len(order)))

    run = CiRun(
        outcomes=tuple(outcomes),
        online=snapshot is not None,
        tool_version=tool_version(),
        changed_files=len(changed),
        database_label=options.database_label or config.database_label,
        degraded_reason=degraded_reason,
        notes=tuple(notes),
        report_root=str(output_root) if parsed else None,
        artifact_name=options.artifact_name,
    )

    body = render_comment(run)
    _publish(run, body, options, context, client, environ, want_comment, want_check_run, log)

    if options.comment_output is not None:
        _write_text(options.comment_output, redactor.scrub(body), log)
    summary_target = options.summary_output or (
        Path(environ["GITHUB_STEP_SUMMARY"]) if environ.get("GITHUB_STEP_SUMMARY") else None
    )
    if summary_target is not None:
        _append_text(summary_target, redactor.scrub(body), log)

    summary = run.summary()
    if options.json_output is not None:
        _write_text(options.json_output, json.dumps(summary, indent=2) + "\n", log)
    if options.emit_json:
        print(redactor.scrub(json.dumps(summary, indent=2)), file=out)
    else:
        print(redactor.scrub(render_summary_line(run)), file=out)

    _write_outputs(
        environ,
        {
            "verdict": str(run.verdict),
            "migrations-detected": str(len(run.outcomes)),
            "assessed": str(len(run.assessed)),
            "unassessed": str(len(run.unsupported) + len(run.errors)),
            "online": "true" if run.online else "false",
            "report-dir": run.report_root or "",
        },
        log,
    )
    return _exit_code(run.verdict, fail_on)


def _publish(
    run: CiRun,
    body: str,
    options: CiOptions,
    context: GitHubContext,
    client: GitHubClient | None,
    environ: dict[str, str],
    want_comment: bool,
    want_check_run: bool,
    log: _Log,
) -> None:
    """Post the comment and set the check status; never fatal.

    A pull request from a fork carries a read-only ``GITHUB_TOKEN``: both
    calls return 403 and there is nothing the run can do about it. Failing
    the job would punish the contributor for a permission model they do not
    control, so the verdict stays in the job summary, the artifact and the
    exit code, and the log says why the comment is missing.
    """
    if client is None:
        if want_comment or want_check_run:
            log.info(
                "no GitHub token or repository in the environment: "
                "not posting a comment or a check status"
            )
        return

    if want_comment:
        if not context.is_pull_request or context.pull_number is None:
            log.info("not a pull request event: no comment to post")
        else:
            try:
                # A pull request that touches no migration gets no comment
                # at all -- and any comment a previous push left is deleted
                # rather than rewritten to an empty table. A check that
                # speaks on every pull request regardless is a check people
                # mute, and "never mind" is better said by leaving than by
                # posting a table of zeros.
                if not run.outcomes:
                    removed = client.delete_comment(context.pull_number, COMMENT_MARKER)
                    log.info(
                        "no migrations in this pull request: "
                        + (
                            "removed the comment an earlier push left"
                            if removed
                            else "no comment posted"
                        )
                    )
                else:
                    action, url = client.upsert_comment(
                        context.pull_number, COMMENT_MARKER, body
                    )
                    log.info(f"comment {action}{f': {url}' if url else ''}")
                    if url:
                        _write_outputs(environ, {"comment-url": url}, log)
            except GitHubError as exc:
                if exc.is_permission:
                    log.warn(
                        f"could not post the comment ({exc}). A pull request "
                        "from a fork has a read-only token; the verdict is in "
                        "the job summary and the workflow artifact."
                    )
                else:
                    log.warn(f"could not post the comment ({exc})")

    if want_check_run:
        if not context.head_sha:
            log.info("no head commit in the environment: no check status set")
            return
        conclusion = CONCLUSION_OF_VERDICT[run.verdict]
        try:
            url = client.create_check_run(
                context.head_sha,
                conclusion=conclusion,
                title=render_summary_line(run),
                summary=body,
            )
            log.info(f"check status '{conclusion}'{f': {url}' if url else ''}")
        except GitHubError as exc:
            if exc.is_permission:
                log.warn(
                    f"could not set the check status ({exc}). This needs "
                    "'checks: write'; without it the neutral state for "
                    "requires_approval is not available and the exit code "
                    "is the only signal."
                )
            else:
                log.warn(f"could not set the check status ({exc})")
