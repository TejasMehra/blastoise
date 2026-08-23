# artifacts/validation/

The validation harness's committed results — the run behind the
"Validation harness" section of `DECISIONS.md`. The harness itself lives
in `validation/` (code) and `validation/corpus/` (the 172-case corpus);
this directory holds one run's output so the numbers in `DECISIONS.md` are
reproducible rather than asserted.

Three runs are committed, one per `DECISIONS.md` section:

**First run** (behind "The validation harness", UNSAFE precision 70%):

| file | what it is |
|---|---|
| `results_2026-08-22.json` | every case: predicted, measured (locks, hold, rewrite, traffic stalls), ground-truth label, outcome, and the full calibration series |
| `report_2026-08-22.txt` | the plain-text report: per-tier precision/recall, confusion, UNSAFE detail, file-level BLOCK, error breakdowns, boundary and normalization sections |
| `digest_2026-08-22.txt` | every disagreement with the engine's predicted upper bound beside the measured hold, plus the concurrency and traffic-probe detail |

**Re-measured run** (behind "Fixing the measurements behind the false
BLOCKs": `validation_scan` measured, `heap_rewrite` split into plain vs
compute-per-row with a variance-tied band, idle/active contention). Same
corpus, same labeling rule, no tuning:

| file | what it is |
|---|---|
| `results_2026-08-22_remeasured.json` | the re-run's per-case records and calibration series |
| `report_2026-08-22_remeasured.txt` | its plain-text report (UNSAFE precision 69.2%) |
| `digest_2026-08-22_remeasured.txt` | its disagreements, concurrency, and traffic detail |

The headline UNSAFE precision barely moved (70% → 69.2%) because this
second run's hardware normalization flipped a *different* subset of the
~6 boundary cases that sit within 25% of the outage thresholds — the
finding, not a regression. The controlled comparison isolates the engine
change from that measurement noise: re-scoring the **first run's** measured
holds with the new engine gives **UNSAFE precision 100%** (all six named
false BLOCKs gone), and the new engine beats the old on the second run's
own holds too (55% → 69.2%, nine false BLOCKs → four). See the DECISIONS
section for both tables.

**Hardware-as-input run** (behind "Replicated measurement, the target's
hardware as an input, and refusing at the boundary": snapshot format 5
carries a calibration probe, every estimate is scaled to the target, the
constants are re-anchored to `../profiles/laptop-nvme.json`, and verdicts
whose estimate straddles a threshold inside the constant's known spread
are refused as UNKNOWN). Same corpus, same labeling rule, same thresholds:

| file | what it is |
|---|---|
| `results_2026-08-23_hardware.json` | the run's per-case records, each with the snapshot's probe reading and the verdict's refusal fields |
| `results_2026-08-23_hardware.txt` | its plain-text report, with the new HARDWARE PROBE and BOUNDARY REFUSALS sections |
| `digest_2026-08-23_hardware.txt` | its disagreements, concurrency, and traffic detail |

Truth basis changed with the engine: because the estimate is now scaled
to the machine the case ran on, the comparable truth is the **raw** hold,
not the reference-normalized one (the normalized label is still computed
and reported as `tier_normalized`). The per-tier table therefore reads
against raw holds for every case whose snapshot carried a probe reading.

Re-run: `python -m validation.harness run` (needs `BLASTOISE_TEST_PG_BIN`).
The run is ~40 min and its ground-truth tiers are hardware-normalized
against `../scale/`, so a different machine reproduces the *tiers* and the
*disagreements*, not the absolute milliseconds. Re-score an existing JSON
without a database: `python -m validation.harness score <json>`.

Caveat carried from `../scale/`: one uncontended laptop. The harness
measures ceilings; production storage and contention only slow things
down, which the ground-truth labeling absorbs into the upper side. The
calibration series in the JSON shows this machine moving 0.75x -> 0.80x
over the run (0.41x-0.80x across probe passes) against the reference — a
single factor would misplace the boundary cases, which is why each case is
labeled with the factor interpolated at the time it ran.
