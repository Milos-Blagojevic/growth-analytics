"""The browser sign-in both mail providers use: an OAuth authorization-code flow with a loopback redirect.

The provider module builds the authorization URL; this module listens on a random port of 127.0.0.1, shows the URL
(and opens it), waits for the browser to come back with `code` and the right `state`, and returns the code for the
provider to exchange. Google does not allow Gmail scopes in the device-code flow and Microsoft's device-code consent
does not work for every account, so both sign in this way, as native apps are meant to (RFC 8252).
"""

import logging
import secrets
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger("oauth")

TIMEOUT = 900  # seconds to wait for the browser


class Declined(RuntimeError):
    """The browser came back with an error (the user declined, or the provider refused the request)."""


class _Redirect(BaseHTTPRequestHandler):
    """Catches the provider's redirect back to the loopback server: one GET / with `code` and `state`, or `error`."""

    def do_GET(self):  # noqa: N802 (http.server's name)
        query = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
        if urlparse(self.path).path != "/" or query.get("state") != self.server.state:
            self._reply(400, "Unexpected request; go back to the terminal and start the sign-in again.")
            return  # not our redirect: keep waiting
        if query.get("error"):
            self.server.error = f"{query['error']}: {query.get('error_description', '')}".rstrip(": ")[:300]
            self._reply(200, f"Sign-in failed ({self.server.error}); you can close this tab.")
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


def browser_code(build_url, *, host="127.0.0.1", show=print, open_browser=webbrowser.open, timeout=TIMEOUT):
    """Runs the browser part of the flow; returns (code, redirect_uri) for the token exchange.

    `build_url(redirect_uri, state)` returns the provider's authorization URL. `host` is the host name in the
    redirect URI (the server always listens on 127.0.0.1): Google wants the IP literal, Microsoft's portal only
    registers `http://localhost`. Raises Declined when the browser comes back with an error and TimeoutError
    when nothing comes back within `timeout` seconds.
    """
    with HTTPServer(("127.0.0.1", 0), _Redirect) as server:
        server.state, server.code, server.error = secrets.token_urlsafe(16), None, None
        server.timeout = 1  # handle_request returns every second so the deadline is checked
        redirect_uri = f"http://{host}:{server.server_port}"
        url = build_url(redirect_uri, server.state)
        show(f"Sign in in your browser (this window is listening on {redirect_uri}):\n{url}")
        try:
            open_browser(url)
        except Exception:  # noqa: BLE001 (no browser here: the URL is on screen)
            pass
        deadline = time.monotonic() + timeout
        while server.code is None and server.error is None:
            if time.monotonic() >= deadline:
                raise TimeoutError("sign-in timed out; start it again")
            server.handle_request()
        if server.error:
            raise Declined(server.error)
        return server.code, redirect_uri
