import json
import sys
import time
from datetime import datetime, timedelta

import pytest

from dashboard import db
from dashboard.app import create_app
from dashboard_data import client as make_client
from dashboard_data import fit, result, write_run


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


def test_inbox_window(client):
    week = client.get("/inbox", params={"days": 7}).text
    assert "Growth Analyst" in week and "Data Lead" not in week
    # Acme counts as new: its best role changed in the recent run. Gamma (skip) never shows.
    assert "Analytics Engineer" in week and "Growth Engineer" in week and "Gamma" not in week

    assert "Data Lead" in client.get("/inbox", params={"days": 0}).text


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
    assert c.get("/inbox", params={"days": 7}).status_code == 200

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
