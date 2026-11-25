import json
import re
import sys
import time

import pytest

from pipeline import runs
from dashboard import db, jobs
from dashboard.app import create_app
from dashboard_data import BASE_URL, client, fit, result, write_run

FIBER_BOX = re.compile(r'<input type="checkbox" name="fiber" value="1"\s*(checked)?\s*>')

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
    assert "Exa: about $0.24 ($0.120 per company, average of 1 saved run without Fiber)" in dry  # $0.12 for 1 result
    assert "Claude: at most about $0.03" in dry and "Telegram" not in dry
    assert "Jev (when TYPESAFE_API_KEY is set): under $0.001 per company" in dry
    research_only = c.post("/runs/preview", data={"kind": "run", "mode": "research_only"}).text
    assert "Claude:" not in research_only and "Jev (" not in research_only

    deliver = c.post("/runs/preview", data={"kind": "run", "mode": "deliver", "domains": ["delta.ai"]}).text
    assert "Run and notify · Delta" in deliver and "Sends the notification to Telegram." in deliver

    assert "unknown companies: nope.io" in c.post("/runs/preview", data={"kind": "run", "domains": ["nope.io"]}).text
    assert "limit must be between 1 and 50" in c.post("/runs/preview", data={"kind": "discover", "limit": "500"}).text


def test_a_fiber_run_passes_the_flag_and_is_estimated_from_saved_runs_with_fiber(root, app):
    c = client(app)

    # Fiber is off unless ticked: the Runs page's box starts unticked, and the dialog's toggle shows what Start runs
    assert FIBER_BOX.findall(c.get("/runs").text) == [""]
    assert FIBER_BOX.findall(c.post("/runs/preview", data={"kind": "run", "domains": ["delta.ai"]}).text) == [""]

    # no saved run with Fiber yet: the usual research, plus Fiber's range from the first runs for each company
    first = c.post("/runs/preview", data={"kind": "run", "mode": "dry_run", "fiber": "1"}).text
    assert "Dry run with Fiber · 2 companies in companies.csv" in first
    assert "python hunt.py run --dry-run --fiber" in first
    assert '<input type="hidden" name="fiber" value="1">' in first  # what Start submits: the run previewed
    assert FIBER_BOX.findall(first) == ["checked"]
    assert "Exa: about $0.24 ($0.120 per company, average of 1 saved run without Fiber)" in first
    assert "Fiber: no saved run with Fiber yet; expect $0.09 to $0.40 more per verified company, up to $0.80 here" in first

    # a saved run with Fiber is averaged on its own, and does not raise the estimate of runs without it
    write_run(root, "20260109-090000", [result("Acme", "acme.com")],
              manifest={"mode": "research_only", "fiber": True, "exa": {"requests": 3, "dollars": 0.30}})
    later = c.post("/runs/preview", data={"kind": "run", "mode": "dry_run", "fiber": "1"}).text
    assert "Exa: about $0.60 ($0.300 per company, average of 1 saved run with Fiber)." in later
    assert "no saved run with Fiber yet" not in later
    assert "average of 1 saved run without Fiber" in c.post("/runs/preview", data={"kind": "run"}).text
    assert "research only · Fiber" in c.get("/runs").text  # the saved run is marked in the history

    # Fiber on a run of selected companies, and off again
    job = finished(root, started_job_id(c.post("/runs", data={"kind": "run", "mode": "research_only", "fiber": "1",
                                                              "domains": ["delta.ai"]})))
    assert job["args"][:3] == ["run", "--research-only", "--fiber"]
    assert job["label"] == "Research only with Fiber · Delta"
    plain = finished(root, started_job_id(c.post("/runs", data={"kind": "run", "mode": "dry_run", "fiber": ""})))
    assert plain["args"] == ["run", "--dry-run"]


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


def test_finished_run_links_to_board_results(root, app):
    write_run(root, "20260110-090000", [
        result("Delta", "delta.ai", fit=fit(80, "apply", "Analytics Engineer", "https://delta.ai/jobs/1")),
    ])
    c = client(app)
    job_id = started_job_id(c.post("/runs", data={"kind": "run", "mode": "dry_run"}))
    assert finished(root, job_id)["status"] == "succeeded"

    page = c.get(f"/runs/jobs/{job_id}").text
    assert 'href="/?sort=recent"' in page
    assert 'href="/inbox' not in page
    assert "Analytics Engineer" in c.get("/?sort=recent").text


def test_exit_code_decides_the_job_status(root, app, monkeypatch):
    c = client(app)
    monkeypatch.setenv("STUB_EXIT", "1")

    run = finished(root, started_job_id(c.post("/runs", data={"kind": "run", "mode": "research_only"})))
    discover = finished(root, started_job_id(c.post("/runs", data={"kind": "discover", "focus": "fintech", "limit": "5"})))

    assert run["status"] == "partial"  # results saved, some companies failed
    assert (discover["status"], discover["args"]) == ("failed", ["discover", "--limit", "5", "fintech"])


def test_discover_focus_cannot_pass_hunt_options(root, app):
    """A focus such as `--history=companies.csv` would otherwise reach hunt.py as an option and make `discover`
    append its 5-column rows to companies.csv."""
    c = client(app)

    preview = c.post("/runs/preview", data={"kind": "discover", "focus": "--history=companies.csv"})
    assert "focus cannot start with a dash" in preview.text
    assert c.post("/runs", data={"kind": "discover", "focus": " -h"}).status_code == 400
    conn = db.connect(root)
    assert jobs.recent(conn, 10) == []
    conn.close()

    job = finished(root, started_job_id(c.post("/runs", data={"kind": "discover", "focus": "fintech"})))
    assert job["args"] == ["discover", "--limit", "15", "fintech"]


def test_email_check_needs_the_outlook_sign_in_and_names_what_claude_sees(root, app):
    c = client(app)
    assert "Sign in to Outlook first" in c.post("/runs/preview", data={"kind": "mail"}).text
    assert c.post("/runs", data={"kind": "mail"}).status_code == 400

    (root / "outlook_token.json").write_text("{}")
    preview = c.post("/runs/preview", data={"kind": "mail"}).text
    assert "first 2,000 characters of each email" in preview
    assert "to Jev (TypeSafe, when TYPESAFE_API_KEY is set)" in preview
    job = finished(root, started_job_id(c.post("/runs", data={"kind": "mail", "dry_run": "1"})))
    assert (job["status"], job["args"]) == ("succeeded", ["mail", "--dry-run"])


def test_the_scheduled_monitor_jobs_run_their_commands_and_say_what_exa_bills(root, app):
    c = client(app)

    sync = c.post("/runs/preview", data={"kind": "monitor_sync"}).text
    assert "python hunt.py monitor-sync" in sync
    assert "Exa then bills every monitor $0.002 a run" in sync and "about $0.36 a month for each company in monitors.csv" in sync
    assert "Creates, updates and deletes monitors on your Exa account to match monitors.csv" in sync
    check = c.post("/runs/preview", data={"kind": "monitor"}).text
    assert "Sends the suitable new postings to Telegram" in check and "one call per new posting" in check
    assert "Telegram" not in c.post("/runs/preview", data={"kind": "monitor", "dry_run": "1"}).text

    synced = finished(root, started_job_id(c.post("/runs", data={"kind": "monitor_sync", "dry_run": "1"})))
    assert synced["args"] == ["monitor-sync"]  # it has no dry run
    checked = finished(root, started_job_id(c.post("/runs", data={"kind": "monitor", "dry_run": "1"})))
    assert (checked["status"], checked["args"]) == ("succeeded", ["monitor", "--dry-run"])


def test_new_monitor_adds_the_company_and_syncs_and_runs_shows_which_have_a_monitor(root, app):
    c = client(app)
    (root / "monitors.csv").write_text("# name,domain\nAcme,acme.com")  # no newline at the end, as an editor may leave it
    (root / "monitor_state.json").write_text(json.dumps(
        {"folder": "f1", "monitors": {"acme.com": {"id": "mon_1", "name": "Acme", "params": {}, "created_at": "2026-11-02T08:00:00Z"}}}))

    form = c.get("/runs/monitors/new").text
    assert "python hunt.py monitor-sync" in form and "about $0.36 a month for each company" in form
    job = finished(root, started_job_id(c.post("/runs/monitors", data={"name": " Beta  Labs ", "domain": "https://www.Beta.io/"})))

    assert (job["kind"], job["args"], job["label"]) == ("monitor_sync", ["monitor-sync"], "New monitor: Beta Labs (beta.io)")
    assert (root / "monitors.csv").read_text() == "# name,domain\nAcme,acme.com\nBeta Labs,beta.io\n"
    assert runs.read_monitors(root / "monitors.csv") == [("Acme", "acme.com"), ("Beta Labs", "beta.io")]
    page = c.get("/runs").text
    assert "Check monitors" not in page and "Sync monitors" not in page and "Resend notification" not in page
    assert re.search(r"Acme</td>\s*<td[^>]*>acme\.com</td>\s*<td[^>]*>on Exa since 2026-11-02</td>", page)
    assert re.search(r"Beta Labs</td>\s*<td[^>]*>beta\.io</td>\s*<td[^>]*>not on Exa yet</td>", page)


@pytest.mark.parametrize(
    "company, message",
    [
        ({"name": "Acme EU", "domain": "www.acme.com"}, "acme.com is monitored already"),
        ({"name": "", "domain": "gamma.dev"}, "needs a name and a domain"),
        ({"name": "Gamma", "domain": "gamma.dev/careers"}, "needs a name and a domain"),
        ({"name": "Gamma", "domain": "localhost"}, "needs a name and a domain"),
        ({"name": "# Gamma", "domain": "gamma.dev"}, "needs a name and a domain"),  # monitors.csv would read a comment
        ({"name": "Gamma", "domain": "gamma.dev,evil.com"}, "needs a name and a domain"),  # no host name
        ({"name": "Gamma", "domain": "gam ma.dev"}, "needs a name and a domain"),
        ({"name": "G" * 200_000, "domain": "gamma.dev"}, "the name at most 100 characters"),  # beyond csv's field limit
    ],
)
def test_new_monitor_refuses_a_company_it_cannot_monitor_and_writes_nothing(root, app, company, message):
    (root / "monitors.csv").write_text("Acme,acme.com\n")

    response = client(app).post("/runs/monitors", data=company)

    assert response.status_code == 400 and message in response.text
    assert (root / "monitors.csv").read_text() == "Acme,acme.com\n"
    conn = db.connect(root)
    assert jobs.recent(conn) == []  # no sync started
    conn.close()


def test_new_monitor_adds_nothing_while_another_command_runs_or_while_monitors_csv_cannot_be_read(root, app):
    c = client(app)
    with runs.lock(root):  # a sync that cannot start must not leave a company listed without a monitor
        assert c.post("/runs/monitors", data={"name": "Beta", "domain": "beta.io"}).status_code == 409
    assert not (root / "monitors.csv").exists()

    (root / "monitors.csv").write_text("Acme\n")  # a row monitor-sync would refuse
    assert "monitors.csv: each company needs a name and a domain" in c.get("/runs").text
    response = c.post("/runs/monitors", data={"name": "Beta", "domain": "beta.io"})
    assert response.status_code == 400 and "monitors.csv: each company needs a name" in response.text
    assert (root / "monitors.csv").read_text() == "Acme\n"


def test_new_monitor_never_starts_a_list_that_would_make_its_sync_delete_the_monitors_running(root, app):
    """monitor-sync deletes the monitor of every company monitors.csv leaves out: a list of the new company alone,
    while monitor_state.json lists others, would delete them and forget the postings they judged."""
    (root / "monitor_state.json").write_text(json.dumps({"folder": "f1", "monitors": {
        "acme.com": {"id": "mon_1", "name": "Acme", "params": {}}, "beta.io": {"id": "mon_2", "name": "Beta", "params": {}}}}))
    c = client(app)

    assert "monitors.csv not found, and 2 monitor(s) of this folder run on Exa" in c.get("/runs").text
    response = c.post("/runs/monitors", data={"name": "Gamma", "domain": "gamma.dev"})
    assert response.status_code == 400 and "create it with a name,domain row per company to keep" in response.text
    assert not (root / "monitors.csv").exists()

    (root / "monitors.csv").write_text("Acme,acme.com\nBeta,beta.io\n")  # the list the sync keeps them for
    finished(root, started_job_id(c.post("/runs/monitors", data={"name": "Gamma", "domain": "gamma.dev"})))
    assert runs.read_monitors(root / "monitors.csv") == [("Acme", "acme.com"), ("Beta", "beta.io"), ("Gamma", "gamma.dev")]


def test_edit_changes_a_monitored_company_and_syncs(root, app):
    c = client(app)
    (root / "monitors.csv").write_text("# name,domain\nAcme,acme.com\nBeta,beta.io\n")

    form = c.get("/runs/monitors/edit", params={"domain": "beta.io"}).text
    assert 'hx-post="/runs/monitors/edit"' in form and 'name="current" value="beta.io"' in form
    assert 'name="name" required maxlength="100" placeholder="e.g. Acme" value="Beta"' in form and "Save and sync" in form
    renamed = finished(root, started_job_id(c.post("/runs/monitors/edit", data={"current": "beta.io", "name": "Beta Labs",
                                                                                "domain": "beta.io"})))
    moved = finished(root, started_job_id(c.post("/runs/monitors/edit", data={  # a typo in the domain
        "current": "acme.com", "name": "Acme", "domain": "https://www.Acme.io/"})))

    assert [(job["kind"], job["args"], job["label"]) for job in (renamed, moved)] == [
        ("monitor_sync", ["monitor-sync"], "Edit monitor: Beta Labs (beta.io)"),
        ("monitor_sync", ["monitor-sync"], "Edit monitor: Acme (acme.io)")]
    assert (root / "monitors.csv").read_text() == "# name,domain\nAcme,acme.io\nBeta Labs,beta.io\n"
    missing = c.get("/runs/monitors/edit", params={"domain": "acme.com"})
    assert (missing.status_code, missing.text) == (404, "acme.com is not in monitors.csv")


@pytest.mark.parametrize(
    "change, message",
    [
        ({"current": "gamma.dev", "name": "Gamma", "domain": "gamma.dev"}, "gamma.dev is not in monitors.csv"),
        ({"current": "acme.com", "name": "Acme", "domain": "www.beta.io"}, "beta.io is monitored already"),
        ({"current": "acme.com", "name": " Acme ", "domain": "https://acme.com"}, "Nothing to change"),
        ({"current": "acme.com", "name": "", "domain": "acme.com"}, "needs a name and a domain"),
        ({"current": "acme.com", "name": "Acme", "domain": "acme.com/jobs"}, "needs a name and a domain"),
    ],
)
def test_edit_refuses_a_change_it_cannot_make_and_writes_nothing(root, app, change, message):
    (root / "monitors.csv").write_text("Acme,acme.com\nBeta,beta.io\n")

    response = client(app).post("/runs/monitors/edit", data=change)

    assert response.status_code == 400 and message in response.text
    assert (root / "monitors.csv").read_text() == "Acme,acme.com\nBeta,beta.io\n"
    conn = db.connect(root)
    assert jobs.recent(conn) == []  # no sync started
    conn.close()


def test_remove_confirms_the_companies_picked_then_removes_them_and_syncs(root, app):
    c = client(app)
    (root / "monitors.csv").write_text("# name,domain\nAcme,acme.com\nBeta,beta.io\nGamma,gamma.dev\n")

    confirm = c.get("/runs/monitors/remove", params={"domains": ["gamma.dev", "acme.com"]}).text
    assert "<h2>Remove 2 monitors</h2>" in confirm and "python hunt.py monitor-sync" in confirm
    assert "Removes Acme (acme.com), Gamma (gamma.dev) from monitors.csv." in confirm
    assert "The sync deletes their monitors on Exa, which stops the billing" in confirm
    assert re.search(r'hx-post="/runs/monitors/remove">\s*<input type="hidden" name="domains" value="acme.com">\s*'
                     r'<input type="hidden" name="domains" value="gamma.dev">', confirm)
    assert "Remove and sync" in confirm
    assert runs.read_monitors(root / "monitors.csv") == [("Acme", "acme.com"), ("Beta", "beta.io"), ("Gamma", "gamma.dev")]

    several = finished(root, started_job_id(c.post("/runs/monitors/remove", data={"domains": ["acme.com", "gamma.dev"]})))
    assert (several["args"], several["label"]) == (["monitor-sync"], "Remove 2 monitors")
    assert (root / "monitors.csv").read_text() == "# name,domain\nBeta,beta.io\n"
    one = finished(root, started_job_id(c.post("/runs/monitors/remove", data={"domains": ["beta.io"]})))
    assert one["label"] == "Remove monitor: Beta (beta.io)"
    assert (root / "monitors.csv").read_text() == "# name,domain\n"
    assert "No companies monitored yet." in c.get("/runs").text


def test_remove_refuses_companies_it_does_not_list_and_nothing_changes_while_another_command_runs(root, app):
    c = client(app)
    (root / "monitors.csv").write_text("Acme,acme.com\nBeta,beta.io\n")

    assert "Not in monitors.csv: nope.io" in c.get("/runs/monitors/remove", params={"domains": ["acme.com", "nope.io"]}).text
    response = c.post("/runs/monitors/remove", data={"domains": ["acme.com", "nope.io"]})
    assert response.status_code == 400 and "Not in monitors.csv: nope.io" in response.text
    assert c.post("/runs/monitors/remove", data={}).text == "Pick the companies to remove."
    with runs.lock(root):  # the list changes only with the sync that makes Exa match it
        assert c.post("/runs/monitors/remove", data={"domains": ["acme.com"]}).status_code == 409
        assert c.post("/runs/monitors/edit", data={"current": "acme.com", "name": "Acme", "domain": "acme.io"}).status_code == 409

    assert (root / "monitors.csv").read_text() == "Acme,acme.com\nBeta,beta.io\n"
    conn = db.connect(root)
    assert jobs.recent(conn) == []
    conn.close()


def test_runs_lists_the_first_ten_monitored_companies_and_the_rest_behind_show_all(root, app):
    c = client(app)
    companies = [(f"Company {i:02}", f"company{i:02}.com") for i in range(1, 13)]
    (root / "monitors.csv").write_text("".join(f"{name},{domain}\n" for name, domain in companies))

    page = c.get("/runs").text
    first, rest = page.split("<h2>Monitors")[1].split('<tbody id="more-monitors" hidden>')
    assert [d for _, d in companies if f">{d}<" in first] == [d for _, d in companies[:10]]
    assert [d for _, d in companies if f">{d}<" in rest.split("</tbody>")[0]] == ["company11.com", "company12.com"]
    assert 'aria-controls="more-monitors" aria-expanded="false"' in page and ">Show all 12</button>" in page
    # Hidden or not, every company can be picked for Remove and edited.
    assert page.count('name="domains" value="company') == 12
    assert page.count('hx-get="/runs/monitors/edit?domain=company') == 12

    (root / "monitors.csv").write_text("".join(f"{name},{domain}\n" for name, domain in companies[:10]))
    page = c.get("/runs").text
    assert 'id="more-monitors"' not in page and "Show all" not in page
    assert page.count('name="domains" value="company') == 10


def test_one_command_at_a_time(root, app, monkeypatch):
    c = client(app)
    with runs.lock(root):  # e.g. a hunt.py command from a shell
        response = c.post("/runs", data={"kind": "news", "dry_run": "1"})
        assert response.status_code == 409
        assert "A hunt.py command is running" in c.get("/runs").text

    monkeypatch.setenv("STUB_SLEEP", "30")
    job_id = started_job_id(c.post("/runs", data={"kind": "news", "dry_run": "1"}))
    assert c.post("/runs", data={"kind": "news"}).status_code == 409

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
    form = {"kind": "news", "dry_run": "1"}

    assert c.post("/runs", data=form, headers={"X-CSRF-Token": "stale"}).status_code == 403
    assert c.post("/runs/monitors", data={"name": "Beta", "domain": "beta.io"},
                  headers={"Origin": "https://evil.example"}).status_code == 403
    assert c.post("/runs", data=form, headers={"Origin": "https://evil.example"}).status_code == 403
    (root / "monitors.csv").write_text("Acme,acme.com\n")
    assert c.post("/runs/monitors/edit", data={"current": "acme.com", "name": "Acme", "domain": "acme.io"},
                  headers={"Origin": "https://evil.example"}).status_code == 403
    assert c.post("/runs/monitors/remove", data={"domains": ["acme.com"]}, headers={"X-CSRF-Token": "stale"}).status_code == 403
    assert (root / "monitors.csv").read_text() == "Acme,acme.com\n"
    assert c.post("/track", data={"kind": "company", "ref": "acme.com", "status": "saved"},
                  headers={"X-CSRF-Token": ""}).status_code == 403
    assert c.get("/", headers={"Host": "evil.example"}).status_code == 400  # DNS rebinding
    assert c.post("/runs", data=form).status_code == 204  # same origin is fine
    del c.headers["Origin"]  # a script that holds the token: no page of the dashboard sent this
    assert c.post("/track", data={"kind": "company", "ref": "acme.com", "status": "saved"}).status_code == 403
