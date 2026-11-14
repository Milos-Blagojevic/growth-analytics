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
INVITE_ETA = mail_update(5, "interview", "Eta", role="Data Analyst")  # applied to outside the pipeline files
REJECTED_ETA = mail_update(6, "rejected", "Eta GmbH", role="Data Analyst")


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


def cards(root, status):
    conn = db.connect(root)
    try:
        return [(i["kind"], i["title"], i["name"], i["url"]) for i in tracking.pipeline(conn)[status]]
    finally:
        conn.close()


def decide(app_client, update, action, **item):
    return app_client.post("/mail/decide", data={"update": update["id"], "action": action, **item})


def test_pipeline_suggests_status_changes_from_email(root, app_client):
    page = app_client.get("/pipeline").text

    assert 'From your email <span class="count">3</span>' in page
    assert suggested(root) == [
        ("Zeta Labs", "rejected", []),  # nothing tracked matches: shown, to add to the pipeline or dismiss
        ("", "interviewing", ["Data Engineer"]),
        ("Acme Inc.", "rejected", ["Analytics Engineer", "Growth Analyst"]),  # the role named in the email first
    ]  # Gamma is rejected already, so its update needs nothing from you
    assert page.count("Add to pipeline as rejected") == 1 and "Add to pipeline as interviewing" not in page  # Zeta only


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


def test_an_update_that_matches_nothing_tracked_is_added_to_the_pipeline(root, app_client):
    write_mail(root, "20261017-090000", [INVITE_ETA])
    assert decide(app_client, REJECTED_ACME, "add").status_code == 400  # Acme is tracked: its status is set instead

    response = decide(app_client, INVITE_ETA, "add")

    assert response.status_code == 204 and response.headers["HX-Refresh"] == "true"
    assert cards(root, "interviewing") == [("email", "Data Analyst", "Eta", "https://outlook.live.com/5")]
    menu = app_client.get("/pipeline").text.split('"kind": "email"')[1].split("</select>")[0]
    assert 'value="interviewing" selected' in menu and 'value="new"' not in menu  # no Board card to go back to as New
    conn = db.connect(root)
    answer = conn.execute("SELECT decision, kind, ref, status FROM mail_decisions WHERE update_id = ?",
                          [INVITE_ETA["id"]]).fetchone()
    conn.close()
    assert tuple(answer) == ("applied", "email", INVITE_ETA["id"], "interviewing")
    app_client.post("/track", data={"kind": "email", "ref": INVITE_ETA["id"], "status": "applied"})
    assert "Eta" not in [s[0] for s in suggested(root)]  # answered: not suggested again when its card moves back
    assert decide(app_client, INVITE_ETA, "add").status_code == 409


def test_a_later_email_from_the_company_moves_the_added_application(root, app_client):
    write_mail(root, "20261017-090000", [INVITE_ETA])
    decide(app_client, INVITE_ETA, "add")
    write_mail(root, "20261018-090000", [REJECTED_ETA])

    app_client.get("/pipeline")  # reads the new mail file
    assert ("Eta GmbH", "rejected", ["Data Analyst"]) in suggested(root)
    assert decide(app_client, REJECTED_ETA, "apply", kind="email", ref=INVITE_ETA["id"]).status_code == 204
    assert cards(root, "interviewing") == [] and "Eta" in [card[2] for card in cards(root, "rejected")]


def test_an_added_application_keeps_its_card_without_the_mail_file(root, app_client):
    unnamed = mail_update(7, "rejected", "", sender="ana@theta.io")  # the email names no company and no role
    path = write_mail(root, "20261017-090000", [INVITE_ETA, unnamed])
    decide(app_client, INVITE_ETA, "add")
    decide(app_client, unnamed, "add")
    path.unlink()  # hunt.db copied to a machine without mail/, or the file removed

    page = app_client.get("/pipeline").text

    assert "Data Analyst" in page and [card[2] for card in cards(root, "interviewing")] == ["Eta"]
    unnamed_card = ("email", "Role not named in the email", "ana@theta.io", "https://outlook.live.com/7")
    assert unnamed_card in cards(root, "rejected")  # labelled by the sender


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


@pytest.mark.parametrize(
    ("company", "sender", "item", "same"),
    [
        ("Acme Inc.", "no-reply@greenhouse-mail.io", {"name": "Acme", "domain": "acme.com"}, True),
        ("Acme Analytics", "no-reply@greenhouse-mail.io", {"name": "Acme", "domain": "acme.com"}, True),
        ("SE Ranking", "jobs@lever.co", {"name": "Seranking", "domain": "seranking.com"}, True),
        ("", "talent@mail.beta.io", {"name": "Beta", "domain": "beta.io"}, True),
        ("Acme Analytics", "no-reply@greenhouse-mail.io", {"name": "Analytics Co", "domain": "analyticsco.io"}, False),
        ("Betamax", "no-reply@greenhouse-mail.io", {"name": "Beta", "domain": "beta.io"}, False),
        ("", "no-reply@greenhouse-mail.io", {"name": "Beta", "domain": "beta.io"}, False),
    ],
)
def test_company_matching(company, sender, item, same):
    assert email_updates.same_company({"company": company, "from_address": sender}, item) is same


def test_only_https_outlook_links_are_shown(tmp_path):
    unsafe = mail_update(1, "rejected", "Zeta") | {"web_link": "javascript:alert(1)"}
    safe = mail_update(2, "rejected", "Eta")
    write_mail(tmp_path, "20261015-090000", [unsafe, safe])

    app_client = client(create_app(tmp_path))
    page = app_client.get("/pipeline").text

    assert "javascript:" not in page and 'href="https://outlook.live.com/2"' in page

    for update in (unsafe, safe):  # the cards of added applications link to the email under the same rule
        assert decide(app_client, update, "add").status_code == 204
    page = app_client.get("/pipeline").text

    assert "javascript:" not in page and 'href="https://outlook.live.com/2"' in page
