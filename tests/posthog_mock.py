"""PostHog's ingestion endpoints in their wire format, for tests (registered on the `responses` mock): the SDK's
POST /batch/ for events and OTLP's POST /i/v1/logs for log records."""

import json

import responses
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest

from clients import posthog_client

HOST = "https://eu.i.posthog.com"
TOKEN = "phc_test"


class PostHogMock:
    """Turns PostHog on for the test (service "tests"; POSTHOG_PROJECT_TOKEN and POSTHOG_HOST are set too, for code
    that configures it from the environment) and collects what reaches it. `events()` and `records()` first send
    what is queued. `status` is what both endpoints answer with, and only what got a 200 counts as received: 400 is
    a refusal the senders do not retry, 503 an outage. `requests()` is every request made, answered or not."""

    def __init__(self, http, monkeypatch):
        monkeypatch.setenv("POSTHOG_PROJECT_TOKEN", TOKEN)
        monkeypatch.setenv("POSTHOG_HOST", HOST)
        self.status = 200
        self._batches, self._exports, self._requests = [], [], []
        http.add_callback(responses.POST, f"{HOST}/batch/", callback=self._batch, content_type="application/json")
        http.add_callback(responses.POST, f"{HOST}{posthog_client.LOGS_PATH}", callback=self._logs)
        posthog_client.configure(TOKEN, HOST, service="tests")

    def _batch(self, request):
        self._requests.append((request.path_url, request.req_kwargs.get("timeout")))
        if self.status == 200:
            self._batches.append(json.loads(request.body))
        return self.status, {}, json.dumps({"status": 1})

    def _logs(self, request):
        self._requests.append((request.path_url, request.req_kwargs.get("timeout")))
        if self.status == 200:
            self._exports.append((request.headers.get("Authorization"), ExportLogsServiceRequest.FromString(request.body)))
        return self.status, {}, ""

    def requests(self):
        """(path, the seconds the sender gives PostHog to answer) of every request so far, oldest first."""
        return list(self._requests)

    def events(self, name=None):
        """Every event received so far, oldest first (only those called `name`, when given): {"event",
        "distinct_id", "properties", ...} as the SDK sent it."""
        posthog_client.flush()
        assert all(batch["api_key"] == TOKEN for batch in self._batches)
        events = [event for batch in self._batches for event in batch["batch"]]
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
