"""Diff two machine-surface dumps and report every difference.

The package name itself is expected to differ; everything the dumps
capture is normalized to ``<pkg>`` so a rename shows up as *nothing*.
Anything that remains is a machine-readable field the rename moved, which
is the thing the naming principle forbids.

Usage: python diff_machine_surface.py <before.json> <after.json>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

before = json.loads(Path(sys.argv[1]).read_text(encoding="utf8"))
after = json.loads(Path(sys.argv[2]).read_text(encoding="utf8"))


def normalize(dump: dict) -> dict:
    """Blank the one field that is run-dependent, not surface-dependent.

    The CLI payload echoes the input file's path, and each dump writes its
    fixture into a fresh temporary directory. The *key* ``path`` is the
    surface under test; its value is noise.
    """
    payload = dump.get("cli", {}).get("json_payload")
    if isinstance(payload, list):
        for script in payload:
            if isinstance(script, dict) and "path" in script:
                script["path"] = "<fixture>"
    return dump


before = normalize(before)
after = normalize(after)

# Python API additions are allowed and reported separately: adding an
# export breaks nobody, and PRESSURE_LEVELS is the sanctioned bridge from
# a machine value to a display name. Removals are not allowed.
additions: list[str] = []
for module in sorted(set(before.get("exports", {})) | set(after.get("exports", {}))):
    old_names = set(before.get("exports", {}).get(module, []))
    new_names = set(after.get("exports", {}).get(module, []))
    for name in sorted(new_names - old_names):
        additions.append(f"+ exports.{module}.{name}")
    for name in sorted(old_names - new_names):
        additions.append(f"! REMOVED exports.{module}.{name}")
before.pop("exports", None)
after.pop("exports", None)

differences: list[str] = []


def compare(path: str, left: object, right: object) -> None:
    if isinstance(left, dict) and isinstance(right, dict):
        for key in sorted(set(left) | set(right)):
            if key not in left:
                differences.append(f"+ {path}.{key} = {right[key]!r}")
            elif key not in right:
                differences.append(f"- {path}.{key} = {left[key]!r}")
            else:
                compare(f"{path}.{key}", left[key], right[key])
        return
    if isinstance(left, list) and isinstance(right, list):
        if len(left) != len(right):
            differences.append(f"~ {path}: length {len(left)} -> {len(right)}")
            return
        for index, (a, b) in enumerate(zip(left, right, strict=True)):
            compare(f"{path}[{index}]", a, b)
        return
    if left != right:
        differences.append(f"~ {path}: {left!r} -> {right!r}")


for section in sorted(set(before) | set(after)):
    compare(section, before.get(section), after.get(section))

for line in differences:
    print(line)
print("")
print("Python API changes (additive is fine, REMOVED is not):")
for line in additions or ["  (none)"]:
    print(f"  {line}")
removals = [line for line in additions if line.startswith("!")]
print("")
print(f"machine-readable differences: {len(differences)}")
print(f"API removals: {len(removals)}")
sys.exit(1 if differences or removals else 0)
