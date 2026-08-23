""".blastoise.yml: defaults, strictness, and the keys that are refused."""

from __future__ import annotations

from pathlib import Path

import pytest

from blastoise.ci.config import (
    DEFAULT_DATABASE_URL_ENV,
    CiConfig,
    ConfigError,
    FailOn,
    load_config,
    parse_config,
)


def _write(tmp_path: Path, text: str, name: str = ".blastoise.yml") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


class TestDefaults:
    def test_no_file_is_the_common_case(self, tmp_path: Path) -> None:
        config = load_config(None, root=tmp_path)
        assert config == CiConfig()
        assert config.database_url_env == DEFAULT_DATABASE_URL_ENV
        assert config.fail_on is FailOn.BLOCK
        assert not config.overrides_detection

    def test_an_empty_file_is_all_defaults(self, tmp_path: Path) -> None:
        assert load_config(_write(tmp_path, "")) == CiConfig()

    def test_yaml_extension_is_found_too(self, tmp_path: Path) -> None:
        _write(tmp_path, "migrations:\n  paths: ['a/*.sql']\n", name=".blastoise.yaml")
        assert load_config(None, root=tmp_path).paths == ("a/*.sql",)

    def test_a_config_named_explicitly_and_missing_is_an_error(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="no such config file"):
            load_config(tmp_path / "nope.yml")


class TestFullDocument:
    def test_every_key_round_trips(self, tmp_path: Path) -> None:
        config = load_config(
            _write(
                tmp_path,
                """
                version: 1
                migrations:
                  paths:
                    - "sql/schema/**/*.sql"
                  exclude:
                    - "**/seed_*.sql"
                database:
                  url_env: MY_STAGING_URL
                  label: staging
                  pg_version: 16
                ci:
                  fail_on: requires_approval
                  comment: false
                  check_run: false
                """,
            )
        )
        assert config.paths == ("sql/schema/**/*.sql",)
        assert config.exclude == ("**/seed_*.sql",)
        assert config.database_url_env == "MY_STAGING_URL"
        assert config.database_label == "staging"
        assert config.pg_version == 16
        assert config.fail_on is FailOn.REQUIRES_APPROVAL
        assert config.comment is False
        assert config.check_run is False
        assert config.overrides_detection


class TestStrictness:
    """A silently ignored key is a team believing an exclusion is in force."""

    @pytest.mark.parametrize(
        ("document", "message"),
        [
            ("migration:\n  paths: []\n", "unknown key 'migration'"),
            ("migrations:\n  path: []\n", "unknown key 'migrations.path'"),
            ("database:\n  urlenv: X\n", "unknown key 'database.urlenv'"),
            ("ci:\n  failon: block\n", "unknown key 'ci.failon'"),
        ],
    )
    def test_unknown_keys_are_errors(self, document: str, message: str) -> None:
        with pytest.raises(ConfigError, match=message):
            parse_config_text(document)

    def test_a_list_written_as_a_string_is_caught(self) -> None:
        with pytest.raises(ConfigError, match="must be a list of globs"):
            parse_config_text("migrations:\n  paths: 'sql/*.sql'\n")

    def test_a_section_written_as_a_scalar_is_caught(self) -> None:
        with pytest.raises(ConfigError, match="must be a mapping"):
            parse_config_text("migrations: sql/*.sql\n")

    def test_unknown_fail_on_lists_the_known_values(self) -> None:
        with pytest.raises(ConfigError, match="block, requires_approval, never"):
            parse_config_text("ci:\n  fail_on: sometimes\n")

    def test_non_boolean_comment_is_caught(self) -> None:
        with pytest.raises(ConfigError, match="must be true or false"):
            parse_config_text("ci:\n  comment: yes-please\n")

    def test_a_future_config_version_is_refused(self) -> None:
        with pytest.raises(ConfigError, match="version 2 is not supported"):
            parse_config_text("version: 2\n")

    def test_invalid_yaml_names_the_file(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="not valid YAML"):
            load_config(_write(tmp_path, "migrations: [\n"))


class TestCredentialsAreRefused:
    """A connection string in a committed file is the failure this prevents."""

    @pytest.mark.parametrize("key", ["url", "dsn", "password"])
    def test_a_credential_key_is_refused_with_the_reason(self, key: str) -> None:
        with pytest.raises(ConfigError) as caught:
            parse_config_text(f"database:\n  {key}: postgres://u:p@h/db\n")
        message = str(caught.value)
        assert "committed file" in message
        assert "url_env" in message or "url_env" in message

    def test_a_connection_string_where_the_name_belongs_is_refused(self) -> None:
        with pytest.raises(ConfigError, match="takes the NAME of an environment"):
            parse_config_text("database:\n  url_env: postgres://u:p@h/db\n")

    def test_the_refusal_does_not_echo_the_credential(self) -> None:
        with pytest.raises(ConfigError) as caught:
            parse_config_text("database:\n  url_env: postgres://u:hunter2@h/db\n")
        assert "hunter2" not in str(caught.value)


def parse_config_text(text: str) -> CiConfig:
    import yaml

    return parse_config(yaml.safe_load(text), where="<test>")


class TestTypeValidation:
    @pytest.mark.parametrize(
        ("document", "message"),
        [
            ("version: 'one'\n", "'version' must be an integer"),
            ("- a\n- b\n", "must be a mapping"),
            ("database:\n  url_env: 4\n", "must be a non-empty string"),
            ("database:\n  url_env: '   '\n", "must be a non-empty string"),
            ("database:\n  label: 4\n", "'database.label' must be a string"),
            ("database:\n  pg_version: 'seventeen'\n", "'database.pg_version' must be an integer"),
            ("ci:\n  fail_on: 4\n", "'ci.fail_on' must be a string"),
            ("migrations:\n  paths: [1, 2]\n", "must be a list of strings"),
        ],
    )
    def test_a_wrong_type_names_the_key_and_the_expected_shape(
        self, document: str, message: str
    ) -> None:
        with pytest.raises(ConfigError, match=message):
            parse_config_text(document)

    def test_blank_glob_entries_are_dropped_not_treated_as_match_all(self) -> None:
        config = parse_config_text("migrations:\n  paths: ['a/*.sql', '', '  ']\n")
        assert config.paths == ("a/*.sql",)

    def test_an_unreadable_config_file_is_a_config_error(self, tmp_path: Path) -> None:
        directory = tmp_path / ".blastoise.yml"
        directory.mkdir()
        with pytest.raises(ConfigError, match="no such config file"):
            load_config(directory)
