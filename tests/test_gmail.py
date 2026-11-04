import base64
import json
import re
import threading
import time
import urllib.request
from email import message_from_bytes, policy
from urllib.parse import parse_qs, urlparse

import pytest
import requests
import responses

import gmail
from browser_login import Login

MESSAGES = f"{gmail.API}/messages"
DRAFTS = f"{gmail.API}/drafts"


@pytest.fixture(autouse=True)
def client(monkeypatch):
    monkeypatch.setenv("GMAIL_CLIENT_ID", "app-123.apps.googleusercontent.com")
    monkeypatch.setenv("GMAIL_CLIENT_SECRET", "shh")


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


def b64url(text):
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")  # Gmail leaves the padding out


def message(n, headers, **extra):
    return {"id": f"msg-{n}", "threadId": f"thread-{n}", "internalDate": "1792314720000",  # 2026-10-18T09:12:00Z
            "snippet": f"Snippet {n}", "payload": {"headers": [{"name": k, "value": v} for k, v in headers.items()]},
            **extra}


def test_configured_needs_both_the_client_id_and_the_secret(monkeypatch):
    assert gmail.configured()
    monkeypatch.delenv("GMAIL_CLIENT_SECRET")
    assert not gmail.configured()


def test_login_catches_the_browser_redirect_and_keeps_only_the_refresh_token(http, tmp_path):
    token_requests = replies(http, responses.POST, gmail.TOKEN,
                             (200, {"access_token": "at-1", "refresh_token": "rt-1", "expires_in": 3599}))
    path = tmp_path / "gmail_token.json"
    login = Login(gmail.login, path)

    assert urlparse(login.url)._replace(query="").geturl() == gmail.AUTH
    assert login.query["client_id"] == "app-123.apps.googleusercontent.com"
    assert login.query["scope"] == gmail.SCOPES and "gmail.send" not in login.query["scope"]
    assert (login.query["response_type"], login.query["access_type"], login.query["prompt"]) == ("code", "offline", "consent")
    assert login.redirect.startswith("http://127.0.0.1:") and len(login.query["state"]) >= 16
    assert login.opened == [login.url]

    assert login.visit(code="x", state="wrong-state")[0] == 400  # not our redirect: keep waiting
    assert login.thread.is_alive() and token_requests == []
    status, page = login.visit(code="auth-code-1", state=login.query["state"])
    assert (status, page) == (200, "Signed in; you can close this tab.")
    login.finish()

    (sent,) = token_requests
    assert sent == {"grant_type": "authorization_code", "code": "auth-code-1", "redirect_uri": login.redirect,
                    "client_id": "app-123.apps.googleusercontent.com", "client_secret": "shh"}
    saved = json.loads(path.read_text())
    assert saved["refresh_token"] == "rt-1" and "access_token" not in saved
    assert path.stat().st_mode & 0o777 == 0o600


def test_login_stops_when_the_sign_in_is_declined(http, tmp_path):
    login = Login(gmail.login, tmp_path / "t.json")

    assert login.visit(error="access_denied", state=login.query["state"])[0] == 200
    with pytest.raises(gmail.AuthError, match="access_denied"):
        login.finish()
    assert not (tmp_path / "t.json").exists() and len(http.calls) == 0


def test_login_times_out_without_a_redirect(tmp_path):
    login = Login(gmail.login, tmp_path / "t.json", timeout=0)

    with pytest.raises(gmail.AuthError, match="timed out"):
        login.finish()


def test_login_reports_a_failed_code_exchange(http, tmp_path):
    replies(http, responses.POST, gmail.TOKEN, (400, {"error": "invalid_grant", "error_description": "Bad Request"}))
    login = Login(gmail.login, tmp_path / "t.json")
    login.visit(code="c", state=login.query["state"])

    with pytest.raises(gmail.AuthError, match=r"invalid_grant: Bad Request"):
        login.finish()
    assert not (tmp_path / "t.json").exists()


def test_login_ignores_a_browser_that_cannot_open(http, tmp_path):
    replies(http, responses.POST, gmail.TOKEN, (200, {"refresh_token": "rt-1"}))
    path = tmp_path / "t.json"
    shown, ready = [], threading.Event()

    def show(text):
        shown.append(text)
        ready.set()

    def broken(url):
        raise OSError("no browser")

    thread = threading.Thread(target=lambda: gmail.login(path, show=show, open_browser=broken), daemon=True)
    thread.start()
    assert ready.wait(5)
    query = {k: v[0] for k, v in parse_qs(urlparse(shown[0].split("\n")[-1]).query).items()}
    urllib.request.urlopen(f"{query['redirect_uri']}/?code=c&state={query['state']}", timeout=5).close()
    thread.join(5)

    assert json.loads(path.read_text())["refresh_token"] == "rt-1"


def test_saved_token_is_never_readable_by_others(tmp_path):
    path = tmp_path / "gmail_token.json"
    path.with_suffix(".json.tmp").write_text("{ half written")
    path.with_suffix(".json.tmp").chmod(0o644)

    gmail._save(path, {"refresh_token": "rt-1", "access_token": "at-1"})

    assert path.stat().st_mode & 0o777 == 0o600
    assert [p.name for p in tmp_path.iterdir()] == ["gmail_token.json"]  # no .tmp left behind
    assert json.loads(path.read_text()) == {"refresh_token": "rt-1", "saved_at": json.loads(path.read_text())["saved_at"]}


def test_access_token_refreshes_with_the_client_secret_and_keeps_the_file(http, tmp_path):
    path = tmp_path / "gmail_token.json"
    path.write_text(json.dumps({"refresh_token": "rt-1"}))
    sent = replies(http, responses.POST, gmail.TOKEN, (200, {"access_token": "at-2", "expires_in": 3599}))

    assert gmail.access_token(path) == "at-2"

    assert sent == [{"grant_type": "refresh_token", "client_id": "app-123.apps.googleusercontent.com",
                     "client_secret": "shh", "refresh_token": "rt-1"}]
    assert json.loads(path.read_text()) == {"refresh_token": "rt-1"}


def test_expired_or_missing_sign_in_asks_to_sign_in_again(http, tmp_path):
    with pytest.raises(gmail.AuthError, match="gmail-login"):
        gmail.access_token(tmp_path / "missing.json")
    assert len(http.calls) == 0

    path = tmp_path / "gmail_token.json"
    path.write_text(json.dumps({"refresh_token": "rt-old"}))
    replies(http, responses.POST, gmail.TOKEN,
            (400, {"error": "invalid_grant", "error_description": "Token has been expired or revoked.\nmore"}))
    with pytest.raises(gmail.AuthError, match=r"invalid_grant: Token has been expired or revoked\.\): run `python hunt.py gmail-login`"):
        gmail.access_token(path)
    assert json.loads(path.read_text())["refresh_token"] == "rt-old"


def test_search_lists_then_reads_each_message_into_the_contract_dict(http, belgrade):
    list_calls, metadata_calls = [], []

    def listing(request):
        list_calls.append(request)
        return 200, {}, json.dumps({"messages": [{"id": "msg-1", "threadId": "t"}, {"id": "msg-2", "threadId": "t"}]})

    def metadata(request):
        metadata_calls.append(request)
        n = request.url.split("/messages/")[1].split("?")[0][len("msg-"):]
        return 200, {}, json.dumps(message(n, {"From": "Ana Ilić <Ana@Acme.com>", "Subject": f"Subject {n}",
                                               "To": "Me <me@example.com>, Other@Example.com"},
                                           snippet="Interview &amp; offer &#39;soon&#39;"))

    http.add_callback(responses.GET, MESSAGES, callback=listing, content_type="application/json")
    http.add_callback(responses.GET, re.compile(rf"{re.escape(MESSAGES)}/msg-\d.*"), callback=metadata,
                      content_type="application/json")

    found = gmail.search("at-1", "interview", days=7, limit=10)

    (listed,) = list_calls
    assert listed.headers["Authorization"] == "Bearer at-1"
    assert parse_qs(urlparse(listed.url).query) == {"q": ["in:inbox interview newer_than:7d"], "maxResults": ["10"]}
    assert [urlparse(c.url).path.rsplit("/", 1)[1] for c in metadata_calls] == ["msg-1", "msg-2"]  # API order
    assert parse_qs(urlparse(metadata_calls[0].url).query) == {"format": ["metadata"],
                                                                "metadataHeaders": ["From", "To", "Subject"]}
    assert metadata_calls[0].headers["Authorization"] == "Bearer at-1"
    assert found[0] == {
        "id": "msg-1",
        "received_at": "2026-10-18T11:12:00",  # 09:12 UTC in Belgrade (CEST)
        "from_name": "Ana Ilić",
        "from_address": "ana@acme.com",
        "to": ["me@example.com", "other@example.com"],
        "subject": "Subject 1",
        "preview": "Interview & offer 'soon'",
        "web_link": "https://mail.google.com/mail/#all/msg-1",
    }
    assert [m["id"] for m in found] == ["msg-1", "msg-2"]


def test_search_defaults_to_newest_inbox_mail_and_caps_the_limit(http):
    seen = []

    def listing(request):
        seen.append(parse_qs(urlparse(request.url).query))
        return 200, {}, json.dumps({"resultSizeEstimate": 0})  # no `messages` key when nothing matches

    http.add_callback(responses.GET, MESSAGES, callback=listing, content_type="application/json")

    assert gmail.search("at-1") == []
    assert gmail.search("at-1", limit=500, folder="spam") == []

    assert seen == [{"q": ["in:inbox"], "maxResults": ["20"]}, {"q": ["in:spam"], "maxResults": ["50"]}]


def test_read_prefers_the_plain_text_part_of_a_multipart_message(http):
    seen = []

    def full(request):
        seen.append(request.url)
        return 200, {}, json.dumps(message(1, {"From": "a@b.c", "Subject": "S", "To": "me@x.y"}, payload={
            "mimeType": "multipart/mixed",
            "headers": [{"name": "From", "value": "a@b.c"}, {"name": "Subject", "value": "S"}, {"name": "To", "value": "me@x.y"}],
            "parts": [
                {"mimeType": "multipart/alternative", "body": {"size": 0}, "parts": [
                    {"mimeType": "text/plain", "body": {"data": b64url("Hello,\n\nsecond paragraph — ok?")}},
                    {"mimeType": "text/html", "body": {"data": b64url("<p>Hello</p>")}},
                ]},
                {"mimeType": "application/pdf", "filename": "cv.pdf", "body": {"attachmentId": "att-1", "size": 1234}},
            ]}))

    http.add_callback(responses.GET, f"{MESSAGES}/msg-1", callback=full, content_type="application/json")

    found = gmail.read("at-1", "msg-1")

    assert parse_qs(urlparse(seen[0]).query) == {"format": ["full"]}
    assert found["body"] == "Hello,\n\nsecond paragraph — ok?"
    assert found["id"] == "msg-1" and found["from_address"] == "a@b.c" and found["to"] == ["me@x.y"]
    assert "payload" not in found


def test_read_falls_back_to_html_stripped_to_text(http):
    page = ("<html><head><style>p { color: red }</style><script>alert(1)</script></head>"
            "<body><div>Hi <b>there</b>,</div><p>We&#39;d like to talk.<br>Soon &amp; often.</p>\n\n\n\n"
            "<p>Bye</p></body></html>")
    http.get(f"{MESSAGES}/msg-2", json=message(2, {"From": "a@b.c", "Subject": "S"},
                                               payload={"mimeType": "text/html", "headers": [{"name": "From", "value": "a@b.c"}],
                                                        "body": {"data": b64url(page)}}))

    assert gmail.read("at-1", "msg-2")["body"] == "Hi there,\nWe'd like to talk.\nSoon & often.\n\nBye"


def test_read_decodes_base64url_without_padding(http):
    data = base64.urlsafe_b64encode("ÿ~?>".encode()).decode()  # the alphabet's - and _ appear, padding is dropped
    assert data.endswith("=") and ("-" in data or "_" in data)
    http.get(f"{MESSAGES}/msg-3", json=message(3, {}, payload={"mimeType": "text/plain",
                                                              "body": {"data": data.rstrip("=")}}))

    assert gmail.read("at-1", "msg-3")["body"] == "ÿ~?>"


def posted_draft(http):
    """Registers the drafts endpoint; returns a list that receives the decoded JSON body of each POST."""
    seen = []

    def callback(request):
        seen.append(json.loads(request.body))
        return 200, {}, json.dumps({"id": "draft-1", "message": {"id": "m-9", "threadId": "thread-9"}})

    http.add_callback(responses.POST, DRAFTS, callback=callback, content_type="application/json")
    return seen


def mime(posted):
    raw = posted["message"]["raw"]
    return message_from_bytes(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)), policy=policy.default)


def test_create_draft_posts_a_plain_text_mime_message(http):
    posted = posted_draft(http)

    draft = gmail.create_draft("at-1", to=["ana@acme.com", "bo@acme.com"], subject="Hello Ana",
                               body="Hi Ana,\n\nthanks for the call.\n\nMiloš")

    assert draft == {"id": "draft-1", "web_link": "https://mail.google.com/mail/#drafts", "subject": "Hello Ana",
                     "to": ["ana@acme.com", "bo@acme.com"]}
    (body,) = posted
    assert set(body["message"]) == {"raw"}  # no thread: a new conversation
    msg = mime(body)
    assert (msg["To"], msg["Subject"]) == ("ana@acme.com, bo@acme.com", "Hello Ana")
    assert msg.get_content_type() == "text/plain"
    assert msg.get_content() == "Hi Ana,\n\nthanks for the call.\n\nMiloš\n"
    assert http.calls[0].request.headers["Authorization"] == "Bearer at-1"


def test_create_reply_draft_answers_the_reply_to_address_in_the_same_thread(http):
    metadata_urls = []

    def metadata(request):
        metadata_urls.append(request.url)
        return 200, {}, json.dumps(message(5, {"From": "Ana <ana@acme.com>", "Reply-To": "Recruiting <jobs@acme.com>",
                                               "Subject": "Interview", "Message-ID": "<orig@acme.com>",
                                               "References": "<first@acme.com> <second@acme.com>"}))

    http.add_callback(responses.GET, f"{MESSAGES}/msg-5", callback=metadata, content_type="application/json")
    posted = posted_draft(http)

    draft = gmail.create_reply_draft("at-1", "msg-5", "Tuesday works.\n\nMiloš")

    assert parse_qs(urlparse(metadata_urls[0]).query) == {
        "format": ["metadata"], "metadataHeaders": ["From", "Reply-To", "Subject", "Message-ID", "References"]}
    assert draft == {"id": "draft-1", "web_link": "https://mail.google.com/mail/#drafts", "subject": "Re: Interview",
                     "to": ["jobs@acme.com"]}
    (body,) = posted
    assert body["message"]["threadId"] == "thread-5"
    msg = mime(body)
    assert (msg["To"], msg["Subject"]) == ("jobs@acme.com", "Re: Interview")
    assert msg["In-Reply-To"] == "<orig@acme.com>"
    assert msg["References"] == "<first@acme.com> <second@acme.com> <orig@acme.com>"
    assert msg.get_content() == "Tuesday works.\n\nMiloš\n"


def test_create_reply_draft_uses_from_and_keeps_an_existing_re_prefix(http):
    http.get(f"{MESSAGES}/msg-6", json=message(6, {"From": "Ana <Ana@Acme.com>", "Subject": "RE: Interview",
                                                   "Message-ID": "<orig@acme.com>"}))
    posted = posted_draft(http)

    draft = gmail.create_reply_draft("at-1", "msg-6", "ok")

    assert (draft["subject"], draft["to"]) == ("RE: Interview", ["Ana@Acme.com"])
    msg = mime(posted[0])
    assert (msg["To"], msg["Subject"], msg["In-Reply-To"], msg["References"]) == (
        "Ana@Acme.com", "RE: Interview", "<orig@acme.com>", "<orig@acme.com>")


def test_refused_sign_in_raises_auth_error(http):
    http.get(MESSAGES, status=401, json={"error": {"code": 401, "message": "Invalid Credentials", "status": "UNAUTHENTICATED"}})

    with pytest.raises(gmail.AuthError, match="gmail-login"):
        gmail.search("at-1")


def test_other_http_errors_raise_runtime_error_with_the_message(http):
    http.get(f"{MESSAGES}/msg-1", status=500, json={"error": {"code": 500, "message": "Backend Error"}})
    http.post(DRAFTS, status=403, body="not json")

    with pytest.raises(RuntimeError, match=r"^Gmail: HTTP 500 Backend Error$"):
        gmail.read("at-1", "msg-1")
    with pytest.raises(RuntimeError, match=r"^Gmail: HTTP 403$"):
        gmail.create_draft("secret-token", to=["a@b.c"], subject="s", body="b")


def test_connection_errors_raise_runtime_error_without_the_token(http):
    http.get(MESSAGES, body=requests.ConnectionError("boom secret-token"))

    with pytest.raises(RuntimeError, match=r"^Gmail request failed: ConnectionError$"):
        gmail.search("secret-token")
