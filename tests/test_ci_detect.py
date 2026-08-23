"""Migration detection: each framework's layout, and the config override.

Detection is what decides whether the CI integration works on a repository
nobody configured, so the layouts are pinned one framework at a time, with
the near-misses that must NOT match sitting beside them.
"""

from __future__ import annotations

import pytest

from blastoise.ci.detect import (
    DSL_ADAPTER_HINT,
    FRAMEWORK_NAMES,
    Framework,
    SourceKind,
    detect_migrations,
    framework_of,
    glob_to_regex,
    normalize_path,
)


def _one(path: str) -> tuple[Framework, SourceKind]:
    result = framework_of(path)
    assert result is not None, f"{path} was not detected as a migration"
    return result


class TestFrameworkLayouts:
    @pytest.mark.parametrize(
        "path",
        [
            "db/migrate/20240101120000_add_plan_to_events.rb",
            "db/migrate/001_init.rb",
            # Multi-database Rails: db/<name>/migrate/.
            "db/primary/migrate/20240101120000_add_plan.rb",
            # A monorepo keeps the app one level down; the layout inside it
            # is unchanged.
            "services/billing/db/migrate/20240101_add.rb",
        ],
    )
    def test_rails(self, path: str) -> None:
        assert _one(path) == (Framework.RAILS, SourceKind.DSL)

    @pytest.mark.parametrize(
        "path",
        [
            "app/users/migrations/0001_initial.py",
            "src/billing/migrations/0042_add_plan.py",
            "migrations/0001_initial.py",
        ],
    )
    def test_django(self, path: str) -> None:
        assert _one(path) == (Framework.DJANGO, SourceKind.DSL)

    def test_django_package_scaffolding_is_not_a_migration(self) -> None:
        assert framework_of("app/users/migrations/__init__.py") is None

    @pytest.mark.parametrize(
        "path",
        [
            "prisma/migrations/20240101120000_add_plan/migration.sql",
            "apps/web/prisma/migrations/20240101_init/migration.sql",
        ],
    )
    def test_prisma(self, path: str) -> None:
        assert _one(path) == (Framework.PRISMA, SourceKind.SQL)

    def test_prisma_beats_the_generic_migrations_rule(self) -> None:
        # The path also matches migrations/**/*.sql; most specific wins.
        framework, _ = _one("prisma/migrations/20240101_init/migration.sql")
        assert framework is Framework.PRISMA

    @pytest.mark.parametrize(
        "path",
        [
            "src/main/resources/db/migration/V1__init.sql",
            "sql/V1_2_3__add_plan.sql",
            "sql/U1__undo_add_plan.sql",
            "sql/R__recreate_views.sql",
        ],
    )
    def test_flyway(self, path: str) -> None:
        assert _one(path) == (Framework.FLYWAY, SourceKind.SQL)

    def test_flyway_name_rule_requires_a_digit_after_the_prefix(self) -> None:
        # The version must start with a digit, which is what stops this from
        # claiming every double-underscore file name in the repository.
        assert framework_of("sql/Views__old.sql") is None
        assert framework_of("docs/Upgrade__notes.sql") is None

    @pytest.mark.parametrize(
        "path",
        [
            "alembic/versions/1a2b3c4d5e6f_add_plan.py",
            "migrations/versions/1a2b3c_add_plan.py",
            "db/migrations/versions/9f8e_init.py",
        ],
    )
    def test_alembic(self, path: str) -> None:
        assert _one(path) == (Framework.ALEMBIC, SourceKind.DSL)

    @pytest.mark.parametrize(
        "path",
        [
            "migrations/000001_init.up.sql",
            "migrations/000001_init.down.sql",
            "db/migrations/20240101_add_plan.up.sql",
        ],
    )
    def test_golang_migrate(self, path: str) -> None:
        # Down files are migrations too: a rollback runs them, so they are
        # SQL that will execute against production.
        assert _one(path) == (Framework.GOLANG_MIGRATE, SourceKind.SQL)

    @pytest.mark.parametrize(
        "path",
        [
            "migrations/0042_add_plan.sql",
            "db/migrations/0042_add_plan.sql",
            "db/migrations/2024/q1/0042.sql",
            "migration/0001.sql",
        ],
    )
    def test_plain_migrations_directory(self, path: str) -> None:
        assert _one(path) == (Framework.GENERIC, SourceKind.SQL)

    @pytest.mark.parametrize(
        "path",
        [
            "README.md",
            "src/app.py",
            "queries/report.sql",
            "test/fixtures/schema.sql",
            "db/schema.rb",
        ],
    )
    def test_non_migrations(self, path: str) -> None:
        assert framework_of(path) is None

    def test_every_framework_has_a_display_name(self) -> None:
        assert set(FRAMEWORK_NAMES) == set(Framework)

    def test_every_dsl_framework_names_its_adapter(self) -> None:
        # A "not supported yet" message that does not say what support would
        # take is just an apology.
        dsl = {Framework.RAILS, Framework.DJANGO, Framework.ALEMBIC}
        assert dsl <= set(DSL_ADAPTER_HINT)
        for framework in dsl:
            assert DSL_ADAPTER_HINT[framework].strip()


class TestNormalization:
    def test_windows_separators_and_dot_prefixes(self) -> None:
        assert normalize_path(r"db\migrate\001_init.rb") == "db/migrate/001_init.rb"
        assert normalize_path("./migrations/0001.sql") == "migrations/0001.sql"
        assert normalize_path("/migrations/0001.sql") == "migrations/0001.sql"

    def test_a_windows_path_still_detects(self) -> None:
        assert _one(r"db\migrate\20240101_add.rb") == (Framework.RAILS, SourceKind.DSL)


class TestDetectMigrations:
    def test_filters_and_sorts_and_deduplicates(self) -> None:
        detected = detect_migrations(
            (
                "README.md",
                "migrations/0002.sql",
                "migrations/0001.sql",
                "./migrations/0001.sql",
                "db/migrate/001_init.rb",
            )
        )
        assert [entry.path for entry in detected] == [
            "db/migrate/001_init.rb",
            "migrations/0001.sql",
            "migrations/0002.sql",
        ]

    def test_empty_and_blank_paths_are_ignored(self) -> None:
        assert detect_migrations(("", "   ", "migrations/0001.sql")) != ()
        assert len(detect_migrations(("", "   "))) == 0

    def test_assessable_tracks_source_kind(self) -> None:
        detected = detect_migrations(("migrations/0001.sql", "db/migrate/001.rb"))
        assert [entry.assessable for entry in detected] == [False, True]


class TestConfigOverridePrecedence:
    """`paths` replaces detection; `exclude` applies either way."""

    def test_paths_override_detection_entirely(self) -> None:
        detected = detect_migrations(
            ("migrations/0001.sql", "sql/schema/001_init.sql"),
            include=("sql/schema/*.sql",),
        )
        # The conventional migrations/ file is NOT included: an override
        # that only added would leave a team unable to turn one off.
        assert [entry.path for entry in detected] == ["sql/schema/001_init.sql"]

    def test_configured_path_still_reports_a_matched_framework(self) -> None:
        detected = detect_migrations(
            ("db/migrate/001_init.rb",), include=("db/migrate/*.rb",)
        )
        assert detected[0].framework is Framework.RAILS
        assert detected[0].source_kind is SourceKind.DSL

    def test_configured_path_no_convention_describes(self) -> None:
        detected = detect_migrations(
            ("ops/schema/001.sql", "ops/schema/001.rb"),
            include=("ops/schema/*",),
        )
        assert [(e.framework, e.source_kind) for e in detected] == [
            (Framework.CONFIGURED, SourceKind.DSL),
            (Framework.CONFIGURED, SourceKind.SQL),
        ]

    def test_exclude_applies_to_detected_files(self) -> None:
        detected = detect_migrations(
            ("migrations/0001.sql", "migrations/seed_users.sql"),
            exclude=("**/seed_*.sql",),
        )
        assert [entry.path for entry in detected] == ["migrations/0001.sql"]

    def test_exclude_applies_to_configured_files_too(self) -> None:
        detected = detect_migrations(
            ("sql/a.sql", "sql/legacy/b.sql"),
            include=("sql/**/*.sql",),
            exclude=("sql/legacy/**",),
        )
        assert [entry.path for entry in detected] == ["sql/a.sql"]

    def test_exclude_wins_over_include(self) -> None:
        detected = detect_migrations(
            ("sql/a.sql",), include=("sql/*.sql",), exclude=("sql/a.sql",)
        )
        assert detected == ()


class TestGlobs:
    @pytest.mark.parametrize(
        ("pattern", "path", "expected"),
        [
            # * stays inside one segment. fnmatch would get this wrong, and
            # getting it wrong means migrations/*.sql matches every SQL
            # file in every subdirectory.
            ("migrations/*.sql", "migrations/0001.sql", True),
            ("migrations/*.sql", "migrations/2024/0001.sql", False),
            ("*.sql", "migrations/0001.sql", False),
            # ** spans segments, including none.
            ("migrations/**/*.sql", "migrations/0001.sql", True),
            ("migrations/**/*.sql", "migrations/2024/q1/0001.sql", True),
            ("sql/**", "sql/a/b/c.sql", True),
            ("sql/**", "other/a.sql", False),
            ("db/?/x.sql", "db/1/x.sql", True),
            ("db/?/x.sql", "db/12/x.sql", False),
            ("v[0-9].sql", "v3.sql", True),
            ("v[0-9].sql", "va.sql", False),
            ("v[!0-9].sql", "va.sql", True),
            # Anchored: a glob matches the whole path, not a fragment.
            ("migrations/0001.sql", "app/migrations/0001.sql", False),
        ],
    )
    def test_semantics(self, pattern: str, path: str, expected: bool) -> None:
        assert bool(glob_to_regex(pattern).match(path)) is expected

    def test_windows_written_glob_is_normalized(self) -> None:
        assert glob_to_regex(r"sql\schema\*.sql").match("sql/schema/a.sql")

    def test_unclosed_class_is_a_literal_not_a_crash(self) -> None:
        assert glob_to_regex("a[b.sql").match("a[b.sql")
