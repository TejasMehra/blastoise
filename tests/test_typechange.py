"""Pure tests for the ALTER COLUMN TYPE rewrite lookup.

No database: TypeChangeFacts are built by hand. The live counterpart
(``test_live_introspect.py``) checks the same rules against relfilenode
ground truth on a real server.
"""

from __future__ import annotations

from typing import Any

from blastoise.live.model import Fact, TypeChangeFacts
from blastoise.live.typechange import (
    RewriteVerdict,
    _parse_type_text,
    assess_type_change,
)


def make_facts(**overrides: Any) -> TypeChangeFacts:
    base: dict[str, Any] = {
        "relation": "public.t",
        "column": "c",
        "new_type_requested": "text",
        "current_type": Fact.of("character varying(10)"),
        "current_typmod": Fact.of(14),
        "current_base_type": Fact.of("character varying"),
        "current_is_domain": Fact.of(False),
        "current_domain_not_null": Fact.of(False),
        "current_domain_constraint_count": Fact.of(0),
        "new_type": Fact.of("text"),
        "new_base_type": Fact.of("text"),
        "new_is_domain": Fact.of(False),
        "new_domain_not_null": Fact.of(False),
        "new_domain_constraint_count": Fact.of(0),
        "new_domain_has_typmod": Fact.of(False),
        "same_type": Fact.of(False),
        "bases_same": Fact.of(False),
        "cast_method": Fact.of("b"),
        "cast_context": Fact.of("i"),
    }
    base.update(overrides)
    return TypeChangeFacts(**base)


class TestParseTypeText:
    def test_format_type_spellings(self) -> None:
        assert _parse_type_text("character varying(20)") == ("varchar", (20,))
        assert _parse_type_text("timestamp(3) without time zone") == ("timestamp", (3,))
        assert _parse_type_text("timestamp with time zone") == ("timestamptz", None)
        assert _parse_type_text("numeric(10,2)") == ("numeric", (10, 2))
        assert _parse_type_text("bit varying(8)") == ("varbit", (8,))

    def test_migration_spellings(self) -> None:
        assert _parse_type_text("varchar(20)") == ("varchar", (20,))
        assert _parse_type_text("pg_catalog.varchar") == ("varchar", None)
        assert _parse_type_text("Decimal(12)") == ("numeric", (12,))
        assert _parse_type_text("timestamptz(3)") == ("timestamptz", (3,))
        assert _parse_type_text("TEXT") == ("text", None)

    def test_unknown_names_pass_through(self) -> None:
        assert _parse_type_text("citext") == ("citext", None)
        assert _parse_type_text("my custom thing") == ("my custom thing", None)

    def test_odd_spellings_are_none(self) -> None:
        assert _parse_type_text('"Quoted"') is None
        assert _parse_type_text("int[]") is None
        assert _parse_type_text("wat(3x)") is None
        assert _parse_type_text("(3)") is None  # typmod with no name
        assert _parse_type_text("3crowds(1)") is None


class TestAssessGuards:
    def test_using_expression_is_worst_case(self) -> None:
        result = assess_type_change(
            make_facts(), pg_major=17, has_using_expression=True
        )
        assert result.verdict is RewriteVerdict.REWRITE
        assert "USING" in result.reason

    def test_missing_fact_is_unknown(self) -> None:
        result = assess_type_change(
            make_facts(cast_method=Fact.unavailable("boom")), pg_major=17
        )
        assert result.verdict is RewriteVerdict.UNKNOWN
        assert "boom" in result.reason

    def test_unparsable_target_spelling_is_unknown(self) -> None:
        result = assess_type_change(
            make_facts(new_type_requested='"MixedCase"'), pg_major=17
        )
        assert result.verdict is RewriteVerdict.UNKNOWN


class TestSameTypeRules:
    def test_identical_type_and_typmod(self) -> None:
        facts = make_facts(
            new_type_requested="varchar(10)",
            new_type=Fact.of("character varying"),
            new_base_type=Fact.of("character varying"),
            same_type=Fact.of(True),
            bases_same=Fact.of(True),
            cast_method=Fact.of(None),
        )
        result = assess_type_change(facts, pg_major=17)
        assert result.verdict is RewriteVerdict.NO_REWRITE

    def test_varchar_growth(self) -> None:
        facts = make_facts(
            new_type_requested="varchar(20)",
            new_type=Fact.of("character varying"),
            new_base_type=Fact.of("character varying"),
            same_type=Fact.of(True),
            bases_same=Fact.of(True),
            cast_method=Fact.of(None),
        )
        result = assess_type_change(facts, pg_major=17)
        assert result.verdict is RewriteVerdict.NO_REWRITE
        assert "varchar_support" in result.source

    def test_varchar_shrink_rewrites(self) -> None:
        facts = make_facts(
            current_type=Fact.of("character varying(20)"),
            new_type_requested="varchar(10)",
            new_type=Fact.of("character varying"),
            new_base_type=Fact.of("character varying"),
            same_type=Fact.of(True),
            bases_same=Fact.of(True),
            cast_method=Fact.of(None),
        )
        assert assess_type_change(facts, pg_major=17).verdict is RewriteVerdict.REWRITE

    def test_unbounded_varchar_gaining_a_limit_rewrites(self) -> None:
        facts = make_facts(
            current_type=Fact.of("character varying"),
            new_type_requested="varchar(10)",
            new_type=Fact.of("character varying"),
            new_base_type=Fact.of("character varying"),
            same_type=Fact.of(True),
            bases_same=Fact.of(True),
            cast_method=Fact.of(None),
        )
        assert assess_type_change(facts, pg_major=17).verdict is RewriteVerdict.REWRITE

    def test_dropping_the_typmod_never_rewrites(self) -> None:
        facts = make_facts(
            new_type_requested="varchar",
            new_type=Fact.of("character varying"),
            new_base_type=Fact.of("character varying"),
            same_type=Fact.of(True),
            bases_same=Fact.of(True),
            cast_method=Fact.of(None),
        )
        assert assess_type_change(facts, pg_major=17).verdict is RewriteVerdict.NO_REWRITE

    def test_numeric_scale_change_rewrites_even_growing(self) -> None:
        facts = make_facts(
            current_type=Fact.of("numeric(10,2)"),
            current_base_type=Fact.of("numeric"),
            new_type_requested="numeric(12,3)",
            new_type=Fact.of("numeric"),
            new_base_type=Fact.of("numeric"),
            same_type=Fact.of(True),
            bases_same=Fact.of(True),
            cast_method=Fact.of(None),
        )
        assert assess_type_change(facts, pg_major=17).verdict is RewriteVerdict.REWRITE

    def test_numeric_precision_growth_same_scale(self) -> None:
        facts = make_facts(
            current_type=Fact.of("numeric(10,2)"),
            current_base_type=Fact.of("numeric"),
            new_type_requested="numeric(12,2)",
            new_type=Fact.of("numeric"),
            new_base_type=Fact.of("numeric"),
            same_type=Fact.of(True),
            bases_same=Fact.of(True),
            cast_method=Fact.of(None),
        )
        assert assess_type_change(facts, pg_major=17).verdict is RewriteVerdict.NO_REWRITE

    def test_bare_timestamp_means_precision_six(self) -> None:
        facts = make_facts(
            current_type=Fact.of("timestamp without time zone"),
            current_base_type=Fact.of("timestamp without time zone"),
            new_type_requested="timestamp(6)",
            new_type=Fact.of("timestamp without time zone"),
            new_base_type=Fact.of("timestamp without time zone"),
            same_type=Fact.of(True),
            bases_same=Fact.of(True),
            cast_method=Fact.of(None),
        )
        assert assess_type_change(facts, pg_major=17).verdict is RewriteVerdict.NO_REWRITE

    def test_bpchar_length_change_rewrites(self) -> None:
        facts = make_facts(
            current_type=Fact.of("character(10)"),
            current_base_type=Fact.of("character"),
            new_type_requested="char(20)",
            new_type=Fact.of("character"),
            new_base_type=Fact.of("character"),
            same_type=Fact.of(True),
            bases_same=Fact.of(True),
            cast_method=Fact.of(None),
        )
        assert assess_type_change(facts, pg_major=17).verdict is RewriteVerdict.REWRITE


class TestTimestampTimezonePair:
    def _facts(self, cur: str, new_requested: str) -> TypeChangeFacts:
        tz = "with" in cur
        return make_facts(
            current_type=Fact.of(cur),
            current_base_type=Fact.of(cur.split("(")[0].strip() if "(" not in cur else cur),
            new_type_requested=new_requested,
            new_type=Fact.of(
                "timestamp without time zone" if tz else "timestamp with time zone"
            ),
            new_base_type=Fact.of(
                "timestamp without time zone" if tz else "timestamp with time zone"
            ),
            same_type=Fact.of(False),
            bases_same=Fact.of(False),
            cast_method=Fact.of("f"),
        )

    def test_conditional_on_utc_from_pg12(self) -> None:
        result = assess_type_change(
            self._facts("timestamp without time zone", "timestamptz"), pg_major=17
        )
        assert result.verdict is RewriteVerdict.NO_REWRITE_IF_SESSION_TZ_UTC
        assert "TimeZone=UTC" in result.reason

    def test_always_rewrites_before_pg12(self) -> None:
        result = assess_type_change(
            self._facts("timestamp without time zone", "timestamptz"), pg_major=11
        )
        assert result.verdict is RewriteVerdict.REWRITE

    def test_precision_shrink_rewrites_even_in_utc(self) -> None:
        facts = make_facts(
            current_type=Fact.of("timestamp(6) without time zone"),
            current_base_type=Fact.of("timestamp without time zone"),
            new_type_requested="timestamptz(3)",
            new_type=Fact.of("timestamp with time zone"),
            new_base_type=Fact.of("timestamp with time zone"),
            same_type=Fact.of(False),
            bases_same=Fact.of(False),
            cast_method=Fact.of("f"),
        )
        assert assess_type_change(facts, pg_major=17).verdict is RewriteVerdict.REWRITE


class TestDomainRules:
    def test_constrained_target_domain_rewrites(self) -> None:
        facts = make_facts(
            new_type_requested="email_address",
            new_type=Fact.of("email_address"),
            new_base_type=Fact.of("text"),
            new_is_domain=Fact.of(True),
            new_domain_constraint_count=Fact.of(1),
            bases_same=Fact.of(True),
            cast_method=Fact.of(None),
        )
        result = assess_type_change(facts, pg_major=17)
        assert result.verdict is RewriteVerdict.REWRITE
        assert "domain" in result.reason

    def test_not_null_target_domain_rewrites(self) -> None:
        facts = make_facts(
            new_type_requested="dom_nn",
            new_type=Fact.of("dom_nn"),
            new_base_type=Fact.of("text"),
            new_is_domain=Fact.of(True),
            new_domain_not_null=Fact.of(True),
            bases_same=Fact.of(True),
            cast_method=Fact.of(None),
        )
        assert assess_type_change(facts, pg_major=17).verdict is RewriteVerdict.REWRITE

    def test_unconstrained_target_domain_over_same_base_is_free(self) -> None:
        # Verified live: text -> unconstrained-domain-over-text keeps the
        # relfilenode; the docs' exception list is narrower than the code.
        facts = make_facts(
            current_type=Fact.of("text"),
            current_base_type=Fact.of("text"),
            new_type_requested="dom_plain",
            new_type=Fact.of("dom_plain"),
            new_base_type=Fact.of("text"),
            new_is_domain=Fact.of(True),
            bases_same=Fact.of(True),
            cast_method=Fact.of(None),
        )
        assert assess_type_change(facts, pg_major=17).verdict is RewriteVerdict.NO_REWRITE

    def test_target_domain_with_typmod_rewrites(self) -> None:
        facts = make_facts(
            new_type_requested="dom_vc20",
            new_type=Fact.of("dom_vc20"),
            new_base_type=Fact.of("character varying"),
            new_is_domain=Fact.of(True),
            new_domain_has_typmod=Fact.of(True),
            bases_same=Fact.of(True),
            cast_method=Fact.of(None),
        )
        assert assess_type_change(facts, pg_major=17).verdict is RewriteVerdict.REWRITE

    def test_domain_source_with_typmod_target_is_worst_case(self) -> None:
        facts = make_facts(
            current_type=Fact.of("dom_text"),
            current_base_type=Fact.of("text"),
            current_is_domain=Fact.of(True),
            new_type_requested="varchar(10)",
            new_type=Fact.of("character varying"),
            new_base_type=Fact.of("character varying"),
            same_type=Fact.of(False),
            bases_same=Fact.of(True),
            cast_method=Fact.of(None),
        )
        result = assess_type_change(facts, pg_major=17)
        assert result.verdict is RewriteVerdict.REWRITE
        assert "not modeled" in result.reason

    def test_constrained_source_domain_dropping_to_base_is_free(self) -> None:
        # Verified live: constraints are dropped, not checked, on the way out.
        facts = make_facts(
            current_type=Fact.of("dom_chk"),
            current_base_type=Fact.of("text"),
            current_is_domain=Fact.of(True),
            current_domain_constraint_count=Fact.of(1),
            new_type_requested="text",
            new_type=Fact.of("text"),
            new_base_type=Fact.of("text"),
            same_type=Fact.of(False),
            bases_same=Fact.of(True),
            cast_method=Fact.of(None),
        )
        assert assess_type_change(facts, pg_major=17).verdict is RewriteVerdict.NO_REWRITE


class TestCrossTypeRules:
    def test_binary_coercible_without_typmod(self) -> None:
        result = assess_type_change(make_facts(), pg_major=17)  # varchar(10) -> text
        assert result.verdict is RewriteVerdict.NO_REWRITE
        assert "castmethod" in result.reason or "binary" in result.reason

    def test_binary_coercible_with_target_typmod_rewrites(self) -> None:
        facts = make_facts(
            current_type=Fact.of("text"),
            current_base_type=Fact.of("text"),
            new_type_requested="varchar(10)",
            new_type=Fact.of("character varying"),
            new_base_type=Fact.of("character varying"),
            cast_method=Fact.of("b"),
        )
        assert assess_type_change(facts, pg_major=17).verdict is RewriteVerdict.REWRITE

    def test_function_cast_rewrites(self) -> None:
        facts = make_facts(
            current_type=Fact.of("integer"),
            current_base_type=Fact.of("integer"),
            new_type_requested="bigint",
            new_type=Fact.of("bigint"),
            new_base_type=Fact.of("bigint"),
            cast_method=Fact.of("f"),
        )
        result = assess_type_change(facts, pg_major=17)
        assert result.verdict is RewriteVerdict.REWRITE
        assert "conversion function" in result.reason

    def test_io_cast_rewrites(self) -> None:
        facts = make_facts(cast_method=Fact.of("i"))
        result = assess_type_change(facts, pg_major=17)
        assert result.verdict is RewriteVerdict.REWRITE
        assert "I/O" in result.reason

    def test_no_cast_at_all_rewrites_and_says_why(self) -> None:
        facts = make_facts(
            current_type=Fact.of("integer"),
            current_base_type=Fact.of("integer"),
            new_type_requested="point",
            new_type=Fact.of("point"),
            new_base_type=Fact.of("point"),
            cast_method=Fact.of(None),
        )
        result = assess_type_change(facts, pg_major=17)
        assert result.verdict is RewriteVerdict.REWRITE
        assert "USING" in result.reason
