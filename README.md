# Blastoise

**Know the blast radius before you migrate.**

Blastoise reads your migration and tells you what it will actually do to
production: which locks it takes, what it blocks, and for how long, checked
against your live database rather than just the SQL. When it can't be sure, it
says so instead of guessing.

> **NOTICE — Blastoise is a working codename.** The Pokémon reference is
> placeholder and will not survive contact with a trademark lawyer. Nothing
> expensive to change is built on it: no domain is assumed, no character
> artwork or other third-party assets are vendored, and the name appears only
> in the package name, the CLI entry points, and prose. **Machine-readable
> surfaces carry no theme at all** — see [Naming](#naming) — so a future rename
> is a find-and-replace over code and copy, never a breaking API change for
> anyone consuming the output.

## Naming

The theme lives in things a person reads: CLI help, this README, the docs, PR
comment headers, and human-readable rationale text. It does **not** touch JSON
keys, schema field names, enum machine values, or exit codes. Someone wiring
Blastoise into CI must not need to know Pokémon to read
`classification: needs_timing`.

Cute in the wrapper, boring in the payload.

| Component | What it is |
|---|---|
| **Torrent** | the parser and IR (`blastoise`, `blastoise.ir`) |
| **Shell Armour** | the lock semantics catalog (`blastoise.catalog`) |
| **Hydro Scan** | the live introspection layer (`blastoise.live`) |
| **Pressure Levels** | the five classification tiers |
| **Shell Report** | the verdict document |
| **Training Ground** | the scale harness (`artifacts/scripts/scale_harness.py`) |
| **Evolution** | the calibration loop |
| **Shell Seal** | signing and attestation |

The five Pressure Levels, and the values they actually serialize as:

| Serialized value | Display name | What the reviewer must do |
|---|---|---|
| `safe` | Calm Water | nothing |
| `safe_irreversible` | One-Way Current | proceed, but record that there is no undo |
| `needs_timing` | Rain Check | safe in itself, disruptive at the wrong moment: off-peak, or `lock_timeout` with retries |
| `unsafe` | Hydro Pump | do not run as written |
| `unknown` | Fog | not enough evidence to decide |

The left column is the contract. `blastoise.verdict.PRESSURE_LEVELS` is the one
lookup that maps it to the right column, and a test pins the left column
against exactly this table.

## How it works

**Torrent** parses `.sql` migration files with
[pglast](https://github.com/lelit/pglast) (libpg_query bindings — the real
Postgres parser, not regex) and classifies every statement by its exact DDL
form, distinguishing forms whose locking or rewrite behavior differs in
production: `ADD COLUMN` with a volatile default vs a constant one,
`CREATE INDEX` vs `CREATE INDEX CONCURRENTLY`, `ADD FOREIGN KEY` vs
`... NOT VALID`, and so on. It also models multi-statement files, explicit vs
implicit transaction boundaries, and statically extracts the statements inside
`DO` blocks.

On top of the IR sits **Shell Armour**, the lock semantics catalog
(`blastoise.catalog`): YAML data mapping every statement classification to the
lock it takes, what that lock blocks, whether the table is rewritten, a
duration model, and the Postgres major versions the entry applies to — every
row cited against the Postgres docs or source, validated exhaustively at load
time.

```python
from blastoise import parse_migration
from blastoise.catalog import load_catalog, resolve

catalog = load_catalog()
script = parse_migration(sql_text)
for statement in script.statements:
    for lock in resolve(catalog, statement, pg_version=16):
        print(lock.entry.lock_mode, lock.entry.duration_model, lock.relations)
```

**Hydro Scan** (`blastoise.live`, requires `pip install blastoise[live]`)
supplies the production context the catalog declares missing: table sizes with
staleness markers, invalid indexes left by failed `CONCURRENTLY` runs, lock
waiters and idle-in-transaction sessions, replication topology and lag, and the
server version. It is strictly read-only — it refuses to run as a role that
*could* write, never reads user table data or query text, and bounds every
query with timeouts. It needs only a three-statement monitoring role: see
[docs/minimum-privilege-role.md](docs/minimum-privilege-role.md).

```python
from blastoise.live import capture_snapshot

targets = {name for s in script.statements for name in s.targets}
snapshot = capture_snapshot("postgresql://blastoise_introspect@db/app", targets)
snapshot.to_canonical_json()  # deterministic; hashed into the evidence bundle
```

Every field that cannot be gathered (no replicas, missing privilege, locked
relation, never-analyzed table) is an explicit `unavailable` marker with the
reason — downstream can always tell "false" from "unknown".

The risk engine (`blastoise.verdict`) combines all three into a Pressure Level
per statement, with the evidence class that produced it.

## Evidence

Design decisions and their reasoning live in [DECISIONS.md](DECISIONS.md). The
measurements behind them live in [artifacts/](artifacts/): per-statement
results over a 3,081-file wild migration corpus, and **Training Ground**, the
scale harness that measures real lock modes and hold durations at
1k / 100k / 1M / 10M rows against a real server, with the scripts that produced
them. See [artifacts/README.md](artifacts/README.md).

## Usage

```console
$ blastoise parse migrations/0001_add_tracking.sql
$ blastoise parse --json migrations/*.sql
```

`bt` is an alias for `blastoise`:

```console
$ bt parse migrations/*.sql
```

```python
from blastoise import parse_migration

script = parse_migration(sql_text)
for statement in script.statements:
    print(statement.span.line, statement.kind, statement.targets)
    for action in statement.alter_actions:
        print("  ", action.kind, action.default)
```

## Development

```console
$ uv sync
$ uv run pytest
$ uv run ruff check .
$ uv run mypy
```

The live tests and both harnesses need PostgreSQL binaries; point
`BLASTOISE_TEST_PG_BIN` at a `bin` directory, or `BLASTOISE_TEST_DSN` at a
server. Without either, the suite falls back to testcontainers.
