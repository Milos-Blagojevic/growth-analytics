import json

import pytest

from dashboard import db, email_updates, tracking
from dashboard.app import create_app
from dashboard_data import client, fit, mail_update, result, write_mail, write_run

ACME_URL = "https://jobs.ashbyhq.com/acme/1"
ACME_WATCH_URL = "https://jobs.ashbyhq.com/acme/w1"
BETA_URL = "https://beta.io/jobs/1"
REJECTED_ACME = mail_update(1, "rejected", "Acme Inc.", role="Analytics Engineer")
INVITE_BETA = mail_update(2, "interview", "", sender="ana@beta.io")  # no company named: matched by sender domain
REJECTED_GAMMA = mail_update(3, "rejected", "Gamma")
REJECTED_ZETA = mail_update(4, "rejected", "Zeta Labs")


@pytest.fixture
def root(tmp_path):
    write_run(tmp_path, "20261001-090000", [
        result("Acme", "acme.com", fit=fit(82, "apply", "Analytics Engineer", ACME_URL)),
        result("Beta", "beta.io", fit=fit(78, "apply", "Data Engineer", BETA_URL)),
        result("Gamma", "gamma.dev", fit=fit(60, "check_careers")),
    ])
    (tmp_path / "watch_state.json").write_text(json.dumps(
        {"acme": {"w1": {"title": "Growth Analyst", "url": ACME_WATCH_URL, "first_seen": "2026-10-01T10:00:00"}}}))
    write_mail(tmp_path, "20261015-090000", [REJECTED_ACME, INVITE_BETA, REJECTED_GAMMA])
    write_mail(tmp_path, "20261016-090000", [REJECTED_ACME, REJECTED_ZETA])  # a re-sent update counts once
    return tmp_path


@pytest.fixture
def app_client(root):
    app_client = client(create_app(root))
    for kind, ref, status in (("posting", ACME_WATCH_URL, "applied"), ("posting", ACME_URL, "applied"),
                              ("posting", BETA_URL, "applied"), ("company", "gamma.dev", "rejected")):
        assert app_client.post("/track", data={"kind": kind, "ref": ref, "status": status}).status_code == 200
    return app_client


def suggested(root):
    conn = db.connect(root)
    try:
        return [(s["company"], s["status"], [i["title"] for i in s["targets"]]) for s in email_updates.suggestions(conn)]
    finally:
        conn.close()


def decide(app_client, update, action, **item):
    return app_client.post("/mail/decide", data={"update": update["id"], "action": action, **item})


def test_pipeline_suggests_status_changes_from_email(root, app_client):
    page = app_client.get("/pipeline").text

    assert 'From your email <span class="count">3</span>' in page
    assert suggested(root) == [
        ("Zeta Labs", "rejected", []),  # nothing tracked matches: shown, with Dismiss only
        ("", "interviewing", ["Data Engineer"]),
        ("Acme Inc.", "rejected", ["Analytics Engineer", "Growth Analyst"]),  # the role named in the email first
    ]  # Gamma is rejected already, so its update needs nothing from you
    assert "None of your tracked applications matches this company" in page


def test_confirming_sets_the_status_once(root, app_client):
    assert decide(app_client, INVITE_BETA, "apply", kind="posting", ref=ACME_URL).status_code == 400

    response = decide(app_client, REJECTED_ACME, "apply", kind="posting", ref=ACME_URL)

    assert response.status_code == 204 and response.headers["HX-Refresh"] == "true"
    conn = db.connect(root)
    acme, other = tracking.get(conn, "posting", ACME_URL), tracking.get(conn, "posting", ACME_WATCH_URL)
    conn.close()
    assert (acme["status"], other["status"]) == ("rejected", "applied")
    assert [s[0] for s in suggested(root)] == ["Zeta Labs", ""]
    assert decide(app_client, REJECTED_ACME, "apply", kind="posting", ref=ACME_WATCH_URL).status_code == 409


def test_handled_updates_stay_handled_after_reingest_and_schema_changes(root, app_client):
    assert decide(app_client, REJECTED_ZETA, "dismiss").status_code == 204
    decide(app_client, INVITE_BETA, "apply", kind="posting", ref=BETA_URL)

    conn = db.connect(root)
    conn.execute("PRAGMA user_version = 0")  # as after a SCHEMA_VERSION bump
    conn.commit()
    conn.close()
    write_mail(root, "20261017-090000", [REJECTED_ZETA, INVITE_BETA])  # sent again by a later check

    assert app_client.get("/pipeline").status_code == 200
    assert [s[0] for s in suggested(root)] == ["Acme Inc."]
