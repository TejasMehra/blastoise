"""Secret redaction, including the paths that leak by accident.

A connection string reaches CI output through four doors: the log line
someone wrote, the exception message a driver raised, the traceback that
escaped, and the comment body posted to a public pull request. All four go
through the redactor, and these tests hold each of them shut.
"""

from __future__ import annotations

import pytest

from blastoise.ci.redact import PLACEHOLDER, Redactor, parse_conninfo

DSN = "postgresql://svc_migrate:hunter2@db-staging.internal.example:6432/billing?sslmode=require"
KEYWORD_DSN = (
    "host=db-staging.internal.example port=6432 dbname=billing "
    "user=svc_migrate password=hunter2"
)


@pytest.fixture
def redactor() -> Redactor:
    instance = Redactor()
    instance.add_connection_string(DSN)
    return instance


class TestParsing:
    def test_uri_form(self) -> None:
        params = parse_conninfo(DSN)
        assert params["user"] == "svc_migrate"
        assert params["password"] == "hunter2"
        assert params["host"] == "db-staging.internal.example"
        assert params["port"] == "6432"
        assert params["dbname"] == "billing"
        assert params["sslmode"] == "require"

    def test_keyword_form(self) -> None:
        params = parse_conninfo(KEYWORD_DSN)
        assert params["password"] == "hunter2"
        assert params["host"] == "db-staging.internal.example"

    def test_quoted_keyword_value_with_spaces(self) -> None:
        params = parse_conninfo("dbname=billing password='two words' user=svc")
        assert params["password"] == "two words"
        assert params["user"] == "svc"

    def test_percent_encoded_password(self) -> None:
        params = parse_conninfo("postgres://u:p%40ss%2Fword@h/db")
        assert params["password"] == "p@ss/word"

    def test_multi_host_uri_does_not_explode(self) -> None:
        # urlsplit raises on the second colon in the authority; the parser
        # splits it itself precisely so a failover DSN still registers.
        params = parse_conninfo("postgres://u:p@h1.example:5432,h2.example:5432/db")
        assert params["host"] == "h1.example"
        assert params["host1"] == "h2.example"

    @pytest.mark.parametrize("text", ["", "   ", "not a dsn at all"])
    def test_unparseable_input_never_raises(self, text: str) -> None:
        assert isinstance(parse_conninfo(text), dict)


class TestLiterals:
    def test_the_whole_dsn_goes(self, redactor: Redactor) -> None:
        assert DSN not in redactor.scrub(f"connecting to {DSN} now")

    def test_the_password_goes_wherever_it_appears(self, redactor: Redactor) -> None:
        assert "hunter2" not in redactor.scrub("PGPASSWORD was hunter2 all along")

    def test_the_host_goes_wherever_it_appears(self, redactor: Redactor) -> None:
        scrubbed = redactor.scrub("could not resolve db-staging.internal.example")
        assert "db-staging.internal.example" not in scrubbed
        assert PLACEHOLDER in scrubbed

    def test_the_user_and_dbname_go(self, redactor: Redactor) -> None:
        scrubbed = redactor.scrub("role svc_migrate has no access to billing")
        assert "svc_migrate" not in scrubbed
        assert "billing" not in scrubbed

    def test_generic_component_values_are_not_shredded(self) -> None:
        # Redacting "postgres" or "localhost" everywhere would mangle prose
        # without hiding anything an attacker does not already assume.
        instance = Redactor()
        instance.add_connection_string("postgres://postgres@localhost:5432/postgres")
        assert instance.scrub("running on localhost") == "running on localhost"

    def test_a_short_password_is_still_redacted(self) -> None:
        # Below the length floor that applies to hosts and user names: a
        # password is registered whatever it is.
        instance = Redactor()
        instance.add_connection_string("postgres://u:abc@h.example/d")
        assert "abc" not in instance.scrub("the password is abc")

    def test_environment_registration(self) -> None:
        instance = Redactor()
        instance.add_environment(
            {"BLASTOISE_DATABASE_URL": DSN, "GITHUB_TOKEN": "ghp_abcdef123456"},
            "BLASTOISE_DATABASE_URL",
            "GITHUB_TOKEN",
        )
        scrubbed = instance.scrub(f"{DSN} and ghp_abcdef123456")
        assert "hunter2" not in scrubbed
        assert "ghp_abcdef123456" not in scrubbed


class TestPatterns:
    """A DSN the redactor was never told about still must not survive."""

    def test_an_unregistered_uri_is_caught(self) -> None:
        instance = Redactor()
        assert "sekret" not in instance.scrub(
            "tried postgresql://bob:sekret@other.example/db and failed"
        )

    def test_a_sqlalchemy_style_scheme_is_caught(self) -> None:
        instance = Redactor()
        assert "sekret" not in instance.scrub("postgresql+psycopg://bob:sekret@h/db")

    def test_a_uri_in_prose_does_not_swallow_the_sentence(self) -> None:
        instance = Redactor()
        scrubbed = instance.scrub("used postgres://u:p@h/db and then gave up")
        assert scrubbed.endswith("and then gave up")

    def test_an_unregistered_password_keyword_is_caught(self) -> None:
        instance = Redactor()
        scrubbed = instance.scrub("host=h dbname=d password=sekret sslmode=require")
        assert "sekret" not in scrubbed
        # Only the password: rewriting host= and dbname= by pattern would
        # rewrite ordinary SQL too.
        assert "dbname=d" in scrubbed
        assert "sslmode=require" in scrubbed

    def test_libpq_connection_failure_text_loses_host_and_address(self) -> None:
        instance = Redactor()
        scrubbed = instance.scrub(
            'connection to server at "db.internal" (10.0.0.4), port 5432 failed: timeout'
        )
        assert "db.internal" not in scrubbed
        assert "10.0.0.4" not in scrubbed
        assert scrubbed.endswith("port 5432 failed: timeout")

    def test_ordinary_sql_is_not_mangled(self) -> None:
        instance = Redactor()
        sql = "UPDATE accounts SET host = 'x' WHERE user = 'bob' AND dbname = 'y';"
        assert instance.scrub(sql) == sql


class TestErrorPaths:
    def test_an_exception_message_is_scrubbed(self, redactor: Redactor) -> None:
        exc = OSError(f"could not connect to {DSN}")
        message = redactor.scrub_exception(exc)
        assert "hunter2" not in message
        assert "db-staging.internal.example" not in message
        assert message.startswith("OSError: ")

    def test_a_traceback_is_scrubbed(self, redactor: Redactor) -> None:
        try:
            raise ValueError(f"bad conninfo: {DSN}")
        except ValueError as exc:
            text = redactor.scrub_traceback(exc)
        assert "hunter2" not in text
        assert "db-staging.internal.example" not in text
        # Still a usable traceback: the frame is there, the secret is not.
        assert "Traceback (most recent call last)" in text
        assert "test_ci_redact.py" in text

    def test_a_chained_cause_is_scrubbed_too(self, redactor: Redactor) -> None:
        try:
            try:
                raise OSError(KEYWORD_DSN)
            except OSError as inner:
                raise RuntimeError("giving up") from inner
        except RuntimeError as exc:
            text = redactor.scrub_traceback(exc)
        assert "hunter2" not in text
        assert "db-staging.internal.example" not in text

    def test_an_exception_with_no_message_still_names_its_type(self, redactor: Redactor) -> None:
        assert redactor.scrub_exception(TimeoutError()) == "TimeoutError: TimeoutError"


class TestBookkeeping:
    def test_registering_the_same_secret_twice_is_idempotent(self) -> None:
        instance = Redactor()
        instance.add_connection_string(DSN)
        first = instance.secret_count
        instance.add_connection_string(DSN)
        assert instance.secret_count == first

    def test_nothing_registered_is_not_an_error(self) -> None:
        instance = Redactor()
        instance.add_connection_string(None)
        instance.add_connection_string("")
        instance.add_secret(None)
        assert instance.secret_count == 0
        assert instance.scrub("plain text") == "plain text"

    def test_the_longest_literal_wins(self, redactor: Redactor) -> None:
        # The whole DSN is replaced as one unit rather than perforated
        # component by component, so the output reads as one placeholder.
        assert redactor.scrub(DSN) == PLACEHOLDER
