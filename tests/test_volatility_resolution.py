"""Tests for the UNKNOWN-volatility wiring: collection and live resolution.

The static classifier records the function names behind an UNKNOWN default
(``DefaultInfo.unknown_functions``); a snapshot's pg_proc facts decide them;
offline everything stays UNKNOWN. No database here — the live half is
covered in test_live_introspect.py.
"""

from __future__ import annotations

import pytest
from pglast import ast, parse_sql

from pgverdict import parse_migration
from pgverdict.ir import Volatility
from pgverdict.volatility import (
    expression_volatility,
    resolve_expression_volatility,
    unknown_function_keys,
)


def expr_of(sql_expr: str) -> ast.Node:
    [raw] = parse_sql(f"SELECT {sql_expr}")
    stmt = raw.stmt
    assert isinstance(stmt, ast.SelectStmt) and stmt.targetList is not None
    [target] = stmt.targetList
    assert isinstance(target, ast.ResTarget)
    assert target.val is not None
    return target.val


class TestUnknownFunctionKeys:
    @pytest.mark.parametrize(
        ("expression", "expected"),
        [
            ("my_fn()", ("my_fn",)),
            ("app.my_fn(1)", ("app.my_fn",)),
            ("MY_FN()", ("my_fn",)),  # unquoted identifiers fold to lowercase
            ("lower(name)", ()),  # allowlisted: nothing to resolve
            ("nextval('s')", ()),  # volatile is decided, not unknown
            ("my_fn(other_fn(1))", ("my_fn", "other_fn")),  # nested, both
            ("lower(my_fn())", ("my_fn",)),  # known wrapper, unknown inside
            ("my_fn() || other_fn()", ("my_fn", "other_fn")),  # operator sides
            ("COALESCE(my_fn(), 'x')", ("my_fn",)),
            ("GREATEST(my_fn(), 1)", ("my_fn",)),
            ("CASE WHEN my_fn() THEN a_fn() ELSE b_fn() END", ("a_fn", "b_fn", "my_fn")),
            ("CASE my_fn() WHEN 1 THEN 2 END", ("my_fn",)),
            ("my_fn() IS NOT NULL", ("my_fn",)),
            ("ARRAY[my_fn(), 1]", ("my_fn",)),
            ("ROW(my_fn(), 2)", ("my_fn",)),
            ("(my_fn())::text", ("my_fn",)),  # cast of a non-literal descends
            ("'now'::timestamptz", ()),  # cast of a literal is CONSTANT: skip
            ("my_fn() = ANY(ARRAY[other_fn()])", ("my_fn", "other_fn")),
            ("1 + 2", ()),
            ("my_fn() AND true", ("my_fn",)),
        ],
    )
    def test_collects_exactly_the_undecided_names(
        self, expression: str, expected: tuple[str, ...]
    ) -> None:
        node = expr_of(expression)
        assert unknown_function_keys(node) == expected
        # Collection triggers iff the static answer is UNKNOWN-tainted;
        # every collected case must actually be statically UNKNOWN.
        if expected:
            assert expression_volatility(node) is Volatility.UNKNOWN

    def test_duplicates_collapse(self) -> None:
        assert unknown_function_keys(expr_of("my_fn(my_fn())")) == ("my_fn",)

    def test_none_is_empty(self) -> None:
        assert unknown_function_keys(None) == ()

    def test_sequence_input_walks_every_member(self) -> None:
        nodes = [expr_of("my_fn()"), expr_of("other_fn()")]
        assert unknown_function_keys(nodes) == ("my_fn", "other_fn")

    def test_nameless_function_call_yields_no_key(self) -> None:
        # A FuncCall with no String parts is UNKNOWN but unresolvable by
        # name: nothing to collect (mirrors expression_volatility).
        nameless = ast.FuncCall(funcname=None, args=None)
        assert unknown_function_keys(nameless) == ()


class TestResolveExpressionVolatility:
    def test_resolution_decides_and_combines(self) -> None:
        # f_x resolved IMMUTABLE still combines with the STABLE now() inside.
        assert (
            resolve_expression_volatility("f_x(now())", {"f_x": Volatility.IMMUTABLE})
            is Volatility.STABLE
        )
        assert (
            resolve_expression_volatility("f_x(now())", {"f_x": Volatility.VOLATILE})
            is Volatility.VOLATILE
        )

    def test_empty_mapping_is_the_offline_answer(self) -> None:
        assert resolve_expression_volatility("f_x()", {}) is Volatility.UNKNOWN

    def test_partial_resolution_stays_unknown(self) -> None:
        assert (
            resolve_expression_volatility(
                "f_x() || f_y()", {"f_x": Volatility.IMMUTABLE}
            )
            is Volatility.UNKNOWN
        )

    def test_resolved_names_never_override_static_answers(self) -> None:
        # nextval is statically VOLATILE; a lying mapping cannot demote it.
        assert (
            resolve_expression_volatility(
                "nextval('s')", {"nextval": Volatility.IMMUTABLE}
            )
            is Volatility.VOLATILE
        )

    def test_qualified_names_match_by_dotted_key(self) -> None:
        assert (
            resolve_expression_volatility(
                "app.gen_id()", {"app.gen_id": Volatility.STABLE}
            )
            is Volatility.STABLE
        )


class TestClassifierWiring:
    def test_unknown_default_records_its_functions(self) -> None:
        script = parse_migration(
            "ALTER TABLE t ADD COLUMN c text DEFAULT app.gen_slug(7);"
        )
        [statement] = script.statements
        [action] = statement.alter_actions
        assert action.default is not None
        assert action.default.volatility is Volatility.UNKNOWN
        assert action.default.unknown_functions == ("app.gen_slug",)

    def test_decided_defaults_record_nothing(self) -> None:
        script = parse_migration(
            "ALTER TABLE t ADD COLUMN c uuid DEFAULT gen_random_uuid();"
        )
        [statement] = script.statements
        [action] = statement.alter_actions
        assert action.default is not None
        assert action.default.volatility is Volatility.VOLATILE
        assert action.default.unknown_functions == ()
