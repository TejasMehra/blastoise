"""The validation corpus: YAML cases, loaded and validated.

A case is a migration file, a database fixture (which seeded table it runs
against, extra setup, session settings, concurrent activity), and a
hand-written *expectation* — the tier, lock, rewrite, error and
irreversibility the author believes the statement will show. The
expectation is not the ground truth. Ground truth is what the runner
measures; the expectation is cross-checked against it and every
disagreement is reported, so a case whose fixture did not produce the
scenario its author intended is visible instead of silently scored.

Files live in ``validation/corpus/*.yaml``, one family per file, each a
mapping with a ``family`` key and a ``cases`` list.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

CORPUS_DIR = Path(__file__).resolve().parent.parent / "corpus"

FAMILIES: tuple[str, ...] = (
    "index_creation",
    "foreign_keys",
    "add_column",
    "alter_column_type",
    "constraints",
    "dml_backfills",
    "drops_irreversible",
    "common_benign",
    "concurrency",
    "transactions",
)

ADVERSARIAL: tuple[str, ...] = ("looks_dangerous", "looks_benign", "judgment_call")

TIERS: tuple[str, ...] = ("safe", "safe_irreversible", "needs_timing", "unsafe", "unknown")

LOCK_MODES: tuple[str, ...] = (
    "ACCESS SHARE",
    "ROW SHARE",
    "ROW EXCLUSIVE",
    "SHARE UPDATE EXCLUSIVE",
    "SHARE",
    "SHARE ROW EXCLUSIVE",
    "EXCLUSIVE",
    "ACCESS EXCLUSIVE",
)

# Seeded fixture tables the corpus may bind ``{t}`` to. Sizes are rows.
FIXTURE_TABLES: dict[str, int] = {
    "t_1k": 1_000,
    "t_100k": 100_000,
    "t_1m": 1_000_000,
    "t_5m": 5_000_000,
}


class CorpusError(ValueError):
    """A corpus file is malformed."""


@dataclass(frozen=True, slots=True)
class Expectation:
    """What the author expects one statement to show when it runs."""

    tier: str
    lock: str | None  # strongest lock on a pre-existing relation, or None
    rewrites: bool
    error: bool
    irreversible: bool
    note: str = ""


@dataclass(frozen=True, slots=True)
class Holder:
    """Concurrent activity: a session that runs ``sql`` then idles in its transaction.

    ``visible_waiter`` additionally parks a third session behind the holder
    (requesting ACCESS EXCLUSIVE) so ``pg_locks`` shows a waiter — the only
    shape the engine's contention escalation can see.
    """

    sql: str
    hold_s: int
    visible_waiter: bool = False
    pre_age_s: int = 0  # idle this long before the snapshot is captured


@dataclass(frozen=True, slots=True)
class Case:
    id: str
    family: str
    migration: str
    expect: tuple[Expectation, ...]
    table: str | None  # fixture table bound to {t}, None for cases that create their own
    why: str = ""
    adversarial: str | None = None
    source: str | None = None
    setup: tuple[str, ...] = ()
    teardown: tuple[str, ...] = ()
    session: tuple[str, ...] = ()
    mode: str = "rollback"  # rollback | autocommit
    holder: Holder | None = None
    vacuum_after: bool = False
    traffic_probe: bool = True
    bindings: dict[str, str] = field(default_factory=dict)

    def sql(self) -> str:
        return self.migration.format(**self._format_map())

    def render(self, text: str) -> str:
        return text.format(**self._format_map())

    def _format_map(self) -> dict[str, str]:
        out = dict(self.bindings)
        if self.table is not None:
            out.setdefault("t", self.table)
        return out


def _require(mapping: dict[str, Any], key: str, where: str) -> Any:
    if key not in mapping:
        raise CorpusError(f"{where}: missing required key '{key}'")
    return mapping[key]


def _expectation(raw: dict[str, Any], where: str) -> Expectation:
    tier = str(_require(raw, "tier", where))
    if tier not in TIERS:
        raise CorpusError(f"{where}: tier '{tier}' is not one of {TIERS}")
    lock = raw.get("lock")
    if lock is not None and lock not in LOCK_MODES:
        raise CorpusError(f"{where}: lock '{lock}' is not a Postgres lock mode")
    return Expectation(
        tier=tier,
        lock=None if lock is None else str(lock),
        rewrites=bool(raw.get("rewrites", False)),
        error=bool(raw.get("error", False)),
        irreversible=bool(raw.get("irreversible", False)),
        note=str(raw.get("note", "")),
    )


def _strings(raw: Any, where: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, str):
        return (raw,)
    if not isinstance(raw, list) or not all(isinstance(s, str) for s in raw):
        raise CorpusError(f"{where}: expected a string or list of strings")
    return tuple(raw)


def _case(raw: dict[str, Any], family: str, where: str) -> Case:
    case_id = str(_require(raw, "id", where))
    where = f"{where} [{case_id}]"
    migration = str(_require(raw, "migration", where))
    expect_raw = _require(raw, "expect", where)
    expect: tuple[Expectation, ...]
    if isinstance(expect_raw, dict):
        expect = (_expectation(expect_raw, where),)
    elif isinstance(expect_raw, list):
        expect = tuple(_expectation(e, f"{where} expect[{i}]") for i, e in enumerate(expect_raw))
    else:
        raise CorpusError(f"{where}: expect must be a mapping or a list of mappings")
    if not expect:
        raise CorpusError(f"{where}: expect is empty")
    table = raw.get("table")
    if table is not None and table not in FIXTURE_TABLES:
        raise CorpusError(f"{where}: table '{table}' is not a fixture table")
    adversarial = raw.get("adversarial")
    if adversarial is not None and adversarial not in ADVERSARIAL:
        raise CorpusError(f"{where}: adversarial '{adversarial}' not in {ADVERSARIAL}")
    mode = str(raw.get("mode", "rollback"))
    if mode not in ("rollback", "autocommit"):
        raise CorpusError(f"{where}: mode must be rollback or autocommit")
    holder = None
    if raw.get("holder") is not None:
        h = raw["holder"]
        holder = Holder(
            sql=str(_require(h, "sql", f"{where} holder")),
            hold_s=int(_require(h, "hold_s", f"{where} holder")),
            visible_waiter=bool(h.get("visible_waiter", False)),
            pre_age_s=int(h.get("pre_age_s", 0)),
        )
    bindings = raw.get("bindings") or {}
    if not isinstance(bindings, dict):
        raise CorpusError(f"{where}: bindings must be a mapping")
    return Case(
        id=case_id,
        family=family,
        migration=migration,
        expect=expect,
        table=None if table is None else str(table),
        why=str(raw.get("why", "")),
        adversarial=None if adversarial is None else str(adversarial),
        source=None if raw.get("source") is None else str(raw["source"]),
        setup=_strings(raw.get("setup"), f"{where} setup"),
        teardown=_strings(raw.get("teardown"), f"{where} teardown"),
        session=_strings(raw.get("session"), f"{where} session"),
        mode=mode,
        holder=holder,
        vacuum_after=bool(raw.get("vacuum_after", False)),
        traffic_probe=bool(raw.get("traffic_probe", True)),
        bindings={str(k): str(v) for k, v in bindings.items()},
    )


def load_corpus(directory: Path = CORPUS_DIR) -> tuple[Case, ...]:
    """Load every ``*.yaml`` under ``directory``; ids must be unique."""
    cases: list[Case] = []
    seen: set[str] = set()
    for path in sorted(directory.glob("*.yaml")):
        with path.open(encoding="utf8") as fh:
            doc = yaml.safe_load(fh)
        if not isinstance(doc, dict):
            raise CorpusError(f"{path.name}: top level must be a mapping")
        family = str(_require(doc, "family", path.name))
        if family not in FAMILIES:
            raise CorpusError(f"{path.name}: family '{family}' not in {FAMILIES}")
        raw_cases = _require(doc, "cases", path.name)
        if not isinstance(raw_cases, list):
            raise CorpusError(f"{path.name}: cases must be a list")
        for i, raw in enumerate(raw_cases):
            if not isinstance(raw, dict):
                raise CorpusError(f"{path.name} cases[{i}]: must be a mapping")
            case = _case(raw, family, f"{path.name} cases[{i}]")
            if case.id in seen:
                raise CorpusError(f"{path.name}: duplicate case id '{case.id}'")
            seen.add(case.id)
            cases.append(case)
    return tuple(cases)
