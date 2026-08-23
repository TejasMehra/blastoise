"""In-process CLI tests for check / verify / explain and their exit codes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from blastoise.cli import main
from blastoise.report import REPORT_FILENAME, SCHEMA_VERSION

SEED_HEX = bytes(range(32)).hex()


def _write(tmp_path: Path, name: str, content: str) -> Path:
    file_path = tmp_path / name
    file_path.write_text(content, encoding="utf-8")
    return file_path


def _themed_json_keys(value: object) -> list[str]:
    themed = ("shell", "hydro", "pressure", "torrent", "seal", "blastoise")
    keys: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if any(word in key.lower() for word in themed):
                keys.append(key)
            keys.extend(_themed_json_keys(item))
    elif isinstance(value, list):
        for item in value:
            keys.extend(_themed_json_keys(item))
    return keys


class TestCheckExitCodes:
    """0 proceed, 1 requires_approval, 2 block, 3 tool error - CI reads these."""

    def test_proceed_exits_0(self, tmp_path: Path) -> None:
        migration = _write(tmp_path, "safe.sql", "CREATE TABLE t (id int);\n")
        assert main(["check", str(migration)]) == 0

    def test_safe_irreversible_still_proceeds(self, tmp_path: Path) -> None:
        migration = _write(
            tmp_path, "sirr.sql", "CREATE TABLE t (id int);\nDROP TABLE t;\n"
        )
        assert main(["check", str(migration)]) == 0

    def test_unknown_requires_approval_exits_1(self, tmp_path: Path) -> None:
        migration = _write(tmp_path, "idx.sql", "CREATE INDEX i ON users (email);\n")
        assert main(["check", str(migration)]) == 1

    def test_needs_timing_requires_approval_exits_1(self, tmp_path: Path) -> None:
        migration = _write(
            tmp_path, "backfill.sql", "INSERT INTO archive SELECT * FROM events;\n"
        )
        assert main(["check", str(migration)]) == 1

    def test_block_exits_2(self, tmp_path: Path) -> None:
        migration = _write(
            tmp_path,
            "boom.sql",
            "BEGIN;\nCREATE INDEX CONCURRENTLY i ON t (c);\nCOMMIT;\n",
        )
        assert main(["check", str(migration)]) == 2

    def test_missing_file_is_tool_error_3(self, tmp_path: Path) -> None:
        assert main(["check", str(tmp_path / "nope.sql")]) == 3

    def test_unparseable_file_is_tool_error_3(self, tmp_path: Path) -> None:
        migration = _write(tmp_path, "broken.sql", "CREATE TABLE oops (id int\n")
        assert main(["check", str(migration)]) == 3

    def test_usage_error_is_tool_error_3(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as excinfo:
            main(["check", "--bogus-flag"])
        assert excinfo.value.code == 3
        capsys.readouterr()

    def test_broken_sign_key_is_tool_error_3(self, tmp_path: Path) -> None:
        migration = _write(tmp_path, "m.sql", "CREATE TABLE t (id int);\n")
        bad_key = _write(tmp_path, "bad.key", "this is not a key")
        assert main(["check", str(migration), "--sign-key", str(bad_key)]) == 3


class TestCheckOutput:
    def test_json_mode_emits_the_canonical_payload(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        migration = _write(tmp_path, "m.sql", "CREATE TABLE t (id int);\n")
        assert main(["check", "--json", str(migration)]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["schema_version"] == SCHEMA_VERSION
        assert payload["verdict"] == "proceed"
        assert payload["unverified"]
        assert _themed_json_keys(payload) == []

    def test_terminal_mode_leads_with_verdict_and_is_ascii(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        migration = _write(tmp_path, "m.sql", "CREATE INDEX i ON users (email);\n")
        assert main(["check", str(migration)]) == 1
        out = capsys.readouterr().out
        assert out.startswith("SHELL REPORT")
        assert "verdict: REQUIRES_APPROVAL" in out
        assert out.isascii()

    def test_verbose_prints_timing_breakdown_on_stderr(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        migration = _write(tmp_path, "m.sql", "CREATE TABLE t (id int);\n")
        assert main(["check", "--verbose", "--json", str(migration)]) == 0
        captured = capsys.readouterr()
        assert "timing:" in captured.err
        assert "total" in captured.err
        json.loads(captured.out)  # stdout stays pure JSON

    def test_pg_version_flag_is_recorded(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        migration = _write(tmp_path, "m.sql", "CREATE TABLE t (id int);\n")
        assert main(["check", "--json", "--pg-version", "14", str(migration)]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["pg_version"] == 14

    def test_unreachable_database_degrades_to_offline_with_loud_warning(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        migration = _write(tmp_path, "m.sql", "CREATE TABLE t (id int);\n")
        code = main(
            [
                "check",
                "--json",
                "--database-url",
                "postgresql://nobody@127.0.0.1:1/nothing?connect_timeout=1",
                str(migration),
            ]
        )
        captured = capsys.readouterr()
        assert code == 0  # still a verdict, not a tool error
        assert "WARNING" in captured.err
        assert "degrading to offline" in captured.err
        payload = json.loads(captured.out)
        assert payload["online"] is False
        no_snapshot = [
            e for e in payload["unverified"] if e["source"] == "no_snapshot"
        ]
        assert no_snapshot and "unavailable" in no_snapshot[0]["reason"]


class TestReportArtifacts:
    def _check_into(
        self, tmp_path: Path, *extra: str, sql: str = "CREATE TABLE t (id int);\n"
    ) -> Path:
        migration = _write(tmp_path, "m.sql", sql)
        out_dir = tmp_path / "out"
        code = main(["check", str(migration), "-o", str(out_dir), *extra])
        assert code in (0, 1, 2)
        return out_dir / REPORT_FILENAME

    def test_output_dir_holds_report_and_evidence(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        report = self._check_into(tmp_path)
        capsys.readouterr()
        assert report.is_file()
        payload = json.loads(report.read_text(encoding="ascii"))
        evidence_dir = report.parent / str(payload["evidence"]["bundle_dir"])
        names = {f["name"] for f in payload["evidence"]["files"]}
        assert names == {
            "migration.sql",
            "parse_tree.json",
            "catalog_rows.json",
            "duration_constants.json",
        }
        for name in names:
            assert (evidence_dir / name).is_file()

    def test_verify_passes_on_signed_untampered_report(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        key = _write(tmp_path, "key.hex", SEED_HEX)
        report = self._check_into(tmp_path, "--sign-key", str(key))
        capsys.readouterr()
        assert main(["verify", str(report)]) == 0
        out = capsys.readouterr().out
        assert "signature: ok" in out
        assert "evidence:  ok" in out

    def test_signing_key_from_environment(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        key = _write(tmp_path, "key.hex", SEED_HEX)
        monkeypatch.setenv("BLASTOISE_SIGNING_KEY", str(key))
        report = self._check_into(tmp_path)
        capsys.readouterr()
        payload = json.loads(report.read_text(encoding="ascii"))
        assert payload["signature"]["algorithm"] == "ed25519"
        assert main(["verify", str(report)]) == 0
        capsys.readouterr()

    def test_verify_fails_on_evidence_tampering(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        key = _write(tmp_path, "key.hex", SEED_HEX)
        report = self._check_into(tmp_path, "--sign-key", str(key))
        evidence = report.parent / "evidence" / "migration.sql"
        evidence.write_bytes(evidence.read_bytes() + b"-- extra\n")
        capsys.readouterr()
        assert main(["verify", str(report)]) == 1
        out = capsys.readouterr().out
        assert "MISMATCH" in out

    def test_verify_fails_on_report_tampering(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        key = _write(tmp_path, "key.hex", SEED_HEX)
        report = self._check_into(tmp_path, "--sign-key", str(key))
        payload = json.loads(report.read_text(encoding="ascii"))
        payload["verdict"] = "proceed" if payload["verdict"] != "proceed" else "block"
        report.write_text(json.dumps(payload), encoding="ascii")
        capsys.readouterr()
        assert main(["verify", str(report)]) == 1
        assert "signature: FAILED" in capsys.readouterr().out

    def test_verify_fails_on_unsigned_report(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        report = self._check_into(tmp_path)
        capsys.readouterr()
        assert main(["verify", str(report)]) == 1
        out = capsys.readouterr().out
        assert "unsigned" in out
        # the evidence hashes themselves are fine; only attestation is missing
        assert "evidence:  ok" in out

    def test_verify_on_garbage_is_tool_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        bogus = _write(tmp_path, "bogus.json", "{not json")
        assert main(["verify", str(bogus)]) == 3
        capsys.readouterr()
        assert main(["verify", str(tmp_path / "missing.json")]) == 3
        capsys.readouterr()

    def test_explain_renders_expanded_form(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        report = self._check_into(
            tmp_path, sql="CREATE INDEX i ON users (email);\n"
        )
        capsys.readouterr()
        assert main(["explain", str(report)]) == 0
        out = capsys.readouterr().out
        assert out.startswith("SHELL REPORT")
        assert "lock: SHARE on users" in out
        assert out.isascii()

    def test_explain_on_missing_report_is_tool_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["explain", str(tmp_path / "missing.json")]) == 3
        capsys.readouterr()

    def test_written_report_round_trips_canonically(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        report = self._check_into(tmp_path)
        capsys.readouterr()
        text = report.read_text(encoding="ascii").rstrip("\n")
        from blastoise.report import canonical_json

        assert canonical_json(json.loads(text)) == text
