import json
import os
import time
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import pytest
import requests
import responses

import outlook

AUTH = "https://login.microsoftonline.com/common/oauth2/v2.0"
INBOX = "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages"
JUNK = "https://graph.microsoft.com/v1.0/me/mailFolders/junkemail/messages"


@pytest.fixture(autouse=True)
def client_id(monkeypatch):
    monkeypatch.setenv("OUTLOOK_CLIENT_ID", "app-123")


@pytest.fixture
def belgrade(monkeypatch):
    monkeypatch.setenv("TZ", "Europe/Belgrade")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def replies(http, method, url, *bodies):
    """Serves (status, body) replies in order (an exception instance as the body is raised instead); returns the decoded
    form bodies of the requests."""
    queue, seen = list(bodies), []

    def callback(request):
        seen.append({k: v[0] for k, v in parse_qs(request.body).items()})
        status, body = queue.pop(0)
        if isinstance(body, Exception):
            return body
        return status, {}, json.dumps(body)

    http.add_callback(method, url, callback=callback, content_type="application/json")
    return seen


def test_login_waits_for_the_browser_sign_in_and_keeps_only_the_refresh_token(http, tmp_path):
    http.post(f"{AUTH}/devicecode", json={"device_code": "dev-1", "user_code": "ABC123", "expires_in": 900,
                                          "interval": 5, "message": "Open https://microsoft.com/devicelogin, enter ABC123"})
    token_requests = replies(http, responses.POST, f"{AUTH}/token",
                             (400, {"error": "authorization_pending"}),
                             (400, {"error": "slow_down"}),
                             (200, {"access_token": "at-1", "refresh_token": "rt-1", "expires_in": 3600}))
    shown, sleeps = [], []
    path = tmp_path / "outlook_token.json"

    outlook.login(path, show=shown.append, sleep=sleeps.append)

    assert shown == ["Open https://microsoft.com/devicelogin, enter ABC123"]
    assert sleeps == [5, 5, 10]  # slow_down adds 5 seconds to the polling interval
    assert {r["device_code"] for r in token_requests} == {"dev-1"}
    saved = json.loads(path.read_text())
    assert saved["refresh_token"] == "rt-1" and "access_token" not in saved
    assert path.stat().st_mode & 0o777 == 0o600


def test_login_stops_when_the_sign_in_is_declined(http, tmp_path):
    http.post(f"{AUTH}/devicecode", json={"device_code": "d", "expires_in": 900, "interval": 1, "message": "m"})
    replies(http, responses.POST, f"{AUTH}/token", (400, {"error": "authorization_declined", "error_description": "No."}))

    with pytest.raises(outlook.AuthError, match="authorization_declined: No."):
        outlook.login(tmp_path / "t.json", show=lambda _: None, sleep=lambda _: None)
    assert not (tmp_path / "t.json").exists()


def test_login_keeps_polling_through_a_network_blip(http, tmp_path):
    http.post(f"{AUTH}/devicecode", json={"device_code": "dev-1", "expires_in": 900, "interval": 5, "message": "m"})
    token_requests = replies(http, responses.POST, f"{AUTH}/token",
                             (None, requests.ConnectionError("wifi dropped")),
                             (400, {"error": "authorization_pending"}),
                             (200, {"access_token": "at-1", "refresh_token": "rt-1"}))
    sleeps = []
    path = tmp_path / "outlook_token.json"

    outlook.login(path, show=lambda _: None, sleep=sleeps.append)

    assert len(token_requests) == 3 and sleeps == [5, 5, 5]  # the blip costs one normal polling interval
    assert json.loads(path.read_text())["refresh_token"] == "rt-1"


@pytest.mark.parametrize("stale_tmp", [False, True])  # a wider .tmp left behind by an interrupted save
def test_saved_token_is_never_readable_by_others(tmp_path, monkeypatch, stale_tmp):
    path = tmp_path / "outlook_token.json"
    if stale_tmp:
        path.with_suffix(".json.tmp").write_text("{ half written")
        path.with_suffix(".json.tmp").chmod(0o644)
    modes = []
    real_dumps = json.dumps

    def dumps(*args, **kwargs):  # runs while the token is being written: what can others see at that moment?
        tmp = path.with_suffix(".json.tmp")
        modes.append(tmp.stat().st_mode & 0o777 if tmp.exists() else None)
        return real_dumps(*args, **kwargs)

    monkeypatch.setattr(json, "dumps", dumps)
    umask = os.umask(0o022)  # a permissive umask: files normally come out 0644
    try:
        outlook._save(path, {"refresh_token": "rt-1", "access_token": "at-1"})
    finally:
        os.umask(umask)

    assert modes == [0o600]  # owner-only from the moment the file exists
    assert path.stat().st_mode & 0o777 == 0o600
    assert [p.name for p in tmp_path.iterdir()] == ["outlook_token.json"]  # no .tmp left behind
    assert json.loads(path.read_text())["refresh_token"] == "rt-1"


def test_access_token_replaces_the_saved_refresh_token(http, tmp_path):
    path = tmp_path / "outlook_token.json"
    path.write_text(json.dumps({"refresh_token": "rt-1"}))
    requests_sent = replies(http, responses.POST, f"{AUTH}/token",
                            (200, {"access_token": "at-2", "refresh_token": "rt-2"}))

    assert outlook.access_token(path) == "at-2"

    (sent,) = requests_sent
    assert (sent["grant_type"], sent["refresh_token"], sent["client_id"]) == ("refresh_token", "rt-1", "app-123")
    assert "Mail.Read" in sent["scope"] and "Mail.Send" not in sent["scope"]
    assert json.loads(path.read_text())["refresh_token"] == "rt-2"


def test_expired_or_missing_sign_in_asks_to_sign_in_again(http, tmp_path):
    with pytest.raises(outlook.AuthError, match="outlook-login"):
        outlook.access_token(tmp_path / "missing.json")
    assert len(http.calls) == 0

    path = tmp_path / "outlook_token.json"
    path.write_text(json.dumps({"refresh_token": "rt-old"}))
    replies(http, responses.POST, f"{AUTH}/token", (400, {"error": "invalid_grant", "error_description": "AADSTS70000: expired.\nTrace ID: x"}))
    with pytest.raises(outlook.AuthError, match=r"invalid_grant: AADSTS70000: expired\.\): run `python hunt.py outlook-login`"):
        outlook.access_token(path)
    assert json.loads(path.read_text())["refresh_token"] == "rt-old"


def graph_message(n, received, **extra):
    return {"id": f"graph-{n}", "internetMessageId": f"<m{n}@mail.example>", "subject": f"Subject {n}",
            "from": {"emailAddress": {"name": "Ana", "address": "Ana@Acme.com"}}, "receivedDateTime": received,
            "body": {"contentType": "text", "content": f"Body {n}"}, "webLink": f"https://outlook.live.com/{n}", **extra}


def test_messages_reads_inbox_pages_and_junk_with_local_times(http, belgrade):
    inbox_urls = []

    def inbox(request):
        inbox_urls.append(request.url)
        if "skiptoken" in request.url:
            return 200, {}, json.dumps({"value": [graph_message(2, "2026-10-18T09:12:00Z")]})
        assert request.headers["Authorization"] == "Bearer at-1"
        assert request.headers["Prefer"] == 'outlook.body-content-type="text"'
        return 200, {}, json.dumps({"value": [graph_message(1, "2026-10-17T08:00:00Z")],
                                    "@odata.nextLink": f"{INBOX}?$skiptoken=abc"})

    http.add_callback(responses.GET, INBOX, callback=inbox, content_type="application/json")
    http.get(JUNK, json={"value": [graph_message(3, "2026-10-18T10:00:00Z", internetMessageId=None)]})

    found, resume = outlook.messages("at-1", datetime(2026, 10, 13, 6, 30, tzinfo=timezone.utc))

    query = parse_qs(urlparse(inbox_urls[0]).query)
    assert query["$filter"] == ["receivedDateTime ge 2026-10-13T06:30:00Z"]
    assert query["$orderby"] == ["receivedDateTime asc"]  # oldest first, so a capped check can resume
    assert [(m["id"], m["folder"], m["received_at"]) for m in found] == [
        ("<m1@mail.example>", "inbox", "2026-10-17T10:00:00"),  # UTC+2 in Belgrade in October
        ("<m2@mail.example>", "inbox", "2026-10-18T11:12:00"),
        ("graph-3", "junkemail", "2026-10-18T12:00:00"),  # no Internet message ID: the Graph id
    ]
    assert (found[0]["from_name"], found[0]["from_address"], found[0]["body"]) == ("Ana", "ana@acme.com", "Body 1")
    assert resume is None


def test_messages_beyond_the_cap_are_left_for_the_next_check(http, monkeypatch):
    monkeypatch.setattr(outlook, "MAX_MESSAGES", 2)
    http.get(INBOX, json={"value": [graph_message(n, f"2026-10-1{n}T09:00:00Z") for n in (1, 2, 3)]})
    http.get(JUNK, json={"value": [graph_message(4, "2026-10-11T08:00:00Z")]})

    found, resume = outlook.messages("at-1", datetime(2026, 10, 1, tzinfo=timezone.utc))

    assert [m["id"] for m in found] == ["<m1@mail.example>", "<m2@mail.example>", "<m4@mail.example>"]
    assert resume == datetime(2026, 10, 12, 9, tzinfo=timezone.utc)  # the newest inbox email read


def test_messages_reports_a_refused_sign_in(http):
    http.get(INBOX, status=401, json={"error": {"code": "InvalidAuthenticationToken"}})

    with pytest.raises(outlook.AuthError, match="outlook-login"):
        outlook.messages("at-1", datetime(2026, 10, 13, tzinfo=timezone.utc))
