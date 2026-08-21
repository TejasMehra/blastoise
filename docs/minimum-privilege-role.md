# The Postgres role pgverdict needs

Three statements. No table access, no write ability, nothing schema-specific:

```sql
CREATE ROLE pgverdict_introspect LOGIN PASSWORD '<generate one>'
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
    CONNECTION LIMIT 2;
GRANT pg_monitor TO pgverdict_introspect;
GRANT CONNECT ON DATABASE your_database TO pgverdict_introspect;
```

That's the whole setup. To remove it: `DROP ROLE pgverdict_introspect;`

## What this role can and cannot do

**Can:** read system catalogs (`pg_class`, `pg_index`, `pg_inherits`,
`pg_attribute`, `pg_constraint`, `pg_proc`, `pg_type`, `pg_cast`),
statistics views (`pg_stat_all_tables`, `pg_stat_activity`,
`pg_stat_replication`, `pg_locks`), settings, and relation sizes. Column
and constraint definitions, function volatility, and cast methods are
plain catalog reads available to any connecting role — they add no grant
beyond the three statements above. This is
the same access every Postgres monitoring agent (Datadog, pganalyze,
pgwatch) asks for — `pg_monitor` is the [built-in role Postgres ships for
exactly this purpose](https://www.postgresql.org/docs/current/predefined-roles.html).

**Cannot:** read a single row of your tables, and cannot write anything.
`SELECT`, `INSERT`, `UPDATE`, `DELETE` on user tables all fail with
`permission denied`. pgverdict never runs `COUNT(*)` or any query against
user data — row counts come from the planner's `reltuples` estimate, with
its staleness recorded alongside.

pgverdict does not trust this setup blindly. On every connection it:

1. opens an explicit `READ ONLY` transaction and verifies it took effect;
2. checks the role's actual privileges, and **refuses to run** — before
   gathering anything — if the role is a superuser, holds
   INSERT/UPDATE/DELETE/TRUNCATE on any user relation, or has
   CREATEDB/CREATEROLE. A role that merely happens not to write is not
   accepted as read-only.
3. bounds every query with a `statement_timeout` and `lock_timeout` of a
   few seconds, so introspection can never become the slow query or sit in
   a lock queue on your production database.

Query text of other sessions is never captured: at most the first keyword
of a statement (`update`, `alter`, ...) and how long it has been running —
never the statement itself, which may contain data literals.

## Notes

- `pg_monitor` is slightly broader than strictly required; the strict
  minimum is `pg_read_all_stats`. Without either, pgverdict still works but
  marks activity- and replication-detail fields as unavailable (Postgres
  masks other roles' `pg_stat_activity` rows), which weakens lock-conflict
  assessment. Grant whichever your policy prefers.
- On PostgreSQL 14 and older, `PUBLIC` can create objects in the `public`
  schema by default; pgverdict records this as a warning in its snapshot.
  It is not specific to this role, and `REVOKE CREATE ON SCHEMA public
  FROM PUBLIC;` (the PG 15+ default) resolves it database-wide.
- Works on PostgreSQL 10 through 18. Wait-duration for lock waiters
  additionally needs PG 14+ (`pg_locks.waitstart`).
