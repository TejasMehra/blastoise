"""What one CI run produced: an outcome per migration file, and one verdict.

Kept separate from the runner so that the renderers (PR comment, job
summary, JSON output) depend on the shape of the result and not on how it
was obtained -- the same discipline the report layer already follows, where
``render_report`` consumes the payload dict and never an engine object.

The file-level verdicts come from :mod:`blastoise.report`; this layer adds
only the two outcomes a single-file check cannot have: a migration that was
*recognized but not assessed* (a Rails or Django DSL file), and one that
*failed to be assessed* (unreadable, unparseable). Both are worse than
nothing being wrong, and neither means the migration is dangerous -- so both
raise the run to ``requires_approval`` and neither can reach ``block``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from blastoise.ci.detect import DetectedFile, Framework, SourceKind
from blastoise.report import FileVerdict
from blastoise.verdict.model import Classification

__all__ = [
    "TIER_ORDER",
    "CiRun",
    "FileOutcome",
    "OutcomeStatus",
    "run_verdict",
]

# Worst first, matching the report renderer: a reader scans from the top and
# stops at the first zero.
TIER_ORDER: tuple[Classification, ...] = (
    Classification.UNSAFE,
    Classification.UNKNOWN,
    Classification.NEEDS_TIMING,
    Classification.SAFE_IRREVERSIBLE,
    Classification.SAFE,
)

_VERDICT_RANK: dict[FileVerdict, int] = {
    FileVerdict.PROCEED: 0,
    FileVerdict.REQUIRES_APPROVAL: 1,
    FileVerdict.BLOCK: 2,
}


class OutcomeStatus(StrEnum):
    """What happened to one detected migration file."""

    ASSESSED = "assessed"
    UNSUPPORTED = "unsupported"
    """Recognized as a migration, but written in a DSL the parser cannot
    read. Not a failure of this file -- a limit of the tool, stated."""

    ERROR = "error"
    """Could not be assessed: unreadable, or not parseable as SQL. Never
    reported as a dangerous migration; no verdict was produced at all."""


@dataclass(frozen=True, slots=True)
class FileOutcome:
    """One detected migration file and what came of it."""

    path: str
    framework: Framework
    source_kind: SourceKind
    status: OutcomeStatus
    verdict: FileVerdict | None = None
    counts: dict[str, int] = field(default_factory=dict)
    payload: dict[str, Any] | None = None
    report_dir: str | None = None
    detail: str | None = None
    """Why, for UNSUPPORTED and ERROR. Already redacted when it is set."""

    @classmethod
    def of(
        cls,
        detected: DetectedFile,
        status: OutcomeStatus,
        **kwargs: Any,
    ) -> FileOutcome:
        return cls(
            path=detected.path,
            framework=detected.framework,
            source_kind=detected.source_kind,
            status=status,
            **kwargs,
        )

    @property
    def effective_verdict(self) -> FileVerdict:
        """The verdict this file contributes to the run.

        A file that was not assessed contributes ``requires_approval``: the
        pull request contains a migration nobody looked at, which is exactly
        the case a green check would misreport.
        """
        if self.status is OutcomeStatus.ASSESSED and self.verdict is not None:
            return self.verdict
        return FileVerdict.REQUIRES_APPROVAL

    def count(self, tier: Classification) -> int:
        return self.counts.get(str(tier), 0)


def run_verdict(outcomes: tuple[FileOutcome, ...]) -> FileVerdict:
    """The run's verdict: the worst of its files.

    No files detected is ``proceed`` -- the same reasoning as an empty
    migration file, which the report layer already treats this way.
    """
    verdict = FileVerdict.PROCEED
    for outcome in outcomes:
        candidate = outcome.effective_verdict
        if _VERDICT_RANK[candidate] > _VERDICT_RANK[verdict]:
            verdict = candidate
    return verdict


@dataclass(frozen=True, slots=True)
class CiRun:
    """Everything one ``blastoise ci`` invocation concluded."""

    outcomes: tuple[FileOutcome, ...]
    online: bool
    tool_version: str
    changed_files: int
    database_label: str | None = None
    degraded_reason: str | None = None
    """Set when a live check was asked for and fell back to offline. Already
    redacted."""

    notes: tuple[str, ...] = ()
    report_root: str | None = None
    artifact_name: str | None = None

    @property
    def verdict(self) -> FileVerdict:
        return run_verdict(self.outcomes)

    @property
    def assessed(self) -> tuple[FileOutcome, ...]:
        return tuple(o for o in self.outcomes if o.status is OutcomeStatus.ASSESSED)

    @property
    def unsupported(self) -> tuple[FileOutcome, ...]:
        return tuple(o for o in self.outcomes if o.status is OutcomeStatus.UNSUPPORTED)

    @property
    def errors(self) -> tuple[FileOutcome, ...]:
        return tuple(o for o in self.outcomes if o.status is OutcomeStatus.ERROR)

    def totals(self) -> dict[str, int]:
        """Per-tier statement counts summed over every assessed file."""
        totals = {str(tier): 0 for tier in TIER_ORDER}
        for outcome in self.assessed:
            for tier, count in outcome.counts.items():
                totals[tier] = totals.get(tier, 0) + count
        return totals

    def summary(self) -> dict[str, Any]:
        """The machine-readable account of the run.

        Plain names throughout, like every other machine surface: something
        consuming this reads ``verdict: block`` and never needs to know what
        a Hydro Pump is.
        """
        return {
            "verdict": str(self.verdict),
            "online": self.online,
            "tool_version": self.tool_version,
            "changed_files": self.changed_files,
            "migrations_detected": len(self.outcomes),
            "assessed": len(self.assessed),
            "unsupported": len(self.unsupported),
            "errors": len(self.errors),
            "classification_counts": self.totals(),
            "database_label": self.database_label,
            "degraded_reason": self.degraded_reason,
            "report_root": self.report_root,
            "files": [
                {
                    "path": outcome.path,
                    "framework": str(outcome.framework),
                    "source_kind": str(outcome.source_kind),
                    "status": str(outcome.status),
                    "verdict": None if outcome.verdict is None else str(outcome.verdict),
                    "classification_counts": dict(outcome.counts),
                    "report_dir": outcome.report_dir,
                    "detail": outcome.detail,
                }
                for outcome in self.outcomes
            ],
            "notes": list(self.notes),
        }
