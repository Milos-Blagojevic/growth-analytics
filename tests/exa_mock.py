"""Mock Exa API for tests.

Payloads use Exa's wire format (camelCase JSON as returned by api.exa.ai) and are served over HTTP
through `responses`, so the real exa-py SDK serializes requests and parses responses exactly as in
production. Use the `exa` fixture (conftest.py) to register handlers.
"""

import json

import responses

SEARCH_URL = "https://api.exa.ai/search"
CONTENTS_URL = "https://api.exa.ai/contents"


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

    def requests(self, url=SEARCH_URL):
        """Decoded JSON bodies of the requests sent to `url`, in order."""
        return [json.loads(call.request.body) for call in self._http.calls if call.request.url == url]

    def _route(self, url, handler):
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
            if isinstance(reply, Exception):
                raise reply
            status, payload = reply if isinstance(reply, tuple) else (200, reply)
            return status, {}, json.dumps(payload)

        self._http.add_callback(responses.POST, url, callback=callback, content_type="application/json")
