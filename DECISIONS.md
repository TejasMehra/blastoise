# Decisions

I built the parsing layer of pgverdict: `pglast` 8.4 (libpg_query, PG18 grammar) parses each
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
`create_index` 1,875 vs `create_index_concurrently` 121 (6%). The safe patterns pgverdict
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

Built the lock semantics catalog as YAML data (`src/pgverdict/catalog/lock_catalog.yaml`,
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

Built `pgverdict.live`: read-only production introspection that supplies what the catalog's
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
back to `PGVERDICT_TEST_DSN`, then to `PGVERDICT_TEST_PG_BIN` local binaries (this machine has no
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

Built the verdict layer: `pgverdict.verdict` — `assess_script(script, catalog, pg_version,
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
is `pgverdict.verdict.constants.DURATION_CONSTANTS`; every entry is `UNCALIBRATED` and every
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

Caveats recorded: the replay's "online" distribution answers "what would pgverdict say against
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
