# Reports, evidence bundles, signing, and CI

## `blastoise check`

```console
$ blastoise check migrations/0042_backfill.sql                      # offline: parse + catalog only
$ blastoise check migrations/0042_backfill.sql --database-url postgres://ro@db/app
$ blastoise check migrations/0042_backfill.sql --json               # canonical JSON on stdout
$ blastoise check migrations/0042_backfill.sql -o report/           # report.json + evidence bundle
$ blastoise verify report/report.json                               # signature + evidence hashes
$ blastoise explain report/report.json                              # expanded rendering
$ blastoise parse --json migrations/*.sql                           # just the parsed IR
```

`bt` is an alias for `blastoise`. If the database is unreachable, `check`
degrades to offline with a loud warning instead of failing; `--offline`
forces that even when a URL is set.

Exit codes CI can branch on: `0` proceed, `1` requires_approval, `2` block,
`3` tool error. See [tiers.md](tiers.md) for how a file's verdict is derived.

## The Shell Report

The verdict document: the file-level verdict, the count per tier, one
block per statement (tier, duration band, evidence class, rationale, and
the condition under which it is acceptable), an `unverified` list spelling
out everything that could not be checked — offline, that is most of it —
a rollback assessment, and the evidence and signature sections.

## Evidence bundle

`-o DIR` writes `report.json` beside a bundle of the exact inputs: the
migration source, the parsed IR, the catalog rows consulted, the live
snapshot (canonical JSON, deterministic, no floats), and the constants
table. The report carries the sha256 of each file whether or not the
bundle is written, so a report can be checked against the inputs that
produced it.

## Signing (Shell Seal)

Reports are signed with Ed25519 when `--sign-key PATH` or
`$BLASTOISE_SIGNING_KEY` (a path) points at a key file — PEM, or a
64-hex-character seed. The signature covers the canonical JSON of the
payload with the `signature` key absent. Unsigned reports are valid, just
unattested; **no key is ever generated silently**. `blastoise verify`
checks the signature and every evidence hash.
