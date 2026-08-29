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

---

## Rails extraction validation (2026-08-28 Windows, 2026-08-29 Linux)

A different question from the runs above, and a different harness. Those
ask whether the engine's verdicts are right; this asks whether the SQL
Blastoise extracts from a Rails migration is the SQL Rails would actually
run. Behind the "Rails migrations, rendered by running them" section of
`DECISIONS.md`.

| file | what it is |
|---|---|
| `rails_extraction_linux_2026-08-29.json` | **the authoritative run**: Linux, every gem installed, same 40 cases |
| `rails_extraction_linux_2026-08-29.txt` | its report |
| `rails_extraction_2026-08-28.json` | the first run, on Windows without a Ruby devkit; kept because the difference between the two runs is itself a finding |
| `rails_extraction_2026-08-28.txt` | its report |
| `rails_extraction_cases_2026-08-28.json` | the 40 cases, so the selection is inspectable rather than asserted |
| `rails_extraction_mastodon_preload.sql` | `timestamp_id()`, lifted from Mastodon's own `lib/mastodon/snowflake.rb`, supplied to isolate a pre-state failure from an extraction failure |

Method: for each migration, find the commit that added it, take the schema
from that commit's **parent**, and build that pre-state twice. Database A
gets the real migration through the shipped harness; database B gets the
SQL that harness extracted, applied one statement at a time. A and B are
then compared column by column, index by index, constraint by constraint,
sequence by sequence. Migrations added by the same commit that sort earlier
run first on both.

40 migrations, 4 applications, each rendered by **its own** ActiveRecord
(7.2.3.2, 8.0.5, 8.0.5.1, 8.1.3.1) resolved from its `Gemfile.lock`.

| run | machine | verified | wrong SQL | failed |
|---|---|---|---|---|
| 2026-08-28 | Windows, no devkit, no pgvector | 27/40 | 0 | 13 |
| 2026-08-29 | Linux, all gems installed | 28/40 | 0 | 12 |
| 2026-08-29 | Linux, after two harness fixes | **35/40** | **0** | 5 |

The first run blamed eleven failures on the machine. Only one of them was:
running on Linux with the gems installed moved just a single case, which
exposed two real bugs — requiring a DSL gem without calling the `.load` its
Railtie would have called, and not supplying stdlib (`tsort`, `logger`) that
gems assume a booting Rails already required. See `DECISIONS.md`.

All five remaining failures are the same, already-recorded limitation: the
migration needs the booted application (`ApplicationRecord`, `Rails.root`,
`Rails.configuration`), not merely ActiveRecord. Every one fails before
producing SQL and falls back to the honest not-assessed message.

Re-run on Linux: `../scripts/rails_extraction_validation_workflow.yml`. It is
deliberately **not** installed under `.github/workflows/` — it clones four
large third-party applications and runs forty migrations twice each, which
does not belong in the CI of a repository that depends on Blastoise. To use
it, copy it into `.github/workflows/` on a branch, dispatch it from the
Actions tab, take the uploaded artifact, and drop the copy. It starts
Postgres with pgvector, installs each application's own ActiveRecord and DSL
gems, clones the four applications, and uploads the results and report.

Locally:

```
python artifacts/scripts/rails_extraction_validation.py \
  --cases artifacts/validation/rails_extraction_cases_2026-08-28.json \
  --repos <dir of clones> --admin-url <throwaway postgres> \
  --ruby <ruby> --gem-home-map '{"discourse":...}' \
  --workdir <tmp> --out results.json
python artifacts/scripts/rails_validation_report.py --results results.json --out report.txt
```

`--missing-extensions` and `--preload-sql` exist for machines that cannot
supply an extension or a gem; the Linux run needs neither except the
Mastodon preload, which isolates a schema.rb expressiveness gap from an
extraction failure. Where they are used, every removed line is recorded per
case in the JSON, and the same adapted file builds both databases so the
comparison still holds.
