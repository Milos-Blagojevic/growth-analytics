import sys
import time

import pytest

import runs
from dashboard import db, jobs
from dashboard.app import create_app
from dashboard_data import BASE_URL, client, fit, result, write_run

# Stands in for hunt.py: echoes its arguments, can sleep, prints the line a saving run prints, exits as told.
STUB = """
import json, os, sys, time
print("args:", json.dumps(sys.argv[1:]))
if os.environ.get("STUB_SLEEP"):
    time.sleep(float(os.environ["STUB_SLEEP"]))
if sys.argv[1] == "run":
    print("Saved raw results to out/20260110-090000/")
sys.exit(int(os.environ.get("STUB_EXIT", "0")))
"""


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.delenv("STUB_SLEEP", raising=False)
    monkeypatch.delenv("STUB_EXIT", raising=False)
    write_run(
        tmp_path,
        "20260108-090000",
        [result("Acme", "acme.com", fit=fit(82, "apply", "AE", "https://acme.com/1"))],
        manifest={"mode": "dry_run", "saved_at": "2026-01-08T09:00:00", "companies": 4, "errors": 0, "exa": {"requests": 20, "dollars": 0.12}},
    )
    (tmp_path / "companies.csv").write_text("# name,domain\nAcme,acme.com\nBeta,beta.io\n")
    (tmp_path / "discovered.csv").write_text(
        "discovered_at,name,domain,why,focus\n2026-01-01T08:00:00,Delta,delta.ai,Growth team,\n2026-01-01T08:00:00,Echo,echo.so,Data team,\n"
    )
    (tmp_path / "stub_hunt.py").write_text(STUB)
    return tmp_path


@pytest.fixture
def app(root):
    return create_app(root, hunt_command=[sys.executable, str(root / "stub_hunt.py")])


def finished(root, job_id, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        conn = db.connect(root)
        job = jobs.get(conn, job_id)
        conn.close()
        if job["status"] != "running":
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} still running")


def started_job_id(response):
    assert response.status_code == 204, response.text
    return int(response.headers["HX-Redirect"].rsplit("/", 1)[1])


def test_preview_estimates_cost_from_saved_runs_and_names_side_effects(app):
    c = client(app)

    dry = c.post("/runs/preview", data={"kind": "run", "mode": "dry_run"}).text
    assert "2 companies in companies.csv" in dry
    assert "Exa: about $0.24 ($0.120 per company, average of 1 saved run)" in dry  # $0.12 for 1 saved result
    assert "Claude: about $0.03" in dry and "Telegram" not in dry

    deliver = c.post("/runs/preview", data={"kind": "run", "mode": "deliver", "domains": ["delta.ai"]}).text
    assert "Run and notify · Delta" in deliver and "Sends the notification to Telegram." in deliver

    assert "unknown companies: nope.io" in c.post("/runs/preview", data={"kind": "run", "domains": ["nope.io"]}).text
    assert "limit must be between 1 and 50" in c.post("/runs/preview", data={"kind": "discover", "limit": "500"}).text


def test_run_on_selected_leads_runs_hunt_with_a_companies_file(root, app):
    c = client(app)

    job_id = started_job_id(c.post("/runs", data={"kind": "run", "mode": "dry_run", "domains": ["delta.ai", "echo.so"]}))
    job = finished(root, job_id)

    csv_path = root / "jobs" / f"{job_id}-companies.csv"
    assert job["args"] == ["run", "--dry-run", "--companies", str(csv_path)]
    assert csv_path.read_text().splitlines() == ["Delta,delta.ai", "Echo,echo.so"]
    assert (job["status"], job["exit_code"], job["run_id"]) == ("succeeded", 0, "20260110-090000")
    page = c.get(f"/runs/jobs/{job_id}").text
    assert "args: [" in page and f"{job_id}-companies.csv" in page and "done" in page  # the log is shown


def test_exit_code_decides_the_job_status(root, app, monkeypatch):
    c = client(app)
    monkeypatch.setenv("STUB_EXIT", "1")

    run = finished(root, started_job_id(c.post("/runs", data={"kind": "run", "mode": "research_only"})))
    discover = finished(root, started_job_id(c.post("/runs", data={"kind": "discover", "focus": "fintech", "limit": "5"})))

    assert run["status"] == "partial"  # results saved, some companies failed
    assert (discover["status"], discover["args"]) == ("failed", ["discover", "fintech", "--limit", "5"])


def test_one_command_at_a_time(root, app, monkeypatch):
    c = client(app)
    with runs.lock(root):  # e.g. a cron `hunt.py watch`
        response = c.post("/runs", data={"kind": "watch", "dry_run": "1"})
        assert response.status_code == 409
        assert "A hunt.py command is running" in c.get("/runs").text

    monkeypatch.setenv("STUB_SLEEP", "30")
    job_id = started_job_id(c.post("/runs", data={"kind": "watch", "dry_run": "1"}))
    assert c.post("/runs", data={"kind": "watch"}).status_code == 409

    assert c.post(f"/runs/jobs/{job_id}/cancel").status_code == 204
    assert finished(root, job_id)["status"] == "cancelled"
    assert c.post(f"/runs/jobs/{job_id}/cancel").status_code == 409


def test_jobs_left_running_by_a_previous_process_are_marked_interrupted(root):
    conn = db.connect(root)
    with conn:
        conn.execute(
            "INSERT INTO jobs (kind, args, label, status, started_at) VALUES ('run', '[]', 'x', 'running', '2026-01-01T00:00:00')"
        )
    conn.close()

    create_app(root)

    conn = db.connect(root)
    assert conn.execute("SELECT status FROM jobs").fetchone()[0] == "interrupted"
    conn.close()


def test_requests_from_other_sites_are_refused(root, app):
    c = client(app)
    form = {"kind": "watch", "dry_run": "1"}

    assert c.post("/runs", data=form, headers={"X-CSRF-Token": "stale"}).status_code == 403
    assert c.post("/runs", data=form, headers={"Origin": "https://evil.example"}).status_code == 403
    assert c.post("/track", data={"kind": "company", "ref": "acme.com", "status": "saved"},
                  headers={"X-CSRF-Token": ""}).status_code == 403
    assert c.get("/", headers={"Host": "evil.example"}).status_code == 400  # DNS rebinding
    assert c.post("/runs", data=form, headers={"Origin": BASE_URL}).status_code == 204  # same origin is fine
