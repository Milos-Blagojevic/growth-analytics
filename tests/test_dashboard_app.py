import json
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from dashboard.app import create_app
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
    return TestClient(create_app(root))


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


def test_inbox_window(client):
    week = client.get("/inbox", params={"days": 7}).text
    assert "Growth Analyst" in week and "Data Lead" not in week
    # Acme counts as new: its best role changed in the recent run. Gamma (skip) never shows.
    assert "Analytics Engineer" in week and "Growth Engineer" in week and "Gamma" not in week

    assert "Data Lead" in client.get("/inbox", params={"days": 0}).text


def test_empty_project_explains_how_to_get_data(tmp_path):
    page = TestClient(create_app(tmp_path)).get("/").text
    assert "No runs yet" in page
    assert "hunt.py discover" in TestClient(create_app(tmp_path)).get("/leads").text
