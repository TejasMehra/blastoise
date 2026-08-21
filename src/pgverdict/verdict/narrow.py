"""Apply live snapshot facts to the catalog's worst-case rows.

The catalog's convention is worst-case-with-``requires_live_context``: a
row models the worst behavior and names the fact that would narrow it.
This module supplies exactly those narrowings, one function per action
kind, each consuming captured facts and returning a :class:`NarrowResult`:

* ``CONSTANT`` — the live facts prove the fast path (no scan, no rewrite);
* ``PROPORTIONAL`` — the scan/rewrite definitely happens (or could not be
  ruled out and the worst case stands *resolved*, e.g. no proving CHECK
  exists — a definite answer, not a missing one);
* ``KEEP_MODEL`` — the fact is missing but the catalog row already models
  the expected path (rows the catalog deliberately modeled best-case);
* ``UNRESOLVED`` — the snapshot could not supply the declared fact. The
  engine maps this to UNKNOWN: a worst case without its context is not a
  verdict.

No narrower guesses: a missing fact is UNRESOLVED with the reason.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum, auto

from pgverdict.catalog.model import LockMode
from pgverdict.ir import AlterTableAction, AlterTableActionKind, ParsedStatement, Volatility
from pgverdict.live.model import (
    ConstraintFacts,
    IndexFacts,
    LiveSnapshot,
    RelationFacts,
    TypeChangeFacts,
    TypeFacts,
)
from pgverdict.live.resolve import decide_default_volatility
from pgverdict.live.typechange import RewriteVerdict, assess_type_change
from pgverdict.verdict.model import Method
from pgverdict.verdict.probes import probe_name

_AK = AlterTableActionKind


class NarrowOutcome(StrEnum):
    CONSTANT = auto()
    PROPORTIONAL = auto()
    KEEP_MODEL = auto()
    UNRESOLVED = auto()


@dataclass(frozen=True, slots=True)
class NarrowResult:
    method: Method
    outcome: NarrowOutcome
    narrative: str
    reason: str | None = None  # for UNRESOLVED
    conditions: tuple[str, ...] = ()  # e.g. the session-TimeZone condition
    notes: tuple[str, ...] = ()
    extra_relation: tuple[str, LockMode] | None = None  # FK referenced table
    family_override: str | None = None  # throughput family replacing the kind's default
    relabel: bool = False  # ALTER COLUMN TYPE proved a pure relabel (reversible)
    rebuild_indexes: tuple[tuple[str, str], ...] = ()  # (index name, throughput family)


def relation_facts_for(snapshot: LiveSnapshot, requested: str) -> RelationFacts | None:
    for relation in snapshot.relations:
        if relation.requested == requested:
            return relation
    return None


def _type_facts_for(snapshot: LiveSnapshot, requested: str) -> TypeFacts | None:
    for entry in snapshot.types:
        if entry.requested == requested:
            return entry
    return None


def _typechange_facts_for(
    snapshot: LiveSnapshot, relation: str, column: str, new_type: str
) -> TypeChangeFacts | None:
    for entry in snapshot.type_changes:
        if (
            entry.relation == relation
            and entry.column == column
            and entry.new_type_requested == new_type
        ):
            return entry
    return None


def _constraints_of(
    facts: RelationFacts | None,
) -> tuple[ConstraintFacts, ...] | str:
    """The relation's constraints, or the reason they are unavailable."""
    if facts is None:
        return "relation not captured in the snapshot"
    if not facts.exists.available:
        return f"existence unknown: {facts.exists.reason}"
    if facts.exists.value is False:
        return "relation does not exist on the target database"
    if not facts.constraints.available or facts.constraints.value is None:
        return facts.constraints.reason or "constraints not gathered"
    return facts.constraints.value


_IS_NOT_NULL_RE = re.compile(r'^"?(?P<name>[^"]+)"?\s+IS\s+NOT\s+NULL$', re.IGNORECASE)


def _strip_outer_parens(text: str) -> str:
    text = text.strip()
    while text.startswith("(") and text.endswith(")"):
        depth = 0
        balanced = True
        for index, char in enumerate(text):
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0 and index != len(text) - 1:
                    balanced = False
                    break
        if not balanced:
            return text
        text = text[1:-1].strip()
    return text


def _split_top_level_and(text: str) -> tuple[str, ...]:
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    tokens = re.split(r"(\s+AND\s+)", text, flags=re.IGNORECASE)
    for token in tokens:
        if re.fullmatch(r"\s+AND\s+", token, flags=re.IGNORECASE) and depth == 0:
            parts.append("".join(current))
            current = []
            continue
        depth += token.count("(") - token.count(")")
        current.append(token)
    parts.append("".join(current))
    return tuple(part.strip() for part in parts if part.strip())


def check_expression_proves_not_null(expression: str, column: str) -> bool:
    """Does a CHECK expression syntactically prove ``column IS NOT NULL``?

    Handles the forms ``pg_get_constraintdef`` deparses: optional outer
    parens and a top-level AND list. Deliberately narrower than Postgres'
    own ``NotNullImpliedByRelConstraints`` implication proof — a miss errs
    toward "the scan runs", never the other way.
    """
    stripped = _strip_outer_parens(expression)
    for conjunct in _split_top_level_and(stripped):
        match = _IS_NOT_NULL_RE.fullmatch(_strip_outer_parens(conjunct))
        if match and match.group("name").strip('"') == column.strip('"'):
            return True
    return False


def _narrow_add_column_domain(
    action: AlterTableAction, snapshot: LiveSnapshot
) -> NarrowResult:
    """The ADD COLUMN fast-path question: is the column's type a constrained domain?"""
    if action.column_type is None:
        return NarrowResult(
            method=Method.UNVERIFIED,
            outcome=NarrowOutcome.UNRESOLVED,
            narrative="",
            reason="the IR records no column type for the added column",
        )
    facts = _type_facts_for(snapshot, action.column_type)
    if facts is None:
        return NarrowResult(
            method=Method.UNVERIFIED,
            outcome=NarrowOutcome.UNRESOLVED,
            narrative="",
            reason=f"type facts for {action.column_type!r} were not captured",
        )
    if not facts.is_domain.available:
        return NarrowResult(
            method=Method.UNVERIFIED,
            outcome=NarrowOutcome.UNRESOLVED,
            narrative="",
            reason=f"whether {action.column_type!r} is a domain is unknown: "
            f"{facts.is_domain.reason}",
        )
    if facts.is_domain.value is False:
        return NarrowResult(
            method=Method.OBSERVED,
            outcome=NarrowOutcome.CONSTANT,
            narrative=(
                f"live: type {action.column_type!r} is not a domain — the fast "
                "default path applies (value stored in attmissingval, no rewrite)"
            ),
        )
    constraints = facts.domain_constraint_count
    not_null = facts.domain_not_null
    if not constraints.available or not not_null.available:
        return NarrowResult(
            method=Method.UNVERIFIED,
            outcome=NarrowOutcome.UNRESOLVED,
            narrative="",
            reason=f"{action.column_type!r} is a domain but its constraints could "
            "not be gathered",
        )
    if (constraints.value or 0) > 0 or bool(not_null.value):
        return NarrowResult(
            method=Method.OBSERVED,
            outcome=NarrowOutcome.PROPORTIONAL,
            narrative=(
                f"live: type {action.column_type!r} is a domain with "
                f"{constraints.value} CHECK constraint(s)"
                f"{' and NOT NULL' if not_null.value else ''} — the fast path is "
                "disabled and the table is rewritten"
            ),
        )
    return NarrowResult(
        method=Method.OBSERVED,
        outcome=NarrowOutcome.CONSTANT,
        narrative=(
            f"live: type {action.column_type!r} is an unconstrained domain — the "
            "fast default path applies"
        ),
    )


def _narrow_add_column_unknown_volatility(
    action: AlterTableAction, snapshot: LiveSnapshot
) -> NarrowResult:
    if action.default is None:
        return NarrowResult(
            method=Method.UNVERIFIED,
            outcome=NarrowOutcome.UNRESOLVED,
            narrative="",
            reason="the IR records no default expression for the added column",
        )
    decided = decide_default_volatility(action.default, snapshot)
    if decided is Volatility.UNKNOWN:
        missing = ", ".join(action.default.unknown_functions) or "the expression"
        return NarrowResult(
            method=Method.UNVERIFIED,
            outcome=NarrowOutcome.UNRESOLVED,
            narrative="",
            reason=f"default volatility undecided: pg_proc could not decide {missing}",
        )
    if decided is Volatility.VOLATILE:
        return NarrowResult(
            method=Method.OBSERVED,
            outcome=NarrowOutcome.PROPORTIONAL,
            narrative=(
                "live: pg_proc.provolatile decides the default VOLATILE — every "
                "existing row is rewritten with a freshly evaluated value"
            ),
        )
    domain = _narrow_add_column_domain(action, snapshot)
    prefix = (
        f"live: pg_proc.provolatile decides the default {decided.value.upper()} "
        "(fast path eligible)"
    )
    if domain.outcome is NarrowOutcome.UNRESOLVED:
        return NarrowResult(
            method=Method.UNVERIFIED,
            outcome=NarrowOutcome.UNRESOLVED,
            narrative="",
            reason=f"{prefix}, but the domain question stayed open: {domain.reason}",
        )
    return NarrowResult(
        method=Method.OBSERVED,
        outcome=domain.outcome,
        narrative=f"{prefix}; {domain.narrative}",
        conditions=domain.conditions,
        notes=domain.notes,
    )


def _rebuild_family(index: IndexFacts) -> str:
    """Which throughput constant a forced rebuild of this index draws on."""
    if index.has_expressions or index.method != "btree":
        return "index_build_expression"
    return "index_build_btree"


def _narrow_no_rewrite_with_indexes(
    action: AlterTableAction,
    relation: RelationFacts | None,
    reason: str,
    source: str,
) -> NarrowResult:
    """The no-rewrite verdict, corrected for dependent-index rebuilds.

    A type change that skips the heap rewrite still drops and recreates
    every index that ``pg_depend`` ties to the column — *unless*
    ``CheckIndexCompatible`` lets the index be reused, which it refuses
    for partial and expression indexes (measured on PG 17.10: a plain-key
    btree is reused in ~0 ms at 1M rows while the partial and expression
    forms rebuild in ~800 ms; the scale harness measured 13.5 s of the
    same rebuild at 10M under the statement's ACCESS EXCLUSIVE). A
    non-default operator class is treated as a rebuild too: reuse is only
    proven for opclasses that carry across the change.
    """
    if (
        relation is None
        or not relation.exists.available
        or relation.exists.value is False
        or not relation.indexes.available
        or relation.indexes.value is None
    ):
        why = (
            "relation facts were not captured"
            if relation is None
            else relation.indexes.reason or "index facts were not gathered"
        )
        return NarrowResult(
            method=Method.UNVERIFIED,
            outcome=NarrowOutcome.UNRESOLVED,
            narrative="",
            reason=(
                f"no table rewrite ({reason}), but the column's dependent "
                f"indexes are unknown ({why}) — a forced index rebuild under "
                "ACCESS EXCLUSIVE cannot be ruled out"
            ),
        )
    column = action.column or ""
    dependent = [
        ix for ix in relation.indexes.value if column in ix.depends_on_columns
    ]
    rebuilt = [
        ix
        for ix in dependent
        if ix.partial or ix.has_expressions or ix.method != "btree" or not ix.default_opclasses
    ]
    if not rebuilt:
        reused = (
            f"; {len(dependent)} dependent plain btree index(es) are reused "
            "(CheckIndexCompatible), not rebuilt"
            if dependent
            else "; no index depends on the column"
        )
        return NarrowResult(
            method=Method.OBSERVED,
            outcome=NarrowOutcome.CONSTANT,
            narrative=f"live: no table rewrite — {reason}{reused}",
            notes=(source,),
            relabel=True,
        )
    described: list[str] = []
    for ix in rebuilt:
        size = (
            f", {ix.size_bytes.value} bytes"
            if ix.size_bytes.available and isinstance(ix.size_bytes.value, int)
            else ""
        )
        shape = "partial" if ix.partial else ("expression" if ix.has_expressions else ix.method)
        described.append(f"{ix.name} ({shape}{size})")
    return NarrowResult(
        method=Method.OBSERVED,
        outcome=NarrowOutcome.PROPORTIONAL,
        narrative=(
            f"live: no table rewrite — {reason} — but {len(rebuilt)} dependent "
            f"index(es) on {column!r} cannot be reused and are rebuilt under "
            f"ACCESS EXCLUSIVE while the statement holds the table lock: "
            f"{', '.join(described)}"
        ),
        notes=(
            source,
            "CheckIndexCompatible reuses plain-key default-opclass btree "
            "indexes across a no-rewrite type change; partial and expression "
            "indexes are always rebuilt (verified against relfilenodes on PG "
            "17.10)",
        ),
        rebuild_indexes=tuple((ix.name, _rebuild_family(ix)) for ix in rebuilt),
        relabel=True,
    )


def _narrow_alter_column_type(
    action: AlterTableAction,
    statement: ParsedStatement,
    snapshot: LiveSnapshot,
    pg_major: int,
) -> NarrowResult:
    if not statement.targets or action.column is None or action.column_type is None:
        return NarrowResult(
            method=Method.UNVERIFIED,
            outcome=NarrowOutcome.UNRESOLVED,
            narrative="",
            reason="the IR does not name the relation, column, and new type",
        )
    facts = _typechange_facts_for(
        snapshot, probe_name(statement.targets[0]), action.column, action.column_type
    )
    if facts is None:
        return NarrowResult(
            method=Method.UNVERIFIED,
            outcome=NarrowOutcome.UNRESOLVED,
            narrative="",
            reason=(
                f"type-change facts for {statement.targets[0]}.{action.column} -> "
                f"{action.column_type!r} were not captured"
            ),
        )
    assessment = assess_type_change(
        facts, pg_major=pg_major, has_using_expression=action.has_using_expression
    )
    if assessment.verdict is RewriteVerdict.NO_REWRITE:
        return _narrow_no_rewrite_with_indexes(
            action,
            relation_facts_for(snapshot, probe_name(statement.targets[0])),
            assessment.reason,
            assessment.source,
        )
    if assessment.verdict is RewriteVerdict.NO_REWRITE_IF_SESSION_TZ_UTC:
        return NarrowResult(
            method=Method.OBSERVED,
            outcome=NarrowOutcome.PROPORTIONAL,
            narrative=f"live: conditional rewrite — {assessment.reason}",
            conditions=(
                "the migration session must run with TimeZone=UTC; under any "
                "other zone the whole table is rewritten",
            ),
            notes=(assessment.source,),
        )
    if assessment.verdict is RewriteVerdict.REWRITE:
        return NarrowResult(
            method=Method.OBSERVED,
            outcome=NarrowOutcome.PROPORTIONAL,
            narrative=f"live: the table is rewritten — {assessment.reason}",
            notes=(assessment.source,),
        )
    return NarrowResult(
        method=Method.UNVERIFIED,
        outcome=NarrowOutcome.UNRESOLVED,
        narrative="",
        reason=f"rewrite undecidable: {assessment.reason}",
    )


def _narrow_set_not_null(
    action: AlterTableAction, facts: RelationFacts | None
) -> NarrowResult:
    if action.column is None:
        return NarrowResult(
            method=Method.UNVERIFIED,
            outcome=NarrowOutcome.UNRESOLVED,
            narrative="",
            reason="the IR records no column name",
        )
    if (
        facts is not None
        and facts.columns.available
        and facts.columns.value is not None
    ):
        for column in facts.columns.value:
            if column.name == action.column and column.not_null:
                return NarrowResult(
                    method=Method.OBSERVED,
                    outcome=NarrowOutcome.CONSTANT,
                    narrative=f"live: column {action.column!r} is already NOT NULL — no-op",
                )
    constraints = _constraints_of(facts)
    if isinstance(constraints, str):
        return NarrowResult(
            method=Method.UNVERIFIED,
            outcome=NarrowOutcome.UNRESOLVED,
            narrative="",
            reason=f"constraint facts unavailable: {constraints}",
        )
    for constraint in constraints:
        proves_by_check = (
            constraint.contype == "c"
            and constraint.validated
            and constraint.check_expression is not None
            and check_expression_proves_not_null(constraint.check_expression, action.column)
        )
        proves_by_not_null_row = (
            constraint.contype == "n"
            and constraint.validated
            and action.column in constraint.columns
        )
        if proves_by_check or proves_by_not_null_row:
            return NarrowResult(
                method=Method.OBSERVED,
                outcome=NarrowOutcome.CONSTANT,
                narrative=(
                    f"live: valid constraint {constraint.name!r} already proves "
                    f"{action.column!r} NOT NULL — the verification scan is skipped "
                    "(NotNullImpliedByRelConstraints, PG 12+)"
                ),
            )
    return NarrowResult(
        method=Method.OBSERVED,
        outcome=NarrowOutcome.PROPORTIONAL,
        narrative=(
            f"live: no valid constraint proves {action.column!r} NOT NULL — the "
            "full verification scan runs under ACCESS EXCLUSIVE"
        ),
    )


def _narrow_validate_constraint(
    action: AlterTableAction, facts: RelationFacts | None
) -> NarrowResult:
    constraints = _constraints_of(facts)
    if isinstance(constraints, str):
        return NarrowResult(
            method=Method.UNVERIFIED,
            outcome=NarrowOutcome.UNRESOLVED,
            narrative="",
            reason=f"constraint facts unavailable: {constraints}",
        )
    named = next((c for c in constraints if c.name == action.constraint_name), None)
    if named is None:
        return NarrowResult(
            method=Method.UNVERIFIED,
            outcome=NarrowOutcome.UNRESOLVED,
            narrative="",
            reason=(
                f"constraint {action.constraint_name!r} not found on the relation — "
                "the statement would error"
            ),
        )
    if named.validated:
        return NarrowResult(
            method=Method.OBSERVED,
            outcome=NarrowOutcome.CONSTANT,
            narrative=f"live: constraint {named.name!r} is already validated — no-op",
        )
    if named.contype == "f":
        extra = (
            (named.referenced_table, LockMode.ROW_SHARE)
            if named.referenced_table is not None
            else None
        )
        return NarrowResult(
            method=Method.OBSERVED,
            outcome=NarrowOutcome.PROPORTIONAL,
            narrative=(
                f"live: {named.name!r} is a FOREIGN KEY — validation scans this "
                "table and takes ROW SHARE on the referenced table "
                f"({named.referenced_table or 'unresolved'})"
            ),
            extra_relation=extra,
            family_override="fk_validation",
        )
    return NarrowResult(
        method=Method.OBSERVED,
        outcome=NarrowOutcome.PROPORTIONAL,
        narrative=(
            f"live: {named.name!r} is contype {named.contype!r} — validation scans "
            "only this table"
        ),
    )


def _narrow_drop_constraint(
    action: AlterTableAction, facts: RelationFacts | None
) -> NarrowResult:
    constraints = _constraints_of(facts)
    if isinstance(constraints, str):
        return NarrowResult(
            method=Method.UNVERIFIED,
            outcome=NarrowOutcome.UNRESOLVED,
            narrative="",
            reason=f"constraint facts unavailable: {constraints}",
        )
    named = next((c for c in constraints if c.name == action.constraint_name), None)
    if named is None:
        note = (
            "constraint absent: IF EXISTS makes this a no-op"
            if action.missing_ok
            else "constraint absent: the statement would error"
        )
        return NarrowResult(
            method=Method.OBSERVED,
            outcome=NarrowOutcome.CONSTANT,
            narrative=f"live: {action.constraint_name!r} does not exist — {note}",
        )
    if named.contype == "f":
        extra = (
            (named.referenced_table, LockMode.ACCESS_EXCLUSIVE)
            if named.referenced_table is not None
            else None
        )
        return NarrowResult(
            method=Method.OBSERVED,
            outcome=NarrowOutcome.CONSTANT,
            narrative=(
                f"live: {named.name!r} is a FOREIGN KEY — dropping it also takes "
                "ACCESS EXCLUSIVE on the referenced table "
                f"({named.referenced_table or 'unresolved'}) to remove its trigger"
            ),
            extra_relation=extra,
        )
    return NarrowResult(
        method=Method.OBSERVED,
        outcome=NarrowOutcome.CONSTANT,
        narrative=(
            f"live: {named.name!r} is contype {named.contype!r} — only this table "
            "is locked"
        ),
    )


def _narrow_add_pk_using_index(
    action: AlterTableAction, facts: RelationFacts | None
) -> NarrowResult:
    if (
        facts is not None
        and facts.columns.available
        and facts.columns.value is not None
        and all(column.not_null for column in facts.columns.value)
    ):
        return NarrowResult(
            method=Method.OBSERVED,
            outcome=NarrowOutcome.CONSTANT,
            narrative=(
                "live: every column of the table is already NOT NULL — no "
                "implicit SET NOT NULL scan can occur"
            ),
        )
    return NarrowResult(
        method=Method.OBSERVED if facts is not None else Method.UNVERIFIED,
        outcome=NarrowOutcome.KEEP_MODEL,
        narrative="",
        notes=(
            "the catalog models this form as constant-time (it exists as the "
            "fast path); if an index column is still nullable, Postgres runs an "
            "implicit SET NOT NULL scan under ACCESS EXCLUSIVE — the index's "
            "columns are not captured, so this could not be ruled out",
        ),
    )


def _narrow_set_expression(
    action: AlterTableAction, facts: RelationFacts | None
) -> NarrowResult:
    if (
        action.column is not None
        and facts is not None
        and facts.columns.available
        and facts.columns.value is not None
    ):
        for column in facts.columns.value:
            if column.name == action.column:
                if column.generated == "v":
                    return NarrowResult(
                        method=Method.OBSERVED,
                        outcome=NarrowOutcome.CONSTANT,
                        narrative=(
                            f"live: column {action.column!r} is VIRTUAL generated — "
                            "catalog-only change, nothing stored"
                        ),
                    )
                if column.generated == "s":
                    return NarrowResult(
                        method=Method.OBSERVED,
                        outcome=NarrowOutcome.PROPORTIONAL,
                        narrative=(
                            f"live: column {action.column!r} is STORED generated — "
                            "every row is rewritten with the new expression"
                        ),
                    )
    return NarrowResult(
        method=Method.UNVERIFIED,
        outcome=NarrowOutcome.UNRESOLVED,
        narrative="",
        reason="whether the column is STORED or VIRTUAL could not be read",
    )


def narrow_action(
    action: AlterTableAction,
    statement: ParsedStatement,
    snapshot: LiveSnapshot,
    pg_major: int,
) -> NarrowResult | None:
    """The narrowing for one rlc ALTER TABLE action, or None if none is registered."""
    facts = (
        relation_facts_for(snapshot, probe_name(statement.targets[0]))
        if statement.targets
        else None
    )
    if action.kind is _AK.ADD_COLUMN_DEFAULT_NONVOLATILE:
        return _narrow_add_column_domain(action, snapshot)
    if action.kind is _AK.ADD_COLUMN_DEFAULT_UNKNOWN_VOLATILITY:
        return _narrow_add_column_unknown_volatility(action, snapshot)
    if action.kind is _AK.ALTER_COLUMN_TYPE:
        return _narrow_alter_column_type(action, statement, snapshot, pg_major)
    if action.kind in (_AK.SET_NOT_NULL, _AK.ADD_NOT_NULL_CONSTRAINT):
        return _narrow_set_not_null(action, facts)
    if action.kind is _AK.VALIDATE_CONSTRAINT:
        return _narrow_validate_constraint(action, facts)
    if action.kind is _AK.DROP_CONSTRAINT:
        return _narrow_drop_constraint(action, facts)
    if action.kind is _AK.ADD_PRIMARY_KEY_USING_INDEX:
        return _narrow_add_pk_using_index(action, facts)
    if action.kind is _AK.SET_EXPRESSION:
        return _narrow_set_expression(action, facts)
    # ATTACH PARTITION deliberately has no narrower: its catalog row is an
    # UNCALIBRATED stub (the form never occurs in the wild corpus), so the
    # engine's calibration rule turns it UNKNOWN before narrowing could run.
    return None
