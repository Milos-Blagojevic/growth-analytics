"""Drives a provider's browser sign-in (oauth.browser_code) in a thread and plays the browser's redirect."""

import threading
import urllib.request
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse


class Login:
    """Runs `login(path, show=..., open_browser=..., **kwargs)` in a thread and hands the test the URL it showed."""

    def __init__(self, login, path, **kwargs):
        self.shown, self.opened, self.error = [], [], None
        self.ready = threading.Event()

        def show(text):
            self.shown.append(text)
            self.ready.set()

        def run():
            try:
                login(path, show=show, open_browser=self.opened.append, **kwargs)
            except Exception as exc:  # noqa: BLE001 (re-raised by the test)
                self.error = exc

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
        assert self.ready.wait(5), "login never showed a URL"
        self.url = self.shown[0].split("\n")[-1]
        self.query = {k: v[0] for k, v in parse_qs(urlparse(self.url).query).items()}
        self.redirect = self.query["redirect_uri"]

    def visit(self, **params):
        """The browser landing on the loopback server after the provider's redirect: returns (status, body).
        The server listens on 127.0.0.1 whatever host name the redirect URI carries (urllib resolves localhost)."""
        url = self.redirect + "/?" + "&".join(f"{k}={v}" for k, v in params.items())
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                return r.status, r.read().decode()
        except HTTPError as exc:
            return exc.code, exc.read().decode()

    def finish(self):
        self.thread.join(5)
        assert not self.thread.is_alive(), "login is still running"
        if self.error:
            raise self.error
