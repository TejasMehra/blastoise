"""In-process CLI tests: ``blastoise.cli.main`` invoked directly, no subprocess."""

from __future__ import annotations

import json
import runpy
import sys
from pathlib import Path

import pytest

from blastoise import parse_migration
from blastoise.cli import _print_script, main

ALTER_MIGRATION = """\
BEGIN;

ALTER TABLE users
    ADD COLUMN nickname text DEFAULT lower('X'),
    ADD CONSTRAINT users_email_key UNIQUE (email);

COMMIT;

SELECT count(*) FROM users;
"""


def _write(tmp_path: Path, name: str, content: str) -> Path:
    file_path = tmp_path / name
    file_path.write_text(content, encoding="utf-8")
    return file_path


def test_text_output_prints_actions_targets_and_groups(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    migration = _write(tmp_path, "0001_users.sql", ALTER_MIGRATION)
    assert main(["parse", str(migration)]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.startswith(str(migration))
    assert "alter_table  target=users" in captured.out
    assert "- add_column_default_nonvolatile column=nickname type=text" in captured.out
    assert "default=lower('X') [immutable]" in captured.out
    assert "- add_unique constraint=users_email_key" in captured.out
    # the SELECT has no target, so its line ends right after the kind
    assert ": select\n" in captured.out
    assert "tx: explicit group of 1 statement(s)" in captured.out
    assert "tx: implicit group of 1 statement(s)" in captured.out


def test_text_output_prints_warnings(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    migration = _write(tmp_path, "0002_commit_only.sql", "COMMIT;\n")
    assert main(["parse", str(migration)]) == 0
    out = capsys.readouterr().out
    assert "warning: line 1: COMMIT without a matching BEGIN" in out


def test_multiple_files_are_all_printed(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    first = _write(tmp_path, "a.sql", "CREATE TABLE a (id int);\n")
    second = _write(tmp_path, "b.sql", "DROP TABLE a;\n")
    assert main(["parse", str(first), str(second)]) == 0
    out = capsys.readouterr().out
    assert str(first) in out
    assert str(second) in out
    assert ": create_table  target=a" in out
    assert ": drop_table  target=a" in out


def test_json_output_strips_source(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    migration = _write(tmp_path, "0003_add_default.sql", ALTER_MIGRATION)
    assert main(["parse", "--json", str(migration)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert isinstance(payload, list)
    (script,) = payload
    assert "source" not in script
    assert script["path"] == str(migration)
    kinds = [statement["kind"] for statement in script["statements"]]
    assert kinds == ["begin", "alter_table", "commit", "select"]
    alter = script["statements"][1]
    assert alter["targets"] == [{"name": "users", "schema": None}]
    actions = alter["alter_actions"]
    assert actions[0]["kind"] == "add_column_default_nonvolatile"
    assert actions[0]["default"] == {
            "volatility": "immutable",
            "expression": "lower('X')",
            "unknown_functions": [],
        }
    assert actions[1]["kind"] == "add_unique"
    assert actions[1]["constraint_name"] == "users_email_key"


def test_unparseable_file_exits_2_and_reports_on_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    broken = _write(tmp_path, "broken.sql", "CREATE TABLE oops (id int\n")
    assert main(["parse", str(broken)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("error: ")
    assert "broken.sql" in captured.err


def test_missing_file_exits_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    missing = tmp_path / "does_not_exist.sql"
    assert main(["parse", str(missing)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "cannot read" in captured.err
    assert "does_not_exist.sql" in captured.err


def test_bad_file_does_not_block_good_ones(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = _write(tmp_path, "bad.sql", "ALTER TABLE;\n")
    good = _write(tmp_path, "good.sql", "CREATE INDEX CONCURRENTLY i ON t (c);\n")
    assert main(["parse", str(bad), str(good)]) == 2
    captured = capsys.readouterr()
    assert "error: " in captured.err
    assert ": create_index_concurrently  target=t" in captured.out


def test_json_with_only_bad_files_prints_empty_list(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = _write(tmp_path, "bad.sql", "definitely not sql;\n")
    assert main(["parse", "--json", str(bad)]) == 2
    captured = capsys.readouterr()
    assert json.loads(captured.out) == []
    assert "error: " in captured.err


@pytest.mark.filterwarnings("ignore:.*found in sys.modules.*:RuntimeWarning")
def test_module_entrypoint_runs_main(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # python -m blastoise.cli, executed in-process via runpy (not subprocess)
    migration = _write(tmp_path, "entry.sql", "TRUNCATE audit_log;\n")
    monkeypatch.setattr(sys, "argv", ["blastoise", "parse", str(migration)])
    with pytest.raises(SystemExit) as excinfo:
        runpy.run_module("blastoise.cli", run_name="__main__")
    assert excinfo.value.code == 0
    assert ": truncate  target=audit_log" in capsys.readouterr().out


def test_print_script_labels_pathless_scripts_stdin(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _print_script(parse_migration("VACUUM FULL users;"))
    out = capsys.readouterr().out
    assert out.startswith("<stdin>\n")
    assert ": vacuum_full  target=users" in out
