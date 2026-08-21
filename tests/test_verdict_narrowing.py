"""Live-fact narrowing tests: worst cases resolve, missing facts stay UNKNOWN."""

from __future__ import annotations

from verdict_helpers import (
    check_constraint,
    column,
    fk_constraint,
    function_fact,
    index_facts,
    relation,
    snapshot,
    type_change,
    type_fact,
)

from pgverdict.catalog.loader import load_catalog
from pgverdict.catalog.model import LockMode
from pgverdict.live.model import IndexFacts, LiveSnapshot
from pgverdict.parser import parse_migration
from pgverdict.verdict import (
    Classification,
    DurationBand,
    Method,
    Reversibility,
    StatementAssessment,
    assess_script,
)
from pgverdict.verdict.narrow import check_expression_proves_not_null

CATALOG = load_catalog()


def one(sql: str, snap: LiveSnapshot | None = None, pg: int = 17) -> StatementAssessment:
    result = assess_script(parse_migration(sql), CATALOG, pg, snap)
    assert len(result.statements) == 1
    return result.statements[0]


BIG_USERS = relation("users", rows=40_000_000)
SMALL_USERS = relation("users", rows=200)


class TestAddColumnDomain:
    SQL = "ALTER TABLE users ADD COLUMN note text DEFAULT 'x';"

    def test_plain_type_narrows_to_fast_path(self) -> None:
        snap = snapshot(relations=(BIG_USERS,), types=(type_fact("text"),))
        statement = one(self.SQL, snap)
        # Fast path proven: brief ACCESS EXCLUSIVE only, even on 40M rows.
        assert statement.verdict.classification is Classification.NEEDS_TIMING
        assert any("not a domain" in n for n in statement.rows[0].narrowings)

    def test_constrained_domain_rewrites(self) -> None:
        sql = "ALTER TABLE users ADD COLUMN mail email_addr DEFAULT 'x';"
        snap = snapshot(
            relations=(BIG_USERS,),
            types=(type_fact("email_addr", is_domain=True, domain_constraints=2),),
        )
        statement = one(sql, snap)
        assert statement.verdict.classification is Classification.UNSAFE
        assert any("disabled" in n for n in statement.rows[0].narrowings)

    def test_unconstrained_domain_keeps_fast_path(self) -> None:
        sql = "ALTER TABLE users ADD COLUMN mail email_addr DEFAULT 'x';"
        snap = snapshot(
            relations=(BIG_USERS,),
            types=(type_fact("email_addr", is_domain=True),),
        )
        statement = one(sql, snap)
        assert statement.verdict.classification is Classification.NEEDS_TIMING

    def test_missing_type_fact_stays_unknown(self) -> None:
        snap = snapshot(relations=(BIG_USERS,))
        statement = one(self.SQL, snap)
        assert statement.verdict.classification is Classification.UNKNOWN
        assert "could not supply" in statement.verdict.rationale


class TestUnknownVolatility:
    SQL = "ALTER TABLE users ADD COLUMN uid text DEFAULT my_gen();"

    def test_volatile_function_forces_rewrite(self) -> None:
        snap = snapshot(
            relations=(BIG_USERS,),
            functions=(function_fact("my_gen", "v"),),
            types=(type_fact("text"),),
        )
        statement = one(self.SQL, snap)
        assert statement.verdict.classification is Classification.UNSAFE
        assert any("VOLATILE" in n for n in statement.rows[0].narrowings)

    def test_immutable_function_takes_fast_path(self) -> None:
        snap = snapshot(
            relations=(BIG_USERS,),
            functions=(function_fact("my_gen", "i"),),
            types=(type_fact("text"),),
        )
        statement = one(self.SQL, snap)
        assert statement.verdict.classification is Classification.NEEDS_TIMING
        assert statement.verdict.method is Method.OBSERVED

    def test_undecided_function_stays_unknown(self) -> None:
        snap = snapshot(relations=(BIG_USERS,), types=(type_fact("text"),))
        statement = one(self.SQL, snap)
        assert statement.verdict.classification is Classification.UNKNOWN


class TestAlterColumnType:
    SQL = "ALTER TABLE users ALTER COLUMN id TYPE bigint;"

    def test_rewrite_on_big_table_is_unsafe(self) -> None:
        snap = snapshot(
            relations=(BIG_USERS,),
            type_changes=(type_change("users", "id", "bigint", cast_method="f"),),
        )
        assert one(self.SQL, snap).verdict.classification is Classification.UNSAFE

    def test_relabel_needs_timing_and_is_reversible(self) -> None:
        sql = "ALTER TABLE users ALTER COLUMN name TYPE text;"
        snap = snapshot(
            relations=(BIG_USERS,),
            type_changes=(
                type_change(
                    "users",
                    "name",
                    "text",
                    current_type="character varying(50)",
                    cast_method="b",
                ),
            ),
        )
        statement = one(sql, snap)
        assert statement.verdict.classification is Classification.NEEDS_TIMING
        assert statement.reversibility.reversibility is Reversibility.REVERSIBLE
        assert statement.reversibility.method is Method.OBSERVED

    def test_missing_typechange_facts_stay_unknown(self) -> None:
        snap = snapshot(relations=(BIG_USERS,))
        assert one(self.SQL, snap).verdict.classification is Classification.UNKNOWN

    def test_offline_reversibility_is_unknown(self) -> None:
        statement = one(self.SQL)
        assert statement.reversibility.reversibility is Reversibility.UNKNOWN

    def test_relabel_and_rewrite_agree_at_1k_rows(self) -> None:
        # The scale harness's exact asymmetry, at its smallest size:
        # alter_type_varchar_widen_plain_idx is a pure relabel whose
        # indexes are reused (constant path) and alter_type_int_bigint is
        # a real heap rewrite (proportional path). Both hold ACCESS
        # EXCLUSIVE on a live relation for the whole statement, so both
        # need a window — the old engine read them NEEDS_TIMING and SAFE
        # respectively, purely on which path estimated the duration.
        small = relation(
            "users",
            rows=1_000,
            indexes=(index_facts("users_name_idx", depends_on=("name",)),),
        )
        relabel = one(
            "ALTER TABLE users ALTER COLUMN name TYPE text;",
            snapshot(
                relations=(small,),
                type_changes=(
                    type_change(
                        "users",
                        "name",
                        "text",
                        current_type="character varying(50)",
                        cast_method="b",
                    ),
                ),
            ),
        )
        rewrite = one(
            self.SQL,
            snapshot(
                relations=(relation("users", rows=1_000),),
                type_changes=(type_change("users", "id", "bigint", cast_method="f"),),
            ),
        )
        assert any("reused" in n for n in relabel.rows[0].narrowings)
        assert relabel.verdict.classification is Classification.NEEDS_TIMING
        assert rewrite.verdict.classification is Classification.NEEDS_TIMING


class TestNoRewriteDependentIndexes:
    """The scale harness's one real severity bug, pinned.

    A no-rewrite type change still rebuilds every dependent index that
    CheckIndexCompatible cannot reuse (partial, expression, non-default
    opclass) — measured 13.5s of ACCESS EXCLUSIVE at 10M rows where the
    old narrowing promised catalog-time. Plain-key default-opclass btrees
    are reused (measured ~0 ms), so they must NOT trigger the rebuild
    model — that would manufacture false UNSAFEs the other way.
    """

    SQL = "ALTER TABLE users ALTER COLUMN name TYPE text;"

    def _snap(
        self,
        *,
        indexes: tuple[IndexFacts, ...] = (),
        indexes_unavailable: str | None = None,
    ) -> LiveSnapshot:
        return snapshot(
            relations=(
                relation(
                    "users",
                    rows=40_000_000,
                    indexes=indexes,
                    indexes_unavailable=indexes_unavailable,
                ),
            ),
            type_changes=(
                type_change(
                    "users",
                    "name",
                    "text",
                    current_type="character varying(50)",
                    cast_method="b",
                ),
            ),
        )

    def test_partial_index_on_the_column_forces_the_rebuild_model(self) -> None:
        snap = self._snap(
            indexes=(index_facts("users_name_active", depends_on=("name", "flag"), partial=True),)
        )
        statement = one(self.SQL, snap)
        assert statement.verdict.classification is Classification.UNSAFE
        assert any("rebuilt" in n for n in statement.rows[0].narrowings)
        # Still a relabel: values are unchanged, so it stays reversible.
        assert statement.reversibility.reversibility is Reversibility.REVERSIBLE

    def test_expression_index_forces_the_rebuild_model(self) -> None:
        snap = self._snap(
            indexes=(index_facts("users_name_lower", depends_on=("name",), has_expressions=True),)
        )
        assert one(self.SQL, snap).verdict.classification is Classification.UNSAFE

    def test_non_default_opclass_forces_the_rebuild_model(self) -> None:
        snap = self._snap(
            indexes=(
                index_facts("users_name_pattern", depends_on=("name",), default_opclasses=False),
            )
        )
        assert one(self.SQL, snap).verdict.classification is Classification.UNSAFE

    def test_plain_btree_on_the_column_is_reused_not_rebuilt(self) -> None:
        snap = self._snap(indexes=(index_facts("users_name_idx", depends_on=("name",)),))
        statement = one(self.SQL, snap)
        assert statement.verdict.classification is Classification.NEEDS_TIMING
        assert any("reused" in n for n in statement.rows[0].narrowings)

    def test_partial_index_on_another_column_is_untouched(self) -> None:
        snap = self._snap(
            indexes=(index_facts("users_email_active", depends_on=("email",), partial=True),)
        )
        statement = one(self.SQL, snap)
        assert statement.verdict.classification is Classification.NEEDS_TIMING
        assert any("no index depends" in n for n in statement.rows[0].narrowings)

    def test_unavailable_index_facts_stay_unknown(self) -> None:
        snap = self._snap(indexes_unavailable="size query timed out")
        statement = one(self.SQL, snap)
        assert statement.verdict.classification is Classification.UNKNOWN
        assert "cannot be ruled out" in statement.verdict.rationale


class TestSetNotNull:
    SQL = "ALTER TABLE users ALTER COLUMN email SET NOT NULL;"

    def test_proving_check_skips_the_scan(self) -> None:
        snap = snapshot(
            relations=(
                relation(
                    "users",
                    rows=40_000_000,
                    constraints=(check_constraint("chk", "(email IS NOT NULL)"),),
                ),
            )
        )
        statement = one(self.SQL, snap)
        assert statement.verdict.classification is Classification.NEEDS_TIMING
        assert any("skipped" in n for n in statement.rows[0].narrowings)

    def test_not_valid_check_does_not_prove(self) -> None:
        snap = snapshot(
            relations=(
                relation(
                    "users",
                    rows=40_000_000,
                    constraints=(
                        check_constraint("chk", "(email IS NOT NULL)", validated=False),
                    ),
                ),
            )
        )
        assert one(self.SQL, snap).verdict.classification is Classification.UNSAFE

    def test_no_proof_scans_small_table_briefly(self) -> None:
        # No proving CHECK, so the scan really runs — but on 200 rows it is
        # sub-second work. The verdict is NEEDS_TIMING on the strength of
        # the ACCESS EXCLUSIVE lock alone, not the scan: the narrowing is
        # absent (nothing was skipped) and the band is sub-second.
        snap = snapshot(relations=(relation("users", rows=200, constraints=()),))
        statement = one(self.SQL, snap)
        assert statement.verdict.classification is Classification.NEEDS_TIMING
        assert statement.verdict.band is DurationBand.SUB_SECOND
        assert not any(
            "skipped" in n or "no-op" in n for n in statement.rows[0].narrowings
        )

    def test_already_not_null_is_noop(self) -> None:
        snap = snapshot(
            relations=(
                relation(
                    "users",
                    rows=40_000_000,
                    columns_facts=(column("email", not_null=True),),
                ),
            )
        )
        statement = one(self.SQL, snap)
        assert statement.verdict.classification is Classification.NEEDS_TIMING
        assert any("no-op" in n for n in statement.rows[0].narrowings)

    def test_pre_12_always_scans(self) -> None:
        snap = snapshot(
            relations=(
                relation(
                    "users",
                    rows=40_000_000,
                    constraints=(check_constraint("chk", "(email IS NOT NULL)"),),
                ),
            )
        )
        # On PG 11 the proving-CHECK shortcut does not exist: plain scan row.
        assert one(self.SQL, snap, pg=11).verdict.classification is Classification.UNSAFE


class TestConstraintNarrowing:
    def test_validate_fk_adds_referenced_relation(self) -> None:
        sql = "ALTER TABLE users VALIDATE CONSTRAINT users_org_fkey;"
        snap = snapshot(
            relations=(
                relation(
                    "users",
                    rows=1000,
                    constraints=(
                        fk_constraint("users_org_fkey", "public.orgs", validated=False),
                    ),
                ),
            )
        )
        statement = one(sql, snap)
        row = statement.rows[0]
        referenced = [r for r in row.relations if r.relation == "public.orgs"]
        assert referenced and referenced[0].lock_mode is LockMode.ROW_SHARE
        assert statement.verdict.classification is Classification.SAFE

    def test_validate_already_validated_is_noop(self) -> None:
        sql = "ALTER TABLE users VALIDATE CONSTRAINT users_org_fkey;"
        snap = snapshot(
            relations=(
                relation(
                    "users",
                    rows=40_000_000,
                    constraints=(fk_constraint("users_org_fkey", "public.orgs"),),
                ),
            )
        )
        statement = one(sql, snap)
        assert statement.verdict.classification is Classification.SAFE
        assert any("already validated" in n for n in statement.rows[0].narrowings)

    def test_drop_fk_locks_referenced_table(self) -> None:
        sql = "ALTER TABLE users DROP CONSTRAINT users_org_fkey;"
        snap = snapshot(
            relations=(
                relation(
                    "users",
                    rows=1000,
                    constraints=(fk_constraint("users_org_fkey", "public.orgs"),),
                ),
            )
        )
        row = one(sql, snap).rows[0]
        referenced = [r for r in row.relations if r.relation == "public.orgs"]
        assert referenced and referenced[0].lock_mode is LockMode.ACCESS_EXCLUSIVE

    def test_constraints_unavailable_stays_unknown(self) -> None:
        sql = "ALTER TABLE users DROP CONSTRAINT users_org_fkey;"
        snap = snapshot(relations=(relation("gone_users", rows=10),))
        assert one(sql, snap).verdict.classification is Classification.UNKNOWN


class TestCheckExpressionProof:
    def test_simple_forms(self) -> None:
        assert check_expression_proves_not_null("(email IS NOT NULL)", "email")
        assert check_expression_proves_not_null("email IS NOT NULL", "email")
        assert check_expression_proves_not_null(
            "((email IS NOT NULL) AND (length(email) > 3))", "email"
        )
        assert check_expression_proves_not_null('("Email" IS NOT NULL)', "Email")

    def test_non_proving_forms(self) -> None:
        assert not check_expression_proves_not_null("(other IS NOT NULL)", "email")
        assert not check_expression_proves_not_null("(email IS NULL)", "email")
        assert not check_expression_proves_not_null(
            "((email IS NOT NULL) OR (id > 0))", "email"
        )
        assert not check_expression_proves_not_null("(length(email) IS NOT NULL)", "email")
