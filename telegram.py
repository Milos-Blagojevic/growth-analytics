"""Telegram delivery via the Bot API (sendMessage, plain text)."""

import os

import requests

MAX_MESSAGE_CHARS = 4096  # Telegram sendMessage text limit


def configured():
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"))


def pack_blocks(blocks, limit=MAX_MESSAGE_CHARS):
    """Join text blocks with blank lines into messages of at most `limit` chars.

    A single block longer than `limit` is split on line boundaries (hard-split if a line is too long).
    """
    messages, current = [], ""
    for block in blocks:
        for piece in _split(block, limit):
            candidate = f"{current}\n\n{piece}" if current else piece
            if len(candidate) <= limit:
                current = candidate
            else:
                messages.append(current)
                current = piece
    if current:
        messages.append(current)
    return messages


def _split(text, limit):
    if len(text) <= limit:
        return [text]
    pieces, current = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                pieces.append(current)
                current = ""
            pieces.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) <= limit:
            current = candidate
        else:
            pieces.append(current)
            current = line
    if current:
        pieces.append(current)
    return pieces


def send(text):
    """Send one message. Raises RuntimeError with Telegram's description on failure."""
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": os.environ["TELEGRAM_CHAT_ID"],
                "text": text,
                "disable_web_page_preview": True,
            },
            timeout=30,
        )
    except requests.RequestException as exc:
        # The exception text embeds the URL, which contains the bot token.
        raise RuntimeError(f"Telegram request failed: {type(exc).__name__}") from None
    data = response.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram sendMessage failed: {data.get('description')} (code {data.get('error_code')})")
