import pytest

from dashboard import db, ingest, projects
from dashboard.app import create_app
from dashboard_data import client, fit, result, write_run

IDEAS = ["Ship a feature-flag experiment harness", "Write up a growth loop teardown"]


@pytest.fixture
def root(tmp_path):
    write_run(
        tmp_path,
        "20260108-090000",
        [
            result(
                "PostHog",
                "posthog.com",
                fit=fit(72, "stretch", "Growth Engineer", "https://posthog.com/g", gap_note="6 months.", proof_ideas=IDEAS),
            )
        ],
    )
    return tmp_path


@pytest.fixture
def app_client(root):
    return client(create_app(root))


def board(root):
    conn = db.connect(root)
    try:
        ingest.refresh(conn, root)
        return projects.board(conn), projects.suggestions(conn)
    finally:
        conn.close()


def test_scorer_ideas_are_suggested_until_accepted_or_dismissed(root, app_client):
    _, suggestions = board(root)
    assert [(s["text"], s["gap"], s["name"]) for s in suggestions] == [(i, "6 months.", "PostHog") for i in IDEAS]

    harness, teardown = suggestions
    assert app_client.post(f"/projects/suggestions/{harness['key']}", data={"action": "accept"}).status_code == 204
    assert app_client.post(f"/projects/suggestions/{teardown['key']}", data={"action": "dismiss"}).status_code == 204

    columns, suggestions = board(root)
    assert suggestions == []
    (idea,) = columns["idea"]
    assert (idea["title"], idea["source"], idea["origin_domain"], idea["gap"]) == (IDEAS[0], "scorer", "posthog.com", "6 months.")
    page = app_client.get("/projects").text
    assert IDEAS[0] in page and IDEAS[1] not in page

    # Deleting an accepted scorer idea hides it rather than letting it be suggested again.
    assert app_client.post(f"/projects/{idea['id']}/delete").status_code == 204
    columns, suggestions = board(root)
    assert columns["idea"] == [] and suggestions == []


def test_a_rescored_company_does_not_resuggest_handled_ideas(root, app_client):
    _, suggestions = board(root)
    app_client.post(f"/projects/suggestions/{suggestions[0]['key']}", data={"action": "accept"})
    reworded = " ".join(IDEAS[0].upper().split())  # same idea, different case and spacing
    write_run(
        root,
        "20260109-090000",
        [result("PostHog", "posthog.com", fit=fit(74, "stretch", "Growth Engineer", "https://posthog.com/g", proof_ideas=[reworded, "Build a pricing-page A/B test"]))],
    )

    app_client.get("/projects")  # ingests the new run
    _, suggestions = board(root)
    assert [s["text"] for s in suggestions] == ["Build a pricing-page A/B test"]


def test_own_projects_can_be_created_moved_edited_and_deleted(root, app_client):
    assert app_client.post("/projects", data={"title": "  ", "description": "x"}).status_code == 400
    response = app_client.post("/projects", data={"title": "dbt metrics package", "gap": "Open source", "repo_url": ""})
    assert (response.status_code, response.headers["HX-Redirect"]) == (204, "/projects")

    (project,) = board(root)[0]["idea"]
    app_client.post(f"/projects/{project['id']}", data={"status": "active"})
    app_client.post(f"/projects/{project['id']}", data={"title": "dbt metrics package v2", "description": "Semantic layer",
                                                       "gap": "Open source", "repo_url": "https://github.com/me/pkg"})
    columns, _ = board(root)
    (active,) = columns["active"]
    assert (active["title"], active["description"], active["repo_url"]) == ("dbt metrics package v2", "Semantic layer", "https://github.com/me/pkg")
    assert app_client.post(f"/projects/{project['id']}", data={"status": "shipped"}).status_code == 400

    app_client.post(f"/projects/{project['id']}/delete")
    conn = db.connect(root)
    assert conn.execute("SELECT COUNT(*) FROM side_projects").fetchone()[0] == 0
    conn.close()
