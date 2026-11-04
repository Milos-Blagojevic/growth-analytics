import json
import os
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest
import requests
import responses

import outlook
from browser_login import Login

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


def test_login_catches_the_browser_redirect_and_keeps_only_the_refresh_token(http, tmp_path):
    token_requests = replies(http, responses.POST, f"{AUTH}/token",
                             (200, {"access_token": "at-1", "refresh_token": "rt-1", "expires_in": 3600}))
    path = tmp_path / "outlook_token.json"
    login = Login(outlook.login, path)

    assert urlparse(login.url)._replace(query="").geturl() == f"{AUTH}/authorize"
    assert login.query["client_id"] == "app-123"
    assert "Mail.ReadWrite" in login.query["scope"] and "Mail.Send" not in login.query["scope"]  # read and draft, never send
    assert (login.query["response_type"], login.query["response_mode"]) == ("code", "query")
    assert login.redirect.startswith("http://localhost:") and len(login.query["state"]) >= 16  # the portal's desktop URI
    assert login.opened == [login.url]

    assert login.visit(code="x", state="wrong-state")[0] == 400  # not our redirect: keep waiting
    assert login.thread.is_alive() and token_requests == []
    status, page = login.visit(code="auth-code-1", state=login.query["state"])
    assert (status, page) == (200, "Signed in; you can close this tab.")
    login.finish()

    (sent,) = token_requests
    assert sent == {"grant_type": "authorization_code", "code": "auth-code-1", "redirect_uri": login.redirect,
                    "client_id": "app-123", "scope": outlook.SCOPES}
    saved = json.loads(path.read_text())
    assert saved["refresh_token"] == "rt-1" and "access_token" not in saved
    assert path.stat().st_mode & 0o777 == 0o600


def test_login_stops_when_the_sign_in_is_declined_or_refused(http, tmp_path):
    login = Login(outlook.login, tmp_path / "t.json")

    status, page = login.visit(error="invalid_request", error_description="AADSTS50011:+redirect+mismatch", state=login.query["state"])
    assert status == 200 and "invalid_request: AADSTS50011: redirect mismatch" in page  # the reason shows in the tab too
    with pytest.raises(outlook.AuthError, match="sign-in failed \\(invalid_request: AADSTS50011: redirect mismatch\\)"):
        login.finish()
    assert not (tmp_path / "t.json").exists() and len(http.calls) == 0


def test_login_times_out_without_a_redirect(tmp_path):
    login = Login(outlook.login, tmp_path / "t.json", timeout=0)
    with pytest.raises(outlook.AuthError, match="timed out"):
        login.finish()


def test_login_reports_a_failed_code_exchange(http, tmp_path):
    replies(http, responses.POST, f"{AUTH}/token",
            (400, {"error": "invalid_grant", "error_description": "AADSTS70000: bad code.\nTrace ID: x"}))
    login = Login(outlook.login, tmp_path / "t.json")
    login.visit(code="c", state=login.query["state"])

    with pytest.raises(outlook.AuthError, match=r"sign-in failed \(invalid_grant: AADSTS70000: bad code\.\)"):
        login.finish()
    assert not (tmp_path / "t.json").exists()


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
                            (200, {"access_token": "at-2", "refresh_token": "rt-2"}),
                            (200, {"access_token": "at-3", "refresh_token": "rt-3"}))

    assert outlook.access_token(path) == "at-2"

    (sent,) = requests_sent
    assert (sent["grant_type"], sent["refresh_token"], sent["client_id"]) == ("refresh_token", "rt-1", "app-123")
    assert sent["scope"].split() == ["offline_access", "https://graph.microsoft.com/Mail.Read"]  # `mail`: read-only
    assert json.loads(path.read_text())["refresh_token"] == "rt-2"

    assert outlook.access_token(path, scope=outlook.SCOPES) == "at-3"  # the chat bot: drafts too, never Mail.Send

    sent = requests_sent[1]
    assert sent["refresh_token"] == "rt-2"
    assert sent["scope"].split() == ["offline_access", "https://graph.microsoft.com/Mail.ReadWrite"]
    assert json.loads(path.read_text())["refresh_token"] == "rt-3"


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


MESSAGES = "https://graph.microsoft.com/v1.0/me/messages"
CHAT_SELECT = "id,subject,from,toRecipients,receivedDateTime,bodyPreview,webLink"


def chat_message(n, received="2026-10-18T09:12:00Z", **extra):
    return {"id": f"graph-{n}", "subject": f"Subject {n}", "receivedDateTime": received,
            "from": {"emailAddress": {"name": "Ana", "address": "Ana@Acme.com"}},
            "toRecipients": [{"emailAddress": {"name": "Me", "address": "Me@Example.com"}},
                             {"emailAddress": {"address": "Boss@Example.com"}}],
            "bodyPreview": f"Preview {n}", "webLink": f"https://outlook.live.com/{n}", **extra}


def graph_calls(http, method, url, *bodies):
    """Like `replies` for Graph: records (headers, query, JSON body) of each request."""
    queue, seen = list(bodies), []

    def callback(request):
        seen.append((request.headers, parse_qs(urlparse(request.url).query),
                     json.loads(request.body) if request.body else None))
        status, body = queue.pop(0)
        if isinstance(body, Exception):
            return body
        return status, {}, json.dumps(body)

    http.add_callback(method, url, callback=callback, content_type="application/json")
    return seen


def test_search_without_a_query_lists_the_newest_mail_of_the_last_days(http, belgrade):
    calls = graph_calls(http, responses.GET, INBOX,
                        (200, {"value": [chat_message(1), chat_message(2, "2026-10-17T08:00:00Z", toRecipients=None)]}))
    before = datetime.now(timezone.utc)

    found = outlook.search("at-1", days=7, limit=80)

    (headers, query, body), = calls
    assert headers["Authorization"] == "Bearer at-1" and body is None
    assert query["$orderby"] == ["receivedDateTime desc"] and query["$top"] == ["50"]  # newest first, capped
    assert query["$select"] == [CHAT_SELECT] and "$search" not in query
    (cutoff,) = query["$filter"]
    assert cutoff.startswith("receivedDateTime ge ") and cutoff.endswith("Z")
    since = datetime.fromisoformat(cutoff.removeprefix("receivedDateTime ge ").replace("Z", "+00:00"))
    assert timedelta(days=7) <= before - since < timedelta(days=7, minutes=1)
    assert found == [
        {"id": "graph-1", "received_at": "2026-10-18T11:12:00", "from_name": "Ana", "from_address": "ana@acme.com",
         "to": ["me@example.com", "boss@example.com"], "subject": "Subject 1", "preview": "Preview 1",
         "web_link": "https://outlook.live.com/1"},
        {"id": "graph-2", "received_at": "2026-10-17T10:00:00", "from_name": "Ana", "from_address": "ana@acme.com",
         "to": [], "subject": "Subject 2", "preview": "Preview 2", "web_link": "https://outlook.live.com/2"},
    ]


def test_search_with_a_query_uses_graph_search_in_the_given_folder(http):
    url = "https://graph.microsoft.com/v1.0/me/mailFolders/sentitems/messages"
    calls = graph_calls(http, responses.GET, url, (200, {"value": [chat_message(1)]}))

    found = outlook.search("at-1", 'from:ana "offer letter"', days=3, limit=5, folder="sentitems")

    (_, query, _), = calls
    date = f"{datetime.now(timezone.utc) - timedelta(days=3):%Y-%m-%d}"
    assert query["$search"] == [f'"from:ana \\"offer letter\\" received>={date}"']  # inner quotes escaped
    assert query["$top"] == ["5"] and query["$select"] == [CHAT_SELECT]
    assert "$orderby" not in query and "$filter" not in query  # Graph rejects them next to $search
    assert [m["id"] for m in found] == ["graph-1"]


def test_search_without_days_has_no_filter(http):
    calls = graph_calls(http, responses.GET, INBOX, (200, {"value": []}))

    assert outlook.search("at-1") == []

    (_, query, _), = calls
    assert "$filter" not in query and query["$top"] == ["20"]


def test_read_returns_the_whole_body_as_text(http, belgrade):
    calls = graph_calls(http, responses.GET, f"{MESSAGES}/graph-1",
                        (200, chat_message(1, body={"contentType": "text", "content": "Hi,\n\nall good.\n"})))

    m = outlook.read("at-1", "graph-1")

    (headers, query, _), = calls
    assert headers["Prefer"] == 'outlook.body-content-type="text"'
    assert query["$select"] == [f"{CHAT_SELECT},body"]
    assert (m["id"], m["received_at"], m["to"], m["body"]) == (
        "graph-1", "2026-10-18T11:12:00", ["me@example.com", "boss@example.com"], "Hi,\n\nall good.\n")


def test_create_draft_posts_a_text_draft(http):
    calls = graph_calls(http, responses.POST, MESSAGES, (201, {
        "id": "draft-1", "subject": "Hello", "webLink": "https://outlook.live.com/draft-1", "isDraft": True,
        "toRecipients": [{"emailAddress": {"address": "Ana@acme.com"}}]}))

    draft = outlook.create_draft("at-1", to=["Ana@acme.com", "bob@acme.com"], subject="Hello", body="Hi Ana,\n\nThanks.")

    (headers, _, posted), = calls
    assert headers["Authorization"] == "Bearer at-1"
    assert posted == {"subject": "Hello", "body": {"contentType": "Text", "content": "Hi Ana,\n\nThanks."},
                      "toRecipients": [{"emailAddress": {"address": "Ana@acme.com"}},
                                       {"emailAddress": {"address": "bob@acme.com"}}]}
    assert draft == {"id": "draft-1", "web_link": "https://outlook.live.com/draft-1", "subject": "Hello",
                     "to": ["ana@acme.com"]}


def reply_draft(content_type, content):
    return {"id": "draft-9", "subject": "Re: Subject 1", "webLink": "https://outlook.live.com/draft-9",
            "toRecipients": [{"emailAddress": {"name": "Ana", "address": "Ana@Acme.com"}}],
            "body": {"contentType": content_type, "content": content}}


def test_create_reply_draft_puts_the_reply_above_the_quoted_html_original(http):
    original = '<html><body><div id="divRplyFwdMsg">From: Ana<br>Sent: ...</div>Original text</body></html>'
    created = graph_calls(http, responses.POST, f"{MESSAGES}/graph-1/createReply", (201, reply_draft("html", original)))
    patched = graph_calls(http, responses.PATCH, f"{MESSAGES}/draft-9", (200, reply_draft("html", "...")))

    draft = outlook.create_reply_draft("at-1", "graph-1", "Hi Ana,\n\nTuesday at 3 <works> & suits me.")

    (_, _, posted), = created
    assert posted == {}
    (headers, _, body), = patched
    assert headers["Authorization"] == "Bearer at-1"
    assert body == {"body": {"contentType": "HTML", "content":
                             "<div>Hi Ana,<br><br>Tuesday at 3 &lt;works&gt; &amp; suits me.</div><br>" + original}}
    assert draft == {"id": "draft-9", "web_link": "https://outlook.live.com/draft-9", "subject": "Re: Subject 1",
                     "to": ["ana@acme.com"]}


def test_create_reply_draft_keeps_a_text_original_as_text(http):
    graph_calls(http, responses.POST, f"{MESSAGES}/graph-1/createReply",
                (201, reply_draft("text", "\n\nFrom: Ana\n\nOriginal text")))
    patched = graph_calls(http, responses.PATCH, f"{MESSAGES}/draft-9", (200, {}))

    outlook.create_reply_draft("at-1", "graph-1", "Hi Ana,\n\nTuesday.")

    (_, _, body), = patched
    assert body == {"body": {"contentType": "Text", "content": "Hi Ana,\n\nTuesday.\n\n\n\nFrom: Ana\n\nOriginal text"}}


def test_chat_requests_report_errors_without_the_token(http):
    http.get(INBOX, status=401, json={"error": {"code": "InvalidAuthenticationToken"}})
    with pytest.raises(outlook.AuthError, match="outlook-login"):
        outlook.search("secret-token")

    http.get(f"{MESSAGES}/gone", status=404,
             json={"error": {"code": "ErrorItemNotFound", "message": "The specified object was not found in the store."}})
    with pytest.raises(RuntimeError, match="^Outlook: HTTP 404 The specified object was not found in the store.$"):
        outlook.read("secret-token", "gone")

    http.post(MESSAGES, body=requests.ConnectionError("secret-token leaked?"))
    with pytest.raises(RuntimeError, match="^Outlook request failed: ConnectionError$") as info:
        outlook.create_draft("secret-token", to=["a@b.c"], subject="s", body="b")
    assert "secret-token" not in str(info.value) and info.value.__cause__ is None
