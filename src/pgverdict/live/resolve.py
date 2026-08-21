"""Turn snapshot facts into the decisions the parser had to leave open.

The static classifier reports ``Volatility.UNKNOWN`` for functions its
allowlists do not know — the correct conservative answer offline. With a
snapshot present, ``pg_proc.provolatile`` decides them:
``decide_default_volatility`` re-evaluates a default expression with the
snapshot's function facts and returns a decided volatility, or the original
``UNKNOWN`` when the snapshot cannot decide (function missing, overloads
disagreeing, capture failed). Without a snapshot nothing changes: offline
stays UNKNOWN.
"""

from __future__ import annotations

from pgverdict.ir import DefaultInfo, Volatility
from pgverdict.live.model import LiveSnapshot
from pgverdict.volatility import resolve_expression_volatility

_PROVOLATILE: dict[str, Volatility] = {
    "i": Volatility.IMMUTABLE,
    "s": Volatility.STABLE,
    "v": Volatility.VOLATILE,
}


def resolved_function_volatilities(snapshot: LiveSnapshot) -> dict[str, Volatility]:
    """Requested-name → volatility, for every decided function fact.

    Functions whose volatility fact is unavailable (missing, or overloads
    disagreeing) are simply absent from the mapping, so downstream keeps
    them UNKNOWN rather than guessing.
    """
    out: dict[str, Volatility] = {}
    for fn in snapshot.functions:
        if fn.volatility.available and fn.volatility.value in _PROVOLATILE:
            out[fn.requested] = _PROVOLATILE[fn.volatility.value]
    return out


def decide_default_volatility(default: DefaultInfo, snapshot: LiveSnapshot) -> Volatility:
    """The default's volatility with live facts applied.

    Anything the static analysis already decided is returned unchanged — a
    live fact can never override a statically known volatility. An UNKNOWN
    is re-evaluated with the snapshot's ``pg_proc.provolatile`` facts and
    stays UNKNOWN when they do not cover every unknown function (or the
    expression's uncertainty is not function-shaped at all).
    """
    if default.volatility is not Volatility.UNKNOWN or default.expression is None:
        return default.volatility
    resolved = resolved_function_volatilities(snapshot)
    if not any(name in resolved for name in default.unknown_functions):
        return default.volatility
    return resolve_expression_volatility(default.expression, resolved)
