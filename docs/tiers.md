# The five verdicts (Pressure Levels)

Every statement gets one of five verdicts. They are ordered by **what the
reviewer has to do**, not by how alarming the statement sounds.

| Serialized value | Display name | What you do |
|---|---|---|
| `safe` | Calm Water | nothing |
| `safe_irreversible` | One-Way Current | proceed, but record that there is no undo |
| `needs_timing` | Rain Check | safe in itself, disruptive at the wrong moment: run it off-peak, or behind `lock_timeout` with retries |
| `unsafe` | Hydro Pump | do not run as written |
| `unknown` | Fog | not enough evidence to decide |

The left column is the contract. It is what appears in `--json` output, in
the report, and in exit codes; the display names live in exactly one lookup
(`blastoise.verdict.PRESSURE_LEVELS`) and touch no machine-readable surface.
Someone wiring Blastoise into CI reads `classification: needs_timing` and
never needs to know the display name.

## How a verdict is reached

- **Lock first.** The statement's exact DDL form maps to the lock it takes
  and what that lock blocks (reads, writes, or nothing), from a catalog
  cited against the Postgres docs and source.
- **Then duration.** For work proportional to the table, the live snapshot's
  row count divided by a measured throughput constant gives an estimate, with
  an interval that widens for stale statistics and for how well the constant
  is known. The estimate is scaled to *your* database by a calibration probe
  the snapshot runs (see [how-it-works.md](how-it-works.md)).
- **Then the thresholds.** A full block (reads and writes) is `safe` under
  ~2 s, `needs_timing` under ~20 s, `unsafe` beyond; a write-only block uses
  5 s / 60 s.
- **Any `ACCESS EXCLUSIVE` on a table that already exists is at least
  `needs_timing`**, however short the work: the queue behind the lock
  acquisition does not shrink because the hold is brief.
- **Irreversibility never makes something unsafe.** It only selects
  `safe_irreversible` over `safe`.
- **Refusing to decide.** When the estimate straddles a threshold by less
  than the constant's known hardware spread, the verdict is `unknown` with a
  `refusal: boundary` marker and the two tiers it is refusing between. A
  refused verdict costs a reviewer thirty seconds; a false block costs trust.

## File-level verdict and exit codes

The file's verdict is its worst statement: `proceed` (exit `0`),
`requires_approval` (exit `1`, any `needs_timing` or `unknown`), or `block`
(exit `2`, any `unsafe`). Exit `3` is a tool error.

## Method tags

Every verdict, lock, and duration carries the kind of evidence that produced
it, shown in the report's right-hand column:

| tag | meaning |
|---|---|
| `proven` | follows from the grammar and the lock catalog alone |
| `observed` | read directly from the live snapshot (a constraint exists, a lock is held) |
| `simulated` | produced by the duration model over live statistics |
| `unverified` | asserted without sufficient evidence — offline, or a fact the snapshot could not gather |

A conclusion built from several pieces of evidence is only as strong as its
weakest contributor.
