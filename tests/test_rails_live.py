"""Rails extraction, end to end, against a real Ruby and a real Postgres.

This is the test that would have caught every mistake the unit tests
cannot: whether ActiveRecord actually emits what the design assumes. It
needs a Ruby that can `require 'active_record'` and `require 'pg'`, so it
skips rather than fails where there is none -- but where those exist it
runs the shipped harness, on a real migration, against a real database.

The migration it uses is chosen to be the one a schema diff would get
wrong: an index built CONCURRENTLY (which the catalog does not record as
concurrent), a backfill UPDATE (which leaves no trace in a schema at all),
and `disable_ddl_transaction!` (which is not a property of any table).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from live_harness import PgServer

from blastoise.parser import parse_migration
from blastoise.rails import extract_rails_sql

MIGRATION = """\
class AddStatusToUsers < ActiveRecord::Migration[7.0]
  disable_ddl_transaction!

  def up
    add_column :users, :status, :string, default: 'active', null: false
    add_index :users, :status, algorithm: :concurrently
    execute "UPDATE users SET status = 'legacy' WHERE created_at < now()"
  end
end
"""

SCHEMA = """\
ActiveRecord::Schema.define(version: 2024_01_01_000000) do
  create_table "users", force: :cascade do |t|
    t.string "email"
    t.datetime "created_at", null: false
  end
end
"""


def _ruby() -> str:
    """A Ruby that can load ActiveRecord and pg, or skip."""
    candidate = os.environ.get("BLASTOISE_TEST_RUBY") or shutil.which("ruby")
    if not candidate:
        pytest.skip(
            "no ruby: set BLASTOISE_TEST_RUBY to an interpreter with "
            "activerecord and pg installed"
        )
    try:
        result = subprocess.run(
            [candidate, "-e", "require 'active_record'; require 'pg'"],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover
        pytest.skip(f"could not run {candidate}: {exc}")
    if result.returncode != 0:
        pytest.skip(
            f"{candidate} cannot load activerecord and pg "
            f"({result.stderr.strip().splitlines()[-1:] or ['unknown']})"
        )
    return candidate


@pytest.fixture
def rails_app(tmp_path: Path) -> Path:
    """A minimal application with the schema committed, as a repo would be."""
    root = tmp_path / "app"
    (root / "db" / "migrate").mkdir(parents=True)
    (root / "db" / "schema.rb").write_text(SCHEMA, encoding="utf-8")
    (root / "db" / "migrate" / "20240102000000_add_status_to_users.rb").write_text(
        MIGRATION, encoding="utf-8"
    )
    for args in (
        ["init", "-q"],
        ["add", "-A"],
        ["-c", "user.email=t@example.invalid", "-c", "user.name=t", "commit", "-qm", "base"],
    ):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    return root


class TestExtractionAgainstRealRails:
    def test_it_recovers_what_a_schema_diff_would_lose(
        self, rails_app: Path, pg_server: PgServer
    ) -> None:
        extraction = extract_rails_sql(
            "db/migrate/20240102000000_add_status_to_users.rb",
            repo_root=rails_app,
            scratch_url=pg_server.admin_dsn,
            base_ref="HEAD",
            ruby=_ruby(),
        )

        joined = "\n".join(extraction.statements).upper()
        # The three facts that only survive because the SQL was recorded as
        # it ran, rather than reconstructed from the resulting schema.
        assert "CONCURRENTLY" in joined
        assert "UPDATE USERS" in joined
        assert extraction.disable_ddl_transaction is True
        # disable_ddl_transaction! means Rails wraps nothing, so there is no
        # BEGIN to find.
        assert "BEGIN" not in joined

        assert extraction.schema_source.startswith("db/schema.rb")

    def test_a_migration_installed_from_an_engine_still_renders(
        self, rails_app: Path, pg_server: PgServer
    ) -> None:
        # `rails railties:install:migrations` copies an engine's migrations
        # in with a scope suffix: 20240103000000_add_thing.active_storage.rb.
        # The scope is not part of the class name, and treating it as one
        # names a class that does not exist. Found against openfoodnetwork,
        # which carries Active Storage's migrations this way.
        name = "20240103000000_add_note_to_users.active_storage.rb"
        (rails_app / "db" / "migrate" / name).write_text(
            "class AddNoteToUsers < ActiveRecord::Migration[7.0]\n"
            "  def change\n"
            "    add_column :users, :note, :string\n"
            "  end\n"
            "end\n",
            encoding="utf-8",
        )
        extraction = extract_rails_sql(
            f"db/migrate/{name}",
            repo_root=rails_app,
            scratch_url=pg_server.admin_dsn,
            base_ref="HEAD",
            ruby=_ruby(),
        )
        assert extraction.migration_class == "AddNoteToUsers"
        assert any("note" in statement for statement in extraction.statements)

    def test_the_extracted_sql_enters_the_ordinary_parser(
        self, rails_app: Path, pg_server: PgServer
    ) -> None:
        # The engine is not adapted to the extractor. If this ever needs a
        # special case, the extraction is wrong.
        extraction = extract_rails_sql(
            "db/migrate/20240102000000_add_status_to_users.rb",
            repo_root=rails_app,
            scratch_url=pg_server.admin_dsn,
            base_ref="HEAD",
            ruby=_ruby(),
        )
        script = parse_migration(extraction.sql, path="db/migrate/x.rb")
        kinds = [str(statement.kind) for statement in script.statements]
        assert "create_index_concurrently" in kinds
        assert "update" in kinds
        assert all(not group.explicit for group in script.transaction_groups)
