"""The comment body: what leads, what is always visible, what is dropped."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from blastoise.catalog import load_catalog
from blastoise.ci.detect import Framework, SourceKind
from blastoise.ci.markdown import (
    COMMENT_MARKER,
    GITHUB_COMMENT_LIMIT,
    render_comment,
    render_summary_line,
)
from blastoise.ci.model import CiRun, FileOutcome, OutcomeStatus, run_verdict
from blastoise.parser import parse_migration
from blastoise.report import FileVerdict, build_report
from blastoise.verdict import assess_script


def _payload(sql: str) -> dict[str, Any]:
    script = parse_migration(sql)
    catalog = load_catalog()
    assessment = assess_script(script, catalog, 17, None)
    payload, _ = build_report(
        script,
        assessment,
        catalog=catalog,
        snapshot=None,
        evaluated_at=datetime.now(UTC).isoformat(),
    )
    return payload


def _outcome(path: str, sql: str) -> FileOutcome:
    payload = _payload(sql)
    return FileOutcome(
        path=path,
        framework=Framework.GENERIC,
        source_kind=SourceKind.SQL,
        status=OutcomeStatus.ASSESSED,
        verdict=FileVerdict(payload["verdict"]),
        counts=dict(payload["classification_counts"]),
        payload=payload,
    )


def _run(*outcomes: FileOutcome, **kwargs: Any) -> CiRun:
    defaults: dict[str, Any] = {
        "online": False,
        "tool_version": "0.1.0",
        "changed_files": len(outcomes),
    }
    return CiRun(outcomes=tuple(outcomes), **(defaults | kwargs))


BLOCKING = "BEGIN;\nCREATE INDEX CONCURRENTLY i ON events (plan);\nCOMMIT;\n"
DROPPING = "DROP TABLE audit_log;\n"


class TestWhatLeads:
    def test_the_marker_is_first_so_the_comment_can_be_found_again(self) -> None:
        body = render_comment(_run(_outcome("migrations/0001.sql", BLOCKING)))
        assert body.startswith(COMMENT_MARKER)

    def test_the_verdict_is_in_the_heading(self) -> None:
        body = render_comment(_run(_outcome("migrations/0001.sql", BLOCKING)))
        heading = body.splitlines()[1]
        assert heading.startswith("## ")
        assert "BLOCK" in heading

    def test_the_tier_counts_come_before_any_file_section(self) -> None:
        body = render_comment(_run(_outcome("migrations/0001.sql", BLOCKING)))
        counts_at = body.index("| unsafe | unknown | needs_timing |")
        assert counts_at < body.index("### ")

    def test_the_tiers_are_worst_first(self) -> None:
        body = render_comment(_run(_outcome("migrations/0001.sql", BLOCKING)))
        header = body[body.index("| unsafe") :].splitlines()[0]
        assert header == "| unsafe | unknown | needs_timing | safe_irreversible | safe |"

    def test_the_legend_bridges_machine_names_to_display_names(self) -> None:
        body = render_comment(_run(_outcome("migrations/0001.sql", BLOCKING)))
        assert "`unsafe` Hydro Pump" in body
        assert "`safe` Calm Water" in body

    def test_offline_says_so_where_it_cannot_be_missed(self) -> None:
        body = render_comment(_run(_outcome("migrations/0001.sql", BLOCKING)))
        assert "**offline**" in body[: body.index("### ")]

    def test_an_empty_run_says_there_was_nothing_to_assess(self) -> None:
        body = render_comment(_run())
        assert "No migration files were changed" in body


class TestUnverifiedIsAlwaysVisible:
    """The section that must never be behind a disclosure widget."""

    def test_it_is_present(self) -> None:
        body = render_comment(_run(_outcome("migrations/0001.sql", BLOCKING)))
        assert "**Not verified (" in body

    def test_it_is_not_inside_a_details_block(self) -> None:
        body = render_comment(_run(_outcome("migrations/0001.sql", BLOCKING)))
        for start in _indices(body, "**Not verified ("):
            before = body[:start]
            assert before.count("<details>") == before.count("</details>"), (
                "the unverified section starts inside an open <details> block"
            )

    def test_every_entry_is_rendered(self) -> None:
        outcome = _outcome("migrations/0001.sql", BLOCKING)
        assert outcome.payload is not None
        entries = outcome.payload["unverified"]
        body = render_comment(_run(outcome))
        section = body[body.index("**Not verified (") :]
        for entry in entries:
            assert f"`{entry['source']}`" in section

    def test_statement_detail_is_collapsible_but_unverified_is_not(self) -> None:
        body = render_comment(_run(_outcome("migrations/0001.sql", BLOCKING)))
        assert "<details><summary>" in body
        details_at = body.index("<details><summary>")
        assert body.index("**Not verified (") > body.index("</details>", details_at)


class TestPerFileSections:
    def test_each_file_gets_its_own_verdict_and_counts(self) -> None:
        run = _run(
            _outcome("migrations/0001.sql", "CREATE TABLE t (id int);\n"),
            _outcome("migrations/0002.sql", BLOCKING),
        )
        body = render_comment(run)
        assert "`migrations/0001.sql` — PROCEED" in body
        assert "`migrations/0002.sql` — BLOCK" in body

    def test_irreversible_work_is_called_out(self) -> None:
        body = render_comment(_run(_outcome("migrations/0001.sql", DROPPING)))
        assert "**Irreversible (" in body

    def test_a_dsl_file_says_what_it_is_and_what_support_would_take(self) -> None:
        outcome = FileOutcome(
            path="db/migrate/001_add.rb",
            framework=Framework.RAILS,
            source_kind=SourceKind.DSL,
            status=OutcomeStatus.UNSUPPORTED,
            detail="Rails migrations are a DSL",
        )
        body = render_comment(_run(outcome))
        assert "NOT ASSESSED" in body
        assert "**Rails**" in body
        assert "rails db:migrate" in body
        assert "was not assessed" in body

    def test_an_error_file_is_a_tool_failure_not_a_finding(self) -> None:
        outcome = FileOutcome(
            path="migrations/0001.sql",
            framework=Framework.GENERIC,
            source_kind=SourceKind.SQL,
            status=OutcomeStatus.ERROR,
            detail="could not be parsed as SQL (syntax error at 3)",
        )
        body = render_comment(_run(outcome))
        assert "tool failure" in body
        assert "syntax error at 3" in body

    def test_unassessed_files_are_flagged_above_the_fold(self) -> None:
        run = _run(
            _outcome("migrations/0001.sql", "CREATE TABLE t (id int);\n"),
            FileOutcome(
                path="db/migrate/001.rb",
                framework=Framework.RAILS,
                source_kind=SourceKind.DSL,
                status=OutcomeStatus.UNSUPPORTED,
            ),
        )
        body = render_comment(run)
        header = body[: body.index("### ")]
        assert "1 of 2 detected migrations was not assessed" in header


class TestEscaping:
    def test_engine_prose_cannot_open_an_html_tag(self) -> None:
        # A type-change rationale says "timestamp<->timestamptz"; a renderer
        # that reads that as a tag eats the rest of the sentence.
        body = render_comment(
            _run(_outcome("migrations/0001.sql", "ALTER TABLE t ALTER COLUMN c TYPE text;\n"))
        )
        section = body[body.index("**Not verified (") :]
        assert "&lt;-&gt;" in section or "<->" not in section

    def test_a_pipe_in_prose_cannot_break_a_table_row(self) -> None:
        outcome = FileOutcome(
            path="migrations/0001.sql",
            framework=Framework.GENERIC,
            source_kind=SourceKind.SQL,
            status=OutcomeStatus.ASSESSED,
            verdict=FileVerdict.PROCEED,
            counts={"safe": 1},
            payload={
                "statements": [
                    {
                        "line": 1,
                        "kind": "create_table",
                        "classification": "safe",
                        "band": "sub_second",
                        "method": "proven",
                        "rationale": "a | b | c",
                    }
                ],
                "unverified": [{"source": "execution_state", "reason": "x", "line": None}],
            },
        )
        row = next(
            line for line in render_comment(_run(outcome)).splitlines() if "create_table" in line
        )
        assert "a \\| b \\| c" in row
        # Six columns means seven cell boundaries; an unescaped pipe in the
        # prose would make it eight and shift every later column.
        assert row.replace("\\|", "").count("|") == 7


class TestSizeLimit:
    """GitHub caps a comment at 65536 characters. Nothing is dropped silently."""

    def test_a_normal_run_is_nowhere_near_the_limit(self) -> None:
        body = render_comment(_run(_outcome("migrations/0001.sql", BLOCKING)))
        assert len(body) < 8000

    def test_a_huge_run_fits_and_says_what_it_dropped(self) -> None:
        outcomes = tuple(
            _outcome(f"migrations/{index:04d}.sql", BLOCKING) for index in range(80)
        )
        body = render_comment(_run(*outcomes))
        assert len(body) <= GITHUB_COMMENT_LIMIT
        assert "omitted to fit this comment" in body
        assert "workflow artifact" in body

    def test_the_verdict_and_the_unverified_section_survive_truncation(self) -> None:
        outcomes = tuple(
            _outcome(f"migrations/{index:04d}.sql", BLOCKING) for index in range(80)
        )
        body = render_comment(_run(*outcomes))
        assert "BLOCK" in body.splitlines()[1]
        assert "**Not verified (" in body

    def test_a_run_too_large_for_any_degradation_is_clipped_visibly(self) -> None:
        outcomes = tuple(
            _outcome(f"migrations/{index:04d}.sql", BLOCKING) for index in range(400)
        )
        body = render_comment(_run(*outcomes))
        assert len(body) <= GITHUB_COMMENT_LIMIT
        assert "clipped at GitHub's size limit" in body

    def test_a_small_limit_still_produces_something_honest(self) -> None:
        body = render_comment(_run(_outcome("migrations/0001.sql", BLOCKING)), limit=1200)
        assert len(body) <= 1200
        assert body.startswith(COMMENT_MARKER)


class TestSummaryLine:
    def test_it_names_the_verdict_and_the_counts(self) -> None:
        run = _run(_outcome("migrations/0001.sql", BLOCKING))
        assert render_summary_line(run) == "BLOCK: 1 migration file assessed"

    def test_it_reports_what_was_not_assessed(self) -> None:
        run = _run(
            _outcome("migrations/0001.sql", "CREATE TABLE t (id int);\n"),
            FileOutcome(
                path="db/migrate/001.rb",
                framework=Framework.RAILS,
                source_kind=SourceKind.DSL,
                status=OutcomeStatus.UNSUPPORTED,
            ),
        )
        assert render_summary_line(run) == (
            "REQUIRES APPROVAL: 1 of 2 migration files assessed, 1 not assessed"
        )

    def test_nothing_changed(self) -> None:
        assert render_summary_line(_run()) == "no migration files changed"


class TestRunVerdict:
    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (OutcomeStatus.UNSUPPORTED, FileVerdict.REQUIRES_APPROVAL),
            (OutcomeStatus.ERROR, FileVerdict.REQUIRES_APPROVAL),
        ],
    )
    def test_an_unassessed_file_can_never_be_a_pass(
        self, status: OutcomeStatus, expected: FileVerdict
    ) -> None:
        outcome = FileOutcome(
            path="x", framework=Framework.RAILS, source_kind=SourceKind.DSL, status=status
        )
        assert run_verdict((outcome,)) is expected

    def test_an_unassessed_file_can_never_reach_block_either(self) -> None:
        outcome = FileOutcome(
            path="x",
            framework=Framework.GENERIC,
            source_kind=SourceKind.SQL,
            status=OutcomeStatus.ERROR,
        )
        assert run_verdict((outcome,)) is not FileVerdict.BLOCK

    def test_no_files_is_a_pass(self) -> None:
        assert run_verdict(()) is FileVerdict.PROCEED


def _indices(text: str, needle: str) -> list[int]:
    found: list[int] = []
    start = text.find(needle)
    while start != -1:
        found.append(start)
        start = text.find(needle, start + 1)
    return found


class TestOnlineHeader:
    def test_an_online_run_says_what_it_read(self) -> None:
        body = render_comment(
            _run(_outcome("migrations/0001.sql", BLOCKING), online=True)
        )
        header = body[: body.index("### ")]
        assert "live database" in header
        assert "row counts" in header

    def test_the_database_label_appears_but_never_a_host(self) -> None:
        body = render_comment(
            _run(
                _outcome("migrations/0001.sql", BLOCKING),
                online=True,
                database_label="staging-replica",
            )
        )
        assert "`staging-replica` (live)" in body[: body.index("### ")]

    def test_a_degraded_run_says_the_live_check_fell_back(self) -> None:
        body = render_comment(
            _run(
                _outcome("migrations/0001.sql", BLOCKING),
                degraded_reason="live snapshot unavailable (timeout)",
            )
        )
        assert "fell back to offline" in body
        assert "timeout" in body

    def test_the_artifact_is_named_so_the_evidence_can_be_found(self) -> None:
        body = render_comment(
            _run(
                _outcome("migrations/0001.sql", BLOCKING),
                artifact_name="blastoise-reports",
                report_root="/tmp/reports",
            )
        )
        assert "**blastoise-reports**" in body


class TestNothingDetected:
    def test_the_artifact_is_not_named_when_nothing_was_written(self) -> None:
        # Pointing a reader at an artifact that does not exist is worse than
        # pointing at nothing.
        body = render_comment(_run(artifact_name="blastoise-reports"))
        assert "blastoise-reports" not in body

    def test_the_artifact_is_named_when_reports_exist(self) -> None:
        body = render_comment(
            _run(
                _outcome("migrations/0001.sql", BLOCKING),
                artifact_name="blastoise-reports",
                report_root="/tmp/reports",
            )
        )
        assert "**blastoise-reports**" in body

    def test_no_assessment_means_no_claim_about_how_it_was_assessed(self) -> None:
        body = render_comment(_run(artifact_name="blastoise-reports"))
        assert "Assessed" not in body
        assert "No migration files were changed" in body
