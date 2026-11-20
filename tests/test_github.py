import json
from urllib.parse import parse_qs, urlparse

import pytest
import requests

from clients import github

API = "https://api.github.com/repos/me/repo"


def test_starting_a_workflow_returns_the_page_of_the_run_it_started(http):
    http.post(f"{API}/actions/workflows/factory.yml/dispatches",
              json={"workflow_run_id": 7, "run_url": f"{API}/actions/runs/7", "html_url": "https://github.com/me/repo/actions/runs/7"})

    url = github.start_workflow("me/repo", "factory.yml", "main", {"task_id": "20261118-143000", "task": "Fix it"}, "tok")

    request = http.calls[0].request
    assert url == "https://github.com/me/repo/actions/runs/7"
    assert json.loads(request.body) == {"ref": "main", "inputs": {"task_id": "20261118-143000", "task": "Fix it"}}
    # An older API version starts the run too, but answers 204 with no run to link to.
    assert request.headers["X-GitHub-Api-Version"] == "2026-03-10" and request.headers["Authorization"] == "Bearer tok"
    assert request.req_kwargs["timeout"] == 15  # every request: GitHub must not be able to hang the bot or the job


def test_a_run_started_without_details_is_still_a_start(http):
    http.post(f"{API}/actions/workflows/factory.yml/dispatches", status=204)

    assert github.start_workflow("me/repo", "factory.yml", "main", {}, "tok") is None


def test_failures_carry_githubs_reason_and_never_the_token(http):
    http.post(f"{API}/pulls", status=422, json={"message": "Validation Failed", "errors": [
        {"resource": "PullRequest", "message": "A pull request already exists for me:factory/x."}]})
    http.get(f"{API}/actions/workflows/factory.yml/runs", status=403, body="<html>blocked</html>")
    http.post(f"{API}/actions/workflows/factory.yml/dispatches", body=requests.ConnectionError("https://api.github.com tok-secret"))

    with pytest.raises(RuntimeError) as refused:
        github.open_pull("me/repo", "tok-secret", head="factory/x", base="main", title="t", body="b")
    with pytest.raises(RuntimeError) as blocked:
        github.workflow_runs("me/repo", "factory.yml", "tok-secret")
    with pytest.raises(RuntimeError) as dropped:
        github.start_workflow("me/repo", "factory.yml", "main", {}, "tok-secret")

    assert str(refused.value) == ("GitHub answered HTTP 422: Validation Failed: "
                                  "A pull request already exists for me:factory/x.")
    assert str(blocked.value) == "GitHub answered HTTP 403"
    assert str(dropped.value) == "GitHub request failed: ConnectionError"


def test_the_pull_request_of_a_branch_is_looked_up_by_that_branch(http):
    http.get(f"{API}/pulls", json=[{"number": 5, "title": "Fix it", "state": "closed", "merged_at": "2026-11-18T15:00:00Z",
                                    "draft": False, "html_url": "https://github.com/me/repo/pull/5"}])

    pull = github.pull_for_branch("me/repo", "factory/20261118-143000", "tok")

    query = parse_qs(urlparse(http.calls[0].request.url).query)
    assert query["head"] == ["me:factory/20261118-143000"] and query["state"] == ["all"]  # closed and merged ones too
    assert pull == {"number": 5, "title": "Fix it", "state": "closed", "merged": True, "draft": False,
                    "html_url": "https://github.com/me/repo/pull/5"}


def test_a_branch_without_a_pull_request_has_none(http):
    http.get(f"{API}/pulls", json=[])

    assert github.pull_for_branch("me/repo", "factory/20261118-143000", "tok") is None
