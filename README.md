<p align="center">
  <img src="https://raw.githubusercontent.com/TejasMehra/blastoise/master/docs/assets/logo.png" alt="Blastoise — know the blast radius before you migrate." width="100%">
</p>

```sql
ALTER TABLE events ADD COLUMN plan text DEFAULT 'free';                     -- 12 ms
ALTER TABLE events ADD COLUMN customer_ref uuid DEFAULT gen_random_uuid();  -- blocks every read and write for 56 seconds
```

Same shape, same 5M-row table, measured on the same machine. Nothing in the diff tells you which is which.

## What it looks like

Real output of `blastoise check` on a three-statement migration, against a live database with a 5M-row `events` table (trailing sections trimmed):

```text
SHELL REPORT
============
verdict: BLOCK
change 7f6e927e9f3be30a  pg 17  online  evaluated 2026-08-23T11:41:33.172711+00:00

pressure levels
  unsafe                1   Hydro Pump       do not run as written
  unknown               0   Fog              not enough evidence to say
  needs_timing          1   Rain Check       safe in itself, wrong at the wrong moment
  safe_irreversible     0   One-Way Current  proceed, but there is no undo
  safe                  1   Calm Water       nothing to do

statements
  L1     create_index_concurrently    safe               seconds     proven
         create_index_concurrently holds no lock that blocks reads or writes
         on a pre-existing relation (the work itself runs in the seconds band)
  L2     alter_table                  needs_timing       sub_second  observed
         add_column_default_nonvolatile is catalog-only but takes a read-and-
         write-blocking lock on events; the work is brief, the wait for the
         lock may not be
         condition: acquisition must be prompt: set lock_timeout with retries
         or run in a low-traffic window - while this statement waits for its
         lock, every later query on the relation queues behind it
  L3     alter_table                  unsafe             minutes     simulated
         alter_column_type blocks reads and writes for a hold measured in
         minutes at worst: an outage-length stall
```

Exit code `2`, so CI stops it. When Blastoise can't be sure, it says `unknown` and why, instead of guessing.

## Try it in thirty seconds

No database needed for the first run — it reads the SQL and tells you what it can from that alone:

```console
$ pip install pgblastoise
$ blastoise check migrations/0042_add_customer_ref.sql --offline
```

> **The PyPI package is [`pgblastoise`](https://pypi.org/project/pgblastoise/),
> not `blastoise`.** The plain name was already taken by an unrelated parquet
> library, so `pip install blastoise` gets you the wrong thing. Only the
> distribution name carries the prefix — the command you run is `blastoise`
> and the module you import is `blastoise`.

Point it at a read-only connection and the `unknown`s turn into answers:

```console
$ pip install 'pgblastoise[live]'
$ blastoise check migrations/0042_add_customer_ref.sql --database-url postgres://ro@db/app
```

## In CI, where it actually gets used

A tool you have to remember to run is a tool you stop running. Add the
Action and every pull request that touches a migration gets a comment
leading with the verdict, and a check status — `proceed` passes,
`requires_approval` is **neutral**, `block` fails:

```yaml
name: migrations
on: pull_request
permissions: { contents: read, pull-requests: write, checks: write }

jobs:
  blastoise:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: TejasMehra/blastoise/action@v0
        env:
          BLASTOISE_DATABASE_URL: ${{ secrets.BLASTOISE_STAGING_DATABASE_URL }}
```

No path configuration: migrations are found by the layout your framework
already imposes — Rails, Django, Prisma, Flyway, Alembic, golang-migrate,
or a plain `migrations/` directory.

**Rails migrations are analyzed**, and it is opt-in because of what it
takes. A Rails migration is Ruby: the SQL it runs does not exist until
ActiveRecord renders it, and Rails has no dry-run that renders without
applying. So Blastoise runs the migration against a throwaway database and
records the SQL ActiveRecord actually emits — which keeps the things a
schema diff would lose, like `CREATE INDEX CONCURRENTLY`, backfill
`UPDATE`s, and whether the whole thing ran in a transaction. That SQL then
goes through the same parser and engine as a `.sql` file.

Running the migration means executing Ruby from the pull request, so it is
off until you ask for it, it refuses to run under `pull_request_target`,
and it refuses a scratch database on the same server as the one being
assessed:

```yaml
# .blastoise.yml
rails:
  extract: true
```

```yaml
      - uses: TejasMehra/blastoise/action@v0
        env:
          BLASTOISE_DATABASE_URL: ${{ secrets.BLASTOISE_STAGING_DATABASE_URL }}
          BLASTOISE_SCRATCH_DATABASE_URL: postgres://postgres@localhost:5432/postgres
```

The job needs Ruby and a completed `bundle install`, because the Rails that
renders the SQL has to be the app's own — a migration declaring
`ActiveRecord::Migration[8.1]` will not render under any older
ActiveRecord. It also needs a committed `db/schema.rb` or `db/structure.sql`
to rebuild the state the migration expects; without one, Blastoise replays
the earlier migrations, and gives up rather than replaying a whole history.

One thing does not render: a migration that needs your booted application
rather than just ActiveRecord — one referencing a model constant,
`Rails.root`, or `Rails.configuration`. The harness loads and installs your
gems, but it does not boot your app, deliberately: it is a subprocess with a
scratch database and no secrets. Separately, an application whose
`db/schema.rb` depends on a SQL function it cannot express (Ruby schema
format cannot declare functions) falls back to replaying migrations.

Either way the file is reported as unassessed **with the reason** and the run
is held at `requires_approval`. Nothing is ever given a guessed verdict built
from reconstructed SQL. Measured against 40 migrations from Discourse,
Mastodon, Forem and OpenFoodNetwork: 35 rendered and verified against the
real migration's effect, 5 refused, 0 wrong — see
[`artifacts/validation/`](artifacts/validation/).

**Django and Alembic migrations are detected but not yet analyzed** — they
write a DSL rather than SQL, so Blastoise reports them and holds the run at
`requires_approval` instead of passing them silently. Want one of them
actually analyzed? [Open an issue](https://github.com/TejasMehra/blastoise/issues/new)
— that is what decides which adapter gets built.

The connection string comes from a secret and from nowhere else; there is
no input that takes one, and every output path is redacted. On GitLab or
Buildkite, the same check runs from the Docker image. Details, and the
three `GRANT` statements the role needs:
[the Action's README](https://github.com/TejasMehra/blastoise/blob/master/action/README.md).

## What makes it different

Other migration linters read the SQL. Blastoise reads the SQL **and your live database**, because `CREATE INDEX` on 200 rows is fine and on 40 million rows takes your site down — the statement is the same, only the table knows.

## Tested against real migrations

The classifier has been run over **3,081 real migration files** from coder, sourcegraph, mattermost, cal.com, discourse, zulip, temporal, and eight other open-source projects. Among them: **1,875 plain `CREATE INDEX`** versus **121 `CREATE INDEX CONCURRENTLY`**.

## Docs

- [The five verdicts, thresholds, and exit codes](https://github.com/TejasMehra/blastoise/blob/master/docs/tiers.md)
- [How it works: parser, lock catalog, live introspection](https://github.com/TejasMehra/blastoise/blob/master/docs/how-it-works.md)
- [Reports, evidence bundles, signing](https://github.com/TejasMehra/blastoise/blob/master/docs/reports.md)
- [The GitHub Action, the Docker image, and the config](https://github.com/TejasMehra/blastoise/blob/master/action/README.md)
- [The read-only database role it needs (three statements)](https://github.com/TejasMehra/blastoise/blob/master/docs/minimum-privilege-role.md)
- [Design decisions and measurements](https://github.com/TejasMehra/blastoise/blob/master/DECISIONS.md) · [Artifacts](https://github.com/TejasMehra/blastoise/blob/master/artifacts/README.md)
- [The package on PyPI](https://pypi.org/project/pgblastoise/) — `pip install pgblastoise`

## Development

```console
$ uv sync && uv run pytest && uv run ruff check . && uv run mypy
```

Live tests and harnesses need Postgres binaries (`BLASTOISE_TEST_PG_BIN`) or a server (`BLASTOISE_TEST_DSN`); otherwise they use testcontainers.
