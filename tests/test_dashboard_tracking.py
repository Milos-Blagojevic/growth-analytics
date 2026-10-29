import json

import pytest

from dashboard import db, tracking
from dashboard.app import create_app
from dashboard_data import client, fit, result, write_run

ACME_URL = "https://jobs.ashbyhq.com/acme/1"


@pytest.fixture
def root(tmp_path):
    write_run(
        tmp_path,
        "20260108-090000",
        [
            result("Acme", "acme.com", fit=fit(82, "apply", "Analytics Engineer", ACME_URL)),
            result("Beta", "beta.io", fit=fit(78, "apply", "Data Engineer", "https://beta.io/jobs/1")),
            result("Gamma", "gamma.dev", fit=fit(60, "check_careers")),  # no posting: tracked by domain
        ],
    )
    (tmp_path / "watch_state.json").write_text(
        json.dumps({"acme": {"w1": {"title": "Growth Analyst", "url": "https://jobs.ashbyhq.com/acme/w1", "first_seen": "2026-01-08T10:00:00"}}})
    )
    return tmp_path


@pytest.fixture
def app_client(root):
    return client(create_app(root))


def track(app_client, kind, ref, **fields):
    return app_client.post("/track", data={"kind": kind, "ref": ref, **fields})


def test_status_follows_the_card_and_feeds_the_pipeline(root, app_client):
    assert track(app_client, "posting", ACME_URL, status="applied").status_code == 200
    assert track(app_client, "posting", ACME_URL, notes="Recruiter: Ana, call on Friday").status_code == 200
    assert track(app_client, "company", "gamma.dev", status="saved").status_code == 200

    board = app_client.get("/").text
    assert "Applied</span>" in board and ">notes<" in board
    panel = app_client.get("/c/acme.com", headers={"HX-Request": "true"}).text
    assert "Recruiter: Ana, call on Friday" in panel

    track(app_client, "posting", ACME_URL, status="interviewing")
    conn = db.connect(root)
    acme = tracking.get(conn, "posting", ACME_URL)
    assert (acme["status"], acme["notes"]) == ("interviewing", "Recruiter: Ana, call on Friday")
    assert acme["applied_at"] is not None  # kept from when it became applied
    columns = tracking.pipeline(conn)
    assert [(i["name"], i["title"]) for i in columns["interviewing"]] == [("Acme", "Analytics Engineer")]
    assert [(i["name"], i["domain"]) for i in columns["saved"]] == [("Gamma", "gamma.dev")]
    conn.close()
    pipeline = app_client.get("/pipeline").text
    assert "Analytics Engineer" in pipeline and "Gamma" in pipeline


def test_dismissed_cards_leave_the_board_unless_asked_for(app_client):
    track(app_client, "posting", "https://beta.io/jobs/1", status="dismissed")

    assert "Beta" not in app_client.get("/").text.split('id="results"')[1]
    assert 'Apply<span class="count">1</span>' in app_client.get("/").text  # tab counts skip dismissed too
    assert "Beta" in app_client.get("/", params={"status": "all"}).text.split('id="results"')[1]
    assert "Acme" not in app_client.get("/", params={"status": "dismissed"}).text.split('id="results"')[1]


def test_back_to_new_without_notes_forgets_the_item(root, app_client):
    track(app_client, "posting", ACME_URL, status="saved")
    track(app_client, "posting", ACME_URL, status="new")

    conn = db.connect(root)
    assert conn.execute("SELECT COUNT(*) FROM tracking").fetchone()[0] == 0
    conn.close()


def test_watch_postings_can_be_tracked_from_the_inbox(app_client):
    url = "https://jobs.ashbyhq.com/acme/w1"
    response = track(app_client, "posting", url, status="saved", variant="select")
    assert 'class="status-select s-saved"' in response.text

    assert 'value="saved" selected' in app_client.get("/inbox", params={"days": 0}).text
    assert "Growth Analyst" in app_client.get("/pipeline").text


def test_invalid_tracking_is_rejected(app_client):
    assert track(app_client, "posting", ACME_URL, status="hired").status_code == 400
    assert track(app_client, "job", ACME_URL, status="saved").status_code == 400


def test_your_data_survives_reingest_and_derived_schema_changes(root, app_client):
    track(app_client, "posting", ACME_URL, status="offer", notes="Negotiating")
    app_client.post("/projects", data={"title": "dbt package"})

    conn = db.connect(root)
    conn.execute("PRAGMA user_version = 0")  # as after a SCHEMA_VERSION bump
    conn.commit()
    conn.close()
    write_run(root, "20260109-090000", [result("Acme", "acme.com", fit=fit(85, "apply", "Analytics Engineer", ACME_URL))])

    page = app_client.get("/c/acme.com", headers={"HX-Request": "true"}).text
    assert "Negotiating" in page and "85" in page
    assert "dbt package" in app_client.get("/projects").text


def test_a_company_status_saved_under_an_old_domain_spelling_follows_the_card(tmp_path):
    """Before domains were normalised, a companies.csv row `Gamma,www.Gamma.dev` saved `www.Gamma.dev.json` and the
    check-careers status was keyed by that spelling; the next run saves `gamma.dev.json`. One card, status kept."""
    write_run(tmp_path, "20260108-090000", [result("Gamma", "www.Gamma.dev", fit=fit(60, "check_careers"))])
    old = db.connect(tmp_path)
    old.executescript("BEGIN; DELETE FROM user_schema; INSERT INTO user_schema VALUES (2); COMMIT;")  # pre-migration
    with old:
        old.execute("INSERT INTO tracking VALUES ('company', 'www.Gamma.dev', 'saved', 'Ask Ana', NULL, '2026-01-08T10:00:00')")
        old.execute("INSERT INTO tracking VALUES ('company', 'https://Delta.io/careers', 'saved', '', NULL, '2026-01-08T10:00:00')")
        old.execute("INSERT INTO tracking VALUES ('company', 'delta.io', 'applied', 'twin', '2026-01-09', '2026-01-09T10:00:00')")
        old.execute("INSERT INTO mail_decisions VALUES ('<m1>', 'applied', 'company', 'www.Gamma.dev', 'rejected', '2026-01-08T11:00:00')")
    old.close()
    write_run(tmp_path, "20260109-090000", [result("Gamma", "gamma.dev", fit=fit(62, "check_careers"))])

    app_client = client(create_app(tmp_path))
    board = app_client.get("/?verdict=check_careers").text.split('id="results"')[1]
    assert board.count("Gamma") == 1 and "www.Gamma.dev" not in board  # one card, under the normalised domain
    page = app_client.get("/c/gamma.dev", headers={"HX-Request": "true"}).text
    assert "Ask Ana" in page and "62" in page
    conn = db.connect(tmp_path)
    refs = {r["ref"]: r["status"] for r in conn.execute("SELECT ref, status FROM tracking WHERE kind = 'company'")}
    assert refs["gamma.dev"] == "saved" and refs["delta.io"] == "applied"  # the twin that already matched the card wins
    assert len(refs) == 3 and list(refs.values()).count("saved") == 2  # the other Delta row is kept, unmatched, not lost
    assert conn.execute("SELECT ref FROM mail_decisions").fetchone()["ref"] == "gamma.dev"
    assert conn.execute("SELECT version FROM user_schema").fetchone()[0] == len(db.USER_MIGRATIONS)
