"""Database fixtures: the disposable server, the seeded reference tables,
and the concurrent-activity machinery (idle holders, visible waiters, a
read/write traffic probe, the pg_locks monitor).

The seeded schema is the scale harness's, verbatim — same 14 columns, same
five indexes, same NOT VALID FK, same proving CHECK, same constrained
domain, same seed expression — so that the calibration probe's readings
are comparable to the committed reference runs. Added on top, because the
corpus needs them to pre-exist: an enum type, an unconstrained domain, a
view, a function, a trigger function, a sequence, and a small table with
an enum column.
"""

from __future__ import annotations

import contextlib
import os
import socket
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path

import psycopg

from validation.harness.labeling import catalog_mode

RO_ROLE = "blastoise_ro"
RO_PASSWORD = "blastoise-validate"

SERVER_OPTIONS = (
    "-c listen_addresses=127.0.0.1 -c shared_buffers=512MB -c maintenance_work_mem=256MB "
    "-c work_mem=64MB -c max_wal_size=8GB -c autovacuum=off -c max_connections=50"
)


def find_pg_bin(explicit: str | None) -> Path:
    for candidate in (
        explicit,
        os.environ.get("BLASTOISE_TEST_PG_BIN"),
        os.environ.get("BLASTOISE_VALIDATION_PG_BIN"),
    ):
        initdb = "initdb.exe" if os.name == "nt" else "initdb"
        if candidate and (Path(candidate) / initdb).exists():
            return Path(candidate)
    raise SystemExit(
        "no Postgres binaries: pass --pg-bin or set BLASTOISE_TEST_PG_BIN to a bin/ "
        "directory holding initdb and pg_ctl"
    )


def start_server(pg_bin: Path) -> tuple[str, Callable[[], None], Path]:
    exe = ".exe" if os.name == "nt" else ""
    # Under the repo's scratch/ when it exists: a run lasts an hour and a
    # temp-folder cleanup killed one mid-flight (2026-08-22).
    scratch = Path(__file__).resolve().parent.parent.parent / "scratch"
    work = Path(
        tempfile.mkdtemp(prefix="blastoise-validate-", dir=scratch if scratch.is_dir() else None)
    )
    data = work / "data"
    log = work / "server.log"
    subprocess.run(
        [str(pg_bin / f"initdb{exe}"), "-D", str(data), "-U", "postgres", "-A", "trust",
         "-E", "UTF8", "--no-sync"],
        check=True,
        capture_output=True,
    )
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    # No capture_output: the daemonized postmaster inherits the pipe on
    # Windows and run() would wait forever (DECISIONS.md, live layer).
    subprocess.run(
        [str(pg_bin / f"pg_ctl{exe}"), "-D", str(data), "-l", str(log), "-w",
         "-o", f"-p {port} {SERVER_OPTIONS}", "start"],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    def stop() -> None:
        subprocess.run(
            [str(pg_bin / f"pg_ctl{exe}"), "-D", str(data), "-m", "immediate", "stop"],
            check=False,
            capture_output=True,
        )

    return f"postgresql://postgres@127.0.0.1:{port}/postgres", stop, work


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

TABLE_SQL = """
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

# Objects the corpus needs to pre-exist beyond the reference tables.
FIXTURE_OBJECTS_SQL: tuple[str, ...] = (
    "CREATE TYPE ticket_status AS ENUM ('open', 'closed')",
    "CREATE DOMAIN plain_domain AS text",
    "CREATE TABLE tickets (id bigint PRIMARY KEY, status ticket_status NOT NULL, "
    "note text)",
    "INSERT INTO tickets SELECT g, (CASE WHEN g % 2 = 0 THEN 'open' ELSE 'closed' END)"
    "::ticket_status, 'n' || g FROM generate_series(1, 1000) g",
    "CREATE VIEW t_1k_active AS SELECT id, title FROM t_1k WHERE is_active",
    "CREATE FUNCTION fixture_add(a integer, b integer) RETURNS integer "
    "LANGUAGE sql IMMUTABLE AS 'SELECT a + b'",
    "CREATE FUNCTION fixture_touch() RETURNS trigger LANGUAGE plpgsql AS "
    "$$ BEGIN NEW.note := NEW.note; RETURN NEW; END $$",
    "CREATE SEQUENCE fixture_seq",
    "CREATE TABLE empty_pre (id bigint PRIMARY KEY, label text)",
    "CREATE TABLE orphan_child (id bigint PRIMARY KEY, parent_id integer)",
    "INSERT INTO orphan_child SELECT g, 1 + (g % 10000) FROM generate_series(1, 50000) g",
)


def seed(admin_dsn: str, db: str, sizes: dict[str, int], log: bool = True) -> dict[str, int]:
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(f"CREATE DATABASE {db}")
    dsn = admin_dsn.rsplit("/", 1)[0] + "/" + db
    heap_bytes: dict[str, int] = {}
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("SET synchronous_commit = off")
        conn.execute("CREATE TABLE accounts (id integer PRIMARY KEY, name text NOT NULL)")
        conn.execute(
            "INSERT INTO accounts SELECT g, 'account-' || g FROM generate_series(1, 10000) g"
        )
        conn.execute("CREATE DOMAIN label_domain AS text CHECK (length(VALUE) < 100)")
        for t, n in sizes.items():
            started = time.monotonic()
            conn.execute(TABLE_SQL.format(t=t))
            conn.execute(SEED_SQL.format(t=t, n=n))
            conn.execute(
                f"ALTER TABLE {t} ADD CONSTRAINT {t}_account_fk_nv FOREIGN KEY "
                f"(account_id) REFERENCES accounts(id) NOT VALID"
            )
            row = conn.execute(
                "SELECT convalidated FROM pg_constraint WHERE conname = %s",
                (f"{t}_account_fk_nv",),
            ).fetchone()
            if row is None or row[0]:
                raise RuntimeError(f"{t}_account_fk_nv unexpectedly validated")
            conn.execute(f"CREATE INDEX {t}_account_id_idx ON {t} (account_id)")
            conn.execute(f"CREATE INDEX {t}_created_at_idx ON {t} (created_at)")
            conn.execute(f"CREATE INDEX {t}_status_active_idx ON {t} (status) WHERE is_active")
            conn.execute(f"CREATE INDEX {t}_title_idx ON {t} (title)")
            conn.execute(f"VACUUM (ANALYZE) {t}")
            size = conn.execute(
                f"SELECT pg_relation_size('{t}'), pg_total_relation_size('{t}')"
            ).fetchone()
            assert size is not None
            heap_bytes[t] = int(size[0])
            if log:
                print(
                    f"  seeded {t}: {n} rows, heap {size[0] // (1 << 20)} MiB, total "
                    f"{size[1] // (1 << 20)} MiB in {time.monotonic() - started:.0f}s",
                    flush=True,
                )
        conn.execute("VACUUM (ANALYZE) accounts")
        for statement in FIXTURE_OBJECTS_SQL:
            conn.execute(statement)
        conn.execute("VACUUM (ANALYZE) tickets")
        conn.execute("VACUUM (ANALYZE) orphan_child")
        conn.execute("VACUUM (ANALYZE) empty_pre")
    return heap_bytes


def settle(dsn: str) -> None:
    """Checkpoint and flush after seeding: the first run's start probe ran
    on 1.5 GB of freshly dirtied buffers and read 1.44x, the end probe
    0.69x. Calibration should measure the machine, not the seed's wake."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("CHECKPOINT")
        conn.execute("SELECT count(*) FROM t_1m")  # warm the 1M heap the probes read
        conn.execute("CHECKPOINT")


def preexisting_relations(dsn: str) -> set[str]:
    """Every relation (tables, indexes, views, sequences...) in ``public`` right now."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public'"
        ).fetchall()
    return {str(r[0]) for r in rows}


class LockMonitor:
    """Samples pg_locks for one PID every 25 ms from its own connection.

    Records granted (relation, mode) pairs with counts, and the first moment
    any granted table-level lock on the target set appeared — which, minus
    the statement's start, is the lock *wait*.
    """

    def __init__(self, dsn: str, pid: int, targets: set[str]) -> None:
        self._conn = psycopg.connect(dsn, autocommit=True)
        self._pid = pid
        self._targets = targets
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.samples: dict[tuple[str, str], int] = {}
        self.first_granted_at: float | None = None
        self.last_waiting_at: float | None = None
        self.saw_waiting = False
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _query(self) -> list[tuple[str, str, bool]]:
        # An index with indislive = false (the last phase of DROP INDEX
        # CONCURRENTLY) is invisible to every other session; a lock on it
        # cannot block anyone and is not a lock on a relation others can see.
        with self._lock:
            rows = self._conn.execute(
                "SELECT coalesce(c.relname, l.relation::text), l.mode, l.granted "
                "FROM pg_locks l LEFT JOIN pg_class c ON c.oid = l.relation "
                "LEFT JOIN pg_index i ON i.indexrelid = l.relation "
                "WHERE l.pid = %s AND l.locktype = 'relation' "
                "AND coalesce(i.indislive, true)",
                (self._pid,),
            ).fetchall()
        return [(str(r[0]), str(r[1]), bool(r[2])) for r in rows]

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                for rel, mode, granted in self._query():
                    if not granted:
                        self.saw_waiting = True
                        self.last_waiting_at = time.perf_counter()
                        continue
                    self.samples[(rel, mode)] = self.samples.get((rel, mode), 0) + 1
                    if self.first_granted_at is None and rel in self._targets:
                        self.first_granted_at = time.perf_counter()
            except psycopg.Error:
                return
            self._stop.wait(0.025)

    def start(self) -> None:
        self._thread.start()

    def final_sample(self) -> list[tuple[str, str]]:
        try:
            return [(rel, mode) for rel, mode, granted in self._query() if granted]
        except psycopg.Error:
            return []

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
        with contextlib.suppress(psycopg.Error):
            self._conn.close()

    def modes_on(self, relations: set[str], final: list[tuple[str, str]]) -> dict[str, list[str]]:
        """Catalog-spelled modes per relation, for relations in ``relations``."""
        out: dict[str, set[str]] = {}
        for rel, mode in set(self.samples) | set(final):
            if rel in relations:
                out.setdefault(rel, set()).add(catalog_mode(mode))
        return {rel: sorted(modes) for rel, modes in sorted(out.items())}


class IdleHolder(threading.Thread):
    """A session that runs ``sql`` inside a transaction, then idles.

    ``ready`` fires once the statement has run (locks held). The hold of
    ``hold_s`` is counted from ``start_timer`` — set by the runner right
    before it executes the migration statement — so the measured lock wait
    is the configured hold and not the hold minus whatever the snapshot
    capture took. ``release`` ends the hold early.
    """

    def __init__(self, dsn: str, sql: str, hold_s: float) -> None:
        super().__init__(daemon=True)
        self._dsn = dsn
        self._sql = sql
        self._hold_s = hold_s
        self.ready = threading.Event()
        self.start_timer = threading.Event()
        self.release = threading.Event()
        self.pid: int | None = None
        self.error: str | None = None

    def run(self) -> None:
        try:
            with psycopg.connect(self._dsn) as conn:
                row = conn.execute("SELECT pg_backend_pid()").fetchone()
                self.pid = int(row[0]) if row else None
                conn.commit()
                conn.execute(self._sql)
                self.ready.set()
                self.start_timer.wait(600)
                self.release.wait(self._hold_s)
                conn.rollback()
        except psycopg.Error as exc:
            self.error = str(exc).split("\n")[0]
            self.ready.set()


class VisibleWaiter(threading.Thread):
    """A session parked behind the holder requesting ACCESS EXCLUSIVE, so that
    pg_locks shows a waiter; it rolls back the instant it is granted."""

    def __init__(self, dsn: str, table: str) -> None:
        super().__init__(daemon=True)
        self._dsn = dsn
        self._table = table
        self.pid: int | None = None
        self.error: str | None = None

    def run(self) -> None:
        try:
            with psycopg.connect(self._dsn) as conn:
                row = conn.execute("SELECT pg_backend_pid()").fetchone()
                self.pid = int(row[0]) if row else None
                conn.commit()
                conn.execute("SET statement_timeout = '600s'")
                conn.execute(f"LOCK TABLE {self._table} IN ACCESS EXCLUSIVE MODE")
                conn.rollback()
        except psycopg.Error as exc:
            self.error = str(exc).split("\n")[0]


def wait_for_waiter(dsn: str, pid: int | None, timeout_s: float = 10.0) -> bool:
    if pid is None:
        return False
    deadline = time.monotonic() + timeout_s
    with psycopg.connect(dsn, autocommit=True) as conn:
        while time.monotonic() < deadline:
            row = conn.execute(
                "SELECT count(*) FROM pg_locks WHERE pid = %s AND NOT granted", (pid,)
            ).fetchone()
            if row and int(row[0]) > 0:
                return True
            time.sleep(0.05)
    return False


class TrafficProbe(threading.Thread):
    """Production stand-in: a point SELECT and a point UPDATE on the target
    table every ~40 ms, recording the worst latency of each. A read stall
    is what a blocked reader would experience; a write stall, a blocked
    writer. Paused while the snapshot is captured so its own locks never
    appear in the facts the engine sees."""

    def __init__(self, dsn: str) -> None:
        super().__init__(daemon=True)
        self._dsn = dsn
        self._table: str | None = None
        self._enabled = threading.Event()
        self._halt = threading.Event()
        self._lock = threading.Lock()
        self.max_read_ms = 0
        self.max_write_ms = 0
        self.errors = 0
        self._inflight: tuple[str, float] | None = None  # ("read"|"write", started_at)

    def target(self, table: str | None) -> None:
        with self._lock:
            self._table = table
            self.max_read_ms = 0
            self.max_write_ms = 0
            self.errors = 0

    def stalls(self) -> tuple[int, int]:
        """(max read stall, max write stall) in ms, including a query that is
        still stuck right now — a reader parked behind an ACCESS EXCLUSIVE
        request only returns after the migration's transaction ends."""
        with self._lock:
            read, write = self.max_read_ms, self.max_write_ms
            if self._inflight is not None:
                kind, since = self._inflight
                elapsed = int((time.perf_counter() - since) * 1000)
                if kind == "read":
                    read = max(read, elapsed)
                else:
                    write = max(write, elapsed)
        return read, write

    def resume(self) -> None:
        self._enabled.set()

    def pause(self) -> None:
        self._enabled.clear()

    def stop(self) -> None:
        self._halt.set()
        self._enabled.set()

    def run(self) -> None:
        conn = psycopg.connect(self._dsn, autocommit=True)
        conn.execute("SET statement_timeout = '1200s'")
        while not self._halt.is_set():
            if not self._enabled.wait(0.5):
                continue
            if self._halt.is_set():
                break
            with self._lock:
                table = self._table
            if table is None:
                time.sleep(0.04)
                continue
            try:
                t0 = time.perf_counter()
                with self._lock:
                    self._inflight = ("read", t0)
                conn.execute(f"SELECT id FROM {table} WHERE id = 1")
                read_ms = int((time.perf_counter() - t0) * 1000)
                t0 = time.perf_counter()
                with self._lock:
                    if self._table == table:
                        self.max_read_ms = max(self.max_read_ms, read_ms)
                    self._inflight = ("write", t0)
                conn.execute(f"UPDATE {table} SET score = score WHERE id = 1")
                write_ms = int((time.perf_counter() - t0) * 1000)
                with self._lock:
                    if self._table == table:
                        self.max_write_ms = max(self.max_write_ms, write_ms)
                    self._inflight = None
            except psycopg.Error:
                self.errors += 1
                with self._lock:
                    self._inflight = None
                with contextlib.suppress(psycopg.Error):
                    conn.close()
                conn = psycopg.connect(self._dsn, autocommit=True)
                conn.execute("SET statement_timeout = '1200s'")
            self._halt.wait(0.04)
        with contextlib.suppress(psycopg.Error):
            conn.close()
