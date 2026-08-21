"""Session fixtures for the live-introspection tests.

Everything here is lazy: importing this file costs nothing unless a test
actually asks for a server, so the parsing/catalog test suite is unaffected
on machines with no Postgres available.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from live_harness import (
    RO_ROLE,
    RO_ROLE_NOMON,
    PgServer,
    acquire_server,
    create_roles,
    drop_roles,
)


@pytest.fixture(scope="session")
def pg_server() -> Iterator[PgServer]:
    server = acquire_server()
    create_roles(server.admin_dsn)
    try:
        yield server
    finally:
        try:
            drop_roles(server.admin_dsn)
        finally:
            server.cleanup()


@pytest.fixture(scope="session")
def admin_dsn(pg_server: PgServer) -> str:
    return pg_server.admin_dsn


@pytest.fixture(scope="session")
def ro_dsn(pg_server: PgServer) -> str:
    """DSN for the documented minimum-privilege role (with pg_monitor)."""
    return pg_server.dsn_for(RO_ROLE)


@pytest.fixture(scope="session")
def nomon_dsn(pg_server: PgServer) -> str:
    """DSN for a read-only role that lacks pg_monitor/pg_read_all_stats."""
    return pg_server.dsn_for(RO_ROLE_NOMON)
