"""Build the verdict document and its evidence bundle.

The document (called the **Shell Report** in prose; the payload is plain
JSON) is the tool's output artifact: a versioned, signable account of what
one migration file will do, what could not be checked, and what cannot be
undone. Every claim in it references the evidence bundle — the raw inputs
the engine consumed — by file name and sha256, so a verdict can be traced
back to the exact bytes that produced it.

Two invariants are enforced here rather than documented:

* ``unverified`` never serializes empty. The honest account of what was
  not checked is the point of the artifact; an engine run that claims to
  have verified everything is a bug, and :func:`build_report` raises on it.
* Every hash in the payload is the sha256 of bytes that exist in the
  bundle mapping, computed from the same canonical serialization that
  would be written to disk — whether or not the bundle is written.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from blastoise.catalog import LockCatalog
from blastoise.catalog.model import Calibration
from blastoise.catalog.resolve import resolve
from blastoise.ir import MigrationScript
from blastoise.live.model import LiveSnapshot
from blastoise.report.model import SCHEMA_VERSION, file_verdict
from blastoise.report.serialize import canonical_json, jsonable, sha256_hex
from blastoise.verdict import constants as _k
from blastoise.verdict.constants import DURATION_CONSTANTS
from blastoise.verdict.model import (
    CannotEstimate,
    Classification,
    DurationEstimate,
    Reversibility,
    ScriptAssessment,
    StatementAssessment,
)

_BASE_EVIDENCE = ("migration.sql", "parse_tree.json", "catalog_rows.json")


def tool_version() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("blastoise")
    except PackageNotFoundError:  # pragma: no cover - installed in every env we run
        return "0+unknown"


def _statement_payload(
    statement: StatementAssessment, *, online: bool
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    uses_constants = False
    for row in statement.rows:
        if isinstance(row.duration, DurationEstimate):
            uses_constants = True
            duration: dict[str, Any] = {
                "type": "estimate",
                "point_ms": row.duration.point_ms,
                "low_ms": row.duration.low_ms,
                "high_ms": row.duration.high_ms,
                "band": str(row.duration.band),
                "confidence": row.duration.confidence,
                "method": str(row.duration.method),
                "constant_key": row.duration.constant_key,
                "inputs": list(row.duration.inputs),
            }
        else:
            duration = {
                "type": "cannot_estimate",
                "reason": row.duration.reason,
                "method": str(row.duration.method),
            }
        rows.append(
            {
                "kind": str(row.kind),
                "lock_mode": str(row.lock_mode),
                "relations": jsonable(row.relations),
                "duration": duration,
                "contention": jsonable(row.contention),
                "verdict": {
                    "classification": str(row.verdict.classification),
                    "band": None if row.verdict.band is None else str(row.verdict.band),
                    "method": str(row.verdict.method),
                    "rationale": row.verdict.rationale,
                    "conditions": list(row.verdict.conditions),
                    "refusal": row.verdict.refusal,
                    "refused_from": (
                        None if row.verdict.refused_from is None
                        else str(row.verdict.refused_from)
                    ),
                },
                "narrowings": list(row.narrowings),
                "notes": list(row.notes),
            }
        )

    # Statement-level view of every relation any row locks, deduplicated —
    # the rows keep the full per-row detail.
    seen: set[tuple[str | None, str, str]] = set()
    relations: list[dict[str, Any]] = []
    for row in statement.rows:
        for rel in row.relations:
            key = (rel.relation, rel.role, str(rel.lock_mode))
            if key in seen:
                continue
            seen.add(key)
            relations.append(
                {
                    "relation": rel.relation,
                    "role": rel.role,
                    "lock_mode": str(rel.lock_mode),
                    "blocks_reads": rel.blocks_reads,
                    "blocks_writes": rel.blocks_writes,
                    "certain": rel.certain,
                    "method": str(rel.method),
                }
            )

    evidence = list(_BASE_EVIDENCE)
    if uses_constants:
        evidence.append("duration_constants.json")
    if online:
        evidence.append("snapshot.json")

    return {
        "index": statement.statement_index,
        "line": statement.line,
        "kind": str(statement.kind),
        "sql": statement.sql,
        "classification": str(statement.verdict.classification),
        "band": None if statement.verdict.band is None else str(statement.verdict.band),
        "method": str(statement.verdict.method),
        "rationale": statement.verdict.rationale,
        "conditions": list(statement.verdict.conditions),
        "refusal": statement.verdict.refusal,
        "refused_from": (
            None if statement.verdict.refused_from is None
            else str(statement.verdict.refused_from)
        ),
        "statement_lock_mode": str(statement.statement_lock_mode),
        "relations": relations,
        "reversibility": {
            "reversibility": str(statement.reversibility.reversibility),
            "method": str(statement.reversibility.method),
            "basis": statement.reversibility.basis,
            "what_is_lost": statement.reversibility.what_is_lost,
        },
        "rows": rows,
        "notes": list(statement.notes),
        "evidence": evidence,
    }


def _irreversible_payload(assessment: ScriptAssessment) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for statement in assessment.statements:
        rev = statement.reversibility
        if rev.reversibility is not Reversibility.IRREVERSIBLE:
            continue
        entries.append(
            {
                "index": statement.statement_index,
                "line": statement.line,
                "kind": str(statement.kind),
                "what_is_lost": rev.what_is_lost or "state is lost",
                "basis": rev.basis,
                "method": str(rev.method),
            }
        )
    return entries


def _rollback_payload(assessment: ScriptAssessment) -> dict[str, Any]:
    blockers: list[dict[str, Any]] = []
    undecided: list[dict[str, Any]] = []
    for statement in assessment.statements:
        rev = statement.reversibility
        if rev.reversibility is Reversibility.IRREVERSIBLE:
            blockers.append(
                {
                    "index": statement.statement_index,
                    "line": statement.line,
                    "reason": rev.what_is_lost or rev.basis,
                }
            )
        elif rev.reversibility is Reversibility.UNKNOWN:
            undecided.append(
                {
                    "index": statement.statement_index,
                    "line": statement.line,
                    "reason": rev.basis,
                }
            )
    if blockers:
        feasible = "no"
        basis = (
            f"{len(blockers)} statement(s) destroy state that no reverse "
            "migration can recreate"
        )
    elif undecided:
        feasible = "unknown"
        basis = (
            f"reversibility of {len(undecided)} statement(s) could not be "
            "determined; a rollback plan cannot be promised"
        )
    else:
        feasible = "yes"
        basis = (
            "every statement's committed effect can be undone by a reverse "
            "migration; rolling back still requires writing and running one"
        )
    return {"feasible": feasible, "basis": basis, "blockers": blockers, "undecided": undecided}


def _collect_unverified(
    assessment: ScriptAssessment, *, degraded_reason: str | None
) -> list[dict[str, Any]]:
    """Everything the engine did not check, and why.

    Ordered file-level structural entries first, then per-statement entries
    by statement index, then the calibration state of every duration
    constant an estimate leaned on — deterministic, so the canonical
    serialization is stable.
    """
    entries: list[dict[str, Any]] = []

    def add(source: str, reason: str, statement: StatementAssessment | None = None) -> None:
        entries.append(
            {
                "index": None if statement is None else statement.statement_index,
                "line": None if statement is None else statement.line,
                "source": source,
                "reason": reason,
            }
        )

    if not assessment.online:
        reason = (
            "no live snapshot: relation sizes, statistics, current lock "
            "holders, replication state, and schema facts were not checked; "
            "every size- or state-dependent judgment is unknown"
        )
        if degraded_reason:
            reason = f"{degraded_reason}; {reason}"
        add("no_snapshot", reason)
    else:
        add(
            "snapshot_limits",
            "the snapshot describes the database at capture time, not at "
            "execution time; it is not transactionally consistent across "
            "sections, and reltuples is an estimate even when fresh",
        )
    add(
        "execution_state",
        "the lock acquisition queue at execution time cannot be known in "
        "advance: any transaction open when the migration runs can make it "
        "wait, and everything arriving later queues behind that wait",
    )

    constants_used: set[str] = set()
    for statement in assessment.statements:
        if statement.verdict.classification is Classification.UNKNOWN:
            add("unknown_classification", statement.verdict.rationale, statement)
        for row in statement.rows:
            if isinstance(row.duration, CannotEstimate):
                add("cannot_estimate", row.duration.reason, statement)
            elif row.duration.constant_key is not None:
                constants_used.add(row.duration.constant_key)

    for key in sorted(constants_used):
        constant = DURATION_CONSTANTS[key]
        if constant.calibration is Calibration.UNCALIBRATED:
            add(
                "uncalibrated_constant",
                f"duration constant '{key}' is an uncalibrated guess; "
                "estimates built on it carry a 4x-widened interval",
            )
        elif constant.calibration is Calibration.MEASURED:
            add(
                "uncalibrated_constant",
                f"duration constant '{key}' was measured on one uncontended "
                "machine, not calibrated across environments; production can "
                "only be slower",
            )
    return entries


def _duration_constants_payload() -> dict[str, Any]:
    return {
        "constants": {
            key: {
                "unit": str(constant.unit),
                "value": constant.value,
                "calibration": str(constant.calibration),
                "runs": constant.runs,
                "spread_tenths": constant.spread_tenths,
                "widen_tenths": _k.base_widen_tenths(constant),
                "basis": constant.basis,
            }
            for key, constant in sorted(DURATION_CONSTANTS.items())
        },
        "thresholds_ms": {
            "full_block_short": _k.FULL_BLOCK_SHORT_MS,
            "full_block_long": _k.FULL_BLOCK_LONG_MS,
            "write_block_short": _k.WRITE_BLOCK_SHORT_MS,
            "write_block_long": _k.WRITE_BLOCK_LONG_MS,
        },
        "bands_ms": {
            "sub_second_max": _k.BAND_SUB_SECOND_MAX_MS,
            "seconds_max": _k.BAND_SECONDS_MAX_MS,
            "minutes_max": _k.BAND_MINUTES_MAX_MS,
        },
        "widening_tenths": {
            "uncalibrated": _k.WIDEN_UNCALIBRATED_TENTHS,
            "measured_single_run": _k.WIDEN_MEASURED_SINGLE_RUN_TENTHS,
            "measured_drift_floor": _k.WIDEN_MEASURED_DRIFT_FLOOR_TENTHS,
            "measured_cap": _k.WIDEN_MEASURED_CAP_TENTHS,
            "calibrated": _k.WIDEN_CALIBRATED_TENTHS,
            "constant_op": _k.WIDEN_CONSTANT_OP_TENTHS,
            "note": (
                "a measured constant's band is derived per-constant from its "
                "runs and spread_tenths (see each constant's widen_tenths), not "
                "a flat per-tier multiplier"
            ),
        },
    }


def _catalog_rows_payload(
    script: MigrationScript, catalog: LockCatalog, pg_version: int
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for index, statement in enumerate(script.statements):
        rows: list[dict[str, Any]] = []
        for resolved in resolve(catalog, statement, pg_version):
            rows.append(
                {
                    "entry": jsonable(resolved.entry),
                    "relations": jsonable(resolved.relations),
                    "action_kind": None
                    if resolved.action is None
                    else str(resolved.action.kind),
                    "in_do_block": resolved.in_do_block,
                    "conditional": resolved.conditional,
                }
            )
        out.append({"statement_index": index, "line": statement.span.line, "rows": rows})
    return out


def build_report(
    script: MigrationScript,
    assessment: ScriptAssessment,
    *,
    catalog: LockCatalog,
    snapshot: LiveSnapshot | None,
    evaluated_at: str,
    change_id: str | None = None,
    version: str | None = None,
    bundle_dir: str | None = None,
    degraded_reason: str | None = None,
    notes: tuple[str, ...] = (),
) -> tuple[dict[str, Any], dict[str, bytes]]:
    """The verdict document payload and its evidence bundle.

    Returns ``(payload, bundle)`` where ``bundle`` maps evidence file name
    to the exact bytes :func:`write_bundle` would put on disk; the payload's
    evidence section carries the sha256 of those bytes whether or not they
    are written. ``bundle_dir`` is recorded verbatim as the path (relative
    to the report file) where the bundle will live, or ``None`` when the
    caller does not intend to write it. ``change_id`` defaults to the
    sha256 of the migration source. ``degraded_reason`` records why a
    requested live connection fell back to offline.

    Raises :class:`AssertionError` if ``unverified`` would serialize empty:
    that is never a true account of an assessment.
    """
    source_bytes = script.source.encode("utf-8")

    bundle: dict[str, bytes] = {
        "migration.sql": source_bytes,
        "parse_tree.json": _parse_tree_bytes(script),
        "catalog_rows.json": canonical_json(
            _catalog_rows_payload(script, catalog, assessment.pg_version)
        ).encode("ascii"),
        "duration_constants.json": canonical_json(_duration_constants_payload()).encode(
            "ascii"
        ),
    }
    snapshot_hash: str | None = None
    if snapshot is not None:
        snapshot_bytes = snapshot.to_canonical_json().encode("ascii")
        bundle["snapshot.json"] = snapshot_bytes
        snapshot_hash = sha256_hex(snapshot_bytes)

    unverified = _collect_unverified(assessment, degraded_reason=degraded_reason)
    if not unverified:
        raise AssertionError(
            "unverified is empty: the engine always leaves something "
            "unchecked (execution-time lock queues at minimum), so an empty "
            "list is a collection bug, not a clean bill of health"
        )

    classifications = tuple(s.verdict.classification for s in assessment.statements)
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "tool_version": tool_version() if version is None else version,
        "change_id": sha256_hex(source_bytes) if change_id is None else change_id,
        "evaluated_at": evaluated_at,
        "pg_version": assessment.pg_version,
        "online": assessment.online,
        "verdict": str(file_verdict(classifications)),
        "classification_counts": {
            str(tier): count for tier, count in assessment.classification_counts().items()
        },
        "snapshot_hash": snapshot_hash,
        "statements": [
            _statement_payload(statement, online=assessment.online)
            for statement in assessment.statements
        ],
        "irreversible": _irreversible_payload(assessment),
        "unverified": unverified,
        "rollback": _rollback_payload(assessment),
        "transaction_warnings": [
            {
                "relations": list(warning.relations),
                "description": warning.description,
                "method": str(warning.method),
                "hypothetical": warning.hypothetical,
            }
            for warning in assessment.transaction_warnings
        ],
        "notes": [*assessment.notes, *notes],
        "evidence": {
            "bundle_dir": bundle_dir,
            "files": [
                {"name": name, "sha256": sha256_hex(data), "bytes": len(data)}
                for name, data in sorted(bundle.items())
            ],
        },
    }
    return payload, bundle


def _parse_tree_bytes(script: MigrationScript) -> bytes:
    tree = jsonable(script)
    assert isinstance(tree, dict)
    # The raw source lives in migration.sql; carrying it again would only
    # bloat the tree without adding evidence.
    tree.pop("source", None)
    return canonical_json(tree).encode("ascii")


def write_bundle(bundle: dict[str, bytes], directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name, data in bundle.items():
        (directory / name).write_bytes(data)


def check_evidence(payload: dict[str, Any], report_path: Path) -> tuple[bool, list[str]]:
    """Confirm every evidence file exists and matches its recorded sha256.

    Returns ``(all_ok, lines)`` where ``lines`` describe each file's result.
    Also cross-checks ``snapshot_hash`` against the bundle's snapshot entry:
    a report whose headline hash disagrees with its own evidence manifest
    has been tampered with or mis-built.
    """
    evidence = payload.get("evidence")
    if not isinstance(evidence, dict):
        return False, ["report has no evidence section"]
    bundle_dir = evidence.get("bundle_dir")
    files = evidence.get("files")
    if not isinstance(files, list):
        return False, ["report has no evidence file list"]
    if bundle_dir is None:
        return False, [
            "evidence bundle was not written when this report was produced; "
            "its hashes reference files that do not exist on disk"
        ]

    base = report_path.parent / str(bundle_dir)
    ok = True
    lines: list[str] = []
    recorded: dict[str, str] = {}
    for entry in files:
        name = str(entry.get("name", ""))
        expected = str(entry.get("sha256", ""))
        recorded[name] = expected
        if not name or Path(name).name != name:
            ok = False
            lines.append(f"BAD NAME  {name!r}: evidence names must be plain file names")
            continue
        target = base / name
        if not target.is_file():
            ok = False
            lines.append(f"MISSING   {name}")
            continue
        actual = sha256_hex(target.read_bytes())
        if actual != expected:
            ok = False
            lines.append(f"MISMATCH  {name}: recorded {expected[:12]}..., found {actual[:12]}...")
        else:
            lines.append(f"ok        {name}")

    snapshot_hash = payload.get("snapshot_hash")
    if snapshot_hash is not None and recorded.get("snapshot.json") != snapshot_hash:
        ok = False
        lines.append(
            "MISMATCH  snapshot_hash does not match the evidence manifest's "
            "entry for snapshot.json"
        )
    return ok, lines
