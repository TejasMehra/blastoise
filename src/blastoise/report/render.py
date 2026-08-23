"""Human-readable rendering of a verdict document.

This is a *named surface*: the theme lives here — the Shell Report header
and the Pressure Level display names — and nowhere in the payload it
renders. Everything printed is plain ASCII (Windows consoles mangle
anything else), and every machine-relevant value appears under its machine
name with the flavour beside it, never instead of it.

Renders from the JSON payload dict, not from engine objects, so ``check``
output and ``explain <report.json>`` are guaranteed to show the same thing.
"""

from __future__ import annotations

import textwrap
from typing import Any

from blastoise.verdict.model import Classification, pressure_level

_TIER_FLAVOUR: dict[Classification, str] = {
    Classification.UNSAFE: "do not run as written",
    Classification.UNKNOWN: "not enough evidence to say",
    Classification.NEEDS_TIMING: "safe in itself, wrong at the wrong moment",
    Classification.SAFE_IRREVERSIBLE: "proceed, but there is no undo",
    Classification.SAFE: "nothing to do",
}

# Worst first: the reader scans from the top and stops at the first zero.
_TIER_ORDER = (
    Classification.UNSAFE,
    Classification.UNKNOWN,
    Classification.NEEDS_TIMING,
    Classification.SAFE_IRREVERSIBLE,
    Classification.SAFE,
)

_WIDTH = 78

# Engine prose uses typographic punctuation (em-dashes, multiplication
# signs); Windows consoles mangle anything beyond ASCII, so the rendering
# transliterates. The JSON payload is untouched — this is display only.
_ASCII_TABLE = str.maketrans(
    {
        "—": "-",  # em dash
        "\u2013": "-",  # en dash
        "\u00d7": "x",  # multiplication sign
        "≤": "<=",  # less-than or equal
        "≥": ">=",  # greater-than or equal
        "·": "*",  # middle dot
        "→": "->",  # rightwards arrow
        "\u2018": "'",  # left single quote
        "\u2019": "'",  # right single quote
        "“": '"',  # left double quote
        "”": '"',  # right double quote
        "…": "...",  # ellipsis
    }
)


def _wrap(text: str, indent: str) -> list[str]:
    return textwrap.wrap(
        text, width=_WIDTH, initial_indent=indent, subsequent_indent=indent
    ) or [indent.rstrip()]


def _statement_lines(statement: dict[str, Any], *, expanded: bool) -> list[str]:
    band = statement.get("band") or "-"
    lines = [
        f"  L{statement['line']:<5} {statement['kind']:<28} "
        f"{statement['classification']:<18} {band:<11} {statement['method']}"
    ]
    lines.extend(_wrap(str(statement["rationale"]), "         "))
    for condition in statement.get("conditions", ()):
        lines.extend(_wrap(f"condition: {condition}", "         "))
    if expanded:
        for relation in statement.get("relations", ()):
            name = relation["relation"] or "<unnamed>"
            blocks = (
                "blocks reads and writes"
                if relation["blocks_reads"]
                else "blocks writes"
                if relation["blocks_writes"]
                else "blocks neither reads nor writes"
            )
            certainty = "" if relation["certain"] else " (only if such relations exist)"
            lines.extend(
                _wrap(
                    f"lock: {relation['lock_mode']} on {name} "
                    f"[{relation['role']}] - {blocks}{certainty}",
                    "         ",
                )
            )
        for row in statement.get("rows", ()):
            duration = row.get("duration", {})
            if duration.get("type") == "estimate":
                lines.extend(
                    _wrap(
                        f"duration ({row['kind']}): {duration['band']} at worst "
                        f"[{duration['low_ms']}..{duration['high_ms']} ms, "
                        f"confidence {duration['confidence']}, "
                        f"constant {duration['constant_key']}]",
                        "         ",
                    )
                )
            elif duration.get("type") == "cannot_estimate":
                lines.extend(
                    _wrap(f"duration ({row['kind']}): cannot estimate - {duration['reason']}",
                          "         ")
                )
            for narrowing in row.get("narrowings", ()):
                lines.extend(_wrap(f"live: {narrowing}", "         "))
        rev = statement.get("reversibility", {})
        if rev:
            lost = rev.get("what_is_lost")
            detail = f" - {lost}" if lost else ""
            lines.extend(
                _wrap(f"reversibility: {rev.get('reversibility')}{detail}", "         ")
            )
        for note in statement.get("notes", ()):
            lines.extend(_wrap(f"note: {note}", "         "))
        refs = ", ".join(statement.get("evidence", ()))
        if refs:
            lines.extend(_wrap(f"evidence: {refs}", "         "))
    return lines


def render_report(payload: dict[str, Any], *, expanded: bool = False) -> str:
    """The terminal rendering: file verdict first, then the tier counts."""
    lines: list[str] = []
    verdict = str(payload["verdict"])
    lines.append("SHELL REPORT")
    lines.append("=" * len("SHELL REPORT"))
    lines.append(f"verdict: {verdict.upper()}")
    mode = "online" if payload.get("online") else "offline"
    lines.append(
        f"change {str(payload['change_id'])[:16]}  pg {payload['pg_version']}  "
        f"{mode}  evaluated {payload['evaluated_at']}"
    )
    lines.append("")

    lines.append("pressure levels")
    counts = payload.get("classification_counts", {})
    for tier in _TIER_ORDER:
        count = counts.get(str(tier), 0)
        lines.append(
            f"  {tier!s:<18} {count:>4}   "
            f"{pressure_level(tier):<16} {_TIER_FLAVOUR[tier]}"
        )
    lines.append("")

    statements = payload.get("statements", [])
    if statements:
        lines.append("statements")
        for statement in statements:
            lines.extend(_statement_lines(statement, expanded=expanded))
        lines.append("")

    irreversible = payload.get("irreversible", [])
    if irreversible:
        lines.append(f"irreversible ({len(irreversible)})")
        for entry in irreversible:
            lines.extend(
                _wrap(
                    f"L{entry['line']} {entry['kind']}: {entry['what_is_lost']}",
                    "  ",
                )
            )
        lines.append("")

    unverified = payload.get("unverified", [])
    lines.append(f"unverified ({len(unverified)})")
    for entry in unverified:
        where = f"L{entry['line']} " if entry.get("line") is not None else ""
        lines.extend(_wrap(f"[{entry['source']}] {where}{entry['reason']}", "  "))
    lines.append("")

    for warning in payload.get("transaction_warnings", ()):
        lines.extend(_wrap(f"transaction warning: {warning['description']}", "  "))
    for note in payload.get("notes", ()):
        lines.extend(_wrap(f"note: {note}", "  "))
    if payload.get("transaction_warnings") or payload.get("notes"):
        lines.append("")

    rollback = payload.get("rollback", {})
    feasible = rollback.get("feasible", "unknown")
    label = {"yes": "feasible", "no": "NOT feasible", "unknown": "undetermined"}.get(
        str(feasible), str(feasible)
    )
    lines.append(f"rollback: {label}")
    lines.extend(_wrap(str(rollback.get("basis", "")), "  "))
    for blocker in rollback.get("blockers", ()):
        lines.extend(_wrap(f"L{blocker['line']}: {blocker['reason']}", "  "))
    if expanded:
        for entry in rollback.get("undecided", ()):
            lines.extend(_wrap(f"L{entry['line']} undetermined: {entry['reason']}", "  "))
    lines.append("")

    evidence = payload.get("evidence", {})
    files = evidence.get("files", [])
    where = evidence.get("bundle_dir")
    location = f"written to {where}/" if where else "not written to disk"
    lines.append(f"evidence: {len(files)} file(s), {location}")
    if expanded:
        for entry in files:
            lines.append(f"  {entry['sha256'][:16]}  {entry['name']}  ({entry['bytes']} bytes)")
    signature = payload.get("signature")
    if signature is None:
        lines.append("signature: unsigned (Shell Seal not applied)")
    else:
        lines.append(
            f"signature: ed25519, public key {str(signature.get('public_key', ''))[:16]}..."
        )
    text = ("\n".join(lines) + "\n").translate(_ASCII_TABLE)
    # Belt and braces: anything the table does not cover renders escaped
    # rather than as console mojibake.
    return text.encode("ascii", "backslashreplace").decode("ascii")
