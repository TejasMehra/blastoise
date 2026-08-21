"""Validating loader for ``lock_catalog.yaml``.

Every structural or semantic problem in the data raises
:class:`CatalogError` at load time: a missing kind, a version gap, a row
that spans a registered breakpoint, a ``blocks_reads`` flag that contradicts
the conflict matrix, an unknown ``when`` key. Lookup can therefore never
fall through to a default — if a classification has no row, the catalog
does not load.
"""

from __future__ import annotations

import dataclasses
import functools
from collections.abc import Iterable, Mapping
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from blastoise import ir
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
)
from blastoise.ir import AlterTableActionKind, StatementKind

CATALOG_RESOURCE = "lock_catalog.yaml"
SCHEMA_VERSION = 1

# Roles the resolver knows how to map onto IR objects. Roles that cannot be
# named statically (the table owning an index, FK referencers of a truncated
# table, ...) must be declared with ``statically_resolvable: false``.
KNOWN_ROLES: frozenset[str] = frozenset(
    {
        "target",
        "additional_targets",
        "referenced_table",
        "referenced_tables",
        "partition",
        "parent",
        # unresolvable statically
        "owning_table",
        "source_relations",
        "dependent_relations",
        "referencing_tables",
        "domain_dependent_tables",
        "owned_sequences",
        "all_partitions",
    }
)
UNRESOLVABLE_ROLES: frozenset[str] = frozenset(
    {
        "owning_table",
        "source_relations",
        "dependent_relations",
        "referencing_tables",
        "domain_dependent_tables",
        "owned_sequences",
        "all_partitions",
    }
)

# The only classifications allowed to carry LockMode.UNKNOWN /
# DurationModel.UNKNOWN. They are the classifier's grab bags; everything else
# must commit to a modeled value.
UNKNOWN_ALLOWED: frozenset[StatementKind | AlterTableActionKind] = frozenset(
    {
        StatementKind.OTHER,
        AlterTableActionKind.OTHER,
        AlterTableActionKind.ADD_CONSTRAINT_OTHER,
    }
)

_SOURCE_PREFIXES = ("doc:", "src:", "release:")

_REQUIRED_FIELDS = (
    "pg_versions",
    "calibration",
    "lock_mode",
    "blocks_reads",
    "blocks_writes",
    "requires_table_rewrite",
    "duration_model",
    "source",
)
_OPTIONAL_FIELDS = (
    "available",
    "write_block_scope",
    "row_locks",
    "requires_live_context",
    "live_context",
    "transaction_block",
    "failure_mode",
    "affected_relations",
    "when",
    "notes",
)
_ALL_FIELDS = frozenset(_REQUIRED_FIELDS + _OPTIONAL_FIELDS)


class CatalogError(ValueError):
    """The catalog data is malformed, incomplete, or internally inconsistent."""


def _ir_attribute_names() -> frozenset[str]:
    """Field names a ``when`` predicate may reference."""
    names: set[str] = set()
    classes: tuple[type[Any], ...] = (
        ir.ParsedStatement,
        ir.AlterTableAction,
        ir.CreateTableDetails,
        ir.CreateTableAsDetails,
        ir.IndexDetails,
        ir.DropDetails,
        ir.TruncateDetails,
        ir.ReindexDetails,
        ir.RenameDetails,
        ir.SetDetails,
        ir.LockDetails,
        ir.TransactionDetails,
        ir.DoBlockDetails,
        ir.DmlDetails,
        ir.InsertDetails,
    )
    for cls in classes:
        names.update(f.name for f in dataclasses.fields(cls))
    return frozenset(names)


def _fail(where: str, message: str) -> CatalogError:
    return CatalogError(f"{where}: {message}")


def _enum(cls: type[Any], value: object, where: str, field: str) -> Any:
    try:
        return cls(value)
    except ValueError:
        valid = ", ".join(m.value for m in cls)
        raise _fail(where, f"{field} {value!r} is not one of: {valid}") from None


def _bool(value: object, where: str, field: str) -> bool:
    if not isinstance(value, bool):
        raise _fail(where, f"{field} must be a boolean, got {value!r}")
    return value


def _opt_str(value: object, where: str, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise _fail(where, f"{field} must be a non-empty string or null")
    return value


def _parse_versions(value: object, where: str, domain: VersionRange) -> VersionRange:
    if (
        not isinstance(value, list)
        or len(value) != 2
        or not all(isinstance(v, int) and not isinstance(v, bool) for v in value)
    ):
        raise _fail(where, f"pg_versions must be [min, max] integers, got {value!r}")
    lo, hi = value
    if lo > hi:
        raise _fail(where, f"pg_versions min {lo} > max {hi}")
    if not (domain.covers(lo) and domain.covers(hi)):
        raise _fail(where, f"pg_versions {lo}-{hi} leave the version domain {domain}")
    return VersionRange(min=lo, max=hi)


def _parse_conflict_matrix(raw: object) -> dict[LockMode, frozenset[LockMode]]:
    where = "conflict_matrix"
    if not isinstance(raw, Mapping):
        raise _fail(where, "must be a mapping of lock mode -> conflicting modes")
    matrix: dict[LockMode, frozenset[LockMode]] = {}
    for key, value in raw.items():
        mode = _enum(LockMode, key, where, "lock mode")
        if not mode.is_table_lock:
            raise _fail(where, f"{mode.value} is not a table lock mode")
        if not isinstance(value, list):
            raise _fail(where, f"{mode.value}: conflicts must be a list")
        matrix[mode] = frozenset(_enum(LockMode, v, where, "conflict") for v in value)
    missing = [m.value for m in TABLE_LOCK_MODES if m not in matrix]
    if missing:
        raise _fail(where, f"missing rows for {missing}")
    for mode, conflicts in matrix.items():
        for other in conflicts:
            if mode not in matrix[other]:
                raise _fail(
                    where,
                    f"asymmetric: {mode.value} conflicts with {other.value} but not vice versa",
                )
    return matrix


def _parse_affected(
    raw: object, where: str, default_mode: LockMode
) -> tuple[AffectedRelation, ...]:
    if raw is None:
        return (AffectedRelation(role="target", lock_mode=default_mode),)
    if not isinstance(raw, list):
        raise _fail(where, "affected_relations must be a list ([] = nothing locked) or omitted")
    result: list[AffectedRelation] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, Mapping):
            raise _fail(where, "each affected relation must be a mapping")
        unknown = set(item) - {"role", "lock_mode", "optional", "statically_resolvable"}
        if unknown:
            raise _fail(where, f"affected relation has unknown keys {sorted(unknown)}")
        role = item.get("role")
        if role not in KNOWN_ROLES:
            raise _fail(where, f"unknown affected-relation role {role!r}")
        if role in seen:
            raise _fail(where, f"duplicate affected-relation role {role!r}")
        seen.add(role)
        mode = _enum(LockMode, item.get("lock_mode"), where, f"{role}.lock_mode")
        optional = _bool(item.get("optional", False), where, f"{role}.optional")
        resolvable = _bool(
            item.get("statically_resolvable", role not in UNRESOLVABLE_ROLES),
            where,
            f"{role}.statically_resolvable",
        )
        if (role in UNRESOLVABLE_ROLES) == resolvable:
            raise _fail(
                where,
                f"role {role!r} must declare "
                f"statically_resolvable={role not in UNRESOLVABLE_ROLES}",
            )
        result.append(
            AffectedRelation(
                role=role, lock_mode=mode, optional=optional, statically_resolvable=resolvable
            )
        )
    return tuple(result)


def _parse_when(
    raw: object, where: str, attribute_names: frozenset[str]
) -> tuple[tuple[str, Any], ...]:
    if raw is None:
        return ()
    if not isinstance(raw, Mapping) or not raw:
        raise _fail(where, "when must be a non-empty mapping or omitted")
    items: list[tuple[str, Any]] = []
    for key, value in raw.items():
        if key not in attribute_names:
            raise _fail(where, f"when references unknown IR attribute {key!r}")
        if isinstance(value, list):
            items.append((key, list(value)))
        else:
            items.append((key, value))
    return tuple(items)


def _parse_entry(
    kind: StatementKind | AlterTableActionKind,
    raw: object,
    index: int,
    *,
    domain: VersionRange,
    matrix: dict[LockMode, frozenset[LockMode]],
    attribute_names: frozenset[str],
) -> CatalogEntry:
    where = f"{kind.value}[{index}]"
    if not isinstance(raw, Mapping):
        raise _fail(where, "entry must be a mapping")
    unknown = set(raw) - _ALL_FIELDS
    if unknown:
        raise _fail(where, f"unknown fields {sorted(unknown)}")
    missing = [f for f in _REQUIRED_FIELDS if f not in raw]
    if missing:
        raise _fail(where, f"missing required fields {missing}")

    versions = _parse_versions(raw["pg_versions"], where, domain)
    calibration = _enum(Calibration, raw["calibration"], where, "calibration")
    if calibration is Calibration.MEASURED:
        raise _fail(
            where,
            "MEASURED is reserved for duration constants; catalog rows are "
            "CALIBRATED or UNCALIBRATED",
        )
    available = _bool(raw.get("available", True), where, "available")
    lock_mode = _enum(LockMode, raw["lock_mode"], where, "lock_mode")
    blocks_reads = _bool(raw["blocks_reads"], where, "blocks_reads")
    blocks_writes = _bool(raw["blocks_writes"], where, "blocks_writes")
    rewrite = _bool(raw["requires_table_rewrite"], where, "requires_table_rewrite")
    duration = _enum(DurationModel, raw["duration_model"], where, "duration_model")
    live = _bool(raw.get("requires_live_context", False), where, "requires_live_context")
    live_context = _opt_str(raw.get("live_context"), where, "live_context")
    txn = _enum(
        TransactionBlock, raw.get("transaction_block", "ALLOWED"), where, "transaction_block"
    )
    failure_mode = _opt_str(raw.get("failure_mode"), where, "failure_mode")
    row_locks = _opt_str(raw.get("row_locks"), where, "row_locks")
    notes = _opt_str(raw.get("notes"), where, "notes")
    affected = _parse_affected(raw.get("affected_relations"), where, lock_mode)
    when = _parse_when(raw.get("when"), where, attribute_names)

    raw_source = raw["source"]
    if isinstance(raw_source, str):
        raw_source = [raw_source]
    if (
        not isinstance(raw_source, list)
        or not raw_source
        or not all(isinstance(s, str) and s.strip() for s in raw_source)
    ):
        raise _fail(where, "source must be a non-empty string or list of strings")
    source = tuple(raw_source)

    # --- semantic checks -------------------------------------------------
    if lock_mode is LockMode.UNKNOWN or duration is DurationModel.UNKNOWN:
        if kind not in UNKNOWN_ALLOWED:
            raise _fail(where, "UNKNOWN lock_mode/duration_model is only allowed for OTHER kinds")
        if calibration is not Calibration.UNCALIBRATED:
            raise _fail(where, "UNKNOWN rows must be UNCALIBRATED")
    if lock_mode is LockMode.UNKNOWN and not (blocks_reads and blocks_writes):
        raise _fail(where, "UNKNOWN lock_mode must assume the worst case: blocks_reads/writes true")

    if calibration is Calibration.CALIBRATED and not any(
        s.startswith(_SOURCE_PREFIXES) for s in source
    ):
        raise _fail(where, "CALIBRATED rows need a doc:/src:/release: citation in source")

    if not available:
        if lock_mode is not LockMode.NONE:
            raise _fail(where, "available: false rows take no lock (lock_mode must be NONE)")
        if failure_mode is None:
            raise _fail(where, "available: false rows must state the failure_mode")
        if duration is not DurationModel.CONSTANT or rewrite:
            raise _fail(where, "available: false rows must be CONSTANT and not rewrite")

    if live and live_context is None:
        raise _fail(where, "requires_live_context needs live_context to say what context")
    if live_context is not None and not live:
        raise _fail(where, "live_context given but requires_live_context is false")
    if txn is not TransactionBlock.ALLOWED and notes is None and failure_mode is None:
        raise _fail(where, "a transaction_block restriction needs notes or failure_mode")

    conflicts = (
        frozenset(TABLE_LOCK_MODES)
        if lock_mode is LockMode.UNKNOWN
        else matrix.get(lock_mode, frozenset())
    )
    if lock_mode.is_table_lock:
        implied_reads = LockMode.ACCESS_SHARE in conflicts
        implied_writes = LockMode.ROW_EXCLUSIVE in conflicts
        if blocks_reads != implied_reads:
            raise _fail(
                where,
                f"blocks_reads={blocks_reads} contradicts the matrix for {lock_mode.value}",
            )
        if implied_writes and not blocks_writes:
            raise _fail(where, f"{lock_mode.value} blocks writes but blocks_writes is false")
    elif lock_mode is LockMode.NONE and (blocks_reads or blocks_writes):
        raise _fail(where, "lock_mode NONE cannot block reads or writes")

    scope_raw = raw.get("write_block_scope")
    if scope_raw is None:
        if not blocks_writes:
            scope = WriteBlockScope.NONE
        elif (
            lock_mode.is_table_lock and LockMode.ROW_EXCLUSIVE in conflicts
        ) or lock_mode is LockMode.UNKNOWN:
            scope = WriteBlockScope.TABLE
        else:
            raise _fail(
                where, "blocks_writes is true but the lock does not imply it: set write_block_scope"
            )
    else:
        scope = _enum(WriteBlockScope, scope_raw, where, "write_block_scope")
    if scope is WriteBlockScope.NONE and blocks_writes:
        raise _fail(where, "write_block_scope NONE contradicts blocks_writes true")
    if scope is not WriteBlockScope.NONE and not blocks_writes:
        raise _fail(where, f"write_block_scope {scope.value} requires blocks_writes true")
    if scope is WriteBlockScope.MATCHED_ROWS and row_locks is None:
        raise _fail(where, "MATCHED_ROWS scope must name the row_locks taken")
    if row_locks is not None and scope is not WriteBlockScope.MATCHED_ROWS:
        raise _fail(where, "row_locks is only meaningful with write_block_scope MATCHED_ROWS")

    # The entry-level lock describes the primary object: the `target` role if
    # present, else the strongest non-optional relation, else NONE.
    by_role = {rel.role: rel for rel in affected}
    if "target" in by_role:
        primary = by_role["target"].lock_mode
    else:
        required = [rel.lock_mode for rel in affected if not rel.optional]
        primary = max(required, key=lambda m: m.level) if required else LockMode.NONE
    if primary is not lock_mode:
        raise _fail(
            where,
            f"lock_mode {lock_mode.value} does not match the primary affected relation "
            f"({primary.value})",
        )
    if lock_mode is LockMode.NONE and any(
        rel.lock_mode is not LockMode.NONE and not rel.optional for rel in affected
    ):
        raise _fail(where, "lock_mode NONE but a required affected relation is locked")

    return CatalogEntry(
        kind=kind,
        pg_versions=versions,
        calibration=calibration,
        available=available,
        lock_mode=lock_mode,
        conflicts_with=conflicts,
        blocks_reads=blocks_reads,
        blocks_writes=blocks_writes,
        write_block_scope=scope,
        row_locks=row_locks,
        requires_table_rewrite=rewrite,
        duration_model=duration,
        requires_live_context=live,
        live_context=live_context,
        transaction_block=txn,
        failure_mode=failure_mode,
        affected_relations=affected,
        when=when,
        source=source,
        notes=notes,
    )


def _check_coverage(
    kind: StatementKind | AlterTableActionKind,
    entries: tuple[CatalogEntry, ...],
    domain: VersionRange,
) -> None:
    """Every version has exactly one default row; variants don't collide."""
    defaults = [e for e in entries if e.is_default_row]
    for version in range(domain.min, domain.max + 1):
        covering = [e for e in defaults if e.pg_versions.covers(version)]
        if not covering:
            raise _fail(kind.value, f"no default row covers PG {version}")
        if len(covering) > 1:
            raise _fail(kind.value, f"{len(covering)} default rows overlap on PG {version}")
    variants = [e for e in entries if not e.is_default_row]
    for i, first in enumerate(variants):
        for second in variants[i + 1 :]:
            if first.when == second.when and first.pg_versions.overlaps(second.pg_versions):
                raise _fail(
                    kind.value,
                    f"two rows with identical when={first.when_mapping} overlap on "
                    f"{first.pg_versions}/{second.pg_versions}",
                )


def _check_breakpoints(
    breakpoints: Iterable[VersionBreakpoint],
    statements: Mapping[StatementKind, tuple[CatalogEntry, ...]],
    actions: Mapping[AlterTableActionKind, tuple[CatalogEntry, ...]],
) -> None:
    for bp in breakpoints:
        rows: tuple[CatalogEntry, ...]
        if isinstance(bp.kind, StatementKind):
            rows = statements.get(bp.kind, ())
        else:
            rows = actions.get(bp.kind, ())
        for entry in rows:
            if entry.pg_versions.min < bp.version <= entry.pg_versions.max:
                raise _fail(
                    bp.kind.value,
                    f"row for PG {entry.pg_versions} spans the {bp.version} breakpoint "
                    f"({bp.what_changed})",
                )


def _parse_breakpoints(raw: object, domain: VersionRange) -> tuple[VersionBreakpoint, ...]:
    where = "version_breakpoints"
    if not isinstance(raw, list):
        raise _fail(where, "must be a list")
    result: list[VersionBreakpoint] = []
    for item in raw:
        if not isinstance(item, Mapping) or set(item) != {
            "kind",
            "version",
            "what_changed",
            "source",
        }:
            raise _fail(
                where, f"each breakpoint needs exactly kind/version/what_changed/source: {item!r}"
            )
        kind = _kind_from_ref(item["kind"], where)
        version = item["version"]
        if not isinstance(version, int) or not (domain.min < version <= domain.max):
            raise _fail(where, f"version {version!r} must lie in ({domain.min}, {domain.max}]")
        result.append(
            VersionBreakpoint(
                kind=kind,
                version=version,
                what_changed=str(item["what_changed"]),
                source=str(item["source"]),
            )
        )
    return tuple(result)


def _kind_from_ref(ref: object, where: str) -> StatementKind | AlterTableActionKind:
    """``statement/<kind>`` or ``alter_table_action/<kind>``."""
    if not isinstance(ref, str) or "/" not in ref:
        raise _fail(
            where,
            f"kind reference {ref!r} must be 'statement/<kind>' or 'alter_table_action/<kind>'",
        )
    namespace, _, name = ref.partition("/")
    try:
        if namespace == "statement":
            return StatementKind(name)
        if namespace == "alter_table_action":
            return AlterTableActionKind(name)
    except ValueError:
        raise _fail(where, f"unknown kind {ref!r}") from None
    raise _fail(where, f"unknown kind namespace in {ref!r}")


def parse_catalog(data: Mapping[str, Any]) -> LockCatalog:
    """Build and validate a :class:`LockCatalog` from decoded YAML."""
    top = "catalog"
    expected_keys = {
        "schema_version",
        "version_domain",
        "conflict_matrix",
        "version_breakpoints",
        "statement_resolution",
        "statements",
        "alter_table_actions",
    }
    if not isinstance(data, Mapping):
        raise _fail(top, "top level must be a mapping")
    if set(data) != expected_keys:
        raise _fail(
            top, f"top-level keys must be exactly {sorted(expected_keys)}, got {sorted(data)}"
        )
    if data["schema_version"] != SCHEMA_VERSION:
        raise _fail(top, f"schema_version {data['schema_version']!r} != {SCHEMA_VERSION}")

    raw_domain = data["version_domain"]
    if (
        not isinstance(raw_domain, Mapping)
        or set(raw_domain) != {"min", "max"}
        or not all(isinstance(raw_domain[k], int) for k in ("min", "max"))
        or raw_domain["min"] > raw_domain["max"]
    ):
        raise _fail(top, "version_domain must be {min: int, max: int}")
    domain = VersionRange(min=raw_domain["min"], max=raw_domain["max"])

    matrix = _parse_conflict_matrix(data["conflict_matrix"])
    attribute_names = _ir_attribute_names()

    raw_resolution = data["statement_resolution"]
    if not isinstance(raw_resolution, Mapping):
        raise _fail(top, "statement_resolution must be a mapping")
    resolution: dict[StatementKind, Resolution] = {}
    for key, value in raw_resolution.items():
        kind = _enum(StatementKind, key, "statement_resolution", "kind")
        mode = _enum(Resolution, value, "statement_resolution", f"{kind.value}")
        if mode is Resolution.DIRECT:
            raise _fail("statement_resolution", f"{kind.value}: direct is the default; omit it")
        resolution[kind] = mode
    if resolution.get(StatementKind.ALTER_TABLE) is not Resolution.PER_ACTION:
        raise _fail("statement_resolution", "alter_table must resolve per_action")
    if resolution.get(StatementKind.DO_BLOCK) is not Resolution.INNER_STATEMENTS:
        raise _fail("statement_resolution", "do_block must resolve via inner_statements")

    raw_statements = data["statements"]
    if not isinstance(raw_statements, Mapping):
        raise _fail("statements", "must be a mapping of kind -> rows")
    statements: dict[StatementKind, tuple[CatalogEntry, ...]] = {}
    for key, rows in raw_statements.items():
        kind = _enum(StatementKind, key, "statements", "kind")
        if kind in resolution:
            raise _fail(
                "statements",
                f"{kind.value} resolves via {resolution[kind].value} and must not have direct rows",
            )
        if not isinstance(rows, list) or not rows:
            raise _fail(kind.value, "rows must be a non-empty list")
        entries = tuple(
            _parse_entry(
                kind, row, i, domain=domain, matrix=matrix, attribute_names=attribute_names
            )
            for i, row in enumerate(rows)
        )
        _check_coverage(kind, entries, domain)
        statements[kind] = entries
    missing_statements = [
        k.value for k in StatementKind if k not in statements and k not in resolution
    ]
    if missing_statements:
        raise _fail("statements", f"no catalog rows for StatementKinds {missing_statements}")

    raw_actions = data["alter_table_actions"]
    if not isinstance(raw_actions, Mapping):
        raise _fail("alter_table_actions", "must be a mapping of kind -> rows")
    actions: dict[AlterTableActionKind, tuple[CatalogEntry, ...]] = {}
    for key, rows in raw_actions.items():
        kind = _enum(AlterTableActionKind, key, "alter_table_actions", "kind")
        if not isinstance(rows, list) or not rows:
            raise _fail(kind.value, "rows must be a non-empty list")
        entries = tuple(
            _parse_entry(
                kind, row, i, domain=domain, matrix=matrix, attribute_names=attribute_names
            )
            for i, row in enumerate(rows)
        )
        _check_coverage(kind, entries, domain)
        actions[kind] = entries
    missing_actions = [k.value for k in AlterTableActionKind if k not in actions]
    if missing_actions:
        raise _fail(
            "alter_table_actions", f"no catalog rows for AlterTableActionKinds {missing_actions}"
        )

    breakpoints = _parse_breakpoints(data["version_breakpoints"], domain)
    _check_breakpoints(breakpoints, statements, actions)

    return LockCatalog(
        version_domain=domain,
        conflict_matrix=matrix,
        breakpoints=breakpoints,
        statement_resolution=resolution,
        statements=statements,
        alter_table_actions=actions,
    )


def load_catalog_file(path: str | Path) -> LockCatalog:
    """Load and validate a catalog from an explicit YAML file."""
    with Path(path).open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    return parse_catalog(data)


@functools.cache
def load_catalog() -> LockCatalog:
    """Load and validate the bundled catalog (cached)."""
    text = (
        resources.files("blastoise.catalog").joinpath(CATALOG_RESOURCE).read_text(encoding="utf-8")
    )
    return parse_catalog(yaml.safe_load(text))
