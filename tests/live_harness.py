"""Disposable Postgres server for live-introspection tests.

Acquisition order:

1. ``PGVERDICT_TEST_DSN`` — a superuser DSN to a disposable server someone
   already runs (CI service container, local dev instance).
2. testcontainers — ``postgres:17`` via Docker, when Docker is available.
3. ``PGVERDICT_TEST_PG_BIN`` — a directory of Postgres binaries
   (initdb/pg_ctl/postgres); the harness initdb's a throwaway cluster in a
   temp directory and runs it on a free port. This is the path for
   machines without Docker.

If none works the live tests skip with an explanation; the model and
redaction tests run everywhere.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

RO_ROLE = "pgverdict_ro"  # the documented minimum-privilege role (pg_monitor)
RO_ROLE_NOMON = "pgverdict_ro_nomon"  # same, without pg_monitor
ROLE_PASSWORD = "pgverdict-test"


@dataclass
class PgServer:
    """A running disposable Postgres server and its superuser DSN."""

    admin_dsn: str
    cleanup: Callable[[], None]
    description: str

    def dsn_for(self, user: str) -> str:
        import psycopg.conninfo

        return psycopg.conninfo.make_conninfo(
            self.admin_dsn, user=user, password=ROLE_PASSWORD
        )


def acquire_server() -> PgServer:
    dsn = os.environ.get("PGVERDICT_TEST_DSN")
    if dsn:
        return PgServer(admin_dsn=dsn, cleanup=lambda: None, description="PGVERDICT_TEST_DSN")
    server = _try_testcontainers()
    if server is not None:
        return server
    server = _try_local_binaries()
    if server is not None:
        return server
    pytest.skip(
        "no Postgres available for live tests: set PGVERDICT_TEST_DSN to a "
        "disposable superuser DSN, install Docker for testcontainers, or set "
        "PGVERDICT_TEST_PG_BIN to a Postgres bin/ directory"
    )


def _try_testcontainers() -> PgServer | None:
    try:
        from testcontainers.community.postgres import PostgresContainer
    except ImportError:
        try:
            from testcontainers.postgres import PostgresContainer  # < 4.15
        except ImportError:
            return None
    try:
        container = PostgresContainer(
            "postgres:17", username="postgres", password="postgres", dbname="pgverdict_test"
        )
        container.start()
    except Exception:
        return None
    host = container.get_container_host_ip()
    port = container.get_exposed_port(5432)
    dsn = f"postgresql://postgres:postgres@{host}:{port}/pgverdict_test"
    return PgServer(
        admin_dsn=dsn,
        cleanup=lambda: container.stop(),
        description="testcontainers postgres:17",
    )


def _try_local_binaries() -> PgServer | None:
    bin_dir = os.environ.get("PGVERDICT_TEST_PG_BIN")
    if not bin_dir:
        return None
    bin_path = Path(bin_dir)
    exe = ".exe" if os.name == "nt" else ""
    initdb = bin_path / f"initdb{exe}"
    pg_ctl = bin_path / f"pg_ctl{exe}"
    if not initdb.exists() or not pg_ctl.exists():
        pytest.skip(f"PGVERDICT_TEST_PG_BIN={bin_dir} has no initdb/pg_ctl")

    work = Path(tempfile.mkdtemp(prefix="pgverdict-live-"))
    data = work / "data"
    log = work / "server.log"
    subprocess.run(
        [str(initdb), "-D", str(data), "-U", "postgres", "-A", "trust", "-E", "UTF8",
         "--no-sync"],
        check=True,
        capture_output=True,
    )
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server_options = (
        f"-p {port} -c listen_addresses=127.0.0.1 -c fsync=off "
        f"-c synchronous_commit=off -c full_page_writes=off"
    )
    # No capture_output here: the daemonized postgres inherits the pipe
    # handles on Windows and run() would wait forever for EOF. The server
    # log lands in ``log`` via -l.
    subprocess.run(
        [str(pg_ctl), "-D", str(data), "-l", str(log), "-w", "-o", server_options, "start"],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    def cleanup() -> None:
        subprocess.run(
            [str(pg_ctl), "-D", str(data), "-m", "immediate", "stop"],
            check=False,
            capture_output=True,
        )
        shutil.rmtree(work, ignore_errors=True)

    return PgServer(
        admin_dsn=f"postgresql://postgres@127.0.0.1:{port}/postgres",
        cleanup=cleanup,
        description=f"local binaries at {bin_dir}",
    )


def create_roles(admin_dsn: str) -> None:
    """(Re)create the two test roles the introspection connects as."""
    import psycopg

    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        for role in (RO_ROLE, RO_ROLE_NOMON):
            conn.execute(f"DROP ROLE IF EXISTS {role}")
            conn.execute(f"CREATE ROLE {role} LOGIN PASSWORD '{ROLE_PASSWORD}'")
        conn.execute(f"GRANT pg_monitor TO {RO_ROLE}")


def drop_roles(admin_dsn: str) -> None:
    import psycopg

    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        for role in (RO_ROLE, RO_ROLE_NOMON):
            conn.execute(f"DROP ROLE IF EXISTS {role}")


def unique_name(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"
