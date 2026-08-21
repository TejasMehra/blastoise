"""Model tests: method combining, severity combining, table completeness."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from pgverdict.ir import AlterTableActionKind, StatementKind
from pgverdict.verdict import (
    CannotEstimate,
    Classification,
    DurationBand,
    DurationEstimate,
    Method,
    Reversibility,
    ReversibilityAssessment,
    Tristate,
    Verdict,
    duration_band,
    weakest_method,
    worse_classification,
)
from pgverdict.verdict.reversibility import (
    action_reversibility,
    combine,
    statement_reversibility,
)


def test_weakest_method_ordering() -> None:
    assert weakest_method(Method.PROVEN, Method.OBSERVED) is Method.OBSERVED
    assert weakest_method(Method.OBSERVED, Method.SIMULATED) is Method.SIMULATED
    assert weakest_method(Method.SIMULATED, Method.UNVERIFIED) is Method.UNVERIFIED
    assert weakest_method(Method.PROVEN) is Method.PROVEN
    assert (
        weakest_method(Method.PROVEN, Method.SIMULATED, Method.OBSERVED)
        is Method.SIMULATED
    )


def test_worse_classification_ordering() -> None:
    """The action ladder: nothing < record < schedule < investigate < stop."""
    ladder = (
        Classification.SAFE,
        Classification.SAFE_IRREVERSIBLE,
        Classification.NEEDS_TIMING,
        Classification.UNKNOWN,
        Classification.UNSAFE,
    )
    for lower_index, lower in enumerate(ladder):
        for higher in ladder[lower_index + 1 :]:
            assert worse_classification(lower, higher) is higher
            assert worse_classification(higher, lower) is higher
    assert (
        worse_classification(Classification.SAFE, Classification.NEEDS_TIMING)
        is Classification.NEEDS_TIMING
    )
    assert (
        worse_classification(Classification.NEEDS_TIMING, Classification.UNKNOWN)
        is Classification.UNKNOWN
    )
    assert (
        worse_classification(Classification.UNKNOWN, Classification.UNSAFE)
        is Classification.UNSAFE
    )


@pytest.mark.parametrize(
    "factory",
    [
        lambda: Verdict(classification=Classification.SAFE, rationale="x"),  # type: ignore[call-arg]
        lambda: CannotEstimate(reason="x"),  # type: ignore[call-arg]
        lambda: Tristate(value=True, basis="x"),  # type: ignore[call-arg]
        lambda: ReversibilityAssessment(  # type: ignore[call-arg]
            reversibility=Reversibility.REVERSIBLE, basis="x"
        ),
    ],
)
def test_conclusions_cannot_be_built_without_a_method(factory: Callable[[], object]) -> None:
    with pytest.raises(TypeError):
        factory()


def test_duration_band_edges() -> None:
    assert duration_band(0) is DurationBand.SUB_SECOND
    assert duration_band(999) is DurationBand.SUB_SECOND
    assert duration_band(1_000) is DurationBand.SECONDS
    assert duration_band(59_999) is DurationBand.SECONDS
    assert duration_band(60_000) is DurationBand.MINUTES
    assert duration_band(3_599_999) is DurationBand.MINUTES
    assert duration_band(3_600_000) is DurationBand.LONG


def test_estimate_band_derives_from_the_upper_bound() -> None:
    estimate = DurationEstimate(
        point_ms=500,
        low_ms=100,
        high_ms=2_500,
        confidence="medium",
        method=Method.SIMULATED,
        inputs=(),
        constant_key=None,
    )
    # The band reads the worst plausible hold, not the point estimate.
    assert estimate.band is DurationBand.SECONDS


def test_reversibility_tables_cover_every_kind() -> None:
    for kind in StatementKind:
        assessment = statement_reversibility(kind)
        if assessment.reversibility is Reversibility.IRREVERSIBLE:
            assert assessment.what_is_lost, f"{kind} irreversible without a loss statement"
    for action in AlterTableActionKind:
        assessment = action_reversibility(action)
        if assessment.reversibility is Reversibility.IRREVERSIBLE:
            assert assessment.what_is_lost, f"{action} irreversible without a loss statement"


def test_reversibility_combining_is_worst_part() -> None:
    reversible = statement_reversibility(StatementKind.CREATE_TABLE)
    unknown = statement_reversibility(StatementKind.CALL)
    irreversible = statement_reversibility(StatementKind.DROP_TABLE)
    assert combine((reversible, unknown)).reversibility is Reversibility.UNKNOWN
    assert (
        combine((reversible, unknown, irreversible)).reversibility
        is Reversibility.IRREVERSIBLE
    )
    assert combine((reversible,)).reversibility is Reversibility.REVERSIBLE
