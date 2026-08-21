"""Reconstruct the pre-restructure engine exactly, by reversing every edit.

Each pair below is (new_text, old_text): the exact inverse of an edit
applied to src/pgverdict this session. Every reversal asserts it matched
exactly once, so the reconstruction is exact rather than approximate --
which is what makes the old-vs-new per-statement diff evidence rather
than argument.

The reconstructed tree keeps the new enum members (unused by the old code
paths) and re-adds CONDITIONALLY_SAFE with the old combine ordering, so
the baseline engine emits precisely the tiers it emitted before.
"""

from __future__ import annotations

from pathlib import Path

BASE = Path(__file__).parent / "baseline" / "pgverdict"


def revert(rel: str, pairs: list[tuple[str, str]]) -> None:
    path = BASE / rel
    src = path.read_text(encoding="utf8")
    for index, (new, old) in enumerate(pairs):
        assert src.count(new) == 1, f"{rel}[{index}]: matched {src.count(new)}"
        src = src.replace(new, old)
    path.write_text(src, encoding="utf8")
    print(f"  reverted {rel}: {len(pairs)} edits")


revert(
    "verdict/model.py",
    [
        (
            "    SAFE = auto()\n"
            "    SAFE_IRREVERSIBLE = auto()\n"
            "    NEEDS_TIMING = auto()\n"
            "    UNSAFE = auto()\n"
            "    UNKNOWN = auto()\n",
            "    SAFE = auto()\n"
            "    SAFE_IRREVERSIBLE = auto()\n"
            "    NEEDS_TIMING = auto()\n"
            "    CONDITIONALLY_SAFE = auto()\n"
            "    UNSAFE = auto()\n"
            "    UNKNOWN = auto()\n",
        ),
        (
            "_COMBINE_RANK: dict[Classification, int] = {\n"
            "    Classification.SAFE: 0,\n"
            "    Classification.SAFE_IRREVERSIBLE: 1,\n"
            "    Classification.NEEDS_TIMING: 2,\n"
            "    Classification.UNKNOWN: 3,\n"
            "    Classification.UNSAFE: 4,\n"
            "}\n",
            "_COMBINE_RANK: dict[Classification, int] = {\n"
            "    Classification.SAFE: 0,\n"
            "    Classification.SAFE_IRREVERSIBLE: 1,\n"
            "    Classification.CONDITIONALLY_SAFE: 1,\n"
            "    Classification.NEEDS_TIMING: 1,\n"
            "    Classification.UNKNOWN: 2,\n"
            "    Classification.UNSAFE: 3,\n"
            "}\n",
        ),
    ],
)

ENGINE_PAIRS: list[tuple[str, str]] = [
    # 1. duration band ladder
    (
        "    if high_ms < long:\n        return Classification.NEEDS_TIMING",
        "    if high_ms < long:\n        return Classification.CONDITIONALLY_SAFE",
    ),
    # 2. _proportional_verdict middle band
    (
        "    if cls is Classification.NEEDS_TIMING:\n"
        "        return Verdict(\n"
        "            classification=cls,\n"
        "            method=method,\n",
        "    if cls is Classification.CONDITIONALLY_SAFE:\n"
        "        return Verdict(\n"
        "            classification=cls,\n"
        "            method=method,\n",
    ),
    # 3. brief ACCESS EXCLUSIVE
    (
        "                classification=Classification.NEEDS_TIMING,\n"
        "                method=method,\n",
        "                classification=Classification.CONDITIONALLY_SAFE,\n"
        "                method=method,\n",
    ),
    # 4. CTAS / CREATE MATVIEW
    (
        "            classification=Classification.NEEDS_TIMING,\n"
        "            method=Method.PROVEN,\n"
        "            rationale=(\n"
        '                f"{what} creates a new object (blocking nothing) but reads its "',
        "            classification=Classification.CONDITIONALLY_SAFE,\n"
        "            method=Method.PROVEN,\n"
        "            rationale=(\n"
        '                f"{what} creates a new object (blocking nothing) but reads its "',
    ),
    # 5. INSERT ... SELECT
    (
        "            classification=Classification.NEEDS_TIMING,\n"
        "            method=Method.PROVEN,\n"
        "            rationale=(\n"
        '                "INSERT ... SELECT blocks no reads or writes, but its volume "',
        "            classification=Classification.CONDITIONALLY_SAFE,\n"
        "            method=Method.PROVEN,\n"
        "            rationale=(\n"
        '                "INSERT ... SELECT blocks no reads or writes, but its volume "',
    ),
    # 6. conditional-branch cap
    (
        "    if conditions_extra and verdict.classification in (\n"
        "        Classification.NEEDS_TIMING,\n"
        "        Classification.UNSAFE,\n"
        "    ):\n"
        "        verdict = Verdict(\n"
        "            classification=Classification.NEEDS_TIMING,",
        "    if conditions_extra and verdict.classification in (\n"
        "        Classification.CONDITIONALLY_SAFE,\n"
        "        Classification.UNSAFE,\n"
        "    ):\n"
        "        verdict = Verdict(\n"
        "            classification=Classification.CONDITIONALLY_SAFE,",
    ),
    # 7. matched DML
    (
        "    return Verdict(\n"
        "        classification=Classification.NEEDS_TIMING,\n"
        "        method=weakest_method(Method.PROVEN, worst.method),",
        "    return Verdict(\n"
        "        classification=Classification.CONDITIONALLY_SAFE,\n"
        "        method=weakest_method(Method.PROVEN, worst.method),",
    ),
    # 8. all-rows DML middle band
    (
        "    if cls is Classification.NEEDS_TIMING:\n"
        "        return Verdict(\n"
        "            classification=cls,\n"
        "            method=duration.method,\n",
        "    if cls is Classification.CONDITIONALLY_SAFE:\n"
        "        return Verdict(\n"
        "            classification=cls,\n"
        "            method=duration.method,\n",
    ),
    # 9. contention escalation
    (
        "    escalated = (\n"
        "        Classification.NEEDS_TIMING\n"
        "        if verdict.classification in SAFE_TIERS\n"
        "        else Classification.UNSAFE\n"
        "    )",
        "    escalated = (\n"
        "        Classification.CONDITIONALLY_SAFE\n"
        "        if verdict.classification is Classification.SAFE\n"
        "        else Classification.UNSAFE\n"
        "    )",
    ),
    # 10a. guard cap, UNSAFE side
    (
        "    if verdict.classification is Classification.UNSAFE:\n"
        "        return Verdict(\n"
        "            classification=Classification.NEEDS_TIMING,",
        "    if verdict.classification is Classification.UNSAFE:\n"
        "        return Verdict(\n"
        "            classification=Classification.CONDITIONALLY_SAFE,",
    ),
    # 10b. guard cap, other side
    (
        "    return Verdict(\n"
        "        classification=Classification.NEEDS_TIMING,\n"
        "        method=verdict.method,\n"
        "        rationale=verdict.rationale,\n"
        "        conditions=(guard, *verdict.conditions),",
        "    return Verdict(\n"
        "        classification=Classification.CONDITIONALLY_SAFE,\n"
        "        method=verdict.method,\n"
        "        rationale=verdict.rationale,\n"
        "        conditions=(guard, *verdict.conditions),",
    ),
    # 12. held-lock escalation (done before 11: 11 rewrites nearby text)
    (
        "            if long_running and combined.classification in SAFE_TIERS:\n"
        "                combined = Verdict(\n"
        "                    classification=Classification.NEEDS_TIMING,",
        "            if long_running and combined.classification is Classification.SAFE:\n"
        "                combined = Verdict(\n"
        "                    classification=Classification.CONDITIONALLY_SAFE,",
    ),
    (
        '                        f"transaction keeps holding {held_desc}",\n'
        "                        *combined.conditions,\n"
        "                    ),",
        '                        f"transaction keeps holding {held_desc}",\n'
        "                    ),",
    ),
]

# 11. the irreversibility floor, including its file-local exemption.
NEW_FLOOR = (
    "        if (\n"
    "            combined.classification is Classification.SAFE\n"
    "            and reversibility.reversibility is Reversibility.IRREVERSIBLE\n"
    "        ):\n"
    "            combined = Verdict(\n"
    "                classification=Classification.SAFE_IRREVERSIBLE,\n"
)
OLD_FLOOR = (
    "        file_local_safe = combined.classification is Classification.SAFE and any(\n"
    '            "this file itself creates" in row.verdict.rationale for row in rows\n'
    "        )\n"
    "        if (\n"
    "            combined.classification is Classification.SAFE\n"
    "            and reversibility.reversibility is Reversibility.IRREVERSIBLE\n"
    "            and not file_local_safe\n"
    "        ):\n"
    "            combined = Verdict(\n"
    "                classification=Classification.CONDITIONALLY_SAFE,\n"
)
ENGINE_PAIRS.append((NEW_FLOOR, OLD_FLOOR))
ENGINE_PAIRS.append(
    (
        '                    "it; there is no undo",',
        '                    "this is intended",',
    )
)

revert("verdict/engine.py", ENGINE_PAIRS)

revert(
    "verdict/constants.py",
    [
        (
            '        _m(\n'
            '            "dml_update",\n'
            "            ConstantUnit.ROWS_PER_SECOND,\n"
            "            22_000,",
            '        _c(\n'
            '            "dml_update",\n'
            "            ConstantUnit.ROWS_PER_SECOND,\n"
            "            50_000,",
        ),
    ],
)

print("baseline reconstructed at", BASE)
