"""Does the extracted SQL do what the migration does?

The claim Rails support makes is that running a migration and recording the
SQL ActiveRecord emits yields statements equivalent to the migration
itself. This checks that claim the only way it can be checked: against real
migrations from real applications, by building the same pre-state twice,
running the migration on one and the extracted SQL on the other, and
comparing the two resulting schemas.

Two databases, identically prepared:

* **A** -- the pre-migration schema, then the real migration, run through
  the shipped harness. This is also what produces the extracted SQL.
* **B** -- the pre-migration schema, then the extracted SQL, applied
  statement by statement.

If extraction is faithful, A and B are the same database. Where they are
not, the difference is printed rather than summarized: a validation report
that says "97% match" without saying what the 3% was is not evidence.

Statements go in one at a time rather than as one script, because
``CREATE INDEX CONCURRENTLY`` cannot run inside the implicit transaction a
multi-statement simple query gets -- which is itself a small demonstration
that the concurrency information survived extraction.

Every failure is a result. A migration that cannot be extracted is counted
and its reason reported; it is never dropped from the denominator.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import secrets
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

import psycopg

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from blastoise.parser import MigrationParseError, parse_migration  # noqa: E402
from blastoise.rails import HARNESS  # noqa: E402
from blastoise.rails.extract import _scratch_url  # noqa: E402

APPLY_HELPER = Path(__file__).with_name("rails_apply_schema.rb")

SCHEMA_FILES = (("db/structure.sql", "sql"), ("db/schema.rb", "ruby"))

# Column types an extension brings with it. Removing only the CREATE
# EXTENSION line leaves every column of its types undefined, which fails
# later and less legibly.
EXTENSION_TYPES: dict[str, tuple[str, ...]] = {
    "vector": ("vector", "halfvec", "sparsevec"),
}

# schema.rb statements that a gem defines and that span several lines.
DSL_BLOCKS: dict[str, tuple[str, ...]] = {
    "hairtrigger": ("create_trigger(",),
}


# --------------------------------------------------------------------------
# comparing two databases
# --------------------------------------------------------------------------

# Compared as sorted tuples rather than as a text dump: a pg_dump diff is
# dominated by ordering and formatting noise, and the question here is
# whether the same objects exist with the same definitions.
_COLUMNS = """
SELECT table_name, column_name, data_type, is_nullable, column_default,
       character_maximum_length, numeric_precision, numeric_scale
FROM information_schema.columns WHERE table_schema = 'public'
ORDER BY table_name, column_name
"""

_INDEXES = """
SELECT indexname, indexdef FROM pg_indexes
WHERE schemaname = 'public' ORDER BY indexname
"""

_CONSTRAINTS = """
SELECT rel.relname, con.conname, pg_get_constraintdef(con.oid)
FROM pg_constraint con
JOIN pg_class rel ON rel.oid = con.conrelid
JOIN pg_namespace ns ON ns.oid = rel.relnamespace
WHERE ns.nspname = 'public' ORDER BY rel.relname, con.conname
"""

_SEQUENCES = """
SELECT sequence_name, data_type FROM information_schema.sequences
WHERE sequence_schema = 'public' ORDER BY sequence_name
"""


def snapshot(url: str) -> dict[str, list[tuple[object, ...]]]:
    with psycopg.connect(url) as conn, conn.cursor() as cur:
        out = {}
        for name, query in (
            ("columns", _COLUMNS),
            ("indexes", _INDEXES),
            ("constraints", _CONSTRAINTS),
            ("sequences", _SEQUENCES),
        ):
            cur.execute(query)
            out[name] = [tuple(row) for row in cur.fetchall()]
        return out


def compare(
    left: dict[str, list[tuple[object, ...]]],
    right: dict[str, list[tuple[object, ...]]],
) -> list[str]:
    """Human-readable differences, empty when the two schemas agree."""
    problems: list[str] = []
    for section in left:
        only_left = [row for row in left[section] if row not in right[section]]
        only_right = [row for row in right[section] if row not in left[section]]
        for row in only_left:
            problems.append(f"{section}: only after the migration: {row}")
        for row in only_right:
            problems.append(f"{section}: only after the extracted SQL: {row}")
    return problems


# --------------------------------------------------------------------------
# case selection
# --------------------------------------------------------------------------


@dataclass(slots=True)
class Case:
    repo: str
    path: str
    commit: str = ""
    parent: str = ""
    schema_path: str = ""
    schema_kind: str = ""
    status: str = "pending"
    reason: str = ""
    statements: tuple[str, ...] = ()
    differences: list[str] = field(default_factory=list)
    rails_version: str = ""
    preceding: list[str] = field(default_factory=list)
    """Migrations added by the same commit that must run first, by file name.

    The name, not the path it was staged at: the staging directory is a
    temporary one belonging to whichever machine ran the validation, and it
    would tell a reader of the committed results nothing they can use.
    """
    adaptations: list[str] = field(default_factory=list)
    """Lines removed from the pre-state because this validation machine
    cannot provide them (an extension with no Windows build). Recorded per
    case because an undisclosed change to the input is not validation."""


def adapt_schema(text: str, kind: str, missing: tuple[str, ...]) -> tuple[str, list[str]]:
    """Drop what an unavailable extension contributes to the pre-state.

    This is a property of the machine running the validation, not of the
    migration: pgvector has no Windows build, and two of these applications
    enable it. The adaptation is applied to the one schema file both
    databases are built from, so A and B still start identical -- which is
    what the comparison actually depends on. Every removed line is returned
    so the report can say exactly what was not there.
    """
    if not missing:
        return text, []

    removed: list[str] = []

    # Some schema.rb files are written in a DSL a gem provides. Where that
    # gem cannot be installed on this machine, its whole block goes -- a
    # half-removed `create_trigger ... end` is a syntax error, not a
    # smaller schema.
    for gem, openers in DSL_BLOCKS.items():
        if gem not in missing:
            continue
        lines = text.splitlines(keepends=True)
        kept_lines: list[str] = []
        skipping = False
        for line in lines:
            if not skipping and any(
                line.lstrip().startswith(opener) for opener in openers
            ):
                skipping = True
                removed.append(f"{line.strip()[:90]} ... (block)")
                continue
            if skipping:
                # hairtrigger closes its block with `end` at the same
                # two-space indent it opened at.
                if line.rstrip() == "  end":
                    skipping = False
                continue
            kept_lines.append(line)
        text = "".join(kept_lines)

    # An extension contributes more than its CREATE EXTENSION line: pgvector
    # also brings column types, and a column of an absent type fails just as
    # hard as the missing extension itself. The word boundary is what keeps
    # `tsvector` -- a core type, present everywhere -- out of this.
    types: list[str] = []
    for extension in missing:
        types.extend(EXTENSION_TYPES.get(extension, (extension,)))
    type_pattern = re.compile(
        r"\b(?:public\.)?(?:" + "|".join(re.escape(t) for t in types) + r")\b",
        re.IGNORECASE,
    )
    index_pattern = re.compile(r"using:?\s*:?\s*hnsw|opclass:\s*:vector", re.IGNORECASE)

    kept: list[str] = []
    for line in text.splitlines(keepends=True):
        lowered = line.lower()
        drop = any(
            f"enable_extension \"{extension}\"" in line
            or f"extension if not exists {extension}" in lowered
            or f"extension {extension} " in lowered
            for extension in missing
        )
        if not drop and (type_pattern.search(line) or index_pattern.search(line)):
            # Only where the line is a column or index definition rather
            # than prose: structure.sql is full of comment banners naming
            # the very objects being removed.
            stripped = line.strip()
            drop = not stripped.startswith("--")
        if drop:
            removed.append(line.strip())
        else:
            kept.append(line)

    adapted = "".join(kept)
    # Removing the last column of a table leaves the previous line's comma
    # dangling in front of the closing paren.
    adapted = re.sub(r",(\s*\n\s*\);)", r"\1", adapted)
    return adapted, removed


def git(repo: Path, *args: str, timeout: int = 300) -> str | None:
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=False, timeout=timeout
    )
    return result.stdout if result.returncode == 0 else None


def preload(text: str, kind: str, sql: str) -> str:
    """Put SQL in front of the schema file, in the schema file's own language.

    Some applications' schema files depend on a SQL object the file itself
    cannot declare -- Mastodon's `db/schema.rb` uses `timestamp_id()` as a
    column default, and Ruby schema format has no way to create a function.
    Supplying it here isolates the variable: it separates "the pre-state
    cannot be rebuilt" from "the extraction does not work", which are very
    different findings and would otherwise be reported as the same failure.
    """
    if not sql.strip():
        return text
    if kind == "sql":
        return f"{sql}\n{text}"
    heredoc = "ActiveRecord::Base.connection.execute(<<-'BLASTOISE_PRELOAD')"
    return f"{heredoc}\n{sql}\nBLASTOISE_PRELOAD\n{text}"


def prepare(
    case: Case,
    repo: Path,
    workdir: Path,
    missing: tuple[str, ...] = (),
    preload_sql: str = "",
) -> bool:
    """Find the commit that added the migration and the schema before it."""
    log = git(repo, "log", "--diff-filter=A", "-1", "--format=%H", "--", case.path)
    if not log or not log.strip():
        case.status = "skipped"
        case.reason = "could not find the commit that added the file"
        return False
    case.commit = log.strip()
    parent = git(repo, "rev-parse", f"{case.commit}^")
    if not parent or not parent.strip():
        case.status = "skipped"
        case.reason = "the adding commit has no parent (root commit)"
        return False
    case.parent = parent.strip()

    for path, kind in SCHEMA_FILES:
        text = git(repo, "show", f"{case.parent}:{path}")
        if text and text.strip():
            case.schema_path, case.schema_kind = path, kind
            adapted, removed = adapt_schema(text, kind, missing)
            if preload_sql.strip():
                adapted = preload(adapted, kind, preload_sql)
                removed = [*removed, "PRELOADED: " + preload_sql.strip().splitlines()[0]]
            case.adaptations = removed
            (workdir / Path(path).name).write_text(adapted, encoding="utf-8")
            break
    else:
        case.status = "skipped"
        case.reason = "no db/schema.rb or db/structure.sql at the parent commit"
        return False

    migration = git(repo, "show", f"{case.commit}:{case.path}")
    if migration is None:
        case.status = "skipped"
        case.reason = "could not read the migration at its adding commit"
        return False
    (workdir / Path(case.path).name).write_text(migration, encoding="utf-8")

    # Migrations added by the same commit that sort before this one. A
    # change that adds a column in one migration and indexes it in the next
    # is ordinary Rails, and the second one's pre-state includes the first.
    # This mirrors what the runner does with a pull request's file list.
    directory = case.path.rsplit("/", 1)[0]
    target_version = Path(case.path).name.split("_", 1)[0]
    added = git(repo, "show", "--name-only", "--diff-filter=A", "--format=", case.commit) or ""
    siblings = []
    for line in added.splitlines():
        line = line.strip()
        if (
            not line.endswith(".rb")
            or line == case.path
            or line.rsplit("/", 1)[0] != directory
        ):
            continue
        version = Path(line).name.split("_", 1)[0]
        if version.isdigit() and version < target_version:
            siblings.append((version, line))
    for _, path in sorted(siblings):
        text = git(repo, "show", f"{case.commit}:{path}")
        if text is None:
            continue
        (workdir / Path(path).name).write_text(text, encoding="utf-8")
        case.preceding.append(Path(path).name)
    return True


# --------------------------------------------------------------------------
# the run
# --------------------------------------------------------------------------


def ruby(cmd: list[str], env: dict[str, str], timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd, capture_output=True, text=True, check=False, timeout=timeout, env=env
    )


def run_case(
    case: Case,
    repo: Path,
    workdir: Path,
    admin_url: str,
    ruby_bin: str,
    env: dict[str, str],
    timeout: int,
    missing: tuple[str, ...] = (),
    preload_sql: str = "",
) -> None:
    if not prepare(case, repo, workdir, missing, preload_sql):
        return

    schema_file = workdir / Path(case.schema_path).name
    migration_file = workdir / Path(case.path).name
    token = secrets.token_hex(5)
    db_a, db_b = f"rv_a_{token}", f"rv_b_{token}"
    url_a, url_b = _scratch_url(admin_url, db_a), _scratch_url(admin_url, db_b)

    try:
        # --- A: the real migration ---------------------------------------
        request = {
            "admin_url": admin_url,
            "scratch_url": url_a,
            "scratch_dbname": db_a,
            "schema_file": str(schema_file),
            "schema_kind": case.schema_kind,
            # Recorded by name, staged in this run's working directory.
            "replay": [str(workdir / name) for name in case.preceding],
            "migration": str(migration_file),
            "drop_when_done": False,
        }
        req = workdir / "req.json"
        res = workdir / "res.json"
        req.write_text(json.dumps(request), encoding="utf-8")
        done = ruby([ruby_bin, str(HARNESS), str(req), str(res)], env, timeout)
        if not res.is_file():
            case.status = "extract_failed"
            tail = (done.stderr or done.stdout or "").strip().splitlines()
            case.reason = f"harness did not run: {tail[-1] if tail else 'no output'}"
            return
        payload = json.loads(res.read_text(encoding="utf-8"))
        case.rails_version = payload.get("rails_version") or ""
        if not payload.get("ok"):
            case.status = "extract_failed"
            case.reason = f"{payload.get('error_class', 'error')}: {payload.get('error', '')}"
            return
        statements = tuple(
            s["sql"].strip() for s in payload.get("statements", []) if s.get("sql")
        )
        case.statements = statements
        if not statements:
            case.status = "no_sql"
            case.reason = "the migration ran but emitted no SQL"
            return

        # --- the parser must take it unchanged ---------------------------
        sql = ";\n".join(s.rstrip(";") for s in statements) + ";\n"
        try:
            parse_migration(sql, path=case.path)
        except MigrationParseError as exc:
            case.status = "parse_failed"
            case.reason = str(exc)
            return

        # --- B: the extracted SQL ----------------------------------------
        request_b = dict(request, scratch_url=url_b, scratch_dbname=db_b)
        req_b = workdir / "req_b.json"
        res_b = workdir / "res_b.json"
        req_b.write_text(json.dumps(request_b), encoding="utf-8")
        done_b = ruby([ruby_bin, str(APPLY_HELPER), str(req_b), str(res_b)], env, timeout)
        if not res_b.is_file() or not json.loads(res_b.read_text(encoding="utf-8")).get("ok"):
            case.status = "setup_failed"
            detail = ""
            if res_b.is_file():
                detail = json.loads(res_b.read_text(encoding="utf-8")).get("error", "")
            tail = (done_b.stderr or "").strip().splitlines()
            case.reason = f"could not rebuild the pre-state: {detail or (tail[-1] if tail else '')}"
            return

        with psycopg.connect(url_b, autocommit=True) as conn:
            for statement in statements:
                try:
                    conn.execute(statement)  # type: ignore[arg-type]
                except psycopg.Error as exc:
                    case.status = "replay_failed"
                    case.reason = f"{statement[:80]}: {str(exc).strip().splitlines()[0]}"
                    return

        case.differences = compare(snapshot(url_a), snapshot(url_b))
        case.status = "match" if not case.differences else "mismatch"
    except subprocess.TimeoutExpired:
        case.status = "timeout"
        case.reason = f"exceeded {timeout}s"
    except Exception as exc:  # a validator must not die on one case
        case.status = "error"
        case.reason = f"{type(exc).__name__}: {exc}"
    finally:
        with psycopg.connect(admin_url, autocommit=True) as conn:
            for name in (db_a, db_b):
                with contextlib.suppress(psycopg.Error):
                    conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')  # type: ignore[arg-type]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", required=True, help="JSON list of {repo, path}")
    parser.add_argument("--repos", required=True, help="directory holding the clones")
    parser.add_argument("--admin-url", required=True)
    parser.add_argument("--ruby", required=True)
    parser.add_argument("--gem-home", default=None)
    parser.add_argument(
        "--gem-home-map",
        default=None,
        help="JSON {repo: gem_home}: each application renders with its own "
        "ActiveRecord, which is the whole point of using its bundle",
    )
    parser.add_argument(
        "--preload-sql",
        default=None,
        help="JSON {repo: path-to-sql}: SQL run before the schema file, for "
        "objects the schema file cannot itself declare",
    )
    parser.add_argument(
        "--missing-extensions",
        default="",
        help="comma-separated extensions this machine cannot provide; what "
        "they contribute is removed from the pre-state and reported",
    )
    parser.add_argument("--workdir", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()

    env = dict(os.environ)
    if args.gem_home:
        env["GEM_HOME"] = args.gem_home
        env["GEM_PATH"] = args.gem_home
    gem_homes = json.loads(args.gem_home_map) if args.gem_home_map else {}
    preloads = {
        repo: Path(path).read_text(encoding="utf-8")
        for repo, path in (json.loads(args.preload_sql) if args.preload_sql else {}).items()
    }
    missing = tuple(x.strip() for x in args.missing_extensions.split(",") if x.strip())

    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    cases = [Case(**entry) for entry in json.loads(Path(args.cases).read_text())]

    for index, case in enumerate(cases, start=1):
        case_dir = workdir / f"case{index:03d}"
        case_dir.mkdir(exist_ok=True)
        case_env = dict(env)
        if case.repo in gem_homes:
            case_env["GEM_HOME"] = gem_homes[case.repo]
            case_env["GEM_PATH"] = gem_homes[case.repo]
        run_case(
            case,
            Path(args.repos) / case.repo,
            case_dir,
            args.admin_url,
            args.ruby,
            case_env,
            args.timeout,
            missing,
            preloads.get(case.repo, ""),
        )
        print(f"[{index:3d}/{len(cases)}] {case.status:15s} {case.repo}/{Path(case.path).name}")
        if case.reason:
            print(f"          {case.reason[:150]}")
        sys.stdout.flush()

    Path(args.out).write_text(
        json.dumps([asdict(c) for c in cases], indent=2, default=str), encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
