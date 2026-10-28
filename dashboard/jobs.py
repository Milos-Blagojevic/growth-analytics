"""hunt.py commands started from the dashboard: validation, cost estimates, and a one-at-a-time subprocess runner.

Each job runs `python hunt.py ...` (the hunt.py next to this package) with the data folder (root) as working
directory, exactly as from a shell there, so retries, cost logging, the run lock (runs.LOCK_FILE) and Telegram
delivery behave the same. Its output goes to jobs/<id>.log and its outcome to the jobs table.
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

import interviews
import mail
import outlook
import runs
from dashboard import db, ingest

JOBS_DIR = "jobs"
HUNT_SCRIPT = Path(__file__).resolve().parent.parent / "hunt.py"
SAVED_RUN = re.compile(r"Saved raw results to out/([^/\s]+)/")
LOG_TAIL_BYTES = 200_000

RUN_MODES = {
    "dry_run": ("Dry run", ["--dry-run"]),  # research + score, digest printed in the log
    "deliver": ("Run and notify", []),  # research + score + Telegram notification
    "research_only": ("Research only", ["--research-only"]),
}
# README: "a full 19-company run currently costs about $0.27 in Claude" (before the Jev screen, so an upper bound).
CLAUDE_DOLLARS_PER_COMPANY = 0.27 / 19
JEV_RUN_ESTIMATE = ("Jev (when TYPESAFE_API_KEY is set): under $0.001 per company, one request per open role; "
                    "companies whose roles all fail the screen skip Claude.")
# README: discovery runs 6 searches, "roughly $0.15-0.20 for --limit 20".
DISCOVER_ESTIMATE = "Exa: roughly $0.15–0.20 (one search per target role plus one deep search; README figure)"
MAX_DISCOVER_LIMIT = 50
MAIL_ESTIMATE = ("Jev (when TYPESAFE_API_KEY is set): one request per application email, a fraction of a cent in total. "
                 "Claude: one call per 20 application emails Jev does not rule out, a few cents each; none when no "
                 "email looks like one. Reading Outlook is free.")
NEWS_ESTIMATE = ["Exa: about $0.007 per search, one per [news].topics entry.",
                 "Jev (when TYPESAFE_API_KEY is set): one request per new article, a fraction of a cent in total.",
                 "Claude: one call, a few cents; none when no new article is relevant."]


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
    if kind == "mail":
        if not (Path(root) / outlook.TOKEN_FILE).exists():
            raise ValueError("Sign in to Outlook first: run `python hunt.py outlook-login` in the data folder "
                             "(see README, Email updates).")
        dry = form.get("dry_run") == "1"
        warnings = [f"Sends the sender, subject and first {mail.BODY_CHARS:,} characters of each email that looks "
                    "like an application email to Jev (TypeSafe, when TYPESAFE_API_KEY is set) and, unless Jev rules "
                    "it out, to Claude."]
        if not dry:
            warnings.append("Sends rejections and interview invites to Telegram and records the checked mail in "
                            f"{mail.STATE}.")
        return Spec("mail", ["mail"] + (["--dry-run"] if dry else []), "Check email" + (" (dry run)" if dry else ""),
                    {"kind": kind, "dry_run": "1" if dry else ""}, estimate=[MAIL_ESTIMATE], warnings=warnings)
    if kind == "news":
        dry = form.get("dry_run") == "1"
        warnings = [] if dry else ["Sends the digest to Telegram and records the articles in news_state.json."]
        return Spec("news", ["news"] + (["--dry-run"] if dry else []), "News digest" + (" (dry run)" if dry else ""),
                    {"kind": kind, "dry_run": "1" if dry else ""}, estimate=NEWS_ESTIMATE, warnings=warnings)
    if kind == "send":
        run_id = form.get("run_id", "")
        scored = {r["run_id"] for r in conn.execute("SELECT DISTINCT run_id FROM results WHERE score IS NOT NULL")}
        if run_id not in scored:
            raise ValueError("choose a saved run with scored results")
        return Spec("send", ["send", f"{ingest.OUT_DIR}/{run_id}"], f"Send notification for run {run_id}",
                    {"kind": kind, "run_id": run_id}, estimate=["Free: no research or scoring."],
                    warnings=["Sends the run's notification to Telegram."])
    if kind in ("interview_prep", "interview_review"):
        return _build_interview(root, kind, form.get("interview", ""))
    raise ValueError(f"unknown job kind {kind!r}")


CLAUDE_INTERVIEW_ESTIMATE = "Claude: one call, a few cents; the saved result records the actual tokens."


def _build_interview(root, kind, interview_id):
    try:
        path = interviews.directory(root, interview_id)
    except LookupError as exc:
        raise ValueError(str(exc)) from None
    interview = interviews.load_json(path, interviews.INTERVIEW)
    name = f"{interview['company']} · {interview['role']}"
    fields = {"kind": kind, "interview": interview_id}
    args = [kind.replace("_", "-"), f"{interviews.DIR}/{interview_id}"]
    if kind == "interview_prep":
        estimate = [CLAUDE_INTERVIEW_ESTIMATE]
        if not interview.get("posting_text") and interview.get("posting_url"):
            estimate.append("Exa: about $0.001 to fetch the posting text.")
        warnings = ["Sends your profile, CV, the posting and the findings of your past interview reviews to Claude."]
        if (path / interviews.PREP).exists():
            warnings.append("Replaces the current prep brief.")
        return Spec(kind, args, f"Interview prep · {name}", fields, estimate=estimate, warnings=warnings)
    transcript = interviews.load_json(path, interviews.TRANSCRIPT)
    if transcript is None:
        raise ValueError("upload a transcript first")
    if transcript.get("me") is None:
        raise ValueError("choose which speaker is you first")
    words = sum(s["words"] for s in transcript["speakers"].values())
    warnings = ["Sends the whole transcript, including the interviewer's words, to Claude."]
    if (path / interviews.REVIEW).exists():
        warnings.append("Replaces the current review (the progress table keeps only the latest one).")
    return Spec(kind, args, f"Interview review · {name}", fields,
                estimate=[f"{CLAUDE_INTERVIEW_ESTIMATE} The transcript has about {words:,} words."], warnings=warnings)


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
        estimate.append(f"Claude: at most about ${CLAUDE_DOLLARS_PER_COMPANY * n:.2f} (README figure: $0.27 for 19 "
                        "companies, before the Jev screen).")
        estimate.append(JEV_RUN_ESTIMATE)
    if mode == "deliver":
        warnings.append("Sends the notification to Telegram.")
    fields = {"kind": "run", "mode": mode, "domains": [d for _, d in companies] if companies else []}
    return Spec("run", ["run", *flags], f"{name} · {target}", fields, companies=companies, estimate=estimate,
                warnings=warnings)


class Runner:
    """Starts at most one job at a time and records its outcome. Thread-safe."""

    def __init__(self, root, command=None):
        self.root = Path(root)
        self.command = list(command or [sys.executable, str(HUNT_SCRIPT)])
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
