"""Local dashboard over the job-hunt pipeline (served by `python -m dashboard`).

Pages read the pipeline's saved files (through hunt.db); your statuses, notes and side projects live in hunt.db;
jobs start hunt.py commands as subprocesses. Every POST needs the per-process CSRF token (sent by htmx as the
X-CSRF-Token header) and a matching Origin, and every request a loopback Host, so other websites open in the
browser cannot start paid runs.
"""

import secrets
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import runs
from dashboard import db, ingest, jobs, projects, queries, tracking

HERE = Path(__file__).parent
VERDICT_LABELS = {
    "apply": "Apply",
    "consider": "Consider",
    "stretch": "Stretch",
    "check_careers": "Check careers",
    "skip": "Skip",
}
STATUS_LABELS = {
    "new": "New",
    "saved": "Saved",
    "applied": "Applied",
    "interviewing": "Interviewing",
    "offer": "Offer",
    "rejected": "Rejected",
    "dismissed": "Dismissed",
}
JOB_LABELS = {
    "running": "running",
    "succeeded": "done",
    "partial": "done, some failed",
    "failed": "failed",
    "cancelled": "cancelled",
    "interrupted": "interrupted",
}
MODE_LABELS = {"research_only": "research only", "dry_run": "dry run", "deliver": "sent to Telegram"}
INBOX_DAYS = (7, 14, 30, 0)
LOOPBACK_HOSTS = ("127.0.0.1", "localhost")


def _ago(iso):
    if not iso:
        return ""
    delta = datetime.now() - datetime.fromisoformat(iso)
    if delta.days >= 1:
        return f"{delta.days}d ago"
    if delta.seconds < 60:
        return "just now"
    hours = delta.seconds // 3600
    return f"{hours}h ago" if hours else f"{delta.seconds // 60}m ago"


def _duration(job):
    end = datetime.fromisoformat(job["finished_at"]) if job["finished_at"] else datetime.now()
    seconds = int((end - datetime.fromisoformat(job["started_at"])).total_seconds())
    return f"{seconds // 60}m {seconds % 60:02d}s" if seconds >= 60 else f"{seconds}s"


def _host(url):
    return urlparse(url).netloc.removeprefix("www.") if url else ""


def create_app(root=".", *, hunt_command=None, allowed_hosts=LOOPBACK_HOSTS):
    """`hunt_command` replaces `python hunt.py` (tests use a stub); `allowed_hosts` are the accepted Host names."""
    root = Path(root).resolve()
    app = FastAPI(title="Job hunt dashboard", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    csrf_token = secrets.token_urlsafe(32)
    templates.env.globals.update(
        csrf_token=csrf_token,
        statuses=tracking.STATUSES,
        project_statuses=projects.STATUSES,
        key_for=tracking.key_for,
    )
    templates.env.filters.update(
        ago=_ago,
        host=_host,
        duration=_duration,
        label=lambda v: VERDICT_LABELS.get(v, v or ""),
        mode=lambda m: MODE_LABELS.get(m, "unscored"),
        status=lambda s: STATUS_LABELS.get(s, s),
        job_status=lambda s: JOB_LABELS.get(s, s),
    )
    ingest_lock = threading.Lock()  # one ingest at a time; requests run in a thread pool
    runner = jobs.Runner(root, hunt_command)
    app.state.runner = runner
    app.state.csrf_token = csrf_token
    startup = db.connect(root)
    runner.recover(startup)
    startup.close()

    @app.middleware("http")
    async def guard(request: Request, call_next):
        host = request.headers.get("host", "")
        if urlparse(f"//{host}").hostname not in allowed_hosts:
            return PlainTextResponse("Unknown host.", status_code=400)  # also blocks DNS rebinding
        if request.method not in ("GET", "HEAD"):
            origin = request.headers.get("origin")
            if origin is not None and urlparse(origin).netloc != host:
                return PlainTextResponse("Cross-origin request refused.", status_code=403)
            if not secrets.compare_digest(request.headers.get("x-csrf-token", ""), csrf_token):
                return PlainTextResponse("This page is out of date (the dashboard restarted). Reload it.", status_code=403)
        return await call_next(request)

    @contextmanager
    def connection():
        conn = db.connect(root)
        try:
            with ingest_lock:
                ingest.refresh(conn, root)
            yield conn
        finally:
            conn.close()

    def render(request, name, conn, **context):
        return templates.TemplateResponse(
            request,
            name,
            {
                "latest_run": queries.latest_run(conn),
                "problems": queries.problems(conn),
                "running_job": next((j for j in jobs.recent(conn, 1) if j["status"] == "running"), None),
                **context,
            },
        )

    def redirect(url):
        """Full navigation after an htmx POST."""
        return Response(status_code=204, headers={"HX-Redirect": url})

    # Board, detail, inbox, leads ----------------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def board(request: Request, verdict: str = "apply", q: str = "", min_score: int = 0, sort: str = "score",
              status: str = ""):
        if verdict not in queries.VERDICTS:
            verdict = "apply"
        if status not in ("", "all", *tracking.STATUSES):
            status = ""
        with connection() as conn:
            return render(
                request,
                "board.html",
                conn,
                page="board",
                verdict=verdict,
                q=q,
                min_score=min_score,
                sort=sort,
                status=status,
                counts=queries.verdict_counts(conn, status=status),
                failures=queries.latest_failures(conn),
                cards=queries.board(conn, verdict=verdict, q=q.strip(), min_score=min_score, sort=sort, status=status),
            )

    @app.get("/c/{domain}", response_class=HTMLResponse)
    def company(request: Request, domain: str):
        with connection() as conn:
            detail = queries.company(conn, domain)
            if detail is None:
                raise HTTPException(404, f"No results for {domain}")
            kind, ref = tracking.key_for(detail["result"]["best_role_url"], domain)
            # HTMX loads the slide-over panel only; a direct visit gets the full page.
            name = "partials/company.html" if request.headers.get("HX-Request") else "company.html"
            return render(request, name, conn, page="board", track=tracking.get(conn, kind, ref), **detail)

    @app.post("/track", response_class=HTMLResponse)
    async def track(request: Request):
        form = await request.form()
        with connection() as conn:
            try:
                item = tracking.update(
                    conn, form.get("kind", ""), form.get("ref", ""), status=form.get("status"), notes=form.get("notes")
                )
            except ValueError as exc:
                return PlainTextResponse(str(exc), status_code=400)
            template = "partials/track_select.html" if form.get("variant") == "select" else "partials/track.html"
            return templates.TemplateResponse(request, template, {"track": item})

    @app.get("/inbox", response_class=HTMLResponse)
    def inbox(request: Request, days: int = 14):
        with connection() as conn:
            return render(request, "inbox.html", conn, page="inbox", days=days, day_options=INBOX_DAYS,
                          **queries.inbox(conn, days=days))

    @app.get("/leads", response_class=HTMLResponse)
    def leads(request: Request, status: str = ""):
        with connection() as conn:
            return render(request, "leads.html", conn, page="leads", status=status, **queries.leads(conn, status=status))

    # Pipeline and side projects -----------------------------------------------------------------------------

    @app.get("/pipeline", response_class=HTMLResponse)
    def pipeline(request: Request):
        with connection() as conn:
            return render(request, "pipeline.html", conn, page="pipeline", columns=tracking.pipeline(conn))

    @app.get("/projects", response_class=HTMLResponse)
    def projects_page(request: Request):
        with connection() as conn:
            return render(request, "projects.html", conn, page="projects", columns=projects.board(conn),
                          suggestions=projects.suggestions(conn))

    @app.get("/projects/new", response_class=HTMLResponse)
    def project_new(request: Request):
        return templates.TemplateResponse(request, "partials/project_form.html", {"project": None})

    @app.get("/projects/{project_id}/edit", response_class=HTMLResponse)
    def project_edit(request: Request, project_id: int):
        with connection() as conn:
            project = projects.get(conn, project_id)
        if project is None:
            raise HTTPException(404, "No such side project")
        return templates.TemplateResponse(request, "partials/project_form.html", {"project": project})

    @app.post("/projects")
    async def project_create(request: Request):
        form = await request.form()
        with connection() as conn:
            try:
                projects.create(conn, **{k: form.get(k, "") for k in projects.FIELDS})
            except ValueError as exc:
                return PlainTextResponse(str(exc), status_code=400)
        return redirect("/projects")

    @app.post("/projects/{project_id}")
    async def project_update(request: Request, project_id: int):
        form = await request.form()
        with connection() as conn:
            try:
                projects.update(conn, project_id, status=form.get("status"), **{k: form.get(k) for k in projects.FIELDS})
            except LookupError as exc:
                return PlainTextResponse(str(exc), status_code=404)
            except ValueError as exc:
                return PlainTextResponse(str(exc), status_code=400)
        return redirect("/projects")

    @app.post("/projects/{project_id}/delete")
    def project_delete(project_id: int):
        with connection() as conn:
            try:
                projects.delete(conn, project_id)
            except LookupError as exc:
                return PlainTextResponse(str(exc), status_code=404)
        return redirect("/projects")

    @app.post("/projects/suggestions/{key}")
    async def suggestion(request: Request, key: str):
        form = await request.form()
        with connection() as conn:
            try:
                projects.accept(conn, key, status="dismissed" if form.get("action") == "dismiss" else "idea")
            except LookupError as exc:
                return PlainTextResponse(str(exc), status_code=404)
        return redirect("/projects")

    # Runs ---------------------------------------------------------------------------------------------------

    @app.get("/runs", response_class=HTMLResponse)
    def runs_page(request: Request):
        with connection() as conn:
            return render(
                request,
                "runs.html",
                conn,
                page="runs",
                busy=runner.busy(),
                jobs=jobs.recent(conn),
                saved=queries.saved_runs(conn),
                companies=len(ingest.read_companies(root / ingest.COMPANIES)),
                modes=jobs.RUN_MODES,
            )

    @app.post("/runs/preview", response_class=HTMLResponse)
    async def run_preview(request: Request):
        form = await request.form()
        with connection() as conn:
            try:
                spec = jobs.build(conn, root, form)
                error = None
            except ValueError as exc:
                spec, error = None, str(exc)
            return templates.TemplateResponse(
                request,
                "partials/run_confirm.html",
                {"spec": spec, "error": error, "busy": runner.busy(), "modes": jobs.RUN_MODES},
            )

    @app.post("/runs")
    async def run_start(request: Request):
        form = await request.form()
        with connection() as conn:
            try:
                job_id = runner.start(conn, jobs.build(conn, root, form))
            except ValueError as exc:
                return PlainTextResponse(str(exc), status_code=400)
            except runs.Busy:
                return PlainTextResponse("Another hunt.py command is running; wait for it to finish.", status_code=409)
            except OSError as exc:
                return PlainTextResponse(f"Could not start hunt.py: {exc}", status_code=500)
        return redirect(f"/runs/jobs/{job_id}")

    def job_or_404(conn, job_id):
        job = jobs.get(conn, job_id)
        if job is None:
            raise HTTPException(404, "No such job")
        return job

    @app.get("/runs/jobs/{job_id}", response_class=HTMLResponse)
    def job_page(request: Request, job_id: int):
        with connection() as conn:
            job = job_or_404(conn, job_id)
            return render(request, "job.html", conn, page="runs", job=job, log=runner.read_log(job_id),
                          run=queries.run_summary(conn, job["run_id"]) if job["run_id"] else None)

    @app.get("/runs/jobs/{job_id}/log", response_class=HTMLResponse)
    def job_log(request: Request, job_id: int):
        """Polled every second while the job runs. The first poll that sees it finished reloads the whole page,
        so the top bar and the saved-run summary update too."""
        with connection() as conn:
            job = job_or_404(conn, job_id)
            if job["status"] != "running":
                return Response(status_code=204, headers={"HX-Refresh": "true"})
            return templates.TemplateResponse(
                request, "partials/job_log.html", {"job": job, "log": runner.read_log(job_id), "run": None}
            )

    @app.post("/runs/jobs/{job_id}/cancel")
    def job_cancel(job_id: int):
        if not runner.cancel(job_id):
            return PlainTextResponse("This job is not running.", status_code=409)
        return Response(status_code=204)

    return app
