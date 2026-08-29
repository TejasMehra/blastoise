# Decisions

> The project was called **pgverdict** until 2026-08-22. The sections below were
> written under that name and have been renamed in place, so that the module paths,
> file paths and identifiers they cite still resolve against the code; nothing else
> about them was edited. The rename itself is recorded in the last section.

I built the parsing layer of blastoise: `pglast` 8.4 (libpg_query, PG18 grammar) parses each
migration file into real Postgres parse trees, and `classify.py` maps every statement onto a
deliberately fine-grained typed IR — ~95 `StatementKind`s and ~75 `AlterTableActionKind`s as
`StrEnum`s, frozen/slotted dataclasses for statements, spans, transaction groups, and per-family
details — where any two forms with different locking or rewrite behavior get different kinds
(`ADD COLUMN` splits eight ways by what fills existing rows, constraints split by `NOT VALID`
and `USING INDEX`, everything `CONCURRENTLY` is its own kind) and the rest becomes typed fields
(`only`, `cascade`, `not_valid`, volatility). I chose pglast over regex/sqlparse for obvious
reasons, but also over writing a lock table now: this layer records facts (e.g. `SET DEFAULT
gen_random_uuid()` is *volatile* but rewrites nothing) and leaves judgments to the future
analyzer; default-expression volatility uses small allowlists of known functions and operators
with everything unrecognized reported as `UNKNOWN` rather than guessed; DO blocks are parsed
with `parse_plpgsql`, collecting exactly the expressions with `parseMode == 0` (which turn out
to be precisely the embedded full statements) and counting dynamic `EXECUTE`s as opaque; and
transaction grouping records explicit `BEGIN`/`COMMIT`/`AND CHAIN` structure while deliberately
not deciding whether a runner wraps the file. Surprises: pglast ships `py.typed` with fully
typed stubs, so mypy `--strict` checks the AST handling for real; libpg_query already strips
plpgsql `INTO var` from embedded queries; `stmt_location` (PG17+) skips leading comments, making
verbatim statement slices trivial; `ParseError` hides its offset in `args[1]` (not a `.location`
attribute — found by a test agent) and double-converts byte/char offsets when emoji precede an
error, which I work around by re-finding the quoted token; boolean utility options mean
`VACUUM (FULL OFF)` parses with a `full` DefElem you must actually read the value of (an
adversarial review caught me classifying it as `VACUUM FULL`); a scalar subquery parses fine in
DEFAULT position and only fails at analysis time; and `DEFAULT NULL NOT NULL` — which ORMs emit —
is semantically the *failing* no-default form despite having a DEFAULT clause. The test corpus
(318 tests, ~300 real-world-style migration examples, 98% branch coverage with each remaining
miss justified as unreachable from valid SQL) was written by six parallel agents, one per DDL
category, then hardened by an adversarial semantics/robustness review that produced six real
bug fixes.

## Real-world corpus findings (2026-08-19)

Ran the classifier over **3,081 real migration files (15,514 statements)** harvested from 15
mature open-source projects: coder (573), lemmy (342), windmill (300), sourcegraph (300),
boundary (290), ory/kratos (250), mattermost (213), temporal (46), zed (1), marquez/Flyway (84),
three Prisma apps (cal.com 150, langfuse 150, formbricks 100), plus SQL extracted from
Discourse's Rails `execute` heredocs (250) and Zulip's Django `RunSQL` (32). Rails/Django/Alembic
migrations are DSL files, so only their embedded raw SQL is represented. Nothing was fixed;
this section records the gap.

### Parse failures

**Zero genuine failures.** 3,078/3,081 files parse; the 3 failures are all Discourse extraction
artifacts (Ruby `\'` escape sequences and `:named` bind parameters leaking through the harvest),
not Postgres SQL the parser mishandles. Every DO block in the wild (129 of them, 261 embedded
statements, 39 dynamic EXECUTEs) parsed fully — `fully_parsed=False` never occurred.

### Catch-all / UNKNOWN classifications

- `StatementKind.OTHER` (top-level): 16 statements (0.10%) — `CREATE STATISTICS` ×10 (lemmy),
  `CREATE AGGREGATE` ×2 + `DROP AGGREGATE` ×1 (sourcegraph), `ALTER ROLE ... SET` ×2 (zulip;
  pglast's `AlterRoleSetStmt` is a separate node the classifier doesn't map even though
  `ALTER_ROLE` exists), `ALTER STATISTICS` ×1. Inside DO blocks: `ALTER DEFAULT PRIVILEGES` ×7
  (windmill).
- `RENAME_OTHER`: 43 (0.28%) — the big one is **`ALTER TYPE ... RENAME TO` ×29**, the standard
  Prisma enum-change recipe (rename type → create new → alter column → drop old); rest are
  sequence ×6, function ×5, trigger ×2, domain ×1 renames. A `RENAME_TYPE` kind is warranted.
- Unknown-volatility defaults: 16 occurrences, 4 distinct expressions — three are custom
  functions where UNKNOWN is the correct conservative answer (`generate_unique_changeme()` ×13,
  `wt_to_sentinel(...)`, `random_smallint()`), one is a volatility-table miss:
  `tstzrange(now(), NULL, '[]')` — range constructors are immutable built-ins.
- `AlterTableActionKind.OTHER` / `ADD_CONSTRAINT_OTHER`: **zero** in the wild.

### Frequency distribution: wild vs synthetic

Top wild kinds: alter_table 20.4%, create_index 12.1%, **comment_on 8.7%**, create_table 7.7%,
**create_trigger 6.5%**, update 5.1%, insert 4.1%, **create_function 3.7%**, commit 3.2%,
create_view 3.0%, alter_enum_add_value 2.5%. The synthetic corpus is DDL-lock-centric (34%
alter_table); the wild is full of documentation, trigger/function plumbing, and DML backfills.
Most underweighted in the synthetic corpus relative to reality: `COMMENT ON` (32× under),
`CREATE TRIGGER` (24×), `CREATE FUNCTION` (14×), `CREATE VIEW`/`DROP VIEW` (~6-8×), `INSERT`
(8×), `UPDATE` (3×), `CREATE INDEX` (3×), `ALTER TYPE ADD VALUE` (3×).

39 kinds never appear in 3,081 wild files, including every maintenance form (vacuum,
vacuum_full, cluster, reindex_concurrently, refresh_matview — matviews get *created* in
migrations but apparently refreshed elsewhere), every savepoint/rollback form, **all partition
DDL** (no `PARTITION OF`, no ATTACH/DETACH in any repo — partition management evidently lives
outside migration files), COPY, MERGE, SELECT INTO, foreign tables, ALTER SYSTEM, and
database/tablespace/role DDL. These stay in the taxonomy (they're exactly what a safety tool
must flag when they *do* appear) but the synthetic corpus overweights them.

Action-level reality check: `add_foreign_key` 527 vs `add_foreign_key_not_valid` **1**;
`validate_constraint` **1**; `add_primary_key`/`add_unique` 283 vs USING INDEX variants **4**;
`create_index` 1,875 vs `create_index_concurrently` 121 (6%). The safe patterns blastoise
exists to recommend are nearly absent in real migration history — which validates the product
thesis but means the synthetic corpus (written lock-first) over-represents them.
`add_column_default_volatile` is real but rare (9, mostly `gen_random_uuid()`); most wild
"volatile default" sightings are `SET DEFAULT nextval(...)` in pg_dump-style squash files,
which set defaults without rewriting.

### What the synthetic corpus missed

(1) Squash-genre files — pg_dump-style migrations with hundreds of statements, `SET DEFAULT
nextval`, and blanket `COMMENT ON` (sourcegraph, zed) — no synthetic test looks like this;
(2) the Prisma enum-migration dance (`ALTER TYPE RENAME` + recreate + `ALTER COLUMN TYPE ...
USING` + drop); (3) trigger/function-heavy plumbing migrations (boundary averages 13
statements/file, largely plpgsql functions and triggers); (4) `CREATE STATISTICS`,
`ALTER DEFAULT PRIVILEGES`, `ALTER ROLE ... SET`, and aggregate DDL, all currently `OTHER`;
(5) `tstzrange`/range constructors in the volatility table; (6) conditional-DDL DO blocks
guarding `ALTER` with `information_schema` probes — the wild's dominant DO idiom, well handled
but thinly tested. Corpus and full JSON report retained in the session scratchpad
(`corpus/`, `corpus_report.json`) with per-file manifests mapping back to repo paths.

## Corpus gap closure (2026-08-19)

Closed the gaps from the corpus report and re-ran the same 3,081-file corpus. Taxonomy:
`ALTER TYPE ... RENAME TO` is now `RENAME_TYPE` (29 wild hits, and rename statements without a
relation now carry the renamed object as a target); `CREATE STATISTICS` / `ALTER STATISTICS` /
`ALTER DEFAULT PRIVILEGES` / `CREATE AGGREGATE` / `DROP AGGREGATE` got kinds; pglast's
`AlterRoleSetStmt` node now maps to `ALTER_ROLE`; the six range constructors (`tstzrange`,
`tsrange`, `numrange`, `daterange`, `int4range`, `int8range`) joined the immutable allowlist
(arguments still dominate, so `tstzrange(now(), ...)` is stable, not constant). New shape
detection, all detect-don't-judge: **DML granularity** — UPDATE/DELETE split into
`*_WITHOUT_WHERE` / `*_BATCHED` / plain kinds with a `DmlDetails(has_where, batch_signals)`
record; batching is recognized syntactically as LIMIT-bounded subselects/CTEs, ctid targeting,
or two-sided key windows (`id >= a AND id < b`, `BETWEEN`), and a one-sided retention cutoff is
deliberately not a window. **Baseline files** — `MigrationScript.baseline_shaped` flags files
with ≥50 statements where every altered/indexed/filled/dropped relation was created earlier in
the same file (DROP IF EXISTS preambles allowed, renames and partition parents tracked, names
compared unqualified). **Guarded DO blocks** — `DoBlockDetails.existence_guarded` marks blocks
where an embedded statement sits under an IF whose condition (parsed, not string-matched)
probes `information_schema`, `pg_catalog` tables, or `to_reg*()`.

Re-run results on the same corpus (3,078 parsed, 15,514 statements): top-level
`StatementKind.OTHER` **16 → 0**; DO-inner `OTHER` **7 → 0** (all `ALTER DEFAULT PRIVILEGES`);
`RENAME_OTHER` **43 → 14** (sequence 6, function 5, trigger 2, domain 1 — legitimately
generic, and all now carry targets); unknown-volatility defaults **16 → 15**, every one a
user-defined function where UNKNOWN is the correct conservative answer. The new DML split
surfaces **176 `update_without_where` + 20 `delete_without_where`** (~1.3% of all wild
statements — the unbatched-backfill hazard is real and common), against 599 plain UPDATEs, 10
`update_batched`, and 0 `delete_batched` in the wild. 10 files across six repos flag as
`baseline_shaped` (sourcegraph and boundary squashes, temporal/windmill/zed/formbricks base
schemas). 23 of 129 DO blocks (18%) are `existence_guarded`. Suite after the changes: 354
tests, 97% branch coverage, ruff and mypy --strict clean.

## Lock semantics catalog (2026-08-20)

Built the lock semantics catalog as YAML data (`src/blastoise/catalog/lock_catalog.yaml`,
~3,300 lines) with a validating loader and a resolver, in wild-frequency order per the corpus
report. Each row maps one classification × PG-major-range × optional IR-shape variant (`when:`
predicates over IR fields — `LOCK TABLE` splits eight ways by mode, `INSERT` by VALUES vs
SELECT, `REINDEX` by scope, `RENAME_OTHER` by object class) to lock_mode, conflicts_with (from
the doc's Table 13.2, validated symmetric + complete), blocks_reads/writes,
requires_table_rewrite, duration_model, transaction_block, failure_mode, and
**affected_relations** — every relation locked, by role, with its own mode, so `ADD FOREIGN
KEY` yields the referenced table as a second SHARE ROW EXCLUSIVE relation, `DROP INDEX` the
unnamed owning table at ACCESS EXCLUSIVE, `ATTACH PARTITION` the partition at ACCESS EXCLUSIVE
under a SHARE UPDATE EXCLUSIVE parent. Version-awareness is structural: a `version_breakpoints`
registry (26 entries: fast default in 11, ATTACH/SET NOT NULL/RENAME INDEX/enum-in-txn in 12,
CIC-wait removal in 14, GRANT taking ACCESS SHARE in 18, feature introductions...) makes the
loader reject any row spanning a registered change, and forms that predate their version get
explicit `available: false` rows. Kinds absent from the wild corpus (41 statement kinds + 11
actions) are explicit `UNCALIBRATED` stubs; only the three grab-bag kinds may say
`lock_mode: UNKNOWN`, and nothing falls through to a default — a missing kind fails the load
(the loader also cross-checks blocks_reads/writes against the matrix, citations on every
CALIBRATED row, and that the row's lock matches its target role's). Every fact was verified
against the docs *and* `AlterTableGetLockLevel`/`table_open` call sites in REL_10/11/12/14/17/18
sources fetched this session — the doc text is quoted and the source line cited per row.
Small IR additions to support this: `AlterTableAction.referenced_table` and
`CreateTableDetails.referenced_tables` (FK targets), `InsertDetails.source`
(values/default_values/select). Validation: the full 3,081-file corpus resolves with **zero
lookup errors on every version 10–18**, hitting only CALIBRATED rows; 36% of resolved rows come
out no-block+CONSTANT (the harmless majority: COMMENT ON, CREATE TRIGGER at SRE-but-instant,
CREATE FUNCTION...), and 417 rows are rewrites. Suite: 482 tests, 97% branch coverage, ruff and
mypy --strict clean.

Uncertain entries and judgment calls (all marked in the rows themselves):
(1) **Worst-case-with-live_context convention** — where the real behavior depends on state the
statement can't show, the row models the worst case and sets `requires_live_context` naming
exactly what would narrow it: ALTER COLUMN TYPE (binary-coercible changes skip the rewrite),
SET NOT NULL on 12+ (proving CHECK skips the scan), DML row counts, add-column-with-domain-type
(constrained domain disables the fast path — the IR sees only a type name), ALTER DOMAIN
(subtype not classified: modeled as the constraint-adding form that SHARE-locks every table
using the domain). (2) **ADD PRIMARY KEY USING INDEX modeled as CONSTANT** with live_context for
the nullable-column case (docs: full SET NOT NULL scan then) — the form exists solely as the
fast path, so worst-casing it felt wrong; flagged instead. (3) **set_storage_params modeled as
SHARE UPDATE EXCLUSIVE** although nine reloptions escalate to ACCESS EXCLUSIVE
(security_barrier, fastupdate, ...) — the IR doesn't record option names; every wild occurrence
is autovacuum/fillfactor; escalating options listed in live_context. (4) **statement-level lock
for ALTER TABLE** is max over subcommand rows, mirroring AlterTableGetLockLevel's "strictest of
any subcommand" — exposed as `statement_lock_mode()`. (5) CALIBRATED means
verified-against-docs/source AND the kind occurs in the wild; UNCALIBRATED stubs still carry
real citations where the doc states the behavior plainly (VACUUM FULL), but I didn't burn
research effort on partition DDL/MERGE/COPY/savepoints beyond transcription.

Where the docs were ambiguous enough to need a call: **COMMENT ON** — explicit-locking.html
says SHARE UPDATE EXCLUSIVE with no per-object-class qualification; comment.c shows the lock
is taken via get_object_address on the *relation* only for relation-attached objects, so
comments on functions/schemas lock nothing — the row's target is optional and the note explains
the split. **DROP INDEX's table lock** appears nowhere in explicit-locking.html or
sql-dropindex.html; the honest citation is index_drop's comment plus a sentence hiding in
sql-reindex.html's Notes. **ALTER TABLE docs say "An ACCESS EXCLUSIVE lock is acquired unless
explicitly noted" but the notes are incomplete** — DISABLE/ENABLE RULE has no note yet is
AccessExclusiveLock in source while the trigger forms' SRE is documented; identity and
SET DEFAULT forms likewise ride the default sentence and are only pinned by
AlterTableGetLockLevel. **ALTER SEQUENCE**: no doc statement of the SRE lock at all
(source-only citation; PG 10's "Fix ALTER SEQUENCE locking" behavior). **GRANT before 18 takes
no relation lock whatsoever** (`RangeVarGetRelid(..., NoLock)`) — surprising enough that I
diffed objectNamesToOids across 10/12/14/15/16/17/18 to find where it changed (18, not
mentioned in any doc page I found; cited by source diff). **DETACH PARTITION's plain-form lock**
is stated nowhere for the parent in older docs; PG 12's page only mentions the FK SHARE locks —
source cited. **CREATE OR REPLACE VIEW vs CREATE VIEW**: the classifier doesn't record
OR REPLACE, so create_view models the plain form and the note carries the replace caveat — a
taxonomy gap worth fixing when the verdict layer needs it, same for COMMENT ON target decoding
and DROP POLICY/DROP TRIGGER table names (roles exist but are unnamed today).

## Live introspection layer (2026-08-20)

Built `blastoise.live`: read-only production introspection that supplies what the catalog's
`requires_live_context` rows and `duration_model` fields declare missing. `capture_snapshot(dsn,
relations)` returns a typed, JSON-serializable `LiveSnapshot` — per-relation facts (existence,
reltuples/relpages with staleness from `pg_stat_all_tables`, `pg_relation_size`/total, partition
count and summed size via a recursive `pg_inherits` CTE, per-index sizes and `indisvalid`),
concurrency facts (waiters on the targets from `pg_locks` + `pg_blocking_pids` with modes on both
sides, long transactions with idle-in-transaction distinguished, client-backend count vs
`max_connections`), replication facts (existence, per-replica byte and interval lag, sync mode),
and server facts (`server_version_num`, configured `lock_timeout`/`statement_timeout`,
`pg_is_in_recovery`, server clock for staleness math). Every independently-failable field is a
`Fact[T]` — `{available, value, reason}` — so downstream always distinguishes known-false from
unknown; `Fact.of(None)` (e.g. never analyzed) is deliberately different from
`unavailable(reason)`. Serialization is canonical: sorted keys, compact separators, tuples sorted
at capture, ISO-UTC strings (session forced to `TimeZone=UTC`), and **no floats anywhere** — the
serializer raises on one; every `EXTRACT(EPOCH ...)` is `::bigint`-cast server-side to integer
milliseconds and `reltuples` to bigint, because psycopg would otherwise hand back Decimals/floats
that hash unstably. The read-only stance is enforced, not assumed: `default_transaction_read_only`
in the startup packet, an explicit `READ ONLY` transaction verified via `transaction_read_only`,
and a privilege gate that refuses (WritableRoleError) superusers, roles holding any
INSERT/UPDATE/DELETE/TRUNCATE on user relations, and CREATEDB/CREATEROLE — while schema/database
CREATE (the pre-15 `public` default), REPLICATION, and BYPASSRLS demote to recorded warnings,
since failing every default PG≤14 database over `public` CREATE would make the tool unusable and
none of those can modify existing rows. Sections run in savepoints (psycopg nested transactions)
so one failure can't abort the rest. Docs: `docs/minimum-privilege-role.md` — the whole ask is
three statements (`CREATE ROLE ... NOSUPERUSER ...; GRANT pg_monitor; GRANT CONNECT`), pitched
against what Datadog/pganalyze already require. Tests: 66 new (482 → 548 total; the live layer at
99% branch coverage, missing only the two guards that need a pre-PG10 server) — a shared harness
(`tests/live_harness.py`) that prefers testcontainers, falls
back to `BLASTOISE_TEST_DSN`, then to `BLASTOISE_TEST_PG_BIN` local binaries (this machine has no
Docker; the suite ran for real against zonky PG 17.10 binaries — full run 14s), plus a scripted
fake connection for branches a healthy single-node server can't produce (replica rows, per-section
timeouts, pre-14 waiters, masked query text). The staged degraded paths all assert
marked-unavailable-not-exception: no-pg_monitor role, no replicas, never-analyzed table, missing
relation, unparsable name, and a live ACCESS EXCLUSIVE conflict from a second connection.

Facts that turned out unavailable or unreliable in practice: (1) **`pg_settings.reset_val` lies if
you set your bounds in the startup packet** — options passed via `-c` in the connection string
become the session's RESET target, so my own 5s statement_timeout came back as "the server's
configured value"; caught by a test, fixed by applying bounds with session-level `SET` instead,
and only then does reset_val mean server/db/role config. (2) **`pg_relation_size` takes ACCESS
SHARE on the relation** and queues behind an ACCESS EXCLUSIVE holder — the introspection itself
would hang on exactly the contended table it's assessing; `lock_timeout` turns that into an
explicit `unavailable: lock not acquired` marker, and the queries are split so pg_class facts
(no relation open) survive while size facts degrade — the lock-conflict test asserts both halves.
(3) **Without pg_read_all_stats, `pg_stat_activity` masks other roles' rows entirely**
(xact_start included, not just query text), so "long transactions" silently gathered as an empty
list would be indistinguishable from "none" — the whole field goes unavailable instead, and the
same masking forces connection-count to degrade rather than miscount from NULL backend_types.
(4) **reltuples=-1 ("never analyzed") exists only on PG 14+**; below that a never-analyzed table
reads 0 and cannot be told apart from an empty one — the snapshot preserves -1 as
`unavailable("never vacuumed or analyzed")` and lets last_analyze/last_autoanalyze (known-null)
carry the staleness story on older versions. (5) **`to_regclass` stopped raising on syntactically
invalid names in PG 16** — garbage now resolves to a clean `exists=false`, older servers take the
error path; the test accepts both. (6) `pg_locks.waitstart` is PG 14+, NULL lag columns in
`pg_stat_replication` mean "caught up", not "unknown", and byte lag is uncomputable on a server
in recovery (`pg_current_wal_lsn()` errors) — each is its own marked state. Windows harness
gotcha for the record: `pg_ctl start` under `subprocess.run(capture_output=True)` hangs forever
because the daemonized postmaster inherits the stdout pipe; DEVNULL fixes it.

What the catalog declares it needs that this layer cannot supply (relation-level scope was the
brief; these are the next introspection increment, all column/constraint/object-level): the
current type of a column (binary-coercible ALTER TYPE fast path) and whether that type is a
constrained domain; `pg_proc.provolatile` of user functions in defaults; whether a valid
`CHECK (col IS NOT NULL)` exists (SET NOT NULL scan skip) and per-column `attnotnull` for ADD PK
USING INDEX; a named constraint's `contype`/`convalidated` (VALIDATE/DROP CONSTRAINT rows need FK
vs CHECK to know about referenced-table locks); whether an ATTACH'd table's CHECK implies the
partition bound and whether a default partition exists; extension install/update scripts;
procedure bodies; the table lists behind REINDEX SCHEMA/DATABASE. Also honest limits of what it
does supply: reltuples is an estimate even fresh (row-count-driven duration verdicts must carry
the staleness fields with them), "table is empty" is evidenced (relpages=0, size=0) but not
proven, and the snapshot is not transactionally consistent across sections — each savepoint
query sees its own instant, fine for evidence, wrong for an audit log.

## Column, constraint, and type introspection (2026-08-20)

Second introspection increment, extending `capture_snapshot` (same call, same Fact discipline,
same canonical serialization; `SNAPSHOT_FORMAT` 1 → 2): per-relation **ColumnFacts** (name,
`format_type` with typmod, `attnotnull`, `atthasdef`, default source via `pg_get_expr`,
`attidentity`/`attgenerated` — version-gated to `''` before 12 — and the column's-type-is-a-domain
facts) plus `dropped_column_count` (rewrite cost); **ConstraintFacts** (`contype`, `convalidated`,
conkey columns in index order, `pg_get_constraintdef`, bare CHECK expression, FK referenced table
resolved to schema.name — the fact VALIDATE/DROP CONSTRAINT rows declare missing); and three new
probe parameters: `functions=` → **FunctionFacts** from `pg_proc.provolatile` (an unqualified name
matches *every* schema since the migration session's search_path is unknowable — decided only when
all same-named functions agree, otherwise unavailable with the disagreement in the reason),
`types=` → **TypeFacts** (the constrained-domain question for ADD COLUMN, domain chain walked),
and `type_changes=` → **TypeChangeFacts** (both sides reduced through domains, `pg_cast.castmethod`
looked up on the base pair — live pg_cast, so extension casts like citext are covered without a
hardcoded pair list). Volatility wiring: `DefaultInfo.unknown_functions` now records the dotted
names behind an UNKNOWN (only then — a decided volatility has nothing to ask);
`volatility.expression_volatility` takes an optional resolved-name mapping consulted *only* where
the static answer is UNKNOWN (a live fact can never override an allowlist answer, and a lying
mapping cannot demote `nextval`); `live.decide_default_volatility(default, snapshot)` re-parses the
deparsed expression and decides — offline, or when overloads disagree, it stays UNKNOWN.

**The rewrite lookup** (`live/typechange.py`): `assess_type_change(facts, pg_major, has_using)` →
NO_REWRITE / REWRITE / NO_REWRITE_IF_SESSION_TZ_UTC / UNKNOWN, every rule cited (doc quote,
source function, or PG12 release note + commit 3c59263) and every rule additionally verified
against **relfilenode ground truth on the real PG 17.10** — a test creates the table, captures the
probe, assesses, then actually ALTERs and compares. Where the docs and the implementation
disagree, the verified implementation won, and the surprises were real: (1) base type →
*unconstrained* domain is free — the docs' exception lists only old-type-is-a-domain, but
`ATColumnChangeRequiresRewrite` skips `CoerceToDomain` iff `DomainHasConstraints` is false;
(2) a CHECK or NOT NULL *source* domain dropping to its base is also free (constraints are
dropped, not checked); (3) bare `timestamp` → `timestamp(6)` is free because bare means
precision 6, while bare `varchar` → `varchar(10)` rewrites because bare varchar is truly
unbounded — the "no typmod" default differs per type family and the growth rules encode it;
(4) the PG 12 UTC mechanism is *not* a prosupport function (the tz cast functions have
`prosupport = '-'`); it lives in `TimestampTimestampTzRequiresRewrite` called from tablecmds, and
the verdict is deliberately conditional — the *migration session's* TimeZone decides, so
`ServerFacts.timezone` carries the configured zone as evidence, not proof. Judged worst-case on
purpose: any USING expression (PG avoids the rewrite for a provably-no-op `USING c`, but that is
not statically provable), interval typmods (range mask not modeled), bpchar length changes
(re-padding; verified: `char(10)→char(20)` rewrites), domain-with-typmod combinations.

**Capture ordering fixed** (it was wrong): sizes ran before the pg_locks capture, so our own
timed-out `pg_relation_size` ACCESS SHARE attempts — and granted size locks on uncontended
relations — could surface as waiters/blockers in the very snapshot assessing the contention. New
contract, documented and tested: resolution → all lock-free facts (columns/constraints included:
`pg_get_expr`/`pg_get_constraintdef` deparse from syscache without opening the relation) →
pg_locks → sizes last; `pg_backend_pid()` excluded in both pg_locks queries *and* stripped from
`pg_blocking_pids` output. Regression test: an ACCESS EXCLUSIVE holder and nobody else — sizes
degrade, columns survive, and the waiter list is exactly `()`. Related trap resurfaced:
`TimeZone=UTC` sat in the startup packet since the first increment, which would have poisoned
`pg_settings.reset_val` for the new timezone fact exactly like the timeouts did — moved to
session-level `SET`, with a test that pins reset_val to an `ALTER DATABASE ... SET TimeZone`
value while the session runs UTC.

**Privileges: zero new.** Everything this increment reads (`pg_attribute`, `pg_attrdef`,
`pg_constraint`, `pg_proc`, `pg_type`, `pg_cast`) is world-readable catalog — it needs not even
pg_monitor, which only unmasks activity/replication. Tested explicitly: a
`TestNoNewPrivilegesRequired` capture as the no-pg_monitor role gets every new fact. The
three-statement role grant is untouched; the doc now says so.

**Corpus re-run** (the justification): 3,081 files / 15,514 statements, zero resolve errors.
2,837 statements resolve to ≥1 `requires_live_context` row. 1,500 were already decidable with
increment 1 alone (row counts, sizes, emptiness). **1,289 statements move from
"requires_live_context, unresolvable" to "decided" with this increment** (stable across PG 13–18;
1,166 on PG 11, where the SET NOT NULL scan-skip doesn't exist yet — the registered breakpoint):
add_column_default_nonvolatile 728 (the domain question), alter_column_type 306, drop_constraint
272 (FK-or-not decides the referenced-table lock), set_not_null 172, unknown-volatility defaults
8, ADD PK USING INDEX 2, validate_constraint 1. All 15 wild unknown-volatility default statements
(3 distinct user functions: `generate_unique_changeme` ×13, `wt_to_sentinel`, `random_smallint`)
are now decidable given a connection. Remaining open: **48 statements** — create_extension 19
(install scripts), update_batched 14 (key-window width in rows), alter_domain 10 (IR gap + the
domain-dependent-table list), set_storage_params 4 (IR gap: option names), opaque SELECT/CALL 3.
Still declared-but-unsupplied for later increments: extension install/update scripts, procedure
bodies, REINDEX SCHEMA/DATABASE table lists, default-partition existence (constraint facts now
supply the attached table's CHECKs, but bound-implication is a verdict-layer judgment and the
default partition's existence is not captured), domain-dependent tables for ALTER DOMAIN.

Suite: 548 → **633 tests**, 98% total branch coverage (live modules 99–100%; introspect's two
remaining misses are the same pre-PG10 guards as before), ruff and mypy --strict clean, full run
against real zonky PG 17.10 binaries in 68s (re-downloaded from Maven Central — the previous
session's copy did not survive its scratchpad). Honest limits, recorded in docstrings too:
unqualified function matching is deliberately schema-broad, so a same-named function in an
unrelated schema blocks a decision (unavailable, never a guess); `ColumnFacts.domain_constraint_count`
counts only the domain's own CHECKs while TypeFacts/TypeChangeFacts walk the chain; `to_regtype`
raises on garbage before PG 16 (handled like `to_regclass`, savepoint → unavailable); and column
default *expressions* are captured (schema DDL, unlike the never-captured query text of other
sessions) — a default containing a secret literal would be a schema smell, but the line is worth
knowing.

## The risk engine (2026-08-21)

Built the verdict layer: `blastoise.verdict` — `assess_script(script, catalog, pg_version,
snapshot=None)` combines the parsed IR, the lock catalog, and an optional live snapshot into
per-statement assessments: the locks per affected relation and what each blocks (conflict sets
spelled out), a duration estimate with a confidence interval **or** an explicit
`CannotEstimate(reason)`, currently observable contention, reversibility with the exact loss
named, and a SAFE / CONDITIONALLY_SAFE / UNSAFE / UNKNOWN classification. Every conclusion type
(`Verdict`, `DurationEstimate`, `CannotEstimate`, `Tristate`, `ReversibilityAssessment`,
`RelationLockAssessment`) has `method` as a required constructor field with no default —
PROVEN (grammar + catalog), OBSERVED (read from the snapshot), SIMULATED (stats + duration
model), UNVERIFIED — so constructing a conclusion without stating its evidence class is a
TypeError, and a test walks every dataclass the engine emits asserting the tag. A conclusion
combining several evidence classes gets the **weakest contributor's** tag (a SAFE built from
catalog facts plus a simulated duration is SIMULATED, not PROVEN); UNKNOWN is always
UNVERIFIED. `snapshot_probes(script)` derives what to ask `capture_snapshot` for, and its
`probe_name()` quotes non-lowercase identifiers so Prisma-style `"User"` tables resolve on the
server — the engine looks all snapshot facts up by that same spelling. No LLM anywhere.

The three mandated constraints, as implemented: (1) the catalog's worst-case-with-live_context
convention inverts at the verdict boundary — a `requires_live_context` row whose declared fact
the snapshot could not supply (or with no snapshot at all) is UNKNOWN with the `live_context`
text as the reason, enforced by a single code path with no bypass; offline, the only reachable
UNSAFE is a proven failure (a FORBIDDEN statement inside an explicit transaction or DO block —
it errors, which is a fact of grammar, not of size). (2) No fallbacks: UNCALIBRATED catalog
rows are UNKNOWN even when their values look plausible (this makes ATTACH PARTITION, VACUUM,
MERGE, COPY et al. UNKNOWN by construction — the narrower I had written for ATTACH was dead
code behind that rule and was deleted); a duration model with a missing input refuses rather
than substituting (`reltuples` swamped by `n_mod_since_analyze` — more than 10x the estimate —
refuses; reltuples=0 on a never-analyzed table refuses, because pre-14 that is
indistinguishable from emptiness). (3) Severity comes from lock x duration x size, never from
the kind: the test pins plain CREATE INDEX at SAFE on 200 rows, UNSAFE on 40M, UNKNOWN with no
snapshot, and the same for a rewriting ADD COLUMN.

**Duration model and its invented constants.** The model is `rows / throughput` (or
`bytes / throughput` for REINDEX INDEX) with the interval widened by calibration state and
statistics staleness — staleness widens, never shifts the point, so a stale estimate stays
SIMULATED with reduced confidence instead of quietly becoming wrong. Classification bands on
the interval's **upper** bound (worst plausible hold), so stale stats push borderline
statements to the stricter class. The single table prompt 8's calibration loop will overwrite
is `blastoise.verdict.constants.DURATION_CONSTANTS`; every entry is `UNCALIBRATED` and every
basis starts with "guess:" (a test enforces both). What I invented and what it is based on:

- `heap_rewrite` 100,000 rows/s — rewrites read the heap, write a new one, WAL-log all of it,
  and rebuild every index; public war stories put full rewrites at ~1-5 GB/min on cloud disks
  (~100-500k rows/s at ~150 B/row); low end taken.
- `validation_scan` 500,000 rows/s — read-only seqscan plus one cheap predicate per row.
- `fk_validation` 100,000 rows/s — per-referencing-row probes into the referenced table's
  index; ~5x slower than a plain scan.
- `index_build` 250,000 rows/s — sort-dominated; ~1M pgbench rows build in a few seconds
  cached in public benchmarks.
- `dml_update` 50,000 rows/s — new tuple + index maintenance + WAL per row; slowest family.
- `dml_delete` 100,000 rows/s — marks dead without writing new versions; ~2x update.
- `index_bytes` 50 MB/s — REINDEX reads/sorts/rewrites index-sized data on network storage.
- `constant_op` 10 ms fixed (interval 1-100 ms) — catalog rows plus one WAL flush; commit
  latency dominates the spread.
- Thresholds: full block (reads+writes stall) 2 s / 20 s, write-only block 5 s / 60 s — below
  the first bound SAFE, between them CONDITIONALLY_SAFE, above UNSAFE; based on p99 retry
  budgets and health-check conventions, not measurement.
- Widening: x4 each way for an uncalibrated constant (x1.5 once calibrated), x10 for
  constant-time ops; analyze-age x1.0/x1.5/x2.0/x3.0 (day/week/month/older, x3.0 when the age
  is unknowable), churn 1+n_mod/rows capped at x5, x2 when n_mod is masked. Confidence label:
  high <=2x, medium <=6x, low above — with everything uncalibrated, "high" is unreachable
  today, which is the honest state.

Judgment calls worth recording: **brief ACCESS EXCLUSIVE is CONDITIONALLY_SAFE, never SAFE**
(the work is constant but the acquisition queue poisons everything behind it; the condition
says lock_timeout + retry), while brief write-only blocks (CREATE TRIGGER's SHARE ROW
EXCLUSIVE) are SAFE with a note — the wild corpus has 1,007 CREATE TRIGGERs and calling them
all conditional would be noise. **Irreversible statements are never SAFE** — floored to
CONDITIONALLY_SAFE with the loss as the condition; consequence: all 389 wild
`ALTER TYPE ADD VALUE` are conditional because an enum label can never be dropped, and every
UPDATE/DELETE carries its lost-values condition. The floor is skipped when every locked
relation was created earlier in the same file (dropping a table you just created loses
nothing). **File-local safety**: a statement whose certain relations were all created earlier
in the file is SAFE/PROVEN regardless of duration model — nothing production-visible can
block — with created-state tracked as "clean" (VALUES-only inserts) vs "loaded"
(CTAS/INSERT-SELECT: still invisible to traffic, but duration honestly `CannotEstimate`); this
plus `baseline_shaped` (treat every relation as file-created) is what keeps squash files out
of the UNKNOWN bucket. **Matched-row DML never reaches UNSAFE** — matched rows are bounded by
the table, so the worst case is stated as a condition (with a batching suggestion for
unbatched forms), but the actual count "is not knowable; do not guess" per the catalog;
`*_WITHOUT_WHERE` forms, where every row provably matches, do band up to UNSAFE.
**INSERT ... SELECT / CTAS / CREATE MATVIEW are CONDITIONALLY_SAFE** (non-blocking but
unbounded source volume: long transaction, vacuum horizon, WAL burst). **Existence-guarded DO
statements cap at CONDITIONALLY_SAFE both ways** per the brief — a guarded SAFE may not
execute, a guarded UNSAFE records "if the guard passes: UNSAFE — ..." in its conditions;
guarded UNKNOWN stays UNKNOWN. **Observed contention escalates one step** (SAFE to COND, COND
to UNSAFE) only on a positive pg_locks observation; because holders are visible only when
someone waits, the no-waiters answer is an explicit tri-state None ("cannot be ruled out"),
never False. **Cumulative transactions**: statements carry `held_locks_before`; a long
statement running while earlier AELs are held escalates SAFE to CONDITIONALLY_SAFE; a
transaction accumulating AEL on 2+ named relations emits a `TransactionWarning` (72 real ones
in the wild corpus), and files with no explicit transactions get the same computed
hypothetically "if your runner wraps this file" (354 files) plus a must-not-wrap note when the
file contains FORBIDDEN statements. Narrowings wired to live facts: unknown-volatility
defaults through `pg_proc` (then into the domain question), the ADD COLUMN constrained-domain
fast path, `assess_type_change` for ALTER COLUMN TYPE (a proven pure relabel also upgrades
reversibility to REVERSIBLE/OBSERVED; the timestamp-timezone verdict becomes a TimeZone=UTC
condition), SET NOT NULL proving-CHECK (syntactic conjunct match, deliberately narrower than
`NotNullImpliedByRelConstraints` — a miss errs toward "the scan runs"), VALIDATE/DROP
CONSTRAINT contype (FK adds the referenced table at ROW SHARE / ACCESS EXCLUSIVE;
already-validated is a proven no-op), ADD PK USING INDEX (all-columns-NOT-NULL check, else
keep the catalog's deliberate best-case model with the caveat), SET EXPRESSION (STORED vs
VIRTUAL from `attgenerated`).

Tests: 633 to **753**, 97% total branch coverage (verdict modules 90-100%), ruff and mypy
--strict clean, full run against real zonky PG 17.10 in ~3 min. The three mandated invariants
are pinned directly: removing the snapshot moves size-dependent statements to UNKNOWN and
never to UNSAFE (offline UNSAFE only for proven failures), the same CREATE INDEX classifies
three ways by table size, and no conclusion type constructs without a method tag.

### Corpus classification distribution (offline vs online replay)

Both runs over the same 3,081 files / 15,514 statements at PG 17 (3 known Discourse extraction
artifacts fail to parse; their files are skipped).

**Offline** (no snapshot): SAFE **8,659** (55.8%) / CONDITIONALLY_SAFE **3,355** (21.6%) /
UNSAFE **0** / UNKNOWN **3,500** (22.6%). Methods: proven 12,014, unverified 3,500. Offline
UNSAFE is zero by design. 3,698 statements are SAFE via file-local reasoning (10 files
baseline-shaped). UNKNOWN concentrates exactly where size or state is missing: alter_table
1,503, plain create_index 944, matched UPDATE 596, update/delete_without_where 196, opaque DO
blocks 55, create_extension 15, alter_domain 10 (IR gap), reindex-with-scope 11.

**Online** was a real replay, not a synthetic snapshot: each repo's chain ordered by manifest
number into a fresh database on the embedded PG 17.10 — for every file, derive probes, capture
a read-only snapshot of the database as it stood *before* that file, assess online, then apply
the file statement-by-statement as superuser (autocommit, so CIC works; missing roles created
on demand and retried once; errors roll back any open transaction and the chain continues).
12,843 of 15,514 statements applied (83%); coder (573 files), mattermost, marquez, and zed
replayed with **zero** failures, while discourse/zulip (independent extracts, not chains)
mostly cannot apply — their later files honestly see missing relations. Zero capture failures
in 3,078 captures; 10m19s wall clock. Result: SAFE **8,728** (56.3%) / CONDITIONALLY_SAFE
**4,266** (27.5%) / UNSAFE **0** / UNKNOWN **2,520** (16.2%). Methods: proven 12,043,
observed 725, simulated 226, unverified 2,520. The snapshot decided **980 statements** that
were UNKNOWN offline — mostly into CONDITIONALLY_SAFE (alter_table 949 to 1,718 conditional:
the domain question resolving to "fast path, brief AEL", proving-CHECK misses resolving to
"scan on a small table", FK contypes resolving the referenced-table lock) and some into SAFE.
Remaining online UNKNOWN is dominated by relations genuinely absent from the replay database
(broken chains and partial extracts: create_index 843, alter_table 728, matched UPDATE 529)
plus the honestly-unknowable tail (dynamic-SQL DO blocks 50, create_extension 15, alter_domain
10, REINDEX SCHEMA 10, CALL 1). UNSAFE is zero online too, and that is honest rather than
lenient: replay databases are near-empty (migrations create schema, not data), so no
size-driven hazard can trigger — the corpus also contains no proven-failure forms. UNSAFE
needs production-sized tables, which only a snapshot of a real production database can supply.

Caveats recorded: the replay's "online" distribution answers "what would blastoise say against
an empty-but-schema-correct staging database", not against production; temporal's corpus mixes
overlapping schema variants ("already exists" failures), calcom has environment-dependent data
migrations that abort mid-file and degrade its chain; per-statement autocommit apply is harsher
than Prisma/Flyway's per-file transactions, so a mid-file failure leaves half-applied state
the real runner would have rolled back. Scripts and full JSON reports
(`corpus_offline_results.json`, `corpus_online_results.json`, per-repo apply stats included)
retained in the session scratchpad.

## UNKNOWN diagnosis and production-scale validation (2026-08-21)

Two experiments before building the verdict document layer: (1) an instrumented re-run of the
online replay attributing every UNKNOWN to a cause, (2) a scale harness measuring real lock
modes and hold durations at 1k/100k/1M/10M rows beside the engine's predictions. Scripts and
full JSON retained in the session scratchpad (`diagnose_unknown.py`/`unknown_diagnosis.json`,
`scale_harness.py`/`scale_results.json`, `analyze_scale.py`). No constants were changed.

### Part 1 — the online UNKNOWN rate is ~97% replay artifact

Method: the same replay, but each UNKNOWN's rationale is bucketed by cause, and every file is
assessed a second time after `ANALYZE` of its probed relations, so the stats-freshness share is
a measured per-statement delta. (Honesty note: those ANALYZEs persist, so this run's own
"vanilla" count, 1,081, is already partially decontaminated — the clean comparison is the
original replay's 2,520 vs the stats-fresh 747.) Attribution of the original 2,520 (16.2%):

- **~1,773 (70%) — statistics freshness, replay artifact.** Never-analyzed tables: migrations
  create schema faster than autovacuum analyzes it, `reltuples=-1`, and the duration model
  correctly refuses. Post-ANALYZE the same statements decide (this run's never_analyzed bucket:
  335 → 222 SAFE + 112 CONDITIONALLY_SAFE + 1 residue). Swamped-statistics refusals: **zero**.
  Production keeps this bucket only for tables created moments before being indexed/backfilled
  in the same deploy window — real, but a corner, not 70% of statements.
- **~680 (27%) — schema absence, replay artifact.** "does not exist on the target database"
  (580: update 207, alter_table 104, create_index 102, delete 90 — discourse alone 295, being
  an independent extract, not a chain), plus ~100 live-context failures whose reasons are the
  same cause in disguise ("rewrite undecidable: relation or column not found:
  job_run.server_id"; "whether 'wh_dim_key' is a domain is unknown" for types a broken chain
  never created).
- **~67 (2.7%) — engine-inherent** (0.43% of 15,514 statements): extension install scripts 16,
  ALTER DOMAIN IR gap 10, storage-params IR gap 4, CALL 1, DO blocks with dynamic SQL or no
  analyzable body 26, and a 10-statement corner worth remembering: ADD FOREIGN KEY onto a table
  the same file bulk-loaded — file-local safety doesn't apply because the FK also locks the
  pre-existing referenced table, and the loaded table's volume is honestly unknowable. This
  reconciles with the introspection increment's "48 truly undecidable" (same categories,
  statement-level counting; update_batched became CONDITIONALLY_SAFE via the worst-case bound
  once its relation exists). UNCALIBRATED-stub UNKNOWNs: zero — the 41 stubbed kinds genuinely
  never occur in migration files.

Against a schema-complete, stats-fresh database: SAFE 9,847 (63.5%) / CONDITIONALLY_SAFE 4,920
(31.7%) / UNSAFE 0 / UNKNOWN 747 (4.8%), and 91% of that residual UNKNOWN is the schema-absence
artifact. The engine's honest UNKNOWN floor is ~1% of statements.

### Part 2 — scale harness

Fresh PG 17.10, fsync=on, synchronous_commit=on, shared_buffers 512MB, maintenance_work_mem
256MB, autovacuum off (explicit VACUUM/ANALYZE so statistics state is controlled); local NVMe
laptop, single session, zero concurrency — throughputs here are ceilings relative to contended
cloud disks. Four tables with identical schema (14 mixed-type columns, PK + 3 secondary
indexes, one partial; a 10k-row `accounts` FK target; a pre-seeded NOT VALID FK; a valid
`CHECK (body IS NOT NULL)`; a constrained domain), seeded and ANALYZEd at 1k/100k/1M/10M rows
(10M heap: 3.0 GB, ~300 B/row incl. jsonb). 33 cases, each adapted from a recorded wild-corpus
statement, weighted per the frequency distribution: 6 CREATE INDEX shapes (single/composite/
expression/partial/GIN/unique), 4 FK forms (plain, cascade, NOT VALID, VALIDATE), 7 ALTER
COLUMN TYPE (int→bigint, text→varchar(n) rewriting; varchar widen, varchar→text,
numeric-widen, ts→tstz-under-UTC non-rewriting), SET NOT NULL ×2 (with/without proving CHECK),
ADD CHECK, ADD UNIQUE, 7 ADD COLUMN variants (bare, constant default, volatile, serial,
identity, generated stored, constrained-domain default), REINDEX INDEX/TABLE, UPDATE ×3 (two
matched, one unbatched), DELETE without WHERE. Per case × size: snapshot → online assessment →
BEGIN → execute (wall-clocked) → pg_locks sampled at 25 ms plus one definitive sample while the
transaction is still open → relfilenode rewrite check → ROLLBACK (rewrites leave nothing;
VACUUM ANALYZE after DML cases). One seeding trap worth recording: `NOT VALID` inside CREATE
TABLE is silently created already-validated — the constraint must be ADDed after the rows
exist, or VALIDATE measures a no-op (caught by the smoke run's 0 ms "already validated"
narrowing).

### Part 3 — results

**Classification distribution (33 cases each; UNKNOWN zero everywhere — schema-complete and
stats-fresh):** 1k: 23 SAFE / 10 COND / 0 UNSAFE · 100k: 15 / 18 / 0 · 1M: 3 / 22 / **8** ·
10M: 2 / 10 / **21**. UNSAFE appears exactly where expected and grows with size; every case's
class moves monotonically stricter with size (e.g. plain CREATE INDEX safe→safe→cond→unsafe;
every rewriting form safe→cond→unsafe→unsafe; the fast-path forms stay flat). Severity is
wired to the size facts for real, not just in the unit test. The measured reality behind the
10M UNSAFEs is sobering enough to justify the label: unbatched UPDATE held 10M row locks for
**449.5 s** (plus 103 s of vacuum debt), int→bigint blocked reads+writes 106.5 s, volatile-
default ADD COLUMN 169.7 s.

**Lock modes: 124/124 correct.** Zero mismatches between the catalog's statement lock mode and
the strongest mode observed in pg_locks, every size. The FK referenced-table claim is observed
for real: ADD FK held SHARE ROW EXCLUSIVE on `accounts` simultaneously with the target's SRE;
REINDEX INDEX held ACCESS EXCLUSIVE on the index with SHARE on its table. Rewrite ground truth
(relfilenode): all 13 checked forms match the typechange layer's verdicts at every size —
including numeric(12,2)→(14,2) and ts→tstz-under-UTC not rewriting, and constrained-domain
ADD COLUMN rewriting.

**Interval coverage: 91/124 measured durations inside the predicted interval** (73%); 27 below
the low bound (over-prediction — the safe direction), 6 above (under-prediction — the dangerous
direction). Median interval width high/low is 16x; median high/measured 7.7x. All 6 dangerous
misses have exactly two causes: (a) three are `reindex_index` (the index_bytes guess is ~5x
optimistic), and (b) three are **the one real severity bug found: the ALTER TYPE no-rewrite
narrowing ignores dependent-index rebuilds.** varchar(32)→varchar(64) on a column carrying the
partial index narrows to "no-op coercion, constant time ≤100 ms" — the heap is indeed not
rewritten, but Postgres rebuilds the dependent index under ACCESS EXCLUSIVE (index) + SHARE
(table) while the statement holds table AEL: measured 255 ms / 1.12 s / **13.5 s** at
100k/1M/10M. Unindexed no-rewrite columns (numeric, varchar→text) measure 0-4 ms as promised.
Fix direction for later: the CONSTANT narrowing must require no dependent index on the column,
else model an index_build. Three of the LOW misses are by design, not error: ts→tstz's interval
describes the rewrite branch of a conditional verdict while the UTC session took the no-rewrite
branch (0-3 ms).

**The eight constants, guess vs measured** (median of 1M+10M cases; conditional no-rewrite
cases excluded):

| constant | guess | measured | verdict on the guess |
|---|---|---|---|
| heap_rewrite | 100k rows/s | **~100k rows/s** (59k volatile-default … 125k) | spot on |
| validation_scan | 500k rows/s | ~1.2M rows/s | 2.4x conservative |
| fk_validation | 100k rows/s | **1.2M-4.5M rows/s** | **12-45x wrong** — RI_Initial_Check is one join, not per-row probes; consequence: ADD FK banded UNSAFE at 10M (400 s worst case) yet measured 2.8 s |
| index_build | 250k rows/s | bimodal: **0.9-1.2M** plain btree, **210-280k** expression/GIN | 4-5x conservative for btree; right for expression/GIN — one constant hides a real split |
| dml_update | 50k rows/s | **22-29k rows/s** | 2x optimistic — the wrong side, on the family behind unbatched backfills (saved only by the 4x widening) |
| dml_delete | 100k rows/s | 166k (10M, cold) - 900k (1M, cached) rows/s | 1.7x conservative at scale, cache-sensitive |
| index_bytes | 50 MB/s | **~9.5 MB/s** | **5x optimistic** — the wrong side; source of the reindex_index misses |
| constant_op | 10 ms (1-100) | median 3 ms, honest max 41 ms | fine (the 13.5 s outlier above is the narrowing bug, not this constant) |

Both badly-wrong constants err in opposite directions with opposite product costs:
fk_validation's pessimism produces false UNSAFEs on the second-most-common wild migration form;
index_bytes' and dml_update's optimism under-predicts real blocking. Calibration (prompt 8)
has its table; nothing was adjusted here per the brief. Caveat for that calibration: these
numbers are one uncontended NVMe machine — the shapes (bimodality, join-vs-probe) transfer,
the absolute rates only bound production from above.

## Acting on the harness: the severity bug, measured constants, classification-first output (2026-08-21)

Three changes in the priority the Part 2/3 results demanded, then a full re-run of the same
harness. Both mandated outcomes landed: **all six dangerous misses are gone** and **the false
UNSAFEs on ADD FK at 10M cleared**. Suite: 753 → 770 tests, 96% total branch coverage
(verdict modules 90–100%, introspect 99%), ruff and mypy --strict clean.

### 1. The dependent-index rebuild bug — fixed, with a reuse rule the docs don't state

Before touching the narrowing I ran a relfilenode ground-truth experiment on the same PG 17.10
(1M rows, six index shapes, `reuse_experiment.py` in the session scratchpad), because the
blanket fix — "model a rebuild whenever any index sits on the column" — would have manufactured
false UNSAFEs in the opposite direction. The result is a sharp rule: **a no-rewrite ALTER
COLUMN TYPE rebuilds only the dependent indexes `CheckIndexCompatible` refuses to reuse.**
Plain-key btrees are reused in ~0 ms — even `varchar_pattern_ops` on a typmod widen, even
varchar→text — while **partial and expression indexes always rebuild** (765/827 ms at 1M,
exactly the btree build rate; the harness's 13.5 s at 10M was the partial index on `status`).
Implementation: `IndexFacts` grew `method`, `partial`, `has_expressions`, `default_opclasses`,
and `depends_on_columns` (SNAPSHOT_FORMAT 2 → 3; zero new privileges — pg_index/pg_depend/
pg_opclass/pg_am are world-readable and the query stays in the lock-free stage). The
dependency set is the **union of `indkey` and pg_depend's column-level entries** — pg_depend
alone lost a real test to the PK trap (a constraint-backed index depends on its *constraint*,
not the columns), while indkey alone misses predicate/expression-only columns like the partial
index's `is_active`. The narrowing now returns CONSTANT only when every dependent index is a
plain-key default-opclass btree (non-default opclasses count as rebuilds: reuse is only proven
where the opclass carries), else models one `index_build` per rebuilt index. Judgment call
against the brief's letter: the rebuild cost is **rows-based, not index-bytes-based** — the
snapshot's per-index sizes identify and evidence the rebuilds, but btree deduplication makes
final index bytes a poor proxy for the heap-scan-and-sort work (the 73 MB account_id index at
10M is ~7 bytes/entry; a bytes model would have under-predicted the 13.5 s rebuild and kept
the dangerous miss alive). The relabel-reversibility upgrade survives either way: a rebuild
changes no values.

### 2. Constants: two remeasured, one split, and a fixed-overhead floor

`Calibration.MEASURED` was added between UNCALIBRATED and CALIBRATED (the loader rejects it in
catalog rows — it belongs to duration constants only, with the harness date in the basis):

- `fk_validation` 100k → **1,200,000 rows/s** (measured 1.2–4.5M; the slowest at-scale case
  chosen, so FK cases now miss only LOW/safe).
- `index_bytes` 50 MB/s → **9,500,000 bytes/s** (measured 6.9–10 MB/s).
- `index_build` split: **`index_build_btree` 1,000,000 rows/s** (measured 0.84–1.4M; plain,
  composite, partial, unique) and **`index_build_expression` 250,000 rows/s** (measured
  176–282k; expression + GIN), keyed on the parse tree — `IndexDetails.has_expression` is new
  IR, non-btree access methods and expression columns select the slow constant,
  `ADD_EXCLUSION` maps to expression, and table-scope REINDEX uses the expression constant
  because its work is *every* index (whole-table rate measured right there).
- Left alone per the brief: `heap_rewrite` (measured spot-on again: median 125k this run),
  `dml_update`/`dml_delete` (err safe inside 4x), `validation_scan`, `constant_op`.

Two supporting changes the measured values forced. **`WIDEN_MEASURED_TENTHS = 20`** (2x each
way, vs 4x uncalibrated / 1.5x future-calibrated): 1.5x around a one-machine measurement was
too tight for the residual spread the harness itself shows across same-size shapes, and the
2x upper side absorbs "production is slower than my NVMe". **An additive fixed-overhead floor
(+1/+10/+100 ms, constant_op's own triple) on every proportional estimate**: with throughputs
now 4–12x faster, small-table intervals collapsed to single-digit ms, and a measured 8 ms
against a [0..2] ms interval counts as a dangerous miss — the floor states the truth that even
a zero-row DDL opens, locks, and commits. Without it the constants fix would have traded six
big dangerous misses for a dozen tiny ones.

### 3. Output restructured around classification

The intervals are 16x wide at median by construction; the harness showed the classification
plus the *band* of the upper bound is what tracks reality. `Verdict` now carries
`band: DurationBand | None` (sub_second / seconds / minutes / long, derived from the upper
bound; None exactly when no bounded estimate underlies the verdict) as the primary assessment
next to the classification, and every rationale speaks in bands ("blocks writes for a hold
measured in seconds at worst") instead of quoting point estimates. The numeric interval is
unchanged but demoted: it lives on the row's `DurationEstimate` (which also grew a `band`
property) as secondary detail. No numbers appear in headline rationales or conditions anymore.

### Re-run (same harness + one new case; 35 cases × 4 sizes, 10M seed 3.0 GB, 172 min)

Two deliberate deltas from the 2026-08-21 setup, both recorded in the harness: a fifth seeded
index (plain btree on `title`) so the reuse side of the fix is pinned, and a new case
`alter_type_varchar_widen_plain_idx` (`title varchar(255) → varchar(300)`). The fifth index
makes every rewrite/DML case carry one more index than the prior run — comparisons of those
absolute times are polluted by design; the six dangerous-miss cases and the FK cases are not.

**The six dangerous misses: all gone.** reindex_index measured 75/989/3950 ms inside
[51..300]/[362..1546]/[3852..15506] at 100k/1M/10M; alter_type_varchar_widen measured
264/1447/10087 ms inside [51..300]/[501..2100]/[5001..20100] — the narrowing now says "no
table rewrite, but 1 dependent index on 'status' cannot be reused and is rebuilt under ACCESS
EXCLUSIVE", modeled as an `index_build_btree`. **The reuse side holds**: the new plain-index
widen case and the (now indexed) varchar→text case both narrow to "dependent plain btree
index(es) are reused (CheckIndexCompatible), not rebuilt" — constant-time predicted, 0–3 ms
measured, no false UNSAFE.

**ADD FK at 10M cleared**: add_fk_plain and add_fk_cascade went UNSAFE → CONDITIONALLY_SAFE
(measured ~1 s; this run's FK joins ran at 5–10M rows/s, so the 1.2M rows/s choice over-predicts
— every FK miss is LOW, the safe direction). validate_fk stays SAFE.

**Coverage: 97/128 inside (76%), 29 below low (safe), 2 above high** — versus 91/124, 27, 6.
High/measured median dropped 7.7x → 5.4x; interval width is still 16x median (structurally so:
the still-uncalibrated families keep their 4x). The two remaining HIGHs are honest and small:
reindex_table at 100k (1.75 s vs 900 ms bound — five per-index fixed costs against one +100 ms
floor; the rows model also doesn't scale with index count) and create_index_composite at 1M
(2.5 s vs 2.1 s bound — the same case measured 1.1 s in the prior run; run-to-run variance).
**Neither crosses a classification boundary**: both predicted SAFE, and both measured values
still band SAFE (< 5 s write-block). The three ts→tstz LOWs remain by design (the interval
describes the rewrite branch of a conditional verdict; the UTC session took the 0 ms branch).

**Classification distribution** (UNKNOWN zero everywhere): 1k 24 SAFE / 10 COND · 100k 16/18 ·
1M 9/17/8 UNSAFE · 10M 2/16/16. Reclassifications at 10M worth owning: the four plain-btree
index builds went UNSAFE → CONDITIONALLY_SAFE (measured 5.9–7.5 s of blocked writes — the old
UNSAFE was an artifact of 4x widening on a 4x-too-slow constant, and 6–7 s sits squarely in
the 5–60 s conditional band); ADD UNIQUE stays UNSAFE at 10M on the same measurement because
its lock is ACCESS EXCLUSIVE, not SHARE — same duration, different blast radius, which is the
engine working as designed. alter_type_varchar_widen at 10M went COND → UNSAFE (upper bound
20.1 s grazes the 20 s full-block threshold; measured 10.1 s would band COND — conservative,
boundary effect, acceptable). Every case's class still moves monotonically stricter with size.

Artifacts: `scale_results.json` (new run), `scale_results_prev.json` (2026-08-21 run),
`reuse_experiment.py`, `compare_runs.py`, updated `scale_harness.py`/`analyze_scale.py`, all
in the session scratchpad. Caveats unchanged from Part 3: one uncontended NVMe machine;
measured rates are ceilings; MEASURED is not the fitted cross-environment calibration prompt 8
still owes.


## Five tiers by required action, and dml_update fixed to measurement (2026-08-21)

`CONDITIONALLY_SAFE` had grown to 27.5% of the online corpus while holding three unrelated
things — harmless-but-irreversible operations, brief ACCESS EXCLUSIVE acquisitions, and
genuinely disruptive multi-second write blocks. It is replaced by two tiers, giving five, and a
statement's tier is now decided by **what the reviewer must do**, never by how severe the form
sounds: `SAFE` (nothing) → `SAFE_IRREVERSIBLE` (proceed; record that there is no undo) →
`NEEDS_TIMING` (safe in itself, disruptive at the wrong moment: off-peak, or `lock_timeout` with
retries) → `UNKNOWN` (cannot determine) → `UNSAFE` (do not run as written). That ladder is also
the combine order for the rows of one statement, so a statement is still never called safe while
a piece of it is undecided. The consequence that mattered most: **irreversibility no longer
forces a statement out of the safe tiers** — it selects *which* safe tier, and the old
"irreversible ⇒ at best conditional" floor is gone.

### What each tier absorbed, and the suppression that was deleted

To `SAFE_IRREVERSIBLE`: the whole population of the old irreversibility floor — enum label
additions (389 wild, every one formerly conditional), `DROP FUNCTION`/`DROP TYPE` and the other
definition drops that hold no lock a reader waits on, and matched `UPDATE`/`DELETE` whose worst
case still bands under the write-block threshold. To `NEEDS_TIMING`: the brief-AEL
`_constant_verdict` branch, the middle duration band in both `_band()` consumers, `INSERT ...
SELECT` / CTAS / `CREATE MATVIEW` (unbounded source volume), matched DML whose worst case bands
past the threshold, the existence-guard cap, the conditional-branch cap (the ts→tstz TimeZone
case), and both escalations. The escalations now lift *either* safe tier: observed `pg_locks`
contention and "a long statement running while the transaction still holds an AEL" are
orthogonal to reversibility, so a `SAFE_IRREVERSIBLE` statement escalates exactly as a `SAFE`
one does — and keeps its loss condition, which the old held-lock path silently dropped.

The **file-local exemption was deleted**, deliberately. The old engine suppressed the
irreversibility floor when every relation a statement locked was created earlier in the same
file, because `CONDITIONALLY_SAFE` was too heavy a price for dropping a table nobody could see.
`SAFE_IRREVERSIBLE` costs a reviewer one line, so the fact is recorded rather than hidden: 19
wild statements move `SAFE` → `SAFE_IRREVERSIBLE` on that change alone, and they are exactly the
drops-on-empty/file-created-relations the new tier was specified to hold.

### dml_update: 50,000 → 22,000 rows/s, MEASURED

The harness had measured the unbatched-backfill family at 29.2k / 27.7k / 22.2k rows/s at 100k /
1M / 10M against a 50k guess — ~2x optimistic, the dangerous direction, on the family behind
every unbatched backfill. It survived the last pass because the numeric interval was the headline
and the 4x uncalibrated widening absorbed the error; with the *band* of the upper bound now
primary, a 2x underestimate can drop a statement a full band. Fixed to the slowest at-scale
measurement, 22,000 rows/s, by the same rule used for `fk_validation`; `MEASURED` also tightens
its widening from 4x to 2x. The SAFE→NEEDS_TIMING boundary for a full-table UPDATE moves from
~62k rows to ~55k — stricter, the safe direction.

**The re-run validated it in the sharpest possible way.** On this run's `update_without_where`,
the old constant's upper bound at 10M was 800,076 ms and the statement measured **821,113 ms** —
the old value would have produced a *new* dangerous miss on this very harness. The new bound
(909,166 ms) covers it, and all four sizes land inside: measured 48 / 4,889 / 53,647 / 821,113 ms
against point estimates of 55 / 4,555 / 45,464 / 454,543 ms.

Recorded as a limit rather than fixed: **the family is bimodal, and it also scales with index
count**, neither of which one rows/second constant can express. Scattered *partial* updates
(matched `WHERE`, index-driven) measured 7.9–19.5k rows/s here, slower than the sequential
rewrite the constant is fitted to; they are not split off because the model cannot see
selectivity statically and because matched-row DML is *bounded*, never predicted. And the index
count moves the whole family: adding this run's fifth index dropped the same statement from
22.2k to 12.2k rows/s at 10M (449.5 s → 821.1 s), which is why the 10M point estimate reads 1.8x
optimistic here while it was accurate to 1.1% on the four-index table it was fitted to. The
calibration loop owns both splits. `dml_delete` was left alone: measured 152k–1.2M rows/s at 100k
rows and above against a 100k guess, i.e. conservative, the safe direction.

### Re-run: the wild corpus (3,081 files, 3,078 parsed, 15,514 statements, PG 17)

To make "nothing moved out of UNSAFE" evidence rather than argument, the pre-restructure engine
was **reconstructed exactly** — every edit reversed, each reversal asserting it matched once —
and both engines were run over the same corpus recording one row per statement, so the check is
per statement rather than on totals that can cancel two opposite errors out. The reconstruction
validates itself: offline it reproduces the 2026-08-21 record to the statement (SAFE 8,659 /
COND 3,355 / UNSAFE 0 / UNKNOWN 3,500; proven 12,014, unverified 3,500).

**Offline** (no snapshot): SAFE **8,640** (55.7%) / SAFE_IRREVERSIBLE **556** (3.6%) /
NEEDS_TIMING **2,818** (18.2%) / UNSAFE **0** / UNKNOWN **3,500** (22.6%). Every movement:
COND→NEEDS_TIMING 2,818, COND→SAFE_IRREVERSIBLE 537, SAFE→SAFE_IRREVERSIBLE 19. Nothing else
moved at all — offline the new constant changes nothing, because row counts are unknown without
a snapshot and the DML families are UNKNOWN anyway.

**Online** (same replay: each repo's chain into a fresh PG 17.10 database, snapshot captured
before each file): SAFE **8,709** (56.1%) / SAFE_IRREVERSIBLE **616** (4.0%) / NEEDS_TIMING
**3,669** (23.6%) / UNSAFE **0** / UNKNOWN **2,520** (16.2%). Movements: COND→NEEDS_TIMING 3,669,
COND→SAFE_IRREVERSIBLE 597, SAFE→SAFE_IRREVERSIBLE 19, plus 3 statements into UNKNOWN that are
replay noise rather than tier logic — all three are live-context-dependent `alter_table`s that
are UNKNOWN offline too, and the two replays also disagree on *method* counts (proven 12,044 vs
12,043, simulated 228 vs 226), which both engines compute identically, so the snapshots
themselves differed.

Two honest readings of the split. First, it is 15/85, not 50/50: ~4% of all statements are the
"just record it" kind and ~24% genuinely need a timing decision, so `NEEDS_TIMING` is still the
second-largest tier. What changed is that it now has one uniform remedy instead of three, and
the 616 record-only statements no longer hide among the 3,669 that need a window. Second, online
`NEEDS_TIMING` is **entirely** brief-AEL — 3,396 of 3,669 band sub-second and the other 273 have
no bounded estimate (unbounded source queries), with *nothing* in the seconds-or-longer bands,
because replay databases hold no data. The seconds+ population exists only against
production-sized tables, which is what the harness supplies.

### Re-run: the scale harness (34 cases × 4 sizes = 136 records, 10M seed 3.0 GB, 62 min)

Rebuilt to the configuration this file already documents — the fifth seeded index (plain btree
on `title`) and the `alter_type_varchar_widen_plain_idx` case — because the previous session's
harness and its results did not survive its scratchpad; the two files left behind under
`scale_results.json`/`scale_results_prev.json` were both copies of the *pre-fix* 33-case run.
Correcting the record while I am here: that re-run was **34 cases, not 35** — the distribution
recorded for it sums to 34 per size, and the reconstructed old engine reproduces it exactly.

**New five-tier distribution** (UNKNOWN zero everywhere — schema-complete and stats-fresh):

| size | SAFE | SAFE_IRREVERSIBLE | NEEDS_TIMING | UNSAFE |
|---|---|---|---|---|
| 1k | 24 | 4 | 6 | 0 |
| 100k | 16 | 1 | 17 | 0 |
| 1M | 9 | 0 | 17 | 8 |
| 10M | 2 | 0 | 16 | 16 |

Re-assessing the harness's **pickled snapshots** with the reconstructed engine — identical
inputs, so every difference is the engine change and nothing else — gives the old tiers as
24/10, 16/18, 9/17/8, 2/16/16, reproducing the prior record exactly. The movement is
COND→NEEDS_TIMING 56, COND→SAFE_IRREVERSIBLE 5, SAFE→SAFE 51, UNSAFE→UNSAFE 24.

**The mandated check, on the one dataset where UNSAFE actually exists: 24 UNSAFE before, 24
after, zero statements moved into a safe tier and zero moved out of UNSAFE at all.** Every case
still moves monotonically stricter with size (0 non-monotonic cases), and lock modes remain
136/136 correct with rewrite ground truth matching at every size, including the new reuse case
(`rewrote=False`).

Interval coverage improved: **110/128 inside (86%), 14 below (safe), 4 above** — versus 97/128
(76%), 29, 2. The over-prediction count halving is the `dml_update` fix landing. **None of the
four dangerous misses crosses a classification boundary**, the same standard the last run met:
`alter_type_varchar_widen` @100k (307 ms vs a 300 ms bound) and `create_unique_index` @100k
(350 vs 300) both still band SAFE, and `reindex_table` misses at 100k (2,090 vs 900 ms, still
SAFE under the 5 s write-block threshold) and at 10M (100,988 vs 80,100 ms, UNSAFE either way).
Both `reindex_table` misses are the limitation the last run already named — the rows model does
not scale with index count — now worse because the table carries one more index.

### Judgment calls, flagged rather than buried

**The existence-guard cap was preserved, not re-derived.** A guarded statement caps at
`NEEDS_TIMING` both ways, exactly as it capped at `CONDITIONALLY_SAFE`. The strict reading of
"what must the reviewer DO" would leave a guarded SAFE at SAFE (no action either way the guard
falls) and a guarded UNSAFE at UNSAFE; that is a change beyond the tier split, so it was not
made. Impact is negligible either way: **20 statements of 15,514** carry the cap, all landing in
`NEEDS_TIMING`, none of them a would-be UNSAFE.

**Three statements lost conservatism that was accidental.** `DROP EXTENSION pg_trgm`, `DROP
SCHEMA ... CASCADE` and one `DROP TABLE` were conditional before *only* because the
irreversibility floor caught them; the engine models no certain lock for them (CASCADE
dependents cannot be named statically, so `_block_type` sees nothing certain) and their
rationale has always read "blocks neither reads nor writes". They are now `SAFE_IRREVERSIBLE`.
If CASCADE blast radius should raise them, it must come from the **lock model** naming the
dependents, not from reversibility standing in for it — which is the confusion the split exists
to end.

**A pre-existing asymmetry the new names make visible, left unchanged — worth settling before
the schema freezes.** A brief ACCESS EXCLUSIVE hold reached through `_constant_verdict`
(catalog-only work) becomes `NEEDS_TIMING`, while one reached through `_proportional_verdict` on
a small table (real work, but under the 2 s read-block threshold) stays `SAFE`. The harness now
shows this plainly: at 1k rows `alter_type_varchar_widen_plain_idx` — a *pure relabel* whose
indexes are reused — reads NEEDS_TIMING, while `alter_type_int_bigint`, an actual heap rewrite
holding the same ACCESS EXCLUSIVE lock, reads SAFE. Read against the old tier's contents,
"brief-AEL" names the constant-op branch and behavior is correct as shipped; read against the
*lock*, every AEL on a live relation would qualify: 13 of the 24 SAFE cases at 1k and 5 of the
16 at 100k hold ACCESS EXCLUSIVE and would move to NEEDS_TIMING. It costs almost nothing offline (17 statements, all DO blocks) but fires whenever a
live table is small.

Suite: 770 → **788 tests**, 97% total branch coverage (verdict modules 90–100%, engine 94%),
ruff and mypy `--strict` clean, full run against real zonky PG 17.10 binaries in 2m48s. New
tests pin the placement rule directly: irreversibility never leaves the safe tiers when it is
the only concern, brief-AEL needs timing while a brief write-block does not, timing beats
recording when both apply, contention lifts both safe tiers, and a parametrized guard asserts no
UNSAFE case softens into a safe tier. Artifacts in the session scratchpad: `make_baseline.py`
(the exact reversal) and the reconstructed `baseline/` tree, `corpus_tiers.py`,
`compare_tiers.py`, `compare_scale.py`, `reassess_baseline.py`,
`corpus_offline_{baseline,new}.json`, `corpus_online_{baseline,new}.json`,
`scale_results.json` with `snapshots/`,
`scale_baseline_predictions.json`, and the pre-fix run preserved as
`scale_results_2026-08-21_prefix.json`. Caveats unchanged: one uncontended NVMe machine, measured
rates are ceilings, and MEASURED is still not the fitted cross-environment calibration prompt 8
owes.

## The ACCESS EXCLUSIVE floor, and the artifacts moved into the repo (2026-08-21)

Two changes: the flagged AEL asymmetry is settled on the lock rather than the code path, and the
measurement evidence stops living in session scratchpads.

### 1. The tier boundary is drawn on the lock, not on which path estimated the duration

The asymmetry flagged last session was real, and the flag understated it slightly. A brief
ACCESS EXCLUSIVE hold reached through `_constant_verdict` became `NEEDS_TIMING`; the identical
lock reached through `_proportional_verdict` on a small table stayed `SAFE`. At 1k rows the
harness therefore rated `alter_type_varchar_widen_plain_idx` — a pure relabel whose indexes are
reused — `NEEDS_TIMING`, and `alter_type_int_bigint`, an actual heap rewrite holding the same
ACCESS EXCLUSIVE lock, `SAFE`. Both block every reader and writer of the table for their whole
duration; what differed was only which branch computed a number.

`_floor_for_access_exclusive` now floors any safe-tier verdict whose row takes a certain ACCESS
EXCLUSIVE lock on a relation the file did not create. Duration still decides everything above
the floor — seconds and minutes still reach UNSAFE — it no longer decides whether an ACCESS
EXCLUSIVE acquisition needs a window at all. The reasoning the rationale now carries: whether
the lock is *held* briefly is a duration question, whether it must be *acquired* is not, and the
acquisition queues behind every open transaction on the relation while parking every later query
behind that wait. Relations created earlier in the same file are exempt (nothing else can hold a
lock on a relation nothing else has seen), as is every relation of a `baseline_shaped` file.

Four placement decisions worth recording:

**It runs after `_escalate_for_contention`, not before.** As a floor it lifts `SAFE` and
`SAFE_IRREVERSIBLE` to `NEEDS_TIMING` and can move nothing into UNSAFE. Run before the
escalation, a floored statement with observed `pg_locks` contention would have been escalated
`NEEDS_TIMING` -> UNSAFE, manufacturing UNSAFE verdicts out of a lock-shape rule. The escalation
is a statement about *other sessions*; the floor is a statement about *this* lock. They compose
in one direction only.

**"Relation" includes indexes.** `REINDEX INDEX` takes its ACCESS EXCLUSIVE on the index under a
SHARE on the table, and `REINDEX TABLE` on each index it rebuilds. Both qualify: an index is a
relation, and queries that need it queue exactly the same way. This is the one place the
implemented rule is wider than last session's estimate — which is why 14 of the 24 SAFE cases at
1k moved rather than the predicted 13, and 6 of 16 at 100k rather than 5. The estimate counted
table-level ACCESS EXCLUSIVE; the rule as specified counts the lock, wherever it lands.

**Certain-but-unnamed ACCESS EXCLUSIVE relations are not exempt** (the table owning a dropped
index, and the like). File-locality cannot be proven for a relation that cannot be named, and
`_file_local` already refuses to call such a statement file-local — the floor agrees with it
rather than inventing a second, looser answer.

**Two things deliberately not touched.** `_constant_verdict`'s read-blocking branch already
returned `NEEDS_TIMING`, so the floor is a no-op there and no statement collects the condition
twice. And the flagged "three statements lost conservatism" item is untouched by design: `DROP
SCHEMA ... CASCADE` and its two companions model *no certain lock at all*, so a lock-shaped rule
cannot reach them. That was the point of the flag — if CASCADE blast radius should raise them it
must come from the lock model naming the dependents.

### Re-run: the wild corpus (3,081 files, 3,078 parsed, 15,514 statements, PG 17)

**Offline: nothing moved, and the output is byte-identical to the pre-floor run.** SAFE 8,640 /
SAFE_IRREVERSIBLE 556 / NEEDS_TIMING 2,818 / UNSAFE 0 / UNKNOWN 3,500, per-statement matrix
entirely diagonal. Last session estimated the offline cost at "17 statements, all DO blocks"; the
true answer is zero, and the reason is the exemption. 489 offline SAFE-tier statements *do* hold
a certain ACCESS EXCLUSIVE lock (353 `alter_table`, 117 `create_policy`, 17 `do_block`, one
`drop_table`, one `drop_policy`) — and every one of them locks only relations its own file
created, verified statement by statement with zero leaks. Those 17 DO blocks are exactly the ones
the estimate named; they are file-local and correctly stay SAFE. Offline the floor can reach
nothing else, because a live relation's size is unknown without a snapshot and those statements
are UNKNOWN already.

**Online** (same replay: each repo's chain into a fresh PG 17.10 database, snapshot captured
before each file): SAFE **8,703** (56.1%) / SAFE_IRREVERSIBLE **616** (4.0%) / NEEDS_TIMING
**3,677** (23.7%) / UNSAFE **0** / UNKNOWN **2,518** (16.2%). Per statement: 6 `safe` ->
`needs_timing`, 2 `unknown` -> `needs_timing`, nothing else moved and nothing moved toward safer.
The six are the fix — four boundary `ALTER TABLE ... ADD CONSTRAINT` (unique/check), one coder
`SET NOT NULL`, one lemmy `REINDEX TABLE`, all ACCESS EXCLUSIVE on pre-existing relations. The
two are replay noise, and provably not the floor: the floor only touches verdicts already in a
safe tier, so it cannot produce an UNKNOWN transition at all — those two lemmy `SET NOT NULL`s
got facts from this replay's snapshot that the previous replay's snapshot did not supply.

### Re-run: the scale harness (34 cases x 4 sizes = 136 records, 10M seed 3.0 GB, 74 min)

Old and new tiers come from the **same pickled `LiveSnapshot` objects** — the harness pickles
every snapshot it feeds the engine, and `make_baseline_ael.py` reconstructs the pre-floor engine
by reversing its one wiring line, asserting the reversal matched exactly once. Identical inputs,
so every difference is the floor and nothing else.

| size | SAFE | SAFE_IRREVERSIBLE | NEEDS_TIMING | UNSAFE | before (same snapshots) |
|---|---|---|---|---|---|
| 1k | 10 | 4 | 20 | 0 | 24 / 4 / 6 / 0 |
| 100k | 10 | 1 | 23 | 0 | 16 / 1 / 17 / 0 |
| 1M | 8 | 0 | 18 | 8 | 9 / 0 / 17 / 8 |
| 10M | 2 | 0 | 16 | 16 | 2 / 0 / 16 / 16 |

Movement across all 136 records: `safe` -> `needs_timing` **21**, `safe` -> `safe` 30,
`needs_timing` -> `needs_timing` 56, `safe_irreversible` -> `safe_irreversible` 5, `unsafe` ->
`unsafe` 24. **Nothing moved in the other direction, and the mandated check passes: 24 UNSAFE
before, 24 after, zero into a safe tier and zero out of UNSAFE at all.** Every case still moves
monotonically stricter with size (0 non-monotonic), lock modes stay 136/136 correct, and
relfilenode rewrite ground truth matches at every size including the reuse case.

**The floor discriminates on the lock and on nothing else**, which the still-SAFE population
shows better than the moved one: the ten cases still SAFE at both 1k and 100k are exactly the six
`CREATE INDEX` shapes (SHARE), the three `ADD FOREIGN KEY` forms (SHARE ROW EXCLUSIVE) and
`VALIDATE CONSTRAINT` (SHARE UPDATE EXCLUSIVE). The fourteen that moved are exactly the ACCESS
EXCLUSIVE ones: seven `ADD COLUMN` variants, five `ALTER COLUMN TYPE`, `ADD CHECK`, `ADD UNIQUE`,
`SET NOT NULL`, and both REINDEX forms. And the flagged pair now agrees with itself — at 1k
`alter_type_varchar_widen_plain_idx` (relabel, measured 6 ms) and `alter_type_int_bigint` (heap
rewrite, measured 196 ms) both read `NEEDS_TIMING`.

### The same machine, 1.46x slower, and what that costs prompt 8

Interval coverage this run: **102/128 inside (80%), 11 below low, 15 above high** — against
110/128, 14, 4 last session. That is not the floor (which changes no duration) and not a
regression in the model: **the machine ran 1.46x slower this session** — median ratio over the 81
shared cases measuring more than 50 ms, and 74 minutes wall clock against 62. Same binaries, same
seed sizes, same laptop, a few hours apart. `dml_update` measured 11.1-14.9k rows/s against the
22,000 fitted last session and `index_bytes` 5.9 MB/s against 9.5, while `fk_validation` came out
*faster* (2.9M rows/s median).

The check that matters held. `check_boundary_crossings.py` re-bands every miss to ask whether the
*tier* would have differed: **26 misses, 4 crossing a classification boundary, 0 of them in the
dangerous direction.** Every crossing is the engine being stricter than the measurement —
`add_fk_plain` and `add_fk_cascade` at 10M and `delete_without_where` at 1M, all
over-predictions, plus `reindex_table` at 100k whose `NEEDS_TIMING` comes from the floor rather
than from a band.

This is the sharpest evidence yet for the caveat that has ridden along since the first harness,
and prompt 8 should treat it as a requirement rather than a footnote: **a single machine measured
twice, hours apart, disagrees with itself by ~1.5x**, which is most of the 2x
`WIDEN_MEASURED_TENTHS` allows around a MEASURED constant. A cross-environment calibration cannot
be a point fit — the constants need a spread fitted across machines and load states, and MEASURED
values taken from one quiet laptop bound production from above only if that laptop was in fact
quiet.

### 2. artifacts/ is committed

The repository had **no commits at all** before this session; its history starts here. Last
session's post-fix harness results did not survive their scratchpad — only a copy of the pre-fix
run remained — and the run had to be paid for a second time. Prompt 8's calibration reads these
measurements directly, so `artifacts/` now holds them: the 15 corpus manifests, three generations
of per-statement corpus results (offline and online — pre-tier-split, pre-AEL-floor, current),
three generations of scale-harness results (pre-severity-fix 33-case, pre-AEL-floor 34-case,
current 34-case), the two reconstructed-engine prediction sets, the UNKNOWN diagnosis, and every
reconstruction and comparison script. `artifacts/README.md` says what each file is, which engine
produced it, and how to regenerate it.

Two deliberate exclusions and one addition. The **corpus SQL itself is not vendored** — 3,081
files belonging to 15 upstream projects — and neither are the ~120 MB of PostgreSQL binaries. The
manifests are the only pointer back, so they gained **`sha256` and `bytes` per file**: a repo path
alone does not pin which bytes were measured, since upstream files move and get rewritten, and a
re-harvest should be verifiable rather than assumed. Corpus results are stored one row per
statement and never as totals, because totals can cancel two opposite errors out and the safety
checks are only meaningful per statement.

Suite: 788 -> **797 tests**, 96% total branch coverage (verdict modules 90-100%, engine 94%),
ruff and mypy `--strict` clean, full run against real zonky PG 17.10 binaries in 3m31s. The new
tests pin the rule from both sides: the constant-op and proportional paths agree on a small live
table, the relabel/rewrite pair agrees at 1k, a relation created earlier in the same file stays
SAFE while a live relation named in that same file does not, weaker locks (SHARE, SHARE ROW
EXCLUSIVE) on live relations are untouched, the floor never reaches UNSAFE and never
re-classifies UNKNOWN, and the two tests that encoded the old asymmetry were rewritten to state
the new rule rather than deleted.

## Renamed to Blastoise, and the line the theme is not allowed to cross (2026-08-22)

`pgverdict` is now `blastoise` — a blast-radius pun, with `bt` as the short CLI alias. The
rename itself was mechanical. The part worth recording is the rule it was done under, because
that rule is the reason the rename stays cheap the *next* time it happens.

### The naming principle

The theme lives only in what a person reads: CLI help, README, docs, PR comment headers,
human-readable rationale text. It touches **no** JSON key, schema field name, enum machine
value, or exit code. Someone wiring this into CI reads `classification: needs_timing` and must
never need to know Pokémon to do it. Cute in the wrapper, boring in the payload.

Component names, for docs and CLI help: **Torrent** (parser and IR), **Shell Armour** (lock
semantics catalog), **Hydro Scan** (live introspection), **Pressure Levels** (the five tiers),
**Shell Report** (the verdict document), **Training Ground** (the scale harness), **Evolution**
(the calibration loop), **Shell Seal** (signing and attestation). Tier display names —
Calm Water, One-Way Current, Rain Check, Hydro Pump, Fog — live in exactly one lookup,
`blastoise.verdict.PRESSURE_LEVELS`, pointed *at* the enum rather than stored in it. The
`Classification` values remain `safe` / `safe_irreversible` / `needs_timing` / `unsafe` /
`unknown`.

The README carries a NOTICE saying Blastoise is a working codename and the Pokémon reference is
placeholder: no domain assumed, no character artwork or third-party assets vendored, the name
confined to the package name, the entry points, and prose. That NOTICE is only credible if the
machine contract is genuinely clean — otherwise "we'll rename it later" means breaking every
downstream consumer. So the principle is enforced, not asserted.

### Enforcement: a guardrail test and a before/after surface diff

**`tests/test_naming.py` (14 tests)** walks every module in the package and asserts that no enum
*value* and no dataclass *field name* contains themed vocabulary, that no key in the canonical
snapshot JSON or the CLI's `--json` payload does either, and that the five `Classification`
values are exactly the pinned strings. It also pins the bridge: `PRESSURE_LEVELS` is total over
`Classification`, no display name equals or normalizes to a machine value, and — because
`Classification` is a `StrEnum` — `f"{member}"` is the machine value, so a careless f-string in
reporting code cannot leak the theme and a careless one in machine output cannot leak the
display name.

Writing that test taught the one thing worth passing on: **substring matching is wrong.**
The first version flagged `create_index_concurrently` (con**current**ly), `constraint_name`
(const**rain**t) and every `add_check`. The vocabulary overlaps ordinary SQL badly. The test now
splits identifiers on separators and camelCase boundaries and matches whole tokens, with
`check`, `current`, `water`, `way` and `one` explicitly allowed as Postgres' words rather than
ours, and the multi-word display names (`calm water`, `rain check`, `hydro pump`, ...) matched as
phrases — a phrase never collides by accident where a bare token does.

**The before/after surface diff** is the stronger check, because it does not depend on my
vocabulary list being right. `dump_machine_surface.py` (in the session scratchpad) dumps
everything machine-visible about the package — every enum's members and values, every public
dataclass's field names *in order*, the `__all__` of every subpackage, the duration-constant keys
and units, `SNAPSHOT_FORMAT`, the catalog's entry-field vocabulary, conflict matrix and
statement-resolution map, the canonical `LiveSnapshot` JSON, the CLI's `--json` payload, and all
three CLI exit codes — with the package name normalized to `<pkg>`. Dumped before the rename and
after it, then diffed:

**0 machine-readable differences.** 16 enums, 62 dataclasses, exit codes 0/2/2, snapshot format
3, catalog conflict matrix and resolution map: identical. The only Python API change is additive
— `PRESSURE_LEVELS` and `pressure_level` appear in `blastoise.verdict.__all__`; nothing was
removed.

A third check, end to end: re-running the offline wild corpus (3,081 files, 15,514 statements)
under the new package reproduces `artifacts/corpus/offline_current.json` **row for row** — all
15,514 per-statement tiers, bands and methods identical, and the `tiers`, `methods` and `by_kind`
summaries equal. The rename is a behavioral no-op, demonstrated rather than assumed.

A quieter piece of evidence arrived for free: **not one committed artifact JSON contained the
string `pgverdict`** — not the corpus results, not the harness results, not the manifests. The
payloads never carried the name, which is exactly what the principle predicts.

### What moved, and two judgment calls

Moved: the package directory (`src/pgverdict` → `src/blastoise`), 264 identifier occurrences
across source, tests, docs, `pyproject.toml` and the `artifacts/scripts` tooling, the entry point
(plus the new `bt` alias), the coverage source, the environment variables (`PGVERDICT_TEST_DSN`,
`PGVERDICT_TEST_PG_BIN`, `PGVERDICT_SCALE_SIZES` → `BLASTOISE_*`) and the suggested Postgres role
names in the docs (`pgverdict_introspect` → `blastoise_introspect`). Those last two are
developer- and operator-facing configuration, not payload, so they follow the package name.

**Printed CLI text is ASCII.** The help epilog originally used em-dashes and middle dots and came
out as mojibake on a Windows console. Docstrings keep their em-dashes (they are never printed);
anything argparse renders is plain ASCII now.

**`prog` stays hardcoded to `blastoise`** even when invoked as `bt`, so usage lines name one
canonical command; the epilog says `bt` is an alias. The alternative — letting argparse take the
invoked name — reads worse under `python -m blastoise.cli`, where it would print `cli.py`.

**Not done: the repository directory is still `jester/pgverdict/`.** Renaming it is a plain
directory move that would invalidate the open editor's paths and this session's working
directory mid-flight, and nothing inside the repo depends on the folder's name. It is a
one-command follow-up whenever the directory is not held open.

Suite: 797 → **811 tests** (797 pre-existing, all passing unchanged, plus the 14 naming
guardrails), ruff and mypy `--strict` clean, full run against real zonky PG 17.10 binaries.

## The Shell Report: verdict document, evidence bundle, seal, and the CLI around it (2026-08-22)

Built `blastoise.report` — the output artifact everything before it was the engine for — plus the
CLI that produces and consumes it: `blastoise check <migration.sql>` (assess, emit the verdict
document, exit 0/1/2/3), `blastoise verify <report.json>` (signature and evidence hashes, both
must pass), `blastoise explain <report.json>` (expanded rendering). Schema v1: schema_version,
tool_version, change_id, evaluated_at, pg_version, online, the file-level verdict, per-tier
counts, snapshot_hash, statements[], irreversible[], unverified[], rollback, transaction
warnings, the evidence manifest, and an optional signature block. The file verdict derives from
the worst per-statement tier through the existing combine ladder — SAFE and SAFE_IRREVERSIBLE →
`proceed`, NEEDS_TIMING and UNKNOWN → `requires_approval`, UNSAFE → `block` — and an empty file
proceeds. Exit codes 0/1/2 mirror those verdicts and 3 is a tool error; argparse usage errors
are remapped from argparse's default exit 2 to 3, because CI reading `check` must never mistake
a typo'd flag for BLOCK, and an unreadable or unparseable migration is likewise 3, not 2 — a
parse failure is deterministic but it is "no verdict was produced", never "the migration is
dangerous", and the help text says so.

**The payload is plain JSON built by typed helpers, not a dataclass tree.** The report is a
serialization boundary — its whole life is being hashed, signed, diffed and read back by other
tools — so the builder produces dicts directly and the canonical form is the same discipline the
snapshot already uses: sorted keys, compact separators, ASCII, floats banned outright,
frozensets sorted (and only sets of strings allowed — anything else has no deterministic order
worth inventing). Round-trip stability is pinned as `canonical(json.loads(canonical(x))) ==
canonical(x)`, two builds from the same inputs are byte-identical, and the `--json` stdout is
byte-identical to the written report.json. A side benefit: no new dataclass fields or enum
values for the naming guardrail to police beyond `FileVerdict` (values `proceed` /
`requires_approval` / `block`, plain on purpose).

**unverified never serializes empty, and that is enforced, not hoped.** `build_report` raises
AssertionError if the collector returns nothing (a test monkeypatches the collector to prove
the trap fires). The list can never legitimately be empty because two entries are structural
truths of the method itself: the lock-acquisition queue at execution time is unknowable in
advance (always), and online, the snapshot describes capture time, is not transactionally
consistent across sections, and reltuples is an estimate even fresh. On top of those: offline
gets the full "nothing live was checked" entry (with the degradation reason folded in when a
requested connection failed), every UNKNOWN statement contributes its rationale, every
CannotEstimate its reason, and every duration constant an estimate actually leaned on is
declared with its calibration state — UNCALIBRATED as an admitted guess, MEASURED with the
one-quiet-machine caveat the AEL-floor session proved matters (~1.5x drift on the same laptop
hours apart). So even a fully-decided online report carries an honest residue; that is the
point, not noise.

**Evidence bundle: hashes always, files when asked.** Five files at most — migration.sql,
parse_tree.json (the classified IR, source key dropped since migration.sql holds it),
catalog_rows.json (the exact resolved catalog entries with their citations, re-resolved from
the same (script, catalog, pg_version) triple the engine consumed, so they are the rows it
used), duration_constants.json (values, calibration states, thresholds, widening factors), and
snapshot.json (the snapshot's own canonical bytes; snapshot_hash is their sha256 and `verify`
cross-checks it against the manifest entry, so the headline hash cannot drift from the
evidence). The manifest records name, sha256 and size for every file whether or not the bundle
is written; `-o/--output-dir` writes report.json plus evidence/, and without it bundle_dir is
null and `verify` fails the evidence check with "bundle was not written" — a report whose
claims cannot be traced to bytes on disk is not auditable, and verify says so instead of
passing vacuously. change_id defaults to the sha256 of `script.source` encoded UTF-8 — the
same bytes migration.sql holds — rather than the on-disk file, so the id and the evidence can
never disagree over BOMs or line endings. Per-statement evidence references use a deliberately
coarse deterministic rule (the base three files always; duration_constants.json when any row
carries an estimate; snapshot.json whenever the assessment was online) rather than claim-level
tracing — the rows already name their constant_key and their narrowings name their facts, so
finer granularity would restate what the statement payload says.

**Shell Seal: Ed25519 over the canonical payload with the signature key absent.** Signing an
already-signed report replaces the seal (the old signature is never signed into the message —
pinned by a test), keys come from `--sign-key` or `$BLASTOISE_SIGNING_KEY` (both are file
paths; PEM via `openssl genpkey -algorithm ed25519`, or a bare 64-hex-char seed) and are never
generated silently; no key means an unsigned report, which is valid, merely unattested — key
setup is not a prerequisite for the thirty-second first run. Two judgment calls worth flagging:
(1) **`verify` fails an unsigned report** (exit 1, with the evidence-hash results still
printed), because a stripped signature is indistinguishable from one that never existed and a
verify that passes on absence protects nothing; the message says "unsigned", distinct from
"does not match". (2) **A broken or unloadable key when signing was requested is a tool error
(3)**, not a silent unsigned report — shipping unattested when attestation was asked for is
the downgrade attack, done to yourself. `cryptography` is an optional extra
(`blastoise[sign]`), lazily imported; everything but sign/verify works without it.

**Degradation and version resolution.** An unreachable server, a refused writable role, or a
missing psycopg all degrade `check` to offline with a loud stderr warning ("the report will
carry far more in unverified") and the reason recorded in the no_snapshot entry — exit code
still comes from the verdict, tested against a connection-refused URL. When connected, the
server's real major version silently outranks `--pg-version`, with a note in the report when
they disagree; offline the flag decides, defaulting to 17 (what everything was validated on).

**Rendering.** One renderer consumes the payload dict — never engine objects — so `check`'s
terminal output and `explain`'s expanded form cannot diverge from what was signed. It leads
with the file verdict, then the five tiers worst-first with machine name, count, display name
and flavour text; the theme appears in the header and that flavour column only, and a test
asserts no display phrase survives into the JSON. The first ASCII implementation asserted
`isascii()` and immediately failed on real output — engine rationale prose carries em-dashes —
so the renderer transliterates (em/en dash, multiplication sign, curly quotes, arrows) and
backslash-escapes anything the table misses, per the rename session's mojibake rule. Timing
breakdown under `--verbose` goes to stderr so `--json` stdout stays pure; offline `check` runs
in ~0.4 s (catalog load ~330 ms dominates), and online cost is bounded by the capture's
existing per-section timeouts, comfortably inside the 90 s budget.

Suite: 811 → **883 tests** (852 passed, 31 pre-existing environment skips), 95% total branch
coverage (report modules 90–100%), ruff and mypy `--strict` clean, full run 2m58s. The
mandated tests are all present and named: round-trip stability, signature verify pass and fail
(tamper, strip, key swap, malformed blocks), evidence hash mismatch and missing-file
detection, the empty-unverified assertion, and exit-code correctness for each verdict level
plus both tool errors. README's Usage section now leads with `check` as the zero-friction
first run.

## The validation harness: is BLOCK right, are the claims accurate (2026-08-22)

Before Blastoise ships, two questions have to be answered against a real database rather than
a unit test: when it says **UNSAFE** (the machine tier behind BLOCK), is it right; and are the
per-statement claims accurate. The whole prior evidence base — the scale harness — measures the
*duration model* (lock modes 136/136, interval coverage, the dangerous-miss checks). It does
not measure the *verdict*: nothing scored the five-tier classification against what a statement
actually did to a production-shaped table. `validation/` does exactly that, and it is a consumer
of the public engine API (`assess_script` + `capture_snapshot`), exactly as the CLI is — nothing
in `src/` imports from it, and it earns its own 20 unit tests inside the 903-test suite.

### The corpus, and why the expectation is not the ground truth

172 cases / 212 labeled statements (`validation/corpus/*.yaml`), weighted by the wild-frequency
distribution this file already recorded — `common_benign` 25, `add_column` 22, `dml_backfills`
22, `index_creation` 21, `constraints` 19, `foreign_keys` 14 dominate; `transactions` 5 and the
maintenance tail are thin — not spread evenly. Each case carries a fixture (which seeded table it
binds, extra setup, session settings, concurrent activity) and a hand-written **expectation**.
The expectation is deliberately *not* used as ground truth: the runner measures what the
statement did, derives truth from the measurement, and reports every disagreement between the
author's expectation and the measurement under "label mismatches" — 9 this run, each a case whose
fixture produced a different scale-band than the author guessed (e.g. `upd_do_loop_batches_1m`
was written as `needs_timing` and measured `unsafe` — the DO block ran 15 minutes). A corpus that
graded itself against its own author's guesses would be worthless; this one grades against the
database.

Adversarial cases in both directions, per the brief. Looks-dangerous-but-isn't (35): CREATE INDEX
on a 1k table, ALTER TYPE on a binary-coercible pair whose only index is a reused plain btree,
DROP COLUMN on a table the same file created, ADD COLUMN with a non-volatile default on PG 11+,
`NOT VALID` FK on 5M rows. Looks-benign-but-isn't (48): the varchar widen on a *partial* index
(the 13.5 s case), an unbatched-looking narrow `WHERE` matching a sixth of a 5M-row table,
`CREATE OR REPLACE VIEW` on an existing view (ACCESS EXCLUSIVE), `SET NOT NULL` with no proving
CHECK. Judgment-call cases (15) that exist only because of a tier decision: enum-label-add landing
`SAFE_IRREVERSIBLE` not `NEEDS_TIMING`, and the ACCESS EXCLUSIVE floor lifting a millisecond
`ADD COLUMN` on a live table out of `SAFE`.

### Ground truth is measured, and consults nothing in the engine but the four thresholds

`validation/harness/labeling.py` derives the tier a statement *earned* from what it measurably
did — the strongest lock it held on a relation that **existed before the file ran** (relations
the file created are ignored; indexes are relations; an index already `indislive = false` is
not), the normalized hold including the lock wait, whether it errored, and two declared facts the
harness cannot measure (irreversibility, and whether it is DML). The rule restates the Postgres
lock-conflict table and the tier ladder from the docs and this file; the *only* thing it imports
from `blastoise` is the four threshold numbers that *define* the tiers (2 s / 20 s full block,
5 s / 60 s write block) — otherwise scoring the engine against a copy of the engine would prove
nothing. An error is `unsafe`; a row-level lock (DML) held long enough bands on the write
thresholds because every touched row stays locked; ACCESS EXCLUSIVE on a pre-existing relation is
at least `needs_timing` whatever the hold (the AEL-floor rule, restated); a safe irreversible
result is `safe_irreversible`. `unknown` is never ground truth — after the statement ran, the
outcome is known.

A **traffic probe** (a point SELECT and point UPDATE every ~40 ms from a separate session,
paused during snapshot capture so its own locks never reach the engine) records what a concurrent
reader and writer actually experienced, including a query still parked when the file's transaction
ends. It is corroboration, not the label: at 5M rows `type_int_bigint` held ACCESS EXCLUSIVE and
the probe's reader stalled 51 s; `conc_addcol_idle_holder_short` parked a reader 8 s behind the
holder — the block the tier names is real, not inferred.

### Hardware normalization, because a precision number that moves with the laptop is not one

The timing-variance finding from the AEL-floor session — the same machine measured twice, hours
apart, disagreeing with itself by ~1.5x — makes a tier banded on a raw millisecond reading a
property of the laptop, not the migration. So the harness re-runs nine scale-harness statements
at the sizes `artifacts/scale/` measured and divides: today's reading over the reference's is the
**machine factor**; measured work is divided by it before banding (lock waits are not — an idle
holder is as long as it is on any hardware), so truth is expressed in *reference-machine
milliseconds*, the units the constants were fitted in. The probe runs at start, at end, and a
light pass every 20 cases; each statement is labeled with the factor **interpolated at the time
it ran**, because a single factor is a fiction: this run moved 0.75x -> 0.80x against the reference
with light passes dipping to 0.41x (median 0.68, range 0.47-0.80). Two findings forced the design.
First, calibration must run on a *settled* database — the first attempt's start probe read 1.44x
on 1.5 GB of freshly-dirtied post-seed buffers and its end probe 0.69x, pure seed-wake, so a
CHECKPOINT-and-warm step now precedes it. Second, the probe *composition* shifts the median, so
the interpolated series is built only from the 1M-row probes present in every pass. **The
normalization is not cosmetic**: raw labels give 165/212 matches and 45.0% UNSAFE precision;
normalized give 172/212 and 70.0% — 7 labels flip, the machine factor moves the headline UNSAFE
number by 25 points. That is the whole point of doing it.

### The numbers, per tier, not aggregated (172 cases, 212 statements, PG 17.10, this machine)

| tier | predicted | truth | precision | recall |
|---|---|---|---|---|
| **UNSAFE** | 20 | 26 | **70.0%** | 53.8% |
| NEEDS_TIMING | 91 | 82 | 78.0% | 86.6% |
| SAFE_IRREVERSIBLE | 11 | 14 | 81.8% | 64.3% |
| SAFE | 82 | 90 | 95.1% | 86.7% |

Outcomes: 172 match, 19 strict (engine stricter than truth — the false-alarm direction), 13
lenient (engine safer than truth — the dangerous direction), 8 UNKNOWN. File-level (proceed /
requires_approval / **block**): BLOCK precision 70.0%, recall 53.8%.

**UNSAFE precision is 70%, and every one of the 6 false BLOCKs is attributable to a named
constant, none to the classifier.** Three are `heap_rewrite` at 1M rows (`type_int_bigint`,
`type_text_varchar`, `type_int_text_using`): the rewrite measured 15.8-18.6 s normalized, the
engine's 2x upper widening on the MEASURED `heap_rewrite` constant put the upper bound at 40 s,
over the 20 s full-block line. Two are `validation_scan` (`setnn_plain_5m`, `check_add_5m`): the
UNCALIBRATED 500k rows/s with 4x widening yields a 40 s upper bound against a 4-6 s measured hold
— `validation_scan` is the single most over-pessimistic constant the harness touched, and it has
never been measured (no wild `SET NOT NULL` / `ADD CHECK` on a table large enough appeared in the
scale harness). One is the contention escalation (`conc_addcol_idle_holder_short_visible`):
observed `pg_locks` traffic escalates to UNSAFE by rule, and the 8 s hold is a real full block
that the truth rule bands `needs_timing` (< 20 s). So the false-BLOCK cost is concentrated in two
constants — one MEASURED-but-2x-widened, one never-measured — and the classifier's own logic
produced zero false BLOCKs. This is exactly what the calibration loop (prompt 8) should read: the
harness points at `validation_scan` and the `heap_rewrite` upper-widening, by name.

### Where it falls short, stated plainly

**UNSAFE recall is 53.8% — 12 of 26 true-UNSAFE statements were not called UNSAFE — and the
breakdown is the honest part.** Nine are runtime data/dependency failures no static tool can see:
a FK that fails on one orphan row, `SET NOT NULL` on a column with NULLs, `varchar(5)` shrink on a
too-long value, a CHECK violated by existing rows, a duplicate-key UNIQUE, using a new enum label
in the same transaction, dropping a type a column depends on. The engine does not call these
safe — it says UNKNOWN for the two ADD-COLUMN-NOT-NULL cases (it cannot know whether the table is
empty) and `needs_timing` for the rest — but "will error" is a fact of the *data*, and the engine
assesses locks and durations, not whether rows satisfy a constraint. That is a real recall ceiling
and it is inherent, not a bug: a migration linter is not a dry-run.

The other **three missed UNSAFEs are genuine lenient misses in the dangerous direction, and all
three are documented judgment calls this file already recorded**:
- `type_ts_tstz_nonutc_5m` — the timestamp-to-timestamptz conditional-branch cap. The engine caps
  at `needs_timing` because under a UTC session it is a no-op; under the non-UTC session this case
  forced, it rewrote 5M rows in 107 s. The cap trades this miss for not crying wolf on every
  ts->tstz under UTC, and the engine can see the server's TimeZone but not the migration session's.
- `upd_do_loop_batches_1m` — matched-row DML is **never** predicted UNSAFE by design ("matched
  rows are bounded; do not guess"). Ten sequential windows in one DO block ran 15 minutes; the
  engine said `needs_timing` with a batching suggestion. The design choice is deliberate and this
  is its cost.
- `conc_addcol_idle_holder_long` — a brief ACCESS EXCLUSIVE behind a 25 s idle-in-transaction
  holder that the snapshot *did* list in `long_transactions` (aged past the 60 s threshold) but
  with no waiter yet queued. The engine lifts one tier on a listed long transaction, not to
  UNSAFE; the acquisition actually blocked everything for 25 s. Escalating a listed idle holder
  straight to UNSAFE would be the fix, and the harness is the argument for it.

None of the 13 lenient misses is a duration-model *underestimate* on a plainly-blocking DDL — the
model never under-banded a rewrite, scan, or index build's blast radius. Every lenient miss is
either a runtime error (6), a concurrency shape the snapshot can't fully see (4: the three above
plus `conc_fk_idle_writer_on_parent` and `conc_create_index_tiny_rowexcl_holder`, brief locks that
waited behind an idle writer the snapshot didn't surface, and `create_or_replace_view_existing`,
the OR-REPLACE taxonomy gap), or a documented cap (3). SAFE precision is 95.1%: the engine rarely
blesses something that wasn't safe, and the four exceptions are the same runtime-error and
snapshot-blindness cases.

### Two harness limitations recorded rather than buried

**Lock-wait attribution takes the first granted target lock, not the slowest.** When a statement
locks two relations and only the second contends (`conc_fk_idle_writer_on_parent`: SHARE ROW
EXCLUSIVE granted instantly on the child, waited 8 s on the parent), `wait_ms` reads 0 — but the
statement's wall clock still captured the block, so the ground-truth tier came out right
(`needs_timing`) and the traffic probe corroborated it (8 s write stall). The attribution field is
imperfect; the label it feeds is not affected for single-statement cases. **Concurrency truth is
what one uncontended machine's holder produced**, and the snapshot the engine saw is a genuine
pre-migration capture, so the concurrency family measures the engine's real blindness to
idle-in-transaction holders — which is the finding, not noise.

### One machine, and what that costs

Every absolute number here is one uncontended NVMe laptop, and the calibration series proves the
machine was not even stable *within the run* (0.47-0.80x against the reference across probe
passes). Ground truth is normalized for that, so the **tiers and the disagreements transfer**; the
raw milliseconds bound production from above only. Nothing was tuned to move any of these numbers
— the corpus, the labeling rule, and the constants are exactly what they were when the run
started, and the run behind this section is committed at `artifacts/validation/`
(`results_2026-08-22.json`, the report, the digest, and a README) so the figures are reproducible
rather than asserted. Suite: 883 -> **903 tests** (20 new: corpus shape, the labeling rule, the
calibration arithmetic), ruff and mypy `--strict` clean, full run against real zonky PG 17.10.


## Fixing the measurements behind the false BLOCKs (2026-08-22)

The validation harness put UNSAFE precision at 70% against a 98% bar, and its own
attribution was right: all six false BLOCKs traced to two duration constants and one
escalation rule, none to the classifier. This session fixed the measurements the
harness pointed at — by name — and left the corpus, the labeling rule, and the
thresholds untouched, then re-ran it.

Before touching anything I checked the two constants against the code rather than the
prose, and the prose was wrong in a way that mattered. The previous section wrote that
the three `heap_rewrite` false BLOCKs came from "the 2x upper widening on the MEASURED
`heap_rewrite` constant." Both halves were false: `heap_rewrite` was `UNCALIBRATED` in
`constants.py`, not `MEASURED`, and it therefore carried the **4x** guess band, not 2x
— which is exactly why the 1M-row rewrite's upper bound read 40 s (10 s point × 4)
rather than 20 s. `validation_scan` was likewise `UNCALIBRATED` (500k rows/s, 4x). The
"never-measured guess" framing was the accurate one; the "MEASURED, 2x" description was
not. The fixes below start from the code as it actually was.

### 1. validation_scan: measured, the way fk_validation was

`validation_scan` was the last throughput constant still carrying a bare `guess:` basis,
and it drove two false BLOCKs (`setnn_plain_5m`, `check_add_5m`): 500k rows/s × 4x put a
5M-row scan's upper bound at 40 s, over the 20 s full-block line, while the scans
actually held ACCESS EXCLUSIVE for a normalized 4–6 s. I measured it the way
`fk_validation` was measured — real fixtures at 1k/100k/1M/10M under BEGIN…ROLLBACK with
`pg_locks` sampling, two passes so run-to-run variance is known, and the validation
harness's hardware normalization (probe the reference cases, divide by the committed
reference run) so the rate is expressed in reference-machine terms rather than this
laptop's. A read-only sequential scan with one predicate per row (ADD CHECK, SET NOT
NULL without a proving CHECK) ran at **~0.9–2.3M rows/s normalized at 1M/10M**; the three
committed scale-harness runs corroborate (0.64–1.56M at scale). I set it to a round
**1,000,000 rows/s**, near the slowest at-scale reading (ADD CHECK at 10M, ~0.92M),
`MEASURED`. At 5M that is a 5 s point and a ~10 s upper bound — NEEDS_TIMING, matching
the measured hold — so both false BLOCKs clear and no scan case (none is truly UNSAFE at
the corpus sizes) turns lenient.

### 2. heap_rewrite is bimodal; the split is what makes the tight band safe

The three remaining constant false BLOCKs (`type_int_bigint_1m`, `type_text_varchar_1m`,
`type_int_text_using_1m`) are all **plain ALTER COLUMN TYPE relabels** at 1M, which held
ACCESS EXCLUSIVE for a normalized 15.8–18.6 s — NEEDS_TIMING, under the 20 s line. The
brief said to tie the widening to calibration status and measurement variance rather
than a flat multiplier, because a constant measured twice and accurate both times should
not carry a guess's band. That is right, but tightening `heap_rewrite`'s band alone would
have traded three false BLOCKs for **three lenient misses in the dangerous direction**:
at the *same* 1M size, `addcol_volatile_default` (21 s), `addcol_serial` (24 s), and
`addcol_generated_stored` (26.5 s) are genuinely UNSAFE, and they share `heap_rewrite`'s
point estimate exactly. One constant cannot be both accurate for the fast relabel and
safe for these.

The measurements say why: `heap_rewrite` is **bimodal**. A plain relabel/format rewrite
(ALTER COLUMN TYPE, SET LOGGED/UNLOGGED, tablespace/access-method, VACUUM FULL, CLUSTER)
copies the heap and rebuilds indexes with no per-row computation and runs at ~87–105k
rows/s across both committed scale runs — tight. An ADD COLUMN that rewrites the heap
*and computes a value per row* — `gen_random_uuid()` for a volatile default, `nextval()`
for serial/identity, an expression for GENERATED STORED — runs slower, gen_random_uuid
the slowest at 28–64k rows/s. So `heap_rewrite` split, exactly as `index_build` split
btree from expression for the same "one constant hides a real split" reason:

- **`heap_rewrite` 100,000 rows/s, MEASURED** — the plain relabels only. Three runs
  agreeing within ~1.3x, so a 1.5x band. At 1M that is a 15.1 s upper bound: NEEDS_TIMING,
  fixing all three false BLOCKs. At 5M it is 75 s: still UNSAFE.
- **`add_column_rewrite` 45,000 rows/s, MEASURED** (new) — the compute-per-row column
  adds. Slowest-at-scale chosen, as for `fk_validation`/`dml_update`, so a 1M-row add
  bands UNSAFE (its ~22 s hold really does breach the outage line) and the fast relabel's
  tight band can never under-predict it.

`ADD COLUMN` with a constant/domain default stays on `heap_rewrite`: it writes one fixed
value per row at relabel speed (measured 82–125k), which is why `addcol_domain_constrained_default`
is correctly plain-speed.

### The widening is now derived from provenance, not a per-tier constant

`base_widen_tenths(constant)` replaces the flat `_base_tenths(calibration)`: a
`DurationConstant` now carries `runs` and `spread_tenths` (the observed max/min normalized
rate across its runs and representative shapes), and the band is derived from them —
UNCALIBRATED → 4x; measured once → 3x (a guess with better manners: anchored, but its
run-to-run variance is unknown, and the AEL-floor session already showed one laptop
disagreeing with itself ~1.5x); measured twice or more → the observed spread, floored at
the documented ~1.5x drift and capped at 2x; CALIBRATED → 1.5x. So `heap_rewrite` (spread
~1.3x) earns 1.5x while `fk_validation` (shapes scatter 3.8x) is held at the 2x cap, and
the other measured constants land where their spread puts them (btree 1.7x, expression
1.6x, index_bytes/dml_update at their cap). The floor and cap encode the honest limits: a
one-quiet-laptop measurement bounds production only from above, and never so tightly that
2x-slower storage escapes the band. This tightening only ever lowers an upper bound, so it
can fix a strict over-prediction but cannot manufacture a false BLOCK, and no measured
constant's true-UNSAFE case sits close enough to a boundary to turn lenient.

### 3. Contention: an idle holder is a timing problem, not an active block

The sixth false BLOCK, `conc_addcol_idle_holder_short_visible`, was the escalation rule:
observed `pg_locks` traffic escalated a brief-AEL ADD COLUMN (already NEEDS_TIMING under
the AEL floor) one tier to UNSAFE, but the 8 s hold behind an *idle* holder bands
NEEDS_TIMING. The holder there is idle-in-transaction — holding ACCESS SHARE, running
nothing — which is precisely the transient queue a `lock_timeout` + retry clears, and
that is the remedy NEEDS_TIMING already prescribes. Escalating it to UNSAFE double-counts
the acquisition-queue risk the statement is already flagged for.

So the escalation now distinguishes idle from active. `LockWaiter` gained
`blockers_all_idle` (SNAPSHOT_FORMAT 3 → 4): the capture reads each conflicting holder's
`pg_stat_activity.state`, and the fact degrades to unavailable when stats are masked
(no pg_read_all_stats) — an unknown holder is never assumed idle. `ContentionAssessment`
carries `active_conflict`, and `_escalate_for_contention` lifts either safe tier to
NEEDS_TIMING on any observed conflict (an idle holder still needs a window) but pushes an
already-timed statement to UNSAFE only when the conflict is active. The escalation-to-
UNSAFE path is unchanged for active contention.

This has a cost the harness makes explicit and I am not tuning away: the engine cannot see
*how long* an idle holder will hold, so `conc_addcol_idle_holder_long_visible` (a 25 s idle
hold, truth UNSAFE) is now a lenient miss rather than a match — the same 8 s and 25 s idle
holders are indistinguishable in a snapshot, and softening the 8 s case necessarily softens
the 25 s one. It joins the two idle-holder lenient misses the previous section already
recorded; all three are the snapshot's real blindness to idle-in-transaction duration, not
a scoring error.

### Re-run: the validation harness, unchanged

Same 172-case corpus, same labeling rule, same thresholds, nothing tuned to the run.

| tier | predicted | truth | precision | recall |
|---|---|---|---|---|
| **UNSAFE** | 13 | 23 | **69.2%** | 39.1% |
| NEEDS_TIMING | 98 | 85 | 77.6% | 89.4% |
| SAFE_IRREVERSIBLE | 11 | 14 | 81.8% | 64.3% |
| SAFE | 82 | 90 | 95.1% | 86.7% |

**UNSAFE precision is 69.2% — still short of the 0.98 bar, and the honest reading is that
this run's hardware normalization flipped a *different* subset of boundary cases than the
run that set the target.** The headline barely moved (70.0% → 69.2%), but the headline
hides the fix, because the same normalization variance the whole project has warned about
(±~1.5x, one laptop against itself) moved several cases across the outage thresholds
between the two runs. Two controlled comparisons isolate the engine change from that noise,
and both show the fix working:

- **The new engine on the *first* run's measured holds — the run that named the six false
  BLOCKs — scores UNSAFE precision 100.0% (0 false BLOCKs), up from 70.0%.** All six clear:
  the two `validation_scan` scans and the three plain `heap_rewrite` relabels drop to
  NEEDS_TIMING to match their measured 4–19 s holds, and the idle-holder contention case
  no longer escalates. Recall moves 53.8% → 50.0% — the one point is `conc_addcol_idle_holder_long_visible`,
  the contention trade named above.
- **The new engine beats the old on *this* run's holds too: 55.0% → 69.2%, nine false
  BLOCKs down to four.** The five it fixes here are `type_text_varchar_1m`,
  `type_int_text_using_1m`, `setnn_plain_5m`, `check_add_5m`, and
  `conc_addcol_idle_holder_short_visible` — the same mechanisms, landing on the safe side
  of the boundary this run.

The new engine strictly dominates the old on both runs. File-level BLOCK precision tracks
UNSAFE exactly (69.2%/39.1% this run; 100% on the first run's holds).

### What remains, stated plainly

**All four residual false BLOCKs are boundary cases within 17–23% of an outage threshold,
where the harness's own "hardware decides" rule applies — and two of them are on constants
this session never touched.** Every one measured UNSAFE in the first run and NEEDS_TIMING in
this one:

- `addcol_volatile_default_1m`, `addcol_generated_stored_1m` — compute-per-row adds on
  `add_column_rewrite`. Normalized hold 16.6 s this run (x0.83 of the 20 s line), 21–27 s
  last run. The constant models them at the worst-case ~22 s (UNSAFE); this run's
  normalization put the measurement just under. The *old* engine flagged both too
  (`heap_rewrite` × 4x = 40 s), so the split did not create these.
- `upd_nowhere_1m`, `del_nowhere_5m` — full-table DML on `dml_update` / `dml_delete`,
  **unchanged this session**. Normalized 46 s (x0.77 of the 60 s write line) and just under
  60 s; 85 s and 72 s last run. Pure normalization drift on constants I did not touch.

None is reachable without either tuning to one run's labels or making the engine less
conservative at the outage line — and the cost of the latter is visible in the same run:
`type_int_bigint_1m`, whose `heap_rewrite` 1.5x band correctly called it NEEDS_TIMING to
fix last run's false BLOCK, measured just *over* 20 s this run and so reads as a lenient
miss. That is the boundary being a boundary: at 1M rows a plain relabel holds ACCESS
EXCLUSIVE for a normalized 15–21 s, straddling the line, and no fixed constant lands every
run on the correct side. Per the brief, these are reported, not tuned away.

The rest of the miss ledger is unchanged in character from the first run and equally
inherent: nine of the fourteen missed UNSAFEs are runtime data/dependency failures no
static lock-and-duration model can see (an FK failing on one orphan row, SET NOT NULL on a
column with NULLs, a duplicate-key UNIQUE, using a new enum label in the same
transaction), and the remainder are the two documented caps (the ts→tstz TimeZone
conditional; matched-row DML never predicted UNSAFE) plus the idle-holder-duration blind
spot the contention change makes explicit. UNSAFE recall (39.1%, down from 53.8%) is the
mirror of the precision story — this run's normalization pushed more boundary holds *over*
the line into UNSAFE truth than the engine's conservative-but-bounded upper bounds reach,
which is the same variance, counted from the other end.

Suite: 903 → **909 tests** (6 new: the variance-tied widening derivation, the split
constants banding a 1M rewrite correctly on each side, and the idle/active contention
carve-out from three directions), ruff and mypy `--strict` clean, full run against real
zonky PG 17.10. The re-measurement script and its normalized two-pass results are in the
session scratchpad; the new validation run is committed beside the first under
`artifacts/validation/` (`*_remeasured.*`), the first kept because the prior section's
numbers cite it. Caveats unchanged and, if anything, sharpened by this run: one uncontended
NVMe laptop, measured rates are ceilings, and MEASURED is still not the fitted
cross-environment calibration prompt 8 owes — the widening's 2x cap and 1.5x floor are the
admission that one machine cannot supply it, and the boundary cases that flipped between the
two runs are the proof that it must.


## Replicated measurement, the target's hardware as an input, and refusing at the boundary (2026-08-23)

The previous section ended with four false BLOCKs that were all within 17–23% of an
outage threshold and flipped run-to-run under hardware normalization, and the brief for
this session was the right conclusion from that: single-machine measurement, however
well normalized, cannot decide those cases, so fix the measurement infrastructure and
not the constants. Three changes were asked for — measure every constant across three
distinct hardware profiles and derive the band from the cross-machine spread; make the
target's hardware an input by putting a calibration probe in the live snapshot; and
refuse to decide, rather than coin-flip, where an estimate straddles a threshold by less
than the constant's known spread — then re-run the validation harness unchanged. Two of
the three are done and measured below. The first is built, exercised end to end on one
profile, and blocked on credentials only the owner of this machine can supply; that is
recorded plainly, not buried, because the band the boundary rule uses today is the
single-profile floor and not the cross-profile spread it is designed to carry.

### 1. Replicated measurement: the infrastructure is built, the cloud profiles did not run

`artifacts/scripts/measure_profiles.py` is the per-profile half of the replicated
measurement. It runs unchanged on any profile (Linux, macOS, Windows), seeds the scale
schema at 1k/100k/1M/10M, runs the fourteen representative statements that stand for
the ten duration constants (the scale harness's and the 2026-08-22 re-measurement's SQL,
verbatim) under BEGIN…ROLLBACK with `pg_locks` sampled, two passes so within-profile
variance is known per profile, and — the part that makes profiles comparable — reads
the calibration probe (the same bounded read-only operation the snapshot captures,
section 2) before, between, and after the passes, so every rate is paired with the probe
reading of the machine state that produced it. It records the CPU model and count, the
memory, and the disk under `PGDATA`. `artifacts/scripts/derive_constants.py` takes N
profile files and emits, per constant: the slowest-at-scale value on each profile, the
probe on each profile, each value *as the probe predicts it on the anchor profile*, and
the spread of those probe-scaled values — the residual the probe does not explain, which
is what the constant's band and the boundary rule's strip are defined to be. A constant
gained the provenance to carry this: `profiles`, `per_profile` (the observed value on
each profile, so the spread can be read rather than trusted), and
`cross_profile_spread_tenths`.

The three cloud profiles did not run. The plan was GCE — an `e2-small` on `pd-standard`
(HDD-class network disk, the slow end), a `c3-standard-4` on `pd-ssd`, and an
`n2-standard-4` on a local NVMe SSD — and `artifacts/scripts/run_profiles_gce.sh` creates
them, ships the measurement bundle, runs the script under `nohup`, polls, collects, and
deletes them in one command. The `gcloud` install on this machine is authenticated to a
project but its refresh token is dead (`invalid_grant`), and re-authenticating needs a
browser session only a person can complete. The fallback was GitHub-hosted runners,
which are genuinely distinct hardware (an x86 Azure VM, an ARM64 Cobalt VM, Apple
silicon with local NVMe, a Windows x86 VM) — `artifacts/scripts/measure_profiles_workflow.yml`
is that workflow, one job per profile, and a bundle of exactly the eleven files the
script imports was assembled so that nothing beyond the measurement code would leave the
machine — but creating the private repository to hold it was refused by the session's
permission gate, and pushing code to an external service is not something to route
around. So: one command away on either path, and a decision the owner has to make.

What ran is the anchor profile, this laptop, under the new script
(`artifacts/profiles/laptop-nvme.json`, 1414 s). That exercised the pipeline end to end
and did something necessary on its own: **every constant was re-anchored to a run in
which the probe was read alongside it**. The earlier values came from runs with no probe,
so there was no probe reading to pair them with, and a constant can only be scaled by a
probe ratio if its value and the anchor reading come from the same machine state. The
laptop was 1.2–2x slower in this run than in the state the 2026-08-21/22 constants were
fitted in (its compute probe read 196/220/215 ms; the anchor is the median, 215 ms), and
the re-anchored table records the drop constant by constant: `index_build_btree` 1.0M →
740k rows/s, `index_build_expression` 250k → 170k, `validation_scan` 1.0M → 790k,
`fk_validation` 1.2M → 1.0M, `dml_update` 22k → 13k, `index_bytes` 9.5 → 7.8 MB/s,
`add_column_rewrite` 45k → 55k (gen_random_uuid ran faster this time; the value is still
the slowest shape). Two are worth their own sentence. `heap_rewrite` read 74–84k at 1M
and **45–52k at 10M** — a 3 GB heap copy is write-path-bound, and the compute probe
cannot see the write path. The slowest-at-scale convention would take 45k, which would
model a plain relabel as *slower* than a compute-per-row add and undo the 2026-08-22
split; the value is the 1M reading, 74k, the 10M drop is what sets its 1.9x spread, and
the exception is recorded in its basis. `dml_delete` was the last throughput guess in
the table (100k rows/s, "~2x the update rate"); the same script measured it at
632k–1.02M, six times faster, which with its 4x guess band is exactly what put
`del_nowhere_5m`'s upper bound at 200 s in both earlier runs. It is `MEASURED` now, by the
same method as everything else. `constant_op` stays a guess: commit latency is a
property of the storage path no read-only probe can measure, and it is never banded.

Every constant therefore carries `profiles=1`, and `boundary_spread_tenths` — the
half-width of the refusal strip — treats one profile, however many passes, as an
unknown spread and applies the floor (1.5x, the documented same-machine drift). When
profile files land, the derivation script re-runs, the three provenance fields are set
from it, and the strip becomes the observed residual without another code change.

### 2. The target's hardware is an input

Snapshot format 5 adds `LiveSnapshot.calibration` (`CalibrationFacts`): the capture
runs a fixed unit of work on the target and records the time. The probe is a sort of a
generated series — `SELECT count(*) FROM (SELECT g FROM generate_series(1, 500000) g
ORDER BY (g::bigint * 7919) % 1000003)` — pure backend CPU and `work_mem` sort machinery,
which is what an index build, a rewrite's per-row work, and a validation scan's
predicate spend their time on. It runs three times and the minimum is kept (the minimum
is the machine's capability; the spread above it is whatever else the server was doing),
inside its own savepoint under the snapshot's `statement_timeout`, last in the capture
so it delays nothing and is observed by nothing. It touches no relation, takes no lock,
and reads no user data.

That last clause was a design decision this session almost got wrong. The first cut
had a second component, a bounded heap read of the largest captured table — real pages
through the real buffer cache, the nearest thing to a disk probe — and it was removed
before any run used it, for a reason the tests would not have caught: `docs/minimum-
privilege-role.md` promises that Hydro Scan never queries user tables and the documented
role has no `SELECT` on them, so the scan probe would have both broken the promise and
failed under the role it was documented for (the harness's own read-only role is
`pg_monitor` only, and would have degraded it silently). Every way of reading real heap
pages needs `SELECT` on a user table; there is no disk probe that respects the contract.
The consequence is stated in `calibrate.py` and it matters for the whole design: **the
probe sees the CPU and sort path and not the write path**, so the residual the write
path leaves across hardware — the 10M `heap_rewrite` drop above is an instance — is
precisely the cross-profile spread the constants are meant to carry and the boundary
rule is meant to refuse inside. The probe narrows the estimate; the spread says how far
the probe falls short; the measurement script still records a scan probe, as
information for the derivation, so a future reader can see how much of the spread a disk
probe *would* have explained. The privilege doc now says all this.

`hardware_factor_tenths` turns the reading into a factor against the anchor's 215 ms,
clamped to 0.3x–8x (outside every measured profile, with the clamp recorded in the
note), and every proportional estimate — rows, bytes, dependent-index rebuilds — is
multiplied by it before the band is applied. The estimate's `inputs` carry the scaling
in the open (`hardware: compute probe 612 ms vs anchor 215 ms -> x2.8`), and a snapshot
with no usable reading leaves the estimate unscaled and says `hardware: unscaled
(...reason)` — the assumption the model always silently made, now visible. A format-4
snapshot constructs with an unavailable probe; nothing downstream needs to know.

The validation harness's truth basis changed with this, and deliberately. The harness
normalized measured holds into reference-machine milliseconds because that was the only
way to compare a hardware-blind estimate with a measurement; now the estimate is scaled
to the machine the case ran on, the thresholds are wall-clock outage lines on that
machine, and the comparable truth is the **raw** hold. `score()` labels on the raw hold
for every case whose snapshot carried a probe reading, keeps the normalized label as the
diagnostic it has become (`tier_normalized`, and the normalization-sensitivity section
still reports both), and falls back to it for a case whose capture failed. The corpus,
the labeling rule, and the four thresholds are untouched; the old runs re-score to their
published numbers through the new scorer (69.2% / 39.1% for the remeasured run).

### 3. The boundary-proximity rule

A threshold is a line; an estimate is a strip. `_boundary_refusal` draws the strip
`[point / S, point × S]` around the hardware-scaled point estimate, where `S` is the
constant's known spread — the observed cross-profile residual once it exists, the 1.5x
floor until then, the 4x guess band for an uncalibrated constant — and if a tier
threshold lies inside it, the engine returns UNKNOWN with `refusal="boundary"`,
`refused_from` (what the upper-bound rule would have said), and the two tiers it is
refusing between. The rationale states the point and interval, names the line, the
spread, and the probe reading, and the condition tells the reviewer what resolves it
(time it on a production-sized copy of the target; with no measurement, treat it as the
slower tier). File level, UNKNOWN is `requires_approval`, which is the thirty seconds
the brief priced it at. Two details keep the rule from firing where it would mean
nothing: the strip is drawn around the proportional part of the estimate (the fixed
overhead is not hardware), and the SAFE/NEEDS_TIMING line is ignored where the ACCESS
EXCLUSIVE floor makes both sides NEEDS_TIMING — a refusal is only issued where the two
sides of the line are different verdicts. The upper bound still decides everything
outside the strip, with the staleness widening on top, so a stale-statistics estimate
that clears the line is as conservative as before.

The report carries `refusal` and `refused_from` per statement and per row; the harness
scores every refusal's `refused_from` against the truth so the ledger shows what each
refusal replaced — a false BLOCK absorbed (`strict`), a correct call given up (`match`),
or a lenient miss hidden (`lenient`).

### Re-run: the validation harness, unchanged

Same 172-case corpus, same labeling rule, same four thresholds; nothing tuned to the run.
The run is committed as `artifacts/validation/*_2026-08-23_hardware.*`. It happened on a
laptop that was throttled to 1.3 GHz when it started and un-throttled partway through:
the harness's own nine-statement machine factor read **2.93x** slower than the reference
at the start probe and **0.79x** at the end — a 3.7x swing inside one run, the worst
the project has seen. That is not a complaint about the run. It is the condition the
probe exists for, and it made the run a harder test than a quiet one would have been.

| tier | predicted | truth | precision | recall |
|---|---|---|---|---|
| **UNSAFE** | 12 | 30 | **100.0%** | 40.0% |
| NEEDS_TIMING | 91 | 82 | 78.0% | 86.6% |
| SAFE_IRREVERSIBLE | 12 | 13 | 83.3% | 76.9% |
| SAFE | 77 | 87 | 94.8% | 83.9% |

Outcomes: 166 match, 11 strict, 15 lenient, **20 UNKNOWN, of which 12 are boundary
refusals**. File level: BLOCK precision 100.0%, recall 40.0%; the twelve refusals land in
`requires_approval`.

**UNSAFE precision is 100% — zero false BLOCKs — for the first time on a fresh run, and
the trade the brief priced is visible in the refusal ledger.** Scoring each refusal's
`refused_from` (what the upper-bound rule would have said) against the truth: **7 of the
12 refusals absorbed a false BLOCK** (`idx_gin_5m`, `type_varchar_widen_partial_idx_5m`,
`setnn_plain_5m`, `check_add_5m`, `unique_add_5m` — all would have been UNSAFE against a
measured NEEDS_TIMING — plus two NEEDS_TIMING-vs-SAFE refusals, `idx_composite_1m` and
`idx_if_not_exists_existing`), **5 gave up a call that was right** (`idx_expr_5m`,
`addcol_generated_stored_1m`, `type_text_varchar_1m`, `txn_ael_then_backfill_1m` were
truly UNSAFE; `del_nowhere_5m` truly NEEDS_TIMING), and **none hid a lenient miss**.
Without the rule the same run scores UNSAFE precision 76.2% (16 of 21) and recall 53.3%;
with it, 100% and 40.0%. UNSAFE recall fell as expected — 39.1% → 40.0% is flat against
the previous run only because this run's throttled first half pushed more holds over the
line into UNSAFE truth (30, up from 23), and the engine's probe-scaled estimates followed
them there (12 true UNSAFEs called, up from 9). Every one of the four residual false
BLOCKs the previous section left — `addcol_volatile_default_1m`,
`addcol_generated_stored_1m`, `upd_nowhere_1m`, `del_nowhere_5m` — is gone: the first is
a match (UNSAFE, a 40 s hold on the throttled machine), the second and fourth are
refusals, the third is a match (UNSAFE: the same 1M-row UPDATE that held 46 s last run
held 207 s on the throttled machine, and the probe-scaled `dml_update` estimate was over
the line with it).

**The probe works, and the number that says so is the correlation with the harness's
own calibration.** The snapshot's single 500k-row sort read 171–1064 ms over the 172
captures (median 400 ms against the 215 ms anchor), and its ratio to the anchor tracks
the harness's interpolated nine-statement machine factor at **Pearson r = 0.82** across
the run — 2.2x at 130 s in, 2.9x at 1900 s, 0.83x at 3200 s, 0.93x at the end. A one-
query probe inside the snapshot reproduced what the harness had to run nine reference
statements and interpolate between passes to estimate. That is also why the truth basis
could move to the raw hold: scoring this run on raw holds gives 166 matches, scoring it
on the old reference-normalized labels gives 164, and the two disagree on only the
boundary cases — the probe-scaled estimate and the raw measurement are in the same units.

**What the probe does not see is visible in the same table, and it is the write path.**
The refusals on `validation_scan` and `index_build_btree` at 5M (`setnn_plain_5m`,
`check_add_5m`, `unique_add_5m`, `type_varchar_widen_partial_idx_5m`) all measured 10–16 s
raw against an upper bound of 30–34 s: the throttled CPU made the probe read ~2.5x, the
estimate scaled with it, but a sequential scan or a sort-dominated build on a warm cache
did not slow down as much as the sort-heavy probe did. They were refused, correctly,
rather than blocked — the rule caught exactly the case it was written for — but a probe
with a storage component would have landed them. The privilege contract (section 2)
forbids that probe; the cross-profile spread is what is supposed to stand in for it, and
this run is the argument for running the profiles: the strip the rule used here is the
1.5x single-profile floor, and every refusal above sits inside a 1.5x strip that the
real cross-profile residual may well be narrower than on the scan and build families.

**The probe's own noise is the floor of what it can do.** Min-of-three readings taken
minutes apart on the throttled machine spread 412–709 ms (1.7x) with no change in the
harness's factor between them; on the un-throttled second half they spread 171–200 ms.
The anchor is a median over three probe passes for the same reason. A constant's 1.5x
floor is not smaller than the probe's own jitter on a bad day, and should not be.

The fifteen lenient misses are the same population as before — seven runtime data and
dependency failures no lock-and-duration model can see (`fk_orphans_fail`,
`setnn_fails_on_nulls`, `check_add_fails`, `unique_add_fails_duplicates`,
`type_varchar_shrink_fails`, `drop_type_in_use_fails`, the enum-in-same-transaction
case; the two ADD COLUMN NOT NULL failures are UNKNOWN, as before), the three documented
caps (`type_ts_tstz_nonutc` at 1M and 5M under a non-UTC session — the 1M case is a
lenient miss this run only because the throttled machine held it 29 s; and
`upd_matched_narrow_looking_5m`, matched-row DML never predicted UNSAFE), the two
idle-holder-duration cases, two brief locks behind idle writers the snapshot does not
surface, and the OR-REPLACE taxonomy gap. Nothing lenient is new, and nothing lenient is
a duration-model underestimate on a plainly blocking DDL.

What is owed, in order. The three cloud profiles, so the strip is an observed
cross-hardware residual rather than a floor — the runbooks are in `artifacts/scripts/`
and the derivation script reads their output directly. Then a read of the refusal
ledger against that residual: if the scan and btree families' cross-profile spread after
probe scaling is tighter than 1.5x, four of this run's seven absorbed false BLOCKs become
clean NEEDS_TIMING calls with no rule change. And the anchor itself should move to a
cloud profile once one exists, because a laptop that swings 3.7x within an hour is a
poor thing to express a constant against, however well the probe tracks it.

Suite: 909 → **925 tests** (16 new: the probe-to-factor mapping and its clamp, scaling
through every estimate path, the refusal on both lines and its absence under the AEL
floor, the probe moving one table across the strip, the report payload, and three live
tests — the probe reads without `pg_monitor`, survives an ACCESS EXCLUSIVE holder on the
target, and serializes), ruff and mypy `--strict` clean over `src`, `validation`, and
`tests`, full run against real zonky PG 17.10. Tests that pinned the old rates or used
"big" tables that now land inside the strip were moved clear of it (a BIG table is 400M
rows, not 40M) and rewritten to derive row counts from the constants, so the next
re-anchoring does not re-break them.

## The CI integration: a check that runs without being remembered (2026-08-23)

Everything before this session produced a verdict when someone asked for one. This
session makes the asking automatic: `blastoise ci` — one command that finds the
migrations a change touched, assesses them, and publishes the result — plus a GitHub
Action and a Docker image that are both thin wrappers around it. There is deliberately no
second implementation: the Action's `run:` step and the GitLab/Buildkite `docker run` are
the same argv, so the thing a team runs on GitHub and the thing they run elsewhere cannot
drift. `blastoise.ci` is 8 modules — detection, config, redaction, the result model, the
Markdown renderer, the GitHub client, the runner, and the package's own facade — and it
imports from `blastoise.verdict` and `blastoise.report` exactly as the CLI does; nothing
in the engine knows it exists.

### 1. Detection: the part that decides whether it works out of the box

A CI tool that needs a path list before it does anything is a tool that gets configured
once, wrongly, and then distrusted. So detection matches the layouts the frameworks
already impose, as **ordered rules over paths, first match wins**: Prisma
(`prisma/migrations/*/migration.sql`), Rails (`db/migrate/`, and `db/<name>/migrate/` for
multi-database setups), Alembic (`*/versions/*.py`, whatever the parent directory is
called — `alembic/`, `migrations/`, `db/migrations/` are all in the wild), Django
(`*/migrations/*.py`), Flyway (`V<digit>…__*.sql`, `U…`, `R__…`), golang-migrate
(`*.up.sql` / `*.down.sql`), and a plain `migrations/` or `migration/` directory of
`.sql`. Every pattern is anchored `(?:^|/)`, so a monorepo's
`services/billing/db/migrate/` is found exactly like a root-level one — which is the case
the layout conventions are silent about and the one most likely to be a monorepo.

Ordering rather than scoring, because a scored match is one more thing to explain when it
surprises someone. It resolves the three real collisions: `prisma/migrations/x/migration.sql`
also matches the generic `migrations/` rule and is Prisma; `migrations/0001.up.sql` also
matches it and is golang-migrate; `migrations/versions/x.py` matches Alembic and not
Django, because Django's rule requires the `.py` directly inside `migrations/`.

Three judgment calls, flagged rather than buried:

- **Flyway is matched by file name at any depth**, because its location is configurable
  and most projects move it (`src/main/resources/db/migration` is only the default). The
  guard against claiming every double-underscore file in the repository is that the
  version must start with a digit: `V1__init.sql` and `V1_2_3__add.sql` match,
  `Views__old.sql` does not. It is the one rule that is not directory-scoped and the one
  most likely to need an `exclude`.
- **`.down.sql` is detected too**, though the brief named only `*.up.sql`. A down
  migration is SQL that will run against production on a rollback, and detecting it as
  `generic` (which the `migrations/` rule would have done) while its sibling is
  `golang_migrate` would be worse than either answer alone.
- **Detection is over paths, never contents.** Reading files to sniff their type would
  make the tool behave differently depending on whether a file survived the diff, and
  would classify a renamed-away migration by whatever replaced it.

`.blastoise.yml` is the escape hatch, and `migrations.paths` **overrides detection
entirely** rather than adding to it — a config that only added would leave a team unable
to turn a false positive off, which is precisely why they opened the config file.
`exclude` applies either way and is checked last. The globs are path globs, not `fnmatch`:
`*` stays inside one segment, `**` spans them (including none, so `a/**/b` matches `a/b`).
`fnmatch` was tried first and is wrong in the direction that matters — its `*` crosses
`/`, so `migrations/*.sql` would silently match `migrations/2024/01.sql` and `*.sql` would
match every SQL file in the tree.

Config validation is strict: an unknown key is an **error**, not a warning. The whole file
is optional, so the only way to have one is to have written it on purpose, and the failure
mode of a silently ignored key is a team believing their `exclude:` is in force when it is
not.

### 2. Rails, Django and Alembic: recognized, reported, and not assessed

Those three are a DSL. The statements they run do not exist until the framework renders
them, and the parser reads SQL. The requirement was to detect them and say so rather than
skip or crash, and that is what happens — per file, in the comment, naming the framework
and what support would take. But the verdict question the brief left open has an answer
this project's own rules force: **an unassessed migration holds the run at
`requires_approval`.** A green check on a pull request whose only migration was never read
is a worse outcome than no check at all, and it is the same principle as the boundary
refusal and as `unverified` never serializing empty. A file that could not be *parsed* is
treated identically — reported as a tool failure, never as a finding, contributing
`requires_approval` and never `block`, because "no verdict was produced" has never been
allowed to mean "the migration is dangerous" anywhere else in this codebase.

What each adapter would need, recorded in `DSL_ADAPTER_HINT` so the message a user sees
names the command rather than apologizing:

- **Rails** — `rails db:migrate` emits the SQL it runs, but only while running it. An
  adapter needs a dry-run that renders without applying: either a `Migration` subclass
  that captures `ActiveRecord::Base.connection` calls, or running the migration inside an
  aborted transaction against a scratch database with `ActiveRecord::Base.logger` capturing
  statements. Both need a booted Rails app in CI, which is the expensive part.
- **Django** — `django-admin sqlmigrate <app> <name>` already prints the exact SQL, which
  makes this the cheapest of the three. It needs the project's settings module and an
  importable app registry (so, the project's own Python environment), and it needs the
  migration's dependencies resolvable; `RunPython` operations render as a comment and would
  have to surface as an explicit "this migration also runs Python we cannot see".
- **Alembic** — `alembic upgrade <rev> --sql` renders offline into SQL, which is exactly
  the shape wanted. It needs `alembic.ini` and `env.py`, and offline mode fails on any
  migration that inspects the database at render time.

All three amount to the same thing: to read a DSL migration you have to run the
framework's own renderer, which means running the team's application code in the checker's
process. That is a materially larger trust boundary than "parse a .sql file", and it is
why they are not in this release rather than why they are impossible.

### 3. The connection string comes from a secret, and there is no other door

The Action declares **no input** for the database URL, and `blastoise ci` has **no flag**
that takes one. The config names an environment *variable*; the value comes from the CI
secret store. Three enforcement points rather than three sentences of documentation:

1. `database.url` and `database.password` in `.blastoise.yml` are refused outright with
   the reason (not merely "unknown key"), because that is the mistake that puts a
   credential in a committed file.
2. A value written where the *name* belongs — `url_env: postgres://…` — is refused, and
   the error does not echo the string it refused.
3. A name in the `INPUT_*` namespace is refused. That is how a GitHub Actions input
   arrives, so the refusal holds even if someone wires one up by hand. A workflow input is
   not a secret: it is echoed into the run's log, it is readable in the event payload a
   `workflow_run` can see, and under `pull_request_target` it can be influenced by whoever
   opened the pull request.

Redaction is a `Redactor` every output path goes through — the log, the comment, the job
summary, the machine JSON, exception messages, and tracebacks. Two mechanisms
deliberately, because neither is sufficient. **Literals**: the connection string and each
component parsed out of it (password, host, user, dbname) are registered and replaced
wherever they appear, which catches a driver formatting the host into a message in a shape
no pattern anticipated — libpq's `connection to server at "db.internal" (10.0.0.4)` is the
example that motivated it. **Patterns**: URI-shaped text and `password=` in keyword form
are replaced even when never registered, because the string that leaks may not be the one
we were configured with.

The parsing is stdlib-only rather than reusing `blastoise.live.redact_conninfo`, which
needs `psycopg`: the offline install has no driver, and the run that never connects still
has the credential in its environment. Two calibrations, both erring toward the secret:
component literals are held to a four-character floor and a stoplist (`localhost`,
`postgres`, `app`, `db`…) so ordinary prose is not shredded, but **a password is registered
however short and however generic** — mangling the word "app" in a comment is not
comparable to printing a credential. And `user=` / `host=` are *not* redacted by pattern,
only as literals, because `UPDATE t SET host = 'x' WHERE user = 'bob'` is ordinary SQL and
a redactor that rewrites migrations is a redactor people turn off.

### 4. The comment, and the one section that is never collapsed

Order is the argument: file-level verdict, then per-tier counts, then per file the same two
again, then — always — what was not verified. Statement detail sits in a `<details>`
toggle; **`unverified` does not, and that is a rule, not a style preference.** The tool's
whole claim is that it tells you what it does not know, and a disclosure widget is where a
reader's eye learns to stop. Statement detail is an elaboration of a verdict already stated
in the open; the limits of the verdict are not an elaboration of it. A test asserts that no
`Not verified` heading ever appears inside an unclosed `<details>`.

GitHub caps a comment at 65536 characters, and a run with eighty migrations exceeds it. The
degradation is a fixed ladder — drop statement detail, then cap the unverified list at 20
per file, then at 5 — and **every rung says what it dropped and where the complete version
is**, because a truncation nobody is told about reads as "there was nothing more". If even
the last rung overflows, the body is clipped with a visible final line rather than ending
mid-sentence. The unverified section is never the first thing dropped.

Two rendering details worth recording. Engine prose carries `->` and `<->` (the type-change
rationale says `timestamp<->timestamptz`), and a Markdown renderer that reads `<->` as an
opening tag eats the rest of the sentence — so prose is HTML-escaped on the way in, the
Markdown counterpart of the ASCII transliteration the terminal renderer needed. And table
cells escape `|`, because a rationale containing one shifts every later column.

Re-push updates the comment rather than adding one, found by an invisible `<!--
blastoise-ci-report -->` marker. The marker identifies the comment, **not the author**: the
token that posts is the workflow's, which is not a stable identity across a repository's
history of tokens and apps, and matching on author would orphan the comment the first time
that changed. A test with 150 unrelated comments proves the paginated search finds it, and
another proves other people's comments are left alone.

### 5. Neutral is why the Checks API is used at all

`proceed` → success, `requires_approval` → **neutral**, `block` → failure. A job's exit code
can only pass or fail, and "a human has to look at this" is neither — so the check status
goes through `POST /check-runs`, which needs `checks: write`. The check run attaches to
`pull_request.head.sha`, not `GITHUB_SHA`: on a `pull_request` event the latter is the
ephemeral merge commit, and a check run against it appears on nothing the pull request
displays.

The exit code is a separate, cruder signal, and `ci.fail_on` decides it: `block` (default)
fails the job only on BLOCK, `requires_approval` fails on that too, `never` reports without
failing — which is what a team introducing the check to a repository whose migrations were
never gated before actually needs. Exit 3 stays what it has always been: the run itself
failed, never a claim about the migration. The Action maps 3 to a failed step with an
`::error` that says so explicitly.

Publishing is never fatal. A pull request from a fork gets a read-only `GITHUB_TOKEN`:
both calls return 403, and failing the job would punish a contributor for a permission
model they do not control. The verdict stays in the job summary, the artifact and the exit
code, and the log says why the comment is missing.

### 6. One snapshot for the whole pull request

Every migration in the change is assessed against the **same** captured state — one
connection, one `snapshot_hash`, probes unioned across all the parsed scripts. Three
reports that agree with each other are three views of one database; three reports captured
seconds apart are three databases. It is also one connection instead of N, which matters
for a role with `CONNECTION LIMIT 2`.

**Staging by default**, and the honest version of why: the check wants a database with
production's *schema*, but it reads sizes and statistics from whatever it is given, so a
staging database at 1% of production's size produces verdicts calibrated to 1% of
production's row counts. That is a real limitation of the recommended configuration and it
is stated in the Action's README rather than left for someone to discover. The mitigations
are that the comment says which database answered (via a team-chosen `database.label` —
free text, never a host) and that `reltuples` staleness is already in every report's
`unverified`. Pointing it at a production replica works with the same three-statement role;
the trade is better numbers against one more thing holding a credential.

### 7. Changed-file discovery, and the shallow-checkout trap

Default is the GitHub API's pull-request file list, because it is computed against the
merge base server-side and therefore works with `actions/checkout`'s default
`fetch-depth: 1` — the single most common reason a "detect changed files" action fails on
first install. `git diff` is the fallback and the only option outside GitHub: three-dot
first (the changes the branch introduced), two-dot if that fails, because three-dot needs
the merge base and a shallow clone does not have it while two-dot needs only the two
commit objects. When both fail the error names the fix (`fetch-depth: 0`) rather than
reporting that git exited non-zero.

### 8. The Action is composite, and the artifact is uploaded before the verdict is applied

Composite rather than a Docker action, because a Docker action cannot call
`actions/upload-artifact`. The step order is load-bearing: the check's exit code is
*captured*, the artifact is uploaded, and only then does a final step apply the verdict.
A `BLOCK` that leaves you no evidence bundle to open is worth much less than one that
does. The Docker image exists for the pipelines the Action does not serve; it runs as a
non-root uid, sets `safe.directory` at `--system` scope (a caller passing
`docker run --user` to match its own uid would never see a `--global` setting written into
one user's home), and carries `git` for `--changed-source git` and nothing else.

### Tests, and what they hold

Suite: 925 → **1145 tests** (220 new, counting parametrized cases individually: five new
test files plus a fake GitHub API), ruff and mypy `--strict` clean over `src`, `tests` and
`validation`, 94% branch coverage over `blastoise.ci`. The four the brief named
are all present and named as claims rather than as function calls: each framework layout
with its near-misses beside it (`Views__old.sql` must not be Flyway;
`migrations/__init__.py` must not be Django), config override precedence in both
directions (`paths` replaces detection, `exclude` applies either way and beats `include`),
comment update rather than duplicate (including the 150-comment pagination case and the
"leave other people's comments alone" case), and secret redaction in error paths — the
exception message, the traceback, the chained cause, and an unexpected crash mid-run, each
asserting the password and host are absent while the traceback is still usable.

Beyond those: the exit-code matrix against `fail_on`, that a DSL file holds the run at
`requires_approval`, that an unparseable migration is an `error` and never a `block`, that
the unverified section never renders inside an open `<details>`, that an 80-file run fits
GitHub's limit and says what it dropped, that the check conclusion maps
success/neutral/failure, that a read-only fork token is reported and not fatal, and — the
one that is a security assertion rather than a behavioural one — that the `ci` subparser
has no `--database-url` flag at all.

Everything runs offline, including the git-discovery test, which builds a real two-commit
repository in a temporary directory. The one thing not covered here is the live path
through `_capture_snapshot`, which the existing introspection suite already exercises;
what these tests hold is the layer above the engine.

### What is owed

The three DSL adapters, in the order their cost suggests: Django first (`sqlmigrate`
already prints exactly what is wanted), then Alembic (`--sql` renders offline), then Rails
(no dry-run exists; one has to be built). Each brings the same open question — running the
team's application code inside the checker — which is a design decision, not an
implementation gap.

Second: the comment currently reports each file independently. A pull request that adds
three migrations to the same table is three separate assessments of a table that will have
changed shape twice by the time the third runs, and the report layer has no concept of
"the state after the previous file". `assess_script` already models this *within* a file
(`_FileState`); extending it across an ordered set of files is the natural next step and
is what would make a multi-migration pull request's verdict actually correct rather than
merely conservative.

Third: the check status is created, never updated. A re-push produces a second check run
with the same name, which GitHub displays as the latest — correct in the UI, but it means
the run history accumulates. Using `PATCH /check-runs/{id}` would need the run id carried
across pushes, which the comment marker solves for comments and nothing solves for checks;
recorded as known, not fixed.

## Rails migrations, rendered by running them (2026-08-28)

The previous section listed the three DSL adapters in the order their cost suggested and put
Rails last, on the grounds that "no dry-run exists; one has to be built". That was right, and
I checked it rather than inheriting it: `grep -ni "dry_run|pretend|sql_only|offline"` over
`activerecord/lib/active_record/migration.rb` and `railties/databases.rake` at v8.0.2 returns
nothing. There is no `--sql` mode, no pretend mode, and no `rails db:migrate --plan`. Alembic's
offline rendering has no Rails equivalent, and the reason is structural rather than an
oversight: `add_column` asks the adapter for a type name, `remove_index` looks an index up by
its columns, `change_column` reads the column's current type, and a `change_table` block does
not become statements until it is executed. The SQL does not exist until ActiveRecord renders
it against a live connection.

So the question was never "how do we avoid running it", it was "what do we record while it
runs". Three candidates, and the choice between them matters more than any of the code:

**Diffing the schema before and after** — the obvious one, and disqualifying. I ran a migration
that did `add_index ... algorithm: :concurrently`, `add_column ... default:`, a backfill
`UPDATE`, and `remove_column`, then compared what each method recovered. The schema diff found
the added column, the dropped column, and an index reported by `pg_indexes` as
`CREATE INDEX ... USING btree (email)`. Not `CONCURRENTLY` — the catalog does not record how an
index was built, because that is a property of the build, not of the index. And the backfill
`UPDATE` was invisible: it leaves no trace in a schema at all. Nor can a schema say whether the
whole thing ran inside a transaction. Those three facts are most of what this tool exists to
judge, so a schema diff would not merely be lossy, it would be lossy in the direction that
turns a dangerous migration into a clean verdict. In the 217 real migrations I sampled while
choosing, 23% use `disable_ddl_transaction!`, 20% use `algorithm: :concurrently`, and 21% call
`execute` — this is the common case, not the tail.

**Reconstructing SQL from ActiveRecord without a database** — the adapter's DDL generation is
not connection-free (see the list above), so this fails on a large fraction of real migrations
and, worse, fails silently differently depending on which methods were used.

**Recording the statements as they run**, which is what I built. ActiveRecord publishes every
statement it executes to the `sql.active_record` ActiveSupport notification; subscribing to it
and running the migration against a throwaway database yields the exact statements, in order,
byte for byte. I verified the mechanism in the Rails source rather than from memory:
`raw_execute` calls `log(...)` which instruments `sql.active_record` (v8.0.2,
`abstract/database_statements.rb`), and the payload's `:sql` and `:name` keys are unchanged from
6.1 through 8.0 — later versions *added* `async`, `transaction` and `row_count` and removed
nothing. `:name` is what makes the stream usable: ActiveRecord tags its own catalog
introspection `"SCHEMA"`, and `begin_db_transaction`/`commit_db_transaction` emit literal
`BEGIN`/`COMMIT` tagged `"TRANSACTION"` (6.1 via `execute`, 8.0 via `internal_execute`, both
through `log`). So transaction structure arrives as real SQL, and `disable_ddl_transaction!`
arrives as the *absence* of a `BEGIN` — which is exactly right, and unrecoverable any other way.

The empirical check that settled it: the same migration file, one unchanged harness, three
ActiveRecords — 6.1.7.10, 7.1.5.1 and 8.0.2 — produced byte-identical SQL. And the output goes
into `parse_migration` with no engine change at all, classifying as
`create_index_concurrently / alter_table / update / alter_table` with the transaction group
correctly implicit. That was the stated bar: if the extracted SQL had needed a special case in
the parser, the extraction would have been wrong.

### What it costs, and the three refusals

Every other part of Blastoise only ever *reads* the branch. This executes it. Rendering a Rails
migration means `load`-ing a Ruby file that arrived in a pull request and running it, which is
a different security posture from anything else in the tool, and pretending otherwise would be
the dishonest part. So:

`rails.extract` defaults to **off**. It is opt-in in `.blastoise.yml`, and a repository that has
not asked for it keeps the honest not-assessed message. Second, extraction **refuses under
`pull_request_target`** and no configuration turns that back on: that event runs with the base
repository's secrets and a writable token against a fork's code, so executing the fork's Ruby
there would hand over the repository. Third, it **refuses a scratch database on the same host
and port as the database being assessed**, because extraction creates and drops databases and
the one unrecoverable mistake is doing that on the server that matters. The scratch connection
string is named by config and valued by the environment, on the same rule as the assessed one —
`rails.scratch_url` in a committed file is refused with the reason.

It also has to run under the application's own bundle, which is not a preference. A migration
declaring `ActiveRecord::Migration[8.1]` raises `Unknown migration version "8.1"` on any older
ActiveRecord — I confirmed this against 8.0.2 — and the corpus spans compatibility versions 4.2
through 8.1. `safety_assured` is a method that exists only if the app's strong_migrations is
loaded; 10% of the sampled migrations call it. So the Rails that renders the SQL must be the
app's, and the harness opportunistically requires `strong_migrations`, `hairtrigger`, `fx` and
`scenic` — the gems that extend the migration and schema DSL — each guarded, because an app that
does not bundle one simply does not get it.

### The pre-state, and two fallbacks

`add_column :users, ...` needs a `users` table, so the migration has to run against the schema
as it was *before* the change. Not the branch's `db/schema.rb`: Rails regenerates that when a
developer runs the migration locally and they commit it, so loading the branch's schema and
then migrating would die on "column already exists" for every migration whose author had ever
run it. The pre-state is therefore `git show <base>:db/structure.sql` (preferred, because a
project that moved to it did so when its schema outgrew the Ruby dumper) or `db/schema.rb`.

Two fallbacks, both of which validation forced me to build rather than predict. First, a
committed schema file that is *absent* falls back to replaying the earlier migrations, capped —
past 200 the honest answer is that committing a schema file is what makes this assessable.
Second, and this one I did not see coming: a committed schema file that **will not load**.
`db/schema.rb` cannot express a function, a trigger, or a custom type. Mastodon's schema.rb
declares `id` columns with `default: -> { "timestamp_id('accounts'::text)" }` and never creates
`timestamp_id`, which lives in a migration that calls `Mastodon::Snowflake.define_timestamp_id`.
That schema does not load standalone — not for Blastoise and not for Mastodon either. So the
harness reports which *stage* it failed at, and a schema-stage failure retries via replay while
a migration-stage failure does not, because a migration that raised would raise again.

The third thing validation forced: a pull request that adds two migrations, where the second
indexes a column the first adds. Assessing each against the base commit's schema alone fails on
a column the branch creates — which is ordinary Rails, not an edge case. Migrations in one
change are now ordered by the timestamp in their file name and each is rendered with the earlier
ones already applied, bucketed by directory so two Rails apps in a monorepo do not precede each
other. This is a narrow instance of the "no concept of the state after the previous file" gap
the previous section recorded as owed; it is closed for the Rails pre-state, and still open for
the assessment engine.

### Validation

The claim is that the extracted SQL does what the migration does, and the only way to check it
is against real migrations from real applications. For each case: find the commit that added the
migration, take the schema from its parent, build that pre-state **twice**, run the real
migration on database A through the shipped harness, apply the SQL that harness extracted to
database B statement by statement, and compare A and B column by column, index by index,
constraint by constraint, sequence by sequence. Statements go into B one at a time rather than
as a script, because `CREATE INDEX CONCURRENTLY` cannot run inside the implicit transaction a
multi-statement simple query gets — which is itself a small proof that the concurrency survived.

40 migrations from four applications — discourse, mastodon, forem and openfoodnetwork —
each rendered by **its own** ActiveRecord resolved from its `Gemfile.lock`: 7.2.3.2, 8.0.5,
8.0.5.1 and 8.1.3.1. The result:

| | cases | verified faithful | wrong SQL | could not render |
|---|---|---|---|---|
| discourse (AR 8.0.5.1, `structure.sql`) | 10 | 10 | 0 | 0 |
| forem (AR 8.0.5, `schema.rb`) | 10 | 9 | 0 | 1 |
| openfoodnetwork (AR 7.2.3.2, `schema.rb`) | 10 | 8 | 0 | 2 |
| mastodon (AR 8.1.3.1, `schema.rb`) | 10 | 0 | 0 | 10 |
| **total** | **40** | **27** | **0** | **13** |

The column that matters is the third one. Extraction never produced SQL that differed from what
the migration did — not once in 40 real migrations. Every failure was a refusal, and every
refusal is the honest not-assessed message with its reason rather than a verdict.

What survived extraction, counted across the verified cases: `CREATE INDEX CONCURRENTLY` in 7,
explicit `BEGIN`/`COMMIT` in 20, backfill `UPDATE` in 3, `DROP INDEX CONCURRENTLY` in 1,
`ADD CONSTRAINT ... NOT VALID` in 2, `VALIDATE CONSTRAINT` in 1, `DROP COLUMN` in 3. That list
is the argument against schema diffing restated as evidence: every one of those is a thing a
before/after schema comparison either cannot see or actively misreports.

The 13 that did not render split into two groups, and the split is the finding. Eleven are my
validation machine rather than the approach: it is Windows with no Ruby devkit, so `scenic`,
`hairtrigger` and `neighbor` will not install and pgvector has no Windows build at all. Mastodon
loads `create_view` (scenic) in its schema, forem has a migration using `t.vector`. In a real
CI those gems are in the app's bundle and the harness requires them — I could not prove that
here and am not going to claim it. The other two are real: openfoodnetwork carries Active
Storage's migrations, and one calls `Rails.configuration.generators` to pick a primary key type.
That needs the framework booted, not merely ActiveRecord, and it is a genuine gap.

Mastodon deserves its own note because its failure moved. Before I supplied anything it failed
on `timestamp_id(text) does not exist` — the schema.rb expressiveness gap described above, and
the reason the schema-stage fallback exists. I then preloaded that function, lifted verbatim
from Mastodon's own `lib/mastodon/snowflake.rb`, purely to isolate the variable, and the failure
moved to `create_view`. So Mastodon's schema.rb has *two* things it cannot declare, and in
production the first one sends it to the replay fallback, where a ~1000-migration history
exceeds the cap and it is honestly refused. That is the right outcome; it is not a good one.

The corpus earned its keep by finding three defects that no synthetic test would have. The
first two are in the harness and are fixed:

**Engine-installed migrations were given the wrong class name.** `rails
railties:install:migrations` copies an engine's migrations in with a scope suffix —
OpenFoodNetwork carries Active Storage's as
`20260512062933_create_active_storage_variant_records.active_storage.rb`. Deriving the class by
splitting on the first underscore and camelizing the remainder produces
`CreateActiveStorageVariantRecords.activeStorage`, which is not a constant, and three cases
failed on it. The fix is to use Rails' own filename grammar,
`/\A([0-9]+)_([_a-z0-9]*)\.?([_a-z0-9]*)?\.rb\z/` — version, name, *scope* — and take the name
only. There is now a regression test with a scoped filename.

**A gem's probe queries were being reported as the migration's statements.** strong_migrations
reads `SHOW server_version_num` and `SHOW lock_timeout` before deciding whether to object, and
those arrive in the notification stream tagged as ordinary statements rather than as schema
queries, so they landed in the extracted SQL and would have appeared in the verdict table as
statements the migration runs. `SHOW` is now dropped: it takes no lock, touches no row and
changes nothing, so it can never be the hazard a report is about. `SET` is deliberately *not*
dropped, because `SET lock_timeout` in a migration is exactly the kind of thing worth reporting.

The third is the pull-request ordering described above, which showed up as three separate
"column does not exist" failures before I understood they were all the same thing.

A note on what the comment shows. The per-statement rows cite `L1`, `L2`, `L3`, and for a Rails
file those are lines of the *rendered* SQL, not of the `.rb`. That is not a leak: the evidence
bundle's `migration.sql` is the exact SQL that was assessed, so the citations resolve against a
file the reviewer can read, and the thing being judged is on record rather than being described.

### What is owed, plainly

**Migrations that reference application code do not render.** Mastodon's call to
`Mastodon::Snowflake` is the clean example: the harness runs under the app's bundle, which puts
its *gems* on the load path, but does not boot the app, so `lib/` is not autoloaded and model
constants do not resolve. Booting `config/environment.rb` would fix it and is the obvious next
step, but it runs the app's initializers — connecting to whatever they connect to — and that is
a decision about blast radius, not an implementation gap. Roughly 1% of the sampled corpus
references model constants; Mastodon's case is rarer still and more severe, because it breaks
the *pre-state* rather than the migration.

**Applications with no committed schema file are not assessable** unless their history is short
enough to replay. OpenProject commits neither `db/schema.rb` nor `db/structure.sql`, so every one
of its migrations falls back and then hits the replay cap. That is reported honestly rather than
worked around.

**Django and Alembic are unchanged.** They keep the old message, and `EXTRACTABLE` is the single
place that decides which frameworks claim an adapter, so the comment for a Rails file now says
"Blastoise can render this by running it, but did not here" plus the reason, while Django's still
says support does not exist. Conflating those two would tell a team to keep waiting for something
they already have.

**The failure mode is a missing verdict, never a wrong one.** Every path out of extraction that
is not clean SQL — the harness raising, the schema not loading, the replay cap, the parser
refusing the output — produces `unsupported` with a reason and holds the run at
`requires_approval`. Nothing reconstructs, approximates, or guesses at SQL, because a verdict
about statements the migration never runs is worse than no verdict.

### The Linux re-run, and a wrong attribution corrected (2026-08-29)

I called eleven of the thirteen Windows failures "the machine, not the approach". That was
wrong, and the way it was wrong is worth recording: I asserted a cause I had not tested. The
Windows box could not build `scenic`, `hairtrigger` or `neighbor` and has no pgvector, so all
ten Mastodon cases stopped at the first thing that needed one — and I read "this machine cannot
install the gem" as "with the gem, it would work". Re-running on a Linux GitHub Actions runner
where every one of them installs (`artifacts/scripts/rails_extraction_validation_workflow.yml`,
kept out of `.github/workflows/` so it never runs on a push — it clones four applications; Postgres
via `pgvector/pgvector:pg17` with `pg_stat_statements` preloaded) settled it:

| run | machine | verified | wrong SQL | failed |
|---|---|---|---|---|
| 2026-08-28 | Windows, no devkit, no pgvector | 27/40 | 0 | 13 |
| 2026-08-29 | Linux, all gems installed | 28/40 | 0 | 12 |
| 2026-08-29 | Linux, after the two fixes below | **35/40** | **0** | 5 |

The middle row is the finding. With scenic installed *and verified to load*, Mastodon still
failed on `create_view` — so those ten were never an environment gap. They were two real bugs
in the harness:

**Requiring a gem is not installing it.** `scenic` adds `create_view` to the adapter from
`Scenic.load`, which its Railtie calls during application boot. This harness deliberately does
not boot the application, so the Railtie never runs and the DSL is never installed, however
correctly the gem loads. The gem list is now a map from gem name to the constant whose `.load`
installs it, and that installer is called after the require. `fx` has the same shape;
`hairtrigger`, `neighbor` and `strong_migrations` install themselves on require and are mapped
to nil.

**Gems assume standard library that a booting Rails already required.** `scenic` references
`TSort` without requiring `tsort`, exactly as activesupport <= 6.1 references `Logger` without
requiring `logger`. Both raise `NameError` on load outside a booted application. The harness now
requires `logger`, `tsort`, `set`, `singleton` and `benchmark` before ActiveRecord, guarded.

Neither is a Rails quirk I could have reasoned my way to; both needed a machine that could
install the gems. That is the argument for having run this on Linux rather than reporting the
Windows numbers with a caveat attached.

Of the eleven I mis-attributed: one really was the environment (forem's `t.vector` column needs
pgvector and `neighbor`, and passes on Linux), seven were the two bugs above, and three turned
out to be the app-environment limitation that was already recorded — masked on Windows because
they never got past `create_view` to reach it.

**All five remaining failures are one thing, and it is the limitation already named:** the
migration, or a gem acting for it, needs the booted application rather than only ActiveRecord.
Mastodon has two migrations referencing `ApplicationRecord` and one calling
`Scenic::Definition#to_sql`, which reads a view file relative to `Rails.root` and gets `nil`;
OpenFoodNetwork has two Active Storage migrations calling `Rails.configuration.generators` to
choose a primary key type. Nothing in that set is an extraction defect: every one fails loudly
before producing SQL, and every one falls back to the honest not-assessed message. Extraction
still produced wrong SQL zero times in forty.

Constructs recovered across the 35 verified cases, which is the schema-diff argument restated
against a larger sample: `CREATE INDEX CONCURRENTLY` in 10, explicit `BEGIN`/`COMMIT` in 24,
`DROP INDEX CONCURRENTLY` in 2, backfill `UPDATE` in 3, `ADD CONSTRAINT ... NOT VALID` in 2,
`VALIDATE CONSTRAINT` in 1, `DROP COLUMN` in 3, `CREATE TABLE` in 7.

Booting the application would close the last five, and remains the decision it was before: it
runs the app's initializers, against whatever they connect to. Not doing it is why the harness
is a subprocess with a scratch database and no secrets, and the five files it cannot render say
so rather than being guessed at.
