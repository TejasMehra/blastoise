"""Online corpus run: replay each repo's migration chain against PG 17.10.

For each repo (corpus files grouped by prefix, ordered by number):
  1. create a fresh database;
  2. per file: parse -> derive probes -> capture a read-only snapshot of the
     database AS IT IS BEFORE THE FILE RUNS (the production semantics) ->
     assess online -> apply the file statement by statement as superuser.

Apply errors are recorded and the replay continues (best-effort chain), so
later files see an honest, possibly degraded database. Missing roles are
created on demand and the statement retried once — GRANTs to app roles are
repo environment, not migration behavior.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import time
from collections import Counter, defaultdict
from pathlib import Path

import psycopg

SCRATCH_EARLY = Path(__file__).parent
MODE = sys.argv[1] if len(sys.argv) > 1 else "new"
if MODE != "new":
    # Any reconstructed tree under the scratchpad ("baseline",
    # "baseline_ael"); prepended so it wins the import.
    sys.path.insert(0, str(SCRATCH_EARLY / MODE))

from blastoise.catalog.loader import load_catalog
from blastoise.live import TypeChangeProbe, capture_snapshot
from blastoise.live.introspect import LiveIntrospectionError
from blastoise.parser import MigrationParseError, parse_migration_file
from blastoise.verdict import Classification, assess_script, snapshot_probes

SCRATCH = Path(__file__).parent
CORPUS = SCRATCH / "corpus"
PG_BIN = SCRATCH / "pg" / "bin"
OUT = Path(sys.argv[2]) if len(sys.argv) > 2 else SCRATCH / f"corpus_online_{MODE}.json"

RO_ROLE = "blastoise_ro"
RO_PASSWORD = "blastoise-replay"

_ROLE_RE = re.compile(r'role "([^"]+)" does not exist')


def start_server() -> tuple[str, object]:
    import socket

    work = Path(tempfile.mkdtemp(prefix="blastoise-replay-"))
    data = work / "data"
    log = work / "server.log"
    subprocess.run(
        [str(PG_BIN / "initdb.exe"), "-D", str(data), "-U", "postgres", "-A", "trust",
         "-E", "UTF8", "--no-sync"],
        check=True,
        capture_output=True,
    )
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    options = (
        f"-p {port} -c listen_addresses=127.0.0.1 -c fsync=off "
        f"-c synchronous_commit=off -c full_page_writes=off "
        f"-c max_connections=50"
    )
    # DEVNULL, not capture_output: the daemonized postmaster inherits pipes on
    # Windows and the wait never returns otherwise.
    subprocess.run(
        [str(PG_BIN / "pg_ctl.exe"), "-D", str(data), "-l", str(log), "-w",
         "-o", options, "start"],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    def stop() -> None:
        subprocess.run(
            [str(PG_BIN / "pg_ctl.exe"), "-D", str(data), "-m", "immediate", "stop"],
            check=False,
            capture_output=True,
        )

    return f"postgresql://postgres@127.0.0.1:{port}/postgres", stop


def repo_files() -> dict[str, list[Path]]:
    groups: dict[str, list[tuple[int, Path]]] = defaultdict(list)
    for path in sorted(CORPUS.glob("*.sql")):
        repo, number, _rest = path.name.split("__", 2)
        groups[repo].append((int(number), path))
    return {repo: [p for _, p in sorted(items)] for repo, items in groups.items()}


def apply_file(conn: psycopg.Connection, script) -> tuple[int, int, list[str]]:
    """Apply statement by statement (autocommit); returns (ok, failed, errors)."""
    ok = 0
    failed = 0
    errors: list[str] = []
    in_txn = False
    for statement in script.statements:
        sql = statement.sql
        kind = statement.kind.value
        for attempt in (0, 1):
            try:
                conn.execute(sql)
                if kind == "begin":
                    in_txn = True
                elif kind in ("commit", "rollback"):
                    in_txn = False
                ok += 1
                break
            except psycopg.Error as exc:
                message = str(exc).split("\n")[0]
                match = _ROLE_RE.search(message)
                if attempt == 0 and match:
                    if in_txn:
                        try:
                            conn.execute("ROLLBACK")
                        except psycopg.Error:
                            pass
                        in_txn = False
                    try:
                        conn.execute(f'CREATE ROLE "{match.group(1)}"')
                        continue  # retry the statement once
                    except psycopg.Error:
                        pass
                failed += 1
                errors.append(f"{kind}: {message[:160]}")
                if in_txn:
                    try:
                        conn.execute("ROLLBACK")
                    except psycopg.Error:
                        pass
                    in_txn = False
                break
    if in_txn:
        try:
            conn.execute("COMMIT")
        except psycopg.Error:
            pass
    return ok, failed, errors


def main() -> None:
    catalog = load_catalog()
    admin_dsn, stop = start_server()
    started = time.monotonic()
    try:
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            admin.execute(f"CREATE ROLE {RO_ROLE} LOGIN PASSWORD '{RO_PASSWORD}'")
            admin.execute(f"GRANT pg_monitor TO {RO_ROLE}")

        class_counts: Counter[str] = Counter()
        tier_kinds: dict[str, Counter[str]] = {}
        rows: list[list[object]] = []
        method_counts: Counter[str] = Counter()
        unknown_by_kind: Counter[str] = Counter()
        unsafe_by_kind: Counter[str] = Counter()
        cond_by_kind: Counter[str] = Counter()  # retained: unused, kept for shape
        per_repo: dict[str, dict[str, object]] = {}
        parse_failures: list[str] = []
        capture_failures: list[str] = []
        statements = 0

        # argv[1]=mode, argv[2]=output path; any further args filter repos.
        only = set(sys.argv[3:])
        for repo, files in sorted(repo_files().items()):
            if only and repo not in only:
                continue
            db = "replay_" + re.sub(r"[^a-z0-9_]", "_", repo.lower())
            with psycopg.connect(admin_dsn, autocommit=True) as admin:
                admin.execute(f"CREATE DATABASE {db}")
            base = admin_dsn.rsplit("/", 1)[0]
            apply_dsn = f"{base}/{db}"
            ro_dsn = (
                f"{base}/{db}".replace("postgres@", f"{RO_ROLE}:{RO_PASSWORD}@")
            )
            repo_stats = {
                "files": len(files),
                "applied_ok": 0,
                "apply_failed": 0,
                "classification": Counter(),
                "apply_error_samples": [],
            }
            with psycopg.connect(apply_dsn, autocommit=True) as conn:
                conn.execute("SET statement_timeout = '30s'")
                for path in files:
                    try:
                        script = parse_migration_file(str(path))
                    except MigrationParseError:
                        parse_failures.append(path.name)
                        continue
                    probes = snapshot_probes(script)
                    snapshot = None
                    try:
                        snapshot = capture_snapshot(
                            ro_dsn,
                            probes.relations,
                            functions=probes.functions,
                            types=probes.types,
                            type_changes=[
                                TypeChangeProbe(relation=r, column=c, new_type=t)
                                for r, c, t in probes.type_changes
                            ],
                            connect_timeout_s=5,
                            statement_timeout_ms=4000,
                            lock_timeout_ms=1000,
                        )
                    except (LiveIntrospectionError, psycopg.Error) as exc:
                        capture_failures.append(f"{path.name}: {str(exc)[:120]}")
                    result = assess_script(script, catalog, 17, snapshot)
                    for statement in result.statements:
                        statements += 1
                        verdict = statement.verdict
                        cls = verdict.classification
                        class_counts[cls.value] += 1
                        repo_stats["classification"][cls.value] += 1
                        method_counts[verdict.method.value] += 1
                        tier_kinds.setdefault(cls.value, Counter())[
                            statement.kind.value
                        ] += 1
                        if cls is Classification.UNKNOWN:
                            unknown_by_kind[statement.kind.value] += 1
                        elif cls is Classification.UNSAFE:
                            unsafe_by_kind[statement.kind.value] += 1
                        rows.append(
                            [
                                path.name,
                                statement.statement_index,
                                statement.kind.value,
                                cls.value,
                                verdict.band.value if verdict.band else None,
                                verdict.method.value,
                            ]
                        )
                    ok, failed, errors = apply_file(conn, script)
                    repo_stats["applied_ok"] += ok
                    repo_stats["apply_failed"] += failed
                    if errors and len(repo_stats["apply_error_samples"]) < 8:
                        repo_stats["apply_error_samples"].extend(errors[:2])
            repo_stats["classification"] = dict(repo_stats["classification"])
            per_repo[repo] = repo_stats
            print(
                f"[{time.monotonic() - started:7.1f}s] {repo}: "
                f"{repo_stats['applied_ok']} applied, "
                f"{repo_stats['apply_failed']} failed, "
                f"cls={repo_stats['classification']}",
                flush=True,
            )

        report = {
            "mode": "online-replay",
            "pg_version": 17,
            "statements": statements,
            "classification": dict(class_counts.most_common()),
            "methods": dict(method_counts.most_common()),
            "unknown_by_kind": dict(unknown_by_kind.most_common(25)),
            "unsafe_by_kind": dict(unsafe_by_kind.most_common(25)),
            "by_kind": {t: dict(c.most_common(20)) for t, c in tier_kinds.items()},
            "rows": rows,
            "parse_failures": parse_failures,
            "capture_failures": capture_failures[:50],
            "capture_failure_count": len(capture_failures),
            "per_repo": per_repo,
            "runtime_s": int(time.monotonic() - started),
        }
        OUT.write_text(json.dumps(report, indent=1), encoding="utf8")
        print("statements", statements)
        print("classification:", report["classification"])
        print("methods:", report["methods"])
    finally:
        stop()


if __name__ == "__main__":
    sys.exit(main())
