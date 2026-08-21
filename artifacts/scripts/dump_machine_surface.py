"""Dump every machine-readable surface of the package to a JSON file.

Run before and after the rename and diff the two: any difference is a
machine-readable field that moved, which the rename is not allowed to do.
Covers enum member values, public dataclass field names and their order,
the canonical snapshot serialization, the CLI's --json payload keys, the
CLI exit codes, the catalog YAML's field vocabulary, and the duration
constant keys.

Usage: python dump_machine_surface.py <package_name> <out.json> [tests_dir]
"""

from __future__ import annotations

import dataclasses
import enum
import importlib
import inspect
import json
import pkgutil
import subprocess
import sys
import tempfile
from pathlib import Path

PKG = sys.argv[1]
OUT = Path(sys.argv[2])

root = importlib.import_module(PKG)

modules = [PKG]
for info in pkgutil.walk_packages(root.__path__, prefix=f"{PKG}."):
    modules.append(info.name)

enums: dict[str, dict[str, str]] = {}
dataclass_fields: dict[str, list[str]] = {}
constants: dict[str, object] = {}

for name in sorted(modules):
    try:
        module = importlib.import_module(name)
    except ImportError as exc:  # optional deps
        constants[f"<unimportable>{name}"] = str(exc)
        continue
    for attr, value in vars(module).items():
        if attr.startswith("_"):
            continue
        if isinstance(value, type) and issubclass(value, enum.Enum):
            if value.__module__.startswith(PKG):
                enums[f"{value.__module__}.{value.__qualname__}".replace(PKG, "<pkg>", 1)] = {
                    member.name: str(member.value) for member in value
                }
        elif (
            isinstance(value, type)
            and dataclasses.is_dataclass(value)
            and value.__module__.startswith(PKG)
        ):
            key = f"{value.__module__}.{value.__qualname__}".replace(PKG, "<pkg>", 1)
            dataclass_fields[key] = [f.name for f in dataclasses.fields(value)]

# Public API surface: the names each package __init__ exports.
exports: dict[str, list[str]] = {}
for name in sorted(modules):
    try:
        module = importlib.import_module(name)
    except ImportError:
        continue
    if hasattr(module, "__all__"):
        exports[name.replace(PKG, "<pkg>", 1)] = sorted(module.__all__)

# Duration constant keys (prompt 8 reads these by name).
duration = importlib.import_module(f"{PKG}.verdict.constants")
constants["duration_constant_keys"] = sorted(duration.DURATION_CONSTANTS)
constants["duration_constant_units"] = {
    key: str(value.unit) for key, value in sorted(duration.DURATION_CONSTANTS.items())
}
constants["snapshot_format"] = importlib.import_module(f"{PKG}.live.model").SNAPSHOT_FORMAT

# The catalog's own field vocabulary, as the loader validates it.
catalog_mod = importlib.import_module(f"{PKG}.catalog.loader")
yaml_path = Path(inspect.getfile(catalog_mod)).parent / "lock_catalog.yaml"
catalog = importlib.import_module(f"{PKG}.catalog").load_catalog()
entry_keys: set[str] = set()
count = 0
for table in (catalog.statements, catalog.alter_table_actions):
    for entries in table.values():
        for entry in entries:
            entry_keys.update(f.name for f in dataclasses.fields(entry))
            count += 1
constants["catalog_entry_fields"] = sorted(entry_keys)
constants["catalog_entry_count"] = count
constants["catalog_conflict_matrix"] = {
    str(mode): sorted(str(m) for m in conflicts)
    for mode, conflicts in sorted(catalog.conflict_matrix.items(), key=lambda kv: str(kv[0]))
}
constants["catalog_statement_resolution"] = {
    str(kind): str(res)
    for kind, res in sorted(catalog.statement_resolution.items(), key=lambda kv: str(kv[0]))
}

# The canonical snapshot serialization, on a fully-populated fake snapshot.
sys.path.insert(0, sys.argv[3] if len(sys.argv) > 3 else "tests")
snapshot_json = None
try:
    import verdict_helpers  # type: ignore[import-not-found]

    snap = verdict_helpers.snapshot(
        relations=(verdict_helpers.relation("users", rows=1000),),
    )
    snapshot_json = json.loads(snap.to_canonical_json())
except Exception as exc:  # noqa: BLE001
    snapshot_json = {"<error>": str(exc)}

# The CLI: --json payload shape and the exit codes of both error paths.
cli: dict[str, object] = {}
with tempfile.TemporaryDirectory() as tmp:
    sql = Path(tmp) / "m.sql"
    sql.write_text(
        "BEGIN;\n"
        "CREATE TABLE t (id int);\n"
        "ALTER TABLE users ADD COLUMN e text DEFAULT 'x';\n"
        "COMMIT;\n",
        encoding="utf8",
    )
    run = subprocess.run(
        [sys.executable, "-m", f"{PKG}.cli", "parse", "--json", str(sql)],
        capture_output=True,
        text=True,
    )
    cli["json_exit_code"] = run.returncode
    try:
        payload = json.loads(run.stdout)
        cli["json_payload"] = payload
    except json.JSONDecodeError as exc:
        cli["json_payload"] = {"<error>": str(exc), "stderr": run.stderr[-400:]}

    bad = Path(tmp) / "bad.sql"
    bad.write_text("SELECT ((;\n", encoding="utf8")
    cli["parse_error_exit_code"] = subprocess.run(
        [sys.executable, "-m", f"{PKG}.cli", "parse", str(bad)],
        capture_output=True,
        text=True,
    ).returncode
    cli["missing_file_exit_code"] = subprocess.run(
        [sys.executable, "-m", f"{PKG}.cli", "parse", str(Path(tmp) / "nope.sql")],
        capture_output=True,
        text=True,
    ).returncode

report = {
    "enums": enums,
    "dataclass_fields": dataclass_fields,
    "exports": exports,
    "constants": constants,
    "snapshot_canonical_json": snapshot_json,
    "cli": cli,
}
OUT.write_text(json.dumps(report, indent=1, sort_keys=True), encoding="utf8")
print(f"enums={len(enums)} dataclasses={len(dataclass_fields)} -> {OUT}")
