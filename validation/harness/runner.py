"""Execute corpus cases against a real Postgres and score the engine.

Per case: render the migration, parse it, run the fixture setup, arrange
any concurrent activity, capture a read-only snapshot exactly as the CLI
would, assess, then execute the file statement by statement on a separate
session while a lock monitor samples ``pg_locks`` and a traffic probe
measures what a concurrent reader and writer experience. Every statement
gets a measured record, a ground-truth label from
:mod:`validation.harness.labeling`, the engine's prediction, and an
outcome (``match`` / ``strict`` / ``lenient`` / ``unknown``).

Truth basis. The thresholds are wall-clock outage lines on the target,
so the truth is the hold the target actually experienced. Before the
engine could see hardware (snapshot format 5), the harness normalized
the measured hold into reference-machine milliseconds — the units the
constants were fitted in — because that was the only way to compare a
hardware-blind estimate with a measurement. Now the snapshot carries the
calibration probe and the engine's estimate is scaled to *this* target,
so the comparable truth is the **raw** hold, and that is what a
statement is labeled on whenever the snapshot it was assessed with had a
usable probe reading. The normalized label is still computed and
reported (``tier_normalized``) as the diagnostic it has become; a case
whose snapshot carried no probe (capture failure) falls back to it.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psycopg

from blastoise.catalog.loader import load_catalog
from blastoise.ir import StatementKind
from blastoise.live import TypeChangeProbe, capture_snapshot
from blastoise.live.introspect import LiveIntrospectionError
from blastoise.parser import parse_migration
from blastoise.verdict import assess_script, snapshot_probes
from blastoise.verdict.model import (
    CannotEstimate,
    DurationEstimate,
    StatementAssessment,
    worse_classification,
)
from validation.harness import calibration as cal
from validation.harness import fixtures as fx
from validation.harness.corpus import FIXTURE_TABLES, Case, load_corpus
from validation.harness.labeling import Label, Measured, label, outcome, strongest

PG_VERSION = 17

_DML_KINDS: frozenset[StatementKind] = frozenset(
    {
        StatementKind.INSERT,
        StatementKind.UPDATE,
        StatementKind.UPDATE_BATCHED,
        StatementKind.UPDATE_WITHOUT_WHERE,
        StatementKind.DELETE,
        StatementKind.DELETE_BATCHED,
        StatementKind.DELETE_WITHOUT_WHERE,
        StatementKind.MERGE,
    }
)

# Transaction control acquires no lock of its own; the locks pg_locks shows
# during COMMIT belong to the statements that took them and are already
# attributed there.
_TXN_CONTROL: frozenset[StatementKind] = frozenset(
    {
        StatementKind.BEGIN,
        StatementKind.COMMIT,
        StatementKind.ROLLBACK,
        StatementKind.SAVEPOINT,
        StatementKind.RELEASE_SAVEPOINT,
        StatementKind.ROLLBACK_TO_SAVEPOINT,
        StatementKind.TRANSACTION_OTHER,
    }
)

_FILE_VERDICT: dict[str, str] = {
    "safe": "proceed",
    "safe_irreversible": "proceed",
    "needs_timing": "requires_approval",
    "unknown": "requires_approval",
    "unsafe": "block",
}
_FILE_RANK: dict[str, int] = {"proceed": 0, "requires_approval": 1, "block": 2}


@dataclass
class RunOptions:
    pg_bin: Path
    out: Path
    smoke: bool = False
    only: frozenset[str] = frozenset()
    families: frozenset[str] = frozenset()
    sizes: frozenset[str] = frozenset()
    skip_probes: bool = False
    max_hold_s: int | None = None


@dataclass
class Session:
    """Everything a case needs from the running server."""

    dsn: str
    ro_dsn: str
    catalog: Any
    traffic: fx.TrafficProbe
    run_started: float  # time.perf_counter() at run start; cases stamp their offset
    size_map: dict[str, str] = field(default_factory=dict)  # corpus table -> seeded table


def _predicted(st: StatementAssessment) -> dict[str, Any]:
    deciding = st.rows[0] if st.rows else None
    for row in st.rows[1:]:
        assert deciding is not None
        if (
            worse_classification(deciding.verdict.classification, row.verdict.classification)
            is not deciding.verdict.classification
        ):
            deciding = row
    durations: list[dict[str, Any]] = []
    for row in st.rows:
        d = row.duration
        if isinstance(d, DurationEstimate):
            durations.append(
                {
                    "kind": row.kind.value,
                    "lock_mode": row.lock_mode.value,
                    "point_ms": d.point_ms,
                    "low_ms": d.low_ms,
                    "high_ms": d.high_ms,
                    "constant_key": d.constant_key,
                    "method": d.method.value,
                }
            )
        elif isinstance(d, CannotEstimate):
            durations.append(
                {"kind": row.kind.value, "lock_mode": row.lock_mode.value,
                 "cannot_estimate": d.reason[:200]}
            )
    deciding_constant = None
    deciding_high = None
    if deciding is not None and isinstance(deciding.duration, DurationEstimate):
        deciding_constant = deciding.duration.constant_key
        deciding_high = deciding.duration.high_ms
    return {
        "tier": st.verdict.classification.value,
        "band": st.verdict.band.value if st.verdict.band else None,
        "method": st.verdict.method.value,
        "refusal": st.verdict.refusal,
        "refused_from": (
            None if st.verdict.refused_from is None else st.verdict.refused_from.value
        ),
        "rationale": st.verdict.rationale[:600],
        "conditions": [c[:200] for c in st.verdict.conditions],
        "statement_lock_mode": st.statement_lock_mode.value,
        "row_kinds": [row.kind.value for row in st.rows],
        "deciding_kind": deciding.kind.value if deciding is not None else None,
        "deciding_constant_key": deciding_constant,
        "deciding_high_ms": deciding_high,
        "durations": durations,
        "narrowings": [n[:200] for row in st.rows for n in row.narrowings],
        "reversibility": st.reversibility.reversibility.value,
    }


def _file_verdict(tiers: list[str]) -> str:
    best = "proceed"
    for tier in tiers:
        candidate = _FILE_VERDICT[tier]
        if _FILE_RANK[candidate] > _FILE_RANK[best]:
            best = candidate
    return best


def _oid(conn: psycopg.Connection[Any], table: str | None) -> int | None:
    if table is None:
        return None
    row = conn.execute("SELECT %s::regclass::oid", (table,)).fetchone()
    return None if row is None else int(row[0])


def _filenode(conn: psycopg.Connection[Any], oid: int | None) -> int | None:
    """By OID, not name: a RENAME inside the file must not break the check."""
    if oid is None:
        return None
    row = conn.execute("SELECT relfilenode FROM pg_class WHERE oid = %s", (oid,)).fetchone()
    return None if row is None else int(row[0])


def _rewrote(conn: psycopg.Connection[Any], oid: int | None, before: int | None) -> bool | None:
    if oid is None or before is None:
        return None
    now = _filenode(conn, oid)
    return None if now is None else now != before


def run_case(session: Session, case: Case, opts: RunOptions) -> dict[str, Any]:
    dsn = session.dsn
    bound_table = session.size_map.get(case.table or "", case.table)
    bindings = {k: session.size_map.get(v, v) for k, v in case.bindings.items()}
    rendered = dataclasses.replace(case, table=bound_table, bindings=bindings)
    sql = rendered.sql()
    record: dict[str, Any] = {
        "case": case.id,
        "family": case.family,
        "adversarial": case.adversarial,
        "why": case.why,
        "table": bound_table,
        "rows": FIXTURE_TABLES.get(case.table or "", None) if not opts.smoke else None,
        "sql": sql,
        "mode": case.mode,
        "statements": [],
    }
    started = time.monotonic()

    with psycopg.connect(dsn, autocommit=True) as admin:
        for statement in rendered.setup:
            admin.execute(rendered.render(statement))

    preexisting = fx.preexisting_relations(dsn)
    record["preexisting_count"] = len(preexisting)

    holder: fx.IdleHolder | None = None
    waiter: fx.VisibleWaiter | None = None
    if case.holder is not None:
        hold_s = case.holder.hold_s
        if opts.max_hold_s is not None:
            hold_s = min(hold_s, opts.max_hold_s)
        holder = fx.IdleHolder(dsn, rendered.render(case.holder.sql), hold_s)
        holder.start()
        holder.ready.wait(10)
        pre_age = 0 if opts.smoke else case.holder.pre_age_s
        if pre_age:
            time.sleep(pre_age)
        record["holder"] = {
            "sql": rendered.render(case.holder.sql),
            "hold_s": hold_s,
            "pre_age_s": pre_age,
            "pid": holder.pid,
            "error": holder.error,
        }
        if case.holder.visible_waiter and bound_table is not None:
            waiter = fx.VisibleWaiter(dsn, bound_table)
            waiter.start()
            time.sleep(0.2)
            record["holder"]["visible_waiter_parked"] = fx.wait_for_waiter(dsn, waiter.pid)

    script = parse_migration(sql if sql.rstrip().endswith(";") else sql + ";\n")
    probes = snapshot_probes(script)
    session.traffic.pause()
    time.sleep(0.1)
    snapshot = None
    try:
        snapshot = capture_snapshot(
            session.ro_dsn,
            probes.relations,
            functions=probes.functions,
            types=probes.types,
            type_changes=[
                TypeChangeProbe(relation=r, column=c, new_type=ty)
                for r, c, ty in probes.type_changes
            ],
            connect_timeout_s=5,
            statement_timeout_ms=8000,
            lock_timeout_ms=2000,
        )
    except (LiveIntrospectionError, psycopg.Error) as exc:
        record["capture_error"] = str(exc)[:200]
    assessment = assess_script(script, session.catalog, PG_VERSION, snapshot)
    record["snapshot_captured"] = snapshot is not None
    if snapshot is not None:
        lt = snapshot.concurrency.long_transactions
        record["snapshot_long_transactions"] = (
            None if not lt.available or lt.value is None
            else [
                {"pid": t.pid, "idle_in_transaction": t.idle_in_transaction,
                 "xact_age_ms": t.xact_age_ms}
                for t in lt.value
            ]
        )
        lw = snapshot.concurrency.lock_waiters
        record["snapshot_lock_waiters"] = (
            None if not lw.available or lw.value is None else len(lw.value)
        )
        cb = snapshot.calibration
        record["snapshot_calibration"] = {
            "compute_ms": cb.compute_ms.value if cb.compute_ms.available else None,
            "compute_reason": None if cb.compute_ms.available else cb.compute_ms.reason,
        }

    session.traffic.target(bound_table if case.traffic_probe else None)
    session.traffic.resume()

    apply_conn = psycopg.connect(dsn, autocommit=(case.mode == "autocommit"))
    pid_row = apply_conn.execute("SELECT pg_backend_pid()").fetchone()
    apply_pid = int(pid_row[0]) if pid_row else 0
    apply_conn.execute("SET TIME ZONE 'UTC'")
    apply_conn.execute("SET statement_timeout = '1800s'")
    for statement in rendered.session:
        apply_conn.execute(rendered.render(statement))
    if case.mode == "rollback":
        apply_conn.commit()  # settings stick; the file's own transaction starts now

    table_oid = _oid(apply_conn, bound_table)
    filenode_before = _filenode(apply_conn, table_oid)
    aborted = False
    statement_records: list[dict[str, Any]] = []
    for index, parsed in enumerate(script.statements):
        st = assessment.statements[index]
        predicted = _predicted(st)
        expect = case.expect[index] if index < len(case.expect) else case.expect[-1]
        srec: dict[str, Any] = {
            "index": index,
            "sql": parsed.sql[:300],
            "kind": parsed.kind.value,
            "predicted": predicted,
            "expected": {
                "tier": expect.tier, "lock": expect.lock, "rewrites": expect.rewrites,
                "error": expect.error, "irreversible": expect.irreversible,
                "note": expect.note,
            },
        }
        if aborted:
            srec["measured"] = {"not_executed": "transaction aborted by an earlier error"}
            srec["truth"] = None
            srec["outcome"] = "not_executed"
            statement_records.append(srec)
            continue
        session.traffic.target(bound_table if case.traffic_probe else None)
        monitor = fx.LockMonitor(dsn, apply_pid, preexisting)
        monitor.start()
        error: str | None = None
        rowcount: int | None = None
        if holder is not None and index == 0:
            holder.start_timer.set()
        t0 = time.perf_counter()
        try:
            cur = apply_conn.cursor()
            cur.execute(parsed.sql)
            wall_ms = int((time.perf_counter() - t0) * 1000)
            rowcount = cur.rowcount if cur.rowcount >= 0 else None
            final = monitor.final_sample()
            rewrote = _rewrote(apply_conn, table_oid, filenode_before)
        except psycopg.Error as exc:
            wall_ms = int((time.perf_counter() - t0) * 1000)
            error = str(exc).split("\n")[0][:200]
            final = []
            rewrote = None
            if case.mode == "rollback":
                aborted = True
        finally:
            monitor.stop()
        # Lock wait: time until the first granted lock on a pre-existing
        # relation was sampled; if the grant was only ever seen by the final
        # sample (or never, because the statement errored while waiting),
        # the last not-granted sample bounds it from below. Under 100 ms is
        # sampling noise from the traffic probe's own millisecond locks.
        wait_ms = 0
        if monitor.saw_waiting:
            if monitor.first_granted_at is not None:
                observed_wait = int((monitor.first_granted_at - t0) * 1000)
            elif monitor.last_waiting_at is not None:
                observed_wait = int((monitor.last_waiting_at - t0) * 1000)
            else:
                observed_wait = 0
            if observed_wait > 100:
                wait_ms = min(observed_wait, wall_ms)
        modes = monitor.modes_on(preexisting, final)
        if parsed.kind in _TXN_CONTROL:
            modes = {}
        strongest_mode = strongest([m for ms in modes.values() for m in ms])
        is_dml = parsed.kind in _DML_KINDS
        read_stall, write_stall = session.traffic.stalls()
        measured = {
            "wall_ms": wall_ms,
            "wait_ms": wait_ms,
            "work_ms": max(0, wall_ms - wait_ms),
            "rowcount": rowcount,
            "error": error,
            "rewrote": rewrote,
            "modes_on_preexisting": modes,
            "strongest_preexisting_mode": strongest_mode,
            "traffic": {
                "max_read_stall_ms": read_stall,
                "max_write_stall_ms": write_stall,
                "errors": session.traffic.errors,
            } if case.traffic_probe else None,
            "is_dml": is_dml,
            "at_s": round(t0 - session.run_started, 1),
        }
        srec["measured"] = measured
        srec["truth_inputs"] = {
            "irreversible": expect.irreversible,
            "is_dml": is_dml,
        }
        statement_records.append(srec)
        if rewrote:
            filenode_before = _filenode(apply_conn, table_oid)

    if case.mode == "rollback":
        with contextlib.suppress(psycopg.Error):
            apply_conn.rollback()
    apply_conn.close()
    # A reader parked behind the file's locks returns only now; give it a
    # beat and fold what it saw into the last executed statement.
    if case.traffic_probe and statement_records:
        time.sleep(0.15)
        read_stall, write_stall = session.traffic.stalls()
        for srec in reversed(statement_records):
            tr = (srec.get("measured") or {}).get("traffic")
            if tr is not None:
                tr["max_read_stall_ms"] = max(tr["max_read_stall_ms"], read_stall)
                tr["max_write_stall_ms"] = max(tr["max_write_stall_ms"], write_stall)
                break
    session.traffic.target(None)

    if holder is not None:
        holder.release.set()
        holder.join(timeout=30)
    if waiter is not None:
        waiter.join(timeout=30)
        record["holder"]["visible_waiter_error"] = waiter.error

    with psycopg.connect(dsn, autocommit=True) as admin:
        for statement in rendered.teardown:
            try:
                admin.execute(rendered.render(statement))
            except psycopg.Error as exc:
                record.setdefault("teardown_errors", []).append(str(exc).split("\n")[0][:200])
        if case.vacuum_after and bound_table is not None:
            v0 = time.monotonic()
            admin.execute(f"VACUUM (ANALYZE) {bound_table}")
            record["vacuum_after_s"] = int(time.monotonic() - v0)

    record["statements"] = statement_records
    record["elapsed_s"] = round(time.monotonic() - started, 1)
    return record


def _probe_scaled(record: dict[str, Any]) -> bool:
    """Did the engine's estimate for this case carry a usable hardware probe?"""
    cb = record.get("snapshot_calibration") or {}
    return cb.get("compute_ms") is not None


def score_statement(
    srec: dict[str, Any],
    factor: float,
    per_family: dict[str, float] | None = None,
    *,
    probe_scaled: bool = False,
) -> None:
    """Attach truth labels (raw, normalized, per-family) and the outcome.

    ``probe_scaled`` selects the truth basis: the raw hold when the
    engine's estimate was scaled to this target by the snapshot's
    calibration probe, the reference-normalized hold otherwise.
    """
    measured = srec.get("measured") or {}
    if "not_executed" in measured:
        return
    expected = srec["expected"]
    error = measured["error"] is not None
    inputs = srec["truth_inputs"]
    labels: dict[str, Label] = {}
    variants = {"normalized": factor, "raw": 1.0}
    if per_family:
        key = srec["predicted"].get("deciding_constant_key")
        family = cal.FAMILY_OF_CONSTANT.get(key or "")
        variants["per_family"] = (
            1.0 if family == "constant_op" else per_family.get(family or "", factor)
        )
    for name, f in variants.items():
        hold = cal.normalize(measured["work_ms"], measured["wait_ms"], f)
        labels[name] = label(
            Measured(
                error=error,
                strongest_preexisting_mode=measured["strongest_preexisting_mode"],
                hold_ms=hold,
                is_dml=inputs["is_dml"],
                rows_touched=measured["rowcount"],
                irreversible=inputs["irreversible"],
            )
        )
    truth = labels["raw"] if probe_scaled else labels["normalized"]
    srec["truth"] = {
        "tier": truth.tier,
        "block": truth.block,
        "basis": truth.basis,
        "truth_basis": "raw (engine estimate probe-scaled to this target)"
        if probe_scaled else "normalized to the reference machine (no probe)",
        "hold_ms": measured["work_ms"] + measured["wait_ms"] if probe_scaled
        else cal.normalize(measured["work_ms"], measured["wait_ms"], factor),
        "hold_ms_raw": measured["work_ms"] + measured["wait_ms"],
        "hold_ms_normalized": cal.normalize(measured["work_ms"], measured["wait_ms"], factor),
        "factor": round(factor, 3),
        "thresholds_ms": truth.thresholds_ms,
        "boundary_proximity": (
            None if truth.boundary_proximity is None else round(truth.boundary_proximity, 3)
        ),
        "tier_raw": labels["raw"].tier,
        "tier_normalized": labels["normalized"].tier,
        "tier_per_family": labels["per_family"].tier if "per_family" in labels else None,
    }
    srec["outcome"] = outcome(srec["predicted"]["tier"], truth.tier)
    mismatches: list[str] = []
    if expected["tier"] != truth.tier:
        mismatches.append(f"tier: expected {expected['tier']}, measured {truth.tier}")
    # Lock modes are sampled at 25 ms; an errored statement may release its
    # lock before the first sample, and an autocommit statement shorter than
    # one sample interval is unsampled by construction. Neither disagrees
    # with the expectation.
    unsampled = measured["strongest_preexisting_mode"] is None and (
        error or measured["wall_ms"] < 30
    )
    if expected["lock"] != measured["strongest_preexisting_mode"] and not unsampled:
        mismatches.append(
            f"lock: expected {expected['lock']}, measured {measured['strongest_preexisting_mode']}"
        )
    if measured["rewrote"] is not None and expected["rewrites"] != measured["rewrote"]:
        mismatches.append(
            f"rewrites: expected {expected['rewrites']}, measured {measured['rewrote']}"
        )
    if expected["error"] != error:
        mismatches.append(f"error: expected {expected['error']}, measured {error}")
    srec["label_mismatches"] = mismatches


def score_file(record: dict[str, Any]) -> None:
    executed = [s for s in record["statements"] if s.get("truth")]
    if not executed:
        record["file"] = None
        return
    predicted = _file_verdict([s["predicted"]["tier"] for s in record["statements"]])
    truth = _file_verdict([s["truth"]["tier"] for s in executed])
    record["file"] = {
        "predicted": predicted,
        "truth": truth,
        "match": predicted == truth,
        "direction": (
            "match" if predicted == truth
            else "lenient" if _FILE_RANK[predicted] < _FILE_RANK[truth]
            else "strict"
        ),
    }


def run(opts: RunOptions) -> dict[str, Any]:
    cases = load_corpus()
    if opts.only:
        cases = tuple(c for c in cases if c.id in opts.only)
    if opts.families:
        cases = tuple(c for c in cases if c.family in opts.families)
    wanted_sizes = dict(FIXTURE_TABLES)
    size_map: dict[str, str] = {}
    if opts.smoke:
        wanted_sizes = {"t_1k": 1_000, "t_20k": 20_000}
        size_map = {"t_100k": "t_20k", "t_1m": "t_20k", "t_5m": "t_20k"}
    elif opts.sizes:
        wanted_sizes = {k: v for k, v in wanted_sizes.items() if k in opts.sizes}
        cases = tuple(c for c in cases if c.table is None or c.table in wanted_sizes)
    # Always seed the probe sizes when probes run.
    if not opts.skip_probes and not opts.smoke:
        wanted_sizes.setdefault("t_100k", FIXTURE_TABLES["t_100k"])
        wanted_sizes.setdefault("t_1m", FIXTURE_TABLES["t_1m"])
        wanted_sizes = {k: wanted_sizes[k] for k in FIXTURE_TABLES if k in wanted_sizes}

    catalog = load_catalog()
    admin_dsn, stop, _work = fx.start_server(opts.pg_bin)
    started = time.monotonic()
    results: dict[str, Any] = {
        "harness": "validation",
        "pg_version": PG_VERSION,
        "smoke": opts.smoke,
        "sizes": wanted_sizes,
        "cases_total": len(cases),
        "calibration": {},
        "results": [],
    }
    out = opts.out

    def flush() -> None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(results, indent=1), encoding="utf8")

    try:
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            admin.execute(f"CREATE ROLE {fx.RO_ROLE} LOGIN PASSWORD '{fx.RO_PASSWORD}'")
            admin.execute(f"GRANT pg_monitor TO {fx.RO_ROLE}")
        print("seeding...", flush=True)
        results["heap_bytes"] = fx.seed(admin_dsn, "validate", wanted_sizes)
        dsn = admin_dsn.rsplit("/", 1)[0] + "/validate"
        ro_dsn = dsn.replace("postgres@", f"{fx.RO_ROLE}:{fx.RO_PASSWORD}@")

        probes_on = not opts.skip_probes and not opts.smoke
        run_started = time.perf_counter()
        passes: list[dict[str, Any]] = []
        results["calibration"]["passes"] = passes

        def probe(label: str, *, light: bool) -> None:
            at_s = time.perf_counter() - run_started
            print(f"calibration probe ({label}, {'light' if light else 'full'})...", flush=True)
            c = cal.run_probes(
                dsn, label=label, sizes_present=set(wanted_sizes), log=True, light=light, at_s=at_s
            )
            passes.append(c.to_json())
            if not light:
                results["calibration"][label] = c.to_json()
            print(
                f"  machine factor vs reference: {c.global_factor:.2f}x "
                f"(vs secondary reference: {c.global_factor_secondary})",
                flush=True,
            )
            flush()

        if probes_on:
            print("settling after seed (checkpoint + warm-up)...", flush=True)
            fx.settle(dsn)
            cal.run_probes(dsn, label="warmup", sizes_present=set(wanted_sizes), light=True)
            probe("start", light=False)

        traffic = fx.TrafficProbe(dsn)
        traffic.start()
        session = Session(
            dsn=dsn, ro_dsn=ro_dsn, catalog=catalog, traffic=traffic,
            run_started=run_started, size_map=size_map,
        )
        for i, case in enumerate(cases, 1):
            if probes_on and i > 1 and (i - 1) % cal.PROBE_EVERY == 0:
                traffic.pause()
                probe(f"mid{(i - 1) // cal.PROBE_EVERY}", light=True)
            try:
                record = run_case(session, case, opts)
            except Exception as exc:
                record = {
                    "case": case.id, "family": case.family, "adversarial": case.adversarial,
                    "harness_error": f"{type(exc).__name__}: {str(exc)[:300]}",
                    "statements": [],
                }
                traffic.target(None)
            results["results"].append(record)
            summary = " ".join(
                f"{s['predicted']['tier']}/{(s.get('measured') or {}).get('wall_ms')}ms"
                for s in record["statements"]
            )
            print(
                f"[{time.monotonic() - started:7.1f}s] {i}/{len(cases)} {case.id}: "
                f"{summary or record.get('harness_error')}",
                flush=True,
            )
            flush()
        traffic.stop()
        traffic.join(timeout=5)

        if probes_on:
            traffic.pause()
            probe("end", light=False)
        results["elapsed_s"] = int(time.monotonic() - started)
        flush()
    finally:
        stop()
    return results


def score(results: dict[str, Any]) -> dict[str, Any]:
    """Label every executed statement and every file. Idempotent.

    Each statement's factor is interpolated over the probe passes by the
    time it ran; per-family factors (a diagnostic) come from the full
    start/end passes averaged.
    """
    calib = results.get("calibration") or {}
    passes: list[dict[str, Any]] = list(calib.get("passes") or [])
    if not passes:
        passes = [p for p in (calib.get("start"), calib.get("end")) if p]
    per_family: dict[str, float] = {}
    fulls = [p for p in passes if p.get("label") in ("start", "end")]
    for p in fulls:
        for k, v in p["per_family"].items():
            per_family[k] = per_family.get(k, 0.0) + float(v) / len(fulls)
    factors: list[float] = []
    probe_scaled_cases = 0
    for record in results["results"]:
        scaled = _probe_scaled(record)
        probe_scaled_cases += int(scaled)
        for srec in record.get("statements", []):
            measured = srec.get("measured") or {}
            at_s = float(measured.get("at_s", 0.0))
            factor = cal.factor_at(passes, at_s) if passes else 1.0
            factors.append(factor)
            score_statement(srec, factor, per_family, probe_scaled=scaled)
        if record.get("statements"):
            score_file(record)
    results["truth_basis"] = {
        "probe_scaled_cases": probe_scaled_cases,
        "cases": len(results["results"]),
        "rule": "raw hold where the snapshot carried a usable calibration probe; "
        "reference-normalized hold otherwise",
    }
    results["factor_used"] = round(statistics.median(factors), 3) if factors else 1.0
    results["factor_range"] = (
        [round(min(factors), 3), round(max(factors), 3)] if factors else [1.0, 1.0]
    )
    return results
