import json
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
import requests
from urllib3.exceptions import ReadTimeoutError

from clients import exa_client
from exa_mock import (
    AGENT_URL,
    CONTENTS_URL,
    NEXT_RUN,
    agent_run,
    contents_response,
    error,
    grounded,
    monitor_run,
    result,
    search_response,
    structured_response,
)
from pipeline import research


def test_search_requests_highlights_by_default_and_parses_results(exa, http):
    exa.on_search(search_response([result("https://a.com/x", title="A", highlights=["growth"])]))

    response = exa_client.search("plg pricing")

    assert exa.requests() == [
        {"query": "plg pricing", "type": "auto", "numResults": 10, "contents": {"highlights": True}}
    ]
    assert http.calls[0].request.headers["x-api-key"] == "test-exa-key"
    assert [(r.url, r.title, r.highlights) for r in response.results] == [("https://a.com/x", "A", ["growth"])]


@pytest.mark.parametrize("max_age_hours", [0, -1, 24])
def test_max_age_hours_overrides_value_inside_contents(exa, max_age_hours):
    exa.on_search(search_response())

    exa_client.search("q", contents={"text": {"max_characters": 500}, "max_age_hours": 99}, max_age_hours=max_age_hours)

    assert exa.requests()[0]["contents"] == {"text": {"maxCharacters": 500}, "maxAgeHours": max_age_hours}


def test_search_structured_sends_schema_keys_verbatim(exa):
    # Snake-case property names must reach Exa unchanged: research reads content["business_summary"].
    exa.on_search(structured_response({"summary": "x"}))

    exa_client.search_structured("q", research.RESEARCH_SCHEMA, system_prompt="be strict", include_domains=["a.com"])

    sent = exa.requests()[0]
    assert sent["outputSchema"] == research.RESEARCH_SCHEMA
    assert sent["systemPrompt"] == "be strict"
    assert sent["includeDomains"] == ["a.com"]


def test_search_structured_parses_output_and_grounding(exa):
    exa.on_search(structured_response({"summary": "PLG"}, [grounded("summary", "https://s.com", confidence="low")]))

    output = exa_client.search_structured("q", {"type": "object", "properties": {"summary": {"type": "string"}}}).output

    assert output.content == {"summary": "PLG"}
    assert [(g.field, g.confidence, [c.url for c in g.citations]) for g in output.grounding] == [
        ("summary", "low", ["https://s.com"])
    ]


def test_grounded_answer_merges_grounding_into_one_entry_per_top_level_field(exa):
    exa.on_search(structured_response(
        {"summary": "PLG", "roles": [{"url": "https://a.com/1"}, {"url": "https://a.com/2"}]},
        [grounded("summary", "https://s.com"),
         grounded("roles[0].url", "https://a.com/1", "https://a.com"),
         grounded("roles[1].url", "https://a.com/2", "https://a.com", confidence="low")],
    ))  # fmt: skip

    content, grounding = exa_client.grounded_answer("q", {"type": "object", "properties": {}}, system_prompt="be strict")

    assert content == {"summary": "PLG", "roles": [{"url": "https://a.com/1"}, {"url": "https://a.com/2"}]}
    assert grounding == {
        "summary": {"confidence": "high", "urls": ["https://s.com"]},
        "roles": {"confidence": "low", "urls": ["https://a.com/1", "https://a.com", "https://a.com/2"]},  # lowest, union
    }
    assert exa.requests()[0]["systemPrompt"] == "be strict"


@pytest.mark.parametrize(
    "payload",
    [search_response(), structured_response("plain text instead of JSON")],
    ids=["no-output", "text-output"],
)
def test_grounded_answer_raises_without_structured_output(exa, payload):
    exa.on_search(payload)

    with pytest.raises(RuntimeError, match="no structured output"):
        exa_client.grounded_answer("q", {"type": "object", "properties": {}})


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        pytest.param({}, {"urls": ["https://a.com"], "highlights": True, "text": False}, id="highlights-only"),
        pytest.param(
            {"text": {"max_characters": 4000}},
            {"urls": ["https://a.com"], "highlights": True, "text": {"maxCharacters": 4000}},
            id="highlights-and-text",
        ),
        pytest.param(
            {"highlights": False, "text": {"max_characters": 4000}, "max_age_hours": 0},
            {"urls": ["https://a.com"], "text": {"maxCharacters": 4000}, "maxAgeHours": 0},
            id="text-only",
        ),
    ],
)
def test_contents_request_body(exa, kwargs, expected):
    exa.on_contents(contents_response())

    exa_client.contents(["https://a.com"], **kwargs)

    assert exa.requests(CONTENTS_URL) == [expected]


@pytest.mark.parametrize("status", sorted(exa_client.RETRYABLE_STATUS))
def test_retries_transient_http_errors(exa, status):
    exa.on_search([error(status), search_response([result("https://a.com")])])

    response = exa_client.search("q")

    assert [r.url for r in response.results] == ["https://a.com"]
    assert len(exa.requests()) == 2
    assert exa_client.cost_summary()["requests"] == 1


def test_retries_connection_errors(exa):
    exa.on_search([requests.ConnectionError("reset"), requests.Timeout("slow"), search_response()])

    exa_client.search("q")

    assert len(exa.requests()) == 3


def test_gives_up_after_max_attempts(exa):
    exa.on_search([error(503)] * exa_client.MAX_ATTEMPTS)

    with pytest.raises(ValueError, match="status code 503"):
        exa_client.search("q")
    assert len(exa.requests()) == exa_client.MAX_ATTEMPTS
    assert exa_client.cost_summary()["requests"] == 0


@pytest.mark.parametrize("status", [400, 401, 402, 404])
def test_does_not_retry_client_errors(exa, status):
    exa.on_search([error(status, "bad request")])

    with pytest.raises(ValueError, match=f"status code {status}"):
        exa_client.search("q")
    assert len(exa.requests()) == 1


def test_cost_summary_tallies_per_label(exa):
    exa.on_search(
        [
            search_response(cost=0.005),
            search_response(cost=0.007),
            search_response(cost=None),  # no costDollars: counted as a request, adds $0
            structured_response({}, cost=0.012),
        ]
    )
    exa.on_contents(contents_response(cost=0.001))

    exa_client.fast_search("q")
    exa_client.fast_search("q")
    exa_client.search("q")
    exa_client.search_structured("q", {"type": "object", "properties": {}})
    exa_client.contents(["https://a.com"])

    summary = exa_client.cost_summary()
    assert summary["requests"] == 5
    assert summary["dollars"] == pytest.approx(0.025)
    assert summary["by_label"] == {
        "search:fast": {"requests": 2, "dollars": pytest.approx(0.012)},
        "search:auto": {"requests": 1, "dollars": 0.0},
        "search_structured:auto": {"requests": 1, "dollars": pytest.approx(0.012)},
        "contents": {"requests": 1, "dollars": pytest.approx(0.001)},
    }

    exa_client.reset_costs()
    assert exa_client.cost_summary() == {"requests": 0, "dollars": 0.0, "by_label": {}}


def test_missing_api_key_fails_before_any_request(exa, monkeypatch):
    monkeypatch.delenv("EXA_API_KEY")

    with pytest.raises(RuntimeError, match="EXA_API_KEY is not set"):
        exa_client.search("q")
    assert exa.requests() == []


def test_every_exa_request_has_a_timeout(exa, monkeypatch):
    exa.on_search(search_response())
    exa.on_agent([agent_run({"companies": []}), agent_run(status="running")])
    seen = []
    original = requests.adapters.HTTPAdapter.send

    def send(self, request, **kwargs):
        seen.append((request.method, request.url.removeprefix("https://api.exa.ai"), kwargs.get("timeout")))
        return original(self, request, **kwargs)

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)

    exa_client.search("q")
    exa_client.agent_answer("find companies", {"type": "object"})
    monkeypatch.setattr(exa_client, "AGENT_WAIT_SECONDS", -1)
    with pytest.raises(RuntimeError, match="not finished"):
        exa_client.agent_answer("find companies", {"type": "object"})

    # the SDK passes 600 s of its own to every one of these
    assert seen == [(method, path, exa_client.TIMEOUT_SECONDS) for method, path in [
        ("POST", "/search"),
        ("POST", "/agent/runs"), ("GET", "/agent/runs/agent_run_1"),
        ("POST", "/agent/runs"), ("POST", "/agent/runs/agent_run_2/cancel"),
    ]]  # fmt: skip
    assert exa_client.TIMEOUT_SECONDS[0] > 0 and exa_client.TIMEOUT_SECONDS[1] > 0


# agent runs -------------------------------------------------------------------------------------

AGENT_SCHEMA = {"type": "object", "properties": {"companies": {"type": "array"}}}


def test_agent_answer_runs_an_agent_with_data_sources_and_returns_what_it_found(exa):
    exa.on_agent(agent_run(
        {"companies": [{"name": "Acme", "employees": 40}]},
        [grounded("companies[0].name", "https://linkedin.com/company/acme", confidence=None),
         grounded("companies[0].employees", "https://linkedin.com/company/acme", "https://acme.com", confidence="medium"),
         grounded("companies[1].name", "https://beta.io", confidence="high")],
        cost=0.205, partners={"fiber": 0.18},
    ))  # fmt: skip

    content, grounding = exa_client.agent_answer(
        "find companies", AGENT_SCHEMA, system_prompt="be strict", data_sources=["fiber"], exclude=[{"domain": "old.com"}]
    )

    assert content == {"companies": [{"name": "Acme", "employees": 40}]}
    # "structured." is dropped from the field paths; the lowest confidence Exa gave; every cited URL once
    assert grounding == {
        "companies": {"confidence": "medium", "urls": ["https://linkedin.com/company/acme", "https://acme.com", "https://beta.io"]}
    }
    assert exa.requests(AGENT_URL) == [{
        "query": "find companies",
        "systemPrompt": "be strict",
        "outputSchema": AGENT_SCHEMA,
        "effort": "low",
        "dataSources": [{"provider": "fiber"}],
        "input": {"exclusion": [{"domain": "old.com"}]},
    }]  # fmt: skip
    # one run, with the partner's charge inside its cost
    assert exa_client.cost_summary()["by_label"] == {"agent:low": {"requests": 1, "dollars": pytest.approx(0.205)}}


def test_agent_answer_sends_no_data_sources_or_exclusions_unless_given(exa):
    exa.on_agent(agent_run({"companies": []}))

    exa_client.agent_answer("find companies", AGENT_SCHEMA, effort="minimal")

    assert exa.requests(AGENT_URL) == [{"query": "find companies", "outputSchema": AGENT_SCHEMA, "effort": "minimal"}]


@pytest.mark.parametrize(
    ("run", "message"),
    [
        (agent_run(status="failed", error="Run timed out", cost=0.09), "failed: Run timed out"),
        (agent_run(status="failed", cost=0.09), "failed: no reason given"),
        (agent_run(None, cost=0.09), "no structured output"),
        (agent_run("free text instead of JSON", cost=0.09), "no structured output"),
    ],
    ids=["failed", "failed-without-a-reason", "nothing-structured", "text-output"],
)
def test_agent_answer_raises_for_a_run_without_a_result_and_still_counts_its_cost(exa, run, message):
    exa.on_agent(run)

    with pytest.raises(RuntimeError, match=message):
        exa_client.agent_answer("find companies", AGENT_SCHEMA)
    assert exa_client.cost_summary()["dollars"] == pytest.approx(0.09)


def test_posthog_gets_a_generation_for_each_answer_exas_model_wrote_and_none_of_what_was_asked(exa, posthog_api):
    exa.on_search(structured_response({"summary": "PRIVATE-SUMMARY"}))
    exa.on_agent(agent_run({"companies": [{"name": "PRIVATE-COMPANY"}]}))
    schema = {"type": "object", "properties": {"summary": {"type": "string"}}}

    exa_client.search_structured("PRIVATE-QUERY", schema, type="deep", system_prompt="PRIVATE-PROMPT")
    exa_client.agent_answer("PRIVATE-QUERY", AGENT_SCHEMA, data_sources=["fiber"], effort="minimal")
    exa_client.search("PRIVATE-QUERY")  # results to read, not an answer a model wrote
    exa.on_agent(agent_run(status="failed", cost=0.09))
    with pytest.raises(RuntimeError, match="failed"):  # no answer: nothing to report
        exa_client.agent_answer("PRIVATE-QUERY", AGENT_SCHEMA)

    generations = [event["properties"] for event in posthog_api.events()]
    assert [(g["$ai_provider"], g["$ai_model"]) for g in generations] == [("exa", "search-deep"), ("exa", "agent-minimal")]
    assert all(g["$ai_latency"] >= 0 and "$ai_input_tokens" not in g for g in generations)  # Exa counts no tokens
    assert "PRIVATE" not in json.dumps(posthog_api.events())


def test_agent_answer_keeps_a_paid_result_whose_grounding_is_missing_or_unusual(exa):
    # grounding and its confidence are optional in Exa's replies: neither may cost the result
    without = agent_run({"companies": [{"name": "Acme"}]})
    without["output"]["grounding"] = None
    odd = agent_run(
        {"companies": [{"name": "Acme"}], "summary": "one"},
        [grounded("companies[0].name", "https://acme.com", confidence="very_high"),
         grounded("summary", "https://acme.com/about", confidence="HIGH")],
    )  # fmt: skip
    exa.on_agent([without, odd])

    assert exa_client.agent_answer("find companies", AGENT_SCHEMA) == ({"companies": [{"name": "Acme"}]}, {})
    assert exa_client.agent_answer("find companies", AGENT_SCHEMA)[1] == {
        "companies": {"confidence": None, "urls": ["https://acme.com"]},
        "summary": {"confidence": "high", "urls": ["https://acme.com/about"]},
    }


def test_agent_answer_cancels_a_run_that_does_not_finish_in_time(exa, http, monkeypatch):
    monkeypatch.setattr(exa_client, "AGENT_WAIT_SECONDS", -1)
    exa.on_agent(agent_run(status="running", cost=0.03))

    with pytest.raises(RuntimeError, match="agent_run_1 cancelled: not finished"):
        exa_client.agent_answer("find companies", AGENT_SCHEMA)
    assert [call.request.url for call in http.calls][-1] == f"{AGENT_URL}/agent_run_1/cancel"
    assert exa_client.cost_summary()["dollars"] == pytest.approx(0.03)  # what the run cost until then


def test_agent_answer_returns_a_run_that_ended_just_as_it_was_being_cancelled(exa, monkeypatch):
    monkeypatch.setattr(exa_client, "AGENT_WAIT_SECONDS", -1)
    # Exa refuses to cancel a run that has ended; the run is read once more instead of being given up
    exa.on_agent(agent_run({"companies": []}, cost=0.2), cancels=[error(409), error(409)])

    assert exa_client.agent_answer("find companies", AGENT_SCHEMA) == ({"companies": []}, {})
    assert exa_client.cost_summary()["by_label"] == {"agent:low": {"requests": 1, "dollars": pytest.approx(0.2)}}


def test_agent_answer_retries_a_status_check_that_fails_in_passing(exa, http):
    # as many transient failures as failed checks would end the run: retried, they are two checks at most
    in_passing = [error(503), requests.ConnectionError("reset"), *[error(503)] * (exa_client.AGENT_FAILED_CHECKS - 2)]
    exa.on_agent(agent_run({"companies": []}), checks=in_passing)

    assert exa_client.agent_answer("find companies", AGENT_SCHEMA) == ({"companies": []}, {})
    assert len(exa.requests(AGENT_URL)) == 1  # the same run all along
    assert not any(call.request.url.endswith("/cancel") for call in http.calls)


def test_agent_answer_does_not_give_up_a_paid_run_for_a_few_odd_status_answers(exa, http):
    # a 404 just after the create, say: not retried as transient, and not a reason to cancel. Only failed checks
    # in a row count: a good answer in between starts the count again.
    odd = [error(404)] * (exa_client.AGENT_FAILED_CHECKS - 1)
    still_running = {"id": "agent_run_1", "object": "agent_run", "status": "running"}
    exa.on_agent(agent_run({"companies": []}), checks=[*odd, still_running, *odd])

    assert exa_client.agent_answer("find companies", AGENT_SCHEMA) == ({"companies": []}, {})
    assert not any(call.request.url.endswith("/cancel") for call in http.calls)


def test_agent_answer_pauses_between_status_checks(exa, monkeypatch):
    class Pauses:
        waits = []

        def is_set(self):
            return False

        def wait(self, seconds):
            self.waits.append(seconds)
            return False

    still_running = {"id": "agent_run_1", "object": "agent_run", "status": "running"}
    exa.on_agent(agent_run({"companies": []}), checks=[still_running, still_running])
    monkeypatch.setattr(exa_client, "AGENT_POLL_SECONDS", 2.0)
    monkeypatch.setattr(exa_client, "_stop", Pauses())

    exa_client.agent_answer("find companies", AGENT_SCHEMA)

    assert Pauses.waits == [2.0, 2.0, 2.0]  # one before each check: a run takes a minute, not a thousand requests


def test_agent_answer_stops_a_run_it_can_no_longer_follow_and_counts_its_cost(exa, http):
    for_good = [error(500)] * (exa_client.MAX_ATTEMPTS * exa_client.AGENT_FAILED_CHECKS)
    exa.on_agent(agent_run(status="running", cost=0.03), checks=for_good)

    with pytest.raises(RuntimeError, match="agent_run_1 cancelled: could not be followed .*500.*; stopped"):
        exa_client.agent_answer("find companies", AGENT_SCHEMA)
    assert [call.request.url for call in http.calls][-1] == f"{AGENT_URL}/agent_run_1/cancel"
    assert exa_client.cost_summary()["by_label"] == {"agent:low": {"requests": 1, "dollars": pytest.approx(0.03)}}


def test_agent_answer_says_so_when_a_lost_run_may_still_be_billed(exa):
    # neither the status checks, nor the cancel, nor a last read get through
    exa.on_agent(
        agent_run(status="running", cost=0.03),
        checks=[error(500)] * (exa_client.MAX_ATTEMPTS * exa_client.AGENT_FAILED_CHECKS + 1),
        cancels=[requests.ConnectionError("down")],
    )

    with pytest.raises(RuntimeError, match="agent_run_1 queued: could not be followed .* may still be running and billed"):
        exa_client.agent_answer("find companies", AGENT_SCHEMA)
    assert exa_client.cost_summary()["by_label"] == {"agent:low": {"requests": 1, "dollars": 0.0}}


def test_agent_answer_cancels_the_run_when_interrupted(exa, http):
    # Ctrl-C while waiting: left going, the run would be billed to its end
    exa.on_agent(agent_run(status="running", cost=0.03), checks=[KeyboardInterrupt()])

    with pytest.raises(KeyboardInterrupt):
        exa_client.agent_answer("find companies", AGENT_SCHEMA)
    assert [call.request.url for call in http.calls][-1] == f"{AGENT_URL}/agent_run_1/cancel"
    assert exa_client.cost_summary()["dollars"] == pytest.approx(0.03)


def test_stop_agent_runs_cancels_the_runs_other_threads_wait_for_and_lets_no_new_one_start(exa, http):
    # Ctrl-C reaches the main thread only: hunt.py run waits for its agent runs in worker threads
    exa.on_agent(agent_run(status="running", cost=0.03))
    with ThreadPoolExecutor(max_workers=2) as pool:
        waiting = [pool.submit(exa_client.agent_answer, "find companies", AGENT_SCHEMA) for _ in range(2)]
        while len(exa.requests(AGENT_URL)) < 2:  # both runs created
            time.sleep(0.001)

        exa_client.stop_agent_runs()

        for future in waiting:
            with pytest.raises(KeyboardInterrupt):
                future.result(timeout=10)
    cancelled = {call.request.url for call in http.calls if call.request.url.endswith("/cancel")}
    assert cancelled == {f"{AGENT_URL}/agent_run_1/cancel", f"{AGENT_URL}/agent_run_2/cancel"}
    with pytest.raises(RuntimeError, match="not started: the program is stopping"):
        exa_client.agent_answer("find companies", AGENT_SCHEMA)
    assert len(exa.requests(AGENT_URL)) == 2


def test_agent_answer_takes_only_the_efforts_with_a_fixed_price(http):
    with pytest.raises(ValueError, match="effort must be one of minimal, low, medium, not 'auto'"):
        exa_client.agent_answer("find companies", AGENT_SCHEMA, effort="auto")  # priced by the run, no cap sent
    assert len(http.calls) == 0


def test_agent_answer_retries_a_create_that_exa_turned_down_for_rate(exa):
    exa.on_agent([error(429), agent_run({"companies": []})])
    exa_client.agent_answer("find companies", AGENT_SCHEMA)
    assert len(exa.requests(AGENT_URL)) == 2


@pytest.mark.parametrize(
    "failure",
    [
        requests.Timeout("slow"),
        # how requests reports a reply whose body stalls: the request itself got through
        requests.ConnectionError(ReadTimeoutError(None, None, "Read timed out.")),
        requests.ConnectionError("connection dropped"),
        error(503),
        error(504),
        error(408),
    ],
    ids=["timeout", "body-read-timeout", "dropped-connection", "503", "504", "408"],
)
def test_agent_answer_never_starts_a_second_run_when_the_first_may_have_started(exa, failure):
    # a retry could start, and bill, a second run while the first is never followed or cancelled
    exa.on_agent([failure, agent_run({"companies": []})])

    with pytest.raises(RuntimeError, match="may have started and be billed"):
        exa_client.agent_answer("find companies", AGENT_SCHEMA)
    assert len(exa.requests(AGENT_URL)) == 1


def test_agent_answer_passes_on_a_request_exa_rejected(exa):
    exa.on_agent(error(400, "outputSchema is invalid"))

    with pytest.raises(ValueError, match="status code 400"):  # nothing started: not a "may have started"
        exa_client.agent_answer("find companies", AGENT_SCHEMA)
    assert len(exa.requests(AGENT_URL)) == 1


MONITOR = {
    "search": {"query": "New job openings at Acme (acme.com)", "numResults": 10},
    "trigger": {"type": "interval", "period": "4h"},
    "webhook": {"url": "https://hunt.example.org", "events": ["monitor.deleted"]},
}


def test_a_monitor_is_created_as_asked_and_updated_field_by_field(exa):
    account = exa.monitors()

    created = exa_client.create_monitor(MONITOR)
    assert account.calls("POST") == [MONITOR]
    assert created.next_run_at is None  # Exa sets it a moment later
    assert exa_client.get_monitor(created.id).next_run_at == NEXT_RUN

    changed = exa_client.update_monitor(created.id, {"search": {"query": "New job openings at Acme"}})
    assert (changed.search.query, changed.search.num_results) == ("New job openings at Acme", 10)
    assert exa_client.cost_summary()["by_label"] == {"monitors": {"requests": 3, "dollars": 0.0}}  # Exa bills the runs


def test_create_monitor_retries_a_create_that_exa_turned_down_for_rate(exa):
    account = exa.monitors()
    account.failures["create"] = [error(429)]

    exa_client.create_monitor(MONITOR)

    assert len(account.calls("POST")) == 2 and len(account.monitors) == 1


@pytest.mark.parametrize(
    "failure",
    [requests.Timeout("slow"), requests.ConnectionError("connection dropped"), error(503), error(408)],
    ids=["timeout", "dropped-connection", "503", "408"],
)
def test_create_monitor_never_creates_a_second_monitor_when_the_first_may_exist(exa, failure):
    # a second monitor would run, billed, next to the first, which nothing would know about
    account = exa.monitors()
    account.failures["create"] = [failure]

    with pytest.raises(RuntimeError, match="may have been created"):
        exa_client.create_monitor(MONITOR)
    assert len(account.calls("POST")) == 1


def test_create_monitor_passes_on_a_request_exa_rejected(exa):
    account = exa.monitors()
    account.failures["create"] = [error(400, "[webhook.url]: Webhook URL cannot point to localhost")]

    with pytest.raises(ValueError, match="cannot point to localhost"):  # nothing created: not a "may have been"
        exa_client.create_monitor(MONITOR)
    assert len(account.calls("POST")) == 1


def test_a_monitor_exa_no_longer_has_is_none_to_read_and_nothing_to_delete(exa):
    account = exa.monitors()
    created = exa_client.create_monitor(MONITOR)

    assert exa_client.delete_monitor(created.id) is True and account.monitors == {}
    assert exa_client.get_monitor(created.id) is None
    assert exa_client.delete_monitor(created.id) is False


def test_monitors_and_their_runs_are_read_page_by_page_and_only_as_far_as_asked(exa, http):
    account = exa.monitors()
    ids = [exa_client.create_monitor(MONITOR).id for _ in range(exa_client.MONITORS_PAGE + 1)]
    for n in range(2 * exa_client.MONITORS_PAGE + 5):
        account.add_run(ids[0], monitor_run(f"run_{n}"))

    assert [m.id for m in exa_client.monitors()] == ids

    before = len(http.calls)
    runs = exa_client.monitor_runs(ids[0])
    assert next(runs).id == "run_44"  # newest first
    assert len(http.calls) == before + 1  # the caller stopped: the other pages are never asked for
    assert [run.id for run in runs][-1] == "run_0"
    assert len(http.calls) == before + 3


def test_monitor_run_output_is_what_a_run_found_and_nothing_for_a_run_that_found_nothing(exa):
    account = exa.monitors()
    monitor_id = exa_client.create_monitor(MONITOR).id
    role = {"title": "Analytics Engineer", "url": "https://jobs.ashbyhq.com/acme/1"}
    account.add_run(monitor_id, monitor_run("run_1"))
    account.add_run(monitor_id, monitor_run(
        "run_2",
        results=[result(role["url"], title="Analytics Engineer at Acme", text="dbt, Snowflake.")],
        content={"open_roles": [role]},
        grounding=[grounded("open_roles[0].title", role["url"]), grounded("open_roles[0].url", role["url"], confidence="low")],
    ))
    account.add_run(monitor_id, monitor_run("run_3", status="failed", fail_reason="insufficient_credits"))

    listed = list(exa_client.monitor_runs(monitor_id))
    assert [(run.id, run.status, run.fail_reason, run.empty) for run in listed] == [
        ("run_3", "failed", "insufficient_credits", False), ("run_2", "completed", None, False),
        ("run_1", "completed", None, True)]  # only run_2 is worth reading

    content, grounding, pages = exa_client.monitor_run_output(monitor_id, "run_2")
    assert content == {"open_roles": [role]}
    assert grounding == {"open_roles": {"confidence": "low", "urls": [role["url"]]}}
    assert [(page["url"], page["text"]) for page in pages] == [(role["url"], "dbt, Snowflake.")]
    assert exa_client.monitor_run_output(monitor_id, "run_1") == (None, {}, [])
    assert exa_client.monitor_run_output(monitor_id, "run_3") == (None, {}, [])


def test_a_run_is_not_lost_for_grounding_that_is_missing_or_unusual(exa):
    # grounding is optional in Exa's replies, as with a search's: the SDK's own run model refuses all of these
    account = exa.monitors()
    monitor_id = exa_client.create_monitor(MONITOR).id
    role = {"title": "Analytics Engineer", "url": "https://jobs.ashbyhq.com/acme/1"}
    odd = [
        {"field": "open_roles[0].url", "citations": [{"url": role["url"]}, {"title": "no link"}, "junk"]},  # no confidence
        {"field": "open_roles[0].title", "confidence": "high"},  # no citations
        {"field": "open_roles[0].location", "citations": 5},  # citations that are no list
        {"citations": []},  # no field
        "junk",
    ]
    page = result(role["url"], text="dbt, Snowflake.")
    account.add_run(monitor_id, monitor_run("run_1", results=[page], content={"open_roles": [role]}, grounding=odd))
    without = monitor_run("run_2", results=[page, "junk"], content={"open_roles": [role]})
    without["output"]["grounding"] = None
    account.add_run(monitor_id, without)
    no_lists = monitor_run("run_3", results=[page], content={"open_roles": [role]})
    no_lists["output"] |= {"grounding": 5, "results": 5}
    account.add_run(monitor_id, no_lists)

    assert [run.id for run in exa_client.monitor_runs(monitor_id)] == ["run_3", "run_2", "run_1"]
    content, grounding, pages = exa_client.monitor_run_output(monitor_id, "run_1")
    assert content == {"open_roles": [role]} and pages == [page]
    assert grounding == {"open_roles": {"confidence": "high", "urls": [role["url"]]}}
    assert exa_client.monitor_run_output(monitor_id, "run_2") == ({"open_roles": [role]}, {}, [page])
    assert exa_client.monitor_run_output(monitor_id, "run_3") == ({"open_roles": [role]}, {}, [])


def test_a_list_of_runs_that_never_ends_is_not_asked_for_again_and_again(exa, http):
    account = exa.monitors()
    monitor_id = exa_client.create_monitor(MONITOR).id
    before = len(http.calls)

    account.failures["runs"] = [(200, {"data": [], "hasMore": True, "nextCursor": "0"})] * 3  # more, it says, and gives none
    assert list(exa_client.monitor_runs(monitor_id)) == [] and len(http.calls) == before + 1

    run = {**monitor_run("run_1"), "monitorId": monitor_id}
    account.failures["runs"] = [(200, {"data": [run], "hasMore": True, "nextCursor": "7"})] * 3  # the same page again
    assert [r.id for r in exa_client.monitor_runs(monitor_id)] == ["run_1", "run_1"] and len(http.calls) == before + 3


def test_a_list_of_monitors_that_never_ends_is_not_asked_for_again_and_again(exa, http):
    account = exa.monitors()
    one = account.monitors[exa_client.create_monitor(MONITOR).id]
    before = len(http.calls)

    account.failures["list"] = [(200, {"data": [], "hasMore": True, "nextCursor": "0"})] * 3  # more, it says, and gives none
    assert exa_client.monitors() == [] and len(http.calls) == before + 1

    account.failures["list"] = [(200, {"data": [one], "hasMore": True, "nextCursor": cursor}) for cursor in "abab"]
    assert len(exa_client.monitors()) == 3 and len(http.calls) == before + 4  # stopped where the cursors come round
