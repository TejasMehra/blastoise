"""Shell Armour, the lock semantics catalog: what each statement does to locks.

The data lives in ``lock_catalog.yaml`` (one row per classification x PG
version range x optional IR-shape variant, each with a citation). The loader
validates it exhaustively at load time; the resolver maps parsed statements
onto rows and names every affected relation the IR can supply.

    from blastoise.catalog import load_catalog, resolve

    catalog = load_catalog()
    for lock in resolve(catalog, statement, pg_version=16):
        print(lock.entry.lock_mode, lock.entry.duration_model, lock.relations)
"""

from blastoise.catalog.loader import (
    CATALOG_RESOURCE,
    CatalogError,
    load_catalog,
    load_catalog_file,
    parse_catalog,
)
from blastoise.catalog.model import (
    TABLE_LOCK_MODES,
    AffectedRelation,
    Calibration,
    CatalogEntry,
    DurationModel,
    LockCatalog,
    LockMode,
    Resolution,
    TransactionBlock,
    VersionBreakpoint,
    VersionRange,
    WriteBlockScope,
    when_matches,
)
from blastoise.catalog.resolve import (
    RelationLock,
    ResolvedLock,
    ir_attrs,
    kinds_in,
    resolve,
    statement_lock_mode,
)

__all__ = [
    "CATALOG_RESOURCE",
    "TABLE_LOCK_MODES",
    "AffectedRelation",
    "Calibration",
    "CatalogEntry",
    "CatalogError",
    "DurationModel",
    "LockCatalog",
    "LockMode",
    "RelationLock",
    "Resolution",
    "ResolvedLock",
    "TransactionBlock",
    "VersionBreakpoint",
    "VersionRange",
    "WriteBlockScope",
    "ir_attrs",
    "kinds_in",
    "load_catalog",
    "load_catalog_file",
    "parse_catalog",
    "resolve",
    "statement_lock_mode",
    "when_matches",
]
