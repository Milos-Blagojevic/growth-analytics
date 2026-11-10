"""prospects.duckdb: every saved run as tables, rebuilt from the run files.

The JSON files under prospect_runs/ are the record; this file is derived from them and can be deleted. A rebuild
rates every assessed prospect again with the current prospects.toml (value.py), so the tiers follow the config
without new research. DuckDB lets one process write a file or several read it, never both, so the rebuild
writes a new file next to the old one and swaps it in: a reader holding the old file open keeps working.
"""

import json
import logging
import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path

import duckdb

from pipeline import runs
from prospects import value

logger = logging.getLogger("warehouse")

RUN_FORMAT = "%Y%m%d-%H%M%S"  # how `run` names a run's folder: when it started
TABLES = ("runs", "prospects", "figures", "facts", "news")

SCHEMA = """
CREATE TABLE runs (
    run_id TEXT PRIMARY KEY,  -- the run's folder name: when it started
    mode TEXT,  -- research_only | assessed
    started_at TIMESTAMP,
    saved_at TIMESTAMP,
    companies INTEGER,
    errors INTEGER,
    exa_dollars DOUBLE,
    model TEXT,
    input_tokens BIGINT,
    output_tokens BIGINT
);
-- One row per prospect per run. Columns from fit_score to brief are Claude's assessment (NULL after a
-- research-only run); basis_usd to route_reason come from value.py at rebuild time: the published figure it picks
-- for the pricing basis and for revenue (all of them are in `figures`), then the value range, tier and route.
CREATE TABLE prospects (
    run_id TEXT,
    domain TEXT,
    name TEXT,
    error TEXT,
    business_summary TEXT,
    fit_score INTEGER,
    fit_reasons TEXT[],
    fit_concerns TEXT[],
    disqualifier TEXT,
    headcount_low BIGINT,
    headcount_high BIGINT,
    funding_stage TEXT,
    last_round_usd DOUBLE,
    last_round_date TEXT,
    total_funding_usd DOUBLE,
    value_signals TEXT[],
    brief TEXT,
    basis_usd DOUBLE,
    revenue_usd DOUBLE,
    value_low DOUBLE,
    value_high DOUBLE,
    value_method TEXT,
    tier TEXT,
    route TEXT,
    route_reason TEXT,
    warnings TEXT[],
    PRIMARY KEY (run_id, domain)
);
-- Every published figure Claude copied from the research, one row each. `used` marks the one the value estimate
-- ran on (value.pick), so a prospect with several figures shows what was set aside.
CREATE TABLE figures (
    run_id TEXT,
    domain TEXT,
    kind TEXT,  -- basis (the pricing basis) | revenue
    amount_usd DOUBLE,
    as_of TEXT,  -- YYYY-MM, YYYY, or empty when the research gives no period
    stated_by TEXT,  -- company | third party
    evidence TEXT,
    used BOOLEAN
);
-- What Exa found, one row per research field, with the pages it cites.
CREATE TABLE facts (
    run_id TEXT,
    domain TEXT,
    source TEXT,  -- site (the prospect's own pages) | money (open web)
    field TEXT,
    value TEXT,
    confidence TEXT,
    urls TEXT[]
);
CREATE TABLE news (run_id TEXT, domain TEXT, title TEXT, url TEXT, date TEXT, summary TEXT);
-- Each prospect's newest assessed result, or its newest research when it was never assessed; failed results aside.
-- A later --research-only run or a failure therefore never hides a rating.
CREATE VIEW latest AS
    SELECT * FROM prospects WHERE error IS NULL
    QUALIFY row_number() OVER (PARTITION BY domain ORDER BY fit_score IS NOT NULL DESC, run_id DESC) = 1;
"""

ASSESSED = (
    "fit_score", "fit_reasons", "fit_concerns", "disqualifier", "headcount_low", "headcount_high", "funding_stage",
    "last_round_usd", "last_round_date", "total_funding_usd", "value_signals", "brief",
)  # fmt: skip
KINDS = ("basis", "revenue")  # the figures an assessment lists: <kind>_figures


def is_run(name):
    """True for a folder name `run` creates. Anything else under prospect_runs/ (a backup copy, an editor's folder)
    is not a run: read as one, a name sorting after the timestamps would pass for the newest."""
    try:
        return name.isascii() and datetime.strptime(name, RUN_FORMAT).strftime(RUN_FORMAT) == name
    except ValueError:
        return False


def _load(path):
    """The JSON object in `path`, or None (with a warning) when it cannot be read, e.g. a run interrupted mid-save."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError, RecursionError) as exc:
        logger.warning("skipping %s: %s", path, exc)
        return None
    if not isinstance(data, dict):
        logger.warning("skipping %s: not a JSON object", path)
        return None
    return data


def _line(row):
    """`row` as a line of JSON for DuckDB. Text cut inside an emoji (half a surrogate pair, which Exa can return) is
    neither valid UTF-8 nor JSON that DuckDB accepts: the stray half becomes "?"."""
    return json.dumps(row, ensure_ascii=False).encode("utf-8", "replace")


def _timestamp(text):
    """`text` as an ISO timestamp DuckDB reads, or None when it is not one."""
    try:
        return datetime.fromisoformat(text).isoformat()
    except (TypeError, ValueError):
        return None


def _run_row(run_dir):
    """The run's `runs` row; all but the id is None for a run without a manifest (interrupted before it finished)."""
    manifest = (_load(run_dir / runs.MANIFEST) if (run_dir / runs.MANIFEST).exists() else None) or {}
    exa, claude = (manifest.get(key) if isinstance(manifest.get(key), dict) else {} for key in ("exa", "claude"))
    return {
        "run_id": run_dir.name,
        "mode": manifest.get("mode"),
        "started_at": _timestamp(manifest.get("started_at")),
        "saved_at": _timestamp(manifest.get("saved_at")),
        "companies": manifest.get("companies"),
        "errors": manifest.get("errors"),
        "exa_dollars": exa.get("dollars"),
        "model": claude.get("model"),
        "input_tokens": claude.get("input_tokens"),
        "output_tokens": claude.get("output_tokens"),
    }


def _result_rows(run_id, path, result, config):
    """The rows one result file adds, {table: [row]}. Raises for a file that is not a result as `run` saves it."""
    domain, name = result["domain"], result["name"]
    if not isinstance(domain, str) or path.name != f"{domain}.json":  # e.g. a copy: the same prospect twice in one run
        raise ValueError(f"not named after its domain, {domain}")
    research = result.get("research") or {}
    prospect = {
        "run_id": run_id,
        "domain": domain,
        "name": name,
        "error": result.get("error"),
        "business_summary": (research.get("site") or {}).get("business_summary"),
        "warnings": research.get("warnings") or [],
    }
    assessment = result.get("assessment")
    figures = []
    if assessment:
        worth = value.estimate(assessment, config)
        where = value.route(assessment, worth, config)
        prospect |= {column: assessment.get(column) for column in ASSESSED}
        for kind in KINDS:
            picked = value.pick(assessment[f"{kind}_figures"])
            prospect[f"{kind}_usd"] = picked and picked["amount_usd"]
            figures += [
                {
                    "run_id": run_id, "domain": domain, "kind": kind, "amount_usd": figure["amount_usd"],
                    "as_of": figure["as_of"], "stated_by": figure["stated_by"], "evidence": figure.get("evidence"),
                    "used": figure is worth["figure"],
                }
                for figure in assessment[f"{kind}_figures"]
            ]  # fmt: skip
        prospect |= {"value_low": worth["low"], "value_high": worth["high"], "value_method": worth["method"]}
        prospect |= {"tier": where["tier"], "route": where["route"], "route_reason": where["reason"]}

    facts = []
    for source in ("site", "money"):
        grounding = (research.get("grounding") or {}).get(source) or {}
        for field, text in (research.get(source) or {}).items():
            if field == "news":
                continue
            cited = grounding.get(field) or {}
            facts.append({
                "run_id": run_id, "domain": domain, "source": source, "field": field,
                "value": None if text is None else str(text),
                "confidence": cited.get("confidence"), "urls": cited.get("urls") or [],
            })  # fmt: skip
    news = [
        {"run_id": run_id, "domain": domain, **{key: item.get(key) for key in ("title", "url", "date", "summary")}}
        for item in (research.get("money") or {}).get("news") or []
    ]
    return {"prospects": [prospect], "figures": figures, "facts": facts, "news": news}


def _fill(db, scratch, batches):
    """Loads `batches` ({table: [JSON line]} each) with one COPY per table: row-by-row inserts take seconds per
    thousand rows."""
    for table in TABLES:
        lines = [line for rows in batches for line in rows.get(table, ())]
        if lines:
            rows_file = scratch / f"{table}.json"
            rows_file.write_bytes(b"\n".join(lines))
            quoted = str(rows_file).replace("'", "''")
            db.execute(f"COPY {table} FROM '{quoted}' (FORMAT json)")


def _write(target, scratch, batches):
    for old in (target, Path(f"{target}.wal")):
        old.unlink(missing_ok=True)
    with duckdb.connect(str(target)) as db:
        db.execute(SCHEMA)
        _fill(db, scratch, batches)


def _accepted(scratch, sources):
    """The batches DuckDB takes, each source tried alone. A source it rejects (a value that does not fit its column,
    say) is left out with a warning; a run's manifest then gives way to a bare row for the run."""
    batches = []
    with duckdb.connect() as probe:
        probe.execute(SCHEMA)
        for label, rows, spare in sources:
            probe.execute("BEGIN")
            try:
                _fill(probe, scratch, [rows])
            except duckdb.Error as exc:
                logger.warning("skipping %s: %s", label, str(exc).partition("\n")[0])
                rows = spare
            probe.execute("ROLLBACK")
            if rows:
                batches.append(rows)
    return batches


def build(runs_dir, db_path, config):
    """Rebuilds `db_path` from every run saved under `runs_dir`. Returns (runs, prospect rows).

    A folder that is not a run, a file that is not a result and a result DuckDB will not load are skipped with a
    warning, never fatal: the warehouse has to rebuild after every run, whatever else has landed in the folder.
    """
    sources = []  # (the file the rows come from, {table: [JSON line]}, the rows to load instead if DuckDB rejects them)
    for run_dir in runs.run_dirs(runs_dir):
        if not is_run(run_dir.name):
            logger.warning("skipping %s: not a run folder", run_dir)
            continue
        bare = {"runs": [_line({"run_id": run_dir.name})]}
        sources.append((run_dir / runs.MANIFEST, {"runs": [_line(_run_row(run_dir))]}, bare))
        for path in sorted(run_dir.glob("*.json")):
            result = None if path.name == runs.MANIFEST else _load(path)
            if result is None:
                continue
            try:
                rows = _result_rows(run_dir.name, path, result, config)
                lines = {table: [_line(row) for row in new] for table, new in rows.items()}
            except Exception as exc:  # not a file `run` wrote, or one in a format this code no longer reads
                logger.warning("skipping %s: %s: %s", path, type(exc).__name__, exc)
                continue
            sources.append((path, lines, None))

    db_path = Path(db_path)
    # Built in a scratch folder next to the target: the swap is then a rename within one filesystem, and whatever
    # DuckDB leaves beside the new file goes with the folder.
    scratch = Path(tempfile.mkdtemp(prefix=f"{db_path.name}.", dir=db_path.parent))
    target = scratch / "build.duckdb"
    batches = [rows for _, rows, _ in sources]
    try:
        try:
            _write(target, scratch, batches)
        except duckdb.Error:
            # DuckDB names a line of the scratch file, not the file the value came from: find it and go on without it.
            batches = _accepted(scratch, sources)
            _write(target, scratch, batches)
        # A write-ahead log left by a crashed writer belongs to the file being replaced; beside the new one it
        # would make every open fail.
        Path(f"{db_path}.wal").unlink(missing_ok=True)
        os.replace(target, db_path)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return tuple(sum(len(rows.get(table, ())) for rows in batches) for table in ("runs", "prospects"))


def ranking(db_path):
    """Each prospect's `latest` row: assessed ones by value (the range's midpoint; unknown values after known
    ones), then dropped ones, then those only researched. Dicts with name, domain, fit_score, value_low, value_high,
    value_method, tier, route."""
    columns = ("name", "domain", "fit_score", "value_low", "value_high", "value_method", "tier", "route")
    with duckdb.connect(str(db_path), read_only=True) as db:
        rows = db.execute(
            f"SELECT {', '.join(columns)} FROM latest "
            "ORDER BY tier IS NULL, tier = ?, sqrt(value_low * value_high) DESC NULLS LAST, fit_score DESC, name",
            [value.OUT],
        ).fetchall()
    return [dict(zip(columns, row)) for row in rows]
