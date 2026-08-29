"""Rails extraction: what it refuses, what it finds, and how it fails.

Everything here is offline. Extraction itself needs Ruby, ActiveRecord and
a throwaway Postgres, which is what the validation harness in
``artifacts/scripts/rails_extraction_validation.py`` exercises against real
migrations from real applications. What these tests hold is the layer
around it: the refusals, which are a security boundary rather than a
convenience, and the guarantee that every failure produces a reason a
reviewer can act on rather than a verdict nobody should trust.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from blastoise.ci.config import RailsConfig, parse_config
from blastoise.ci.detect import EXTRACTABLE, Framework, detect_migrations
from blastoise.rails import RailsExtractionError, extract_rails_sql, rails_refusal
from blastoise.rails.extract import (
    _admin_url,
    _app_root,
    _replay_set,
    _resolve_schema,
    _same_server,
    _scratch_url,
)

SCRATCH = "postgres://postgres@scratch.internal:5432/postgres"
ASSESSED = "postgres://svc:pw@db-staging.internal:6432/billing"


class TestRefusals:
    """Each of these turns a read-only tool into one that executes a branch."""

    def test_extraction_is_off_unless_asked_for(self) -> None:
        reason = rails_refusal(
            enabled=False,
            scratch_url=SCRATCH,
            assessed_url=None,
            environ={},
            scratch_url_env="S",
        )
        assert reason is not None
        # The message has to name the switch, or the reader is left knowing
        # only that something is missing.
        assert "rails.extract" in reason

    def test_it_refuses_under_pull_request_target(self) -> None:
        reason = rails_refusal(
            enabled=True,
            scratch_url=SCRATCH,
            assessed_url=None,
            environ={"GITHUB_EVENT_NAME": "pull_request_target"},
            scratch_url_env="S",
        )
        assert reason is not None
        assert "pull_request_target" in reason
        # No configuration turns this back on: it is the one event where a
        # fork's code runs with the base repository's secrets.
        assert "pull_request" in reason

    def test_an_ordinary_pull_request_is_allowed(self) -> None:
        assert (
            rails_refusal(
                enabled=True,
                scratch_url=SCRATCH,
                assessed_url=ASSESSED,
                environ={"GITHUB_EVENT_NAME": "pull_request"},
                scratch_url_env="S",
            )
            is None
        )

    def test_it_refuses_without_a_scratch_database(self) -> None:
        reason = rails_refusal(
            enabled=True,
            scratch_url=None,
            assessed_url=None,
            environ={},
            scratch_url_env="MY_SCRATCH",
        )
        assert reason is not None
        assert "MY_SCRATCH" in reason

    def test_it_refuses_to_write_to_the_server_it_is_assessing(self) -> None:
        # Extraction creates and drops databases. Doing that on the server
        # holding the database under assessment is the one mistake that
        # cannot be walked back.
        reason = rails_refusal(
            enabled=True,
            scratch_url="postgres://postgres@db.internal:5432/postgres",
            assessed_url="postgres://svc:pw@db.internal:5432/billing",
            environ={},
            scratch_url_env="S",
        )
        assert reason is not None
        assert "same server" in reason

    def test_a_different_port_on_the_same_host_is_a_different_server(self) -> None:
        assert (
            rails_refusal(
                enabled=True,
                scratch_url="postgres://postgres@db.internal:5433/postgres",
                assessed_url="postgres://svc:pw@db.internal:5432/billing",
                environ={},
                scratch_url_env="S",
            )
            is None
        )

    def test_the_default_port_is_the_same_server_as_a_spelled_out_5432(self) -> None:
        assert _same_server(
            "postgres://h/postgres", "postgres://h:5432/billing"
        )


class TestConfig:
    """Opt-in is a config key, and a URL in the file is refused."""

    def test_extraction_defaults_to_off(self) -> None:
        assert parse_config(None, where="t").rails == RailsConfig()
        assert parse_config(None, where="t").rails.extract is False

    def test_it_reads_the_switch_and_the_variable_name(self) -> None:
        config = parse_config(
            {"rails": {"extract": True, "scratch_url_env": "CI_SCRATCH"}}, where="t"
        )
        assert config.rails.extract is True
        assert config.rails.scratch_url_env == "CI_SCRATCH"


class TestDetectionRouting:
    """Rails is a DSL that can now be rendered; the others still cannot."""

    def test_rails_is_extractable_and_the_other_dsls_are_not(self) -> None:
        assert Framework.RAILS in EXTRACTABLE
        assert Framework.DJANGO not in EXTRACTABLE
        assert Framework.ALEMBIC not in EXTRACTABLE

    def test_a_rails_file_is_still_not_assessable_on_its_own(self) -> None:
        # `assessable` means "the parser can read this file". Extraction
        # does not change that: it produces a different artefact to read.
        (entry,) = detect_migrations(("db/migrate/20240101_add.rb",))
        assert entry.assessable is False
        assert entry.extractable is True


class TestLocatingTheApp:
    """A Rails app is wherever ``db/migrate`` is, including in a monorepo."""

    def test_it_finds_the_app_root_above_db_migrate(self, tmp_path: Path) -> None:
        root, prefix = _app_root(tmp_path, "services/billing/db/migrate/001_x.rb")
        assert prefix == "services/billing"
        assert root == tmp_path / "services/billing"

    def test_a_top_level_app_has_no_prefix(self, tmp_path: Path) -> None:
        root, prefix = _app_root(tmp_path, "db/migrate/001_x.rb")
        assert prefix == ""
        assert root == tmp_path

    def test_replay_takes_only_strictly_earlier_migrations(self, tmp_path: Path) -> None:
        directory = tmp_path / "db" / "migrate"
        directory.mkdir(parents=True)
        for name in ("001_a.rb", "002_b.rb", "003_c.rb", "notes.txt"):
            (directory / name).write_text("x", encoding="utf-8")
        earlier = _replay_set(tmp_path, "db/migrate/003_c.rb")
        assert [Path(p).name for p in earlier] == ["001_a.rb", "002_b.rb"]


class TestOrderWithinOnePullRequest:
    """A later migration in the same change sees the earlier ones applied."""

    def test_each_migration_is_preceded_by_the_earlier_ones(self) -> None:
        from blastoise.ci.runner import _rails_order

        detected = detect_migrations(
            (
                "db/migrate/20240102000000_index_it.rb",
                "db/migrate/20240101000000_add_column.rb",
                "db/migrate/20240103000000_backfill.rb",
            )
        )
        order = _rails_order(detected)
        assert order["db/migrate/20240101000000_add_column.rb"] == ()
        assert order["db/migrate/20240102000000_index_it.rb"] == (
            "db/migrate/20240101000000_add_column.rb",
        )
        assert len(order["db/migrate/20240103000000_backfill.rb"]) == 2

    def test_two_apps_in_a_monorepo_do_not_precede_each_other(self) -> None:
        # Separate directories are separate histories against separate
        # databases; ordering them together would apply one app's migration
        # to the other's schema.
        from blastoise.ci.runner import _rails_order

        detected = detect_migrations(
            (
                "services/a/db/migrate/20240101000000_x.rb",
                "services/b/db/migrate/20240102000000_y.rb",
            )
        )
        assert all(preceding == () for preceding in _rails_order(detected).values())


class TestPreState:
    """The pre-state is the base commit's schema, and structure.sql wins."""

    def test_structure_sql_is_preferred_over_schema_rb(self, tmp_path: Path) -> None:
        (tmp_path / "db").mkdir()
        (tmp_path / "db" / "schema.rb").write_text("ruby", encoding="utf-8")
        (tmp_path / "db" / "structure.sql").write_text("sql", encoding="utf-8")
        work = tmp_path / "work"
        work.mkdir()
        found, kind, source = _resolve_schema(tmp_path, "", None, work)
        assert kind == "sql"
        assert found is not None and found.read_text() == "sql"
        assert "structure.sql" in source

    def test_no_schema_file_is_reported_rather_than_guessed(self, tmp_path: Path) -> None:
        work = tmp_path / "work"
        work.mkdir()
        found, kind, _ = _resolve_schema(tmp_path, "", None, work)
        assert found is None and kind is None


class TestUrls:
    """The scratch database is created from an admin connection."""

    def test_the_admin_url_points_at_postgres(self) -> None:
        assert _admin_url("postgres://u:p@h:5432/anything") == "postgres://u:p@h:5432/postgres"

    def test_the_scratch_url_carries_the_generated_name(self) -> None:
        assert _scratch_url("postgres://u@h:5432/postgres", "tmp_1") == (
            "postgres://u@h:5432/tmp_1"
        )


class TestFailuresAreReasons:
    """Extraction never degrades into a guess; it degrades into a message."""

    def test_a_missing_schema_and_no_history_is_an_error_with_a_reason(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "db" / "migrate").mkdir(parents=True)
        migration = "db/migrate/20240101_add.rb"
        (tmp_path / migration).write_text("class Add < X; end", encoding="utf-8")
        with pytest.raises(RailsExtractionError) as caught:
            extract_rails_sql(
                migration,
                repo_root=tmp_path,
                scratch_url=SCRATCH,
                ruby="ruby",
            )
        assert "schema" in caught.value.reason

    def test_replaying_a_whole_history_is_refused_with_the_count(
        self, tmp_path: Path
    ) -> None:
        directory = tmp_path / "db" / "migrate"
        directory.mkdir(parents=True)
        for index in range(1, 12):
            (directory / f"{index:03d}_m.rb").write_text("x", encoding="utf-8")
        (directory / "999_target.rb").write_text("x", encoding="utf-8")
        with pytest.raises(RailsExtractionError) as caught:
            extract_rails_sql(
                "db/migrate/999_target.rb",
                repo_root=tmp_path,
                scratch_url=SCRATCH,
                ruby="ruby",
                replay_limit=5,
            )
        # The number is the actionable part: it tells a team whether
        # committing a schema file is worth it.
        assert "11 earlier migrations" in caught.value.reason

    def test_a_ruby_that_cannot_run_is_a_reason_not_a_crash(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "db" / "migrate").mkdir(parents=True)
        (tmp_path / "db" / "schema.rb").write_text("x", encoding="utf-8")
        migration = "db/migrate/20240101_add.rb"
        (tmp_path / migration).write_text("class Add < X; end", encoding="utf-8")
        with pytest.raises(RailsExtractionError) as caught:
            extract_rails_sql(
                migration,
                repo_root=tmp_path,
                scratch_url=SCRATCH,
                ruby=str(tmp_path / "no-such-ruby"),
            )
        assert caught.value.reason


class TestSchemaThatWillNotLoad:
    """`db/schema.rb` is lossy, so a committed schema can fail on its own.

    It cannot express a function, a trigger or a custom type. An
    application that creates one in a migration and then uses it as a
    column default -- Mastodon does exactly this -- has a schema.rb that
    does not load in isolation. Falling back to replaying the migrations
    builds the same state the long way rather than giving up.
    """

    def _app(self, tmp_path: Path, earlier: int) -> str:
        directory = tmp_path / "db" / "migrate"
        directory.mkdir(parents=True)
        (tmp_path / "db" / "schema.rb").write_text("broken", encoding="utf-8")
        for index in range(1, earlier + 1):
            (directory / f"{index:03d}_earlier.rb").write_text("x", encoding="utf-8")
        target = "db/migrate/999_target.rb"
        (tmp_path / target).write_text("class Target < X; end", encoding="utf-8")
        return target

    def _fake_ruby(
        self, monkeypatch: pytest.MonkeyPatch, responses: list[dict[str, object]]
    ) -> list[dict[str, object]]:
        """Stand in for the harness, answering with ``responses`` in order."""
        seen: list[dict[str, object]] = []

        def fake_run(command: list[str], **kwargs: object) -> object:
            seen.append(json.loads(Path(command[-2]).read_text()))
            Path(command[-1]).write_text(
                json.dumps(responses[len(seen) - 1]), encoding="utf-8"
            )
            return subprocess.CompletedProcess(command, 0, "", "")

        monkeypatch.setattr("blastoise.rails.extract.subprocess.run", fake_run)
        return seen

    def test_a_schema_that_fails_to_load_falls_back_to_replay(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = self._app(tmp_path, earlier=3)
        seen = self._fake_ruby(
            monkeypatch,
            [
                {"ok": False, "stage": "schema", "error": "function timestamp_id() ..."},
                {
                    "ok": True,
                    "stage": "migration",
                    "statements": [{"sql": "ALTER TABLE t ADD c int", "name": None}],
                    "rails_version": "8.0.2",
                },
            ],
        )
        result = extract_rails_sql(
            target, repo_root=tmp_path, scratch_url=SCRATCH, ruby="ruby"
        )
        assert len(seen) == 2
        # The retry drops the schema file and replays the history instead.
        assert seen[0]["schema_file"] is not None and seen[0]["replay"] == []
        assert seen[1]["schema_file"] is None
        assert seen[1]["replay"] == [
            str(tmp_path / "db" / "migrate" / f"{index:03d}_earlier.rb")
            for index in (1, 2, 3)
        ]
        # The report says which route produced the state, because a verdict
        # built on a replayed history is a different claim from one built on
        # the committed schema.
        assert "would not load" in result.schema_source
        assert "replayed 3" in result.schema_source

    def test_a_migration_that_fails_is_not_retried(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Only a schema-stage failure is worth another route. A migration
        # that raises would raise again, and retrying would just double the
        # time before the honest answer.
        target = self._app(tmp_path, earlier=3)
        seen = self._fake_ruby(
            monkeypatch,
            [{"ok": False, "stage": "migration", "error": "NoMethodError: nope"}],
        )
        with pytest.raises(RailsExtractionError) as caught:
            extract_rails_sql(target, repo_root=tmp_path, scratch_url=SCRATCH, ruby="ruby")
        assert len(seen) == 1
        assert "nope" in caught.value.reason

    def test_it_refuses_rather_than_replay_an_entire_history(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = self._app(tmp_path, earlier=12)
        self._fake_ruby(
            monkeypatch,
            [{"ok": False, "stage": "schema", "error": "boom"}],
        )
        with pytest.raises(RailsExtractionError) as caught:
            extract_rails_sql(
                target,
                repo_root=tmp_path,
                scratch_url=SCRATCH,
                ruby="ruby",
                replay_limit=5,
            )
        assert "would not load" in caught.value.reason
        assert "12 earlier migrations" in caught.value.reason


class TestHarnessIsValidRuby:
    """The shipped harness is parsed by Ruby, not only by review."""

    def test_it_is_syntactically_valid(self) -> None:
        from blastoise.rails import HARNESS

        assert HARNESS.is_file()
        try:
            result = subprocess.run(
                ["ruby", "-c", str(HARNESS)],
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
        except (OSError, subprocess.SubprocessError):
            pytest.skip("no ruby available to syntax-check the harness")
        assert result.returncode == 0, result.stderr
