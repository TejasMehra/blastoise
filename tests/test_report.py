"""The verdict document: schema, round-trip stability, and honesty invariants."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from verdict_helpers import relation, snapshot

from blastoise.catalog import LockCatalog, load_catalog
from blastoise.parser import parse_migration
from blastoise.report import (
    EXIT_CODES,
    EXIT_TOOL_ERROR,
    SCHEMA_VERSION,
    FileVerdict,
    build_report,
    canonical_json,
    check_evidence,
    exit_code,
    file_verdict,
    jsonable,
    render_report,
    sha256_hex,
    write_bundle,
)
from blastoise.report.build import _collect_unverified
from blastoise.verdict import Classification, assess_script

EVALUATED_AT = "2026-08-22T12:00:00+00:00"


@pytest.fixture(scope="module")
def catalog() -> LockCatalog:
    return load_catalog()


def _offline_report(
    catalog: LockCatalog, sql: str, **kwargs: Any
) -> tuple[dict[str, Any], dict[str, bytes]]:
    script = parse_migration(sql)
    assessment = assess_script(script, catalog, 17, None)
    return build_report(
        script,
        assessment,
        catalog=catalog,
        snapshot=None,
        evaluated_at=EVALUATED_AT,
        **kwargs,
    )


def _online_report(
    catalog: LockCatalog, sql: str, snap: Any, **kwargs: Any
) -> tuple[dict[str, Any], dict[str, bytes]]:
    script = parse_migration(sql)
    assessment = assess_script(script, catalog, 17, snap)
    return build_report(
        script,
        assessment,
        catalog=catalog,
        snapshot=snap,
        evaluated_at=EVALUATED_AT,
        **kwargs,
    )


class TestFileVerdict:
    def test_mapping_from_worst_classification(self) -> None:
        safe = Classification.SAFE
        assert file_verdict((safe, Classification.SAFE_IRREVERSIBLE)) is FileVerdict.PROCEED
        assert file_verdict((safe, Classification.NEEDS_TIMING)) is (
            FileVerdict.REQUIRES_APPROVAL
        )
        assert file_verdict((safe, Classification.UNKNOWN)) is FileVerdict.REQUIRES_APPROVAL
        # UNSAFE outranks UNKNOWN in the combine order, and BLOCK must win.
        assert file_verdict(
            (Classification.UNKNOWN, Classification.UNSAFE, safe)
        ) is FileVerdict.BLOCK

    def test_empty_file_proceeds(self) -> None:
        assert file_verdict(()) is FileVerdict.PROCEED

    def test_exit_codes_are_pinned(self) -> None:
        # CI reads these. Renumbering any of them is a breaking change.
        assert {str(v): exit_code(v) for v in FileVerdict} == {
            "proceed": 0,
            "requires_approval": 1,
            "block": 2,
        }
        assert EXIT_CODES[FileVerdict.PROCEED] == 0
        assert EXIT_TOOL_ERROR == 3


class TestRoundTrip:
    def test_offline_report_serializes_stably(self, catalog: LockCatalog) -> None:
        payload, _ = _offline_report(
            catalog, "CREATE TABLE t (id int);\nCREATE INDEX i ON users (email);\n"
        )
        first = canonical_json(payload)
        second = canonical_json(json.loads(first))
        assert first == second

    def test_online_report_serializes_stably(self, catalog: LockCatalog) -> None:
        snap = snapshot(relations=(relation("users", rows=200),))
        payload, _ = _online_report(catalog, "CREATE INDEX i ON users (email);\n", snap)
        first = canonical_json(payload)
        assert canonical_json(json.loads(first)) == first

    def test_two_builds_of_the_same_inputs_are_identical(
        self, catalog: LockCatalog
    ) -> None:
        sql = "ALTER TABLE users ADD COLUMN nickname text;\n"
        a, bundle_a = _offline_report(catalog, sql)
        b, bundle_b = _offline_report(catalog, sql)
        assert canonical_json(a) == canonical_json(b)
        assert bundle_a == bundle_b

    def test_written_report_reads_back_identically(
        self, catalog: LockCatalog, tmp_path: Path
    ) -> None:
        payload, _ = _offline_report(catalog, "CREATE TABLE t (id int);\n")
        text = canonical_json(payload)
        target = tmp_path / "report.json"
        target.write_text(text + "\n", encoding="ascii")
        assert canonical_json(json.loads(target.read_text(encoding="ascii"))) == text

    def test_jsonable_bans_floats(self) -> None:
        with pytest.raises(TypeError, match="floats are banned"):
            jsonable({"x": 1.5})


class TestSchema:
    def test_top_level_fields(self, catalog: LockCatalog) -> None:
        payload, _ = _offline_report(catalog, "CREATE TABLE t (id int);\n")
        assert payload["schema_version"] == SCHEMA_VERSION
        assert isinstance(payload["tool_version"], str) and payload["tool_version"]
        assert payload["evaluated_at"] == EVALUATED_AT
        assert payload["pg_version"] == 17
        assert payload["online"] is False
        assert payload["snapshot_hash"] is None
        assert payload["verdict"] == "proceed"
        assert set(payload["classification_counts"]) == {
            "safe",
            "safe_irreversible",
            "needs_timing",
            "unsafe",
            "unknown",
        }

    def test_change_id_defaults_to_source_sha256(self, catalog: LockCatalog) -> None:
        sql = "CREATE TABLE t (id int);\n"
        payload, _ = _offline_report(catalog, sql)
        assert payload["change_id"] == sha256_hex(sql.encode("utf-8"))
        overridden, _ = _offline_report(catalog, sql, change_id="PR-4242")
        assert overridden["change_id"] == "PR-4242"

    def test_statements_carry_the_mandated_fields(self, catalog: LockCatalog) -> None:
        payload, _ = _offline_report(catalog, "CREATE INDEX i ON users (email);\n")
        (statement,) = payload["statements"]
        assert statement["classification"] == "unknown"
        assert statement["method"] == "unverified"
        assert statement["band"] is None
        (rel,) = statement["relations"]
        assert rel["relation"] == "users"
        assert rel["lock_mode"] == "SHARE"
        assert rel["blocks_writes"] is True
        assert statement["evidence"]  # references into the bundle
        assert all(isinstance(ref, str) for ref in statement["evidence"])

    def test_snapshot_hash_matches_bundle_entry(self, catalog: LockCatalog) -> None:
        snap = snapshot(relations=(relation("users", rows=200),))
        payload, bundle = _online_report(catalog, "CREATE INDEX i ON users (email);\n", snap)
        assert payload["online"] is True
        assert payload["snapshot_hash"] == sha256_hex(bundle["snapshot.json"])
        manifest = {f["name"]: f["sha256"] for f in payload["evidence"]["files"]}
        assert manifest["snapshot.json"] == payload["snapshot_hash"]

    def test_evidence_hashes_match_bundle_bytes(self, catalog: LockCatalog) -> None:
        payload, bundle = _offline_report(catalog, "CREATE TABLE t (id int);\n")
        manifest = {f["name"]: f for f in payload["evidence"]["files"]}
        assert set(manifest) == set(bundle) == {
            "migration.sql",
            "parse_tree.json",
            "catalog_rows.json",
            "duration_constants.json",
        }
        for name, data in bundle.items():
            assert manifest[name]["sha256"] == sha256_hex(data)
            assert manifest[name]["bytes"] == len(data)

    def test_online_statement_references_the_snapshot(self, catalog: LockCatalog) -> None:
        snap = snapshot(relations=(relation("users", rows=200),))
        payload, _ = _online_report(catalog, "CREATE INDEX i ON users (email);\n", snap)
        (statement,) = payload["statements"]
        assert "snapshot.json" in statement["evidence"]
        assert "duration_constants.json" in statement["evidence"]

    def test_verdict_blocks_on_unsafe(self, catalog: LockCatalog) -> None:
        snap = snapshot(relations=(relation("users", rows=400_000_000),))
        payload, _ = _online_report(catalog, "CREATE INDEX i ON users (email);\n", snap)
        (statement,) = payload["statements"]
        assert statement["classification"] == "unsafe"
        assert payload["verdict"] == "block"


class TestUnverified:
    def test_never_empty_offline(self, catalog: LockCatalog) -> None:
        payload, _ = _offline_report(catalog, "CREATE TABLE t (id int);\n")
        assert payload["unverified"]
        sources = {entry["source"] for entry in payload["unverified"]}
        assert "no_snapshot" in sources

    def test_never_empty_online_even_when_everything_resolved(
        self, catalog: LockCatalog
    ) -> None:
        # A fully decided online assessment still has honest residue: the
        # snapshot's own limits and the unknowable execution-time queue.
        snap = snapshot(relations=(relation("users", rows=200),))
        payload, _ = _online_report(catalog, "CREATE INDEX i ON users (email);\n", snap)
        sources = {entry["source"] for entry in payload["unverified"]}
        assert "snapshot_limits" in sources
        assert "execution_state" in sources

    def test_empty_unverified_is_a_bug_and_raises(
        self, catalog: LockCatalog, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import blastoise.report.build as build_module

        monkeypatch.setattr(build_module, "_collect_unverified", lambda *a, **k: [])
        script = parse_migration("CREATE TABLE t (id int);\n")
        assessment = assess_script(script, catalog, 17, None)
        with pytest.raises(AssertionError, match="unverified is empty"):
            build_report(
                script,
                assessment,
                catalog=catalog,
                snapshot=None,
                evaluated_at=EVALUATED_AT,
            )

    def test_collector_attributes_unknowns_to_statements(
        self, catalog: LockCatalog
    ) -> None:
        script = parse_migration("CREATE INDEX i ON users (email);\n")
        assessment = assess_script(script, catalog, 17, None)
        entries = _collect_unverified(assessment, degraded_reason=None)
        per_statement = [e for e in entries if e["index"] is not None]
        assert per_statement
        assert {e["source"] for e in per_statement} >= {
            "unknown_classification",
            "cannot_estimate",
        }

    def test_degraded_reason_lands_in_no_snapshot_entry(
        self, catalog: LockCatalog
    ) -> None:
        payload, _ = _offline_report(
            catalog,
            "CREATE TABLE t (id int);\n",
            degraded_reason="live snapshot unavailable (connection refused)",
        )
        (entry,) = [e for e in payload["unverified"] if e["source"] == "no_snapshot"]
        assert "connection refused" in entry["reason"]

    def test_uncalibrated_constants_are_declared(self, catalog: LockCatalog) -> None:
        # CREATE TABLE estimates via constant_op, an admitted guess.
        payload, _ = _offline_report(catalog, "CREATE TABLE t (id int);\n")
        reasons = [
            e["reason"]
            for e in payload["unverified"]
            if e["source"] == "uncalibrated_constant"
        ]
        assert any("constant_op" in reason for reason in reasons)


class TestIrreversibleAndRollback:
    def test_irreversible_statement_is_listed_and_blocks_rollback(
        self, catalog: LockCatalog
    ) -> None:
        payload, _ = _offline_report(catalog, "DROP TABLE users;\n")
        (entry,) = payload["irreversible"]
        assert entry["kind"] == "drop_table"
        assert entry["what_is_lost"]
        rollback = payload["rollback"]
        assert rollback["feasible"] == "no"
        assert rollback["blockers"]
        assert rollback["blockers"][0]["reason"]

    def test_reversible_file_is_rollback_feasible(self, catalog: LockCatalog) -> None:
        payload, _ = _offline_report(catalog, "CREATE TABLE t (id int);\n")
        assert payload["irreversible"] == []
        assert payload["rollback"]["feasible"] == "yes"
        assert payload["rollback"]["blockers"] == []


class TestEvidenceChecking:
    def test_written_bundle_verifies(self, catalog: LockCatalog, tmp_path: Path) -> None:
        payload, bundle = _offline_report(
            catalog, "CREATE TABLE t (id int);\n", bundle_dir="evidence"
        )
        write_bundle(bundle, tmp_path / "evidence")
        report_path = tmp_path / "report.json"
        report_path.write_text(canonical_json(payload), encoding="ascii")
        ok, lines = check_evidence(payload, report_path)
        assert ok, lines

    def test_hash_mismatch_is_detected(self, catalog: LockCatalog, tmp_path: Path) -> None:
        payload, bundle = _offline_report(
            catalog, "CREATE TABLE t (id int);\n", bundle_dir="evidence"
        )
        write_bundle(bundle, tmp_path / "evidence")
        (tmp_path / "evidence" / "migration.sql").write_bytes(b"-- tampered\n")
        report_path = tmp_path / "report.json"
        report_path.write_text(canonical_json(payload), encoding="ascii")
        ok, lines = check_evidence(payload, report_path)
        assert not ok
        assert any("MISMATCH" in line and "migration.sql" in line for line in lines)

    def test_missing_file_is_detected(self, catalog: LockCatalog, tmp_path: Path) -> None:
        payload, bundle = _offline_report(
            catalog, "CREATE TABLE t (id int);\n", bundle_dir="evidence"
        )
        write_bundle(bundle, tmp_path / "evidence")
        (tmp_path / "evidence" / "parse_tree.json").unlink()
        report_path = tmp_path / "report.json"
        report_path.write_text(canonical_json(payload), encoding="ascii")
        ok, lines = check_evidence(payload, report_path)
        assert not ok
        assert any("MISSING" in line and "parse_tree.json" in line for line in lines)

    def test_unwritten_bundle_fails_the_check(
        self, catalog: LockCatalog, tmp_path: Path
    ) -> None:
        payload, _ = _offline_report(catalog, "CREATE TABLE t (id int);\n")
        assert payload["evidence"]["bundle_dir"] is None
        ok, lines = check_evidence(payload, tmp_path / "report.json")
        assert not ok
        assert any("not written" in line for line in lines)

    def test_tampered_snapshot_hash_is_detected(
        self, catalog: LockCatalog, tmp_path: Path
    ) -> None:
        snap = snapshot(relations=(relation("users", rows=200),))
        payload, bundle = _online_report(
            catalog, "CREATE INDEX i ON users (email);\n", snap, bundle_dir="evidence"
        )
        write_bundle(bundle, tmp_path / "evidence")
        payload["snapshot_hash"] = "0" * 64
        report_path = tmp_path / "report.json"
        report_path.write_text(canonical_json(payload), encoding="ascii")
        ok, lines = check_evidence(payload, report_path)
        assert not ok
        assert any("snapshot_hash" in line for line in lines)


class TestRendering:
    def test_leads_with_verdict_and_tier_counts(self, catalog: LockCatalog) -> None:
        payload, _ = _offline_report(
            catalog, "CREATE TABLE t (id int);\nCREATE INDEX i ON users (email);\n"
        )
        text = render_report(payload)
        lines = text.splitlines()
        assert lines[0] == "SHELL REPORT"
        assert lines[2] == "verdict: REQUIRES_APPROVAL"
        assert "pressure levels" in text
        # every machine tier name appears with its count and display name
        assert "unknown" in text and "Fog" in text
        assert "safe" in text and "Calm Water" in text

    def test_rendering_is_ascii(self, catalog: LockCatalog) -> None:
        payload, _ = _offline_report(catalog, "DROP TABLE users;\n")
        assert render_report(payload).isascii()
        assert render_report(payload, expanded=True).isascii()

    def test_expanded_form_shows_locks_and_evidence(self, catalog: LockCatalog) -> None:
        snap = snapshot(relations=(relation("users", rows=200),))
        payload, _ = _online_report(catalog, "CREATE INDEX i ON users (email);\n", snap)
        text = render_report(payload, expanded=True)
        assert "lock: SHARE on users" in text
        assert "evidence: migration.sql" in text
        compact = render_report(payload)
        assert "lock: SHARE on users" not in compact

    def test_theme_stays_out_of_the_json(self, catalog: LockCatalog) -> None:
        payload, _ = _offline_report(catalog, "CREATE TABLE t (id int);\n")
        text = canonical_json(payload).lower()
        for phrase in ("calm water", "hydro pump", "rain check", "one-way current"):
            assert phrase not in text
