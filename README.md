# pgverdict

Analyze Postgres schema migrations for production safety.

pgverdict parses `.sql` migration files with [pglast](https://github.com/lelit/pglast)
(libpg_query bindings — the real Postgres parser, not regex) and classifies every
statement by its exact DDL form, distinguishing forms whose locking or rewrite
behavior differs in production: `ADD COLUMN` with a volatile default vs a constant
one, `CREATE INDEX` vs `CREATE INDEX CONCURRENTLY`, `ADD FOREIGN KEY` vs
`... NOT VALID`, and so on. It also models multi-statement files, explicit vs
implicit transaction boundaries, and statically extracts the statements inside
`DO` blocks.

On top of the IR sits the **lock semantics catalog** (`pgverdict.catalog`):
YAML data mapping every statement classification to the lock it takes, what
that lock blocks, whether the table is rewritten, a duration model, and the
Postgres major versions the entry applies to — every row cited against the
Postgres docs or source, validated exhaustively at load time.

```python
from pgverdict import parse_migration
from pgverdict.catalog import load_catalog, resolve

catalog = load_catalog()
script = parse_migration(sql_text)
for statement in script.statements:
    for lock in resolve(catalog, statement, pg_version=16):
        print(lock.entry.lock_mode, lock.entry.duration_model, lock.relations)
```

The **live introspection layer** (`pgverdict.live`, requires
`pip install pgverdict[live]`) supplies the production context the catalog
declares missing: table sizes with staleness markers, invalid indexes left
by failed `CONCURRENTLY` runs, lock waiters and idle-in-transaction
sessions, replication topology and lag, and the server version. It is
strictly read-only — it refuses to run as a role that *could* write, never
reads user table data or query text, and bounds every query with timeouts.
It needs only a three-statement monitoring role: see
[docs/minimum-privilege-role.md](docs/minimum-privilege-role.md).

```python
from pgverdict.live import capture_snapshot

targets = {name for s in script.statements for name in s.targets}
snapshot = capture_snapshot("postgresql://pgverdict_introspect@db/app", targets)
snapshot.to_canonical_json()  # deterministic; hashed into the evidence bundle
```

Every field that cannot be gathered (no replicas, missing privilege, locked
relation, never-analyzed table) is an explicit `unavailable` marker with the
reason — downstream can always tell "false" from "unknown".

The verdict layer is built on top of these.

## Evidence

Design decisions and their reasoning live in [DECISIONS.md](DECISIONS.md).
The measurements behind them live in [artifacts/](artifacts/): per-statement
results over a 3,081-file wild migration corpus, and a scale harness that
measures real lock modes and hold durations at 1k / 100k / 1M / 10M rows
against a real server, with the scripts that produced them. See
[artifacts/README.md](artifacts/README.md).

## Usage

```console
$ pgverdict parse migrations/0001_add_tracking.sql
$ pgverdict parse --json migrations/*.sql
```

```python
from pgverdict import parse_migration

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
