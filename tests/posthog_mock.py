"""PostHog's ingestion endpoints in their wire format, for tests (registered on the `responses` mock): capture v1's
POST /i/v1/analytics/events and POST /i/v1/ai/events for events, and OTLP's POST /i/v1/logs for log records."""

import json
from datetime import datetime

import responses
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest

from clients import posthog_client

HOST = "https://eu.i.posthog.com"
TOKEN = "phc_test"
EVENTS_PATH = "/i/v1/analytics/events"
AI_EVENTS_PATH = "/i/v1/ai/events"


class PostHogMock:
    """Turns PostHog on for the test (service "tests"; POSTHOG_PROJECT_TOKEN and POSTHOG_HOST are set too, for code
    that configures it from the environment) and collects what reaches it. `events()` and `records()` first send
    what is queued. `status` is what every endpoint answers with, and only what got a 200 counts as received: 400 is
    a refusal the senders do not retry, 503 an outage. The AI endpoint drops an event that is not an AI event, as
    PostHog does. `requests()` is every request made, answered or not."""

    def __init__(self, http, monkeypatch):
        monkeypatch.setenv("POSTHOG_PROJECT_TOKEN", TOKEN)
        monkeypatch.setenv("POSTHOG_HOST", HOST)
        self.status = 200
        self._events, self._exports, self._requests = [], [], []
        for path in (EVENTS_PATH, AI_EVENTS_PATH):
            http.add_callback(responses.POST, f"{HOST}{path}", callback=self._capture, content_type="application/json")
        http.add_callback(responses.POST, f"{HOST}{posthog_client.LOGS_PATH}", callback=self._logs)
        posthog_client.configure(TOKEN, HOST, service="tests")

    def _capture(self, request):
        self._requests.append((request.path_url, request.req_kwargs.get("timeout")))
        if self.status != 200:
            return self.status, {}, json.dumps({"error": "unavailable", "error_description": "answered by the test"})
        results = {}
        for event in json.loads(request.body)["batch"]:
            if request.path_url == AI_EVENTS_PATH and not event["event"].startswith("$ai_"):
                results[event["uuid"]] = {"result": "drop", "details": "misrouted_event"}
            else:
                results[event["uuid"]] = {"result": "ok"}
                self._events.append((request.headers.get("Authorization"), request.path_url, event))
        return 200, {}, json.dumps({"results": results})

    def _logs(self, request):
        self._requests.append((request.path_url, request.req_kwargs.get("timeout")))
        if self.status == 200:
            self._exports.append((request.headers.get("Authorization"), ExportLogsServiceRequest.FromString(request.body)))
        return self.status, {}, ""

    def requests(self):
        """(path, the seconds the sender gives PostHog to answer) of every request so far, oldest first."""
        return list(self._requests)

    def events(self, name=None):
        """Every event received so far, oldest first (only those called `name`, when given): {"event", "uuid",
        "distinct_id", "timestamp", "options", "properties"} as the SDK sent it. AI events come through their own
        endpoint, the others through the analytics one."""
        posthog_client.flush()
        assert all(authorization == f"Bearer {TOKEN}" for authorization, _, _ in self._events)
        assert all((path == AI_EVENTS_PATH) == event["event"].startswith("$ai_") for _, path, event in self._events)
        # The two endpoints are sent to by threads of their own: the time each event was made puts them in order.
        events = sorted((event for _, _, event in self._events), key=lambda e: datetime.fromisoformat(e["timestamp"]))
        return [event for event in events if name in (None, event["event"])]

    def records(self):
        """Every log record received so far, oldest first: {"name", "level", "service", **its attributes}."""
        posthog_client.flush()
        assert all(authorization == f"Bearer {TOKEN}" for authorization, _ in self._exports)
        records = []
        for _, export in self._exports:
            for resource_logs in export.resource_logs:
                resource = {a.key: a.value.string_value for a in resource_logs.resource.attributes}
                for scope_logs in resource_logs.scope_logs:
                    for record in scope_logs.log_records:
                        attributes = {a.key: getattr(a.value, a.value.WhichOneof("value")) for a in record.attributes}
                        records.append({"name": record.body.string_value, "level": record.severity_text,
                                        "service": resource["service.name"], **attributes})
        return records
