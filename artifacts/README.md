# artifacts/

Measurement evidence for the claims in `DECISIONS.md`, committed because it
is measurement rather than working notes. Three previous sessions kept these
in a session scratchpad; one of them did not survive, and a post-fix harness
run had to be paid for a second time. Prompt 8's calibration loop reads the
scale-harness numbers directly, so they live in the repo now.

Everything here is data plus the scripts that produced it. Nothing in
`src/` imports from this directory.

## What is not here

The **corpus SQL itself** — 3,081 migration files harvested from 15
open-source projects — is not vendored. `corpus/manifests/` maps every
harvested filename back to its upstream repository and path, and (added
2026-08-21) carries `sha256` and `bytes` of the exact file that was
measured, so a re-harvest can be *verified* rather than assumed. Upstream
files move and get rewritten; a repo path alone does not pin which bytes
produced these numbers.

The **PostgreSQL binaries** (zonky embedded PG 17.10, ~120 MB) are not here
either. Re-download from Maven Central; the live tests and both harnesses
take the bin directory via `PGVERDICT_TEST_PG_BIN` / `scratch/pg/bin`.

## corpus/

Every file records **one row per statement** — `(file, statement_index,
kind, tier, band, method)` — not just totals. That is deliberate: totals can
cancel two opposite errors out, and the safety checks ("did any statement
leave UNSAFE for a safe tier") are only meaningful per statement.

| file | engine generation | run |
|---|---|---|
| `offline_pre_tier_split.json` | 4 tiers, `CONDITIONALLY_SAFE` | offline, no snapshot |
| `offline_pre_ael_floor.json` | 5 tiers | offline |
| `offline_current.json` | 5 tiers + ACCESS EXCLUSIVE floor | offline |
| `online_pre_tier_split.json` | 4 tiers | replay against PG 17.10 |
| `online_pre_ael_floor.json` | 5 tiers | replay |
| `online_current.json` | 5 tiers + AEL floor | replay |

`offline_pre_ael_floor.json` and `offline_current.json` are **byte
identical** — that is the finding, not an oversight: offline the floor
changes nothing, because every statement it could reach is either UNKNOWN
for want of a snapshot or exempt as file-created. Both are kept so
`compare_tiers.py` runs against them directly.

The offline runs are deterministic. The **online runs are not**: they
replay each repo's migration chain into a fresh database and assess each
file against the database as it stood before that file, so a handful of
statements differ run to run (broken chains apply differently). Compare
online runs expecting a few statements of noise; the offline pair is the
exact comparison.

- `unknown_diagnosis.json` — every online UNKNOWN attributed to a cause,
  with a second assessment after `ANALYZE` so the statistics-freshness
  share is measured rather than argued. The conclusion behind
  `DECISIONS.md`: ~97% of the online UNKNOWN rate is replay artifact and
  the engine's honest UNKNOWN floor is ~1% of statements.
- `exemplars.json` — the wild statements the scale-harness cases were
  modeled on, so each synthetic case can be traced to real migration code.

## scale/

The measured runs: real lock modes from `pg_locks`, real wall-clock holds,
and `relfilenode` rewrite ground truth, at 1k / 100k / 1M / 10M rows on
PG 17.10 (`fsync=on`, `synchronous_commit=on`, autovacuum off).

| file | what it is |
|---|---|
| `results_pre_severity_fix_33case.json` | before the dependent-index-rebuild fix and the measured constants; the run that *found* the six dangerous misses |
| `results_pre_ael_floor_34case.json` | after those fixes and the tier split; 34 cases, the fifth seeded index |
| `results_current_34case.json` | after the ACCESS EXCLUSIVE floor |
| `predictions_pre_tier_split.json` | the 4-tier engine re-assessing the *same* pickled snapshots |
| `predictions_pre_ael_floor.json` | the pre-floor engine re-assessing the same pickled snapshots |

The `predictions_*` files are the controlled comparison. Re-running the
harness re-measures, and measurement varies run to run; re-assessing the
identical pickled `LiveSnapshot` objects with a reconstructed older engine
does not. Any difference there is the engine change and nothing else.

Caveat that applies to every number in `scale/`, unchanged since the first
run: one uncontended NVMe laptop, single session, zero concurrency. The
*shapes* transfer (bimodal index builds, join-not-probe FK validation); the
absolute rates only bound production from above. `MEASURED` is not the
fitted cross-environment calibration prompt 8 still owes.

## scripts/

These expect a working directory holding `corpus/` (the harvested SQL),
`pg/bin` (the server binaries), and any reconstructed engine trees. Copy
them there; they resolve their inputs relative to their own location.

Corpus:
- `corpus_offline.py` — offline pass, summary report.
- `corpus_tiers.py <tree> <out.json>` — offline pass emitting per-statement
  rows. `<tree>` is `new` (the working tree) or the name of a reconstructed
  tree in the working directory.
- `corpus_replay.py <tree> <out.json>` — the online replay.
- `compare_tiers.py <old.json> <new.json>` — per-statement movement matrix
  plus the "nothing left UNSAFE for a safe tier" check.
- `diagnose_unknown.py` — the instrumented replay behind `unknown_diagnosis.json`.
- `find_exemplars.py`, `inspect_moves.py`, `manifest_hashes.py` — supporting.

Scale:
- `scale_harness.py` — the measured run (`--smoke` for a short one;
  `PGVERDICT_SCALE_SIZES` restricts sizes). Pickles every `LiveSnapshot`
  it fed the engine into `snapshots/`.
- `reassess_baseline.py <tree> [out.json]` — replays those pickles through
  a reconstructed engine.
- `compare_scale.py [results.json] [predictions.json]` — per-case tier
  movement, the UNSAFE check, and the monotonic-with-size check.
- `analyze_scale.py`, `compare_runs.py` — interval coverage and run-to-run
  diffs.
- `check_boundary_crossings.py [results.json]` — re-bands every interval
  miss to ask whether the *tier* would have differed, and in which
  direction. Crossings where the engine is stricter than the measurement
  are acceptable; crossings where it is more lenient must be zero.
- `reuse_experiment.py` — the `relfilenode` experiment that produced the
  index-reuse rule (`CheckIndexCompatible`) behind the no-rewrite narrowing.

Reconstruction (this is how old-vs-new claims are made honest):
- `make_baseline.py` — reverses every edit of the tier restructure.
- `make_baseline_ael.py` — reverses the ACCESS EXCLUSIVE floor's one
  wiring line.

Both assert that each reversal matched **exactly once**, so the
reconstructed engine is exact rather than approximate. `make_baseline.py`
validates itself further: offline it reproduces the recorded 2026-08-21
distribution to the statement.
