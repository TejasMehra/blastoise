"""``blastoise ci`` end to end: verdicts, exit codes, publishing, redaction.

Everything here runs offline. The live path is exercised by the existing
introspection suite; what these tests hold is the layer above it -- what the
run decides, what it publishes, what it refuses to print, and what it
refuses to read a credential from.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest
from ci_fakes import FakeGitHub, actions_environment, pull_request_event

from blastoise.ci.github import GitHubContext
from blastoise.ci.markdown import COMMENT_MARKER
from blastoise.ci.runner import CiOptions, run_ci
from blastoise.report import EXIT_TOOL_ERROR

SAFE = "CREATE TABLE t (id int);\n"
# CREATE INDEX CONCURRENTLY inside an explicit transaction: Postgres refuses
# it outright, so it is UNSAFE from the grammar alone. That keeps this whole
# file offline -- the size-dependent UNSAFE verdicts need a live database,
# and what is under test here is the layer above the engine, not the engine.
BLOCKING = "BEGIN;\nCREATE INDEX CONCURRENTLY i ON events (plan);\nCOMMIT;\n"
NEEDS_LOOK = "CREATE INDEX i ON users (email);\n"
DSN = "postgresql://svc:hunter2@db-staging.internal.example:6432/billing"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "migrations").mkdir(parents=True)
    (root / "db" / "migrate").mkdir(parents=True)
    return root


def _write(repo: Path, relative: str, text: str) -> str:
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return relative


def _run(
    repo: Path,
    paths: tuple[str, ...],
    *,
    environ: dict[str, str] | None = None,
    transport: Any = None,
    **kwargs: Any,
) -> tuple[int, str, str, dict[str, Any]]:
    out, err = io.StringIO(), io.StringIO()
    options = CiOptions(
        repo_root=repo,
        explicit_paths=paths,
        output_dir=repo / "out",
        emit_json=True,
        **kwargs,
    )
    if transport is not None:
        options.transport = transport
    code = run_ci(options, environ=environ or {}, stdout=out, stderr=err)
    payload: dict[str, Any] = {}
    text = out.getvalue().strip()
    if text.startswith("{"):
        payload = json.loads(text)
    return code, out.getvalue(), err.getvalue(), payload


class TestVerdictsAndExitCodes:
    def test_a_safe_migration_proceeds_and_exits_0(self, repo: Path) -> None:
        path = _write(repo, "migrations/0001.sql", SAFE)
        code, _, _, payload = _run(repo, (path,))
        assert payload["verdict"] == "proceed"
        assert code == 0

    def test_a_blocking_migration_exits_2(self, repo: Path) -> None:
        path = _write(repo, "migrations/0001.sql", BLOCKING)
        code, _, _, payload = _run(repo, (path,))
        assert payload["verdict"] == "block"
        assert code == 2

    def test_requires_approval_does_not_fail_the_job_by_default(self, repo: Path) -> None:
        path = _write(repo, "migrations/0001.sql", NEEDS_LOOK)
        code, _, _, payload = _run(repo, (path,))
        assert payload["verdict"] == "requires_approval"
        assert code == 0, "fail_on defaults to block"

    def test_fail_on_requires_approval_makes_it_exit_1(self, repo: Path) -> None:
        from blastoise.ci.config import FailOn

        path = _write(repo, "migrations/0001.sql", NEEDS_LOOK)
        code, _, _, _ = _run(repo, (path,), fail_on=FailOn.REQUIRES_APPROVAL)
        assert code == 1

    def test_fail_on_never_still_reports_the_verdict(self, repo: Path) -> None:
        from blastoise.ci.config import FailOn

        path = _write(repo, "migrations/0001.sql", BLOCKING)
        code, _, _, payload = _run(repo, (path,), fail_on=FailOn.NEVER)
        assert code == 0
        assert payload["verdict"] == "block"

    def test_the_run_verdict_is_the_worst_of_its_files(self, repo: Path) -> None:
        paths = (
            _write(repo, "migrations/0001.sql", SAFE),
            _write(repo, "migrations/0002.sql", BLOCKING),
            _write(repo, "migrations/0003.sql", NEEDS_LOOK),
        )
        code, _, _, payload = _run(repo, paths)
        assert payload["verdict"] == "block"
        assert code == 2

    def test_no_migrations_changed_is_a_pass_not_an_error(self, repo: Path) -> None:
        _write(repo, "README.md", "hello\n")
        code, _, _, payload = _run(repo, ("README.md",))
        assert code == 0
        assert payload["verdict"] == "proceed"
        assert payload["migrations_detected"] == 0


class TestUnassessableFiles:
    """Recognized but unreadable: say so, hold at requires_approval."""

    def test_a_rails_migration_is_recognized_and_reported(self, repo: Path) -> None:
        path = _write(repo, "db/migrate/001_add.rb", "class A < Migration; end\n")
        code, _, err, payload = _run(repo, (path,))
        (entry,) = payload["files"]
        assert entry["framework"] == "rails"
        assert entry["status"] == "unsupported"
        detail = entry["detail"] or ""
        # The two facts a reader needs: the layout was recognized (so this
        # is not a detection bug), and this file carries no verdict. With
        # no `rails.extract` in config, the reason is that rendering is
        # opt-in -- which is actionable, unlike "unsupported".
        assert "recognized" in detail
        assert "not assessed" in detail
        assert "rails.extract" in detail
        assert "Rails" in err
        assert payload["verdict"] == "requires_approval"
        assert code == 0

    def test_it_does_not_crash_and_does_not_silently_skip(self, repo: Path) -> None:
        paths = (
            _write(repo, "db/migrate/001_add.rb", "class A < Migration; end\n"),
            _write(repo, "app/migrations/0001_initial.py", "operations = []\n"),
            _write(repo, "alembic/versions/ab12_x.py", "def upgrade(): pass\n"),
        )
        _, _, _, payload = _run(repo, paths)
        assert payload["migrations_detected"] == 3
        assert payload["assessed"] == 0
        assert payload["unsupported"] == 3

    def test_a_safe_sql_file_beside_a_dsl_file_still_holds_the_run(self, repo: Path) -> None:
        # The green check is the outcome to avoid: the pull request contains
        # a migration nobody read.
        paths = (
            _write(repo, "migrations/0001.sql", SAFE),
            _write(repo, "db/migrate/001_add.rb", "class A < Migration; end\n"),
        )
        _, _, _, payload = _run(repo, paths)
        assert payload["verdict"] == "requires_approval"

    def test_an_unparseable_migration_is_an_error_not_a_block(self, repo: Path) -> None:
        path = _write(repo, "migrations/0001.sql", "SELECT FROM WHERE ;;;\n")
        code, _, _, payload = _run(repo, (path,))
        (entry,) = payload["files"]
        assert entry["status"] == "error"
        assert entry["verdict"] is None
        assert payload["verdict"] == "requires_approval"
        assert code == 0, "a tool failure is never reported as a dangerous migration"

    def test_a_missing_file_is_an_error_not_a_crash(self, repo: Path) -> None:
        code, _, _, payload = _run(repo, ("migrations/gone.sql",))
        assert payload["files"][0]["status"] == "error"
        assert code == 0


class TestReportsAndEvidence:
    def test_one_report_and_bundle_per_migration(self, repo: Path) -> None:
        paths = (
            _write(repo, "migrations/0001.sql", SAFE),
            _write(repo, "migrations/0002.sql", NEEDS_LOOK),
        )
        _, _, _, payload = _run(repo, paths)
        directories = [Path(entry["report_dir"]) for entry in payload["files"]]
        assert len(directories) == 2
        for directory in directories:
            assert (directory / "report.json").is_file()
            assert (directory / "evidence" / "migration.sql").is_file()
            assert (directory / "evidence" / "parse_tree.json").is_file()

    def test_the_written_report_is_the_one_the_comment_describes(self, repo: Path) -> None:
        path = _write(repo, "migrations/0001.sql", BLOCKING)
        _, _, _, payload = _run(repo, (path,))
        written = json.loads(
            (Path(payload["files"][0]["report_dir"]) / "report.json").read_text(encoding="ascii")
        )
        assert written["verdict"] == payload["files"][0]["verdict"]
        assert written["unverified"], "unverified never serializes empty"


class TestPublishing:
    def _publishing_run(
        self, repo: Path, tmp_path: Path, paths: tuple[str, ...]
    ) -> tuple[FakeGitHub, dict[str, Any]]:
        fake = FakeGitHub(files=[{"filename": p, "status": "added"} for p in paths])
        environ = actions_environment(tmp_path, pull_request_event())
        _, _, _, payload = _run(repo, (), environ=environ, transport=fake)
        return fake, payload

    def test_the_comment_leads_with_the_verdict_and_the_tier_counts(
        self, repo: Path, tmp_path: Path
    ) -> None:
        path = _write(repo, "migrations/0001.sql", BLOCKING)
        fake, _ = self._publishing_run(repo, tmp_path, (path,))
        body = fake.comments[0]["body"]
        head = body[: body.index("###")]
        assert body.startswith(COMMENT_MARKER)
        assert "BLOCK" in head
        # One file, so the counts live in that file's own section rather
        # than being repeated verbatim as a run total above it.
        assert "| unsafe | unknown | needs_timing | safe_irreversible | safe |" in body
        assert body.count("| unsafe | unknown | needs_timing |") == 1

    def test_a_re_push_updates_rather_than_duplicates(
        self, repo: Path, tmp_path: Path
    ) -> None:
        path = _write(repo, "migrations/0001.sql", BLOCKING)
        fake, _ = self._publishing_run(repo, tmp_path, (path,))
        assert len(fake.comments) == 1

        # Second push: the author fixed it.
        _write(repo, "migrations/0001.sql", SAFE)
        environ = actions_environment(tmp_path, pull_request_event())
        _run(repo, (), environ=environ, transport=fake)

        assert len(fake.comments) == 1, "a re-push must edit, not stack up verdicts"
        assert "PROCEED" in fake.comments[0]["body"]
        assert "BLOCK" not in fake.comments[0]["body"]

    def test_the_check_status_maps_from_the_verdict(self, repo: Path, tmp_path: Path) -> None:
        cases = {SAFE: "success", NEEDS_LOOK: "neutral", BLOCKING: "failure"}
        for sql, expected in cases.items():
            path = _write(repo, "migrations/0001.sql", sql)
            fake, _ = self._publishing_run(repo, tmp_path, (path,))
            assert fake.check_runs[-1]["conclusion"] == expected
            assert fake.check_runs[-1]["head_sha"] == "f" * 40

    def test_a_read_only_fork_token_is_reported_not_fatal(
        self, repo: Path, tmp_path: Path
    ) -> None:
        path = _write(repo, "migrations/0001.sql", SAFE)
        fake = FakeGitHub(
            files=[{"filename": path, "status": "added"}],
            status_for={"/issues/": 403, "/check-runs": 403},
        )
        environ = actions_environment(tmp_path, pull_request_event())
        code, _, err, payload = _run(repo, (path,), environ=environ, transport=fake)
        assert code == 0
        assert payload["verdict"] == "proceed"
        assert "read-only token" in err
        assert "checks: write" in err

    def test_step_outputs_are_written_for_the_action(
        self, repo: Path, tmp_path: Path
    ) -> None:
        path = _write(repo, "migrations/0001.sql", BLOCKING)
        output = tmp_path / "gh_output"
        output.write_text("", encoding="utf-8")
        environ = {"GITHUB_OUTPUT": str(output)}
        _run(repo, (path,), environ=environ)
        lines = output.read_text(encoding="utf-8").splitlines()
        written = dict(line.split("=", 1) for line in lines if "=" in line)
        assert written["verdict"] == "block"
        assert written["migrations-detected"] == "1"
        assert written["online"] == "false"

    def test_the_job_summary_gets_the_comment_body(self, repo: Path, tmp_path: Path) -> None:
        path = _write(repo, "migrations/0001.sql", BLOCKING)
        summary = tmp_path / "summary.md"
        summary.write_text("", encoding="utf-8")
        _run(repo, (path,), environ={"GITHUB_STEP_SUMMARY": str(summary)})
        assert "BLOCK" in summary.read_text(encoding="utf-8")

    def test_comment_output_gives_other_ci_the_same_markdown(
        self, repo: Path, tmp_path: Path
    ) -> None:
        path = _write(repo, "migrations/0001.sql", BLOCKING)
        target = tmp_path / "comment.md"
        _run(repo, (path,), comment_output=target)
        assert COMMENT_MARKER in target.read_text(encoding="utf-8")

    def test_the_pull_request_file_list_is_used_when_no_paths_are_given(
        self, repo: Path, tmp_path: Path
    ) -> None:
        _write(repo, "migrations/0001.sql", SAFE)
        _write(repo, "README.md", "hi\n")
        fake = FakeGitHub(
            files=[
                {"filename": "migrations/0001.sql", "status": "added"},
                {"filename": "README.md", "status": "modified"},
            ]
        )
        environ = actions_environment(tmp_path, pull_request_event())
        _, _, _, payload = _run(repo, (), environ=environ, transport=fake)
        assert payload["changed_files"] == 2
        assert payload["migrations_detected"] == 1


class TestSecretHandling:
    """The connection string comes from a secret, and never comes back out."""

    def test_the_url_env_variable_is_read_from_the_environment(self, repo: Path) -> None:
        path = _write(repo, "migrations/0001.sql", SAFE)
        # Unreachable host: the point is that it tried, and that the failure
        # message says nothing about where.
        environ = {"BLASTOISE_DATABASE_URL": "postgresql://u:pw@127.0.0.1:1/none"}
        _, _, _, payload = _run(repo, (path,), environ=environ)
        assert payload["online"] is False
        assert payload["degraded_reason"]
        assert "pw" not in payload["degraded_reason"]

    def test_a_connection_failure_never_prints_the_dsn(self, repo: Path) -> None:
        path = _write(repo, "migrations/0001.sql", SAFE)
        environ = {"BLASTOISE_DATABASE_URL": DSN.replace("6432", "1")}
        _, out, err, payload = _run(repo, (path,), environ=environ)
        everything = out + err + json.dumps(payload)
        assert "hunter2" not in everything
        assert "db-staging.internal.example" not in everything
        assert "svc" not in everything

    def test_a_workflow_input_is_refused_as_the_source(self, repo: Path) -> None:
        # INPUT_* is how a GitHub Actions input arrives. A connection string
        # must never come from one.
        path = _write(repo, "migrations/0001.sql", SAFE)
        code, _, err, _ = _run(
            repo,
            (path,),
            environ={"INPUT_DATABASE_URL": DSN},
            database_url_env="INPUT_DATABASE_URL",
        )
        assert code == EXIT_TOOL_ERROR
        assert "workflow-input namespace" in err
        assert "hunter2" not in err

    def test_a_connection_string_where_the_name_belongs_is_refused(self, repo: Path) -> None:
        path = _write(repo, "migrations/0001.sql", SAFE)
        code, _, err, _ = _run(repo, (path,), database_url_env=DSN)
        assert code == EXIT_TOOL_ERROR
        assert "NAME of an environment variable" in err
        assert "hunter2" not in err

    def test_offline_never_touches_the_environment_variable(self, repo: Path) -> None:
        path = _write(repo, "migrations/0001.sql", SAFE)
        _, out, err, payload = _run(
            repo, (path,), offline=True, environ={"BLASTOISE_DATABASE_URL": DSN}
        )
        assert payload["online"] is False
        assert payload["degraded_reason"] is None
        assert "hunter2" not in out + err

    def test_an_unexpected_crash_prints_a_scrubbed_traceback(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = _write(repo, "migrations/0001.sql", SAFE)

        def explode(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError(f"boom while using {DSN}")

        monkeypatch.setattr("blastoise.ci.runner.detect_migrations", explode)
        code, out, err, _ = _run(
            repo, (path,), environ={"BLASTOISE_DATABASE_URL": DSN}
        )
        assert code == EXIT_TOOL_ERROR
        everything = out + err
        assert "Traceback" in everything, "the traceback is still useful"
        assert "hunter2" not in everything
        assert "db-staging.internal.example" not in everything

    def test_the_token_is_not_echoed(self, repo: Path, tmp_path: Path) -> None:
        path = _write(repo, "migrations/0001.sql", SAFE)
        fake = FakeGitHub(files=[{"filename": path, "status": "added"}])
        environ = actions_environment(tmp_path, pull_request_event())
        _, out, err, _ = _run(repo, (), environ=environ, transport=fake)
        assert environ["GITHUB_TOKEN"] not in out + err


class TestChangedFileDiscovery:
    def test_a_list_from_a_file(self, repo: Path, tmp_path: Path) -> None:
        path = _write(repo, "migrations/0001.sql", SAFE)
        listing = tmp_path / "changed.txt"
        listing.write_text(f"README.md\n{path}\n\n", encoding="utf-8")
        _, _, _, payload = _run(repo, (), changed_from=listing)
        assert payload["changed_files"] == 2
        assert payload["migrations_detected"] == 1

    def test_no_base_and_no_list_is_a_tool_error_with_the_fix_in_it(
        self, repo: Path
    ) -> None:
        code, _, err, _ = _run(repo, (), changed_source="git")
        assert code == EXIT_TOOL_ERROR
        assert "--base-ref" in err


class TestConfigIntegration:
    def test_configured_paths_replace_detection(self, repo: Path) -> None:
        _write(repo, ".blastoise.yml", 'migrations:\n  paths: ["sql/**/*.sql"]\n')
        conventional = _write(repo, "migrations/0001.sql", BLOCKING)
        configured = _write(repo, "sql/schema/0001.sql", SAFE)
        _, _, _, payload = _run(repo, (conventional, configured))
        assert [entry["path"] for entry in payload["files"]] == ["sql/schema/0001.sql"]
        assert payload["verdict"] == "proceed"

    def test_exclude_removes_a_detected_file(self, repo: Path) -> None:
        _write(repo, ".blastoise.yml", 'migrations:\n  exclude: ["**/seed_*.sql"]\n')
        kept = _write(repo, "migrations/0001.sql", SAFE)
        seed = _write(repo, "migrations/seed_users.sql", BLOCKING)
        _, _, _, payload = _run(repo, (kept, seed))
        assert [entry["path"] for entry in payload["files"]] == ["migrations/0001.sql"]

    def test_a_broken_config_is_a_tool_error_not_a_default(self, repo: Path) -> None:
        _write(repo, ".blastoise.yml", "migrations:\n  path: []\n")
        path = _write(repo, "migrations/0001.sql", SAFE)
        code, _, err, _ = _run(repo, (path,))
        assert code == EXIT_TOOL_ERROR
        assert "unknown key" in err

    def test_the_config_names_the_environment_variable(self, repo: Path) -> None:
        _write(repo, ".blastoise.yml", "database:\n  url_env: MY_STAGING_URL\n")
        path = _write(repo, "migrations/0001.sql", SAFE)
        _, _, err, _ = _run(repo, (path,), environ={})
        assert "$MY_STAGING_URL" in err

    def test_the_database_label_reaches_the_comment(self, repo: Path, tmp_path: Path) -> None:
        path = _write(repo, "migrations/0001.sql", SAFE)
        target = tmp_path / "comment.md"
        _run(repo, (path,), comment_output=target, database_label="staging")
        # Offline, so the label is not claimed; the point is it does not
        # crash and the offline notice is what appears instead.
        assert "offline" in target.read_text(encoding="utf-8")


class TestCliWiring:
    def test_the_ci_subcommand_runs(self, repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
        from blastoise.cli import main

        path = _write(repo, "migrations/0001.sql", BLOCKING)
        code = main(
            ["ci", "--repo-root", str(repo), "-o", str(repo / "out"), "--json", path]
        )
        assert code == 2
        assert json.loads(capsys.readouterr().out)["verdict"] == "block"

    def test_there_is_no_flag_that_takes_a_connection_string(self) -> None:
        # Not a style preference: a connection string on a command line is
        # visible in `ps` and in the CI job's own command log.
        import argparse

        from blastoise.cli import _build_parser

        subcommands: dict[str, argparse.ArgumentParser] = {}
        for action in _build_parser()._actions:
            if isinstance(action, argparse._SubParsersAction):
                subcommands.update(action.choices)
        flags = {
            option
            for action in subcommands["ci"]._actions
            for option in action.option_strings
        }
        assert "--database-url" not in flags
        assert "--database-url-env" in flags


class TestContextHelper:
    def test_a_missing_event_file_is_tolerated(self, tmp_path: Path) -> None:
        context = GitHubContext.from_environment(
            {"GITHUB_REPOSITORY": "acme/app", "GITHUB_EVENT_PATH": str(tmp_path / "nope.json")}
        )
        assert not context.is_pull_request


class TestGitDiscovery:
    """The path a pipeline without a pull request API uses."""

    @staticmethod
    def _git(repo: Path, *args: str) -> None:
        import subprocess

        subprocess.run(
            ["git", *args],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        )

    def _repo_with_history(self, repo: Path) -> str:
        self._git(repo, "init", "--quiet", "-b", "main")
        self._git(repo, "config", "user.email", "t@example.com")
        self._git(repo, "config", "user.name", "t")
        _write(repo, "README.md", "hello\n")
        self._git(repo, "add", "-A")
        self._git(repo, "commit", "--quiet", "-m", "base")
        import subprocess

        base = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        _write(repo, "migrations/0001.sql", BLOCKING)
        _write(repo, "docs/notes.md", "notes\n")
        self._git(repo, "add", "-A")
        self._git(repo, "commit", "--quiet", "-m", "add a migration")
        return base

    def test_a_git_diff_finds_the_changed_migration(self, repo: Path) -> None:
        base = self._repo_with_history(repo)
        code, _, _, payload = _run(repo, (), changed_source="git", base_ref=base)
        assert payload["changed_files"] == 2
        assert [entry["path"] for entry in payload["files"]] == ["migrations/0001.sql"]
        assert code == 2

    def test_an_unknown_base_is_a_tool_error_that_says_how_to_fix_it(
        self, repo: Path
    ) -> None:
        self._repo_with_history(repo)
        code, _, err, _ = _run(repo, (), changed_source="git", base_ref="0" * 40)
        assert code == EXIT_TOOL_ERROR
        assert "fetch-depth: 0" in err


class TestOptionPlumbing:
    def test_changed_from_stdin(self, repo: Path) -> None:
        path = _write(repo, "migrations/0001.sql", SAFE)
        out, err = io.StringIO(), io.StringIO()
        code = run_ci(
            CiOptions(
                repo_root=repo,
                output_dir=repo / "out",
                changed_from=Path("-"),
                emit_json=True,
            ),
            environ={},
            stdout=out,
            stderr=err,
            stdin=io.StringIO(f"{path}\n"),
        )
        assert code == 0
        assert json.loads(out.getvalue())["migrations_detected"] == 1

    def test_an_unreadable_changed_from_file_is_a_tool_error(self, repo: Path) -> None:
        code, _, err, _ = _run(repo, (), changed_from=repo / "nope.txt")
        assert code == EXIT_TOOL_ERROR
        assert "cannot read" in err

    def test_changed_source_github_api_without_a_token_is_a_tool_error(
        self, repo: Path
    ) -> None:
        code, _, err, _ = _run(repo, (), changed_source="github_api")
        assert code == EXIT_TOOL_ERROR
        assert "pull request context and a token" in err

    def test_the_json_summary_can_be_written_to_a_file(
        self, repo: Path, tmp_path: Path
    ) -> None:
        path = _write(repo, "migrations/0001.sql", BLOCKING)
        target = tmp_path / "summary.json"
        _run(repo, (path,), json_output=target)
        assert json.loads(target.read_text(encoding="utf-8"))["verdict"] == "block"

    def test_without_json_the_stdout_is_one_readable_line(self, repo: Path) -> None:
        path = _write(repo, "migrations/0001.sql", BLOCKING)
        out, err = io.StringIO(), io.StringIO()
        run_ci(
            CiOptions(repo_root=repo, explicit_paths=(path,), output_dir=repo / "out"),
            environ={},
            stdout=out,
            stderr=err,
        )
        assert out.getvalue().strip() == "BLOCK: 1 migration file assessed"

    def test_a_push_event_assesses_but_does_not_comment(
        self, repo: Path, tmp_path: Path
    ) -> None:
        path = _write(repo, "migrations/0001.sql", SAFE)
        fake = FakeGitHub()
        environ = actions_environment(tmp_path, {})
        code, _, err, payload = _run(
            repo, (path,), environ=environ, transport=fake
        )
        assert code == 0
        assert payload["verdict"] == "proceed"
        assert "not a pull request event" in err
        assert fake.comments == []
        # A push still gets a check status: GITHUB_SHA is the commit.
        assert fake.check_runs[0]["conclusion"] == "success"

    def test_a_corrupt_event_payload_does_not_stop_the_run(
        self, repo: Path, tmp_path: Path
    ) -> None:
        path = _write(repo, "migrations/0001.sql", SAFE)
        event_path = tmp_path / "event.json"
        event_path.write_text("{not json", encoding="utf-8")
        environ = {"GITHUB_EVENT_PATH": str(event_path)}
        code, _, err, payload = _run(repo, (path,), environ=environ)
        assert code == 0
        assert payload["verdict"] == "proceed"
        assert "could not read the workflow event payload" in err

    def test_comment_and_check_run_can_both_be_turned_off(
        self, repo: Path, tmp_path: Path
    ) -> None:
        path = _write(repo, "migrations/0001.sql", SAFE)
        fake = FakeGitHub(files=[{"filename": path, "status": "added"}])
        environ = actions_environment(tmp_path, pull_request_event())
        _run(
            repo,
            (path,),
            environ=environ,
            transport=fake,
            comment=False,
            check_run=False,
        )
        assert fake.comments == []
        assert fake.check_runs == []


class TestQuietWhenThereIsNothingToSay:
    """The all-zeros comment on an unrelated pull request is noise."""

    def test_no_migrations_posts_no_comment(self, repo: Path, tmp_path: Path) -> None:
        _write(repo, "README.md", "hello\n")
        fake = FakeGitHub(files=[{"filename": "README.md", "status": "modified"}])
        environ = actions_environment(tmp_path, pull_request_event())
        code, _, err, payload = _run(repo, (), environ=environ, transport=fake)
        assert code == 0
        assert payload["migrations_detected"] == 0
        assert fake.comments == [], "an unrelated pull request gets no comment"
        assert "no comment posted" in err

    def test_a_check_status_is_still_set(self, repo: Path, tmp_path: Path) -> None:
        # Silence in the conversation, not silence in the checks list: the
        # check ran and passed, and that is worth recording.
        _write(repo, "README.md", "hello\n")
        fake = FakeGitHub(files=[{"filename": "README.md", "status": "modified"}])
        environ = actions_environment(tmp_path, pull_request_event())
        _run(repo, (), environ=environ, transport=fake)
        assert fake.check_runs[-1]["conclusion"] == "success"

    def test_a_push_that_removes_the_migration_deletes_the_comment(
        self, repo: Path, tmp_path: Path
    ) -> None:
        path = _write(repo, "migrations/0001.sql", BLOCKING)
        fake = FakeGitHub(files=[{"filename": path, "status": "added"}])
        environ = actions_environment(tmp_path, pull_request_event())
        _run(repo, (), environ=environ, transport=fake)
        assert "BLOCK" in fake.comments[0]["body"]

        # Second push: the migration is gone from the pull request. There is
        # nothing true to replace the verdict with, so the comment goes.
        fake.files = [{"filename": "README.md", "status": "modified"}]
        _, _, err, _ = _run(repo, (), environ=environ, transport=fake)
        assert fake.comments == []
        assert "removed the comment an earlier push left" in err


class TestCommentDeletion:
    def test_an_unrelated_pull_request_never_had_a_comment_to_delete(
        self, repo: Path, tmp_path: Path
    ) -> None:
        _write(repo, "README.md", "hello\n")
        fake = FakeGitHub(files=[{"filename": "README.md", "status": "modified"}])
        environ = actions_environment(tmp_path, pull_request_event())
        _, _, err, _ = _run(repo, (), environ=environ, transport=fake)
        assert fake.comments == []
        assert "no comment posted" in err
        assert not any(call.method == "DELETE" for call in fake.calls)

    def test_a_failed_delete_is_reported_not_fatal(
        self, repo: Path, tmp_path: Path
    ) -> None:
        _write(repo, "README.md", "hello\n")
        fake = FakeGitHub(files=[{"filename": "README.md", "status": "modified"}])
        fake.add_comment(f"{COMMENT_MARKER}\nBLOCK")
        fake.status_for = {"/issues/comments/": 403}
        environ = actions_environment(tmp_path, pull_request_event())
        code, _, err, _ = _run(repo, (), environ=environ, transport=fake)
        assert code == 0
        assert "read-only token" in err
