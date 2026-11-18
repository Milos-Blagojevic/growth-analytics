import asyncio
import inspect
import json
import logging
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from clients import posthog_client
from posthog_mock import HOST, TOKEN


def sent(posthog_api):
    """Everything PostHog received, as one string to look for text that must not be in it."""
    return json.dumps([posthog_api.events(), posthog_api.records()])


def test_without_both_the_token_and_the_host_nothing_is_sent(caplog):
    """The autouse `http` fixture fails this test if a request leaves all the same."""
    assert not posthog_client.configure(None, None, service="hunt")
    assert caplog.records == []  # PostHog is optional: no word about it when it is simply not set up
    assert not posthog_client.configure(TOKEN, "", service="hunt")
    assert "only POSTHOG_PROJECT_TOKEN is set" in caplog.text
    assert not posthog_client.configure("", HOST, service="hunt")
    assert "only POSTHOG_HOST is set" in caplog.text

    posthog_client.capture("project_created")
    posthog_client.capture_exception(ValueError("boom"))
    posthog_client.generation("anthropic", "claude", input_tokens=1, output_tokens=1)
    posthog_client.log("dashboard_started")
    with posthog_client.agent_turn("session"), posthog_client.command("run"):
        assert posthog_client.instrument_tool(lambda: "result")() == "result"
    posthog_client.flush()
    assert not posthog_client.configured()


def test_events_carry_no_person_and_name_the_service(posthog_api):
    posthog_client.capture("project_created")
    posthog_client.capture("hunt_run_started", {"kind": "monitor", "trigger": "schedule"})

    first, second = posthog_api.events()
    assert (first["event"], second["event"]) == ("project_created", "hunt_run_started")
    assert first["distinct_id"] != second["distinct_id"]  # a new random id each time: no two events are one person's
    assert first["properties"]["$process_person_profile"] is False
    assert first["properties"]["service"] == "tests"
    assert second["properties"] | {"kind": "monitor", "trigger": "schedule"} == second["properties"]


def test_a_claude_call_is_a_generation_with_its_tokens_and_none_of_its_text(posthog_api, fake_claude):
    fake = fake_claude({"overview": "PRIVATE-ANSWER", "picks": []})
    request = {"model": "claude-test", "max_tokens": 100, "system": "PRIVATE-SYSTEM", "output_format": dict,
               "messages": [{"role": "user", "content": "PRIVATE-PROMPT"}]}

    response = posthog_client.parse_anthropic(fake, **request)

    assert response.parsed_output == {"overview": "PRIVATE-ANSWER", "picks": []}  # the caller gets Claude's answer
    assert fake.calls == [request]  # and Claude got the request as it was: no trace id in it
    (generation,) = posthog_api.events("$ai_generation")
    properties = generation["properties"]
    assert (properties["$ai_provider"], properties["$ai_model"]) == ("anthropic", "claude-test")
    assert (properties["$ai_input_tokens"], properties["$ai_output_tokens"]) == (1200, 300)
    assert properties["$ai_latency"] >= 0
    assert "$ai_input" not in properties and "$ai_output_choices" not in properties
    assert "PRIVATE" not in sent(posthog_api)


def test_a_call_that_fails_is_raised_as_it_was_and_not_reported(posthog_api):
    def refused(**request):
        raise RuntimeError("PRIVATE-ERROR")

    with pytest.raises(RuntimeError, match="PRIVATE-ERROR"):
        posthog_client.parse_anthropic(SimpleNamespace(messages=SimpleNamespace(parse=refused)), model="claude-test")
    assert posthog_api.events() == []


def test_generations_are_traces_of_their_own_in_the_process_session_unless_given_one_trace(posthog_api):
    posthog_client.generation("exa", "search-auto", latency=2.5)
    posthog_client.generation("typesafe", "jev-1", input_tokens=500)
    trace = posthog_client.new_trace_id()
    posthog_client.generation("anthropic", "claude", trace_id=trace)
    posthog_client.generation("anthropic", "claude", trace_id=trace)

    one, two, three, four = (event["properties"] for event in posthog_api.events("$ai_generation"))
    assert len({one["$ai_trace_id"], two["$ai_trace_id"], trace}) == 3
    assert three["$ai_trace_id"] == four["$ai_trace_id"] == trace
    assert len({p["$ai_session_id"] for p in (one, two, three, four)}) == 1  # the generations of one command
    assert one["$ai_latency"] == 2.5 and "$ai_input_tokens" not in one  # what a provider does not count is left out
    assert two["$ai_input_tokens"] == 500 and "$ai_output_tokens" not in two


def test_an_agent_turn_is_one_trace_of_its_generations_and_tool_spans(posthog_api):
    def read_email(handle):
        return f"PRIVATE-EMAIL {handle}"

    def draft_email(to):
        raise LookupError(f"PRIVATE-RECIPIENT {to}")

    async def news_digest(topic):
        return f"PRIVATE-NEWS {topic}"

    read, draft, digest = (posthog_client.instrument_tool(tool) for tool in (read_email, draft_email, news_digest))
    assert read.__name__ == "read_email"  # the agent names the tool after the function it was given
    assert inspect.iscoroutinefunction(digest) and not inspect.iscoroutinefunction(read)  # and awaits an async one

    with posthog_client.agent_turn("conversation-1"):
        assert read("PRIVATE-HANDLE") == "PRIVATE-EMAIL PRIVATE-HANDLE"
        with pytest.raises(LookupError):
            draft("PRIVATE-TO")
        assert asyncio.run(digest("PRIVATE-TOPIC")) == "PRIVATE-NEWS PRIVATE-TOPIC"
        posthog_client.generation("anthropic", "claude", input_tokens=10, output_tokens=5)
    assert read("o1") == "PRIVATE-EMAIL o1"  # outside an answer: the tool works, and there is no trace to join

    events = posthog_api.events()
    assert [(e["event"], e["properties"].get("$ai_span_name")) for e in events] == [
        ("$ai_span", "read_email"), ("$ai_span", "draft_email"), ("$ai_span", "news_digest"),
        ("$ai_generation", "model_request")]
    assert len({e["properties"]["$ai_trace_id"] for e in events}) == 1
    assert {e["properties"]["$ai_session_id"] for e in events} == {"conversation-1"}
    failed = events[1]["properties"]
    assert (failed["$ai_is_error"], failed["$ai_error"]) == (True, "LookupError")  # its type, not what it said
    assert "$ai_is_error" not in events[0]["properties"]
    assert "PRIVATE" not in sent(posthog_api)


def test_only_records_written_for_posthog_are_exported_never_the_programs_own_log(posthog_api):
    logging.getLogger("hunt").warning("giving up on 'PRIVATE-SUBJECT' from PRIVATE-SENDER")
    logging.getLogger().error("PRIVATE-ROOT")
    posthog_client.log("dashboard_started")

    assert posthog_api.records() == [{"name": "dashboard_started", "level": "INFO", "service": "tests"}]


@pytest.mark.parametrize(("ending", "level", "extra"), [
    (None, "INFO", {"exit_code": 0}),
    (lambda: sys.exit(0), "INFO", {"exit_code": 0}),
    (lambda: sys.exit(1), "ERROR", {"exit_code": 1}),
    (lambda: sys.exit("PRIVATE: profile.toml names no companies"), "ERROR", {"exit_code": 1}),
    (lambda: 1 / 0, "ERROR", {"exit_code": 1, "error_type": "ZeroDivisionError"}),
    (lambda: (_ for _ in ()).throw(KeyboardInterrupt()), "ERROR", {"exit_code": 1, "error_type": "KeyboardInterrupt"}),
])
def test_a_command_is_logged_when_it_starts_and_when_it_ends_with_its_exit_code(posthog_api, ending, level, extra):
    try:
        with posthog_client.command("monitor"):
            if ending:
                ending()
    except BaseException:  # the command's own exit or failure passes through unchanged
        assert ending is not None

    started, finished = posthog_api.records()
    assert started == {"name": "tests_command_started", "level": "INFO", "service": "tests", "command": "monitor"}
    assert finished.pop("seconds") >= 0
    assert finished == {"name": "tests_command_finished", "level": level, "service": "tests", "command": "monitor",
                        **extra}
    assert "PRIVATE" not in sent(posthog_api)


def test_a_posthog_failure_is_logged_and_never_reaches_the_work_it_reports_on(posthog_api, fake_claude, monkeypatch,
                                                                             caplog):
    def broken(*args, **kwargs):
        raise RuntimeError("queue is gone")

    monkeypatch.setattr(posthog_client._client, "capture", broken)
    monkeypatch.setattr(posthog_client._client, "capture_exception", broken)
    monkeypatch.setattr(posthog_client._records[1], "emit", broken)

    posthog_client.capture("project_created")
    posthog_client.capture_exception(ValueError("boom"))
    posthog_client.log("dashboard_started")
    with posthog_client.agent_turn("conversation-1"):
        assert posthog_client.instrument_tool(lambda: "result")() == "result"
    response = posthog_client.parse_anthropic(fake_claude({"ok": True}), model="claude-test", output_format=dict)

    assert response.parsed_output == {"ok": True}
    assert [record.levelname for record in caplog.records if record.name == "posthog_client"] == ["WARNING"] * 5
    assert posthog_api.events() == [] and posthog_api.records() == []


def test_posthog_refusing_what_is_sent_is_survived_and_shutdown_turns_it_off(posthog_api):
    posthog_api.status = 400
    posthog_client.capture("project_created")
    posthog_client.log("dashboard_started")

    posthog_client.flush()  # both senders log the refusal; neither raises nor tries again
    posthog_client.shutdown()

    assert sorted(path for path, _ in posthog_api.requests()) == ["/batch/", "/i/v1/logs"]
    assert not posthog_client.configured()
    posthog_client.capture("project_deleted")  # off again: nothing is queued, nothing is asked
    assert len(posthog_api.requests()) == 2


def test_a_posthog_that_is_down_gets_seconds_and_one_more_try_then_what_is_left_is_dropped(posthog_api):
    """A command sends what is queued when it exits (`shutdown`). With PostHog down that may hold the exit, and a
    dashboard job with it, only for a few seconds: every request has a short timeout and a batch one more try."""
    posthog_client.log("dashboard_started")
    posthog_client.flush()
    posthog_api.status = 503
    posthog_client.capture("project_created")

    started = time.monotonic()
    posthog_client.shutdown()

    assert time.monotonic() - started < 10
    requests_made = posthog_api.requests()
    assert [path for path, _ in requests_made] == ["/i/v1/logs"] + ["/batch/"] * (posthog_client.RETRIES + 1)
    assert all(0 < timeout <= posthog_client.TIMEOUT <= 5 for _, timeout in requests_made)


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_an_exception_is_reported_by_its_type_and_stack_never_by_what_it_says(posthog_api):
    """A message quotes what could not be read or written. The dashboard asks for the exceptions no code caught too
    (`exceptions=True`), its threads' included; hunt.py does not."""
    def crash():
        raise KeyError("PRIVATE-COMPANY")

    assert sys.excepthook is sys.__excepthook__
    posthog_client.configure(TOKEN, HOST, service="dashboard", exceptions=True)
    assert sys.excepthook is not sys.__excepthook__
    try:
        raise ValueError("PRIVATE-VALUE in /Users/PRIVATE-NAME/notes.txt")
    except ValueError as exc:
        posthog_client.capture_exception(exc, {"route": "/c/{domain}", "method": "GET"})
    thread = threading.Thread(target=crash)
    thread.start()
    thread.join()

    caught, uncaught = posthog_api.events("$exception")
    properties = caught["properties"]
    assert (properties["route"], properties["method"], properties["service"]) == ("/c/{domain}", "GET", "dashboard")
    for reported, kind, function in ((caught, "ValueError", "test_an_exception_is_reported_by_its_type_and_stack_"
                                      "never_by_what_it_says"), (uncaught, "KeyError", "crash")):
        (raised,) = reported["properties"]["$exception_list"]
        frames = raised["stacktrace"]["frames"]
        assert (raised["type"], raised["value"], frames[-1]["function"]) == (kind, "", function)
        assert not any("abs_path" in frame for frame in frames)  # where the code lives on this machine
        for frame in frames:  # a frame quotes the source around its line: code, and here this test's own
            for key in ("pre_context", "context_line", "post_context"):
                frame.pop(key, None)
    assert "PRIVATE" not in json.dumps([caught, uncaught])

    posthog_client.shutdown()
    assert sys.excepthook is sys.__excepthook__  # and no hook is left once PostHog is off
