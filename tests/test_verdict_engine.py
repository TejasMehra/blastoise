"""Engine tests: the three mandated invariants plus classification behavior.

Mandated by the layer's brief:
1. removing the live snapshot moves statements to UNKNOWN, never to UNSAFE;
2. the same statement classifies differently at different table sizes;
3. no code path can produce a classification without a method tag.
"""

from __future__ import annotations

import dataclasses

import pytest
from verdict_helpers import relation, snapshot, waiter

from blastoise.catalog.loader import load_catalog
from blastoise.live.model import LiveSnapshot
from blastoise.parser import parse_migration
from blastoise.verdict import (
    SAFE_TIERS,
    CannotEstimate,
    Classification,
    DurationBand,
    DurationEstimate,
    Method,
    Reversibility,
    ScriptAssessment,
    StatementAssessment,
    Verdict,
    assess_script,
)

CATALOG = load_catalog()


def assess(sql: str, snap: LiveSnapshot | None = None, pg: int = 17) -> ScriptAssessment:
    return assess_script(parse_migration(sql), CATALOG, pg, snap)


def one(sql: str, snap: LiveSnapshot | None = None, pg: int = 17) -> StatementAssessment:
    result = assess(sql, snap, pg)
    assert len(result.statements) == 1
    return result.statements[0]


BIG = snapshot(relations=(relation("users", rows=40_000_000),))
SMALL = snapshot(relations=(relation("users", rows=200),))

SIZE_DEPENDENT = (
    "CREATE INDEX idx ON users (email);",
    "ALTER TABLE users ALTER COLUMN id TYPE bigint;",
    "ALTER TABLE users ALTER COLUMN email SET NOT NULL;",
    "UPDATE users SET email = lower(email);",
    "DELETE FROM users;",
    "ALTER TABLE users ADD COLUMN flag boolean DEFAULT false;",
)


class TestSnapshotRemoval:
    @pytest.mark.parametrize("sql", SIZE_DEPENDENT)
    def test_offline_is_unknown_never_unsafe(self, sql: str) -> None:
        offline = one(sql)
        assert offline.verdict.classification is Classification.UNKNOWN
        assert offline.verdict.method is Method.UNVERIFIED

    def test_online_unsafe_becomes_unknown_offline(self) -> None:
        sql = "CREATE INDEX idx ON users (email);"
        online = one(sql, BIG)
        assert online.verdict.classification is Classification.UNSAFE
        offline = one(sql)
        assert offline.verdict.classification is Classification.UNKNOWN

    def test_offline_unsafe_only_for_proven_failures(self) -> None:
        # The only offline UNSAFE is a statement proven to fail (transaction
        # violation) — never a size-driven worst case.
        result = assess(
            "BEGIN;\nCREATE INDEX CONCURRENTLY idx ON users (email);\nCOMMIT;"
        )
        cic = result.statements[1]
        assert cic.verdict.classification is Classification.UNSAFE
        assert cic.verdict.method is Method.PROVEN
        assert "transaction" in cic.verdict.rationale


class TestSizeDrivesSeverity:
    def test_create_index_three_outcomes(self) -> None:
        sql = "CREATE INDEX idx ON users (email);"
        assert one(sql, SMALL).verdict.classification is Classification.SAFE
        assert one(sql, BIG).verdict.classification is Classification.UNSAFE
        assert one(sql).verdict.classification is Classification.UNKNOWN

    def test_mid_size_needs_timing(self) -> None:
        # 10M rows: a plain btree build's worst plausible hold (~20s of
        # blocked writes at the measured rate) sits between the 5s and 60s
        # write-block thresholds — safe in itself, disruptive at the wrong
        # moment, which is exactly NEEDS_TIMING.
        mid = snapshot(relations=(relation("users", rows=10_000_000),))
        verdict = one("CREATE INDEX idx ON users (email);", mid).verdict
        assert verdict.classification is Classification.NEEDS_TIMING
        assert verdict.conditions

    def test_expression_index_is_stricter_than_plain_at_the_same_size(self) -> None:
        # The same table size classifies differently by index shape: the
        # measured expression/GIN rate is 4x slower than plain btree.
        mid = snapshot(relations=(relation("users", rows=10_000_000),))
        plain = one("CREATE INDEX idx ON users (email);", mid).verdict
        expr = one("CREATE INDEX idx ON users (lower(email));", mid).verdict
        gin = one("CREATE INDEX idx ON users USING gin (payload);", mid).verdict
        assert plain.classification is Classification.NEEDS_TIMING
        assert expr.classification is Classification.UNSAFE
        assert gin.classification is Classification.UNSAFE

    def test_rewrite_three_outcomes(self) -> None:
        # A rewrite takes ACCESS EXCLUSIVE, so the small-table outcome is
        # floored at NEEDS_TIMING however brief the rewrite is — the lock,
        # not the duration, sets that floor. Size still drives everything
        # above it: the same statement reaches UNSAFE on a big table and
        # UNKNOWN with no size fact at all.
        sql = "ALTER TABLE users ADD COLUMN uid uuid DEFAULT gen_random_uuid();"
        small = one(sql, SMALL).verdict
        assert small.classification is Classification.NEEDS_TIMING
        assert small.band is DurationBand.SUB_SECOND
        assert one(sql, BIG).verdict.classification is Classification.UNSAFE
        assert one(sql).verdict.classification is Classification.UNKNOWN

    def test_update_without_where_three_outcomes(self) -> None:
        sql = "UPDATE users SET email = lower(email);"
        small = one(sql, SMALL).verdict
        # SAFE band; irreversibility selects which safe tier it lands in,
        # it does not push the statement out of the safe tiers.
        assert small.classification is Classification.SAFE_IRREVERSIBLE
        assert any("irreversible" in c for c in small.conditions)
        assert one(sql, BIG).verdict.classification is Classification.UNSAFE
        assert one(sql).verdict.classification is Classification.UNKNOWN


class TestMethodTags:
    def test_verdict_requires_method(self) -> None:
        with pytest.raises(TypeError):
            Verdict(classification=Classification.SAFE, rationale="x")  # type: ignore[call-arg]

    def test_every_conclusion_carries_a_method(self) -> None:
        result = assess(
            """
            CREATE TABLE t (id int);
            CREATE INDEX idx ON users (email);
            ALTER TABLE users ALTER COLUMN email SET NOT NULL;
            UPDATE users SET email = 'x' WHERE id = 1;
            DROP TABLE old_stuff;
            """,
            BIG,
        )
        tagged = _collect_method_carriers(result)
        assert tagged, "walk found no conclusions"
        for value in tagged:
            method = getattr(value, "method", None)
            assert isinstance(method, Method), f"{type(value).__name__} lacks a method tag"

    def test_every_statement_has_duration_or_refusal(self) -> None:
        result = assess(
            "CREATE INDEX i ON users(email);\nALTER TABLE users ADD COLUMN c int;",
            SMALL,
        )
        for statement in result.statements:
            for row in statement.rows:
                assert isinstance(row.duration, DurationEstimate | CannotEstimate)
                if isinstance(row.duration, CannotEstimate):
                    assert row.duration.reason


_METHOD_TYPES = (
    "Verdict",
    "DurationEstimate",
    "CannotEstimate",
    "Tristate",
    "ReversibilityAssessment",
    "RelationLockAssessment",
)


def _collect_method_carriers(value: object, out: list[object] | None = None) -> list[object]:
    if out is None:
        out = []
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        if type(value).__name__ in _METHOD_TYPES:
            out.append(value)
        for field in dataclasses.fields(value):
            _collect_method_carriers(getattr(value, field.name), out)
    elif isinstance(value, tuple | list):
        for item in value:
            _collect_method_carriers(item, out)
    return out


class TestConstantOps:
    def test_no_lock_statement_is_safe(self) -> None:
        statement = one("CREATE FUNCTION f() RETURNS int AS 'SELECT 1' LANGUAGE sql;")
        assert statement.verdict.classification is Classification.SAFE

    def test_brief_access_exclusive_needs_timing(self) -> None:
        verdict = one("ALTER TABLE users ALTER COLUMN email SET DEFAULT 'x';").verdict
        assert verdict.classification is Classification.NEEDS_TIMING
        assert any("lock_timeout" in c for c in verdict.conditions)
        assert verdict.method is Method.PROVEN

    def test_brief_write_block_is_safe(self) -> None:
        verdict = one(
            "CREATE TRIGGER trg BEFORE INSERT ON users FOR EACH ROW EXECUTE FUNCTION f();"
        ).verdict
        assert verdict.classification is Classification.SAFE

    def test_uncalibrated_row_is_unknown(self) -> None:
        verdict = one("VACUUM users;").verdict
        assert verdict.classification is Classification.UNKNOWN
        assert "uncalibrated" in verdict.rationale


class TestFileLocalRelations:
    def test_index_on_created_table_is_safe_offline(self) -> None:
        result = assess("CREATE TABLE t (id int);\nCREATE INDEX i ON t (id);")
        assert result.statements[1].verdict.classification is Classification.SAFE
        assert result.statements[1].verdict.method is Method.PROVEN

    def test_bulk_loaded_table_still_safe_but_unestimated(self) -> None:
        result = assess(
            "CREATE TABLE t AS SELECT * FROM big_source;\nCREATE INDEX i ON t (id);"
        )
        index = result.statements[1]
        assert index.verdict.classification is Classification.SAFE
        assert isinstance(index.rows[0].duration, CannotEstimate)

    def test_rename_tracks_created_state(self) -> None:
        result = assess(
            "CREATE TABLE t (id int);\nALTER TABLE t RENAME TO s;\nCREATE INDEX i ON s (id);"
        )
        assert result.statements[2].verdict.classification is Classification.SAFE


class TestBaselineFiles:
    def _baseline_sql(self) -> str:
        parts = [f"CREATE TABLE t{i} (id int, v text);" for i in range(30)]
        parts += [f"CREATE INDEX i{i} ON t{i} (id);" for i in range(30)]
        parts.append("ALTER TABLE t0 ADD COLUMN extra text DEFAULT 'x';")
        return "\n".join(parts)

    def test_baseline_file_generates_no_size_warnings(self) -> None:
        script = parse_migration(self._baseline_sql())
        assert script.baseline_shaped
        result = assess_script(script, CATALOG, 17)
        counts = result.classification_counts()
        assert counts[Classification.UNKNOWN] == 0
        assert counts[Classification.UNSAFE] == 0
        assert any("baseline" in note for note in result.notes)

    def test_same_statements_unflagged_are_not_all_safe(self) -> None:
        sql = "CREATE INDEX i0 ON existing_table (id);"
        assert one(sql).verdict.classification is Classification.UNKNOWN


class TestGuardedDoBlocks:
    GUARDED = """
    DO $$
    BEGIN
      IF EXISTS (
        SELECT 1 FROM information_schema.tables WHERE table_name = 'users'
      ) THEN
        CREATE TABLE archive_users (id bigint);
      END IF;
    END $$;
    """

    def test_guarded_safe_statement_is_capped_at_needs_timing(self) -> None:
        statement = one(self.GUARDED)
        assert statement.verdict.classification is Classification.NEEDS_TIMING
        assert any("existence check" in c for c in statement.verdict.conditions)

    def test_guarded_unsafe_statement_is_capped_with_record(self) -> None:
        guarded_index = """
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM information_schema.tables WHERE table_name = 'users'
          ) THEN
            CREATE INDEX idx_guarded ON users (email);
          END IF;
        END $$;
        """
        statement = one(guarded_index, BIG)
        assert statement.verdict.classification is Classification.NEEDS_TIMING
        assert any("UNSAFE" in c for c in statement.verdict.conditions)

    def test_guarded_unknown_stays_unknown(self) -> None:
        statement = one(self.GUARDED.replace(
            "CREATE TABLE archive_users (id bigint);",
            "ALTER TABLE users ALTER COLUMN email SET NOT NULL;",
        ))
        assert statement.verdict.classification is Classification.UNKNOWN


class TestTransactions:
    def test_multi_ael_transaction_is_flagged(self) -> None:
        result = assess(
            "BEGIN;\nALTER TABLE a ADD COLUMN x int;\nALTER TABLE b ADD COLUMN y int;\nCOMMIT;"
        )
        assert len(result.transaction_warnings) == 1
        warning = result.transaction_warnings[0]
        assert warning.relations == ("a", "b")
        assert not warning.hypothetical
        assert warning.method is Method.PROVEN

    def test_hypothetical_warning_without_explicit_txn(self) -> None:
        result = assess(
            "ALTER TABLE a ADD COLUMN x int;\nALTER TABLE b ADD COLUMN y int;"
        )
        assert len(result.transaction_warnings) == 1
        assert result.transaction_warnings[0].hypothetical

    def test_held_locks_recorded_and_escalated(self) -> None:
        small_b = snapshot(
            relations=(relation("a", rows=100), relation("b", rows=100))
        )
        result = assess(
            "BEGIN;\nALTER TABLE a ADD COLUMN x int;\nCREATE INDEX i ON b (id);\nCOMMIT;",
            small_b,
        )
        index_statement = result.statements[2]
        assert index_statement.held_locks_before
        held_modes = {lock.lock_mode.value for lock in index_statement.held_locks_before}
        assert "ACCESS EXCLUSIVE" in held_modes
        assert any("COMMIT" in note for note in index_statement.notes)

    def test_forbidden_statement_notes_runner_wrapping(self) -> None:
        result = assess("CREATE INDEX CONCURRENTLY i ON users (email);")
        assert any("must not wrap" in note for note in result.notes)


class TestContention:
    def test_observed_conflict_escalates(self) -> None:
        contended = snapshot(
            relations=(relation("users", rows=200),),
            waiters=(waiter("public.users"),),
        )
        verdict = one("CREATE INDEX i ON users (email);", contended).verdict
        assert verdict.classification is Classification.NEEDS_TIMING
        assert "pg_locks" in verdict.rationale
        # weakest contributor wins: SIMULATED duration + OBSERVED conflict
        assert verdict.method is Method.SIMULATED

    def test_waiters_unavailable_degrades_not_escalates(self) -> None:
        degraded = snapshot(
            relations=(relation("users", rows=200),),
            waiters_unavailable="pg_monitor missing",
        )
        statement = one("CREATE INDEX i ON users (email);", degraded)
        assert statement.verdict.classification is Classification.SAFE
        contention = statement.rows[0].contention[0]
        assert contention.conflicting_lock_held.value is None
        assert contention.conflicting_lock_held.method is Method.UNVERIFIED


class TestReversibility:
    def test_drop_table_is_irreversible_and_never_safe(self) -> None:
        statement = one("DROP TABLE payments;", SMALL)
        assert statement.reversibility.reversibility.value == "irreversible"
        assert statement.reversibility.what_is_lost
        assert statement.verdict.classification is not Classification.SAFE

    def test_insert_values_stays_safe(self) -> None:
        statement = one("INSERT INTO users (id) VALUES (1);", SMALL)
        assert statement.verdict.classification is Classification.SAFE

    def test_dropping_file_created_table_is_safe_irreversible(self) -> None:
        # Nothing production-visible is blocked and nothing anyone could
        # read is lost, so it stays in the safe tiers — but a DROP has no
        # undo, and saying so costs the reviewer only a note. The old
        # engine suppressed that fact here; SAFE_IRREVERSIBLE records it.
        result = assess("CREATE TABLE tmp (id int);\nDROP TABLE tmp;")
        verdict = result.statements[1].verdict
        assert verdict.classification is Classification.SAFE_IRREVERSIBLE
        assert any("no undo" in c for c in verdict.conditions)


class TestDurationBands:
    """The primary assessment is classification + band; numbers are detail."""

    def test_band_is_the_primary_duration_reading(self) -> None:
        mid = snapshot(relations=(relation("users", rows=10_000_000),))
        statement = one("CREATE INDEX idx ON users (email);", mid)
        assert statement.verdict.band is DurationBand.SECONDS
        # The numeric interval stays available, but as secondary detail on
        # the row's estimate — the headline rationale carries no numbers.
        [row] = statement.rows
        assert isinstance(row.duration, DurationEstimate)
        assert row.duration.band is statement.verdict.band
        assert "estimated" not in statement.verdict.rationale

    def test_band_tracks_size(self) -> None:
        big = one("CREATE INDEX idx ON users (email);", BIG).verdict
        assert big.band is DurationBand.MINUTES
        small = one("CREATE INDEX idx ON users (email);", SMALL).verdict
        assert small.band is DurationBand.SUB_SECOND

    def test_unknown_verdicts_carry_no_band(self) -> None:
        offline = one("CREATE INDEX idx ON users (email);")
        assert offline.verdict.classification is Classification.UNKNOWN
        assert offline.verdict.band is None

    def test_constant_ops_read_sub_second(self) -> None:
        verdict = one("ALTER TABLE users ALTER COLUMN email SET DEFAULT 'x';").verdict
        assert verdict.band is DurationBand.SUB_SECOND


class TestDml:
    def test_matched_update_small_table_is_safe_irreversible(self) -> None:
        verdict = one("UPDATE users SET email = 'x' WHERE id = 4;", SMALL).verdict
        assert verdict.classification is Classification.SAFE_IRREVERSIBLE
        assert any("irreversible" in c for c in verdict.conditions)

    def test_matched_update_large_table_needs_timing_with_bound(self) -> None:
        verdict = one("UPDATE users SET email = 'x' WHERE id > 4;", BIG).verdict
        assert verdict.classification is Classification.NEEDS_TIMING
        assert any("worst case" in c for c in verdict.conditions)

    def test_insert_select_needs_timing(self) -> None:
        verdict = one("INSERT INTO users SELECT * FROM staging;", SMALL).verdict
        assert verdict.classification is Classification.NEEDS_TIMING
        assert any("source query" in c for c in verdict.conditions)


class TestFiveTierPlacement:
    """The tiers name the action required, not how severe the form sounds.

    These pin the split that replaced CONDITIONALLY_SAFE: irreversibility
    selects a safe tier rather than leaving the safe tiers, and only a
    lock that is disruptive at the wrong moment reaches NEEDS_TIMING.
    """

    def test_enum_addition_is_safe_irreversible_not_timed(self) -> None:
        # The canonical harmless-but-permanent statement: no lock anyone
        # waits on, no undo. 389 of these in the wild corpus, every one of
        # them formerly CONDITIONALLY_SAFE.
        verdict = one("ALTER TYPE mood ADD VALUE 'elated';", SMALL).verdict
        assert verdict.classification is Classification.SAFE_IRREVERSIBLE
        assert any("never be removed" in c for c in verdict.conditions)

    def test_irreversibility_alone_never_leaves_the_safe_tiers(self) -> None:
        # Cases where the *only* thing against the statement is that it
        # cannot be undone. (A DROP VIEW, by contrast, legitimately lands
        # in NEEDS_TIMING — not for its irreversibility but for the
        # ACCESS EXCLUSIVE lock it takes on a live relation.)
        for sql in (
            "ALTER TYPE mood ADD VALUE 'elated';",
            "UPDATE users SET email = 'x' WHERE id = 4;",
            "DELETE FROM users WHERE id = 4;",
        ):
            statement = one(sql, SMALL)
            assert statement.reversibility.reversibility is Reversibility.IRREVERSIBLE
            assert statement.verdict.classification in SAFE_TIERS, sql

    def test_brief_ael_needs_timing_while_brief_write_block_stays_safe(self) -> None:
        # Same constant-time work, different blast radius: the AEL queue
        # poisons everything behind it, the SHARE ROW EXCLUSIVE one does
        # not. This is the distinction NEEDS_TIMING exists to carry.
        ael = one("ALTER TABLE users ALTER COLUMN email SET DEFAULT 'x';", SMALL)
        sre = one(
            "CREATE TRIGGER trg BEFORE INSERT ON users "
            "FOR EACH ROW EXECUTE FUNCTION f();",
            SMALL,
        )
        assert ael.verdict.classification is Classification.NEEDS_TIMING
        assert sre.verdict.classification is Classification.SAFE

    def test_seconds_band_on_a_live_table_needs_timing(self) -> None:
        mid = snapshot(relations=(relation("users", rows=10_000_000),))
        verdict = one("CREATE INDEX idx ON users (email);", mid).verdict
        assert verdict.classification is Classification.NEEDS_TIMING
        assert verdict.band is DurationBand.SECONDS

    def test_timing_beats_recording_when_both_apply(self) -> None:
        # An unbatched backfill big enough to band up is both irreversible
        # and disruptive; the stronger required action owns the headline,
        # and the loss stays on the reversibility field regardless.
        mid = snapshot(relations=(relation("users", rows=300_000),))
        statement = one("UPDATE users SET email = lower(email);", mid)
        assert statement.verdict.classification is Classification.NEEDS_TIMING
        assert statement.reversibility.reversibility is Reversibility.IRREVERSIBLE
        assert statement.reversibility.what_is_lost

    def test_contention_lifts_both_safe_tiers_to_needs_timing(self) -> None:
        contended = snapshot(
            relations=(relation("users", rows=200),),
            waiters=(waiter("public.users"),),
        )
        plain = one("CREATE INDEX i ON users (email);", contended).verdict
        irreversible = one("DELETE FROM users WHERE id = 1;", contended).verdict
        assert plain.classification is Classification.NEEDS_TIMING
        assert irreversible.classification is Classification.NEEDS_TIMING

    def test_no_verdict_still_uses_the_retired_tier(self) -> None:
        assert not hasattr(Classification, "CONDITIONALLY_SAFE")
        assert [c.value for c in Classification] == [
            "safe",
            "safe_irreversible",
            "needs_timing",
            "unsafe",
            "unknown",
        ]


class TestAccessExclusiveFloor:
    """The NEEDS_TIMING boundary is drawn on the lock, not on the code path.

    Before this floor, a brief ACCESS EXCLUSIVE hold reached through the
    constant-op branch became NEEDS_TIMING while the identical lock
    reached through the proportional branch on a small table stayed SAFE.
    At 1k rows that rated a pure relabel NEEDS_TIMING and an actual heap
    rewrite SAFE — both holding ACCESS EXCLUSIVE.
    """

    def test_constant_and_proportional_ael_agree_on_a_small_table(self) -> None:
        # Left: catalog-only work (constant path). Right: a real heap
        # rewrite (proportional path) that happens to be brief because the
        # table is small. Same lock, same required action.
        constant = one("ALTER TABLE users ALTER COLUMN email SET DEFAULT 'x';", SMALL)
        rewrite = one(
            "ALTER TABLE users ADD COLUMN uid uuid DEFAULT gen_random_uuid();", SMALL
        )
        assert constant.verdict.classification is Classification.NEEDS_TIMING
        assert rewrite.verdict.classification is Classification.NEEDS_TIMING
        assert rewrite.verdict.band is DurationBand.SUB_SECOND

    def test_the_floor_names_the_lock_and_asks_for_a_window(self) -> None:
        verdict = one(
            "ALTER TABLE users ADD COLUMN uid uuid DEFAULT gen_random_uuid();", SMALL
        ).verdict
        assert "ACCESS EXCLUSIVE on users" in verdict.rationale
        assert any("lock_timeout" in c for c in verdict.conditions)

    def test_relation_created_in_the_same_file_is_exempt(self) -> None:
        # Nothing else can hold a lock on a relation nothing else has seen.
        result = assess(
            "CREATE TABLE t (id int);\n"
            "ALTER TABLE t ADD COLUMN uid uuid DEFAULT gen_random_uuid();",
            SMALL,
        )
        assert result.statements[1].verdict.classification is Classification.SAFE

    def test_a_live_relation_is_not_exempt_by_a_same_named_creation_elsewhere(
        self,
    ) -> None:
        # The exemption is per relation, not per file: users is untouched
        # by the CREATE, so its ACCESS EXCLUSIVE still needs a window.
        result = assess(
            "CREATE TABLE t (id int);\n"
            "ALTER TABLE users ADD COLUMN uid uuid DEFAULT gen_random_uuid();",
            SMALL,
        )
        assert result.statements[1].verdict.classification is Classification.NEEDS_TIMING

    def test_weaker_locks_on_live_relations_are_untouched(self) -> None:
        # The floor is ACCESS EXCLUSIVE only. A brief SHARE (index build)
        # or SHARE ROW EXCLUSIVE (trigger) block on a live relation stays
        # SAFE — calling 1,007 wild CREATE TRIGGERs timed would be noise.
        index = one("CREATE INDEX idx ON users (email);", SMALL)
        trigger = one(
            "CREATE TRIGGER trg BEFORE INSERT ON users "
            "FOR EACH ROW EXECUTE FUNCTION f();",
            SMALL,
        )
        assert index.verdict.classification is Classification.SAFE
        assert trigger.verdict.classification is Classification.SAFE

    def test_the_floor_never_reaches_unsafe(self) -> None:
        # It runs after the contention escalation precisely so it can only
        # lift the safe tiers: an observed conflict on a small table is
        # still NEEDS_TIMING, not UNSAFE.
        contended = snapshot(
            relations=(relation("users", rows=200),),
            waiters=(waiter("public.users"),),
        )
        verdict = one(
            "ALTER TABLE users ADD COLUMN uid uuid DEFAULT gen_random_uuid();",
            contended,
        ).verdict
        assert verdict.classification is Classification.NEEDS_TIMING

    def test_the_floor_keeps_the_irreversibility_condition(self) -> None:
        # An irreversible statement that also takes a live AEL reports the
        # timing requirement as its tier and keeps the loss on the record.
        statement = one("ALTER TABLE users DROP COLUMN email;", SMALL)
        assert statement.verdict.classification is Classification.NEEDS_TIMING
        assert statement.reversibility.reversibility is Reversibility.IRREVERSIBLE
        assert statement.reversibility.what_is_lost

    def test_unknown_is_not_floored(self) -> None:
        # No snapshot: the statement is undecided, and an undecided
        # statement must not be quietly reclassified as merely timed.
        assert (
            one("ALTER TABLE users ADD COLUMN uid uuid DEFAULT gen_random_uuid();")
            .verdict.classification
            is Classification.UNKNOWN
        )


class TestUnsafeIsNeverSoftened:
    """No path introduced by the tier split may demote UNSAFE to a safe tier."""

    # ALTER COLUMN TYPE is deliberately absent: without type-change facts
    # it is UNKNOWN, not UNSAFE, which TestSnapshotRemoval already pins.
    UNSAFE_CASES = (
        ("CREATE INDEX idx ON users (lower(email));", BIG),
        ("CREATE INDEX idx ON users USING gin (payload);", BIG),
        ("UPDATE users SET email = lower(email);", BIG),
        ("DELETE FROM users;", BIG),
        ("ALTER TABLE users ADD COLUMN uid uuid DEFAULT gen_random_uuid();", BIG),
    )

    @pytest.mark.parametrize(("sql", "snap"), UNSAFE_CASES)
    def test_stays_unsafe(self, sql: str, snap: LiveSnapshot) -> None:
        assert one(sql, snap).verdict.classification is Classification.UNSAFE

    @pytest.mark.parametrize(("sql", "snap"), UNSAFE_CASES)
    def test_irreversibility_does_not_soften_it(self, sql: str, snap: LiveSnapshot) -> None:
        statement = one(sql, snap)
        assert statement.verdict.classification not in SAFE_TIERS

    def test_proven_failure_stays_unsafe(self) -> None:
        statement = assess(
            "BEGIN;\nCREATE INDEX CONCURRENTLY idx ON users (email);\nCOMMIT;"
        ).statements[1]
        assert statement.verdict.classification is Classification.UNSAFE
