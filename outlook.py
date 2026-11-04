"""Outlook mail through Microsoft Graph: browser sign-in, token refresh, recent messages, search, drafts.

Sign-in uses your own app registration (OUTLOOK_CLIENT_ID, a public client with the `http://localhost` desktop
redirect) with the delegated Mail.ReadWrite permission, so this code can read mail and create drafts (the chat
bot's new emails and replies) but never send, move or delete it: Mail.Send is never requested. `hunt.py mail`
reads untrusted email and asks for read-only (Mail.Read) access tokens from the same refresh token. The refresh
token lives in TOKEN_FILE in the data folder; Microsoft returns a new one on every refresh, which replaces the old
one there.
"""

import html
import json
import logging
import os
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

import requests

import oauth

logger = logging.getLogger("outlook")

TOKEN_FILE = "outlook_token.json"
SCOPES = "offline_access https://graph.microsoft.com/Mail.ReadWrite"  # consented at login: read and draft
READ_SCOPE = "offline_access https://graph.microsoft.com/Mail.Read"  # what `mail` asks for: read only
GRAPH = "https://graph.microsoft.com/v1.0"
FOLDERS = ("inbox", "junkemail")  # Graph's well-known folder names; some rejections land in Junk
MAX_MESSAGES = 500  # per folder and check
PAGE_SIZE = 50
TIMEOUT = 15
SELECT = "id,internetMessageId,subject,from,receivedDateTime,body,webLink"
CHAT_SELECT = "id,subject,from,toRecipients,receivedDateTime,bodyPreview,webLink"
MAX_SEARCH = 50
TEXT_BODY = {"Prefer": 'outlook.body-content-type="text"'}  # Graph converts HTML bodies to text for us
LOGIN_HINT = "run `python hunt.py outlook-login` (see README, Email updates)"


class AuthError(RuntimeError):
    """Not signed in, or the sign-in expired or was revoked."""


def configured():
    return bool(os.environ.get("OUTLOOK_CLIENT_ID"))


def _authority():
    return f"https://login.microsoftonline.com/{os.environ.get('OUTLOOK_TENANT') or 'common'}/oauth2/v2.0"


def _post(url, data):
    try:
        response = requests.post(url, data=data, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise RuntimeError(f"Microsoft sign-in request failed: {type(exc).__name__}") from None
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


def _auth_url(client_id, redirect_uri, state):
    return f"{_authority()}/authorize?" + urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "response_mode": "query",
        "scope": SCOPES,
        "state": state,
    })


def login(path=TOKEN_FILE, *, show=print, open_browser=webbrowser.open, timeout=oauth.TIMEOUT):
    """Browser sign-in (authorization code, loopback redirect): shows the URL, catches the redirect, saves the
    refresh token. Microsoft's portal registers the desktop redirect as `http://localhost` and ignores the port."""
    client_id = os.environ["OUTLOOK_CLIENT_ID"]
    try:
        code, redirect_uri = oauth.browser_code(lambda uri, state: _auth_url(client_id, uri, state), host="localhost",
                                                show=show, open_browser=open_browser, timeout=timeout)
    except (oauth.Declined, TimeoutError) as exc:
        raise AuthError(f"sign-in failed ({exc})" if isinstance(exc, oauth.Declined) else str(exc)) from None
    status, body = _post(f"{_authority()}/token", {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": SCOPES,
    })
    if status != 200 or not body.get("refresh_token"):
        raise AuthError(f"sign-in failed ({_error(body)})")
    _save(path, body)


def access_token(path=TOKEN_FILE, *, scope=READ_SCOPE):
    """A fresh access token for `scope` from the saved refresh token, saving the new refresh token Microsoft returns.

    The default is read-only; the chat bot passes SCOPES to create drafts."""
    path = Path(path)
    if not path.exists():
        raise AuthError(f"not signed in to Outlook ({path} is missing): {LOGIN_HINT}")
    saved = json.loads(path.read_text())
    status, body = _post(f"{_authority()}/token", {
        "grant_type": "refresh_token",
        "client_id": os.environ["OUTLOOK_CLIENT_ID"],
        "refresh_token": saved["refresh_token"],
        "scope": scope,
    })
    if status != 200 or not body.get("access_token"):
        raise AuthError(f"the Outlook sign-in no longer works ({_error(body)}): {LOGIN_HINT}")
    if body.get("refresh_token"):
        _save(path, body)
    return body["access_token"]


def _local(stamp):
    """Graph's UTC "2026-10-18T09:12:00Z" -> local naive ISO time, like every other time the dashboard shows."""
    return datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone().replace(tzinfo=None).isoformat(
        timespec="seconds")


def _message(m, folder):
    sender = (m.get("from") or {}).get("emailAddress") or {}
    return {
        "id": m.get("internetMessageId") or m["id"],  # the Graph id changes when a message moves folders
        "received_at": _local(m["receivedDateTime"]),
        "from_name": sender.get("name") or "",
        "from_address": (sender.get("address") or "").lower(),
        "subject": m.get("subject") or "",
        "body": (m.get("body") or {}).get("content") or "",
        "web_link": m.get("webLink") or "",
        "folder": folder,
    }


def _request(method, url, token, *, params=None, json=None, headers=None, label="Outlook"):
    """One Graph call; the decoded JSON body. 401 -> AuthError, any other failure -> RuntimeError (without the token)."""
    headers = {"Authorization": f"Bearer {token}", **(headers or {})}
    try:
        response = requests.request(method, url, params=params, json=json, headers=headers, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise RuntimeError(f"Outlook request failed: {type(exc).__name__}") from None
    if response.status_code == 401:
        raise AuthError(f"Outlook refused the sign-in: {LOGIN_HINT}")
    if not 200 <= response.status_code < 300:
        try:
            detail = response.json().get("error", {}).get("message", "")
        except ValueError:
            detail = ""
        raise RuntimeError(f"{label}: HTTP {response.status_code} {detail}".rstrip())
    try:
        return response.json()
    except ValueError:
        return {}


def messages(token, since):
    """Messages received at or after `since` (an aware datetime) in FOLDERS, oldest first per folder.

    Returns (messages, resume): at most MAX_MESSAGES per folder are read. When a folder holds more, `resume` is the
    receipt time (aware) of the newest message read there (the earliest such time over the folders), and the next
    check should start there; otherwise it is None.
    """
    found, resume = [], None
    for folder in FOLDERS:
        url = f"{GRAPH}/me/mailFolders/{folder}/messages"
        params = {
            "$filter": f"receivedDateTime ge {since.astimezone(timezone.utc):%Y-%m-%dT%H:%M:%SZ}",
            "$orderby": "receivedDateTime asc",
            "$select": SELECT,
            "$top": str(PAGE_SIZE),
        }
        count, last = 0, None
        while url:
            page = _request("GET", url, token, params=params, headers=TEXT_BODY, label=f"Outlook {folder}")
            values = page.get("value", [])
            taken = values[: MAX_MESSAGES - count]
            for m in taken:
                found.append(_message(m, folder))
                last = m["receivedDateTime"]
            count += len(taken)
            url, params = page.get("@odata.nextLink"), None  # the next link carries the query
            if count >= MAX_MESSAGES:
                if url or len(values) > len(taken):  # more mail in this folder than one check reads
                    cut = datetime.fromisoformat(last.replace("Z", "+00:00"))
                    resume = cut if resume is None else min(resume, cut)
                break
    return found, resume


# The chat bot's view of a message. Unlike _message, `id` is the Graph id: that is what read() and
# create_reply_draft() take, and the bot never stores it across checks.
def _chat_message(m):
    sender = (m.get("from") or {}).get("emailAddress") or {}
    return {
        "id": m["id"],
        "received_at": _local(m["receivedDateTime"]),
        "from_name": sender.get("name") or "",
        "from_address": (sender.get("address") or "").lower(),
        "to": _addresses(m.get("toRecipients")),
        "subject": m.get("subject") or "",
        "preview": m.get("bodyPreview") or "",
        "web_link": m.get("webLink") or "",
    }


def _addresses(recipients):
    return [a.lower() for a in (((r.get("emailAddress") or {}).get("address") or "") for r in recipients or ()) if a]


def _draft(d):
    return {"id": d["id"], "web_link": d.get("webLink") or "", "subject": d.get("subject") or "",
            "to": _addresses(d.get("toRecipients"))}


def search(token, query="", *, days=None, limit=20, folder="inbox"):
    """Up to `limit` (at most MAX_SEARCH) messages in `folder` (a Graph well-known folder name), one page.

    Without `query`: newest first, from the last `days` days when given. With one: Graph's $search (KQL, relevance
    order; it cannot be combined with $orderby or $filter, so `days` becomes a `received>=` term in the query).
    """
    params = {"$select": CHAT_SELECT, "$top": str(max(1, min(limit, MAX_SEARCH)))}
    if query:
        kql = query.replace('"', '\\"')
        if days is not None:
            kql += f" received>={datetime.now(timezone.utc) - timedelta(days=days):%Y-%m-%d}"
        params["$search"] = f'"{kql}"'
    else:
        params["$orderby"] = "receivedDateTime desc"
        if days is not None:
            params["$filter"] = f"receivedDateTime ge {datetime.now(timezone.utc) - timedelta(days=days):%Y-%m-%dT%H:%M:%SZ}"
    page = _request("GET", f"{GRAPH}/me/mailFolders/{folder}/messages", token, params=params)
    return [_chat_message(m) for m in page.get("value", [])]


def read(token, message_id):
    """One message by Graph id, with its whole body as plain text."""
    m = _request("GET", f"{GRAPH}/me/messages/{message_id}", token, params={"$select": f"{CHAT_SELECT},body"},
                 headers=TEXT_BODY)
    return {**_chat_message(m), "body": (m.get("body") or {}).get("content") or ""}


def create_draft(token, *, to, subject, body):
    """A new plain-text draft in Drafts; nothing is sent."""
    draft = _request("POST", f"{GRAPH}/me/messages", token, json={
        "subject": subject,
        "body": {"contentType": "Text", "content": body},
        "toRecipients": [{"emailAddress": {"address": a}} for a in to],
    })
    return _draft(draft)


def create_reply_draft(token, message_id, body):
    """A reply draft to the message's sender (Graph picks Reply-To or From and the "Re:" subject, and keeps the
    thread) with `body` above the quoted original; nothing is sent."""
    draft = _request("POST", f"{GRAPH}/me/messages/{message_id}/createReply", token, json={})
    original = draft.get("body") or {}
    if (original.get("contentType") or "").lower() == "html":
        content = {"contentType": "HTML",
                   "content": f"<div>{html.escape(body).replace(chr(10), '<br>')}</div><br>{original.get('content') or ''}"}
    else:
        content = {"contentType": "Text", "content": f"{body}\n\n{original.get('content') or ''}"}
    _request("PATCH", f"{GRAPH}/me/messages/{draft['id']}", token, json={"body": content})
    return _draft(draft)
