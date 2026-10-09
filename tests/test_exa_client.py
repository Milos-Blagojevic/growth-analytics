import pytest
import requests

import exa_client
import research
from exa_mock import CONTENTS_URL, contents_response, error, grounded, result, search_response, structured_response


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
