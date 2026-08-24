# How it works

Three layers, each usable on its own from Python.

## Torrent — the parser and IR (`blastoise`, `blastoise.ir`)

Migration `.sql` files are parsed with [pglast](https://github.com/lelit/pglast)
(libpg_query — the real Postgres parser, not regex) and every statement is
classified by its exact DDL form, distinguishing forms whose locking or
rewrite behavior differs in production: `ADD COLUMN` with a volatile default
vs a constant one, `CREATE INDEX` vs `CREATE INDEX CONCURRENTLY`,
`ADD FOREIGN KEY` vs `... NOT VALID`, and so on. Multi-statement files,
explicit vs implicit transaction boundaries, and the statements inside `DO`
blocks are modeled too.

```python
from blastoise import parse_migration

script = parse_migration(sql_text)
for statement in script.statements:
    print(statement.span.line, statement.kind, statement.targets)
    for action in statement.alter_actions:
        print("  ", action.kind, action.default)
```

## Shell Armour — the lock catalog (`blastoise.catalog`)

YAML data mapping every statement classification to the lock it takes, what
that lock blocks, whether the table is rewritten, a duration model, and the
Postgres major versions the entry applies to — every row cited against the
Postgres docs or source and validated exhaustively at load time.

```python
from blastoise import parse_migration
from blastoise.catalog import load_catalog, resolve

catalog = load_catalog()
script = parse_migration(sql_text)
for statement in script.statements:
    for lock in resolve(catalog, statement, pg_version=16):
        print(lock.entry.lock_mode, lock.entry.duration_model, lock.relations)
```

## Hydro Scan — live introspection (`blastoise.live`, `pip install pgblastoise[live]`)

Supplies the production context the catalog declares missing: table sizes
with staleness markers, columns and constraints, invalid indexes left by
failed `CONCURRENTLY` runs, lock waiters and idle-in-transaction sessions,
replication topology and lag, the server version — and a **calibration
probe**: a fixed unit of CPU work timed on the target, so duration
estimates are scaled to the hardware the migration will actually run on
rather than to the machine the constants were measured on.

It is strictly read-only: it refuses to run as a role that *could* write,
never reads user table data or query text, bounds every query with
timeouts, and needs only a three-statement monitoring role — see
[minimum-privilege-role.md](minimum-privilege-role.md).

```python
from blastoise.live import capture_snapshot

targets = {name for s in script.statements for name in s.targets}
snapshot = capture_snapshot("postgresql://blastoise_introspect@db/app", targets)
snapshot.to_canonical_json()  # deterministic; hashed into the evidence bundle
```

Every field that cannot be gathered (no replicas, missing privilege, locked
relation, never-analyzed table) is an explicit `unavailable` marker with the
reason — downstream can always tell "false" from "unknown".

## The risk engine (`blastoise.verdict`)

Combines all three into a verdict per statement with the evidence class
that produced it. The rules, thresholds, and the refusal-at-the-boundary
behavior are in [tiers.md](tiers.md). Every design decision and the
measurements behind the constants are in [../DECISIONS.md](../DECISIONS.md)
and [../artifacts/](../artifacts/README.md).
