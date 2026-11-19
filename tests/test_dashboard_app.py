import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import uvicorn

from clients import posthog_client
from dashboard import __main__ as entry
from dashboard import db, jobs, projects, queries, schedule
from dashboard.app import create_app
from dashboard_data import BASE_URL, INTERVIEW_TURNS, fit, mail_update, result, wispr, write_mail, write_run
from dashboard_data import client as make_client
from test_dashboard_jobs import STUB, finished, started_job_id


def stamp(days_ago):
    when = datetime.now() - timedelta(days=days_ago)
    return when.strftime("%Y%m%d-%H%M%S"), when.isoformat(timespec="seconds")


@pytest.fixture
def root(tmp_path):
    """Old run (40 days ago): Acme consider for role 1. Recent run (2 days ago): Acme apply for role 2,
    Beta stretch, Gamma skip. A watch posting from 1 day ago and one from 30 days ago."""
    old_id, _ = stamp(40)
    write_run(tmp_path, old_id, [result("Acme", "acme.com", fit=fit(64, "consider", "Data Analyst", "https://acme.com/1"))])
    new_id, new_at = stamp(2)
    write_run(
        tmp_path,
        new_id,
        [
            result(
                "Acme",
                "acme.com",
                fit=fit(84, "apply", "Analytics Engineer", "https://acme.com/2", scope_note="Mostly dbt work."),
                roles=[("Analytics Engineer", "https://acme.com/2", "live")],
            ),
            result(
                "Beta",
                "beta.io",
                fit=fit(70, "stretch", "Growth Engineer", "https://beta.io/g", gap_note="6 months.", proof_ideas=["Build an A/B harness"]),
            ),
            result("Gamma", "gamma.dev", fit=fit(20, "skip")),
        ],
        manifest={"mode": "deliver", "saved_at": new_at, "companies": 3, "errors": 0, "exa": {"requests": 9, "dollars": 0.05}},
    )
    (tmp_path / "watch_state.json").write_text(
        json.dumps(
            {
                "acme": {
                    "fresh": {"title": "Growth Analyst", "location": "Remote", "remote": True, "url": "https://jobs.ashbyhq.com/acme/fresh", "first_seen": stamp(1)[1]},
                    "stale": {"title": "Data Lead", "first_seen": stamp(30)[1]},
                }
            }
        )
    )
    return tmp_path


@pytest.fixture
def client(root):
    return make_client(create_app(root))


def test_board_shows_latest_verdict_tab_with_filters(client):
    page = client.get("/").text
    assert "Acme" in page and "Analytics Engineer" in page
    assert "Beta" not in page  # stretch tab, not apply
    assert "Exa $0.05" in page

    stretch = client.get("/", params={"verdict": "stretch"}).text
    assert "Beta" in stretch and "1 project idea" in stretch

    assert "Acme" not in client.get("/", params={"q": "nomatch"}).text.split('id="results"')[1]
    assert "Acme" not in client.get("/", params={"min_score": 90}).text.split('id="results"')[1]


def test_company_detail_is_a_panel_for_htmx_and_a_page_otherwise(client):
    panel = client.get("/c/acme.com", headers={"HX-Request": "true"}).text
    assert 'class="sheet slide' in panel and "<html" not in panel
    assert "Mostly dbt work." in panel and 'onclick="closePanel()"' in panel
    assert "<polyline" in panel  # two scored runs: 64 then 84

    page = client.get("/c/acme.com").text
    assert "<html" in page and 'class="sheet page' in page and "Mostly dbt work." in page

    assert client.get("/c/unknown.com").status_code == 404


def test_company_page_shows_research_warnings_and_old_results_without_them_still_load(client, root):
    assert "Research warnings" not in client.get("/c/acme.com").text  # the fixture's results predate the key

    run_id, _ = stamp(1)
    write_run(root, run_id, [result("Acme", "acme.com", fit=fit(80, "apply", "AE", "https://acme.com/3"),
                                    warnings=["ATS fallback search failed: ValueError: <boom>"])])

    page = client.get("/c/acme.com").text
    assert "Research warnings" in page
    assert "ATS fallback search failed: ValueError: &lt;boom&gt;" in page


def test_company_page_shows_a_fiber_result_and_no_chip_for_a_missing_confidence(client, root):
    assert 'class="conf conf-high"' in client.get("/c/acme.com").text

    run_id, _ = stamp(1)
    saved = result("Acme", "acme.com", fit=fit(80, "apply", "AE", "https://acme.com/3"), roles=[("AE", "https://acme.com/3", "live")])
    saved["research"]["role_source"] = "fiber+main"
    saved["research"]["grounding"]["open_roles"]["confidence"] = None  # Fiber's sources can come without one
    write_run(root, run_id, [saved])

    page = client.get("/c/acme.com").text
    assert "found by fiber+main search" in page
    assert "open roles</dt>" in page and 'class="conf' not in page


def test_third_party_listings_show_on_the_card_and_in_the_details_and_old_results_load_without_them(client, root):
    assert "Seen only on third-party sites" not in client.get("/c/acme.com").text  # the fixture's results predate the key
    assert "third-party listing" not in client.get("/").text

    run_id, _ = stamp(1)
    listing = ("Head of Data <b>Science</b>", "https://www.hirify.me/jobs/1347972-head-data-science")
    write_run(root, run_id, [result("Acme", "acme.com", fit=fit(61, "check_careers", "Head of Data Science"),
                                    third_party=[listing])])

    board = client.get("/", params={"verdict": "check_careers"}).text
    assert "Acme" in board and "Head of Data Science" in board and "third-party listing</span>" in board
    page = client.get("/c/acme.com").text
    assert "Seen only on third-party sites" in page
    assert (f'<a href="{listing[1]}" target="_blank" rel="noopener">Head of Data &lt;b&gt;Science&lt;/b&gt;</a>\n'
            '        <span class="muted small">hirify.me</span>') in page


def test_inbox_is_removed_and_remaining_sections_stay_in_navigation(client):
    assert client.get("/inbox?days=7").status_code == 404

    page = client.get("/").text
    navigation = page.split('<nav class="nav">')[1].split("</nav>")[0]
    assert 'href="/inbox' not in navigation
    for path in ("/", "/pipeline", "/interviews", "/projects", "/leads", "/runs"):
        assert f'href="{path}"' in navigation


def test_empty_project_explains_how_to_get_data(tmp_path):
    empty = make_client(create_app(tmp_path))
    assert "No runs yet" in empty.get("/").text
    assert "hunt.py discover" in empty.get("/leads").text


def test_leads_can_be_added_to_companies_csv(tmp_path):
    (tmp_path / "companies.csv").write_text("# name,domain\nAcme,acme.com")  # no trailing newline
    (tmp_path / "discovered.csv").write_text(
        "discovered_at,name,domain,why,focus\n"
        "2026-01-01T08:00:00,Delta,delta.ai,Growth team,\n2026-01-01T08:00:00,\"Echo, Inc\",echo.so,Data team,\n"
    )
    c = make_client(create_app(tmp_path))

    response = c.post("/leads/queue", data={"domains": ["delta.ai", "echo.so"]})
    assert (response.status_code, response.headers["HX-Refresh"]) == (204, "true")
    assert c.post("/leads/queue", data={"domains": ["delta.ai"]}).status_code == 204  # already listed: no duplicate
    assert (tmp_path / "companies.csv").read_text() == '# name,domain\nAcme,acme.com\nDelta,delta.ai\n"Echo, Inc",echo.so\n'
    assert "Echo, Inc" in c.get("/leads", params={"status": "queued"}).text

    assert c.post("/leads/queue", data={"domains": ["nope.com"]}).status_code == 400
    assert c.post("/leads/queue", data={}).status_code == 400


def test_a_corrupt_watch_state_or_csv_is_reported_without_taking_down_the_pages(root):
    """Every request ingests first, so one unreadable file used to 500 every page."""
    (root / "watch_state.json").write_text('{"acme": {"j1": {"title": "x", "first_seen": "2026-01-01T10:00:00"')
    (root / "discovered.csv").write_text("discovered_at,name,domain,focus\n2026-01-01T08:00:00,Delta,delta.ai,\n")  # no `why`
    # a NUL byte reads fine since Python 3.11; a field beyond csv's field limit is what still raises csv.Error
    (root / "companies.csv").write_text("# name,domain\nAcme,acme.com\nBad\x00Corp," + "x" * 200_000 + "\n")
    c = make_client(create_app(root))

    board = c.get("/")
    assert board.status_code == 200
    assert "Acme" in board.text  # the runs were still ingested
    assert "Some saved files could not be read" in board.text and "watch_state.json: JSONDecodeError" in board.text
    assert "companies.csv: cannot be read" in board.text and "discovered.csv: KeyError" in board.text  # both named
    assert c.get("/leads").status_code == 200
    assert c.get("/pipeline").status_code == 200

    (root / "companies.csv").write_text("# name,domain\nAcme,acme.com\n")
    leads = c.get("/leads")
    assert leads.status_code == 200 and "discovered.csv: KeyError" in leads.text and "companies.csv:" not in leads.text

    (root / "discovered.csv").write_text("discovered_at,name,domain,why,focus\n2026-01-01T08:00:00,Delta,delta.ai,why,\n")
    (root / "companies.csv").unlink()
    (root / "companies.csv").mkdir()  # a directory where the file should be: OSError rather than csv.Error
    leads = c.get("/leads")
    assert leads.status_code == 200 and "Delta" in leads.text and "companies.csv: cannot be read" in leads.text  # leads still listed
    assert c.post("/leads/queue", data={"domains": ["delta.ai"]}).status_code == 400  # nothing appended blind
    assert c.get("/runs").status_code == 200

    (root / "watch_state.json").write_text("[]")  # a list instead of the boards dict
    assert "watch_state.json: AttributeError" in c.get("/").text


def test_an_unreadable_companies_csv_disables_full_runs_without_taking_down_the_runs_page(root):
    (root / "companies.csv").write_text("# name,domain\nAcme,acme.com\nBad," + "x" * 200_000 + "\n")
    c = make_client(create_app(root))

    page = c.get("/runs")
    assert page.status_code == 200
    assert "companies.csv: cannot be read" in page.text
    assert "(0)" in page.text and "disabled>Review and start…" in page.text

    preview = c.post("/runs/preview", data={"kind": "run", "mode": "dry_run"})
    assert preview.status_code == 200 and "companies.csv cannot be read: field larger than field limit" in preview.text
    assert c.post("/runs", data={"kind": "run", "mode": "dry_run"}).status_code == 400
    conn = db.connect(root)
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
    conn.close()


def test_runs_page_points_at_chat_bot_when_the_dashboard_does_not_run_it(client):
    page = client.get("/runs").text
    assert "<h2>Chat bot</h2>" in page
    assert "Not running here. Set <code>CHAT_BOT=1</code> to keep <code>hunt.py chat</code> running" in page
    assert 'hx-get="/runs/chat"' not in page
    assert client.get("/runs/chat").status_code == 404


def test_runs_page_shows_the_chat_bot_it_keeps_running(tmp_path, monkeypatch):
    monkeypatch.setenv("STUB_SLEEP", "30")
    (tmp_path / "stub_hunt.py").write_text(
        "import os, sys, time\nprint('chat stub', *sys.argv[1:], flush=True)\ntime.sleep(float(os.environ['STUB_SLEEP']))\n"
    )
    app = create_app(tmp_path, hunt_command=[sys.executable, str(tmp_path / "stub_hunt.py")], chat_bot=True)
    assert app.state.bot is not None and not app.state.bot.status()["running"]  # started by the lifespan
    with make_client(app) as c:
        deadline = time.monotonic() + 5
        while not (app.state.bot.status()["running"] and "chat stub chat" in app.state.bot.read_log()):
            assert time.monotonic() < deadline
            time.sleep(0.02)
        assert not app.state.runner.busy()

        page = c.get("/runs").text
        assert "<h2>Chat bot</h2>" in page and "CHAT_BOT=1" not in page
        assert 'id="chat-bot" class="job" hx-get="/runs/chat" hx-trigger="every 5s"' in page
        assert "running" in page and "since just now · 1 start" in page and "chat stub chat" in page

        partial = c.get("/runs/chat")
        assert partial.status_code == 200
        assert partial.text.startswith('<div id="chat-bot"') and "python hunt.py chat" in partial.text
        assert 'id="chat-log">chat stub chat\n' in partial.text
    assert not app.state.bot.status()["running"]  # stopped with the app
    assert (tmp_path / "jobs" / "chat.log").read_text() == "chat stub chat\n"


# PostHog ----------------------------------------------------------------------------------------


def own(event):
    """An event's name and the properties the dashboard gave it (the SDK adds the $-ones)."""
    return event["event"], {k: v for k, v in event["properties"].items() if not k.startswith("$") and k != "service"}


def test_every_action_of_the_owner_reaches_posthog_as_an_event_that_names_nothing_of_theirs(root, posthog_api,
                                                                                         monkeypatch):
    monkeypatch.delenv("STUB_SLEEP", raising=False)
    monkeypatch.delenv("STUB_EXIT", raising=False)
    (root / "stub_hunt.py").write_text(STUB)
    (root / "companies.csv").write_text("# name,domain\nAcme,acme.com\n")
    (root / "discovered.csv").write_text("discovered_at,name,domain,why,focus\n2026-01-01T08:00:00,Delta,delta.ai,PRIVATE-WHY,\n")
    update = mail_update(1, "rejected", "PRIVATE-EMPLOYER")
    write_mail(root, "20261015-090000", [update])
    write_run(root, stamp(1)[0], [result("Epsilon", "epsilon.dev", fit=fit(
        71, "stretch", "Growth Engineer", "https://epsilon.dev/g", gap_note="1 year.", proof_ideas=["PRIVATE-IDEA"]))])
    app = create_app(root, hunt_command=[sys.executable, str(root / "stub_hunt.py")],
                     schedule_entries=schedule.parse("* * * * * discover PRIVATE-FOCUS --limit 9"))
    c = make_client(app)  # without the lifespan: the scheduler's own thread stays off, and this test ticks it
    acme = {"kind": "company", "ref": "acme.com"}

    # Each action once, next to what does not count: the notes, which save as they are typed, and every refusal.
    assert c.post("/track", data=acme | {"status": "applied"}).status_code == 200
    assert c.post("/track", data=acme | {"notes": "PRIVATE-NOTE"}).status_code == 200
    assert c.post("/track", data=acme | {"status": "nonsense"}).status_code == 400
    assert c.post("/leads/queue", data={"domains": ["delta.ai"]}).status_code == 204
    assert c.post("/leads/queue", data={"domains": ["nope.com"]}).status_code == 400
    assert c.post("/mail/decide", data={"update": update["id"], "action": "dismiss"}).status_code == 204
    assert c.post("/mail/decide", data={"update": update["id"], "action": "dismiss"}).status_code == 409
    assert c.post("/projects", data={"title": "PRIVATE-PROJECT"}).status_code == 204
    assert c.post("/projects", data={"title": "  "}).status_code == 400
    conn = db.connect(root)
    (project,), (first, second) = projects.board(conn)["idea"], projects.suggestions(conn)
    conn.close()
    assert c.post(f"/projects/{project['id']}", data={"status": "active"}).status_code == 204
    assert c.post(f"/projects/{project['id']}/delete").status_code == 204
    assert c.post(f"/projects/{project['id']}/delete").status_code == 404
    assert c.post(f"/projects/suggestions/{first['key']}", data={"action": "accept"}).status_code == 204
    assert c.post(f"/projects/suggestions/{second['key']}", data={"action": "dismiss"}).status_code == 204
    interview = {"company": "PRIVATE-EMPLOYER", "role": "PRIVATE-ROLE", "stage": "hr", "language": "English"}
    page = c.post("/interviews", data=interview).headers["HX-Redirect"]
    assert c.post("/interviews", data=interview | {"company": ""}).status_code == 400
    assert c.post(page, data=interview | {"role": "PRIVATE-NEW-ROLE"}).status_code == 204
    assert c.post(page + "/transcript", data={"text": ""}).status_code == 400
    pasted = "Ana: PRIVATE-QUESTION one\nMe: PRIVATE-ANSWER one\nAna: PRIVATE-QUESTION two\nMe: PRIVATE-ANSWER two\n"
    assert c.post(page + "/transcript", data={"text": pasted}).status_code == 204
    assert c.post(page + "/transcript", files={"file": ("refined.ndjson", wispr(*INTERVIEW_TURNS).encode())}).status_code == 204
    assert c.post(page + "/me", data={"me": "9"}).status_code == 400
    assert c.post(page + "/me", data={"me": "2"}).status_code == 204
    assert c.post("/runs/preview", data={"kind": "discover", "focus": "PRIVATE-FOCUS"}).status_code == 200  # starts nothing
    # A job says how it was started, from a button or the schedule: a run its mode and whether it asks Fiber, a discover
    # whether it has a focus (never the focus) and its limit, a check whether it is a dry run.
    (root / "outlook_token.json").write_text("{}")  # Check email starts only after the Outlook sign-in
    for started in ({"kind": "run", "mode": "deliver", "fiber": "1"},
                    {"kind": "run", "mode": "research_only", "domains": ["delta.ai"]},
                    {"kind": "run"},
                    {"kind": "discover", "focus": "PRIVATE-FOCUS", "limit": "7"},
                    {"kind": "discover"},
                    {"kind": "monitor", "dry_run": "1"},
                    {"kind": "mail"},
                    {"kind": "news", "dry_run": "1"},
                    {"kind": "monitor_sync"}):
        finished(root, started_job_id(c.post("/runs", data=started)))
    assert c.post("/runs", data={"kind": "discover", "focus": "PRIVATE-FOCUS", "limit": "500"}).status_code == 400
    app.state.scheduler.tick(datetime.now() + timedelta(minutes=1))
    conn = db.connect(root)
    finished(root, jobs.recent(conn, 1)[0]["id"])
    conn.close()
    monkeypatch.setenv("STUB_SLEEP", "30")
    running = started_job_id(c.post("/runs", data={"kind": "watch"}))
    assert c.post("/runs", data={"kind": "watch", "dry_run": "1"}).status_code == 409
    assert c.post(f"/runs/jobs/{running}/cancel").status_code == 204
    finished(root, running)
    assert c.post(f"/runs/jobs/{running}/cancel").status_code == 409

    events = posthog_api.events()
    assert [own(event) for event in events] == [
        ("pipeline_status_updated", {"status": "applied"}),
        ("leads_queued", {"lead_count": 1}),
        ("email_update_decided", {}),
        ("project_created", {}),
        ("project_updated", {}),
        ("project_deleted", {}),
        ("project_suggestion_decided", {"decision": "accepted"}),
        ("project_suggestion_decided", {"decision": "dismissed"}),
        ("interview_created", {}),
        ("interview_updated", {}),
        ("interview_transcript_saved", {"source": "pasted"}),
        ("interview_transcript_saved", {"source": "file"}),
        ("interview_speaker_selected", {}),
        ("hunt_run_started", {"kind": "run", "trigger": "button", "mode": "deliver", "fiber": True}),
        ("hunt_run_started", {"kind": "run", "trigger": "button", "mode": "research_only", "fiber": False}),
        ("hunt_run_started", {"kind": "run", "trigger": "button", "mode": "dry_run", "fiber": False}),
        ("hunt_run_started", {"kind": "discover", "trigger": "button", "has_focus": True, "limit": 7}),
        ("hunt_run_started", {"kind": "discover", "trigger": "button", "has_focus": False, "limit": 15}),
        ("hunt_run_started", {"kind": "monitor", "trigger": "button", "dry_run": True}),
        ("hunt_run_started", {"kind": "mail", "trigger": "button", "dry_run": False}),
        ("hunt_run_started", {"kind": "news", "trigger": "button", "dry_run": True}),
        ("hunt_run_started", {"kind": "monitor_sync", "trigger": "button"}),
        ("hunt_run_started", {"kind": "discover", "trigger": "schedule", "has_focus": True, "limit": 9}),
        ("hunt_run_started", {"kind": "watch", "trigger": "button", "dry_run": False}),
        ("hunt_run_cancelled", {}),
    ]
    assert {event["distinct_id"] for event in events} == {posthog_client.OWNER}  # one owner, no person profile
    assert all(event["options"]["process_person_profile"] is False for event in events)
    told = json.dumps(events).lower()
    for private in ("acme", "delta", "epsilon", "private", "127.0.0.1", "$ip", "$current_url", "$request_path"):
        assert private not in told


def test_each_page_the_owner_opens_reaches_posthog_as_a_pageview_of_its_route_and_nothing_they_asked_for(
        root, posthog_api):
    c = make_client(create_app(root, password="correct horse battery"))
    htmx = {"HX-Request": "true"}

    def clicked_on(address, **more):
        """What a browser sends with a click on a link of the page at `address`: that page's address, as Referer."""
        return {"Referer": BASE_URL + address, **more}

    assert c.get("/?q=PRIVATE-SEARCH").status_code == 200  # not logged in: the guard sends the browser to /login
    assert c.post("/login", data={"password": "correct horse battery"}).status_code == 204
    assert c.get("/?q=PRIVATE-SEARCH&verdict=stretch").status_code == 200  # a typed address: opened from no page
    assert c.get("/?q=PRIVATE-SEARCH", headers=htmx).status_code == 200  # the board's live filter: the same page
    assert c.get("/c/acme.com", headers=clicked_on("/?q=PRIVATE-SEARCH")).status_code == 200  # a card of the board
    assert c.get("/c/beta.io", headers=htmx).status_code == 200  # the slide-over panel over the board
    assert c.get("/projects/new", headers=htmx).status_code == 200  # a form in the panel
    assert c.get("/leads", headers=clicked_on("/c/acme.com")).status_code == 200  # the Leads tab, on a company's page
    # Loaded again: the browser's reload button repeats the Referer the page was first opened with.
    assert c.get("/leads", headers=clicked_on("/c/acme.com", **{"Cache-Control": "max-age=0"})).status_code == 200
    assert c.get("/pipeline", headers={"Referer": "https://evil.example/c/acme.com"}).status_code == 200  # another site
    assert c.get("/runs", headers=clicked_on("/PRIVATE-PATH")).status_code == 200  # an address that is no page
    assert c.get("/projects", headers={"Referer": "http://[PRIVATE"}).status_code == 200  # nor an address at all
    assert c.get("/static/app.css").status_code == 200  # not a page
    assert c.get("/c/nope.com").status_code == 404  # an answer, not a page
    assert c.head("/leads").status_code == 405  # nothing was opened
    assert c.get("/login").status_code == 200  # logged in already: sent on to the board
    assert c.get("/leads", headers={"Host": "evil.example"}).status_code == 400  # refused by the guard

    events = posthog_api.events()
    assert [(event["event"], event["properties"]["$current_url"], event["properties"]["$pathname"],
             event["properties"].get("previous_route")) for event in events] == [
        ("$pageview", route, route, previous) for route, previous in (
            ("/login", None), ("/", None), ("/c/{domain}", "/"), ("/leads", "/c/{domain}"), ("/leads", "/leads"),
            ("/pipeline", None), ("/runs", None), ("/projects", None), ("/", None))]
    assert {event["distinct_id"] for event in events} == {posthog_client.OWNER}
    told = json.dumps(events).lower()
    for private in ("acme", "beta", "private", "stretch", "127.0.0.1", "evil", "$ip", "$referrer", "$host"):
        assert private not in told


def test_an_exception_no_page_handles_reaches_posthog_by_type_stack_and_route_without_what_it_says(root, posthog_api,
                                                                                                 monkeypatch):
    def unwritable(conn, domain):  # what Python says about a file names the file, and the file names the interview
        raise PermissionError(13, "Permission denied", "/data/interviews/20261020-acme-senior-analyst/transcript.json")

    monkeypatch.setattr(queries, "company", unwritable)
    monkeypatch.setattr(projects, "board", lambda conn: dict.fromkeys(projects.STATUSES, 5))  # fails in the template
    c = make_client(create_app(root))

    with pytest.raises(PermissionError):  # the test client raises what a browser gets a 500 for
        c.get("/c/acme.com")
    with pytest.raises(TypeError):
        c.get("/projects")
    assert c.get("/interviews/no-such-interview").status_code == 404  # an answer, not an exception
    assert c.get("/", headers={"Host": "evil.example"}).status_code == 400  # refused by the guard: nothing reported

    in_a_route, in_a_template = posthog_api.events("$exception")
    assert own(in_a_route)[1] == {"route": "/c/{domain}", "method": "GET"}
    assert own(in_a_template)[1] == {"route": "/projects", "method": "GET"}
    raised = [r for reported in (in_a_route, in_a_template) for r in reported["properties"]["$exception_list"]]
    assert [(r["type"], r["value"]) for r in raised] == [("PermissionError", ""), ("TypeError", "")]
    frames = [frame for r in raised for frame in r["stacktrace"]["frames"]]
    assert "unwritable" in {frame["function"] for frame in frames}
    assert "dashboard/templates/projects.html" in {frame["filename"] for frame in frames}  # where it is in the code
    for frame in frames:  # and nowhere where the code is on this machine
        assert "abs_path" not in frame
        assert not any(os.path.isabs(frame.get(key) or "") for key in ("filename", "module")), frame
        for key in ("pre_context", "context_line", "post_context"):  # the source around the line: code, here this test's
            frame.pop(key, None)
    told = json.dumps([in_a_route, in_a_template])
    assert "acme" not in told and "/data" not in told and "Permission denied" not in told
    assert str(Path(__file__).parent.parent) not in told


def test_the_dashboard_takes_posthog_from_its_environment_and_works_the_same_without_it(tmp_path, posthog_api,
                                                                                       monkeypatch):
    served = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: served.append(app))
    monkeypatch.setattr(sys, "argv", ["dashboard", "--root", str(tmp_path)])
    monkeypatch.delenv("HUNT_SCHEDULE", raising=False)
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)

    entry.main()  # POSTHOG_PROJECT_TOKEN and POSTHOG_HOST are set, as on Railway
    with make_client(served[0]) as c:  # started and stopped, as uvicorn does
        assert c.post("/projects", data={"title": "dbt package"}).status_code == 204
    (event,) = posthog_api.events()
    assert (event["event"], event["properties"]["service"]) == ("project_created", "dashboard")
    assert posthog_api.records() == [{"name": "dashboard_started", "level": "INFO", "service": "dashboard"}]

    monkeypatch.delenv("POSTHOG_HOST")
    entry.main()
    with make_client(served[1]) as c:
        assert c.post("/projects", data={"title": "dbt tests"}).status_code == 204
    assert len(posthog_api.events()) == 1 and len(posthog_api.records()) == 1
