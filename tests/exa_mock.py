"""Mock Exa API for tests.

Payloads use Exa's wire format (camelCase JSON as returned by api.exa.ai) and are served over HTTP
through `responses`, so the real exa-py SDK serializes requests and parses responses exactly as in
production. Use the `exa` fixture (conftest.py) to register handlers.
"""

import json
import re

import responses

SEARCH_URL = "https://api.exa.ai/search"
CONTENTS_URL = "https://api.exa.ai/contents"
AGENT_URL = "https://api.exa.ai/agent/runs"


def result(url, *, title="", highlights=None, text=None):
    """One entry of a /search or /contents `results` list."""
    entry = {"id": url, "url": url, "title": title}
    if highlights is not None:
        entry["highlights"] = highlights
    if text is not None:
        entry["text"] = text
    return entry


def search_response(results=(), *, cost=0.007, search_time=450):
    """/search body. cost=None omits costDollars (Exa does this for some request types)."""
    payload = {
        "requestId": "req-test",
        "resolvedSearchType": "neural",
        "results": list(results),
        "searchTime": search_time,
    }
    if cost is not None:
        payload["costDollars"] = {"total": cost}
    return payload


def grounded(field, *urls, confidence="high"):
    """One `output.grounding` row: field path (e.g. "open_roles[0].url"), cited URLs, confidence."""
    return {"field": field, "citations": [{"url": u, "title": u} for u in urls], "confidence": confidence}


def structured_response(content, grounding=(), *, cost=0.012):
    """/search body for a request with outputSchema: synthesized JSON in output.content."""
    payload = search_response(cost=cost)
    payload["output"] = {"content": content, "grounding": list(grounding)}
    return payload


def contents_response(results=(), statuses=(), *, cost=0.001):
    """/contents body. statuses: [(url, "success" | "error")]."""
    return {
        "requestId": "req-test",
        "results": list(results),
        "statuses": [{"id": url, "status": state, "source": "live"} for url, state in statuses],
        "costDollars": {"total": cost},
    }


def agent_run(structured=None, grounding=(), *, status="completed", cost=0.205, partners=None, error=None):
    """An agent run as GET /agent/runs/<id> returns it once it has ended (or status="running": it never ends).

    grounding: rows as grounded(...) builds them; the API names the fields "structured.<field>".
    cost: the run's total in dollars, of which `partners` ({"fiber": dollars}) is the Exa Connect share.
    """
    return {
        "id": "agent_run_test",
        "object": "agent_run",
        "status": status,
        "stopReason": {"completed": "schema_satisfied", "failed": "error"}.get(status),
        "createdAt": "2026-11-10T10:00:00.000Z",
        "completedAt": None if status == "running" else "2026-11-10T10:00:40.000Z",
        "request": None,
        "error": {"type": "SERVER_ERROR", "code": "SERVER_ERROR", "message": error} if error else None,
        "output": {
            "text": "",
            "structured": structured,
            "grounding": [{**row, "field": f"structured.{row['field']}"} for row in grounding],
        } if status == "completed" else None,
        "usage": {"agentComputeUnits": 0.1, "searches": 3, "emails": 0, "phoneNumbers": 0,
                  "dataSources": {name: 1 for name in partners or {}}},
        "costDollars": {"total": cost, "agentCompute": 0.01, "search": 0.015, "emails": 0, "phoneNumbers": 0,
                        "dataSources": partners or {}},
    }  # fmt: skip


def error(status, message="mocked Exa error"):
    """An HTTP error reply; the SDK turns it into ValueError("Request failed with status code N: ...")."""
    return status, {"error": message}


class ExaMock:
    """Routes Exa endpoints to handlers.

    A handler is one of:
    - a payload dict (served for every request),
    - a list of replies, consumed one per request (for retry sequences),
    - a callable taking the decoded request body and returning a reply.
    A reply is a payload dict, an `error(...)` tuple, or an exception instance to raise
    (e.g. requests.ConnectionError()).
    """

    def __init__(self, http):
        self._http = http

    def on_search(self, handler):
        self._route(SEARCH_URL, handler)

    def on_contents(self, handler):
        self._route(CONTENTS_URL, handler)

    def on_agent(self, handler, *, checks=(), cancels=()):
        """Routes agent runs. The handler's reply to a create request is the run as it ends (agent_run(...)): the
        create itself answers "queued" and every status check answers that reply. A reply with status "running"
        never ends until the run is cancelled. An error(...) tuple or an exception answers the create itself.

        checks, cancels: what the first status checks and the first cancel requests get instead, one each: an
        error(...) tuple, an exception, or a run payload (to answer "running" between failures)."""
        runs = {}
        failing = {"checks": list(checks), "cancels": list(cancels)}

        def failed(kind):
            if not failing[kind]:
                return None
            reply = failing[kind].pop(0)
            if isinstance(reply, BaseException):  # KeyboardInterrupt included
                raise reply
            if isinstance(reply, dict):
                return 200, {}, json.dumps(reply)
            return reply[0], {}, json.dumps(reply[1])

        def queued(run):
            run_id = f"agent_run_{len(runs) + 1}"
            runs[run_id] = {**run, "id": run_id}
            return {"id": run_id, "object": "agent_run", "status": "queued"}

        def status(request):
            return failed("checks") or (200, {}, json.dumps(runs[request.url.rsplit("/", 1)[-1]]))

        def cancel(request):
            refused = failed("cancels")
            if refused:
                return refused
            run_id = request.url.split("/")[-2]
            runs[run_id] = {**runs[run_id], "status": "cancelled", "stopReason": "cancelled", "output": None}
            return 200, {}, json.dumps(runs[run_id])

        self._route(AGENT_URL, handler, wrap=queued)
        one_run = re.escape(AGENT_URL) + "/[^/]+"
        self._http.add_callback(responses.GET, re.compile(one_run + "$"), callback=status, content_type="application/json")
        self._http.add_callback(
            responses.POST, re.compile(one_run + "/cancel$"), callback=cancel, content_type="application/json"
        )

    def requests(self, url=SEARCH_URL):
        """Decoded JSON bodies of the requests sent to `url`, in order."""
        return [json.loads(call.request.body) for call in self._http.calls if call.request.url == url]

    def _route(self, url, handler, wrap=None):
        queue = list(handler) if isinstance(handler, list) else None

        def callback(request):
            if queue is not None:
                if not queue:
                    raise AssertionError(f"unexpected extra request to {url}")
                reply = queue.pop(0)
            elif callable(handler):
                reply = handler(json.loads(request.body))
            else:
                reply = handler
            if isinstance(reply, BaseException):  # KeyboardInterrupt included
                raise reply
            status, payload = reply if isinstance(reply, tuple) else (200, wrap(reply) if wrap else reply)
            return status, {}, json.dumps(payload)

        self._http.add_callback(responses.POST, url, callback=callback, content_type="application/json")
