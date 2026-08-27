# The Blastoise GitHub Action

Detects the migration files a pull request changes, assesses each one
against a live database, and reports back: a comment that leads with the
verdict, a check status, and the evidence bundles as a workflow artifact.

```yaml
name: migrations
on: pull_request

permissions:
  contents: read
  pull-requests: write   # to post and update the comment
  checks: write          # to set a neutral status for REQUIRES_APPROVAL

jobs:
  blastoise:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: TejasMehra/blastoise/action@v0
        env:
          BLASTOISE_DATABASE_URL: ${{ secrets.BLASTOISE_STAGING_DATABASE_URL }}
```

That is the whole setup. No config file, no path list: migrations are found
by the layout your framework already imposes.

---

## The database role — read this first

Blastoise connects **read-only** and never reads a row of your data. Three
statements, and they are the same three whether you point it at staging or
anywhere else:

```sql
CREATE ROLE blastoise_introspect LOGIN PASSWORD '<generate one>'
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
    CONNECTION LIMIT 2;
GRANT pg_monitor TO blastoise_introspect;
GRANT CONNECT ON DATABASE your_database TO blastoise_introspect;
```

To remove it: `DROP ROLE blastoise_introspect;`

**What that role can do:** read system catalogs (`pg_class`, `pg_index`,
`pg_attribute`, `pg_constraint`, `pg_proc`, `pg_type`, `pg_cast`,
`pg_inherits`), statistics views (`pg_stat_all_tables`, `pg_stat_activity`,
`pg_locks`, `pg_stat_replication`), settings, and relation sizes. This is
what every Postgres monitoring agent asks for; `pg_monitor` is the
[role Postgres ships for exactly this
purpose](https://www.postgresql.org/docs/current/predefined-roles.html).

**What it cannot do:** read a single row of your tables, or write anything.
`SELECT`, `INSERT`, `UPDATE`, `DELETE` on user relations all fail with
`permission denied`. Row counts come from the planner's `reltuples`
estimate, with its staleness recorded in the report — there is no
`COUNT(*)` anywhere in the tool.

**Stricter, if your policy prefers it:** `GRANT pg_read_all_stats` instead
of `pg_monitor`. With neither, the check still runs and marks the
activity- and replication-detail fields unavailable, which weakens
lock-conflict assessment.

The connection is not trusted on the strength of those grants alone. On
every connect Blastoise opens an explicit `READ ONLY` transaction and
verifies it took effect, checks the role's actual privileges and **refuses
to gather anything** if the role is a superuser or holds
INSERT/UPDATE/DELETE/TRUNCATE on any user relation, and bounds every query
with a `statement_timeout` and `lock_timeout` of a few seconds so
introspection can never become the slow query on your database. Full
detail: [docs/minimum-privilege-role.md](../docs/minimum-privilege-role.md).

### Which database to point it at

**Staging, by default**, and the input's name says so. The check wants a
database with production's *schema*; it reads sizes and statistics from
whatever it is given, so a staging database that is 1% of production's size
produces verdicts calibrated to 1% of production's row counts — and the
report says which database it read, in `snapshot_hash` and in the comment's
header, so a reviewer can see the difference is there.

If you point it at a production replica, use the role above unchanged, and
know that the honest trade is: better numbers, one more thing holding a
credential.

### How the credential reaches the check

Through the environment, from a secret, and no other way:

```yaml
- uses: TejasMehra/blastoise/action@v0
  env:
    BLASTOISE_DATABASE_URL: ${{ secrets.BLASTOISE_STAGING_DATABASE_URL }}
```

**There is no `database-url` input, and there will not be one.** A workflow
input is not a secret: it is echoed into the run's logs, it is readable in
the event payload of a `workflow_run`, and under
`pull_request_target` it can be influenced by whoever opened the pull
request. `blastoise ci` additionally **refuses** to read its connection
string from any variable in the `INPUT_*` namespace, which is how an Action
input arrives — so the refusal holds even if someone wires one up by hand.

`database-url-env` changes the *name* of the variable that is read, never
the value. A value passed where the name belongs is rejected with the
reason, not treated as a variable that happens not to exist.

Everything the check prints — the log, the comment, the job summary, an
exception message, a traceback out of the driver — goes through a redactor
that replaces the connection string, its password, host, user and database
name, plus anything else shaped like a connection string, before it is
written anywhere.

---

## What it does

**Detects migrations by framework layout.** No configuration needed for:

| Framework | Layout | Assessed? |
|---|---|---|
| Prisma | `prisma/migrations/*/migration.sql` | yes |
| Flyway | `V1__*.sql`, `U1__*.sql`, `R__*.sql` | yes |
| golang-migrate | `*.up.sql`, `*.down.sql` | yes |
| plain SQL | `migrations/**/*.sql`, `db/migrations/**/*.sql` | yes |
| Rails | `db/migrate/*.rb` (and `db/*/migrate/`) | **no — recognized, not assessed** |
| Django | `*/migrations/*.py` | **no — recognized, not assessed** |
| Alembic | `*/versions/*.py` | **no — recognized, not assessed** |

Every pattern matches at any depth, so a monorepo's
`services/billing/db/migrate/` is found the same as a root-level one.

Rails, Django and Alembic migrations are a DSL: the SQL they run does not
exist until the framework renders it, so the parser cannot read them.
The check **says so, per file, in the comment**, and holds the run at
`requires_approval` rather than passing — a green check on a pull request
whose only migration was never read is the one outcome worse than no check
at all. If you want one of them actually analyzed,
[open an issue](https://github.com/TejasMehra/blastoise/issues/new) — that
is what decides which adapter gets built.

**Comments once, and edits it.** The comment carries an invisible marker;
a re-push finds it and rewrites it rather than adding to a column of stale
verdicts.

**Sets a check status from the verdict.**

| Verdict | Check status | Job |
|---|---|---|
| `proceed` | success | passes |
| `requires_approval` | **neutral** | passes (by default) |
| `block` | failure | fails |

`neutral` is why this uses the Checks API rather than the exit code alone:
a job can only pass or fail, and "a human has to look at this" is neither.
It needs `checks: write`; without that permission the check status is
skipped with a note and the exit code is the only signal.

`fail-on: requires_approval` makes the job fail on that too, for a team
that wants an approval gate rather than a marker.

**Uploads the evidence.** Every assessed file gets a `report.json` and an
`evidence/` directory — the migration source, the parsed IR, the exact
catalog rows consulted, the duration constants, and the live snapshot —
each hashed, so any claim in the report traces back to the bytes that
produced it. The upload runs **before** the verdict is applied, so a
`BLOCK` still leaves you the evidence.

---

## Configuration

Optional. Only needed if your migrations are not where any framework puts
them, or you want to change how loud the check is. `.blastoise.yml` at the
repository root:

```yaml
version: 1

migrations:
  # Explicit globs OVERRIDE detection entirely: with `paths` set, a file is
  # a migration if and only if it matches one of these. That is deliberate
  # — a config that only added to the conventions would leave you unable to
  # turn a false positive off.
  paths:
    - "sql/schema/**/*.sql"
  # Applies either way, checked last.
  exclude:
    - "**/seed_*.sql"
    - "sql/schema/legacy/**"

database:
  url_env: BLASTOISE_DATABASE_URL   # the NAME of an env var. Never a URL.
  label: staging                    # shown in the comment. Never a host.
  pg_version: 17                    # assumed when running offline

ci:
  fail_on: block                    # block | requires_approval | never
  comment: true
  check_run: true
```

Globs use the conventional path semantics: `*` within one path segment,
`**` across segments, `?` one character, `[abc]` a class.

Unknown keys are an **error**, not a warning — a silently ignored
`exclude:` is a team believing an exclusion is in force when it is not.
`database.url` and `database.password` are refused outright with the reason.

---

## Inputs

| Input | Default | What it does |
|---|---|---|
| `version` | latest | Version of the `blastoise` package to install. Pin it for a reproducible check. |
| `python-version` | `3.12` | Python used to run it. |
| `working-directory` | `.` | Root the changed paths are relative to. |
| `config` | auto | Path to `.blastoise.yml`. |
| `database-url-env` | `BLASTOISE_DATABASE_URL` | **Name** of the env var holding the connection string. |
| `database-label` | — | Label for the database read, shown in the comment. |
| `offline` | `false` | Skip the database entirely. |
| `pg-version` | `17` | Postgres major version assumed when offline. |
| `fail-on` | `block` | `block`, `requires_approval`, or `never`. |
| `comment` | `true` | Post and update the pull request comment. |
| `check-run` | `true` | Set the check status. |
| `changed-source` | `auto` | `auto`, `github_api`, `git`, `file`. |
| `base-ref` | — | Base commit for a `git` diff. Needs `fetch-depth: 0`. |
| `upload-artifact` | `true` | Upload the reports and evidence. |
| `artifact-name` | `blastoise-reports` | Name of that artifact. |
| `artifact-retention-days` | `30` | How long to keep it. |
| `github-token` | `github.token` | Token used to comment and set the status. |

**Outputs:** `verdict`, `migrations-detected`, `assessed`, `unassessed`,
`online`, `report-dir`, `comment-url`.

---

## Notes on how it finds what changed

By default it asks the API for the pull request's file list, which is
computed against the merge base server-side and works with the default
shallow `actions/checkout`. If the token cannot list files it falls back to
`git diff`, which needs the base commit locally — `fetch-depth: 0`, or
`changed-source: git` with an explicit `base-ref`.

## Fork pull requests

A pull request from a fork gets a read-only `GITHUB_TOKEN` and no access to
secrets. The check runs offline, cannot comment, and cannot set a status.
It says so in the log and leaves the verdict in the job summary, the
artifact and the exit code, rather than failing a contributor's pull
request over a permission model they do not control.

## Other CI systems

The same check, the same code path, from the Docker image:

```console
$ docker run --rm -v "$PWD:/repo" -w /repo \
    -e BLASTOISE_DATABASE_URL \
    ghcr.io/tejasmehra/blastoise:0.1.0 \
    ci --changed-source git --base-ref "$CI_MERGE_REQUEST_DIFF_BASE_SHA" \
       --no-comment --no-check-run \
       --comment-output blastoise.md --json-output blastoise.json
```

`--comment-output` writes the same Markdown the GitHub comment would carry,
for your platform's own comment API; `--json-output` writes the machine
summary. Exit codes are `0` (nothing to stop the merge), `2` (block), `1`
(requires approval, when `fail-on` says so), `3` (the run itself failed —
never a claim that the migration is dangerous).
