"""Command-line interface.

Named surfaces (help text, docs, README) carry the product's vocabulary:
Torrent is the parser and IR, Shell Armour the lock semantics catalog,
Hydro Scan the live introspection layer, Pressure Levels the five
classification tiers, Shell Report the verdict document and Shell Seal its
signature. None of that reaches the payload — ``--json`` keys, enum values
and exit codes are the plain machine names, and a test pins them.

Exit codes of ``check`` (CI depends on these being distinct):
0 = proceed, 1 = requires_approval, 2 = block, 3 = tool error (including
unreadable/unparseable input and bad usage). ``verify`` exits 0 when the
signature and every evidence hash check out, 1 when either fails, 3 on
tool error.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn

from blastoise.ci.config import CONFIG_FILENAME, DEFAULT_DATABASE_URL_ENV, FailOn
from blastoise.ci.runner import DEFAULT_OUTPUT_DIR, ChangedSource
from blastoise.ir import MigrationScript, ParsedStatement
from blastoise.parser import MigrationParseError, parse_migration_file
from blastoise.report import (
    BUNDLE_DIRNAME,
    EXIT_TOOL_ERROR,
    REPORT_FILENAME,
    FileVerdict,
    SigningError,
    SigningUnavailableError,
    build_report,
    canonical_json,
    check_evidence,
    exit_code,
    render_report,
    resolve_signing_key,
    sign_payload,
    verify_signature,
    write_bundle,
)

DEFAULT_PG_VERSION = 17


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

'check' exit codes: 0 proceed, 1 requires_approval, 2 block, 3 tool error.

Components: Torrent (parser and IR), Shell Armour (lock semantics
catalog), Hydro Scan (live introspection), Shell Report (the verdict
document), Training Ground (the scale harness), Evolution (the
calibration loop), Shell Seal (signing and attestation).

'bt' is an alias for this command.
"""


CI_EPILOG = """The connection string is read from an environment variable, whose NAME
comes from .blastoise.yml (database.url_env) or --database-url-env. There
is deliberately no flag that takes the value: a connection string passed
on a command line or as a workflow input is visible in the run's logs.

Migration files are detected by framework layout - Rails db/migrate/,
Django */migrations/, Prisma prisma/migrations/, Flyway V*__*.sql,
Alembic */versions/, golang-migrate *.up.sql, and a plain migrations/
directory. migrations.paths in .blastoise.yml overrides all of that.

Exit codes: 0 nothing to stop the merge, 2 block, 1 requires_approval
when ci.fail_on says so, 3 the run itself failed.
"""


def _add_ci_parser(subparsers: argparse._SubParsersAction[_Parser]) -> None:
    ci_cmd = subparsers.add_parser(
        "ci",
        help="assess the migrations a pull request changed, and report back",
        description=(
            "Detect the migration files a change touches, assess each one, "
            "and publish the result: a pull request comment that leads with "
            "the verdict, a check status (PROCEED passes, REQUIRES_APPROVAL "
            "is neutral, BLOCK fails), a Shell Report and evidence bundle "
            "per file, and a job summary. Outside GitHub, --comment-output "
            "and --json give a pipeline the same material."
        ),
        epilog=CI_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ci_cmd.add_argument(
        "paths",
        nargs="*",
        help="changed files to consider; by default they are discovered (see --changed-source)",
    )
    ci_cmd.add_argument(
        "--config",
        help=f"path to the config file (default: {CONFIG_FILENAME} at the repo root, if present)",
    )
    ci_cmd.add_argument(
        "--repo-root",
        default=".",
        help="root of the checkout that changed paths are relative to (default: .)",
    )
    ci_cmd.add_argument(
        "--changed-source",
        choices=[
            ChangedSource.AUTO,
            ChangedSource.GITHUB_API,
            ChangedSource.GIT,
            ChangedSource.FILE,
        ],
        default=ChangedSource.AUTO,
        help=(
            "how to discover changed files: 'github_api' (the pull request's "
            "file list, works with a shallow checkout), 'git' (diff against "
            "--base-ref), 'file' (--changed-from). 'auto' prefers the API in "
            "a pull request and falls back to git"
        ),
    )
    ci_cmd.add_argument(
        "--changed-from",
        help="file of newline-separated changed paths, or '-' for stdin",
    )
    ci_cmd.add_argument("--base-ref", help="base commit or ref for the git diff")
    ci_cmd.add_argument("--head-ref", default="HEAD", help="head of the git diff (default: HEAD)")
    ci_cmd.add_argument(
        "-o",
        "--output-dir",
        help=f"where to write one report directory per migration (default: {DEFAULT_OUTPUT_DIR}/)",
    )
    ci_cmd.add_argument(
        "--pg-version",
        type=int,
        help="Postgres major version to assess against when offline (default 17)",
    )
    ci_cmd.add_argument(
        "--offline",
        action="store_true",
        help="never connect, whatever the environment holds",
    )
    ci_cmd.add_argument(
        "--database-url-env",
        help=(
            "NAME of the environment variable holding the connection string "
            f"(default: {DEFAULT_DATABASE_URL_ENV}). Not the value: this "
            "command never accepts a connection string as an argument"
        ),
    )
    ci_cmd.add_argument(
        "--database-label",
        help=(
            "human label for the database that was read ('staging'), shown "
            "in the comment so a reviewer knows where the sizes came from"
        ),
    )
    ci_cmd.add_argument(
        "--fail-on",
        choices=[member.value for member in FailOn],
        help="how far the verdict must go before the step fails (default: block)",
    )
    ci_cmd.add_argument(
        "--no-comment", action="store_true", help="do not post or update a pull request comment"
    )
    ci_cmd.add_argument(
        "--no-check-run", action="store_true", help="do not set a check status"
    )
    ci_cmd.add_argument(
        "--comment-output",
        help="also write the comment body (Markdown) to this file, for non-GitHub CI",
    )
    ci_cmd.add_argument(
        "--summary-output",
        help="append the comment body here (default: $GITHUB_STEP_SUMMARY when set)",
    )
    ci_cmd.add_argument("--json-output", help="write the machine summary to this file")
    ci_cmd.add_argument(
        "--json", action="store_true", help="print the machine summary on stdout"
    )
    ci_cmd.add_argument(
        "--artifact-name",
        help="name of the workflow artifact the reports are uploaded as, for the comment to cite",
    )


def _cmd_ci(args: argparse.Namespace) -> int:
    from blastoise.ci import CiOptions, run_ci

    def _path(value: str | None) -> Path | None:
        return None if value is None else Path(value)

    options = CiOptions(
        repo_root=Path(args.repo_root),
        config_path=_path(args.config),
        changed_source=args.changed_source,
        changed_from=_path(args.changed_from),
        explicit_paths=tuple(args.paths),
        base_ref=args.base_ref,
        head_ref=args.head_ref,
        output_dir=_path(args.output_dir),
        pg_version=args.pg_version,
        offline=args.offline,
        database_url_env=args.database_url_env,
        database_label=args.database_label,
        fail_on=None if args.fail_on is None else FailOn(args.fail_on),
        comment=False if args.no_comment else None,
        check_run=False if args.no_check_run else None,
        comment_output=_path(args.comment_output),
        summary_output=_path(args.summary_output),
        json_output=_path(args.json_output),
        artifact_name=args.artifact_name,
        emit_json=args.json,
    )
    return run_ci(options)


class _Parser(argparse.ArgumentParser):
    """argparse, but usage errors exit 3 (tool error), never 2.

    CI reads ``check``'s exit 2 as BLOCK; a typo'd flag must not be
    mistakable for a dangerous migration.
    """

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        self.exit(EXIT_TOOL_ERROR, f"{self.prog}: error: {message}\n")


def _build_parser() -> argparse.ArgumentParser:
    arg_parser = _Parser(
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

    check_cmd = subparsers.add_parser(
        "check",
        help="assess one migration and produce a Shell Report (the verdict document)",
        description=(
            "Assess a migration file and produce the Shell Report: a "
            "versioned verdict document whose every claim references an "
            "evidence bundle by sha256. With --database-url the assessment "
            "is checked against the live database (read-only); without one "
            "it runs offline and the report says, at length, what could "
            "not be checked. Exit code: 0 proceed, 1 requires_approval, "
            "2 block, 3 tool error."
        ),
    )
    check_cmd.add_argument("migration", help="the migration .sql file to assess")
    check_cmd.add_argument(
        "--database-url",
        help=(
            "connection string of the database the migration will run "
            "against; introspection is read-only and refuses writable roles"
        ),
    )
    check_cmd.add_argument(
        "--pg-version",
        type=int,
        help=(
            "Postgres major version to assess against (10-18); ignored in "
            f"favour of the server's real version when connected. Default {DEFAULT_PG_VERSION}"
        ),
    )
    check_cmd.add_argument(
        "--offline",
        action="store_true",
        help="parse and catalog only, no connection (even if --database-url is set)",
    )
    check_cmd.add_argument(
        "--json",
        action="store_true",
        help="emit the report as canonical JSON on stdout instead of the rendering",
    )
    check_cmd.add_argument(
        "-o",
        "--output-dir",
        help=(
            f"write {REPORT_FILENAME} plus the evidence bundle "
            f"({BUNDLE_DIRNAME}/) into this directory; required for "
            "'blastoise verify' to have hashes to check against"
        ),
    )
    check_cmd.add_argument(
        "--sign-key",
        help=(
            "path to an Ed25519 private key (PEM or 64-hex-char seed) to "
            "apply the Shell Seal; defaults to $BLASTOISE_SIGNING_KEY, and "
            "the report ships unsigned when neither is set"
        ),
    )
    check_cmd.add_argument(
        "--change-id",
        help="identifier for this change; defaults to the sha256 of the migration source",
    )
    check_cmd.add_argument(
        "--verbose",
        action="store_true",
        help="print a timing breakdown on stderr",
    )

    verify_cmd = subparsers.add_parser(
        "verify",
        help="check a report's Shell Seal signature and its evidence bundle hashes",
        description=(
            "Verify a Shell Report: the Ed25519 signature must match the "
            "report's canonical content AND every evidence bundle file must "
            "match its recorded sha256. Both must pass for exit 0; an "
            "unsigned report fails verification (a stripped signature is "
            "indistinguishable from one that never existed). Exit 1 on any "
            "failure, 3 on tool error."
        ),
    )
    verify_cmd.add_argument("report", help="the report.json to verify")

    _add_ci_parser(subparsers)

    explain_cmd = subparsers.add_parser(
        "explain",
        help="render a report in expanded human-readable form",
        description=(
            "Render an existing Shell Report in full: every statement with "
            "its locks, durations, live narrowings, reversibility, and "
            "evidence references."
        ),
    )
    explain_cmd.add_argument("report", help="the report.json to explain")

    return arg_parser


def _cmd_parse(args: argparse.Namespace) -> int:
    exit_status = 0
    scripts: list[MigrationScript] = []
    for file_name in args.files:
        try:
            scripts.append(parse_migration_file(file_name))
        except OSError as exc:
            print(f"error: cannot read {file_name}: {exc}", file=sys.stderr)
            exit_status = 2
        except MigrationParseError as exc:
            print(f"error: {exc}", file=sys.stderr)
            exit_status = 2

    if args.json:
        payload = [
            {key: value for key, value in _to_jsonable(script).items() if key != "source"}
            for script in scripts
        ]
        print(json.dumps(payload, indent=2))
    else:
        for script in scripts:
            _print_script(script)
    return exit_status


def _capture(
    database_url: str, script: MigrationScript
) -> tuple[Any | None, str | None]:
    """(snapshot, degraded_reason): degrade to offline rather than fail.

    An unreachable database, a refused (writable) role, or a missing
    psycopg all produce a loud warning and an offline report — the
    zero-friction path must survive a bad Tuesday.
    """
    from blastoise.live import (
        LiveIntrospectionError,
        TypeChangeProbe,
        WritableRoleError,
        capture_snapshot,
    )
    from blastoise.verdict import snapshot_probes

    probes = snapshot_probes(script)
    try:
        snapshot = capture_snapshot(
            database_url,
            probes.relations,
            functions=probes.functions,
            types=probes.types,
            type_changes=tuple(
                TypeChangeProbe(relation=rel, column=col, new_type=new_type)
                for rel, col, new_type in probes.type_changes
            ),
        )
    except (LiveIntrospectionError, WritableRoleError) as exc:
        reason = f"live snapshot unavailable ({exc})"
        print(
            "WARNING: could not capture a live snapshot: "
            f"{exc}\nWARNING: degrading to offline - the report will carry "
            "far more in unverified",
            file=sys.stderr,
        )
        return None, reason
    return snapshot, None


def _cmd_check(args: argparse.Namespace) -> int:
    from blastoise.catalog import load_catalog
    from blastoise.verdict import assess_script

    timings: list[tuple[str, int]] = []

    def timed(label: str, start: float) -> None:
        timings.append((label, int((time.monotonic() - start) * 1000)))

    started = time.monotonic()
    step = time.monotonic()
    try:
        script = parse_migration_file(args.migration)
    except OSError as exc:
        print(f"error: cannot read {args.migration}: {exc}", file=sys.stderr)
        return EXIT_TOOL_ERROR
    except MigrationParseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_TOOL_ERROR
    timed("parse", step)

    try:
        key = resolve_signing_key(args.sign_key, os.environ)
    except (SigningError, SigningUnavailableError) as exc:
        # Signing was explicitly requested; shipping unsigned instead
        # would be a silent downgrade of an attestation someone asked for.
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_TOOL_ERROR

    step = time.monotonic()
    catalog = load_catalog()
    timed("catalog", step)

    snapshot = None
    degraded_reason: str | None = None
    if args.database_url and not args.offline:
        step = time.monotonic()
        snapshot, degraded_reason = _capture(args.database_url, script)
        timed("snapshot", step)

    notes: list[str] = []
    pg_version = args.pg_version or DEFAULT_PG_VERSION
    if snapshot is not None and snapshot.server.pg_major.available:
        server_major = snapshot.server.pg_major.value
        if args.pg_version is not None and args.pg_version != server_major:
            notes.append(
                f"--pg-version {args.pg_version} was overridden by the "
                f"server's actual version {server_major}"
            )
        pg_version = server_major

    step = time.monotonic()
    assessment = assess_script(script, catalog, pg_version, snapshot)
    timed("assess", step)

    step = time.monotonic()
    payload, bundle = build_report(
        script,
        assessment,
        catalog=catalog,
        snapshot=snapshot,
        evaluated_at=datetime.now(UTC).isoformat(),
        change_id=args.change_id,
        bundle_dir=BUNDLE_DIRNAME if args.output_dir else None,
        degraded_reason=degraded_reason,
        notes=tuple(notes),
    )
    if key is not None:
        payload = sign_payload(payload, key)
    timed("report", step)

    if args.output_dir:
        out_dir = Path(args.output_dir)
        try:
            write_bundle(bundle, out_dir / BUNDLE_DIRNAME)
            (out_dir / REPORT_FILENAME).write_text(
                canonical_json(payload) + "\n", encoding="ascii"
            )
        except OSError as exc:
            print(f"error: cannot write report: {exc}", file=sys.stderr)
            return EXIT_TOOL_ERROR

    if args.json:
        print(canonical_json(payload))
    else:
        print(render_report(payload), end="")

    if args.verbose:
        timings.append(("total", int((time.monotonic() - started) * 1000)))
        breakdown = ", ".join(f"{label} {ms}ms" for label, ms in timings)
        print(f"timing: {breakdown}", file=sys.stderr)

    return exit_code(FileVerdict(payload["verdict"]))


def _load_report(path: str) -> dict[str, Any] | None:
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"error: cannot read {path}: {exc}", file=sys.stderr)
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"error: {path} is not valid JSON: {exc}", file=sys.stderr)
        return None
    if not isinstance(payload, dict):
        print(f"error: {path} is not a report object", file=sys.stderr)
        return None
    return payload


def _cmd_verify(args: argparse.Namespace) -> int:
    payload = _load_report(args.report)
    if payload is None:
        return EXIT_TOOL_ERROR
    try:
        signature_ok, signature_detail = verify_signature(payload)
    except SigningUnavailableError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_TOOL_ERROR
    evidence_ok, evidence_lines = check_evidence(payload, Path(args.report))

    print(f"signature: {'ok' if signature_ok else 'FAILED'} - {signature_detail}")
    print(f"evidence:  {'ok' if evidence_ok else 'FAILED'}")
    for line in evidence_lines:
        print(f"  {line}")
    if signature_ok and evidence_ok:
        print("verified: signature and every evidence hash match")
        return 0
    print("NOT VERIFIED", file=sys.stderr)
    return 1


def _cmd_explain(args: argparse.Namespace) -> int:
    payload = _load_report(args.report)
    if payload is None:
        return EXIT_TOOL_ERROR
    print(render_report(payload, expanded=True), end="")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "parse":
        return _cmd_parse(args)
    if args.command == "check":
        return _cmd_check(args)
    if args.command == "verify":
        return _cmd_verify(args)
    if args.command == "ci":
        return _cmd_ci(args)
    return _cmd_explain(args)


if __name__ == "__main__":
    raise SystemExit(main())
