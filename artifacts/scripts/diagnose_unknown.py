"""Part 1: diagnose the online replay's UNKNOWN rate by cause.

Same replay as corpus_replay.py (fresh PG 17.10, per-repo chains, capture a
read-only snapshot before each file, assess online, then apply), but each
file is assessed TWICE:

  variant "vanilla"  — snapshot of the database exactly as the replay left it
                       (what the original online run saw);
  variant "analyzed" — after ANALYZE of every probed relation that exists,
                       so statistics are present and fresh.

Statements that are UNKNOWN in vanilla but decided in analyzed are pure
stats-freshness replay artifacts (never-analyzed / swamped). Every UNKNOWN
rationale is also bucketed by its cause string, both variants.
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

from blastoise.catalog.loader import load_catalog
from blastoise.live import TypeChangeProbe, capture_snapshot
from blastoise.live.introspect import LiveIntrospectionError
from blastoise.parser import MigrationParseError, parse_migration_file
from blastoise.verdict import Classification, assess_script, snapshot_probes

SCRATCH = Path(__file__).parent
CORPUS = SCRATCH / "corpus"
PG_BIN = SCRATCH / "pg" / "bin"
OUT = SCRATCH / "unknown_diagnosis.json"

RO_ROLE = "blastoise_ro"
RO_PASSWORD = "blastoise-replay"

_ROLE_RE = re.compile(r'role "([^"]+)" does not exist')

# Cause buckets, matched in order against the full statement rationale.
# Inner CannotEstimate reasons are embedded in the rationale text, so a
# matched-DML UNKNOWN whose real cause is a missing relation lands in
# missing_relation, not in a generic DML bucket.
BUCKET_RULES: tuple[tuple[str, str], ...] = (
    ("missing_relation", "does not exist on the target database"),
    ("existence_unknown", "existence of"),
    ("not_captured", "was not captured in the snapshot"),
    ("never_analyzed", "never been vacuumed or analyzed"),
    ("never_analyzed", "reltuples is -1"),
    ("never_analyzed", "never analyzed"),
    ("swamped_stats", "statistics unusable"),
    ("uncalibrated_row", "uncalibrated stub"),
    ("unmodeled_form", "statement form is not modeled"),
    ("opaque_do_block", "builds SQL at runtime"),
    ("empty_do_block", "yielded no analyzable inner statements"),
    ("live_context_unsupplied", "could not supply the declared live context"),
    ("live_context_unsupplied", "the snapshot could not supply"),
    ("no_throughput_constant", "no throughput constant"),
    ("no_relation_named", "names no relation to size the work"),
)


def bucket_of(rationale: str) -> str:
    for name, needle in BUCKET_RULES:
        if needle in rationale:
            return name
    return "other"


def start_server() -> tuple[str, object]:
    import socket

    work = Path(tempfile.mkdtemp(prefix="blastoise-diag-"))
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


def apply_file(conn: psycopg.Connection, script) -> tuple[int, int]:
    ok = 0
    failed = 0
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
                        continue
                    except psycopg.Error:
                        pass
                failed += 1
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
    return ok, failed


def capture(ro_dsn: str, probes):
    try:
        return capture_snapshot(
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
    except (LiveIntrospectionError, psycopg.Error):
        return None


def main() -> None:
    catalog = load_catalog()
    admin_dsn, stop = start_server()
    started = time.monotonic()
    try:
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            admin.execute(f"CREATE ROLE {RO_ROLE} LOGIN PASSWORD '{RO_PASSWORD}'")
            admin.execute(f"GRANT pg_monitor TO {RO_ROLE}")

        cls_v: Counter[str] = Counter()
        cls_a: Counter[str] = Counter()
        bucket_v: Counter[str] = Counter()
        bucket_a: Counter[str] = Counter()
        bucket_kind_v: dict[str, Counter[str]] = defaultdict(Counter)
        bucket_repo_v: dict[str, Counter[str]] = defaultdict(Counter)
        transitions: Counter[str] = Counter()  # vanilla UNKNOWN -> analyzed class
        trans_by_bucket: dict[str, Counter[str]] = defaultdict(Counter)
        live_ctx_detail_v: Counter[str] = Counter()
        samples: dict[str, list[dict[str, str]]] = defaultdict(list)
        statements = 0

        only = set(sys.argv[1:])
        for repo, files in sorted(repo_files().items()):
            if only and repo not in only:
                continue
            db = "diag_" + re.sub(r"[^a-z0-9_]", "_", repo.lower())
            with psycopg.connect(admin_dsn, autocommit=True) as admin:
                admin.execute(f"CREATE DATABASE {db}")
            base = admin_dsn.rsplit("/", 1)[0]
            apply_dsn = f"{base}/{db}"
            ro_dsn = f"{base}/{db}".replace("postgres@", f"{RO_ROLE}:{RO_PASSWORD}@")
            with psycopg.connect(apply_dsn, autocommit=True) as conn:
                conn.execute("SET statement_timeout = '30s'")
                for path in files:
                    try:
                        script = parse_migration_file(str(path))
                    except MigrationParseError:
                        continue
                    probes = snapshot_probes(script)
                    snap1 = capture(ro_dsn, probes)
                    res1 = assess_script(script, catalog, 17, snap1)
                    for rel in probes.relations:
                        try:
                            conn.execute(f"ANALYZE {rel}")
                        except psycopg.Error:
                            pass
                    snap2 = capture(ro_dsn, probes)
                    res2 = assess_script(script, catalog, 17, snap2)
                    for st1, st2 in zip(res1.statements, res2.statements):
                        statements += 1
                        c1 = st1.verdict.classification
                        c2 = st2.verdict.classification
                        cls_v[c1.value] += 1
                        cls_a[c2.value] += 1
                        if c1 is Classification.UNKNOWN:
                            b = bucket_of(st1.verdict.rationale)
                            bucket_v[b] += 1
                            bucket_kind_v[b][st1.kind.value] += 1
                            bucket_repo_v[b][repo] += 1
                            transitions[c2.value] += 1
                            trans_by_bucket[b][c2.value] += 1
                            if b == "live_context_unsupplied":
                                m = re.search(
                                    r"live context \(([^)]*)", st1.verdict.rationale
                                )
                                live_ctx_detail_v[
                                    (m.group(1)[:100] if m else st1.verdict.rationale[:100])
                                ] += 1
                            if len(samples[b]) < 12:
                                samples[b].append(
                                    {
                                        "repo": repo,
                                        "file": path.name,
                                        "kind": st1.kind.value,
                                        "rationale": st1.verdict.rationale[:300],
                                        "analyzed_class": c2.value,
                                        "sql": st1.sql[:160],
                                    }
                                )
                        if c2 is Classification.UNKNOWN:
                            bucket_a[bucket_of(st2.verdict.rationale)] += 1
                    apply_file(conn, script)
            print(
                f"[{time.monotonic() - started:7.1f}s] {repo}: "
                f"vanilla={dict(cls_v)} analyzed={dict(cls_a)}",
                flush=True,
            )

        report = {
            "mode": "unknown-diagnosis",
            "statements": statements,
            "classification_vanilla": dict(cls_v.most_common()),
            "classification_analyzed": dict(cls_a.most_common()),
            "unknown_buckets_vanilla": dict(bucket_v.most_common()),
            "unknown_buckets_analyzed": dict(bucket_a.most_common()),
            "bucket_kinds_vanilla": {
                b: dict(c.most_common(12)) for b, c in bucket_kind_v.items()
            },
            "bucket_repos_vanilla": {
                b: dict(c.most_common(8)) for b, c in bucket_repo_v.items()
            },
            "vanilla_unknown_to_analyzed": dict(transitions.most_common()),
            "transitions_by_bucket": {
                b: dict(c.most_common()) for b, c in trans_by_bucket.items()
            },
            "live_context_detail_vanilla": dict(live_ctx_detail_v.most_common(20)),
            "samples": samples,
            "runtime_s": int(time.monotonic() - started),
        }
        OUT.write_text(json.dumps(report, indent=1), encoding="utf8")
        print("done:", statements, "statements")
        print("vanilla:", report["classification_vanilla"])
        print("analyzed:", report["classification_analyzed"])
        print("buckets:", report["unknown_buckets_vanilla"])
    finally:
        stop()


if __name__ == "__main__":
    sys.exit(main())
