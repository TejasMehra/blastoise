"""Which dependent indexes does a no-rewrite ALTER COLUMN TYPE rebuild?

Ground truth on PG 17.10: seed 1M rows, put five differently-shaped btree
indexes on varchar columns, widen each column's typmod (and one varchar->text),
and compare each index's relfilenode before/after plus wall time.
"""

from __future__ import annotations

import subprocess
import tempfile
import time
from pathlib import Path

import psycopg

PG_BIN = Path(__file__).parent / "pg" / "bin"


def start_server():
    import socket

    work = Path(tempfile.mkdtemp(prefix="pgverdict-reuse-"))
    data = work / "data"
    subprocess.run(
        [str(PG_BIN / "initdb.exe"), "-D", str(data), "-U", "postgres", "-A", "trust",
         "-E", "UTF8", "--no-sync"],
        check=True, capture_output=True,
    )
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    options = (
        f"-p {port} -c listen_addresses=127.0.0.1 -c shared_buffers=512MB "
        f"-c maintenance_work_mem=256MB -c autovacuum=off -c fsync=off"
    )
    subprocess.run(
        [str(PG_BIN / "pg_ctl.exe"), "-D", str(data), "-l", str(work / "log"), "-w",
         "-o", options, "start"],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    def stop():
        subprocess.run(
            [str(PG_BIN / "pg_ctl.exe"), "-D", str(data), "-m", "immediate", "stop"],
            check=False, capture_output=True,
        )

    return f"postgresql://postgres@127.0.0.1:{port}/postgres", stop


CASES = [
    ("plain btree, widen typmod", "col_a", "varchar(64)", "ix_a"),
    ("partial btree, widen typmod", "col_b", "varchar(64)", "ix_b"),
    ("expression btree, widen typmod", "col_c", "varchar(64)", "ix_c"),
    ("pattern_ops btree, widen typmod", "col_d", "varchar(64)", "ix_d"),
    ("plain btree, varchar->text", "col_e", "text", "ix_e"),
    ("plain btree high-cardinality, widen", "col_f", "varchar(64)", "ix_f"),
]


def main() -> None:
    dsn, stop = start_server()
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(
                """
                CREATE TABLE t (
                  id bigint PRIMARY KEY,
                  col_a varchar(32), col_b varchar(32), col_c varchar(32),
                  col_d varchar(32), col_e varchar(32), col_f varchar(32),
                  flag boolean NOT NULL DEFAULT true
                )
                """
            )
            conn.execute(
                """
                INSERT INTO t
                SELECT g, 'v' || (g % 6), 'v' || (g % 6), 'v' || (g % 6),
                       'v' || (g % 6), 'v' || (g % 6), 'value-' || g, g % 10 <> 0
                FROM generate_series(1, 1000000) g
                """
            )
            conn.execute("CREATE INDEX ix_a ON t (col_a)")
            conn.execute("CREATE INDEX ix_b ON t (col_b) WHERE flag")
            conn.execute("CREATE INDEX ix_c ON t (lower(col_c))")
            conn.execute("CREATE INDEX ix_d ON t (col_d varchar_pattern_ops)")
            conn.execute("CREATE INDEX ix_e ON t (col_e)")
            conn.execute("CREATE INDEX ix_f ON t (col_f)")
            conn.execute("VACUUM (ANALYZE) t")

            for label, col, newtype, ix in CASES:
                heap_before = conn.execute(
                    "SELECT relfilenode FROM pg_class WHERE relname = 't'"
                ).fetchone()[0]
                ix_before = conn.execute(
                    "SELECT relfilenode FROM pg_class WHERE relname = %s", (ix,)
                ).fetchone()[0]
                size = conn.execute(
                    "SELECT pg_relation_size(%s::regclass)", (ix,)
                ).fetchone()[0]
                t0 = time.perf_counter()
                conn.execute(f"ALTER TABLE t ALTER COLUMN {col} TYPE {newtype}")
                ms = int((time.perf_counter() - t0) * 1000)
                heap_after = conn.execute(
                    "SELECT relfilenode FROM pg_class WHERE relname = 't'"
                ).fetchone()[0]
                ix_after = conn.execute(
                    "SELECT relfilenode FROM pg_class WHERE relname = %s", (ix,)
                ).fetchone()[0]
                print(
                    f"{label:38s} {ms:>6}ms heap_rewrote={heap_after != heap_before} "
                    f"index_rebuilt={ix_after != ix_before} idx_size={size}"
                )
    finally:
        stop()


if __name__ == "__main__":
    main()
