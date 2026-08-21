"""Part 2: the scale harness.

Seeds tables at 1k / 100k / 1M / 10M rows (mixed column types, PK + 3
secondary indexes plus a plain btree on title, a 10k-row FK target, a
pre-seeded NOT VALID FK and a valid CHECK (body IS NOT NULL)), ANALYZEs
them, then runs 34 migration
cases — each modeled on a recorded wild-corpus exemplar — against every
size:

  1. capture a read-only snapshot, produce the online assessment;
  2. BEGIN; execute; sample pg_locks throughout (25 ms) plus one
     definitive sample while the transaction is still open; ROLLBACK;
  3. record predicted (classification, lock mode, duration interval)
     beside measured (wall ms, real lock modes, relfilenode rewrite
     ground truth, rowcount for DML).

ROLLBACK keeps cases independent; VACUUM (ANALYZE) after DML cases
reclaims aborted tuples and re-freshens statistics. The server runs
fsync=on / synchronous_commit=on (honest timings on this disk), with
autovacuum off so statistics state is controlled by the harness.
"""

from __future__ import annotations

import json
import os
import pickle
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import psycopg

from pgverdict.catalog.loader import load_catalog
from pgverdict.live import TypeChangeProbe, capture_snapshot
from pgverdict.live.introspect import LiveIntrospectionError
from pgverdict.parser import parse_migration
from pgverdict.verdict import assess_script, snapshot_probes
from pgverdict.verdict.model import CannotEstimate, DurationEstimate

SCRATCH = Path(__file__).parent
PG_BIN = SCRATCH / "pg" / "bin"
OUT = SCRATCH / "scale_results.json"
SNAPSHOT_DIR = SCRATCH / "snapshots"

RO_ROLE = "pgverdict_ro"
RO_PASSWORD = "pgverdict-scale"

SIZES: dict[str, int] = {
    "t_1k": 1_000,
    "t_100k": 100_000,
    "t_1m": 1_000_000,
    "t_10m": 10_000_000,
}

# PGVERDICT_SCALE_SIZES=t_1k,t_100k restricts the run — used to smoke-test
# harness changes in a minute before committing to the full multi-hour run.
if os.environ.get("PGVERDICT_SCALE_SIZES"):
    _wanted = os.environ["PGVERDICT_SCALE_SIZES"].split(",")
    SIZES = {k: v for k, v in SIZES.items() if k in _wanted}
    OUT = SCRATCH / "scale_results_smoke.json"
    SNAPSHOT_DIR = SCRATCH / "snapshots_smoke"

# ---------------------------------------------------------------- cases
# Each case: one statement, adapted to the seeded schema from a real
# wild-corpus statement (source file + original SQL recorded verbatim).
# Execution order per size: constant-time first, then index builds,
# scans/validations, REINDEX, rewrites, and DML (dirtying) last.

CASES: list[dict[str, object]] = [
    # --- catalog-only / fast-path forms -------------------------------
    dict(
        id="add_column_bare",
        sql="ALTER TABLE {t} ADD COLUMN worker_filter text",
        source="boundary__0027__01_server_tags_migrations.up.sql",
        source_sql="alter table target_tcp add column worker_filter wt_bexprfilter",
        rewrite_check=True,
    ),
    dict(
        id="add_column_default_const",
        sql="ALTER TABLE {t} ADD COLUMN purpose text NOT NULL DEFAULT 'recovery'",
        source="boundary__0056__01_nonce.up.sql",
        source_sql="alter table nonce add column purpose text not null default 'recovery' ...",
        rewrite_check=True,
    ),
    dict(
        id="add_fk_not_valid",
        sql=(
            "ALTER TABLE {t} ADD CONSTRAINT {t}_account_fk2 FOREIGN KEY "
            "(account_id) REFERENCES accounts(id) NOT VALID"
        ),
        source="coder__0385__000385_aibridge_fks.up.sql",
        source_sql="ALTER TABLE aibridge_interceptions ADD CONSTRAINT ... FOREIGN KEY ... NOT VALID",
    ),
    dict(
        id="alter_type_varchar_widen",
        sql="ALTER TABLE {t} ALTER COLUMN status TYPE varchar(64)",
        source="boundary__0046__01_wh_user_dimension_oidc.up.sql",
        source_sql="alter table wh_user_dimension alter column auth_method_external_id type wh_dim_text ...",
        rewrite_check=True,
    ),
    dict(
        id="alter_type_varchar_widen_plain_idx",
        sql="ALTER TABLE {t} ALTER COLUMN title TYPE varchar(300)",
        source="boundary__0046__01_wh_user_dimension_oidc.up.sql",
        source_sql="alter table wh_user_dimension alter column ... type wh_dim_text "
        "(typmod-widening form; plain-btree-indexed column variant)",
        rewrite_check=True,
    ),
    dict(
        id="alter_type_varchar_text",
        sql="ALTER TABLE {t} ALTER COLUMN title TYPE text",
        source="boundary__0030__01_job_migrations.up.sql",
        source_sql="alter table job_run alter column server_id type text",
        rewrite_check=True,
    ),
    dict(
        id="alter_type_numeric_widen",
        sql="ALTER TABLE {t} ALTER COLUMN amount TYPE numeric(14,2)",
        source="boundary__0046__01_wh_user_dimension_oidc.up.sql",
        source_sql="alter table wh_user_dimension alter column ... type wh_dim_text (typmod-widening form)",
        rewrite_check=True,
    ),
    dict(
        id="alter_type_ts_tstz",
        sql="ALTER TABLE {t} ALTER COLUMN updated_at TYPE timestamptz",
        source="boundary__0030__01_job_migrations.up.sql",
        source_sql="alter table job_run alter column server_id type text (type-change form; tz variant)",
        rewrite_check=True,
    ),
    dict(
        id="set_not_null_proving_check",
        sql="ALTER TABLE {t} ALTER COLUMN body SET NOT NULL",
        source="boundary__0134__01_credentials.up.sql",
        source_sql="alter table credential_library alter column project_id set not null ...",
    ),
    # --- index builds --------------------------------------------------
    dict(
        id="create_index_single",
        sql="CREATE INDEX {t}_user_id_idx ON {t} (user_id)",
        source="boundary__0004__02_oplog.up.sql",
        source_sql="create index if not exists idx_oplog_metatadata_key on oplog_metadata(key)",
    ),
    dict(
        id="create_index_composite",
        sql="CREATE INDEX {t}_acct_created_idx ON {t} (account_id, created_at)",
        source="boundary__0023__65_wh_session_dimensions.up.sql",
        source_sql="create unique index wh_host_dim_current_constraint on wh_host_dimension (target_id, host_set_id, host_id) ...",
    ),
    dict(
        id="create_index_expr",
        sql="CREATE INDEX {t}_title_lower_idx ON {t} (lower(title))",
        source="discourse (fix_search migration)",
        source_sql="CREATE INDEX index_topics_on_lower_title ON topics (LOWER(title))",
    ),
    dict(
        id="create_index_partial",
        sql="CREATE INDEX {t}_created_active_idx ON {t} (created_at) WHERE is_active",
        source="boundary__0023__65_wh_session_dimensions.up.sql",
        source_sql="create unique index ... where current_row_indicator = 'Current'",
    ),
    dict(
        id="create_index_gin",
        sql="CREATE INDEX {t}_payload_gin ON {t} USING gin (payload)",
        source="coder__0451__000451_chat_labels.up.sql",
        source_sql="CREATE INDEX idx_chats_labels ON chats USING GIN (labels)",
    ),
    dict(
        id="create_unique_index",
        sql="CREATE UNIQUE INDEX {t}_external_id_uq ON {t} (external_id)",
        source="boundary__0023__65_wh_session_dimensions.up.sql",
        source_sql="create unique index wh_host_dim_current_constraint ...",
    ),
    dict(
        id="add_unique_constraint",
        sql="ALTER TABLE {t} ADD CONSTRAINT {t}_ext_uq UNIQUE (external_id)",
        source="boundary__0066__10_auth.up.sql",
        source_sql="alter table auth_method add constraint auth_method_scope_id_name_uq unique (scope_id, name)",
    ),
    # --- scans / validations -------------------------------------------
    dict(
        id="validate_fk",
        sql="ALTER TABLE {t} VALIDATE CONSTRAINT {t}_account_fk_nv",
        source="coder__0385__000385_aibridge_fks.up.sql",
        source_sql="ALTER TABLE aibridge_interceptions VALIDATE CONSTRAINT aibridge_interceptions_initiator_id_fkey",
    ),
    dict(
        id="add_check",
        sql="ALTER TABLE {t} ADD CONSTRAINT {t}_note_not_empty CHECK (length(trim(note)) > 0)",
        source="boundary__0027__01_server_tags_migrations.up.sql",
        source_sql="alter table server add constraint server_id_must_not_be_empty check(length(trim(private_id)) > 0)",
    ),
    dict(
        id="set_not_null_plain",
        sql="ALTER TABLE {t} ALTER COLUMN note SET NOT NULL",
        source="boundary__0134__01_credentials.up.sql",
        source_sql="alter table credential_library alter column project_id set not null ...",
    ),
    dict(
        id="add_fk_plain",
        sql=(
            "ALTER TABLE {t} ADD CONSTRAINT {t}_account_fk FOREIGN KEY "
            "(account_id) REFERENCES accounts(id)"
        ),
        source="boundary__0043__01_server_type_enum.up.sql",
        source_sql="alter table server add constraint server_type_enm_fkey foreign key (type) references server_type_enm(name) ...",
    ),
    dict(
        id="add_fk_cascade",
        sql=(
            "ALTER TABLE {t} ADD CONSTRAINT {t}_account_fk3 FOREIGN KEY "
            "(account_id) REFERENCES accounts(id) ON DELETE CASCADE ON UPDATE CASCADE"
        ),
        source="boundary__0027__01_server_tags_migrations.up.sql",
        source_sql="alter table session add constraint session_server_id_fkey foreign key (server_id) references server(private_id) on delete set null on update cascade",
    ),
    # --- REINDEX --------------------------------------------------------
    dict(
        id="reindex_index",
        sql="REINDEX INDEX {t}_account_id_idx",
        source="lemmy__0251__2025-08-01-000019_add_report_count__up.sql",
        source_sql="REINDEX TABLE post_aggregates (INDEX-scope variant)",
        reindex_bytes_of="{t}_account_id_idx",
    ),
    dict(
        id="reindex_table",
        sql="REINDEX TABLE {t}",
        source="lemmy__0251__2025-08-01-000019_add_report_count__up.sql",
        source_sql="REINDEX TABLE post_aggregates",
    ),
    # --- rewrites -------------------------------------------------------
    dict(
        id="alter_type_int_bigint",
        sql="ALTER TABLE {t} ALTER COLUMN user_id TYPE bigint",
        source="boundary__0030__01_job_migrations.up.sql",
        source_sql="alter table job_run alter column server_id type text (rewriting form)",
        rewrite_check=True,
    ),
    dict(
        id="alter_type_text_varchar",
        sql="ALTER TABLE {t} ALTER COLUMN body TYPE varchar(2000)",
        source="boundary__0030__01_job_migrations.up.sql",
        source_sql="alter table job_run alter column server_id type text (bounding form)",
        rewrite_check=True,
    ),
    dict(
        id="add_column_default_volatile",
        sql="ALTER TABLE {t} ADD COLUMN new_id uuid NOT NULL DEFAULT gen_random_uuid()",
        source="coder__0218__000218_org_custom_role_audit.up.sql",
        source_sql="ALTER TABLE custom_roles ADD COLUMN id uuid DEFAULT gen_random_uuid() NOT NULL",
        rewrite_check=True,
    ),
    dict(
        id="add_column_serial",
        sql="ALTER TABLE {t} ADD COLUMN seq_id BIGSERIAL",
        source="coder__0071__000071_provisioner_log_lines.up.sql",
        source_sql="ALTER TABLE provisioner_job_logs ADD COLUMN id BIGSERIAL PRIMARY KEY (PK part dropped: isolates the rewrite)",
        rewrite_check=True,
    ),
    dict(
        id="add_column_identity",
        sql="ALTER TABLE {t} ADD COLUMN row_num bigint GENERATED ALWAYS AS IDENTITY",
        source="lemmy__0256__2025-08-01-000024_add_person_content_combined_table__up.sql",
        source_sql="ALTER TABLE person_content_combined ADD COLUMN id int PRIMARY KEY GENERATED ALWAYS AS IDENTITY ...",
        rewrite_check=True,
    ),
    dict(
        id="add_column_generated_stored",
        sql=(
            "ALTER TABLE {t} ADD COLUMN score_band text GENERATED ALWAYS AS "
            "(CASE WHEN score > 500 THEN 'high' ELSE 'low' END) STORED"
        ),
        source="coder__0160__000160_provisioner_job_status.up.sql",
        source_sql="ALTER TABLE provisioner_jobs ADD COLUMN job_status ... GENERATED ALWAYS AS (CASE ...) STORED",
        rewrite_check=True,
    ),
    dict(
        id="add_column_domain_default",
        sql="ALTER TABLE {t} ADD COLUMN region label_domain NOT NULL DEFAULT 'us-east-1'",
        source="boundary__0052__04_wh_credential_dimension.up.sql",
        source_sql="alter table wh_session_accumulating_fact add column credential_group_key wh_dim_key not null default 'Unknown' ...",
        rewrite_check=True,
    ),
    # --- DML (dirtying; vacuum+analyze after each) ----------------------
    dict(
        id="update_matched",
        sql="UPDATE {t} SET status = 'archived' WHERE status = 'deleted'",
        source="boundary__0066__10_auth.up.sql",
        source_sql="update auth_method set name = pw.name from auth_password_method pw where ...",
        vacuum_after=True,
    ),
    dict(
        id="update_backfill_null",
        sql="UPDATE {t} SET title = note WHERE title IS NULL",
        source="boundary__0089__01_wh_network_address_dimensions.up.sql",
        source_sql="update wh_host_dimension set network_address_group_key = host_address",
        vacuum_after=True,
    ),
    dict(
        id="update_without_where",
        sql="UPDATE {t} SET score = score + 1",
        source="boundary__0118__06_kms_collection_version.up.sql",
        source_sql="update kms_schema_version set version = 'v0.0.2'",
        vacuum_after=True,
    ),
    dict(
        id="delete_without_where",
        sql="DELETE FROM {t}",
        source="coder__0037__000037_jwt_licenses.up.sql",
        source_sql="DELETE FROM licenses",
        vacuum_after=True,
    ),
]

_PG_MODE_TO_CATALOG = {
    "AccessShareLock": "ACCESS SHARE",
    "RowShareLock": "ROW SHARE",
    "RowExclusiveLock": "ROW EXCLUSIVE",
    "ShareUpdateExclusiveLock": "SHARE UPDATE EXCLUSIVE",
    "ShareLock": "SHARE",
    "ShareRowExclusiveLock": "SHARE ROW EXCLUSIVE",
    "ExclusiveLock": "EXCLUSIVE",
    "AccessExclusiveLock": "ACCESS EXCLUSIVE",
}
_MODE_RANK = {name: rank for rank, name in enumerate(_PG_MODE_TO_CATALOG)}


def start_server() -> tuple[str, object]:
    import socket

    work = Path(tempfile.mkdtemp(prefix="pgverdict-scale-"))
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
        f"-p {port} -c listen_addresses=127.0.0.1 "
        f"-c shared_buffers=512MB -c maintenance_work_mem=256MB "
        f"-c work_mem=64MB -c max_wal_size=8GB -c autovacuum=off "
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


SEED_SQL = """
INSERT INTO {t} (id, account_id, user_id, status, title, body, score, amount,
                 is_active, payload, external_id, created_at, updated_at, note)
SELECT g,
       1 + (g % 10000),
       CASE WHEN g % 7 = 0 THEN NULL ELSE (g % 100000)::int END,
       (ARRAY['active','pending','archived','deleted','draft','review'])[1 + g % 6],
       CASE WHEN g % 11 = 0 THEN NULL ELSE 'Title ' || g END,
       'body text for row ' || g || ' ' || repeat('lorem ipsum ', 8),
       (g % 1000)::int,
       ((g % 100000)::numeric) / 100,
       g % 10 <> 0,
       CASE WHEN g % 3 = 0 THEN NULL
            ELSE jsonb_build_object('k', g % 50, 'source', 'seed',
                                    'tags', jsonb_build_array(g % 7, g % 11)) END,
       gen_random_uuid(),
       timestamptz '2024-01-01 00:00:00+00' + (g % 730) * interval '1 hour',
       timestamp '2024-01-01 00:00:00' + (g % 730) * interval '1 hour',
       'note ' || g
FROM generate_series(1, {n}) g
"""


def seed(admin_dsn: str, db: str) -> dict[str, int]:
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(f"CREATE DATABASE {db}")
    dsn = admin_dsn.rsplit("/", 1)[0] + "/" + db
    sizes_bytes: dict[str, int] = {}
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("SET synchronous_commit = off")
        conn.execute(
            "CREATE TABLE accounts (id integer PRIMARY KEY, name text NOT NULL)"
        )
        conn.execute(
            "INSERT INTO accounts SELECT g, 'account-' || g "
            "FROM generate_series(1, 10000) g"
        )
        conn.execute("CREATE DOMAIN label_domain AS text CHECK (length(VALUE) < 100)")
        for t, n in SIZES.items():
            started = time.monotonic()
            conn.execute(
                f"""
                CREATE TABLE {t} (
                  id bigint PRIMARY KEY,
                  account_id integer NOT NULL,
                  user_id integer,
                  status varchar(32) NOT NULL,
                  title varchar(255),
                  body text,
                  score integer NOT NULL,
                  amount numeric(12,2),
                  is_active boolean NOT NULL DEFAULT true,
                  payload jsonb,
                  external_id uuid,
                  created_at timestamptz NOT NULL,
                  updated_at timestamp,
                  note text,
                  CONSTRAINT {t}_body_check CHECK (body IS NOT NULL)
                )
                """
            )
            conn.execute(SEED_SQL.format(t=t, n=n))
            # NOT VALID must be added after the rows exist: inside CREATE
            # TABLE, Postgres creates the constraint already-validated.
            conn.execute(
                f"ALTER TABLE {t} ADD CONSTRAINT {t}_account_fk_nv FOREIGN KEY "
                f"(account_id) REFERENCES accounts(id) NOT VALID"
            )
            validated = conn.execute(
                "SELECT convalidated FROM pg_constraint WHERE conname = %s",
                (f"{t}_account_fk_nv",),
            ).fetchone()[0]
            if validated:
                raise RuntimeError(f"{t}_account_fk_nv unexpectedly validated")
            conn.execute(f"CREATE INDEX {t}_account_id_idx ON {t} (account_id)")
            conn.execute(f"CREATE INDEX {t}_created_at_idx ON {t} (created_at)")
            conn.execute(
                f"CREATE INDEX {t}_status_active_idx ON {t} (status) WHERE is_active"
            )
            # Fifth index: a plain-key default-opclass btree, the shape
            # CheckIndexCompatible reuses. Without it the harness only
            # exercises the rebuild half of the no-rewrite narrowing.
            conn.execute(f"CREATE INDEX {t}_title_idx ON {t} (title)")
            conn.execute(f"VACUUM (ANALYZE) {t}")
            row = conn.execute(
                f"SELECT pg_relation_size('{t}'), pg_total_relation_size('{t}')"
            ).fetchone()
            sizes_bytes[t] = row[0]
            print(
                f"  seeded {t}: {n} rows, heap {row[0] // (1 << 20)} MiB, "
                f"total {row[1] // (1 << 20)} MiB in "
                f"{time.monotonic() - started:.0f}s",
                flush=True,
            )
        conn.execute("VACUUM (ANALYZE) accounts")
    return sizes_bytes


class LockMonitor:
    """Samples pg_locks for one PID from its own connection every 25 ms."""

    def __init__(self, dsn: str, pid: int) -> None:
        self._conn = psycopg.connect(dsn, autocommit=True)
        self._pid = pid
        self._stop = threading.Event()
        self._lock = threading.Lock()  # final_sample() races the poll thread
        self.samples: dict[tuple[str, str], int] = {}  # (rel, mode) -> count
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _query(self) -> list[tuple[str, str]]:
        with self._lock:
            return self._query_locked()

    def _query_locked(self) -> list[tuple[str, str]]:
        rows = self._conn.execute(
            "SELECT coalesce(c.relname, l.relation::text) AS rel, l.mode "
            "FROM pg_locks l LEFT JOIN pg_class c ON c.oid = l.relation "
            "WHERE l.pid = %s AND l.locktype = 'relation' AND l.granted",
            (self._pid,),
        ).fetchall()
        return [(r[0], r[1]) for r in rows]

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                for key in self._query():
                    self.samples[key] = self.samples.get(key, 0) + 1
            except psycopg.Error:
                return
            self._stop.wait(0.025)

    def start(self) -> None:
        self._thread.start()

    def final_sample(self) -> list[tuple[str, str]]:
        """One definitive sample; call while the transaction is still open."""
        try:
            return self._query()
        except psycopg.Error:
            return []

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
        try:
            self._conn.close()
        except psycopg.Error:
            pass


def duration_rows(assessment) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    for row in assessment.rows:
        d = row.duration
        if isinstance(d, DurationEstimate):
            out.append(
                dict(
                    kind=row.kind.value,
                    lock_mode=row.lock_mode.value,
                    point_ms=d.point_ms,
                    low_ms=d.low_ms,
                    high_ms=d.high_ms,
                    confidence=d.confidence,
                    constant_key=d.constant_key,
                    method=d.method.value,
                )
            )
        elif isinstance(d, CannotEstimate):
            out.append(
                dict(kind=row.kind.value, lock_mode=row.lock_mode.value,
                     cannot_estimate=d.reason[:220])
            )
    return out


def main() -> None:
    global OUT
    if "--smoke" in sys.argv:
        SIZES.clear()
        SIZES.update({"t_1k": 1_000, "t_20k": 20_000})
        OUT = SCRATCH / "scale_results_smoke.json"
    catalog = load_catalog()
    admin_dsn, stop = start_server()
    started = time.monotonic()
    results: list[dict[str, object]] = []
    try:
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            admin.execute(f"CREATE ROLE {RO_ROLE} LOGIN PASSWORD '{RO_PASSWORD}'")
            admin.execute(f"GRANT pg_monitor TO {RO_ROLE}")
        print("seeding...", flush=True)
        heap_bytes = seed(admin_dsn, "scale")
        dsn = admin_dsn.rsplit("/", 1)[0] + "/scale"
        ro_dsn = dsn.replace("postgres@", f"{RO_ROLE}:{RO_PASSWORD}@")

        with psycopg.connect(dsn, autocommit=True) as inspector:
            for t, n in SIZES.items():
                apply_conn = psycopg.connect(dsn)
                apply_conn.execute("SET TIME ZONE 'UTC'")
                apply_conn.execute("SET statement_timeout = '1800s'")
                apply_conn.commit()
                apply_pid = apply_conn.execute("SELECT pg_backend_pid()").fetchone()[0]
                apply_conn.commit()

                for case in CASES:
                    case_id = case["id"]
                    sql = str(case["sql"]).format(t=t)
                    record: dict[str, object] = dict(
                        case=case_id, table=t, rows=n, sql=sql,
                        source=case["source"],
                    )
                    # 1. assess against a pre-migration snapshot.
                    script = parse_migration(sql + ";\n")
                    probes = snapshot_probes(script)
                    snapshot = None
                    try:
                        snapshot = capture_snapshot(
                            ro_dsn,
                            probes.relations,
                            functions=probes.functions,
                            types=probes.types,
                            type_changes=[
                                TypeChangeProbe(relation=r, column=c, new_type=ty)
                                for r, c, ty in probes.type_changes
                            ],
                            connect_timeout_s=5,
                            statement_timeout_ms=8000,
                            lock_timeout_ms=2000,
                        )
                    except (LiveIntrospectionError, psycopg.Error) as exc:
                        record["capture_error"] = str(exc)[:200]
                    if snapshot is not None:
                        SNAPSHOT_DIR.mkdir(exist_ok=True)
                        with (SNAPSHOT_DIR / f"{case_id}__{t}.pkl").open("wb") as fh:
                            pickle.dump((case_id, t, n, sql, snapshot), fh)
                    assessment = assess_script(script, catalog, 17, snapshot)
                    st = assessment.statements[0]
                    record["predicted"] = dict(
                        classification=st.verdict.classification.value,
                        band=st.verdict.band.value if st.verdict.band else None,
                        method=st.verdict.method.value,
                        rationale=st.verdict.rationale[:300],
                        conditions=[c[:200] for c in st.verdict.conditions],
                        statement_lock_mode=st.statement_lock_mode.value,
                        durations=duration_rows(st),
                        narrowings=[
                            nar[:200] for row in st.rows for nar in row.narrowings
                        ],
                    )
                    # context facts
                    if case.get("reindex_bytes_of"):
                        idx = str(case["reindex_bytes_of"]).format(t=t)
                        record["index_bytes"] = inspector.execute(
                            "SELECT pg_relation_size(%s::regclass)", (idx,)
                        ).fetchone()[0]
                    filenode_before = None
                    if case.get("rewrite_check"):
                        filenode_before = inspector.execute(
                            "SELECT relfilenode FROM pg_class WHERE oid = %s::regclass",
                            (t,),
                        ).fetchone()[0]

                    # 2. apply inside BEGIN...ROLLBACK, sampling pg_locks.
                    monitor = LockMonitor(dsn, apply_pid)
                    monitor.start()
                    error = None
                    measured_ms = None
                    rowcount = None
                    try:
                        cur = apply_conn.cursor()
                        t0 = time.perf_counter()
                        cur.execute(sql)
                        t1 = time.perf_counter()
                        measured_ms = int((t1 - t0) * 1000)
                        rowcount = cur.rowcount if cur.rowcount >= 0 else None
                        final = monitor.final_sample()
                        if case.get("rewrite_check"):
                            node = apply_conn.execute(
                                "SELECT relfilenode FROM pg_class WHERE oid = %s::regclass",
                                (t,),
                            ).fetchone()[0]
                            record["rewrote_table"] = node != filenode_before
                    except psycopg.Error as exc:
                        error = str(exc).split("\n")[0][:200]
                        final = []
                    finally:
                        try:
                            apply_conn.rollback()
                        except psycopg.Error:
                            apply_conn.close()
                            apply_conn = psycopg.connect(dsn)
                            apply_conn.execute("SET TIME ZONE 'UTC'")
                            apply_conn.execute("SET statement_timeout = '1800s'")
                            apply_conn.commit()
                            apply_pid = apply_conn.execute(
                                "SELECT pg_backend_pid()"
                            ).fetchone()[0]
                            apply_conn.commit()
                        monitor.stop()

                    target_modes = sorted(
                        {m for (rel, m) in monitor.samples if rel == t},
                        key=lambda m: _MODE_RANK.get(m, -1),
                    )
                    final_modes = sorted(
                        {m for (rel, m) in final if rel == t},
                        key=lambda m: _MODE_RANK.get(m, -1),
                    )
                    other_rels = sorted(
                        {
                            f"{rel}:{m}"
                            for (rel, m) in set(monitor.samples) | set(final)
                            if rel != t and _MODE_RANK.get(m, 0) >= 2
                        }
                    )
                    record["measured"] = dict(
                        ms=measured_ms,
                        rowcount=rowcount,
                        error=error,
                        table_modes_sampled=target_modes,
                        table_modes_final=final_modes,
                        strongest_table_mode=_PG_MODE_TO_CATALOG.get(
                            (final_modes or target_modes)[-1]
                        )
                        if (final_modes or target_modes)
                        else None,
                        other_locks=other_rels[:12],
                    )
                    if case.get("vacuum_after"):
                        v0 = time.monotonic()
                        inspector.execute(f"VACUUM (ANALYZE) {t}")
                        record["vacuum_after_s"] = int(time.monotonic() - v0)
                    results.append(record)
                    pred = record["predicted"]
                    print(
                        f"[{time.monotonic() - started:7.1f}s] {t} {case_id}: "
                        f"pred={pred['classification']} measured="
                        f"{measured_ms}ms err={error}",
                        flush=True,
                    )
                    OUT.write_text(
                        json.dumps(
                            dict(heap_bytes=heap_bytes, results=results), indent=1
                        ),
                        encoding="utf8",
                    )
                apply_conn.close()
        print(f"done in {time.monotonic() - started:.0f}s", flush=True)
    finally:
        stop()


if __name__ == "__main__":
    sys.exit(main())
