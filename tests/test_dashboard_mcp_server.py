import json
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta

import pytest
import uvicorn
from fastapi.testclient import TestClient

from clients import posthog_client
from dashboard import __main__ as entry
from dashboard import db, mcp_server, queries
from dashboard.app import create_app
from dashboard_data import (BASE_URL, INTERVIEW_TURNS, PREP, fit, mail_update, result, review_fields, wispr,
                            write_interview, write_mail, write_run)
from prospect_data import SETTINGS, assessment, figure, research
from prospect_data import write_run as write_prospect_run
from prospects import warehouse
from test_dashboard_jobs import STUB, finished

TOKEN = "mcp-" + "k" * 40
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/json, text/event-stream"}
READ_ONLY = {"overview", "list_companies", "get_company", "list_runs", "spend_summary", "list_leads", "get_pipeline",
             "pipeline_funnel", "list_email_suggestions", "list_interviews", "get_interview", "interview_progress",
             "list_projects", "list_jobs", "get_job", "prospect_ranking", "get_prospect", "preview_job"}
CHANGES = {"set_status", "decide_email_suggestion", "queue_leads", "create_project", "update_project",
           "create_interview", "start_job", "cancel_job"}


class Refused(Exception):
    """A tool answered with an error: the message the client shows."""


def run_id(days_ago):
    when = datetime.now() - timedelta(days=days_ago)
    return when.strftime("%Y%m%d-%H%M%S"), when.isoformat(timespec="seconds")


@pytest.fixture
def root(tmp_path, monkeypatch):
    """Acme: consider 40 days ago, apply now. Beta stretch with a side-project idea, Gamma check careers, Broken
    failed in the latest run. Delta is a lead, Acme is in companies.csv. One reviewed interview."""
    monkeypatch.delenv("STUB_SLEEP", raising=False)
    monkeypatch.delenv("STUB_EXIT", raising=False)
    write_run(tmp_path, run_id(40)[0], [result("Acme", "acme.com", fit=fit(64, "consider", "Data Analyst",
                                                                          "https://acme.com/1"))])
    latest, saved_at = run_id(2)
    write_run(tmp_path, latest, [
        result("Acme", "acme.com", fit=fit(84, "apply", "Analytics Engineer", "https://acme.com/2"),
               roles=[("Analytics Engineer", "https://acme.com/2", "live")]),
        result("Beta", "beta.io", fit=fit(70, "stretch", "Growth Engineer", "https://beta.io/g", gap_note="6 months.",
                                          proof_ideas=["Build an A/B harness"])),
        result("Gamma", "gamma.dev", fit=fit(58, "check_careers")),
        result("Broken", "broken.dev", error="Exa timed out"),
    ], manifest={"mode": "deliver", "saved_at": saved_at, "companies": 4, "errors": 1,
                 "exa": {"requests": 9, "dollars": 0.05},
                 "claude": {"model": "claude-test", "input_tokens": 9000, "output_tokens": 1200}})
    (tmp_path / "companies.csv").write_text("# name,domain\nAcme,acme.com\n")
    (tmp_path / "discovered.csv").write_text(
        "discovered_at,name,domain,why,focus\n2026-01-01T08:00:00,Delta,delta.ai,Growth team,\n")
    write_interview(tmp_path, transcript=wispr(*INTERVIEW_TURNS), me="2", prep=PREP, review=review_fields(4))
    (tmp_path / "stub_hunt.py").write_text(STUB)
    return tmp_path


def make_app(root, **options):
    return create_app(root, hunt_command=[sys.executable, str(root / "stub_hunt.py")], mcp_token=TOKEN, **options)


@contextmanager
def served(app):
    """A client of the running app: with its lifespan, which runs the MCP endpoint's sessions."""
    with TestClient(app, base_url=BASE_URL) as c:
        yield c


def rpc(c, method, params=None, headers=HEADERS):
    return c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}, headers=headers)


def call(c, tool, **arguments):
    """What the tool answered, or Refused with its message."""
    response = rpc(c, "tools/call", {"name": tool, "arguments": arguments})
    assert response.status_code == 200, response.text
    answer = response.json()["result"]
    text = answer["content"][0]["text"]
    if answer["isError"]:
        raise Refused(text)
    return json.loads(text)


# The endpoint and its guard ---------------------------------------------------------------------------------------

def test_only_the_bearer_token_opens_the_endpoint_and_it_opens_nothing_else(root):
    app = make_app(root, password="correct horse battery")
    with served(app) as c:
        listing = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        refused = c.post("/mcp", json=listing, headers={"Accept": HEADERS["Accept"]})
        assert (refused.status_code, refused.headers["WWW-Authenticate"]) == (401, "Bearer")
        assert rpc(c, "tools/list", headers=HEADERS | {"Authorization": f"Bearer {TOKEN[:-1]}"}).status_code == 401
        assert rpc(c, "tools/list", headers=HEADERS | {"Authorization": f"Basic {TOKEN}"}).status_code == 401

        # A logged-in browser holds the cookie and the page's CSRF token: neither stands in for the MCP token.
        assert c.post("/login", data={"password": "correct horse battery"},
                      headers={"Origin": BASE_URL, "X-CSRF-Token": app.state.csrf_token}).status_code == 204
        browser = {"Accept": HEADERS["Accept"], "Origin": BASE_URL, "X-CSRF-Token": app.state.csrf_token}
        assert c.post("/mcp", json=listing, headers=browser).status_code == 401
        c.cookies.clear()

        assert rpc(c, "tools/list", headers=HEADERS | {"Origin": "https://evil.example"}).status_code == 403
        assert rpc(c, "tools/list", headers=HEADERS | {"Host": "evil.example"}).status_code == 400
        assert c.get("/mcp", headers=HEADERS).status_code == 405
        assert rpc(c, "tools/list", headers=HEADERS | {"Origin": BASE_URL}).status_code == 200  # its own pages
        tools = rpc(c, "tools/list").json()["result"]["tools"]  # no login needed
        assert {t["name"] for t in tools} == READ_ONLY | CHANGES

        # The token opens /mcp only: the pages still want the login.
        page = c.get("/", headers=HEADERS, follow_redirects=False)
        assert (page.status_code, page.headers["location"]) == (303, "/login?next=/")

    without = create_app(root)  # no MCP_TOKEN: no endpoint
    with served(without) as c:
        assert c.get("/mcp", headers=HEADERS).status_code == 404
        assert rpc(c, "tools/list").status_code == 403  # a POST like any other: no CSRF token


def test_the_dashboard_serves_the_endpoint_only_with_a_long_enough_token(tmp_path, monkeypatch):
    served_apps = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: served_apps.append(app))
    monkeypatch.setattr(sys, "argv", ["dashboard", "--root", str(tmp_path)])
    monkeypatch.delenv("HUNT_SCHEDULE", raising=False)
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)

    monkeypatch.setenv("MCP_TOKEN", "x" * (mcp_server.MIN_TOKEN_LENGTH - 1))
    with pytest.raises(SystemExit):
        entry.main()
    assert served_apps == []

    monkeypatch.setenv("MCP_TOKEN", TOKEN)
    entry.main()
    with served(served_apps[0]) as c:
        assert call(c, "overview")["board"] == dict.fromkeys(queries.VERDICTS, 0)


def test_a_client_asks_before_every_tool_that_changes_something_and_before_a_job_above_all(root):
    """Clients run tools marked read-only without asking: only reads may carry the mark."""
    with served(make_app(root)) as c:
        tools = {t["name"]: t["annotations"] for t in rpc(c, "tools/list").json()["result"]["tools"]}
    assert {name for name, hints in tools.items() if hints["readOnlyHint"]} == READ_ONLY
    assert tools["start_job"]["destructiveHint"] and tools["start_job"]["openWorldHint"]
    assert not any(hints["openWorldHint"] for name, hints in tools.items() if name != "start_job")


# Reads ----------------------------------------------------------------------------------------------------------

def test_the_reads_show_what_the_pages_show(root):
    with served(make_app(root)) as c:
        overview = call(c, "overview")
        assert overview["board"] == {"apply": 1, "consider": 0, "stretch": 1, "check_careers": 1, "skip": 0}
        assert (overview["latest_run"]["errors"], overview["running_job"]) == (1, None)
        assert overview["upcoming_interviews"] == 0

        board = call(c, "list_companies")
        assert [(r["name"], r["verdict"]) for r in board["companies"]] == [
            ("Acme", "apply"), ("Beta", "stretch"), ("Gamma", "check_careers")]  # the board's tab order
        assert board["companies"][0]["track"] == {"kind": "posting", "ref": "https://acme.com/2"}
        assert board["companies"][2]["track"] == {"kind": "company", "ref": "gamma.dev"}  # no posting
        assert [r["name"] for r in call(c, "list_companies", verdict="stretch")["companies"]] == ["Beta"]
        assert call(c, "list_companies", query="growth")["total"] == 1
        with pytest.raises(Refused, match="Input should be"):
            call(c, "list_companies", verdict="maybe")

        acme = call(c, "get_company", domain="https://www.Acme.com/jobs")  # spelled as run spells it
        assert (acme["result"]["score"], [h["verdict"] for h in acme["history"]]) == (84, ["consider", "apply"])
        assert acme["roles"][0]["link_status"] == "live" and acme["tracking"]["status"] == "new"
        with pytest.raises(Refused, match="No results for nope.com"):
            call(c, "get_company", domain="nope.com")

        runs = call(c, "list_runs")
        assert [r["errors"] for r in runs["runs"]] == [1, 0]
        assert runs["failed_in_latest_run"] == [{"domain": "broken.dev", "name": "Broken", "error": "Exa timed out"}]
        spend = call(c, "spend_summary", days=7)
        assert [r["exa_dollars"] for r in spend["runs"]] == [0.05]  # the run 40 days ago is older
        assert spend["totals"]["claude_tokens"] == {"claude-test": {"input": 9000, "output": 1200}}

        assert [(i["domain"], i["status"]) for i in call(c, "list_leads")["items"]] == [("delta.ai", "new")]
        assert call(c, "list_projects")["suggestions"][0]["text"] == "Build an A/B harness"

        (past,) = call(c, "list_interviews")["past"]
        assert (past["company"], past["has_review"], past["overall"]) == ("SE Ranking", True, 4.0)
        interview = call(c, "get_interview", interview_id=past["id"])
        assert interview["prep"]["prep"]["positioning"] == PREP["positioning"]
        assert interview["transcript"]["turn_count"] == len(INTERVIEW_TURNS)
        assert "turns" not in interview["transcript"]  # never the words said
        assert call(c, "interview_progress")["interviews"][0]["scores"]["structure"] == 4
        with pytest.raises(Refused, match="No such interview"):
            call(c, "get_interview", interview_id="../../hunt")


def test_the_funnel_counts_applications_by_status_and_by_the_week_they_were_sent(root):
    with served(make_app(root)) as c:
        assert call(c, "pipeline_funnel")["response_rate"] is None  # nothing applied yet
        call(c, "set_status", kind="posting", ref="https://acme.com/2", status="interviewing")
        call(c, "set_status", kind="posting", ref="https://beta.io/g", status="applied")
        call(c, "set_status", kind="company", ref="gamma.dev", status="saved")

        funnel = call(c, "pipeline_funnel", weeks=2)
    assert {k: v for k, v in funnel["by_status"].items() if v} == {"saved": 1, "applied": 1, "interviewing": 1}
    assert (funnel["applications"], funnel["responses"], funnel["response_rate"]) == (2, 1, 0.5)
    assert [week["applications"] for week in funnel["applications_per_week"]] == [0, 2]  # both sent this week


def test_prospects_come_from_the_warehouse_when_there_is_one(root):
    with served(make_app(root)) as c:
        with pytest.raises(Refused, match="No prospects.duckdb"):
            call(c, "prospect_ranking")
        write_prospect_run(root, "20261101-090000", [
            {"name": "Acme", "domain": "acme.com", "research": research("Acme", "acme.com"),
             "assessment": assessment(revenue_figures=[figure(9_000_000.0)])},
            {"name": "Small", "domain": "small.com", "research": research("Small", "small.com"),
             "assessment": assessment(headcount_low=20, headcount_high=20)},
        ])
        warehouse.build(root / "prospect_runs", root / mcp_server.PROSPECTS_DB, SETTINGS)

        assert [p["domain"] for p in call(c, "prospect_ranking")["prospects"]] == ["acme.com", "small.com"]
        acme = call(c, "get_prospect", domain="https://acme.com/")
    assert acme["prospect"]["name"] == "Acme" and acme["prospect"]["tier"]
    assert [(f["kind"], f["amount_usd"], f["used"]) for f in acme["figures"]] == [("revenue", 9_000_000.0, True)]
    assert acme["facts"] and acme["news"]


# Changes to the owner's data -----------------------------------------------------------------------------------

def test_changes_land_where_the_pages_read_them(root):
    with served(make_app(root)) as c:
        call(c, "set_status", kind="posting", ref="https://acme.com/2", status="applied", notes="Referred by Ana")
        noted = call(c, "set_status", kind="posting", ref="https://acme.com/2", notes="Call on Friday")
        assert noted["status"] == "applied"
        with pytest.raises(Refused, match="give a status, notes or both"):
            call(c, "set_status", kind="posting", ref="https://acme.com/2")
        with pytest.raises(Refused, match="Input should be"):
            call(c, "set_status", kind="posting", ref="https://acme.com/2", status="ghosted")
        (applied,) = call(c, "get_pipeline")["applied"]
        assert (applied["name"], applied["notes"]) == ("Acme", "Call on Friday")  # notes replace the old ones
        assert "Call on Friday" in c.get("/pipeline").text

        write_mail(root, "20261015-090000", [mail_update(1, "rejected", "Acme"), mail_update(2, "applied", "Zeta")])
        rejection, confirmation = sorted(call(c, "list_email_suggestions")["suggestions"], key=lambda s: s["company"])
        assert [(t["kind"], t["ref"]) for t in rejection["targets"]] == [("posting", "https://acme.com/2")]
        with pytest.raises(Refused, match="does not match that application"):
            call(c, "decide_email_suggestion", update_id=rejection["id"], action="apply", kind="company",
                 ref="gamma.dev")
        call(c, "decide_email_suggestion", update_id=rejection["id"], action="apply", kind="posting",
             ref="https://acme.com/2")
        call(c, "decide_email_suggestion", update_id=confirmation["id"], action="add")
        with pytest.raises(Refused, match="already handled"):
            call(c, "decide_email_suggestion", update_id=rejection["id"], action="dismiss")
        pipeline = call(c, "get_pipeline")
        assert [i["name"] for i in pipeline["rejected"]] == ["Acme"]
        assert [i["kind"] for i in pipeline["applied"]] == ["email"]

        before = (root / "companies.csv").read_text()
        with pytest.raises(Refused, match="Unknown companies: nope.com"):
            call(c, "queue_leads", domains=["delta.ai", "nope.com"])
        assert (root / "companies.csv").read_text() == before  # nothing appended blind
        assert call(c, "queue_leads", domains=["delta.ai"])["added"] == [{"name": "Delta", "domain": "delta.ai"}]
        assert call(c, "queue_leads", domains=["delta.ai"])["added"] == []  # listed already
        assert (root / "companies.csv").read_text() == before + "Delta,delta.ai\n"

        project = call(c, "create_project", title="dbt package", gap="dbt")
        assert call(c, "update_project", project_id=project["id"], status="active")["title"] == "dbt package"
        assert [p["title"] for p in call(c, "list_projects")["projects"]["active"]] == ["dbt package"]
        with pytest.raises(Refused, match="no side project 999"):
            call(c, "update_project", project_id=999, status="done")
        with pytest.raises(Refused, match="a side project needs a title"):
            call(c, "create_project", title="  ")

        made = call(c, "create_interview", company="Zeta", role="Data Lead", stage="technical",
                    scheduled_at="2099-01-05T10:00")
        assert [i["id"] for i in call(c, "list_interviews")["upcoming"]] == [made["id"]]
        with pytest.raises(Refused, match="the date must look like"):
            call(c, "create_interview", company="Zeta", role="Data Lead", scheduled_at="next Tuesday")


# Jobs -----------------------------------------------------------------------------------------------------------

def jobs_started(root):
    conn = db.connect(root)
    try:
        return [dict(r) for r in conn.execute("SELECT id, kind, status FROM jobs ORDER BY id")]
    finally:
        conn.close()


def test_a_job_starts_only_from_a_preview_and_each_preview_starts_one(root, monkeypatch):
    with served(make_app(root)) as c:
        preview = call(c, "preview_job", kind="run", mode="deliver", domains=["acme.com", "delta.ai"])
        assert preview["command"] == "python hunt.py run --companies jobs/<id>-companies.csv"
        assert preview["companies"] == [{"name": "Acme", "domain": "acme.com"}, {"name": "Delta", "domain": "delta.ai"}]
        assert "Sends the notification to Telegram." in preview["warnings"] and preview["estimate"]
        with pytest.raises(Refused, match="unknown companies: nope.com"):
            call(c, "preview_job", kind="run", domains=["nope.com"])
        assert jobs_started(root) == []  # a preview starts nothing

        with pytest.raises(Refused, match="Unknown or expired token"):
            call(c, "start_job", token="made-up")
        job = call(c, "start_job", token=preview["token"])
        assert finished(root, job["job_id"])["status"] == "succeeded"
        log = call(c, "get_job", job_id=job["job_id"])["log"]
        assert 'args: ["run", "--companies"' in log and "--dry-run" not in log
        with pytest.raises(Refused, match="Unknown or expired token"):
            call(c, "start_job", token=preview["token"])  # once only

        # Busy: refused, and the token still starts the job once the other one is done.
        monkeypatch.setenv("STUB_SLEEP", "30")
        running = call(c, "start_job", token=call(c, "preview_job", kind="news", dry_run=True)["token"])["job_id"]
        waiting = call(c, "preview_job", kind="discover", focus="fintech", limit=5)
        assert waiting["busy"] is True
        with pytest.raises(Refused, match="Another hunt.py command is running"):
            call(c, "start_job", token=waiting["token"])
        assert call(c, "cancel_job", job_id=running) == {"job_id": running, "cancelled": True}
        assert finished(root, running)["status"] == "cancelled"
        with pytest.raises(Refused, match="This job is not running"):
            call(c, "cancel_job", job_id=running)
        monkeypatch.delenv("STUB_SLEEP")
        assert finished(root, call(c, "start_job", token=waiting["token"])["job_id"])["status"] == "succeeded"
        assert [(j["kind"], j["status"], j["label"]) for j in call(c, "list_jobs")["jobs"]] == [
            ("discover", "succeeded", "MCP · Discover “fintech” (limit 5)"),
            ("news", "cancelled", "MCP · News digest (dry run)"),
            ("run", "succeeded", "MCP · Run and notify · 2 selected companies")]

        monkeypatch.setattr(mcp_server, "PREVIEW_SECONDS", 0)
        with pytest.raises(Refused, match="Unknown or expired token"):
            call(c, "start_job", token=call(c, "preview_job", kind="monitor")["token"])


def test_a_new_monitor_is_added_to_monitors_csv_only_when_its_sync_starts(root):
    with served(make_app(root)) as c:
        preview = call(c, "preview_job", kind="new_monitor", name="Acme", domain="https://acme.com/")
        assert preview["command"] == "python hunt.py monitor-sync"
        assert not (root / "monitors.csv").exists()
        with pytest.raises(Refused, match="each company needs a name and a domain"):
            call(c, "preview_job", kind="new_monitor", domain="acme.com")
        job = call(c, "start_job", token=preview["token"])
        assert finished(root, job["job_id"])["status"] == "succeeded"
    assert (root / "monitors.csv").read_text().splitlines()[-1] == "Acme,acme.com"


# PostHog ----------------------------------------------------------------------------------------------------------

def own(event):
    return event["event"], {k: v for k, v in event["properties"].items() if not k.startswith("$") and k != "service"}


def test_posthog_hears_each_call_by_tool_and_each_change_as_the_pages_report_it_and_nothing_of_the_owners(
        root, posthog_api, monkeypatch):
    def unreadable(conn, *, status=""):
        raise PermissionError(13, "Permission denied", "/data/PRIVATE-PATH/discovered.csv")

    write_mail(root, "20261015-090000", [mail_update(1, "applied", "PRIVATE-EMPLOYER")])
    with served(make_app(root)) as c:
        call(c, "get_company", domain="acme.com")
        call(c, "set_status", kind="posting", ref="https://acme.com/2", status="applied", notes="PRIVATE-NOTE")
        call(c, "set_status", kind="posting", ref="https://acme.com/2", notes="PRIVATE-NOTE again")
        with pytest.raises(Refused):
            call(c, "get_company", domain="private-unknown.com")  # refused: nothing sent
        (update,) = call(c, "list_email_suggestions")["suggestions"]
        call(c, "decide_email_suggestion", update_id=update["id"], action="add")
        call(c, "queue_leads", domains=["delta.ai"])
        project = call(c, "create_project", title="PRIVATE-PROJECT")
        call(c, "update_project", project_id=project["id"], status="active")
        call(c, "create_interview", company="PRIVATE-EMPLOYER", role="PRIVATE-ROLE")
        preview = call(c, "preview_job", kind="discover", focus="PRIVATE-FOCUS", limit=7)
        finished(root, call(c, "start_job", token=preview["token"])["job_id"])
        with pytest.raises(Refused):
            call(c, "preview_job", kind="discover", focus="PRIVATE-FOCUS", limit=500)
        monitor = call(c, "preview_job", kind="new_monitor", name="PRIVATE-COMPANY", domain="private-co.io")
        finished(root, call(c, "start_job", token=monitor["token"])["job_id"])
        monkeypatch.setenv("STUB_SLEEP", "30")
        running = call(c, "start_job", token=call(c, "preview_job", kind="news")["token"])["job_id"]
        call(c, "cancel_job", job_id=running)
        finished(root, running)
        monkeypatch.setattr(queries, "leads", unreadable)
        with pytest.raises(Refused, match="^Error executing tool list_leads$"):  # the client learns nothing of it
            call(c, "list_leads")

    events = posthog_api.events()
    assert [own(event) for event in events] == [
        ("mcp_tool_called", {"tool": "get_company"}),
        ("pipeline_status_updated", {"status": "applied", "trigger": "mcp"}),
        ("mcp_tool_called", {"tool": "set_status"}),
        ("mcp_tool_called", {"tool": "set_status"}),  # notes only: the pages send nothing for notes either
        ("mcp_tool_called", {"tool": "list_email_suggestions"}),
        ("email_update_decided", {"trigger": "mcp"}),
        ("mcp_tool_called", {"tool": "decide_email_suggestion"}),
        ("leads_queued", {"lead_count": 1, "trigger": "mcp"}),
        ("mcp_tool_called", {"tool": "queue_leads"}),
        ("project_created", {"trigger": "mcp"}),
        ("mcp_tool_called", {"tool": "create_project"}),
        ("project_updated", {"trigger": "mcp"}),
        ("mcp_tool_called", {"tool": "update_project"}),
        ("interview_created", {"trigger": "mcp"}),
        ("mcp_tool_called", {"tool": "create_interview"}),
        ("mcp_tool_called", {"tool": "preview_job"}),
        ("hunt_run_started", {"kind": "discover", "trigger": "mcp", "has_focus": True, "limit": 7}),
        ("mcp_tool_called", {"tool": "start_job"}),
        ("mcp_tool_called", {"tool": "preview_job"}),
        ("monitor_added", {"trigger": "mcp"}),
        ("hunt_run_started", {"kind": "monitor_sync", "trigger": "mcp"}),
        ("mcp_tool_called", {"tool": "start_job"}),
        ("mcp_tool_called", {"tool": "preview_job"}),
        ("hunt_run_started", {"kind": "news", "trigger": "mcp", "dry_run": False}),
        ("mcp_tool_called", {"tool": "start_job"}),
        ("hunt_run_cancelled", {"trigger": "mcp"}),
        ("mcp_tool_called", {"tool": "cancel_job"}),
        ("$exception", {"route": "/mcp", "method": "POST", "tool": "list_leads"}),
    ]
    assert {event["distinct_id"] for event in events} == {posthog_client.OWNER}
    (raised,) = events[-1]["properties"]["$exception_list"]
    assert (raised["type"], raised["value"]) == ("PermissionError", "")
    for frame in raised["stacktrace"]["frames"]:  # the source around each line: code, here this test's
        for key in ("pre_context", "context_line", "post_context"):
            frame.pop(key, None)
    told = json.dumps(events).lower()
    for private in ("acme", "delta", "private", TOKEN.lower(), "127.0.0.1"):
        assert private not in told
