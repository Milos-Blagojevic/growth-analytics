"""Mock Exa API for tests.

Payloads use Exa's wire format (camelCase JSON as returned by api.exa.ai) and are served over HTTP
through `responses`, so the real exa-py SDK serializes requests and parses responses exactly as in
production. Use the `exa` fixture (conftest.py) to register handlers.
"""

import json
import re
from urllib.parse import parse_qs, urlparse

import responses

SEARCH_URL = "https://api.exa.ai/search"
CONTENTS_URL = "https://api.exa.ai/contents"
AGENT_URL = "https://api.exa.ai/agent/runs"
MONITORS_URL = "https://api.exa.ai/monitors"
NEXT_RUN = "2026-11-18T12:15:12.375Z"  # what a GET says of an active monitor's next run


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


def monitor_run(run_id, *, status="completed", results=(), content=None, grounding=(), fail_reason=None):
    """A monitor run as GET /monitors/<id>/runs/<run id> returns it. A completed run that found nothing new has
    output {"results": []}, as the live API answers; a run with another status has no output.

    results: the new pages, as result(...) builds them; content: the outputSchema-shaped JSON written from them;
    grounding: rows as grounded(...) builds them.
    """
    output = None
    if status == "completed":
        output = {"results": list(results)}
        if results:
            output |= {"content": content, "grounding": list(grounding)}
    return {
        "id": run_id,
        "monitorId": None,  # set when the run is added to a monitor
        "status": status,
        "output": output,
        "failReason": fail_reason,
        "startedAt": "2026-11-18T11:31:02.815Z",
        "completedAt": "2026-11-18T11:31:04.041Z" if status == "completed" else None,
        "failedAt": "2026-11-18T11:31:04.041Z" if status == "failed" else None,
        "cancelledAt": None,
        "durationMs": 1466,
        "createdAt": "2026-11-18T11:31:02.815Z",
        "updatedAt": "2026-11-18T11:31:04.042Z",
    }


class MonitorsMock:
    """Exa's Monitors API over an in-memory account, in the wire format the live API answered with (2026-11-18).

    monitors: {id: the monitor as Exa returns it}, in creation order. add_run() puts a run in front of a monitor's
    history. The list of runs carries a run's pages but not what was written from them: a run is read by id for
    that, as Exa's docs say. failures[<operation>] holds replies served first, one per request (an error(...)
    tuple or an exception to raise); the operations are "create", "list", "get", "update", "delete", "runs", "run".
    unreadable: ids of runs that answer 500 whenever they are read by id.
    """

    def __init__(self, http):
        self._http = http
        self.monitors, self.runs, self.failures, self._created, self.unreadable = {}, {}, {}, 0, set()
        collection, query = re.escape(MONITORS_URL), r"(\?.*)?$"
        one = collection + r"/(?P<monitor_id>[^/?]+)"
        self._route(responses.POST, collection + "$", "create", self._create)
        self._route(responses.GET, collection + query, "list", self._list)
        self._route(responses.GET, one + "$", "get", self._get)
        self._route(responses.PATCH, one + "$", "update", self._update)
        self._route(responses.DELETE, one + "$", "delete", self._delete)
        self._route(responses.GET, one + "/runs" + query, "runs", self._runs)
        self._route(responses.GET, one + r"/runs/(?P<run_id>[^/?]+)$", "run", self._run)

    def add_run(self, monitor_id, run):
        self.runs.setdefault(monitor_id, []).insert(0, {**run, "monitorId": monitor_id})

    def calls(self, method, path=""):
        """Decoded JSON bodies (None for a request without one) of the `method` requests to MONITORS_URL + path."""
        return [json.loads(call.request.body) if call.request.body else None for call in self._http.calls
                if call.request.method == method and call.request.url.split("?")[0] == MONITORS_URL + path]

    def _route(self, method, pattern, operation, handler):
        pattern = re.compile(pattern)

        def callback(request):
            if self.failures.get(operation):
                reply = self.failures[operation].pop(0)
                if isinstance(reply, BaseException):
                    raise reply
                return reply[0], {}, json.dumps(reply[1])
            status, payload = handler(request, **pattern.match(request.url).groupdict())
            return status, {}, json.dumps(payload)

        self._http.add_callback(method, pattern, callback=callback, content_type="application/json")

    @staticmethod
    def _page(items, request):
        query = parse_qs(urlparse(request.url).query)
        start, limit = int(query.get("cursor", ["0"])[0]), int(query.get("limit", ["20"])[0])
        more = start + limit < len(items)
        return 200, {"data": items[start:start + limit], "hasMore": more, "nextCursor": str(start + limit) if more else None}

    def _missing(self, monitor_id):
        if monitor_id not in self.monitors:
            return 404, {"requestId": "req_test", "error": "Search monitor not found", "tag": "NOT_FOUND"}
        return None

    def _create(self, request):
        body = json.loads(request.body)
        self._created += 1
        monitor = {
            "id": f"mon_{self._created}",
            "name": body.get("name"),
            "status": "active",
            "search": body["search"],
            "trigger": body.get("trigger"),
            "outputSchema": body.get("outputSchema"),
            "metadata": body.get("metadata"),
            "webhook": body["webhook"],
            "nextRunAt": None,  # set a moment later: a GET has it
            "createdAt": "2026-11-18T11:31:01.513Z",
            "updatedAt": "2026-11-18T11:31:01.558Z",
        }
        self.monitors[monitor["id"]] = {**monitor, "nextRunAt": NEXT_RUN if monitor["trigger"] else None}
        self.runs.setdefault(monitor["id"], [])
        return 201, {**monitor, "webhookSecret": "secret-returned-once"}

    def _list(self, request):
        return self._page(list(self.monitors.values()), request)

    def _get(self, request, monitor_id):
        return self._missing(monitor_id) or (200, self.monitors[monitor_id])

    def _update(self, request, monitor_id):
        if self._missing(monitor_id):
            return self._missing(monitor_id)
        changes = json.loads(request.body)
        monitor = self.monitors[monitor_id]
        monitor |= {**changes, "search": {**monitor["search"], **changes.get("search", {})}}
        return 200, {**monitor, "nextRunAt": None}

    def _delete(self, request, monitor_id):
        return self._missing(monitor_id) or (200, self.monitors.pop(monitor_id))

    def _runs(self, request, monitor_id):
        if self._missing(monitor_id):
            return self._missing(monitor_id)
        listed = [{**run, "output": {"results": run["output"]["results"]}} if run["output"] else run
                  for run in self.runs[monitor_id]]
        return self._page(listed, request)

    def _run(self, request, monitor_id, run_id):
        if self._missing(monitor_id):
            return self._missing(monitor_id)
        if run_id in self.unreadable:
            return 500, {"requestId": "req_test", "error": "Internal error", "tag": "INTERNAL_ERROR"}
        return 200, next(run for run in self.runs[monitor_id] if run["id"] == run_id)


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
        self._monitors = None

    def monitors(self):
        """The fake Monitors API (MonitorsMock), its routes registered at the first call."""
        if self._monitors is None:
            self._monitors = MonitorsMock(self._http)
        return self._monitors

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
