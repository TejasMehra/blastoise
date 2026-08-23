"""The pull request comment: a Shell Report rendered as Markdown.

The comment is the only part of Blastoise most people will ever read, so
its order is the argument: the file-level verdict first, the per-tier counts
immediately under it, then per file the same two things again, and then --
always -- what was **not** verified.

That last section is not behind a ``<details>`` toggle, and the choice is
deliberate rather than stylistic. The tool's whole claim is that it tells
you what it does not know; a disclosure widget is where a reader's eye
learns to stop. Statement detail is collapsible, because it is an
elaboration of a verdict already stated in the open. The limits of the
verdict are not an elaboration of it.

GitHub caps a comment body at 65536 characters. When a run would exceed
that, sections are dropped in a fixed order and the comment **says which**
-- a truncation nobody is told about reads as "there was nothing more".
"""

from __future__ import annotations

from typing import Any

from blastoise.ci.detect import DSL_ADAPTER_HINT, FRAMEWORK_NAMES, Framework
from blastoise.ci.model import TIER_ORDER, CiRun, FileOutcome, OutcomeStatus
from blastoise.report import FileVerdict
from blastoise.verdict.model import Classification, pressure_level

__all__ = ["COMMENT_MARKER", "GITHUB_COMMENT_LIMIT", "render_comment", "render_summary_line"]

COMMENT_MARKER = "<!-- blastoise-ci-report -->"
"""How the run finds the comment it wrote last time. An HTML comment is
invisible in the rendered body, survives an edit made through the web UI,
and does not depend on the comment's author -- which matters, because the
token that posts is the workflow's, not a stable bot identity."""

GITHUB_COMMENT_LIMIT = 65536

UNVERIFIED_HEADING = "**What this check couldn't establish**"
"""The honest residue, headed as a limit rather than as a score. It is never
collapsed and never counted: see :func:`_unverified_lines`."""

_VERDICT_HEADLINE: dict[FileVerdict, str] = {
    FileVerdict.PROCEED: "PROCEED",
    FileVerdict.REQUIRES_APPROVAL: "REQUIRES APPROVAL",
    FileVerdict.BLOCK: "BLOCK",
}

_VERDICT_ICON: dict[FileVerdict, str] = {
    FileVerdict.PROCEED: "\N{LARGE GREEN CIRCLE}",
    FileVerdict.REQUIRES_APPROVAL: "\N{LARGE YELLOW CIRCLE}",
    FileVerdict.BLOCK: "\N{LARGE RED CIRCLE}",
}

_VERDICT_MEANING: dict[FileVerdict, str] = {
    FileVerdict.PROCEED: "nothing here needs a decision before it runs.",
    FileVerdict.REQUIRES_APPROVAL: (
        "at least one statement needs a timing decision, or could not be "
        "assessed. A human has to look."
    ),
    FileVerdict.BLOCK: "at least one statement should not run as written.",
}


def _text(value: object) -> str:
    """Engine prose, safe to drop into Markdown.

    Rationale strings carry ``->`` and ``<->`` (a type-change explanation
    says ``timestamp<->timestamptz``), and a Markdown renderer that decides
    ``<->`` opens a tag eats the rest of the sentence. Escaping is cheaper
    than finding out which renderer is reading this.
    """
    return str(value).replace("&", "&amp;").replace("<", "&lt;")


def _cell(text: object) -> str:
    """One table cell: pipes and newlines cannot survive in a Markdown row."""
    return _text(text).replace("|", "\\|").replace("\n", " ").strip()


def _plural(count: int, singular: str, plural: str | None = None) -> str:
    return singular if count == 1 else (plural or singular + "s")


def _tier_header() -> tuple[str, str]:
    header = "| " + " | ".join(str(tier) for tier in TIER_ORDER) + " |"
    rule = "|" + "|".join(["---"] * len(TIER_ORDER)) + "|"
    return header, rule


def _tier_row(counts: dict[str, int]) -> str:
    return "| " + " | ".join(str(counts.get(str(tier), 0)) for tier in TIER_ORDER) + " |"


def _tier_legend() -> str:
    return " · ".join(f"`{tier}` {pressure_level(tier)}" for tier in TIER_ORDER)


def _has_finding(outcome: FileOutcome) -> bool:
    """Whether this file has anything in it a reviewer has to look at.

    SAFE_IRREVERSIBLE counts: "there is no undo for this" is a finding even
    though the tier proceeds.
    """
    return any(
        outcome.count(tier) for tier in TIER_ORDER if tier is not Classification.SAFE
    )


def _statement_rows(payload: dict[str, Any]) -> list[str]:
    lines = [
        "| line | statement | tier | duration | evidence | why |",
        "|---|---|---|---|---|---|",
    ]
    for statement in payload.get("statements", []):
        lines.append(
            "| {line} | `{kind}` | `{tier}` | {band} | {method} | {why} |".format(
                line=statement["line"],
                kind=_cell(statement["kind"]),
                tier=_cell(statement["classification"]),
                band=_cell(statement.get("band") or "-"),
                method=_cell(statement["method"]),
                why=_cell(statement["rationale"]),
            )
        )
    return lines


def _unverified_lines(payload: dict[str, Any], limit: int | None) -> list[str]:
    entries = payload.get("unverified", [])
    # No count. These entries are structural -- the execution-time lock queue
    # is unknowable in advance, always -- and a number beside them reads as a
    # tally of failures to anyone who does not already know that.
    lines = [UNVERIFIED_HEADING, ""]
    shown = entries if limit is None else entries[:limit]
    for entry in shown:
        where = f"L{entry['line']} " if entry.get("line") is not None else ""
        lines.append(f"- `{entry['source']}` {where}{_text(entry['reason'])}")
    hidden = len(entries) - len(shown)
    if hidden > 0:
        lines.append(
            f"- _{hidden} further entries were dropped to fit this comment; "
            "all of them are in `report.json` in the workflow artifact._"
        )
    lines.append("")
    return lines


def _file_section(
    outcome: FileOutcome,
    *,
    include_statements: bool,
    unverified_limit: int | None,
) -> list[str]:
    lines: list[str] = []
    if outcome.status is OutcomeStatus.ASSESSED and outcome.payload is not None:
        payload = outcome.payload
        verdict = outcome.effective_verdict
        lines.append(
            f"### {_VERDICT_ICON[verdict]} `{outcome.path}` — {_VERDICT_HEADLINE[verdict]}"
        )
        lines.append("")
        header, rule = _tier_header()
        lines.extend([header, rule, _tier_row(outcome.counts), ""])

        irreversible = payload.get("irreversible", [])
        if irreversible:
            lines.append(f"**Irreversible ({len(irreversible)}).** No undo for:")
            lines.append("")
            for entry in irreversible:
                lines.append(
                    f"- L{entry['line']} `{entry['kind']}`: {_text(entry['what_is_lost'])}"
                )
            lines.append("")

        rollback = payload.get("rollback", {})
        if str(rollback.get("feasible")) == "no":
            lines.append(f"**Rollback: not feasible.** {_text(rollback.get('basis', ''))}")
            lines.append("")

        if include_statements:
            statements = payload.get("statements", [])
            if statements:
                # Open unless every statement is SAFE. The table names the
                # statement, the lock and the reason -- it is the finding,
                # not an elaboration of one, and a finding a reader has to
                # click for is a finding most readers never see. A file with
                # nothing but SAFE in it has no finding to hide, so there it
                # stays collapsed and out of the way.
                interesting = _has_finding(outcome)
                open_attr = " open" if interesting else ""
                summary = (
                    f"{len(statements)} {_plural(len(statements), 'statement')}"
                    if interesting
                    else f"{len(statements)} {_plural(len(statements), 'statement')}, all safe"
                )
                lines.append(f"<details{open_attr}><summary>{summary}</summary>")
                lines.append("")
                lines.extend(_statement_rows(payload))
                lines.append("")
                lines.append("</details>")
                lines.append("")

        # Always open, never in a <details>: see the module docstring.
        lines.extend(_unverified_lines(payload, unverified_limit))
    elif outcome.status is OutcomeStatus.UNSUPPORTED:
        name = FRAMEWORK_NAMES.get(outcome.framework, str(outcome.framework))
        lines.append(f"### \N{LARGE YELLOW CIRCLE} `{outcome.path}` — NOT ASSESSED")
        lines.append("")
        lines.append(
            f"Recognized as a **{name}** migration. Blastoise reads SQL, and "
            "this file is a DSL: the statements it will run do not exist "
            "until the framework renders them, so **this file was not "
            "assessed at all**."
        )
        lines.append("")
        hint = DSL_ADAPTER_HINT.get(outcome.framework)
        if hint:
            lines.append(f"Support would need an extraction step: {hint}.")
            lines.append("")
        lines.append(
            "Until then: any raw SQL this migration executes can be checked "
            "by putting it in a `.sql` file, and the run is held at "
            "`requires_approval` rather than passing."
        )
        lines.append("")
    else:
        lines.append(f"### \N{LARGE YELLOW CIRCLE} `{outcome.path}` — NOT ASSESSED")
        lines.append("")
        lines.append(
            "Blastoise could not produce a verdict for this file: "
            f"{_text(outcome.detail or 'unknown error')}."
        )
        lines.append("")
        lines.append(
            "This is a tool failure, **not** a finding about the migration. "
            "The run is held at `requires_approval` because a migration in "
            "this pull request went unchecked."
        )
        lines.append("")
    return lines


def _header_lines(run: CiRun) -> list[str]:
    verdict = run.verdict
    lines = [
        COMMENT_MARKER,
        f"## {_VERDICT_ICON[verdict]} Blastoise — {_VERDICT_HEADLINE[verdict]}",
        "",
        _VERDICT_MEANING[verdict],
        "",
    ]

    detected = len(run.outcomes)
    files = "migration file" if detected == 1 else "migration files"
    if run.online:
        against = "against the live database"
        if run.database_label:
            against = f"against `{_cell(run.database_label)}` (live)"
        mode = f"Assessed {against}: row counts, existing constraints and current locks were read."
    else:
        mode = (
            "Assessed **offline** — no database was read, so every "
            "size-dependent judgment is a bound, not a measurement. "
            "The same run against a live database resolves most of them."
        )
    changed = f"{run.changed_files} changed {_plural(run.changed_files, 'file')}"
    # With nothing detected there is no assessment to describe the mode of,
    # and "assessed offline" over an all-zero table reads as a finding.
    detail = f" {mode}" if run.outcomes else ""
    lines.append(f"**{detected} {files}** detected in {changed}.{detail}")
    lines.append("")

    if run.degraded_reason:
        lines.append(
            f"> A live check was requested and fell back to offline: {run.degraded_reason}"
        )
        lines.append("")

    # With one assessed file the run total is byte-identical to that file's
    # own table a few lines further down, so it is dropped: a number repeated
    # verbatim reads as two findings on a first scan.
    if len(run.assessed) != 1:
        header, rule = _tier_header()
        lines.extend(
            [
                "Statements by pressure level, across every assessed file:",
                "",
                header,
                rule,
                _tier_row(run.totals()),
                "",
            ]
        )

    unassessed = len(run.unsupported) + len(run.errors)
    if unassessed:
        lines.append(
            f"\N{WARNING SIGN}\N{VARIATION SELECTOR-16} **{unassessed} of {detected} "
            f"detected {_plural(detected, 'migration')} "
            f"{_plural(unassessed, 'was', 'were')} not assessed** and "
            f"{_plural(unassessed, 'is', 'are')} listed below. The counts "
            "above cover only the files that were."
        )
        lines.append("")
    return lines


def _footer_lines(run: CiRun, notes: list[str]) -> list[str]:
    lines = ["---", ""]
    # Only when reports were actually written: pointing a reader at an
    # artifact that does not exist is worse than pointing at nothing.
    if run.artifact_name and run.report_root:
        lines.append(
            f"Full Shell Reports and their evidence bundles (every claim "
            f"traceable to a sha256) are in the **{run.artifact_name}** "
            "workflow artifact."
        )
        lines.append("")
    for note in [*run.notes, *notes]:
        lines.append(f"_{note}_")
        lines.append("")
    # Reference, not argument: it decides nothing, so it sits under the
    # verdict rather than between the reader and it. Only where there is a
    # tier on the page to look up.
    if run.assessed:
        lines.append(f"<sub>{_tier_legend()}</sub>")
        lines.append("")
    lines.append(
        f"<sub>blastoise {run.tool_version} · this comment is updated in "
        "place on every push</sub>"
    )
    return lines


def _assemble(
    run: CiRun,
    *,
    include_statements: bool,
    unverified_limit: int | None,
    notes: list[str],
) -> str:
    lines = _header_lines(run)
    for outcome in run.outcomes:
        lines.extend(
            _file_section(
                outcome,
                include_statements=include_statements,
                unverified_limit=unverified_limit,
            )
        )
    if not run.outcomes:
        lines.append(
            "No migration files were changed in this pull request, so there "
            "was nothing to assess."
        )
        lines.append("")
    lines.extend(_footer_lines(run, notes))
    return "\n".join(lines).rstrip() + "\n"


# Degradation ladder, applied only when the body would not fit. Each step
# says what it dropped; nothing is removed silently.
_LADDER: tuple[tuple[bool, int | None, str | None], ...] = (
    (True, None, None),
    (
        False,
        None,
        "Per-statement detail was omitted to fit this comment; it is in the "
        "workflow artifact.",
    ),
    (
        False,
        20,
        "Per-statement detail and part of the unverified list were omitted "
        "to fit this comment; both are complete in the workflow artifact.",
    ),
    (
        False,
        5,
        "Per-statement detail and most of the unverified list were omitted "
        "to fit this comment; both are complete in the workflow artifact.",
    ),
)


def render_comment(run: CiRun, *, limit: int = GITHUB_COMMENT_LIMIT) -> str:
    """The comment body, degraded as far as it must be to fit ``limit``."""
    body = ""
    for include_statements, unverified_limit, note in _LADDER:
        body = _assemble(
            run,
            include_statements=include_statements,
            unverified_limit=unverified_limit,
            notes=[note] if note else [],
        )
        if len(body) <= limit:
            return body
    # Nothing left to drop that keeps the comment honest: clip, and say so
    # in the last line rather than ending mid-sentence.
    tail = (
        "\n\n---\n\n_This comment was clipped at GitHub's size limit. The "
        "complete reports are in the workflow artifact._\n"
    )
    return body[: max(0, limit - len(tail))].rstrip() + tail


def render_summary_line(run: CiRun) -> str:
    """One line for a check run's title, or a terminal.

    Deliberately not themed: this is read next to other CI checks, where a
    reader is scanning for what failed, not for whose tool it is.
    """
    verdict = _VERDICT_HEADLINE[run.verdict]
    detected = len(run.outcomes)
    if detected == 0:
        return "no migration files changed"
    unassessed = len(run.unsupported) + len(run.errors)
    assessed = len(run.assessed)
    if unassessed:
        return (
            f"{verdict}: {assessed} of {detected} migration "
            f"{_plural(detected, 'file')} assessed, {unassessed} not assessed"
        )
    return f"{verdict}: {assessed} migration {_plural(assessed, 'file')} assessed"


def unsupported_notice(framework: Framework) -> str:
    """The one-line message for a recognized-but-unreadable migration."""
    name = FRAMEWORK_NAMES.get(framework, str(framework))
    return (
        f"{name} migrations are a DSL, not SQL: the statements are generated "
        "at run time, so this file cannot be assessed yet"
    )
