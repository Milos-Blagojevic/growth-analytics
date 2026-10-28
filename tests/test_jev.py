import json

import pytest
import requests

import jev

QUESTIONS = {"relevant": jev.noul("Is this relevant?")}
OK = {"model": "jev-1.14.0", "answers": {"relevant": {"type": "noul", "noul": 0.8}},
      "usage": {"input_tokens": 1_000_000, "output_tokens": 5}}


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-jev-key")


def test_retries_an_overloaded_api_and_tallies_only_billed_input(http):
    api = http.post(jev.API_URL, status=529, json={"detail": "overloaded"})
    http.post(jev.API_URL, json=OK)

    assert jev.ask("some text", QUESTIONS) == OK["answers"]

    assert len(http.calls) == 2
    sent = json.loads(api.calls[0].request.body)
    assert sent == {"model": jev.DEFAULT_MODEL, "state": "some text", "questions": QUESTIONS}
    assert api.calls[0].request.headers["Authorization"] == "Bearer test-jev-key"
    assert jev.usage_summary() == {"model": jev.DEFAULT_MODEL, "requests": 1, "input_tokens": 1_000_000,
                                   "dollars": 0.042}


def test_a_rejected_request_fails_at_once(http):
    http.post(jev.API_URL, status=401, json={"detail": "invalid key"})

    with pytest.raises(jev.JevError, match="HTTP 401"):
        jev.ask("some text", QUESTIONS)
    assert len(http.calls) == 1
    assert jev.usage_summary()["requests"] == 0


@pytest.mark.parametrize("failure", [{"status": 503}, {"body": requests.ConnectionError("refused")},
                                     {"body": requests.ReadTimeout("slow")}], ids=["503", "connection", "timeout"])
def test_after_an_outage_later_requests_skip_typesafe(http, failure):
    http.post(jev.API_URL, **failure)

    with pytest.raises(jev.JevError, match="request failed"):
        jev.ask("some text", QUESTIONS)
    assert len(http.calls) == jev.MAX_ATTEMPTS

    with pytest.raises(jev.JevError, match="unavailable earlier"):
        jev.ask("other text", QUESTIONS)
    assert len(http.calls) == jev.MAX_ATTEMPTS


@pytest.mark.parametrize(("asked", "waited"), [("7", 7.0), ("3600", jev.BACKOFF_MAX_SECONDS)],
                         ids=["as-asked", "capped"])
def test_a_rate_limit_waits_as_asked_and_does_not_switch_jev_off(http, monkeypatch, asked, waited):
    waits = []
    monkeypatch.setattr(jev.time, "sleep", waits.append)
    http.post(jev.API_URL, status=429, headers={"Retry-After": asked})

    with pytest.raises(jev.JevError, match="HTTP 429"):
        jev.ask("some text", QUESTIONS)
    assert waits == [waited] * (jev.MAX_ATTEMPTS - 1)
    http.replace("POST", jev.API_URL, json=OK)

    assert jev.ask("other text", QUESTIONS) == OK["answers"]


def test_a_connection_dropped_mid_reply_is_retried(http):
    http.post(jev.API_URL, body=requests.exceptions.ChunkedEncodingError("connection broken"))
    http.post(jev.API_URL, json=OK)

    assert jev.ask("some text", QUESTIONS) == OK["answers"]
    assert len(http.calls) == 2


@pytest.mark.parametrize(("body", "message"), [("<html>maintenance</html>", "no JSON"), ("[]", "unexpected response")],
                         ids=["html", "json-array"])
def test_a_reply_that_is_not_a_json_object_is_an_error(http, body, message):
    http.post(jev.API_URL, body=body, status=200)

    with pytest.raises(jev.JevError, match=message):
        jev.ask("some text", QUESTIONS)


@pytest.mark.parametrize(
    ("questions", "answers"),
    [
        (QUESTIONS, {}),
        (QUESTIONS, {"relevant": {"type": "noul", "noul": None}}),
        (QUESTIONS, {"relevant": {"type": "noul", "noul": -3.0}}),
        (QUESTIONS, ["not", "an", "object"]),
        ({"pick": jev.choice("Which?", {"a": "A", "b": "B"})}, {"pick": {"type": "choice", "choice": "a"}}),
        ({"pick": jev.choice("Which?", {"a": "A", "b": "B"})}, {"pick": {"type": "choice", "choice": "c",
                                                                         "confidence": 0.9}}),
        ({"pick": jev.choice("Which?", {"a": "A", "b": "B"})}, {"pick": {"type": "choice", "choice": "a",
                                                                         "confidence": 1.5}}),
        ({"level": jev.score("How?", ["low", "high"])}, {"level": {"type": "score", "score": 0.4,
                                                                   "probabilities": {"0": 0.6}}}),
        ({"level": jev.score("How?", ["low", "high"])}, {"level": {"type": "score", "score": True,
                                                                   "probabilities": {"0": 0.6, "1": 0.4}}}),
    ],
    ids=["missing", "null-noul", "noul-out-of-range", "answers-not-an-object", "choice-without-confidence",
         "unknown-choice", "confidence-out-of-range", "score-missing-level", "score-not-a-number"],
)
def test_an_answer_the_screens_cannot_read_is_an_error(http, questions, answers):
    http.post(jev.API_URL, json=OK | {"answers": answers})

    with pytest.raises(jev.JevError, match="no usable answer"):
        jev.ask("some text", questions)
    assert jev.usage_summary()["requests"] == 1  # answered, so billed


@pytest.mark.parametrize(("setting", "sent"), [("jev-1.13.0", "jev-1.13.0"), ("", jev.DEFAULT_MODEL)],
                         ids=["override", "empty-means-pinned"])
def test_jev_model_overrides_the_pinned_version(http, monkeypatch, setting, sent):
    monkeypatch.setenv("JEV_MODEL", setting)
    api = http.post(jev.API_URL, json=OK)

    jev.ask("some text", QUESTIONS)

    assert json.loads(api.calls[0].request.body)["model"] == sent
