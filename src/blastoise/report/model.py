"""The verdict document's file-level contract: verdict values and exit codes.

The document itself is built as a plain JSON payload (``build.py``) because
its whole purpose is to be serialized, hashed, and signed; this module holds
the pieces of it that other code compares against — the file-level verdict
enum, its derivation from statement classifications, and the CLI exit codes
CI depends on. All values here are machine contract: plain, unthemed, and
pinned by tests.
"""

from __future__ import annotations

from enum import StrEnum, auto

from blastoise.verdict.model import Classification, worse_classification

SCHEMA_VERSION = 1
"""Version of the verdict document schema. Bumped on any breaking change to
the payload's keys or their meaning; consumers should refuse versions they
do not know."""

REPORT_FILENAME = "report.json"
BUNDLE_DIRNAME = "evidence"


class FileVerdict(StrEnum):
    """The file-level verdict, derived from the worst statement tier.

    ``PROCEED`` — every statement is in a safe tier (SAFE or
    SAFE_IRREVERSIBLE): nothing needs a human decision.
    ``REQUIRES_APPROVAL`` — at least one statement needs a timing decision
    or could not be assessed (NEEDS_TIMING or UNKNOWN): a human must look.
    ``BLOCK`` — at least one statement is UNSAFE: do not run as written.
    """

    PROCEED = auto()
    REQUIRES_APPROVAL = auto()
    BLOCK = auto()


_VERDICT_OF_CLASSIFICATION: dict[Classification, FileVerdict] = {
    Classification.SAFE: FileVerdict.PROCEED,
    Classification.SAFE_IRREVERSIBLE: FileVerdict.PROCEED,
    Classification.NEEDS_TIMING: FileVerdict.REQUIRES_APPROVAL,
    Classification.UNKNOWN: FileVerdict.REQUIRES_APPROVAL,
    Classification.UNSAFE: FileVerdict.BLOCK,
}


def file_verdict(classifications: tuple[Classification, ...]) -> FileVerdict:
    """The file-level verdict: derived from the worst per-statement tier.

    An empty file has nothing to object to and is PROCEED.
    """
    if not classifications:
        return FileVerdict.PROCEED
    worst = classifications[0]
    for classification in classifications[1:]:
        worst = worse_classification(worst, classification)
    return _VERDICT_OF_CLASSIFICATION[worst]


# CLI exit codes. CI depends on these being distinct; they are pinned by
# tests and must never be renumbered.
EXIT_CODES: dict[FileVerdict, int] = {
    FileVerdict.PROCEED: 0,
    FileVerdict.REQUIRES_APPROVAL: 1,
    FileVerdict.BLOCK: 2,
}
EXIT_TOOL_ERROR = 3
"""Anything that prevented a verdict from being produced at all: unreadable
or unparseable input, bad usage, a broken key file. Distinct from BLOCK —
a 3 means 'the tool failed', never 'the migration is dangerous'."""


def exit_code(verdict: FileVerdict) -> int:
    return EXIT_CODES[verdict]
