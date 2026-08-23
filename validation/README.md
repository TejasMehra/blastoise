# validation/

The validation harness answers two questions before Blastoise ships to
anyone: **when it says BLOCK, is it right**, and **are its claims
accurate** — per tier, not aggregated, with every miss attributed to the
statement classification and duration constant that produced it.

```console
$ python -m validation.harness corpus                       # validate + weights
$ python -m validation.harness run --smoke                  # 1k/20k rows, ~2 min
$ python -m validation.harness run                          # full: 1k/100k/1M/5M, ~1 h
$ python -m validation.harness score results/<run>.json     # re-label without a database
```

`run` needs Postgres binaries (`--pg-bin` or `BLASTOISE_TEST_PG_BIN`); it
starts a disposable PG 17 server with the scale harness's configuration
(`fsync=on`, `synchronous_commit=on`, autovacuum off, 512 MB shared
buffers).

## Corpus format (`corpus/*.yaml`)

One file per family, weighted by the wild-corpus frequency distribution
recorded in `DECISIONS.md` (ALTER TABLE actions, CREATE INDEX, DML
backfills, foreign keys heaviest; transactions and maintenance lightest).
Each case is:

```yaml
- id: type_varchar_widen_partial_idx_5m
  why: the 13.5 s case - no heap rewrite, but the partial index is rebuilt under AEL
  adversarial: looks_benign          # looks_dangerous | looks_benign | judgment_call | omitted
  source: boundary__0046__01_wh_user_dimension_oidc.up.sql   # wild exemplar
  table: t_5m                        # fixture table bound to {t}: t_1k | t_100k | t_1m | t_5m
  migration: ALTER TABLE {t} ALTER COLUMN status TYPE varchar(64);
  setup: [...]                       # autocommit SQL before the case (becomes "pre-existing")
  teardown: [...]                    # autocommit SQL after
  session: SET TIME ZONE 'America/New_York'   # on the migration session
  mode: rollback                     # rollback (file wrapped in BEGIN...ROLLBACK) | autocommit
  holder: {sql: "SELECT 1 FROM {t} LIMIT 1", hold_s: 25, visible_waiter: true, pre_age_s: 62}
  vacuum_after: true                 # VACUUM (ANALYZE) the table afterwards (DML cases)
  expect: {tier: needs_timing, lock: ACCESS EXCLUSIVE, rewrites: false, error: false, irreversible: false}
```

`expect` is the author's prediction, **not the ground truth**. The runner
measures, derives the truth from the measurement, and reports every
disagreement between the two under "label mismatches" so a fixture that
did not produce the intended scenario is visible instead of silently
scored. For multi-statement files `expect` is a list, one per statement.

**Fixture**: the seeded tables are the scale harness's schema verbatim
(14 columns, PK + 4 secondary indexes including a partial one, a 10k-row
`accounts` FK target, a NOT VALID FK, a valid `CHECK (body IS NOT NULL)`,
a constrained domain) so the calibration probe compares like with like.
Added for the corpus: an enum type with a 1k-row table using it, an
unconstrained domain, a view, two functions, a sequence, an empty
pre-existing table, and a 50k-row child table with no FK.

**Concurrent activity**: `holder` opens a second session, runs its SQL,
and idles in the transaction for `hold_s` seconds counted from the moment
the migration statement starts. `visible_waiter` parks a third session
behind it requesting ACCESS EXCLUSIVE, which is the only shape the
engine's contention escalation can see in `pg_locks`. `pre_age_s` idles
the holder that long *before* the snapshot is captured, so it crosses the
60 s threshold above which the snapshot lists it in `long_transactions`.

A **traffic probe** runs throughout: a point SELECT and a point UPDATE on
the target table every ~40 ms from their own session, recording the worst
latency of each — what a concurrent reader and writer actually
experienced. It is paused during snapshot capture so its own locks never
reach the engine.

## Ground truth (`harness/labeling.py`)

The truth rule consults nothing in the engine except the four threshold
numbers that define the tiers; the lock conflict table and the tier ladder
are restated from the Postgres documentation and `DECISIONS.md`:

1. an error is `unsafe` (the migration aborts as written);
2. the strongest lock held on a relation that **existed before the file
   ran** decides what was blocked — reads (ACCESS EXCLUSIVE), writes
   (SHARE, SHARE ROW EXCLUSIVE, EXCLUSIVE), or nothing; relations the
   file created are ignored; indexes are relations; an index already
   marked `indislive = false` is not (nobody can see it);
3. the normalized hold (lock wait included) is banded on that block
   type's thresholds; DML with no table-level block is banded on the
   write thresholds because every touched row is locked for the run;
4. ACCESS EXCLUSIVE on a pre-existing relation is at least `needs_timing`
   regardless of hold — the tier is defined on the acquisition;
5. a safe result that is irreversible (a declared fact; the one input the
   harness cannot measure) is `safe_irreversible`.

`unknown` is never truth. Transaction-control statements acquire nothing.

Two conventions worth knowing. `mode: rollback` wraps the file in one
transaction; the engine deliberately does not assume a runner does that,
so a multi-statement case whose truth depends on lock accumulation uses
an explicit `BEGIN ... COMMIT` in `autocommit` mode. And the ACCESS
EXCLUSIVE floor in step 4 is the product's own judgment call restated as
a rule — the harness tests that the engine *applies* it exactly where the
measured lock says it should, not whether the call is right.

## Hardware normalization (`harness/calibration.py`)

A tier label banded on a raw millisecond reading depends on the laptop —
the same machine measured twice disagreed with itself by ~1.5x. So the
harness re-runs nine scale-harness statements at the sizes the committed
`artifacts/scale` runs measured, divides today's reading by the
reference's, and takes the median as the **machine factor**. Measured
work is divided by it before banding; lock waits are not (an idle holder
is as long as it is on any hardware). Truth is therefore in
*reference-machine milliseconds*, the units the constants were fitted in.

The probe runs at start and at end so drift during the run is measured.
The report shows the factor against both committed reference runs (they
differ by 1.46x), the labels under raw / global-factor / per-family
factors, and every truth label within 25% of a threshold — the cases
where the hardware, not the engine, decides the outcome.

## Output

`results/<run>.json` holds every record (predicted, measured, truth,
outcome, label mismatches, the calibration readings) and a `summary`;
`results/<run>.txt` is the plain-text report: per-tier precision/recall,
the confusion matrix, UNSAFE in detail, file-level BLOCK precision,
errors broken down by statement kind, by deciding catalog row, by
duration constant, by corpus family and by adversarial class, label
mismatches, boundary cases, and normalization sensitivity.

Outcome vocabulary: `match`; `strict` (engine tier above the truth — a
false alarm); `lenient` (engine below the truth — the dangerous kind);
`unknown` (the engine refused). Precision and recall are computed for
each of the four decided tiers; UNKNOWN predictions are a separate row
and count against the recall of whatever the truth was.
