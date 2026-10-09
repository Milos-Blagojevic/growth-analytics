import pytest
import requests

import telegram


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
