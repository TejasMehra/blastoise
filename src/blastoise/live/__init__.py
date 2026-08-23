"""Hydro Scan, the live introspection layer: read-only production context.

``capture_snapshot()`` connects to a database (read-only, bounded, catalog
views only) and returns a :class:`LiveSnapshot` — the facts the lock
catalog's ``requires_live_context`` and ``duration_model`` fields declare
missing from static analysis: relation sizes and staleness, columns and
constraints, function volatility, type-change coercion facts, lock waiters,
replication lag, and the server version. Requires the ``psycopg`` driver
(``pip install blastoise[live]``); everything else in blastoise works
without it.
"""

from blastoise.live.introspect import (
    LiveIntrospectionError,
    WritableRoleError,
    capture_snapshot,
    redact_conninfo,
)
from blastoise.live.model import (
    SNAPSHOT_FORMAT,
    CalibrationFacts,
    CaptureLimits,
    ColumnFacts,
    ConcurrencyFacts,
    ConnectionTarget,
    ConstraintFacts,
    Fact,
    FunctionFacts,
    IndexFacts,
    JsonValue,
    LiveSnapshot,
    LockWaiter,
    LongTransaction,
    RelationFacts,
    ReplicaFacts,
    ReplicationFacts,
    RoleFacts,
    ServerFacts,
    TypeChangeFacts,
    TypeChangeProbe,
    TypeFacts,
)
from blastoise.live.resolve import (
    decide_default_volatility,
    resolved_function_volatilities,
)
from blastoise.live.typechange import (
    RewriteVerdict,
    TypeChangeAssessment,
    assess_type_change,
)

__all__ = [
    "SNAPSHOT_FORMAT",
    "CalibrationFacts",
    "CaptureLimits",
    "ColumnFacts",
    "ConcurrencyFacts",
    "ConnectionTarget",
    "ConstraintFacts",
    "Fact",
    "FunctionFacts",
    "IndexFacts",
    "JsonValue",
    "LiveIntrospectionError",
    "LiveSnapshot",
    "LockWaiter",
    "LongTransaction",
    "RelationFacts",
    "ReplicaFacts",
    "ReplicationFacts",
    "RewriteVerdict",
    "RoleFacts",
    "ServerFacts",
    "TypeChangeAssessment",
    "TypeChangeFacts",
    "TypeChangeProbe",
    "TypeFacts",
    "WritableRoleError",
    "assess_type_change",
    "capture_snapshot",
    "decide_default_volatility",
    "redact_conninfo",
    "resolved_function_volatilities",
]
