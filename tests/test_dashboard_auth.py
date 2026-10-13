import sys

import pytest
import uvicorn

from dashboard import __main__ as entry
from dashboard import auth
from dashboard.app import create_app
from dashboard_data import client as make_client

PASSWORD = "correct horse battery"


@pytest.fixture
def client(tmp_path):
    return make_client(create_app(tmp_path, password=PASSWORD))


def log_in(client, password=PASSWORD, next="/"):
    return client.post("/login", data={"password": password, "next": next})


def test_pages_and_actions_need_the_login(client):
    page = client.get("/runs?x=1", follow_redirects=False)
    assert (page.status_code, page.headers["location"]) == (303, "/login?next=/runs%3Fx%3D1")
    htmx = client.get("/inbox", headers={"HX-Request": "true"})
    assert (htmx.status_code, htmx.headers["HX-Redirect"]) == (401, "/login?next=/inbox")
    # A valid CSRF token is not enough: a POST without the session cookie starts nothing.
    assert client.post("/runs", data={"kind": "watch", "dry_run": "1"}).status_code == 401
    assert client.get("/static/app.css").status_code == 200
    assert 'type="password"' in client.get("/login").text


def test_login_page_works_behind_an_https_proxy(tmp_path):
    """Railway ends TLS at its proxy and forwards plain HTTP. Absolute http:// asset URLs on the https page are
    blocked as mixed content: htmx never loads and the browser submits the form natively instead."""
    app = create_app(tmp_path, allowed_hosts=("hunt-production.up.railway.app",), password=PASSWORD)
    page = make_client(app).get("/login", headers={"Host": "hunt-production.up.railway.app"}).text
    assert 'href="/static/app.css"' in page and 'src="/static/htmx.min.js"' in page
    # Without htmx the password must still never end up in the URL (and the server's access log).
    assert '<form class="login" method="post" action="/login"' in page


def test_login_sets_a_session_and_logout_ends_it(client):
    assert log_in(client, "wrong password!").status_code == 401
    assert client.get("/", follow_redirects=False).status_code == 303

    response = log_in(client, next="/leads")
    assert (response.status_code, response.headers["HX-Redirect"]) == (204, "/leads")
    assert "httponly" in response.headers["set-cookie"].lower()
    page = client.get("/")
    assert page.status_code == 200 and "Log out" in page.text

    assert client.post("/logout").headers["HX-Redirect"] == "/login"
    assert client.get("/", follow_redirects=False).status_code == 303


def test_login_only_redirects_within_the_site(client):
    for target in ("//evil.example/x", "https://evil.example", "/\\evil.example"):
        assert log_in(client, next=target).headers["HX-Redirect"] == "/"


def test_repeated_wrong_passwords_lock_everyone_out_for_a_while():
    now = [1_000_000.0]
    login = auth.Auth(PASSWORD, clock=lambda: now[0])
    for _ in range(auth.MAX_FAILURES):
        assert login.login("guess") is None
    with pytest.raises(auth.TooManyAttempts):
        login.login(PASSWORD)  # even the right password, until the window passes
    now[0] += auth.FAILURE_WINDOW
    assert login.valid(login.login(PASSWORD))


def test_sessions_expire_and_end_when_the_password_changes():
    now = [1_000_000.0]
    login = auth.Auth(PASSWORD, clock=lambda: now[0])
    token = login.login(PASSWORD)
    assert auth.Auth(PASSWORD, clock=lambda: now[0]).valid(token)  # survives a restart
    assert not auth.Auth(PASSWORD + "!", clock=lambda: now[0]).valid(token)
    assert not login.valid(token[:-1] + ("0" if token[-1] != "0" else "1"))
    now[0] += auth.SESSION_DAYS * 86400 + 1
    assert not login.valid(token)


def test_short_passwords_are_refused():
    with pytest.raises(ValueError, match="at least 12"):
        auth.Auth("hunter2")


def test_serving_beyond_loopback_needs_a_password(tmp_path, monkeypatch):
    served = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: served.append((app, kw)))
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    monkeypatch.delenv("HUNT_SCHEDULE", raising=False)
    monkeypatch.setattr(sys, "argv", ["dashboard", "--root", str(tmp_path), "--host", "0.0.0.0"])
    with pytest.raises(SystemExit):
        entry.main()
    assert not served

    # Railway: password and public domain from the service variables, port from PORT.
    monkeypatch.setenv("DASHBOARD_PASSWORD", PASSWORD)
    monkeypatch.setenv("RAILWAY_PUBLIC_DOMAIN", "hunt-production.up.railway.app")
    monkeypatch.setenv("PORT", "4321")
    entry.main()
    app, options = served[0]
    assert options == {"host": "0.0.0.0", "port": 4321}
    client = make_client(app)
    client.base_url = "https://hunt-production.up.railway.app"
    response = log_in(client)
    assert "secure" in response.headers["set-cookie"].lower()
    assert client.get("/").status_code == 200
    assert client.get("/", headers={"Host": "evil.example"}).status_code == 400


def test_a_bad_schedule_stops_startup(tmp_path, monkeypatch):
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: pytest.fail("served despite a bad schedule"))
    monkeypatch.setenv("HUNT_SCHEDULE", "0 7 * * * send")
    monkeypatch.setattr(sys, "argv", ["dashboard", "--root", str(tmp_path)])
    with pytest.raises(SystemExit):
        entry.main()
