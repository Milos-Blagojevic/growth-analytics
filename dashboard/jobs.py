"""hunt.py commands started from the dashboard: validation, cost estimates, and a one-at-a-time subprocess runner.

Each job runs `python hunt.py ...` in the project root, exactly as from a shell, so retries, cost logging, the
run lock (runs.LOCK_FILE) and Telegram delivery behave the same. Its output goes to jobs/<id>.log and its
outcome to the jobs table.
"""

import csv
import json
import os
import re
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import runs
from dashboard import db, ingest

JOBS_DIR = "jobs"
SAVED_RUN = re.compile(r"Saved raw results to out/([^/\s]+)/")
LOG_TAIL_BYTES = 200_000

RUN_MODES = {
    "dry_run": ("Dry run", ["--dry-run"]),  # research + score, digest printed in the log
    "deliver": ("Run and notify", []),  # research + score + Telegram notification
    "research_only": ("Research only", ["--research-only"]),
}
# README: "a full 19-company run currently costs about $0.27 in Claude".
CLAUDE_DOLLARS_PER_COMPANY = 0.27 / 19
# README: discovery runs 6 searches, "roughly $0.15-0.20 for --limit 20".
DISCOVER_ESTIMATE = "Exa: roughly $0.15–0.20 (one search per target role plus one deep search; README figure)"
MAX_DISCOVER_LIMIT = 50


@dataclass
class Spec:
    """A validated job: hunt.py arguments plus what the confirmation dialog shows."""

    kind: str
    args: list
    label: str
    fields: dict  # the form fields that produced it, resubmitted on confirm
    companies: list = None  # (name, domain) subset written to a CSV for --companies; None = companies.csv
    estimate: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


def _now():
    return datetime.now().isoformat(timespec="seconds")


def _exa_per_company(conn):
    row = conn.execute(
        "SELECT SUM(exa_dollars) AS dollars, SUM(companies) AS n, COUNT(*) AS runs FROM runs"
        " WHERE exa_dollars IS NOT NULL AND companies > 0"
    ).fetchone()
    return (row["dollars"] / row["n"], row["runs"]) if row["n"] else (None, 0)


def build(conn, root, form):
    """Validates submitted form fields into a Spec. Raises ValueError with a message for the user."""
    kind = form.get("kind", "")
    if kind == "run":
        return _build_run(conn, root, form)
    if kind == "discover":
        focus = " ".join(form.get("focus", "").split())[:100]
        try:
            limit = int(form.get("limit") or 15)
        except ValueError:
            raise ValueError("limit must be a number") from None
        if not 1 <= limit <= MAX_DISCOVER_LIMIT:
            raise ValueError(f"limit must be between 1 and {MAX_DISCOVER_LIMIT}")
        args = ["discover"] + ([focus] if focus else []) + ["--limit", str(limit)]
        label = f"Discover{f' “{focus}”' if focus else ''} (limit {limit})"
        return Spec("discover", args, label, {"kind": kind, "focus": focus, "limit": str(limit)},
                    estimate=[DISCOVER_ESTIMATE], warnings=["Appends new companies to discovered.csv."])
    if kind == "watch":
        dry = form.get("dry_run") == "1"
        warnings = [] if dry else ["Sends new postings to Telegram and records them in watch_state.json."]
        return Spec("watch", ["watch"] + (["--dry-run"] if dry else []), "Watch list" + (" (dry run)" if dry else ""),
                    {"kind": kind, "dry_run": "1" if dry else ""}, estimate=["Free: Ashby's public API only."],
                    warnings=warnings)
    if kind == "send":
        run_id = form.get("run_id", "")
        scored = {r["run_id"] for r in conn.execute("SELECT DISTINCT run_id FROM results WHERE score IS NOT NULL")}
        if run_id not in scored:
            raise ValueError("choose a saved run with scored results")
        return Spec("send", ["send", f"{ingest.OUT_DIR}/{run_id}"], f"Send notification for run {run_id}",
                    {"kind": kind, "run_id": run_id}, estimate=["Free: no research or scoring."],
                    warnings=["Sends the run's notification to Telegram."])
    raise ValueError(f"unknown job kind {kind!r}")


def _build_run(conn, root, form):
    mode = form.get("mode") or "dry_run"
    if mode not in RUN_MODES:
        raise ValueError(f"unknown run mode {mode!r}")
    name, flags = RUN_MODES[mode]
    domains = [d for d in form.getlist("domains") if d] if hasattr(form, "getlist") else form.get("domains", [])
    if domains:
        known = {
            r["domain"]: r["name"]
            for r in conn.execute("SELECT domain, name FROM leads UNION SELECT domain, name FROM results")
        }
        unknown = [d for d in domains if d not in known]
        if unknown:
            raise ValueError(f"unknown companies: {', '.join(unknown)}")
        companies = [(known[d], d) for d in dict.fromkeys(domains)]
        target = companies[0][0] if len(companies) == 1 else f"{len(companies)} selected companies"
    else:
        companies = None
        listed = ingest.read_companies(Path(root) / ingest.COMPANIES)
        if not listed:
            raise ValueError("companies.csv is missing or empty")
        target = f"{len(listed)} companies in companies.csv"
    n = len(companies or listed)

    per_company, history = _exa_per_company(conn)
    if per_company is None:
        estimate = ["Exa: no cost history yet (saved runs record their cost from now on)."]
    else:
        estimate = [f"Exa: about ${per_company * n:.2f} (${per_company:.3f} per company, average of {history} saved run{'s' if history > 1 else ''})."]
    warnings = []
    if mode != "research_only":
        estimate.append(f"Claude: about ${CLAUDE_DOLLARS_PER_COMPANY * n:.2f} (README figure: $0.27 for 19 companies).")
    if mode == "deliver":
        warnings.append("Sends the notification to Telegram.")
    fields = {"kind": "run", "mode": mode, "domains": [d for _, d in companies] if companies else []}
    return Spec("run", ["run", *flags], f"{name} · {target}", fields, companies=companies, estimate=estimate,
                warnings=warnings)


class Runner:
    """Starts at most one job at a time and records its outcome. Thread-safe."""

    def __init__(self, root, command=None):
        self.root = Path(root)
        self.command = list(command or [sys.executable, "hunt.py"])
        self._lock = threading.Lock()
        self._procs = {}  # job id -> Popen
        self._cancelled = set()

    def recover(self, conn):
        """Jobs left "running" by a previous dashboard process lost their watcher: mark them interrupted."""
        with conn:
            conn.execute("UPDATE jobs SET status = 'interrupted', finished_at = ? WHERE status = 'running'", [_now()])

    def busy(self):
        with self._lock:
            return bool(self._procs) or runs.is_locked(self.root)

    def log_path(self, job_id):
        return self.root / JOBS_DIR / f"{job_id}.log"

    def start(self, conn, spec):
        """Starts the job and returns its id. Raises runs.Busy if a hunt.py command is already running."""
        with self._lock:
            if self._procs or runs.is_locked(self.root):
                raise runs.Busy("another hunt.py command is running")
            (self.root / JOBS_DIR).mkdir(exist_ok=True)
            with conn:
                job_id = conn.execute(
                    "INSERT INTO jobs (kind, args, label, estimate, status, started_at) VALUES (?, ?, ?, ?, 'running', ?)",
                    [spec.kind, json.dumps(spec.args), spec.label, "\n".join(spec.estimate), _now()],
                ).lastrowid
            args = list(spec.args)
            if spec.companies is not None:
                path = self.root / JOBS_DIR / f"{job_id}-companies.csv"
                with open(path, "w", newline="") as f:
                    csv.writer(f).writerows(spec.companies)
                args += ["--companies", str(path)]
                with conn:
                    conn.execute("UPDATE jobs SET args = ? WHERE id = ?", [json.dumps(args), job_id])
            log = open(self.log_path(job_id), "w")
            try:
                proc = subprocess.Popen(
                    self.command + args,
                    cwd=self.root,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    env={**os.environ, "PYTHONUNBUFFERED": "1"},
                )
            except OSError as exc:
                log.write(f"Could not start: {exc}\n")
                log.close()
                with conn:
                    conn.execute(
                        "UPDATE jobs SET status = 'failed', finished_at = ? WHERE id = ?", [_now(), job_id]
                    )
                raise
            self._procs[job_id] = proc
            threading.Thread(target=self._wait, args=(job_id, proc, log), daemon=True).start()
            return job_id

    def cancel(self, job_id):
        """Stops a running job (SIGTERM). Returns False if it is not running."""
        with self._lock:
            proc = self._procs.get(job_id)
            if proc is None:
                return False
            self._cancelled.add(job_id)
            proc.terminate()
            return True

    def _wait(self, job_id, proc, log):
        code = proc.wait()
        log.close()
        match = None
        for match in SAVED_RUN.finditer(self.read_log(job_id)):
            pass
        run_id = match.group(1) if match else None
        with self._lock:
            cancelled = job_id in self._cancelled
        if cancelled:
            status = "cancelled"
        elif code == 0:
            status = "succeeded"
        elif run_id:
            status = "partial"  # results saved, but some companies failed (hunt.py exits 1)
        else:
            status = "failed"
        conn = db.connect(self.root)
        try:
            with conn:
                conn.execute(
                    "UPDATE jobs SET status = ?, exit_code = ?, run_id = ?, finished_at = ? WHERE id = ?",
                    [status, code, run_id, _now(), job_id],
                )
        finally:
            conn.close()
            with self._lock:
                self._procs.pop(job_id, None)
                self._cancelled.discard(job_id)

    def read_log(self, job_id):
        path = self.log_path(job_id)
        if not path.exists():
            return ""
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - LOG_TAIL_BYTES))
            text = f.read().decode(errors="replace")
        return text if size <= LOG_TAIL_BYTES else "…\n" + text.split("\n", 1)[-1]


def recent(conn, limit=30):
    return [dict(r) | {"args": json.loads(r["args"])} for r in conn.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", [limit])]


def get(conn, job_id):
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", [job_id]).fetchone()
    return dict(row) | {"args": json.loads(row["args"])} if row else None
