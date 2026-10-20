"""Outlook mail through Microsoft Graph, read-only: device-code sign-in, token refresh, recent messages.

Sign-in uses your own app registration (OUTLOOK_CLIENT_ID, a public client) with the delegated Mail.Read
permission, so this code can read mail but never send, move or delete it. The refresh token lives in TOKEN_FILE in
the data folder; Microsoft returns a new one on every refresh, which replaces the old one there.
"""

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

TOKEN_FILE = "outlook_token.json"
SCOPES = "offline_access https://graph.microsoft.com/Mail.Read"
GRAPH = "https://graph.microsoft.com/v1.0"
FOLDERS = ("inbox", "junkemail")  # Graph's well-known folder names; some rejections land in Junk
MAX_MESSAGES = 500  # per folder and check
PAGE_SIZE = 50
TIMEOUT = 15
SELECT = "id,internetMessageId,subject,from,receivedDateTime,body,webLink"
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
    tmp.write_text(json.dumps({"refresh_token": body["refresh_token"],
                               "saved_at": datetime.now().isoformat(timespec="seconds")}, indent=2))
    tmp.chmod(0o600)
    tmp.replace(path)  # atomic: a crash never leaves a half-written token


def _error(body):
    description = next(iter((body.get("error_description") or "").splitlines()), "")[:200]
    return f"{body.get('error', 'error')}: {description}" if description else body.get("error", "error")


def login(path=TOKEN_FILE, *, show=print, sleep=time.sleep):
    """Device-code sign-in: shows the URL and code to enter in a browser, waits for it, saves the refresh token."""
    client_id = os.environ["OUTLOOK_CLIENT_ID"]
    status, body = _post(f"{_authority()}/devicecode", {"client_id": client_id, "scope": SCOPES})
    if status != 200:
        raise AuthError(f"could not start the sign-in ({_error(body)})")
    show(body["message"])
    interval = int(body.get("interval", 5))
    deadline = time.monotonic() + int(body.get("expires_in", 900))
    while time.monotonic() < deadline:
        sleep(interval)
        status, reply = _post(f"{_authority()}/token", {
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "client_id": client_id,
            "device_code": body["device_code"],
        })
        if status == 200 and reply.get("refresh_token"):
            _save(path, reply)
            return
        if reply.get("error") == "authorization_pending":
            continue
        if reply.get("error") == "slow_down":
            interval += 5
            continue
        raise AuthError(f"sign-in failed ({_error(reply)})")
    raise AuthError("sign-in timed out; start it again")


def access_token(path=TOKEN_FILE):
    """A fresh access token from the saved refresh token, saving the new refresh token Microsoft returns."""
    path = Path(path)
    if not path.exists():
        raise AuthError(f"not signed in to Outlook ({path} is missing): {LOGIN_HINT}")
    saved = json.loads(path.read_text())
    status, body = _post(f"{_authority()}/token", {
        "grant_type": "refresh_token",
        "client_id": os.environ["OUTLOOK_CLIENT_ID"],
        "refresh_token": saved["refresh_token"],
        "scope": SCOPES,
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


def messages(token, since):
    """Messages received at or after `since` (an aware datetime) in FOLDERS, oldest first per folder.

    Returns (messages, resume): at most MAX_MESSAGES per folder are read. When a folder holds more, `resume` is the
    receipt time (aware) of the newest message read there (the earliest such time over the folders), and the next
    check should start there; otherwise it is None.
    """
    headers = {"Authorization": f"Bearer {token}", "Prefer": 'outlook.body-content-type="text"'}
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
            try:
                response = requests.get(url, params=params, headers=headers, timeout=TIMEOUT)
            except requests.RequestException as exc:
                raise RuntimeError(f"Outlook request failed: {type(exc).__name__}") from None
            if response.status_code == 401:
                raise AuthError(f"Outlook refused the sign-in: {LOGIN_HINT}")
            if response.status_code != 200:
                try:
                    detail = response.json().get("error", {}).get("message", "")
                except ValueError:
                    detail = ""
                raise RuntimeError(f"Outlook {folder}: HTTP {response.status_code} {detail}".rstrip())
            page = response.json()
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
