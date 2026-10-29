"""Rebuilds the derived tables of hunt.db from the pipeline's files.

Sources (all relative to the project root): out/<run>/*.json, discovered.csv, companies.csv, watch_state.json,
interviews/<id>/*.json, mail/*.json.
A full rebuild is cheap at this size and keeps ingest idempotent; `refresh` skips it when no source changed.
"""

import csv
import hashlib
import json
import logging
from datetime import datetime
from pathlib import Path

import interviews
import mail
import runs
from dashboard import db

logger = logging.getLogger("dashboard.ingest")

OUT_DIR = "out"
DISCOVERED = "discovered.csv"
COMPANIES = "companies.csv"
WATCH_STATE = "watch_state.json"
# What a corrupt or half-written source raises: bad JSON/CSV, a missing key or column, a wrong shape (AttributeError
# when watch_state.json holds a list instead of a dict), a NUL byte in a CSV (csv.Error).
UNREADABLE = (OSError, ValueError, KeyError, TypeError, AttributeError, csv.Error)


def signature(root):
    """Fingerprint (paths, sizes, mtimes) of every source file; changes whenever a run, discover, watch or mail writes."""
    root = Path(root)
    files = [p for d in runs.run_dirs(root / OUT_DIR) for p in sorted(d.glob("*.json"))]
    files += [root / name for name in (DISCOVERED, COMPANIES, WATCH_STATE) if (root / name).exists()]
    files += [p for d in interviews.interview_dirs(root) for p in sorted(d.glob("*.json"))]
    files += mail.update_files(root)
    digest = hashlib.sha256()
    for path in files:
        stat = path.stat()
        digest.update(f"{path.relative_to(root)}\0{stat.st_size}\0{stat.st_mtime_ns}\n".encode())
    return digest.hexdigest()


def refresh(conn, root):
    """Ingests if any source changed since the last ingest. Returns True if it ingested."""
    current = signature(root)
    row = conn.execute("SELECT value FROM meta WHERE key = 'signature'").fetchone()
    if row and row["value"] == current:
        return False
    ingest(conn, root, current)
    return True


def ingest(conn, root, sig=None):
    """Replaces every derived row with what the source files hold now, in one transaction.

    A source that cannot be read (for example a result file still being written, or a corrupt watch_state.json,
    discovered.csv or companies.csv) is skipped and listed in meta 'problems'; the other sources are still
    ingested and the next refresh retries it because its files change.
    """
    root = Path(root)
    problems = []
    with conn:
        for table in db.DERIVED_TABLES:  # user tables (tracking, side_projects, jobs, mail_decisions) are never touched
            conn.execute(f"DELETE FROM {table}")
        for run_dir in runs.run_dirs(root / OUT_DIR):
            try:
                _ingest_run(conn, run_dir)
            except UNREADABLE as exc:
                logger.warning("skipping %s: %s", run_dir, exc)
                problems.append(f"{run_dir.name}: {type(exc).__name__}: {exc}")
        try:
            _ingest_watch(conn, root / WATCH_STATE)
        except UNREADABLE as exc:
            logger.warning("skipping %s: %s", WATCH_STATE, exc)
            problems.append(f"{WATCH_STATE}: {type(exc).__name__}: {exc}")
        try:
            queued = {domain for _, domain in read_companies(root / COMPANIES)}
        except ValueError as exc:  # leads still show, none marked as queued; the Runs page cannot start a full run
            logger.warning("skipping %s: %s", COMPANIES, exc)
            problems.append(f"{COMPANIES}: {exc}")
            queued = set()
        try:
            _ingest_leads(conn, root / DISCOVERED, queued)
        except UNREADABLE as exc:
            logger.warning("skipping %s: %s", DISCOVERED, exc)
            problems.append(f"{DISCOVERED}: {type(exc).__name__}: {exc}")
        for path in interviews.interview_dirs(root):
            try:
                _ingest_interview(conn, path)
            except UNREADABLE as exc:
                logger.warning("skipping %s: %s", path, exc)
                problems.append(f"{interviews.DIR}/{path.name}: {type(exc).__name__}: {exc}")
        for path in mail.update_files(root):
            try:
                _ingest_mail(conn, path)
            except UNREADABLE as exc:
                logger.warning("skipping %s: %s", path, exc)
                problems.append(f"{mail.DIR}/{path.name}: {type(exc).__name__}: {exc}")
        meta = {
            "signature": sig or signature(root),
            "ingested_at": datetime.now().isoformat(timespec="seconds"),
            "problems": json.dumps(problems),
        }
        conn.executemany("INSERT INTO meta (key, value) VALUES (?, ?)", meta.items())


def _ingest_mail(conn, path):
    updates = json.loads(path.read_text())["updates"]
    conn.executemany(
        "INSERT OR IGNORE INTO mail_updates VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (u["id"], path.name, u["received_at"], u["from"], u["from_address"], u["subject"], u.get("web_link"),
             u["outcome"], u["company"], u["role"], u["summary"])
            for u in updates
            if u["outcome"] in mail.SUGGESTED_STATUS
        ],
    )


def _run_time(run_dir):
    try:
        return datetime.strptime(run_dir.name, "%Y%m%d-%H%M%S").isoformat(timespec="seconds")
    except ValueError:
        return datetime.fromtimestamp(run_dir.stat().st_mtime).isoformat(timespec="seconds")


def _ingest_run(conn, run_dir):
    results = runs.load_results(run_dir)
    manifest = runs.load_manifest(run_dir) or {}
    exa = manifest.get("exa") or {}
    claude = manifest.get("claude") or {}
    conn.execute(
        "INSERT INTO runs VALUES (:id, :mode, :started_at, :saved_at, :companies, :errors,"
        " :exa_requests, :exa_dollars, :model, :tokens_in, :tokens_out)",
        {
            "id": run_dir.name,
            "mode": manifest.get("mode"),
            "started_at": manifest.get("started_at"),
            "saved_at": manifest.get("saved_at") or _run_time(run_dir),
            "companies": len(results),
            "errors": sum("error" in r for r in results),
            "exa_requests": exa.get("requests"),
            "exa_dollars": exa.get("dollars"),
            "model": claude.get("model"),
            "tokens_in": claude.get("input_tokens"),
            "tokens_out": claude.get("output_tokens"),
        },
    )
    for r in results:
        research = r.get("research") or {}
        content = research.get("content") or {}
        fit = r.get("fit") or {}
        conn.execute(
            "INSERT OR REPLACE INTO results VALUES (:run_id, :domain, :name, :verified, :business_summary, :signals,"
            " :role_source, :dead_roles, :grounding, :warnings, :score, :verdict, :best_role_title, :best_role_url,"
            " :location_assessment, :next_step, :gap_note, :scope_note, :reasons, :concerns, :proof_ideas, :error)",
            {
                "run_id": run_dir.name,
                "domain": runs.normalize_domain(r["domain"]),  # one card per company, whatever companies.csv spelled
                "name": r["name"],
                "verified": content.get("identity_verified"),
                "business_summary": content.get("business_summary"),
                "signals": content.get("signals"),
                "role_source": research.get("role_source"),
                "dead_roles": json.dumps(research.get("dead_roles", [])),
                "grounding": json.dumps(research.get("grounding", {})),
                "warnings": json.dumps(research.get("warnings", [])),  # runs saved before warnings lack the key
                "score": fit.get("score"),
                "verdict": fit.get("verdict"),
                "best_role_title": fit.get("best_role_title"),
                "best_role_url": fit.get("best_role_url"),
                "location_assessment": fit.get("location_assessment"),
                "next_step": fit.get("next_step"),
                "gap_note": fit.get("gap_note"),
                "scope_note": fit.get("scope_note"),
                "reasons": json.dumps(fit.get("reasons", [])),
                "concerns": json.dumps(fit.get("concerns", [])),
                "proof_ideas": json.dumps(fit.get("proof_ideas", [])),
                "error": r.get("error"),
            },
        )
        conn.executemany(
            "INSERT OR IGNORE INTO roles VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    run_dir.name,
                    runs.normalize_domain(r["domain"]),
                    role["url"],
                    role["title"],
                    role.get("location"),
                    role.get("remote_or_relocation"),
                    role.get("link_status"),
                )
                for role in content.get("open_roles", [])
            ],
        )


def _ingest_watch(conn, path):
    if not path.exists():
        return
    state = json.loads(path.read_text())
    conn.executemany(
        "INSERT INTO watch_postings VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (board, job_id, e["title"], e.get("location"), e.get("remote"), e.get("url"), e["first_seen"])
            for board, seen in state.items()
            for job_id, e in seen.items()
        ],
    )


def _ingest_interview(conn, path):
    interview = interviews.load_json(path, interviews.INTERVIEW)
    transcript = interviews.load_json(path, interviews.TRANSCRIPT)
    if transcript is not None:
        metrics = interviews.metrics(transcript, interview.get("language"))
        transcript = {k: v for k, v in transcript.items() if k != "turns"} | {
            "turn_count": len(transcript["turns"]), "metrics": metrics}
    prep, review = interviews.load_json(path, interviews.PREP), interviews.load_json(path, interviews.REVIEW)
    conn.execute(
        "INSERT INTO interviews VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            path.name,
            interview["company"],
            interview.get("domain") or None,
            interview["role"],
            interview["stage"],
            interview.get("scheduled_at") or None,
            interview.get("scheduled_at") or interview["created_at"],
            json.dumps(interview),
            json.dumps(transcript) if transcript is not None else None,
            json.dumps(prep) if prep is not None else None,
            json.dumps(review) if review is not None else None,
        ],
    )


def read_companies(path):
    """(name, domain) rows of companies.csv as hunt.load_companies reads them: domains normalised
    (runs.normalize_domain) and a repeated domain keeps its first row; [] if the file is missing.

    Unlike hunt.py it never exits: malformed rows are skipped. An unreadable file (e.g. a field beyond csv's
    field limit) raises ValueError, which every caller handles.
    """
    path = Path(path)
    if not path.exists():
        return []
    companies = []
    seen = set()
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            for row in csv.reader(f):
                if len(row) < 2 or row[0].strip().startswith("#") or not row[0].strip() or not row[1].strip():
                    continue
                domain = runs.normalize_domain(row[1])
                if domain not in seen:
                    seen.add(domain)
                    companies.append((row[0].strip(), domain))
    except (OSError, csv.Error) as exc:  # a directory, no permission, a field beyond csv's limit, ...
        raise ValueError(f"cannot be read: {exc}") from exc
    return companies


def add_companies(path, companies):
    """Appends (name, domain) rows not yet in companies.csv (domains compared normalised, as hunt.py reads them);
    creates the file if needed. Returns the rows added."""
    path = Path(path)
    listed = {domain for _, domain in read_companies(path)}
    new = [(name, domain) for name, domain in dict.fromkeys(companies) if runs.normalize_domain(domain) not in listed]
    if not new:
        return []
    text = path.read_text() if path.exists() else ""
    with open(path, "a", newline="") as f:
        if text and not text.endswith("\n"):
            f.write("\n")
        csv.writer(f, lineterminator="\n").writerows(new)
    return new


def _ingest_leads(conn, discovered, queued):
    """`queued`: normalised domains of companies.csv."""
    if not discovered.exists():
        return
    with open(discovered, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    conn.executemany(
        "INSERT OR IGNORE INTO leads VALUES (?, ?, ?, ?, ?, ?)",
        [(domain, r["name"], r["why"], r["focus"], r["discovered_at"], domain in queued)
         for r in rows for domain in [runs.normalize_domain(r["domain"])]],  # discover writes it so; hand edits may not
    )
