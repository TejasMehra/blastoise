"""The risk engine: per-statement production-safety assessments.

Its output tiers are the **Pressure Levels** and the document built from
them is the **Shell Report**; both names are display-only, and
:data:`PRESSURE_LEVELS` is the single lookup that carries them.

``assess_script(script, catalog, pg_version, snapshot=None)`` combines the
parsed IR, the lock semantics catalog, and (optionally) a live snapshot
into per-statement verdicts. ``snapshot_probes(script)`` derives what to
ask ``capture_snapshot`` for. Everything the engine emits carries a
:class:`Method` evidence tag; UNKNOWN is a first-class outcome, not a
failure. No LLM is involved anywhere in this path.
"""

from blastoise.verdict.constants import DURATION_CONSTANTS, ConstantUnit, DurationConstant
from blastoise.verdict.engine import assess_script
from blastoise.verdict.model import (
    PRESSURE_LEVELS,
    SAFE_TIERS,
    CannotEstimate,
    Classification,
    ContentionAssessment,
    DurationBand,
    DurationEstimate,
    HeldLock,
    Method,
    RelationLockAssessment,
    Reversibility,
    ReversibilityAssessment,
    RowAssessment,
    ScriptAssessment,
    SnapshotProbes,
    StatementAssessment,
    TransactionWarning,
    Tristate,
    Verdict,
    duration_band,
    pressure_level,
    weakest_method,
    worse_classification,
)
from blastoise.verdict.probes import snapshot_probes

__all__ = [
    "DURATION_CONSTANTS",
    "PRESSURE_LEVELS",
    "SAFE_TIERS",
    "CannotEstimate",
    "Classification",
    "ConstantUnit",
    "ContentionAssessment",
    "DurationBand",
    "DurationConstant",
    "DurationEstimate",
    "HeldLock",
    "Method",
    "RelationLockAssessment",
    "Reversibility",
    "ReversibilityAssessment",
    "RowAssessment",
    "ScriptAssessment",
    "SnapshotProbes",
    "StatementAssessment",
    "TransactionWarning",
    "Tristate",
    "Verdict",
    "assess_script",
    "duration_band",
    "pressure_level",
    "snapshot_probes",
    "weakest_method",
    "worse_classification",
]
