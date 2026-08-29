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
    UNVERIFIED_HEADING,
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
SAFE = "CREATE TABLE t (id int);\n"
NEEDS_TIMING = "ALTER TABLE events ADD COLUMN plan_flag text;\n"


class TestWhatLeads:
    def test_the_marker_is_first_so_the_comment_can_be_found_again(self) -> None:
        body = render_comment(_run(_outcome("migrations/0001.sql", BLOCKING)))
        assert body.startswith(COMMENT_MARKER)

    def test_the_verdict_is_in_the_heading(self) -> None:
        body = render_comment(_run(_outcome("migrations/0001.sql", BLOCKING)))
        heading = body.splitlines()[1]
        assert heading.startswith("## ")
        assert "BLOCK" in heading

    def test_the_run_total_precedes_the_file_sections(self) -> None:
        # Two files: the run total is genuinely informative and leads.
        body = render_comment(
            _run(
                _outcome("migrations/0001.sql", BLOCKING),
                _outcome("migrations/0002.sql", SAFE),
            )
        )
        assert body.index("| unsafe | unknown | needs_timing |") < body.index("### ")

    def test_one_file_does_not_repeat_its_own_counts_as_a_run_total(self) -> None:
        # The run total would be byte-identical to the file's own table, and
        # a number repeated verbatim reads as two findings on a first scan.
        body = render_comment(_run(_outcome("migrations/0001.sql", BLOCKING)))
        assert body.count("| unsafe | unknown | needs_timing |") == 1
        assert body.index("### ") < body.index("| unsafe | unknown | needs_timing |")

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
        assert UNVERIFIED_HEADING in body

    def test_it_is_not_inside_a_details_block(self) -> None:
        body = render_comment(_run(_outcome("migrations/0001.sql", BLOCKING)))
        for start in _indices(body, UNVERIFIED_HEADING):
            before = body[:start]
            # "<details" not "<details>": the statements block is now
            # "<details open>", and matching the closed form only would let
            # this assertion pass while the section was in fact nested.
            assert before.count("<details") == before.count("</details>"), (
                "the unverified section starts inside an open <details> block"
            )

    def test_every_entry_is_rendered(self) -> None:
        outcome = _outcome("migrations/0001.sql", BLOCKING)
        assert outcome.payload is not None
        entries = outcome.payload["unverified"]
        body = render_comment(_run(outcome))
        section = body[body.index(UNVERIFIED_HEADING) :]
        for entry in entries:
            assert f"`{entry['source']}`" in section

    def test_it_follows_the_statement_detail_rather_than_nesting_in_it(self) -> None:
        body = render_comment(_run(_outcome("migrations/0001.sql", BLOCKING)))
        details_at = body.index("<details")
        assert body.index(UNVERIFIED_HEADING) > body.index("</details>", details_at)


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
            path="app/migrations/0001_initial.py",
            framework=Framework.DJANGO,
            source_kind=SourceKind.DSL,
            status=OutcomeStatus.UNSUPPORTED,
            detail="Django migrations are a DSL",
        )
        body = render_comment(_run(outcome))
        assert "NOT ASSESSED" in body
        assert "**Django**" in body
        assert "sqlmigrate" in body
        assert "recognized" in body
        assert "does not support extracting SQL" in body
        assert "not assessed at all" in body
        # Whether anyone wants a Django adapter is a question, not an
        # assumption: the comment is where it gets asked.
        assert "issues/new" in body

    def test_a_rails_file_says_the_adapter_exists_and_why_it_did_not_run(self) -> None:
        # Rails is renderable, so telling a reader "not supported" would
        # send them to ask for something they already have. What they need
        # instead is the reason this particular run did not render it.
        outcome = FileOutcome(
            path="db/migrate/001_add.rb",
            framework=Framework.RAILS,
            source_kind=SourceKind.DSL,
            status=OutcomeStatus.UNSUPPORTED,
            detail="no ruby on PATH",
        )
        body = render_comment(_run(outcome))
        assert "NOT ASSESSED" in body
        assert "**Rails**" in body
        assert "can render this by running it" in body
        assert "no ruby on PATH" in body
        assert "does not support extracting SQL" not in body

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
        section = body[body.index(UNVERIFIED_HEADING) :]
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
        assert UNVERIFIED_HEADING in body

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


class TestStatementsAreOpenWhenThereIsAFinding:
    """The statement table IS the finding; a click is where readers stop."""

    def test_a_needs_timing_file_opens_its_statements(self) -> None:
        body = render_comment(_run(_outcome("migrations/0001.sql", NEEDS_TIMING)))
        assert "<details open>" in body
        assert "<details><summary>" not in body

    def test_an_unsafe_file_opens_its_statements(self) -> None:
        body = render_comment(_run(_outcome("migrations/0001.sql", BLOCKING)))
        assert "<details open>" in body

    def test_an_irreversible_file_opens_its_statements(self) -> None:
        # "there is no undo for this" is a finding even though it proceeds.
        body = render_comment(_run(_outcome("migrations/0001.sql", DROPPING)))
        assert "<details open>" in body

    def test_an_entirely_safe_file_stays_collapsed(self) -> None:
        body = render_comment(_run(_outcome("migrations/0001.sql", SAFE)))
        assert "<details open>" not in body
        assert "<details>" in body
        assert "all safe" in body

    def test_a_mixed_run_opens_only_the_file_with_the_finding(self) -> None:
        body = render_comment(
            _run(
                _outcome("migrations/0001.sql", SAFE),
                _outcome("migrations/0002.sql", NEEDS_TIMING),
            )
        )
        assert body.count("<details open>") == 1
        assert body.count("<details>") == 1
        safe_section = body[body.index("migrations/0001.sql") : body.index("migrations/0002.sql")]
        assert "<details open>" not in safe_section


class TestUnverifiedHeading:
    def test_it_is_not_counted(self) -> None:
        # A number beside structural limits reads as a tally of failures.
        body = render_comment(_run(_outcome("migrations/0001.sql", NEEDS_TIMING)))
        assert UNVERIFIED_HEADING in body
        assert "Not verified" not in body
        assert "**What this check couldn't establish**" in body

    def test_the_entries_are_still_all_there(self) -> None:
        outcome = _outcome("migrations/0001.sql", NEEDS_TIMING)
        assert outcome.payload is not None
        body = render_comment(_run(outcome))
        section = body[body.index(UNVERIFIED_HEADING) :]
        for entry in outcome.payload["unverified"]:
            assert f"`{entry['source']}`" in section


class TestLegendPlacement:
    def test_it_appears_once(self) -> None:
        body = render_comment(
            _run(
                _outcome("migrations/0001.sql", NEEDS_TIMING),
                _outcome("migrations/0002.sql", SAFE),
            )
        )
        assert body.count("Hydro Pump") == 1

    def test_it_sits_below_the_verdict_not_between_it_and_the_reader(self) -> None:
        body = render_comment(_run(_outcome("migrations/0001.sql", NEEDS_TIMING)))
        assert body.index("Hydro Pump") > body.index(UNVERIFIED_HEADING)

    def test_nothing_assessed_means_no_legend_to_look_anything_up_in(self) -> None:
        assert "Hydro Pump" not in render_comment(_run())
