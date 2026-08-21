"""Static volatility analysis for default/generation expressions.

Postgres 11+ adds a column without rewriting the table only when the default
expression is non-volatile, so classifying ``ADD COLUMN`` correctly requires
deciding volatility from the raw parse tree alone. We keep small allowlists
of well-known function names and report ``UNKNOWN`` for anything else, which
later analysis must treat conservatively.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from pglast import ast

from blastoise.ir import Volatility

_RANK: dict[Volatility, int] = {
    Volatility.CONSTANT: 0,
    Volatility.IMMUTABLE: 1,
    Volatility.STABLE: 2,
    Volatility.UNKNOWN: 3,
    Volatility.VOLATILE: 4,
}

VOLATILE_FUNCTIONS = frozenset(
    {
        "random",
        "setseed",
        "gen_random_uuid",
        "gen_random_bytes",
        "gen_salt",
        "uuid_generate_v1",
        "uuid_generate_v1mc",
        "uuid_generate_v4",
        "uuidv4",
        "uuidv7",
        "nextval",
        "currval",
        "lastval",
        "setval",
        "clock_timestamp",
        "timeofday",
        "txid_current",
        "pg_notify",
    }
)

STABLE_FUNCTIONS = frozenset(
    {
        "now",
        "transaction_timestamp",
        "statement_timestamp",
        "current_timestamp",
        "current_date",
        "current_time",
        "localtimestamp",
        "localtime",
        "current_setting",
        "current_schema",
        "current_database",
        "current_user",
        "session_user",
        "to_char",
        "format",
        "concat",
        "concat_ws",
        "array_to_string",  # invokes per-element output functions
    }
)

IMMUTABLE_FUNCTIONS = frozenset(
    {
        "abs",
        "ceil",
        "ceiling",
        "floor",
        "round",
        "trunc",
        "sign",
        "sqrt",
        "power",
        "exp",
        "ln",
        "log",
        "mod",
        "length",
        "char_length",
        "character_length",
        "octet_length",
        "lower",
        "upper",
        "initcap",
        "btrim",
        "ltrim",
        "rtrim",
        "lpad",
        "rpad",
        "left",
        "right",
        "repeat",
        "replace",
        "reverse",
        "substr",
        "substring",
        "split_part",
        "strpos",
        "position",
        "translate",
        "md5",
        "sha224",
        "sha256",
        "sha384",
        "sha512",
        "digest",
        "hmac",
        "encode",
        "decode",
        "greatest",
        "least",
        "array_length",
        "cardinality",
        "string_to_array",
        "timezone",  # AT TIME ZONE parses as pg_catalog.timezone()
        "crypt",  # deterministic given password and salt; gen_salt is the volatile half
        # Range constructors (arguments still dominate: tstzrange(now(), ...)
        # combines to the volatility of its arguments).
        "tstzrange",
        "tsrange",
        "numrange",
        "daterange",
        "int4range",
        "int8range",
    }
)

# Operator spellings whose backing functions are immutable in core Postgres
# (no built-in operator is volatile). Anything else — notably user-defined or
# schema-qualified operators — is UNKNOWN, mirroring the function policy.
_KNOWN_OPERATORS = frozenset(
    {
        "+", "-", "*", "/", "%", "^", "||", "|/", "||/", "@",
        "=", "<>", "!=", "<", ">", "<=", ">=",
        "~", "~*", "!~", "!~*", "~~", "~~*", "!~~", "!~~*",
        "@>", "<@", "&&", "->", "->>", "#>", "#>>", "?", "?|", "?&", "#-",
        "|", "&", "#", "<<", ">>", "!",
        "BETWEEN", "NOT BETWEEN", "BETWEEN SYMMETRIC", "NOT BETWEEN SYMMETRIC",
    }
)


def combine(volatilities: Iterable[Volatility]) -> Volatility:
    """The volatility of a composite expression is its worst part."""
    result = Volatility.CONSTANT
    for vol in volatilities:
        if _RANK[vol] > _RANK[result]:
            result = vol
    return result


def _function_name(call: ast.FuncCall) -> str | None:
    names = [
        part.sval
        for part in call.funcname or ()
        if isinstance(part, ast.String) and part.sval is not None
    ]
    return names[-1].lower() if names else None


def _function_key(call: ast.FuncCall) -> str | None:
    """The full (possibly schema-qualified) dotted name, lowercased.

    This is the key live resolution matches on: the same string is recorded
    in ``DefaultInfo.unknown_functions`` and passed to ``capture_snapshot``
    as a function probe.
    """
    names = [
        part.sval
        for part in call.funcname or ()
        if isinstance(part, ast.String) and part.sval is not None
    ]
    return ".".join(name.lower() for name in names) if names else None


def _static_function_volatility(name: str | None) -> Volatility:
    if name is None:
        return Volatility.UNKNOWN
    if name in VOLATILE_FUNCTIONS:
        return Volatility.VOLATILE
    if name in STABLE_FUNCTIONS:
        return Volatility.STABLE
    if name in IMMUTABLE_FUNCTIONS:
        return Volatility.IMMUTABLE
    return Volatility.UNKNOWN


def _function_volatility(
    call: ast.FuncCall, resolved: Mapping[str, Volatility] | None = None
) -> Volatility:
    base = _static_function_volatility(_function_name(call))
    if base is Volatility.UNKNOWN and resolved is not None:
        key = _function_key(call)
        if key is not None and key in resolved:
            base = resolved[key]
    return combine([base, *(expression_volatility(arg, resolved) for arg in call.args or ())])


def unknown_function_keys(node: object) -> tuple[str, ...]:
    """Names of the functions whose unknown volatility taints ``node``.

    Walks exactly the constructs :func:`expression_volatility` walks (a
    function hidden inside a construct that is itself UNKNOWN — a
    subquery, say — is not collected, because resolving it could not change
    the answer). Sorted and deduplicated; dotted when schema-qualified.
    """
    found: set[str] = set()
    _collect_unknown(node, found)
    return tuple(sorted(found))


def _collect_unknown(node: object, found: set[str]) -> None:
    if node is None or isinstance(node, ast.A_Const | ast.SQLValueFunction | ast.ColumnRef):
        return
    if isinstance(node, ast.TypeCast):
        if not isinstance(node.arg, ast.A_Const):
            _collect_unknown(node.arg, found)
        return
    if isinstance(node, ast.FuncCall):
        if _static_function_volatility(_function_name(node)) is Volatility.UNKNOWN:
            key = _function_key(node)
            if key is not None:
                found.add(key)
        for arg in node.args or ():
            _collect_unknown(arg, found)
        return
    if isinstance(node, ast.A_Expr):
        _collect_unknown(node.lexpr, found)
        _collect_unknown(node.rexpr, found)
        return
    if isinstance(node, ast.BoolExpr | ast.CoalesceExpr | ast.MinMaxExpr | ast.RowExpr):
        for arg in node.args or ():
            _collect_unknown(arg, found)
        return
    if isinstance(node, ast.CaseExpr):
        _collect_unknown(node.arg, found)
        _collect_unknown(node.defresult, found)
        for when in node.args or ():
            _collect_unknown(when, found)
        return
    if isinstance(node, ast.CaseWhen):
        _collect_unknown(node.expr, found)
        _collect_unknown(node.result, found)
        return
    if isinstance(node, ast.NullTest | ast.BooleanTest):
        _collect_unknown(node.arg, found)
        return
    if isinstance(node, ast.A_ArrayExpr):
        for el in node.elements or ():
            _collect_unknown(el, found)
        return
    if isinstance(node, tuple | list):
        for item in node:
            _collect_unknown(item, found)


def resolve_expression_volatility(
    expression: str, resolved: Mapping[str, Volatility]
) -> Volatility:
    """Re-evaluate a deparsed expression with live-resolved function facts.

    ``resolved`` maps the dotted names from
    :func:`unknown_function_keys` to their live-introspected volatility
    (``pg_proc.provolatile``). With an empty mapping — no live connection —
    the result is exactly the static answer: UNKNOWN stays UNKNOWN. Only
    names the static allowlists could not classify are looked up, so a
    live fact can never override a statically known volatility.

    Raises ``pglast.parser.ParseError`` if the expression does not parse in
    scalar position (it came from ``pglast``'s own deparser, so it does).
    """
    from pglast import parse_sql

    [raw] = parse_sql(f"SELECT {expression}")
    stmt = raw.stmt
    assert isinstance(stmt, ast.SelectStmt) and stmt.targetList is not None
    [target] = stmt.targetList
    assert isinstance(target, ast.ResTarget)
    return expression_volatility(target.val, resolved)


def expression_volatility(
    node: object, resolved: Mapping[str, Volatility] | None = None
) -> Volatility:
    """Best-effort volatility of a raw (unanalyzed) expression tree.

    Handles the constructs that legitimately appear in DEFAULT and generated
    column expressions; anything unrecognized is ``UNKNOWN`` rather than
    guessed. Operators are assumed immutable (user-defined volatile operators
    exist but are vanishingly rare in migrations).
    """
    if node is None:
        return Volatility.CONSTANT
    if isinstance(node, ast.A_Const):
        return Volatility.CONSTANT
    if isinstance(node, ast.TypeCast):
        # A cast of a bare untyped literal is folded at parse time, so it is
        # CONSTANT ('now'::timestamptz freezes at ALTER time). Any deeper
        # nesting — including ('now'::text)::timestamptz, the documented idiom
        # for forcing run-time evaluation — goes through the cast function.
        if isinstance(node.arg, ast.A_Const):
            return Volatility.CONSTANT
        return combine([Volatility.IMMUTABLE, expression_volatility(node.arg, resolved)])
    if isinstance(node, ast.SQLValueFunction):
        # CURRENT_TIMESTAMP, CURRENT_DATE, CURRENT_USER, ...
        return Volatility.STABLE
    if isinstance(node, ast.FuncCall):
        return _function_volatility(node, resolved)
    if isinstance(node, ast.A_Expr):
        names = [
            part.sval
            for part in node.name or ()
            if isinstance(part, ast.String) and part.sval is not None
        ]
        if len(names) == 1 and names[0] in _KNOWN_OPERATORS:
            base = Volatility.IMMUTABLE
        else:
            # Schema-qualified or unrecognized operators can wrap volatile
            # functions; treat like unknown function calls.
            base = Volatility.UNKNOWN
        return combine(
            [
                base,
                expression_volatility(node.lexpr, resolved),
                expression_volatility(node.rexpr, resolved),
            ]
        )
    if isinstance(node, ast.BoolExpr):
        return combine(
            [
                Volatility.IMMUTABLE,
                *(expression_volatility(arg, resolved) for arg in node.args or ()),
            ]
        )
    if isinstance(node, ast.CoalesceExpr | ast.MinMaxExpr):
        return combine(
            [
                Volatility.IMMUTABLE,
                *(expression_volatility(arg, resolved) for arg in node.args or ()),
            ]
        )
    if isinstance(node, ast.CaseExpr):
        parts = [
            expression_volatility(node.arg, resolved),
            expression_volatility(node.defresult, resolved),
            *(expression_volatility(when, resolved) for when in node.args or ()),
        ]
        return combine([Volatility.IMMUTABLE, *parts])
    if isinstance(node, ast.CaseWhen):
        return combine(
            [
                expression_volatility(node.expr, resolved),
                expression_volatility(node.result, resolved),
            ]
        )
    if isinstance(node, ast.NullTest | ast.BooleanTest):
        return combine([Volatility.IMMUTABLE, expression_volatility(node.arg, resolved)])
    if isinstance(node, ast.RowExpr):
        return combine(
            [
                Volatility.IMMUTABLE,
                *(expression_volatility(arg, resolved) for arg in node.args or ()),
            ]
        )
    if isinstance(node, ast.A_ArrayExpr):
        return combine(
            [
                Volatility.IMMUTABLE,
                *(expression_volatility(el, resolved) for el in node.elements or ()),
            ]
        )
    if isinstance(node, ast.ColumnRef):
        # A bare reference to another column adds no volatility of its own.
        return Volatility.IMMUTABLE
    if isinstance(node, tuple | list):
        return combine(expression_volatility(item, resolved) for item in node)
    return Volatility.UNKNOWN
