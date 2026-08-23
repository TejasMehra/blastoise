"""The calibration probe: what the target's hardware does with a fixed unit of work.

The duration constants are rates measured on particular machines. Left
alone, every estimate silently assumes the target database performs like
the machine the constants were measured on — a migration on a two-vCPU
burstable instance and one on a sixteen-vCPU box got the same number.
The probe makes the target's hardware an input: the snapshot runs a
bounded, read-only operation of known cost on the target and records how
long it took; the duration model scales every estimate by the ratio of
that reading to the reading on the anchor profile the constants are
expressed at.

The probe is a sort of a generated series: ``generate_series`` plus
``ORDER BY`` a scrambled key over a fixed row count. Pure backend CPU plus
``work_mem`` sort machinery — what an index build, a rewrite's per-row
work, and a validation scan's predicate spend their time on. It touches
no table, takes no lock, and reads no user data, so it runs under the
documented minimum-privilege role (``docs/minimum-privilege-role.md``
promises Hydro Scan never queries user tables, and that promise holds).
Its cost is fixed by construction and bounded by the snapshot's
``statement_timeout`` regardless.

What it cannot see is the storage path: a rewrite's sequential write and
fsync, a validation scan's heap read on a cold cache. A disk probe that
respects the privilege contract does not exist — every read of real
heap pages needs ``SELECT`` on a user table — so the residual those leave
across hardware profiles is measured by ``measure_profiles.py`` (which
runs as a superuser and can read the heap) and carried by the
constants as their cross-profile spread. That spread is what the
boundary-proximity rule refuses inside; the probe narrows the estimate,
the spread says how far the probe falls short.

The probe runs ``PROBE_REPEATS`` times and the **minimum** is recorded:
the minimum is the machine's capability, the spread above it is noise
from whatever else the server was doing, and a capability is what a rate
constant is expressed against. The repeats cost well under a second on
any machine measured.

``REFERENCE_COMPUTE_MS`` is the anchor profile's reading from the
cross-profile measurement; the constants table in
:mod:`blastoise.verdict.constants` is expressed at the same anchor. The
scaling lives in :func:`hardware_factor_tenths`.
"""

from __future__ import annotations

from blastoise.live.model import CalibrationFacts

PROBE_REPEATS = 3

COMPUTE_PROBE_ROWS = 500_000
COMPUTE_PROBE_SQL = (
    "SELECT count(*) FROM (SELECT g FROM generate_series(1, %(n)s) g "
    "ORDER BY (g::bigint * 7919) %% 1000003) s"
)
"""A 500k-row sort. ``7919 * g mod 1000003`` scrambles the series so the
sort does real work rather than detecting presorted input."""

# Anchor reading: the anchor profile's minimum-of-three probe, median over
# the measurement's probe passes. The constants in
# ``blastoise.verdict.constants`` are expressed at this anchor. A target
# whose probe reads 2x this is modeled as 2x slower.
REFERENCE_COMPUTE_MS = 215
"""laptop-nvme, 2026-08-23 (``artifacts/profiles/laptop-nvme.json``)."""

# The probe's own floor: below this a reading is clock granularity, not
# hardware, and the ratio would be noise. Readings at or under the floor
# clamp to it.
PROBE_FLOOR_MS = 2

# The factor is clamped to this range: a target more than 8x slower or
# ~3x faster than the anchor is outside every measured profile, and the
# spread the band carries was not observed there. The clamp is recorded
# in the scaling note so the reader sees it.
FACTOR_MIN_TENTHS = 3
FACTOR_MAX_TENTHS = 80


def _clamp_factor(tenths: int) -> int:
    return max(FACTOR_MIN_TENTHS, min(FACTOR_MAX_TENTHS, tenths))


def hardware_factor_tenths(calibration: CalibrationFacts | None) -> tuple[int, str]:
    """How much slower (>10) or faster (<10) the target is than the anchor, in tenths.

    Returns ``(factor_tenths, note)``. ``10`` means "as the anchor"; a
    factor of ``25`` multiplies every duration estimate by 2.5. The note
    states the reading and the anchor so the estimate's inputs carry the
    scaling in the open.

    With no calibration (an older snapshot, a probe that timed out) the
    factor is ``10`` and the note says the estimate is unscaled — the
    same assumption the model always silently made, now stated.
    """
    if calibration is None:
        return 10, "hardware: unscaled (snapshot carries no calibration probe)"
    compute = calibration.compute_ms
    if not (compute.available and isinstance(compute.value, int)):
        return 10, f"hardware: unscaled (calibration probe unavailable: {compute.reason})"
    reading = max(PROBE_FLOOR_MS, compute.value)
    raw = round(10 * reading / REFERENCE_COMPUTE_MS)
    tenths = _clamp_factor(raw)
    clamped = "" if tenths == raw else " (clamped)"
    return tenths, (
        f"hardware: compute probe {compute.value} ms vs anchor {REFERENCE_COMPUTE_MS} ms "
        f"-> x{tenths / 10:.1f}{clamped}"
    )
