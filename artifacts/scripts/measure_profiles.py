"""Replicated measurement of the duration constants across hardware profiles.

One machine, however well normalized, cannot decide a constant whose
cases sit within 20% of an outage threshold: the 2026-08-22 validation
runs flipped the same boundary cases run-to-run. This script is the
per-profile half of the fix. It is run unchanged on each hardware
profile (a small burstable VM, a compute-optimized VM, a storage-
optimized VM, and the laptop the earlier constants came from), and
records, for every duration constant:

  * the raw rate of each representative statement at each size, two
    passes (so within-machine variance is known per profile), and
  * the calibration probe (``blastoise.live.calibrate``) read before,
    between, and after the passes — the same bounded read-only probe the
    live snapshot captures on a target — so the rates can be expressed
    per unit of probe and the residual the probe does *not* explain can
    be measured across profiles. That residual is the band.

Method per case is the scale harness's: the scale schema seeded at
1k/100k/1M/10M, BEGIN ... execute ... ROLLBACK with pg_locks sampled
every 25 ms for the lock mode, VACUUM (ANALYZE) after DML so aborted
tuples do not poison the next pass. Server options are the scale
harness's (shared_buffers 512MB, autovacuum off, fsync on).

Usage:  python artifacts/scripts/measure_profiles.py --profile NAME \
            --pg-bin /usr/lib/postgresql/17/bin --out results.json \
            [--disk "pd-ssd 50GB"] [--sizes t_1k,t_100k,t_1m,t_10m]

``--profile`` and ``--disk`` are labels recorded verbatim; CPU count,
model, memory, and the data directory's mount are read from the OS.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import platform
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import psycopg

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from blastoise.live import calibrate as cb  # noqa: E402
from validation.harness import fixtures as fx  # noqa: E402

ALL_SIZES = {"t_1k": 1_000, "t_100k": 100_000, "t_1m": 1_000_000, "t_10m": 10_000_000}

# (case id, constant family, SQL template, largest size it runs at). The
# SQL is the scale harness's / the 2026-08-22 re-measurement's, verbatim,
# so readings are comparable with the committed artifacts.
CASES: tuple[tuple[str, str, str, int], ...] = (
    ("create_index_single", "index_build_btree",
     "CREATE INDEX {t}_user_id_idx ON {t} (user_id)", 10_000_000),
    ("create_index_expr", "index_build_expression",
     "CREATE INDEX {t}_title_lower_idx ON {t} (lower(title))", 10_000_000),
    ("rewrite_int_bigint", "heap_rewrite",
     "ALTER TABLE {t} ALTER COLUMN user_id TYPE bigint", 10_000_000),
    ("rewrite_text_varchar", "heap_rewrite",
     "ALTER TABLE {t} ALTER COLUMN body TYPE varchar(2000)", 1_000_000),
    ("compute_addcol_volatile", "add_column_rewrite",
     "ALTER TABLE {t} ADD COLUMN new_id uuid NOT NULL DEFAULT gen_random_uuid()", 1_000_000),
    ("compute_addcol_serial", "add_column_rewrite",
     "ALTER TABLE {t} ADD COLUMN seq_id BIGSERIAL", 1_000_000),
    ("compute_addcol_generated", "add_column_rewrite",
     "ALTER TABLE {t} ADD COLUMN score_band text GENERATED ALWAYS AS "
     "(CASE WHEN score > 500 THEN 'high' ELSE 'low' END) STORED", 1_000_000),
    ("scan_add_check", "validation_scan",
     "ALTER TABLE {t} ADD CONSTRAINT {t}_note_not_empty CHECK (length(trim(note)) > 0)",
     10_000_000),
    ("scan_set_not_null", "validation_scan",
     "ALTER TABLE {t} ALTER COLUMN note SET NOT NULL", 10_000_000),
    ("add_fk_plain", "fk_validation",
     "ALTER TABLE {t} ADD CONSTRAINT {t}_account_fk FOREIGN KEY (account_id) "
     "REFERENCES accounts(id)", 10_000_000),
    ("reindex_index", "index_bytes", "REINDEX INDEX {t}_account_id_idx", 10_000_000),
    ("update_without_where", "dml_update", "UPDATE {t} SET score = score + 1", 1_000_000),
    ("delete_without_where", "dml_delete", "DELETE FROM {t}", 1_000_000),
    ("addcol_nodefault", "constant_op", "ALTER TABLE {t} ADD COLUMN extra_flag boolean",
     10_000_000),
)
_DML = {"update_without_where", "delete_without_where"}

_PG_MODE = {
    "AccessShareLock": "ACCESS SHARE", "RowShareLock": "ROW SHARE",
    "RowExclusiveLock": "ROW EXCLUSIVE", "ShareUpdateExclusiveLock": "SHARE UPDATE EXCLUSIVE",
    "ShareLock": "SHARE", "ShareRowExclusiveLock": "SHARE ROW EXCLUSIVE",
    "ExclusiveLock": "EXCLUSIVE", "AccessExclusiveLock": "ACCESS EXCLUSIVE",
}
_RANK = {n: i for i, n in enumerate(_PG_MODE)}


class Mon(threading.Thread):
    def __init__(self, dsn: str, pid: int, table: str) -> None:
        super().__init__(daemon=True)
        self._c = psycopg.connect(dsn, autocommit=True)
        self._pid, self._t = pid, table
        self._halt = threading.Event()
        self.modes: set[str] = set()

    def _sample(self) -> set[str]:
        rows = self._c.execute(
            "SELECT coalesce(c.relname, l.relation::text), l.mode FROM pg_locks l "
            "LEFT JOIN pg_class c ON c.oid = l.relation "
            "WHERE l.pid = %s AND l.locktype = 'relation' AND l.granted", (self._pid,),
        ).fetchall()
        return {str(m) for rel, m in rows if str(rel) == self._t}

    def run(self) -> None:
        while not self._halt.is_set():
            try:
                self.modes |= self._sample()
            except psycopg.Error:
                return
            self._halt.wait(0.025)

    def final(self) -> set[str]:
        try:
            return self._sample()
        except psycopg.Error:
            return set()

    def stop(self) -> None:
        self._halt.set()
        self.join(timeout=2)
        with contextlib.suppress(psycopg.Error):
            self._c.close()


def machine_info(data_dir: Path, disk_label: str) -> dict[str, Any]:
    info: dict[str, Any] = {
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
        "cpu_count": os.cpu_count(),
        "disk_label": disk_label,
    }
    try:
        if Path("/proc/cpuinfo").exists():
            for line in Path("/proc/cpuinfo").read_text().splitlines():
                if line.startswith("model name"):
                    info["cpu_model"] = line.split(":", 1)[1].strip()
                    break
            for line in Path("/proc/meminfo").read_text().splitlines():
                if line.startswith("MemTotal"):
                    info["mem_kb"] = int(line.split()[1])
                    break
            out = subprocess.run(["df", "-h", str(data_dir)], capture_output=True, text=True)
            info["df"] = out.stdout.strip().splitlines()[-1]
            out = subprocess.run(["lsblk", "-d", "-o", "NAME,ROTA,SIZE,MODEL"],
                                 capture_output=True, text=True)
            info["lsblk"] = out.stdout.strip()
        else:
            info["cpu_model"] = platform.processor()
    except Exception as exc:  # informational only
        info["info_error"] = str(exc)[:200]
    return info


def start_server(pg_bin: Path, work_root: Path) -> tuple[str, Any, Path]:
    exe = ".exe" if os.name == "nt" else ""
    work = Path(tempfile.mkdtemp(prefix="blastoise-profile-", dir=str(work_root)))
    data, log = work / "data", work / "server.log"
    subprocess.run([str(pg_bin / f"initdb{exe}"), "-D", str(data), "-U", "postgres",
                    "-A", "trust", "-E", "UTF8", "--no-sync"], check=True, capture_output=True)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    opts = f"-p {port} {fx.SERVER_OPTIONS}"
    subprocess.run([str(pg_bin / f"pg_ctl{exe}"), "-D", str(data), "-l", str(log), "-w",
                    "-o", opts, "start"], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def stop() -> None:
        subprocess.run([str(pg_bin / f"pg_ctl{exe}"), "-D", str(data), "-m", "immediate",
                        "stop"], check=False, capture_output=True)

    return f"postgresql://postgres@127.0.0.1:{port}/postgres", stop, data


def probe(dsn: str, scan_table: str | None, label: str, at_s: float) -> dict[str, Any]:
    """The calibration probe exactly as the snapshot will run it."""
    out: dict[str, Any] = {"label": label, "at_s": round(at_s, 1)}
    with psycopg.connect(dsn) as conn:
        conn.read_only = True
        conn.execute("SET statement_timeout = '60s'")
        compute: list[int] = []
        for _ in range(cb.PROBE_REPEATS):
            t0 = time.perf_counter()
            conn.execute(cb.COMPUTE_PROBE_SQL, {"n": cb.COMPUTE_PROBE_ROWS})
            compute.append(int((time.perf_counter() - t0) * 1000))
        out["compute_ms_runs"] = compute
        out["compute_ms"] = min(compute)
        if scan_table is not None:
            scans: list[int] = []
            for _ in range(cb.PROBE_REPEATS):
                t0 = time.perf_counter()
                conn.execute(cb.SCAN_PROBE_SQL.format(rel=scan_table), {"n": cb.SCAN_PROBE_ROWS})
                scans.append(int((time.perf_counter() - t0) * 1000))
            out["scan_ms_runs"] = scans
            out["scan_ms"] = min(scans)
            out["scan_relation"] = scan_table
        conn.rollback()
    return out


def _reconnect(dsn: str) -> tuple[psycopg.Connection[Any], int]:
    conn = psycopg.connect(dsn)
    conn.execute("SET TIME ZONE 'UTC'")
    conn.execute("SET statement_timeout = '3600s'")
    conn.commit()
    row = conn.execute("SELECT pg_backend_pid()").fetchone()
    conn.commit()
    return conn, int(row[0]) if row else 0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", required=True)
    ap.add_argument("--pg-bin", default=os.environ.get("BLASTOISE_TEST_PG_BIN"))
    ap.add_argument("--out", required=True)
    ap.add_argument("--disk", default="")
    ap.add_argument("--sizes", default=",".join(ALL_SIZES))
    ap.add_argument("--passes", type=int, default=2)
    ap.add_argument("--work-dir", default=None)
    args = ap.parse_args()
    pg_bin = Path(args.pg_bin)
    out = Path(args.out)
    sizes = {k: ALL_SIZES[k] for k in args.sizes.split(",") if k}
    work_root = Path(args.work_dir) if args.work_dir else out.parent
    work_root.mkdir(parents=True, exist_ok=True)

    admin_dsn, stop, data_dir = start_server(pg_bin, work_root)
    started = time.monotonic()
    run_t0 = time.perf_counter()
    records: list[dict[str, Any]] = []
    probes: list[dict[str, Any]] = []
    results: dict[str, Any] = {
        "profile": args.profile,
        "machine": machine_info(data_dir, args.disk),
        "sizes": sizes,
        "probe": {"compute_rows": cb.COMPUTE_PROBE_ROWS, "scan_rows": cb.SCAN_PROBE_ROWS,
                  "repeats": cb.PROBE_REPEATS},
        "probes": probes,
        "records": records,
    }

    def flush() -> None:
        out.write_text(json.dumps(results, indent=1), encoding="utf8")

    try:
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            row = admin.execute("SHOW server_version").fetchone()
            results["server_version"] = row[0] if row else None
        print(f"[{args.profile}] seeding {sizes}...", flush=True)
        results["heap_bytes"] = fx.seed(admin_dsn, "measure", sizes)
        dsn = admin_dsn.rsplit("/", 1)[0] + "/measure"
        if "t_1m" in sizes:
            fx.settle(dsn)
        scan_table = max(
            (t for t in sizes if sizes[t] >= cb.SCAN_PROBE_ROWS), key=lambda t: sizes[t],
            default=None,
        )
        with psycopg.connect(dsn, autocommit=True) as conn:
            idx_bytes: dict[str, int] = {}
            for t in sizes:
                r = conn.execute(f"SELECT pg_relation_size('{t}_account_id_idx')").fetchone()
                idx_bytes[t] = int(r[0]) if r else 0
            results["index_bytes"] = idx_bytes
        # Warm-up probe (discarded), then the first recorded one.
        probe(dsn, scan_table, "warmup", 0.0)
        probes.append(probe(dsn, scan_table, "start", time.perf_counter() - run_t0))
        print(f"  probe start: {probes[-1]}", flush=True)
        flush()

        apply_conn, pid = _reconnect(dsn)
        for pass_index in range(args.passes):
            pass_name = chr(ord("A") + pass_index)
            for t, n in sizes.items():
                for cid, family, tmpl, maxrows in CASES:
                    if n > maxrows:
                        continue
                    sql = tmpl.format(t=t)
                    mon = Mon(dsn, pid, t)
                    mon.start()
                    err: str | None = None
                    ms: int | None = None
                    rc: int | None = None
                    final: set[str] = set()
                    at_s = round(time.perf_counter() - run_t0, 1)
                    try:
                        cur = apply_conn.cursor()
                        p0 = time.perf_counter()
                        cur.execute(sql)
                        ms = int((time.perf_counter() - p0) * 1000)
                        rc = cur.rowcount if cur.rowcount >= 0 else None
                        final = mon.final()
                    except psycopg.Error as exc:
                        err = str(exc).split("\n")[0][:200]
                    finally:
                        try:
                            apply_conn.rollback()
                        except psycopg.Error:
                            apply_conn.close()
                            apply_conn, pid = _reconnect(dsn)
                        mon.stop()
                    if cid in _DML:
                        with psycopg.connect(dsn, autocommit=True) as vac:
                            vac.execute(f"VACUUM (ANALYZE) {t}")
                    modes = mon.modes | final
                    strongest = None
                    if modes:
                        strongest = _PG_MODE.get(sorted(modes, key=lambda m: _RANK.get(m, -1))[-1])
                    rate = (n * 1000 // ms) if ms else None
                    records.append({
                        "pass_": pass_name, "case": cid, "family": family, "table": t,
                        "rows": n, "ms": ms, "rowcount": rc, "error": err,
                        "strongest_mode": strongest, "rate_raw": rate, "at_s": at_s,
                    })
                    print(f"[{args.profile} {time.monotonic()-started:7.1f}s] pass{pass_name} "
                          f"{t:7s} {cid:26s} {ms}ms rate={rate} mode={strongest} err={err}",
                          flush=True)
                    flush()
            probes.append(probe(dsn, scan_table, f"after_pass_{pass_name}",
                                time.perf_counter() - run_t0))
            print(f"  probe after pass {pass_name}: {probes[-1]}", flush=True)
            flush()
        apply_conn.close()
        results["elapsed_s"] = int(time.monotonic() - started)
        flush()
        print(f"[{args.profile}] done in {results['elapsed_s']}s -> {out}", flush=True)
    finally:
        stop()


if __name__ == "__main__":
    main()
