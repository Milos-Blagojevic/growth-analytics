"""GitHub REST API for the factory: the chat bot starts the Factory workflow and looks up its runs and pull
requests, the worker opens the pull request. The only place that talks to api.github.com.

Every function takes the token to use, because the two callers hold different ones: the chat bot has
FACTORY_DISPATCH_TOKEN (starts workflows, cannot push) and the worker FACTORY_PUSH_TOKEN (pushes branches and opens
pull requests). See factory/README.md.
"""

import requests

API = "https://api.github.com"
# From this version on, starting a workflow answers 200 with the run it started; earlier ones answer 204 with nothing.
API_VERSION = "2026-03-10"
TIMEOUT = 15


def _call(method, path, token, **kwargs):
    """The parsed JSON of one request (None for an empty answer). Raises RuntimeError with GitHub's own message on
    failure; the token is only ever in a header, so no error text carries it."""
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": API_VERSION}
    try:
        response = requests.request(method, f"{API}{path}", headers=headers, timeout=TIMEOUT, **kwargs)
    except requests.RequestException as exc:
        raise RuntimeError(f"GitHub request failed: {type(exc).__name__}") from None
    try:
        data = response.json()
    except ValueError:
        data = None
    if response.status_code >= 400:
        detail = ""
        if isinstance(data, dict):  # {"message": "Validation Failed", "errors": [{"message": "A pull request already exists"}]}
            reasons = [e.get("message") for e in data.get("errors") or [] if isinstance(e, dict)]
            detail = ": ".join(filter(None, [data.get("message"), *reasons]))
        raise RuntimeError(f"GitHub answered HTTP {response.status_code}" + (f": {detail}" if detail else ""))
    return data


def start_workflow(repo, workflow, ref, inputs, token):
    """Starts a workflow_dispatch run of `workflow` (its file name) on the branch `ref`. Returns the run's page, or
    None when GitHub started it without saying which run it is."""
    data = _call("POST", f"/repos/{repo}/actions/workflows/{workflow}/dispatches", token,
                 json={"ref": ref, "inputs": inputs})
    return data.get("html_url") if isinstance(data, dict) else None


def workflow_runs(repo, workflow, token, limit=10):
    """The newest runs of a workflow: title (its run-name), status (queued, in_progress, completed), conclusion
    (success, failure, cancelled, ...; None until it completes) and html_url."""
    data = _call("GET", f"/repos/{repo}/actions/workflows/{workflow}/runs", token, params={"per_page": limit})
    return [{"title": run["display_title"], "status": run["status"], "conclusion": run["conclusion"],
             "html_url": run["html_url"]} for run in data["workflow_runs"]]


def pull_for_branch(repo, branch, token):
    """The newest pull request from `branch` of this repository, open or closed, or None: number, title, state
    (open, closed), merged, draft and html_url."""
    owner = repo.split("/")[0]
    data = _call("GET", f"/repos/{repo}/pulls", token,
                 params={"head": f"{owner}:{branch}", "state": "all", "sort": "created", "direction": "desc", "per_page": 1})
    if not data:
        return None
    pull = data[0]
    return {"number": pull["number"], "title": pull["title"], "state": pull["state"], "merged": bool(pull["merged_at"]),
            "draft": pull["draft"], "html_url": pull["html_url"]}


def open_pull(repo, token, *, head, base, title, body, draft=False):
    """Opens a pull request from the branch `head` into `base`. Returns its number and html_url."""
    data = _call("POST", f"/repos/{repo}/pulls", token,
                 json={"head": head, "base": base, "title": title, "body": body, "draft": draft})
    return {"number": data["number"], "html_url": data["html_url"]}
