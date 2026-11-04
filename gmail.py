"""Gmail through the Gmail REST API: browser sign-in, token refresh, search, full text, drafts. Never sends.

Sign-in uses your own OAuth client (GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET, a "Desktop app" client; its secret is
not really secret, but Google requires it on the token exchange) with the gmail.readonly and gmail.compose scopes.
gmail.compose is the narrowest scope that manages drafts; it would also allow sending, but this code never calls
`send`: drafts are created in the mailbox and the owner sends them from Gmail. Google does not allow Gmail scopes in
the device-code flow, so sign-in opens a browser and catches the redirect on a loopback server. The refresh token
lives in TOKEN_FILE in the data folder; Google keeps it valid until revoked and does not rotate it.
"""

import base64
import html
import json
import logging
import os
import re
import secrets
import time
import webbrowser
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import getaddresses, parseaddr
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import requests

logger = logging.getLogger("gmail")

TOKEN_FILE = "gmail_token.json"
SCOPES = "https://www.googleapis.com/auth/gmail.readonly https://www.googleapis.com/auth/gmail.compose"
AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN = "https://oauth2.googleapis.com/token"
API = "https://gmail.googleapis.com/gmail/v1/users/me"
TIMEOUT = 15
MAX_RESULTS = 50
LOGIN_HINT = "run `python hunt.py gmail-login` (see README, Chat with your mail)"
METADATA_HEADERS = ("From", "To", "Subject")
REPLY_HEADERS = ("From", "Reply-To", "Subject", "Message-ID", "References")


class AuthError(RuntimeError):
    """Not signed in, or the sign-in expired or was revoked."""


def configured():
    return bool(os.environ.get("GMAIL_CLIENT_ID") and os.environ.get("GMAIL_CLIENT_SECRET"))


def _post(url, data):
    try:
        response = requests.post(url, data=data, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise RuntimeError(f"Google sign-in request failed: {type(exc).__name__}") from None
    try:
        return response.status_code, response.json()
    except ValueError:
        return response.status_code, {}


def _save(path, body):
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    # A .tmp left behind by an interrupted save may be wider than 0600: never reuse it. Then the file is created
    # owner-only before anything is written, so the token is never readable by others, not even briefly.
    tmp.unlink(missing_ok=True)
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        f.write(json.dumps({"refresh_token": body["refresh_token"],
                            "saved_at": datetime.now().isoformat(timespec="seconds")}, indent=2))
    tmp.replace(path)  # atomic: a crash never leaves a half-written token


def _error(body):
    description = next(iter((body.get("error_description") or "").splitlines()), "")[:200]
    return f"{body.get('error', 'error')}: {description}" if description else body.get("error", "error")


class _Redirect(BaseHTTPRequestHandler):
    """Catches Google's redirect back to the loopback server: one GET / with `code` and `state`, or `error`."""

    def do_GET(self):  # noqa: N802 (http.server's name)
        query = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
        if urlparse(self.path).path != "/" or query.get("state") != self.server.state:
            self._reply(400, "Unexpected request; go back to the terminal and start the sign-in again.")
            return  # not our redirect: keep waiting
        if query.get("error"):
            self.server.error = query["error"]
            self._reply(200, "Sign-in declined; you can close this tab.")
        elif query.get("code"):
            self.server.code = query["code"]
            self._reply(200, "Signed in; you can close this tab.")
        else:
            self._reply(400, "Missing code; go back to the terminal and start the sign-in again.")

    def _reply(self, status, text):
        body = text.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):  # noqa: A002 (http.server's signature)
        logger.debug(format, *args)


def login(path=TOKEN_FILE, *, show=print, open_browser=webbrowser.open, timeout=900):
    """Browser sign-in (installed-app loopback flow): shows the URL, catches the redirect, saves the refresh token."""
    client_id, client_secret = os.environ["GMAIL_CLIENT_ID"], os.environ["GMAIL_CLIENT_SECRET"]
    with HTTPServer(("127.0.0.1", 0), _Redirect) as server:
        server.state, server.code, server.error = secrets.token_urlsafe(16), None, None
        server.timeout = 1  # handle_request returns every second so the deadline is checked
        redirect_uri = f"http://127.0.0.1:{server.server_port}"
        url = AUTH + "?" + urlencode({
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": SCOPES,
            "access_type": "offline",  # a refresh token
            "prompt": "consent",  # Google only hands out a refresh token when consent is shown
            "state": server.state,
        })
        show(f"Sign in to Google in your browser (this window is listening on {redirect_uri}):\n{url}")
        try:
            open_browser(url)
        except Exception:  # noqa: BLE001 (no browser here: the URL is on screen)
            pass
        deadline = time.monotonic() + timeout
        while server.code is None and server.error is None:
            if time.monotonic() >= deadline:
                raise AuthError("sign-in timed out; start it again")
            server.handle_request()
        if server.error:
            raise AuthError(f"sign-in failed ({server.error})")
        code = server.code
    status, body = _post(TOKEN, {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
    })
    if status != 200 or not body.get("refresh_token"):
        raise AuthError(f"sign-in failed ({_error(body)})")
    _save(path, body)


def access_token(path=TOKEN_FILE):
    """A fresh access token from the saved refresh token (Google does not rotate it, so nothing is saved)."""
    path = Path(path)
    if not path.exists():
        raise AuthError(f"not signed in to Gmail ({path} is missing): {LOGIN_HINT}")
    saved = json.loads(path.read_text())
    status, body = _post(TOKEN, {
        "grant_type": "refresh_token",
        "client_id": os.environ["GMAIL_CLIENT_ID"],
        "client_secret": os.environ["GMAIL_CLIENT_SECRET"],
        "refresh_token": saved["refresh_token"],
    })
    if status != 200 or not body.get("access_token"):
        raise AuthError(f"the Gmail sign-in no longer works ({_error(body)}): {LOGIN_HINT}")
    return body["access_token"]


def _request(method, token, url, **kwargs):
    """A Gmail API call; 401 -> AuthError, other failures -> RuntimeError without the token in the message."""
    headers = {"Authorization": f"Bearer {token}"}
    try:
        response = requests.request(method, url, headers=headers, timeout=TIMEOUT, **kwargs)
    except requests.RequestException as exc:
        raise RuntimeError(f"Gmail request failed: {type(exc).__name__}") from None
    if response.status_code == 401:
        raise AuthError(f"Gmail refused the sign-in: {LOGIN_HINT}")
    if not 200 <= response.status_code < 300:
        try:
            detail = response.json().get("error", {}).get("message", "")
        except (ValueError, AttributeError):
            detail = ""
        raise RuntimeError(f"Gmail: HTTP {response.status_code} {detail}".rstrip())
    return response.json()


def _get_message(token, message_id, fmt, headers=()):
    params = [("format", fmt)] + [("metadataHeaders", h) for h in headers]
    return _request("GET", token, f"{API}/messages/{message_id}", params=params)


def _headers(payload):
    """The message headers as a case-insensitive dict (the first value of each)."""
    found = {}
    for h in payload.get("headers") or []:
        found.setdefault(h.get("name", "").lower(), h.get("value") or "")
    return found


def _local(millis):
    """Gmail's `internalDate` (ms since the epoch) -> local naive ISO time, like outlook._local."""
    return datetime.fromtimestamp(int(millis) / 1000, timezone.utc).astimezone().replace(tzinfo=None).isoformat(
        timespec="seconds")


def _message(m):
    headers = _headers(m.get("payload") or {})
    name, address = parseaddr(headers.get("from", ""))
    return {
        "id": m["id"],
        "received_at": _local(m.get("internalDate") or 0),
        "from_name": name,
        "from_address": address.lower(),
        "to": [a.lower() for _, a in getaddresses([headers.get("to", "")]) if a],
        "subject": headers.get("subject", ""),
        "preview": html.unescape(m.get("snippet") or ""),  # the API HTML-escapes snippets
        "web_link": f"https://mail.google.com/mail/#all/{m['id']}",
    }


def search(token, query="", *, days=None, limit=20, folder="inbox"):
    """Messages matching Gmail's search (newest first): `in:<folder>`, the free-text query, `newer_than:<days>d`."""
    q = " ".join(part for part in (f"in:{folder}", query.strip(), f"newer_than:{days}d" if days else "") if part)
    listing = _request("GET", token, f"{API}/messages", params={"q": q, "maxResults": str(min(limit, MAX_RESULTS))})
    return [_message(_get_message(token, m["id"], "metadata", METADATA_HEADERS)) for m in listing.get("messages") or []]


def _decode(data):
    """Gmail's base64url body data (the `-_` alphabet, padding often missing) -> text."""
    raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    return raw.decode("utf-8", errors="replace")


def _parts(payload):
    """The payload and every nested part, depth first, in order."""
    yield payload
    for part in payload.get("parts") or []:
        yield from _parts(part)


def _text(html_text):
    """Plain text from an HTML body: styles and scripts dropped, block ends become newlines, tags removed."""
    text = re.sub(r"(?is)<(style|script)\b.*?</\1\s*>", "", html_text)
    text = re.sub(r"(?i)<br\s*/?>|</p\s*>|</div\s*>|</tr\s*>|</li\s*>|</h[1-6]\s*>", "\n", text)
    text = re.sub(r"(?s)<[^>]+>", "", text)
    text = html.unescape(text)
    text = "\n".join(line.strip() for line in text.splitlines())
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _body(payload):
    """The first text/plain part's text, else the first text/html part as plain text; attachments are ignored."""
    plain = html_part = None
    for part in _parts(payload):
        mime, data = part.get("mimeType", ""), (part.get("body") or {}).get("data")
        if not data:
            continue
        if mime == "text/plain" and plain is None:
            plain = _decode(data)
        elif mime == "text/html" and html_part is None:
            html_part = _decode(data)
    if plain is not None:
        return plain
    return _text(html_part) if html_part is not None else ""


def read(token, message_id):
    """One message with its whole plain-text body."""
    m = _get_message(token, message_id, "full")
    found = _message(m)
    found["body"] = _body(m.get("payload") or {})
    return found


def _mime(to, subject, body, extra_headers=()):
    msg = EmailMessage()
    msg["To"] = ", ".join(to)
    msg["Subject"] = subject
    for name, value in extra_headers:
        if value:
            msg[name] = value
    msg.set_content(body)
    return msg


def _post_draft(token, msg, thread_id=None):
    message = {"raw": base64.urlsafe_b64encode(msg.as_bytes()).decode()}
    if thread_id:
        message["threadId"] = thread_id
    draft = _request("POST", token, f"{API}/drafts", json={"message": message})
    return {
        "id": draft["id"],
        "web_link": "https://mail.google.com/mail/#drafts",
        "subject": msg["Subject"],
        "to": [a for _, a in getaddresses([msg["To"]]) if a],
    }


def create_draft(token, *, to, subject, body):
    """A new plain-text draft in Drafts; nothing is sent."""
    return _post_draft(token, _mime(to, subject, body))


def create_reply_draft(token, message_id, body):
    """A reply draft to the message's sender (Reply-To when present) in the same thread; nothing is sent."""
    original = _get_message(token, message_id, "metadata", REPLY_HEADERS)
    headers = _headers(original.get("payload") or {})
    _, to = parseaddr(headers.get("reply-to") or headers.get("from", ""))
    subject = headers.get("subject", "")
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"
    message_ref = headers.get("message-id", "")
    references = " ".join(part for part in (headers.get("references", ""), message_ref) if part)
    msg = _mime([to], subject, body, [("In-Reply-To", message_ref), ("References", references)])
    return _post_draft(token, msg, original.get("threadId"))
