"""Does this ALTER COLUMN TYPE rewrite the table? A cited lookup.

The lock catalog's ``alter_column_type`` row is worst-case (rewrite) with
``requires_live_context`` naming the column's current type as the missing
fact. This module supplies the judgment once a :class:`TypeChangeFacts` has
been captured: a pure function over the live facts plus a static table of
the typmod rules Postgres implements in type-specific support functions.

The lookup is keyed on the (current → new) type pair. Cross-type binary
coercibility comes from the live ``pg_cast.castmethod`` fact — pg_cast on
the *connected* server, so extension-provided casts (citext, ...) are
covered without a hardcoded pair list. The same-type typmod rules
(varchar length growth, numeric precision growth, timestamp precision) are
static entries here, each cited; they cannot be read from pg_cast because
they live in per-type prosupport functions.

Every rule was additionally verified against a real server (PG 17.10) by
comparing ``pg_class.relfilenode`` before and after the ALTER: a rewrite
allocates a new relfilenode. Where the documentation is more conservative
than the implementation (unconstrained target domains, constrained source
domains reducing to their base), the verified implementation behavior wins
and the note says so.

Honest limits: a ``USING`` expression is judged REWRITE even though a
provably-no-op ``USING c`` avoids one (not statically provable here);
``interval`` precision changes are judged REWRITE although
``interval_support`` can avoid some (the typmod also encodes a range mask,
not modeled); and the timestamp↔timestamptz verdict is conditional on the
*migration session's* TimeZone, which this tool cannot know — the server's
configured TimeZone travels in ``ServerFacts.timezone`` as evidence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum, auto

from pgverdict.live.model import TypeChangeFacts

# Citations used by more than one rule.
_DOC_ALTERTABLE = (
    'doc:18/sql-altertable.html Notes: "Changing the type of an existing column '
    "will normally cause the entire table and its indexes to be rewritten. As an "
    "exception, when changing the type of an existing column, if the USING clause "
    "does not change the column contents and the old type is either binary "
    "coercible to the new type or an unconstrained domain over the new type, a "
    'table rewrite is not needed."'
)
_SRC_REQUIRES_REWRITE = (
    "src:REL_18_STABLE/src/backend/commands/tablecmds.c ATColumnChangeRequiresRewrite: "
    "a transform that is a Var under RelabelType/unconstrained-CoerceToDomain nodes "
    "needs no rewrite; any surviving function call forces one"
)
_SRC_TIMESTAMP_UTC = (
    'release:12 "Allow ALTER TABLE ... SET DATA TYPE changing between timestamp and '
    'timestamptz to avoid a table rewrite when the session time zone is UTC" '
    "(commit 3c59263, src/backend/utils/adt/timestamp.c "
    "TimestampTimestampTzRequiresRewrite, called from ATColumnChangeRequiresRewrite); "
    "verified live on 17.10: no relfilenode change under TimeZone=UTC, rewrite under "
    "America/New_York"
)


class RewriteVerdict(StrEnum):
    """Whether the ALTER COLUMN TYPE rewrites the table."""

    NO_REWRITE = auto()
    REWRITE = auto()
    # Rewrite is skipped iff the session running the migration has
    # TimeZone=UTC (PG 12+). The tool cannot know that session's setting;
    # ServerFacts.timezone is the configured default, as evidence.
    NO_REWRITE_IF_SESSION_TZ_UTC = auto()
    UNKNOWN = auto()


@dataclass(frozen=True, slots=True)
class TypeChangeAssessment:
    verdict: RewriteVerdict
    reason: str
    source: str  # citation for the rule that decided


@dataclass(frozen=True, slots=True)
class _GrowthRule:
    """Same-base-type typmod rule: when is the new typmod non-lossy?

    ``effective_default`` is what an absent typmod means for comparison
    purposes: None = truly unbounded (varchar — an explicit limit on a
    previously unbounded column always re-checks), or a number (timestamp —
    bare means precision 6, so ``timestamp -> timestamp(6)`` is a no-op).
    """

    effective_default: int | None
    scale_sensitive: bool  # numeric: scale must match exactly
    source: str


_GROWTH_RULES: dict[str, _GrowthRule] = {
    "varchar": _GrowthRule(
        effective_default=None,
        scale_sensitive=False,
        source=(
            "src:REL_18_STABLE/src/backend/utils/adt/varchar.c varchar_support "
            "(prosupport of the varchar length-coercion function, verified in "
            "pg_proc on 17.10): the length coercion simplifies to a no-op when "
            "the new limit is unspecified or not smaller than the old; verified "
            "live: varchar(10)->varchar(20) keeps the relfilenode, "
            "varchar(20)->varchar(10) and varchar->varchar(10) do not"
        ),
    ),
    "varbit": _GrowthRule(
        effective_default=None,
        scale_sensitive=False,
        source=(
            "src:REL_18_STABLE/src/backend/utils/adt/varbit.c varbit_support; "
            "verified live: varbit(8)->varbit(16) and varbit(8)->varbit keep the "
            "relfilenode"
        ),
    ),
    "numeric": _GrowthRule(
        effective_default=None,
        scale_sensitive=True,
        source=(
            "src:REL_18_STABLE/src/backend/utils/adt/numeric.c numeric_support: "
            "no-op when the target is unconstrained, or the scale is unchanged "
            "and the precision does not shrink; verified live: "
            "numeric(10,2)->numeric(12,2) keeps the relfilenode, "
            "numeric(10,2)->numeric(12,3) and numeric->numeric(10,2) do not"
        ),
    ),
    "timestamp": _GrowthRule(
        effective_default=6,
        scale_sensitive=False,
        source=(
            "src:REL_18_STABLE/src/backend/utils/adt/timestamp.c timestamp_support "
            "(prosupport of both the timestamp and timestamptz precision "
            "coercions, verified in pg_proc on 17.10): no-op when the new "
            "precision is unspecified or not smaller; a bare timestamp means the "
            "maximum precision 6, so timestamp->timestamp(6) is also a no-op — "
            "verified live"
        ),
    ),
    "timestamptz": _GrowthRule(
        effective_default=6,
        scale_sensitive=False,
        source=(
            "src:REL_18_STABLE/src/backend/utils/adt/timestamp.c timestamp_support "
            "(shared with timestamp; see the timestamp entry)"
        ),
    ),
    "time": _GrowthRule(
        effective_default=6,
        scale_sensitive=False,
        source=(
            "src:REL_18_STABLE/src/backend/utils/adt/date.c time_support "
            "(prosupport of both the time and timetz precision coercions, "
            "verified in pg_proc on 17.10); verified live: time(3)->time(6) "
            "keeps the relfilenode"
        ),
    ),
    "timetz": _GrowthRule(
        effective_default=6,
        scale_sensitive=False,
        source=(
            "src:REL_18_STABLE/src/backend/utils/adt/date.c time_support "
            "(shared with time; see the time entry)"
        ),
    ),
    # Deliberately absent: bpchar (padding changes with the length: char(10)->
    # char(20) rewrites, verified live), bit (fixed length), and interval
    # (interval_support exists but the typmod also encodes a range mask —
    # not modeled, so interval changes stay worst-case REWRITE).
}

# Canonical spellings -> growth-rule keys. Both format_type() output
# ("character varying(20)") and migration spellings ("varchar(20)") land here.
_TYPE_ALIASES: dict[str, str] = {
    "varchar": "varchar",
    "character varying": "varchar",
    "char varying": "varchar",
    "numeric": "numeric",
    "decimal": "numeric",
    "dec": "numeric",
    "varbit": "varbit",
    "bit varying": "varbit",
    "timestamp": "timestamp",
    "timestamp without time zone": "timestamp",
    "timestamptz": "timestamptz",
    "timestamp with time zone": "timestamptz",
    "time": "time",
    "time without time zone": "time",
    "timetz": "timetz",
    "time with time zone": "timetz",
    "char": "bpchar",
    "character": "bpchar",
    "bpchar": "bpchar",
    "bit": "bit",
    "interval": "interval",
}

_TYPMOD_RE = re.compile(r"\(\s*(\d+)\s*(?:,\s*(\d+)\s*)?\)")
_TYPE_NAME_RE = re.compile(r"[a-z_][a-z0-9_ ]*")


def _parse_type_text(text: str) -> tuple[str, tuple[int, ...] | None] | None:
    """(canonical or raw lowercased name, typmod params) — or None if odd.

    Handles both suffix typmods ("character varying(20)") and infix ones
    ("timestamp(3) with time zone"): the parenthesized typmod is cut out
    wherever it sits, and what remains is the name. Quoted and array
    spellings return None; unknown plain names pass through raw, which the
    caller treats as not-in-the-rules-table — the conservative path.
    """
    lowered = " ".join(text.lower().split())
    if lowered.startswith("pg_catalog."):
        lowered = lowered[len("pg_catalog."):]
    if '"' in lowered or "[" in lowered:
        return None
    params: tuple[int, ...] | None
    match = _TYPMOD_RE.search(lowered)
    if match is None:
        if "(" in lowered or ")" in lowered:
            return None
        params = None
        name = lowered
    else:
        p, s_ = match.groups()
        params = (int(p),) if s_ is None else (int(p), int(s_))
        name = " ".join((lowered[: match.start()] + " " + lowered[match.end():]).split())
    if _TYPE_NAME_RE.fullmatch(name) is None:
        return None
    return _TYPE_ALIASES.get(name, name), params


def _growth_allows(
    rule: _GrowthRule, cur: tuple[int, ...] | None, new: tuple[int, ...] | None
) -> bool:
    """Is the typmod change non-lossy under the type's support-function rule?"""
    if new is None:
        return True  # dropping the constraint never checks anything
    if rule.scale_sensitive:
        cur_scale = (cur[1] if len(cur) > 1 else 0) if cur is not None else None
        new_scale = new[1] if len(new) > 1 else 0
        if cur is None or cur_scale != new_scale:
            return False
        return new[0] >= cur[0]
    if cur is None:
        if rule.effective_default is None:
            return False  # previously unbounded; a new limit re-checks
        return new[0] >= rule.effective_default
    return new[0] >= cur[0]


def assess_type_change(
    facts: TypeChangeFacts,
    *,
    pg_major: int,
    has_using_expression: bool = False,
) -> TypeChangeAssessment:
    """Judge one captured from→to pair. Pure; no connection required.

    ``pg_major`` is the server the migration will run on (usually
    ``ServerFacts.pg_major``); ``has_using_expression`` comes from the IR.
    Unknown stays unknown: any missing fact or unparsable type spelling
    yields UNKNOWN with the reason, never a guess.
    """
    if has_using_expression:
        return TypeChangeAssessment(
            verdict=RewriteVerdict.REWRITE,
            reason=(
                "the statement has a USING expression; whether it changes the "
                "column contents is not statically provable, so the worst case "
                "stands"
            ),
            source=_DOC_ALTERTABLE,
        )
    for fact in (
        facts.current_type,
        facts.new_type,
        facts.same_type,
        facts.bases_same,
        facts.cast_method,
        facts.new_is_domain,
    ):
        if not fact.available:
            return TypeChangeAssessment(
                verdict=RewriteVerdict.UNKNOWN,
                reason=f"live fact missing: {fact.reason}",
                source=_SRC_REQUIRES_REWRITE,
            )

    cur_parsed = _parse_type_text(str(facts.current_type.value))
    new_parsed = _parse_type_text(facts.new_type_requested)
    if cur_parsed is None or new_parsed is None:
        odd = facts.current_type.value if cur_parsed is None else facts.new_type_requested
        return TypeChangeAssessment(
            verdict=RewriteVerdict.UNKNOWN,
            reason=f"type spelling not understood: {odd!r}",
            source=_SRC_REQUIRES_REWRITE,
        )
    cur_name, cur_params = cur_parsed
    new_name, new_params = new_parsed

    # A target domain with constraints re-checks every row; an unconstrained
    # one is skipped by ATColumnChangeRequiresRewrite (verified live: base ->
    # unconstrained domain keeps the relfilenode — the docs' exception list
    # is narrower than the implementation).
    if facts.new_is_domain.value:
        constraints = int(facts.new_domain_constraint_count.value or 0)
        not_null = bool(facts.new_domain_not_null.value)
        if constraints > 0 or not_null:
            return TypeChangeAssessment(
                verdict=RewriteVerdict.REWRITE,
                reason=(
                    "the target type is a domain with "
                    f"{constraints} CHECK constraint(s)"
                    f"{' and NOT NULL' if not_null else ''}; every row is checked"
                ),
                source=_SRC_REQUIRES_REWRITE
                + "; CoerceToDomain forces a rewrite iff DomainHasConstraints",
            )
        if bool(facts.new_domain_has_typmod.value):
            return TypeChangeAssessment(
                verdict=RewriteVerdict.REWRITE,
                reason=(
                    "the target domain carries a typmod on its base type; the "
                    "coercion applies it to every row (not modeled further)"
                ),
                source=_SRC_REQUIRES_REWRITE,
            )
        new_params = None  # a bare domain name carries no written typmod

    # timestamp <-> timestamptz: binary compatible iff the migration
    # session's TimeZone is UTC (PG 12+). Checked before the castmethod
    # branch — their pg_cast rows are castmethod 'f'.
    if {cur_name, new_name} == {"timestamp", "timestamptz"} and cur_name != new_name:
        if pg_major < 12:
            return TypeChangeAssessment(
                verdict=RewriteVerdict.REWRITE,
                reason="timestamp<->timestamptz always rewrites before PG 12",
                source=_SRC_TIMESTAMP_UTC,
            )
        rule = _GROWTH_RULES["timestamp"]
        if _growth_allows(rule, cur_params, new_params):
            return TypeChangeAssessment(
                verdict=RewriteVerdict.NO_REWRITE_IF_SESSION_TZ_UTC,
                reason=(
                    "binary compatible when the migration session runs with "
                    "TimeZone=UTC; any other zone rewrites (the server's "
                    "configured TimeZone is in ServerFacts.timezone as evidence, "
                    "but the migration session decides)"
                ),
                source=_SRC_TIMESTAMP_UTC,
            )
        return TypeChangeAssessment(
            verdict=RewriteVerdict.REWRITE,
            reason="the precision also shrinks, which re-checks every row",
            source=_SRC_TIMESTAMP_UTC,
        )

    if facts.bases_same.value:
        # Same underlying type. Domains on either side reduce to relabels
        # (verified live: CHECK/NOT NULL source domains dropping to their
        # base keep the relfilenode — constraints are dropped, not checked).
        if new_params is None:
            return TypeChangeAssessment(
                verdict=RewriteVerdict.NO_REWRITE,
                reason=(
                    "same underlying type and the target carries no typmod: "
                    "nothing is re-checked"
                ),
                source=_SRC_REQUIRES_REWRITE,
            )
        if facts.current_is_domain.value or facts.new_is_domain.value:
            return TypeChangeAssessment(
                verdict=RewriteVerdict.REWRITE,
                reason=(
                    "a domain is involved and the target carries a typmod; the "
                    "combination is not modeled, so the worst case stands"
                ),
                source=_SRC_REQUIRES_REWRITE,
            )
        if cur_params == new_params:
            return TypeChangeAssessment(
                verdict=RewriteVerdict.NO_REWRITE,
                reason="the type and typmod are unchanged; the transform is a no-op",
                source=_SRC_REQUIRES_REWRITE,
            )
        growth = _GROWTH_RULES.get(cur_name)
        if growth is not None and _growth_allows(growth, cur_params, new_params):
            return TypeChangeAssessment(
                verdict=RewriteVerdict.NO_REWRITE,
                reason=(
                    f"{cur_name}: the typmod change is non-lossy, so the "
                    "coercion simplifies to a no-op"
                ),
                source=growth.source,
            )
        return TypeChangeAssessment(
            verdict=RewriteVerdict.REWRITE,
            reason=(
                f"{cur_name}: the typmod change is (or cannot be proven) "
                "non-lossy; every row is re-checked"
            ),
            source=growth.source if growth is not None else _SRC_REQUIRES_REWRITE,
        )

    # Different underlying types: the live pg_cast fact decides.
    if facts.cast_method.value == "b":
        if new_params is None:
            return TypeChangeAssessment(
                verdict=RewriteVerdict.NO_REWRITE,
                reason=(
                    "binary coercible (pg_cast.castmethod = 'b' on the connected "
                    "server) and the target carries no typmod: a pure relabel"
                ),
                source=_DOC_ALTERTABLE
                + "; doc:18/catalog-pg-cast.html: castmethod 'b' means "
                "'the types are binary-coercible, thus no conversion is required'; "
                "verified live: varchar(10)->text, cidr->inet, xml->text keep the "
                "relfilenode",
            )
        return TypeChangeAssessment(
            verdict=RewriteVerdict.REWRITE,
            reason=(
                "binary coercible, but the target's typmod applies a length/"
                "precision coercion to every row (verified live: "
                "text->varchar(10) rewrites)"
            ),
            source=_DOC_ALTERTABLE,
        )
    if facts.cast_method.value is None:
        return TypeChangeAssessment(
            verdict=RewriteVerdict.REWRITE,
            reason=(
                "no cast exists between the types on the connected server; the "
                "statement needs a USING expression, which rewrites"
            ),
            source=_DOC_ALTERTABLE,
        )
    how = "a conversion function" if facts.cast_method.value == "f" else "I/O conversion"
    return TypeChangeAssessment(
        verdict=RewriteVerdict.REWRITE,
        reason=(
            f"the cast uses {how} (pg_cast.castmethod = "
            f"'{facts.cast_method.value}'); every row is converted (verified "
            "live: integer->bigint rewrites)"
        ),
        source=_DOC_ALTERTABLE,
    )
