import json

import pytest
import requests
import responses

from clients import telegram


def test_pack_blocks_fills_messages_up_to_limit():
    assert telegram.pack_blocks(["aaaa", "bbbb", "cccc"], limit=10) == ["aaaa\n\nbbbb", "cccc"]


def test_pack_blocks_splits_oversized_block_on_lines_then_hard():
    long_line = "x" * 25
    messages = telegram.pack_blocks(["head", f"one\ntwo\n{long_line}"], limit=10)

    assert all(len(m) <= 10 for m in messages)
    assert messages == ["head", "one\ntwo", "xxxxxxxxxx", "xxxxxxxxxx", "xxxxx"]


def test_send_does_not_leak_bot_token_in_errors(http, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "secret-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    http.post(
        "https://api.telegram.org/botsecret-token/sendMessage",
        body=requests.ConnectionError("https://api.telegram.org/botsecret-token/sendMessage unreachable"),
    )

    with pytest.raises(RuntimeError) as info:
        telegram.send("hi")
    assert "secret-token" not in str(info.value)
    assert info.value.__cause__ is None and info.value.__suppress_context__


def test_updates_long_polls_from_the_offset_and_asks_for_messages_only(http, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    api = http.post("https://api.telegram.org/bottok/getUpdates",
                    json={"ok": True, "result": [{"update_id": 7, "message": {"text": "hi", "chat": {"id": 42}}}]})

    assert telegram.updates(5, timeout=20) == [{"update_id": 7, "message": {"text": "hi", "chat": {"id": 42}}}]
    assert telegram.updates() == [{"update_id": 7, "message": {"text": "hi", "chat": {"id": 42}}}]

    first, second = [json.loads(call.request.body) for call in api.calls]
    assert first == {"offset": 5, "timeout": 20, "allowed_updates": ["message"]}
    assert second == {"timeout": telegram.POLL_SECONDS, "allowed_updates": ["message"]}
    assert api.calls[0].request.req_kwargs["timeout"] == 35  # longer than the long poll itself


def test_updates_reports_telegram_errors_without_the_token(http, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "secret-token")
    http.post("https://api.telegram.org/botsecret-token/getUpdates",
              json={"ok": False, "description": "Conflict: terminated by other getUpdates request", "error_code": 409})
    with pytest.raises(RuntimeError, match=r"Conflict: terminated by other getUpdates request \(code 409\)"):
        telegram.updates()

    http.replace(responses.POST, "https://api.telegram.org/botsecret-token/getUpdates",
                 body=requests.ConnectionError("https://api.telegram.org/botsecret-token/getUpdates unreachable"))
    with pytest.raises(RuntimeError) as info:
        telegram.updates()
    assert "secret-token" not in str(info.value) and info.value.__cause__ is None
