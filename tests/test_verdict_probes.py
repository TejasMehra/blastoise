"""Probe derivation and remaining engine/narrowing/duration branches."""

from __future__ import annotations

import dataclasses

from verdict_helpers import column, relation, snapshot, type_change

from blastoise.catalog.loader import load_catalog
from blastoise.catalog.model import LockMode
from blastoise.live.model import Fact, LiveSnapshot
from blastoise.parser import parse_migration
from blastoise.verdict import (
    CannotEstimate,
    Classification,
    DurationEstimate,
    Method,
    StatementAssessment,
    assess_script,
    snapshot_probes,
)
from blastoise.verdict.duration import estimate_from_rows

CATALOG = load_catalog()


def one(sql: str, snap: LiveSnapshot | None = None, pg: int = 17) -> StatementAssessment:
    result = assess_script(parse_migration(sql), CATALOG, pg, snap)
    assert len(result.statements) == 1
    return result.statements[0]


class TestSnapshotProbes:
    def test_collects_every_probe_kind(self) -> None:
        script = parse_migration(
            """
            ALTER TABLE orders ADD COLUMN status order_status DEFAULT my_default();
            ALTER TABLE orders ADD CONSTRAINT fk FOREIGN KEY (uid) REFERENCES users (id);
            ALTER TABLE orders ALTER COLUMN total TYPE numeric(12,2);
            ALTER TABLE events ATTACH PARTITION events_2026 FOR VALUES FROM (1) TO (9);
            DO $$
            BEGIN
              UPDATE audit_log SET seen = true;
            END $$;
            """
        )
        probes = snapshot_probes(script)
        assert "orders" in probes.relations
        assert "users" in probes.relations  # FK referenced table
        assert "events_2026" in probes.relations  # partition
        assert "audit_log" in probes.relations  # DO-inner statement
        assert "my_default" in probes.functions
        assert "order_status" in probes.types
        assert ("orders", "total", "numeric(12, 2)") in probes.type_changes

    def test_partition_of_parent_is_probed(self) -> None:
        probes = snapshot_probes(
            parse_migration(
                "CREATE TABLE events_2027 PARTITION OF events FOR VALUES FROM (1) TO (9);"
            )
        )
        assert "events" in probes.relations


class TestRemainingEngineBranches:
    def test_select_is_safe_read_only(self) -> None:
        statement = one("SELECT count(*) FROM users;")
        assert statement.verdict.classification is Classification.SAFE
        assert "read-only" in statement.verdict.rationale

    def test_reindex_index_uses_bytes_model(self) -> None:
        snap = snapshot(relations=(relation("users_pkey", rows=None, size_bytes=30_000_000),))
        statement = one("REINDEX INDEX users_pkey;", snap)
        row = statement.rows[0]
        assert isinstance(row.duration, DurationEstimate)
        assert row.duration.constant_key == "index_bytes"
        # 30 MB at the measured 9.5 MB/s: ~3s point, ~6s upper — a
        # conditional stall between the read-block thresholds.
        assert statement.verdict.classification is Classification.NEEDS_TIMING

    def test_reindex_index_offline_is_unknown(self) -> None:
        statement = one("REINDEX INDEX users_pkey;")
        assert statement.verdict.classification is Classification.UNKNOWN

    def test_unavailable_form_fails_proven(self) -> None:
        statement = one(
            "ALTER TABLE events DETACH PARTITION events_2020 CONCURRENTLY;", pg=13
        )
        assert statement.verdict.classification is Classification.UNSAFE
        assert statement.verdict.method is Method.PROVEN
        assert "does not exist on PG 13" in statement.verdict.rationale

    def test_cic_outside_txn_is_safe_with_estimate(self) -> None:
        snap = snapshot(relations=(relation("users", rows=40_000_000),))
        statement = one("CREATE INDEX CONCURRENTLY i ON users (email);", snap)
        assert statement.verdict.classification is Classification.SAFE
        assert "no lock that blocks" in statement.verdict.rationale
        assert isinstance(statement.rows[0].duration, DurationEstimate)

    def test_invalid_index_with_same_name_is_noted(self) -> None:
        snap = snapshot(
            relations=(relation("users", rows=200, invalid_indexes=("idx_email",)),)
        )
        statement = one("CREATE INDEX idx_email ON users (email);", snap)
        assert any("INVALID index named 'idx_email'" in n for n in statement.rows[0].notes)

    def test_other_invalid_indexes_are_noted_generally(self) -> None:
        snap = snapshot(
            relations=(relation("users", rows=200, invalid_indexes=("idx_old",)),)
        )
        statement = one("CREATE INDEX idx_email ON users (email);", snap)
        assert any("INVALID index(es) idx_old" in n for n in statement.rows[0].notes)

    def test_dynamic_sql_do_block_is_unknown(self) -> None:
        statement = one(
            "DO $$ DECLARE t text := 'x'; BEGIN EXECUTE 'DROP TABLE ' || t; END $$;"
        )
        assert statement.verdict.classification is Classification.UNKNOWN
        assert any("runtime" in n for n in statement.notes)

    def test_delete_without_where_big_is_unsafe(self) -> None:
        snap = snapshot(relations=(relation("users", rows=40_000_000),))
        statement = one("DELETE FROM users;", snap)
        assert statement.verdict.classification is Classification.UNSAFE
        assert "deletes every row" in statement.verdict.rationale

    def test_matched_update_missing_relation_is_unknown(self) -> None:
        snap = snapshot(relations=(relation("other_table", rows=10),))
        statement = one("UPDATE users SET x = 1 WHERE id = 2;", snap)
        assert statement.verdict.classification is Classification.UNKNOWN

    def test_create_extension_online_still_unknown(self) -> None:
        snap = snapshot()
        statement = one('CREATE EXTENSION IF NOT EXISTS "uuid-ossp";', snap)
        assert statement.verdict.classification is Classification.UNKNOWN
        assert "install script" in statement.verdict.rationale

    def test_waiter_on_other_relation_does_not_escalate(self) -> None:
        from verdict_helpers import waiter

        snap = snapshot(
            relations=(relation("users", rows=200),),
            waiters=(waiter("public.other"),),
        )
        statement = one("CREATE INDEX i ON users (email);", snap)
        assert statement.verdict.classification is Classification.SAFE

    def test_lock_table_access_exclusive_needs_timing(self) -> None:
        statement = one("LOCK TABLE users IN ACCESS EXCLUSIVE MODE;")
        assert statement.verdict.classification is Classification.NEEDS_TIMING


class TestNarrowingBranches:
    def test_set_expression_stored_rewrites(self) -> None:
        snap = snapshot(
            relations=(
                relation(
                    "users",
                    rows=40_000_000,
                    columns_facts=(column("total", generated="s"),),
                ),
            )
        )
        statement = one(
            "ALTER TABLE users ALTER COLUMN total SET EXPRESSION AS (a + b);", snap
        )
        assert statement.verdict.classification is Classification.UNSAFE

    def test_set_expression_virtual_is_constant(self) -> None:
        snap = snapshot(
            relations=(
                relation(
                    "users",
                    rows=40_000_000,
                    columns_facts=(column("total", generated="v"),),
                ),
            )
        )
        statement = one(
            "ALTER TABLE users ALTER COLUMN total SET EXPRESSION AS (a + b);", snap
        )
        assert statement.verdict.classification is Classification.NEEDS_TIMING

    def test_set_expression_unknown_generation_stays_unknown(self) -> None:
        snap = snapshot(relations=(relation("users", rows=100),))
        statement = one(
            "ALTER TABLE users ALTER COLUMN total SET EXPRESSION AS (a + b);", snap
        )
        assert statement.verdict.classification is Classification.UNKNOWN

    def test_add_pk_using_index_all_not_null_is_constant(self) -> None:
        snap = snapshot(
            relations=(
                relation(
                    "users",
                    rows=40_000_000,
                    columns_facts=(column("id", not_null=True),),
                ),
            )
        )
        statement = one(
            "ALTER TABLE users ADD CONSTRAINT pk PRIMARY KEY USING INDEX idx_users_id;",
            snap,
        )
        assert statement.verdict.classification is Classification.NEEDS_TIMING
        assert any("already NOT NULL" in n for n in statement.rows[0].narrowings)

    def test_add_pk_using_index_nullable_keeps_model_with_note(self) -> None:
        snap = snapshot(
            relations=(
                relation(
                    "users",
                    rows=40_000_000,
                    columns_facts=(column("email", not_null=False),),
                ),
            )
        )
        statement = one(
            "ALTER TABLE users ADD CONSTRAINT pk PRIMARY KEY USING INDEX idx_users_id;",
            snap,
        )
        assert statement.verdict.classification is Classification.NEEDS_TIMING
        assert any("could not be ruled out" in n for n in statement.rows[0].notes)

    def test_attach_partition_is_unknown_uncalibrated(self) -> None:
        # The catalog's attach_partition row is an UNCALIBRATED stub (no wild
        # occurrence): the engine refuses to assess it, snapshot or not.
        snap = snapshot(
            relations=(relation("events", rows=1000), relation("events_2026", rows=50)),
        )
        statement = one(
            "ALTER TABLE events ATTACH PARTITION events_2026 FOR VALUES FROM (1) TO (9);",
            snap,
        )
        assert statement.verdict.classification is Classification.UNKNOWN
        assert "uncalibrated" in statement.verdict.rationale

    def test_alter_type_undecidable_facts_stay_unknown(self) -> None:
        facts = dataclasses.replace(
            type_change("users", "id", "bigint"),
            cast_method=Fact.unavailable("pg_cast probe timed out"),
        )
        snap = snapshot(relations=(relation("users", rows=100),), type_changes=(facts,))
        statement = one("ALTER TABLE users ALTER COLUMN id TYPE bigint;", snap)
        assert statement.verdict.classification is Classification.UNKNOWN

    def test_validate_missing_constraint_stays_unknown(self) -> None:
        snap = snapshot(relations=(relation("users", rows=100, constraints=()),))
        statement = one("ALTER TABLE users VALIDATE CONSTRAINT nope;", snap)
        assert statement.verdict.classification is Classification.UNKNOWN
        assert "would error" in statement.verdict.rationale

    def test_drop_missing_constraint_if_exists_narrows_constant(self) -> None:
        snap = snapshot(relations=(relation("users", rows=100, constraints=()),))
        statement = one(
            "ALTER TABLE users DROP CONSTRAINT IF EXISTS nope;", snap
        )
        assert statement.verdict.classification is Classification.NEEDS_TIMING
        assert any("IF EXISTS" in n for n in statement.rows[0].narrowings)

    def test_drop_check_constraint_locks_only_this_table(self) -> None:
        from verdict_helpers import check_constraint

        snap = snapshot(
            relations=(
                relation(
                    "users",
                    rows=100,
                    constraints=(check_constraint("chk", "(x > 0)"),),
                ),
            )
        )
        statement = one("ALTER TABLE users DROP CONSTRAINT chk;", snap)
        row = statement.rows[0]
        assert all(r.lock_mode is not LockMode.ROW_SHARE for r in row.relations)
        assert any("only this table" in n for n in row.narrowings)


class TestDurationBranches:
    SERVER = snapshot().server

    def test_age_bands(self) -> None:
        week = estimate_from_rows(
            relation("t", rows=1_000_000, analyzed_hours_ago=3 * 24),
            self.SERVER,
            "heap_rewrite",
        )
        month = estimate_from_rows(
            relation("t", rows=1_000_000, analyzed_hours_ago=20 * 24),
            self.SERVER,
            "heap_rewrite",
        )
        assert isinstance(week, DurationEstimate) and isinstance(month, DurationEstimate)
        assert month.high_ms > week.high_ms

    def test_mods_unavailable_widens_with_note(self) -> None:
        rel = dataclasses.replace(
            relation("t", rows=1_000_000),
            n_mod_since_analyze=Fact.unavailable("stats masked"),
        )
        estimate = estimate_from_rows(rel, self.SERVER, "heap_rewrite")
        assert isinstance(estimate, DurationEstimate)
        assert any("unavailable" in text for text in estimate.inputs)

    def test_exists_unavailable_refuses(self) -> None:
        rel = dataclasses.replace(
            relation("t", rows=100), exists=Fact.unavailable("name unparsable")
        )
        result = estimate_from_rows(rel, self.SERVER, "heap_rewrite")
        assert isinstance(result, CannotEstimate)
        assert "existence" in result.reason

    def test_server_clock_unavailable_widens(self) -> None:
        server = dataclasses.replace(
            self.SERVER, server_now=Fact.unavailable("clock query failed")
        )
        fresh_clock = estimate_from_rows(
            relation("t", rows=1_000_000), self.SERVER, "heap_rewrite"
        )
        no_clock = estimate_from_rows(
            relation("t", rows=1_000_000), server, "heap_rewrite"
        )
        assert isinstance(fresh_clock, DurationEstimate)
        assert isinstance(no_clock, DurationEstimate)
        assert no_clock.high_ms > fresh_clock.high_ms


class TestFinalBranches:
    def test_empty_do_block_is_unknown(self) -> None:
        statement = one("DO $$ BEGIN NULL; END $$;")
        assert statement.verdict.classification is Classification.UNKNOWN
        assert "no analyzable inner statements" in statement.verdict.rationale

    def test_contended_brief_ael_escalates_to_unsafe(self) -> None:
        from verdict_helpers import waiter

        snap = snapshot(
            relations=(relation("users", rows=200),),
            waiters=(waiter("public.users"),),
        )
        statement = one("ALTER TABLE users ALTER COLUMN email SET DEFAULT 'x';", snap)
        assert statement.verdict.classification is Classification.UNSAFE
        assert "pg_locks" in statement.verdict.rationale

    def test_reindex_index_size_unavailable_is_unknown(self) -> None:
        rel = dataclasses.replace(
            relation("users_pkey", rows=None),
            relation_size_bytes=Fact.unavailable("lock not acquired"),
        )
        statement = one("REINDEX INDEX users_pkey;", snapshot(relations=(rel,)))
        assert statement.verdict.classification is Classification.UNKNOWN

    def test_reindex_index_not_captured_is_unknown(self) -> None:
        statement = one("REINDEX INDEX users_pkey;", snapshot())
        assert statement.verdict.classification is Classification.UNKNOWN

    def test_validate_not_valid_check_scans_this_table_only(self) -> None:
        from verdict_helpers import check_constraint

        snap = snapshot(
            relations=(
                relation(
                    "users",
                    rows=200,
                    constraints=(
                        check_constraint("chk", "(x > 0)", validated=False),
                    ),
                ),
            )
        )
        statement = one("ALTER TABLE users VALIDATE CONSTRAINT chk;", snap)
        assert statement.verdict.classification is Classification.SAFE
        assert any("only this table" in n for n in statement.rows[0].narrowings)


class TestProbeNameQuoting:
    def test_camel_case_names_are_quoted_for_the_server(self) -> None:
        probes = snapshot_probes(parse_migration('CREATE INDEX i ON "User" (email);'))
        assert '"User"' in probes.relations

    def test_engine_looks_up_by_the_quoted_spelling(self) -> None:
        snap = snapshot(relations=(relation('"User"', rows=200),))
        statement = one('CREATE INDEX i ON "User" (email);', snap)
        assert statement.verdict.classification is Classification.SAFE

    def test_lowercase_names_stay_bare(self) -> None:
        probes = snapshot_probes(parse_migration("CREATE INDEX i ON users (email);"))
        assert "users" in probes.relations
