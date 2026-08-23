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
    return _cmd_explain(args)


if __name__ == "__main__":
    raise SystemExit(main())
