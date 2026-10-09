"""Read-only dashboard over the job-hunt pipeline's saved files (served locally by `python -m dashboard`)."""

import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from dashboard import db, ingest, queries

HERE = Path(__file__).parent
VERDICT_LABELS = {
    "apply": "Apply",
    "consider": "Consider",
    "stretch": "Stretch",
    "check_careers": "Check careers",
    "skip": "Skip",
}
MODE_LABELS = {"research_only": "research only", "dry_run": "dry run", "deliver": "sent to Telegram"}
INBOX_DAYS = (7, 14, 30, 0)


def _ago(iso):
    if not iso:
        return ""
    delta = datetime.now() - datetime.fromisoformat(iso)
    if delta.days >= 1:
        return f"{delta.days}d ago"
    hours = delta.seconds // 3600
    return f"{hours}h ago" if hours else f"{max(delta.seconds // 60, 1)}m ago"


def _host(url):
    return urlparse(url).netloc.removeprefix("www.") if url else ""


def create_app(root="."):
    root = Path(root).resolve()
    app = FastAPI(title="Job hunt dashboard", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.filters.update(
        ago=_ago,
        host=_host,
        label=lambda v: VERDICT_LABELS.get(v, v or ""),
        mode=lambda m: MODE_LABELS.get(m, "unscored"),
    )
    lock = threading.Lock()  # one ingest at a time; requests run in a thread pool

    @contextmanager
    def connection():
        conn = db.connect(root)
        try:
            with lock:
                ingest.refresh(conn, root)
            yield conn
        finally:
            conn.close()

    def render(request, name, conn, **context):
        return templates.TemplateResponse(
            request,
            name,
            {"latest_run": queries.latest_run(conn), "problems": queries.problems(conn), **context},
        )

    @app.get("/", response_class=HTMLResponse)
    def board(request: Request, verdict: str = "apply", q: str = "", min_score: int = 0, sort: str = "score"):
        if verdict not in queries.VERDICTS:
            verdict = "apply"
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
                counts=queries.verdict_counts(conn),
                failures=queries.latest_failures(conn),
                cards=queries.board(conn, verdict=verdict, q=q.strip(), min_score=min_score, sort=sort),
            )

    @app.get("/c/{domain}", response_class=HTMLResponse)
    def company(request: Request, domain: str):
        with connection() as conn:
            detail = queries.company(conn, domain)
            if detail is None:
                raise HTTPException(404, f"No results for {domain}")
            # HTMX loads the slide-over panel only; a direct visit gets the full page.
            name = "partials/company.html" if request.headers.get("HX-Request") else "company.html"
            return render(request, name, conn, page="board", **detail)

    @app.get("/inbox", response_class=HTMLResponse)
    def inbox(request: Request, days: int = 14):
        with connection() as conn:
            return render(request, "inbox.html", conn, page="inbox", days=days, day_options=INBOX_DAYS,
                          **queries.inbox(conn, days=days))

    @app.get("/leads", response_class=HTMLResponse)
    def leads(request: Request, status: str = ""):
        with connection() as conn:
            return render(request, "leads.html", conn, page="leads", status=status, **queries.leads(conn, status=status))

    return app
