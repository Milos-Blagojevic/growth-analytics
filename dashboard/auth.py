"""Owner login for a dashboard reachable from other machines (DASHBOARD_PASSWORD, for example on Railway).

One password, no user accounts. A successful login sets a signed session cookie that expires after SESSION_DAYS.
The signing key is derived from the password with scrypt, so changing the password logs every session out, a
restart does not, and a stolen cookie does not make guessing the password cheap. Failed logins are throttled for
the whole process (not per client), which keeps online guessing slow at the cost of a short lockout for the owner
too while someone is guessing.
"""

import hashlib
import hmac
import secrets
import threading
import time
from collections import deque

COOKIE = "hunt_session"
SESSION_DAYS = 30
MIN_PASSWORD_LENGTH = 12
MAX_FAILURES = 10  # failed logins allowed per FAILURE_WINDOW seconds before every attempt is refused
FAILURE_WINDOW = 15 * 60
_SALT = b"job-hunt-dashboard-session"


class TooManyAttempts(Exception):
    """Too many failed logins recently; `retry_after` is in seconds."""

    def __init__(self, retry_after):
        super().__init__(retry_after)
        self.retry_after = retry_after


class Auth:
    def __init__(self, password, *, clock=time.time):
        if len(password) < MIN_PASSWORD_LENGTH:
            raise ValueError(f"DASHBOARD_PASSWORD must be at least {MIN_PASSWORD_LENGTH} characters")
        self._password = password.encode()
        self._key = hashlib.scrypt(self._password, salt=_SALT, n=2**14, r=8, p=1, dklen=32)
        self._clock = clock
        self._failures = deque()
        self._lock = threading.Lock()

    def login(self, attempt):
        """Returns a session token for the right password, None for a wrong one. Raises TooManyAttempts."""
        now = self._clock()
        with self._lock:
            while self._failures and self._failures[0] <= now - FAILURE_WINDOW:
                self._failures.popleft()
            if len(self._failures) >= MAX_FAILURES:
                raise TooManyAttempts(int(self._failures[0] + FAILURE_WINDOW - now) + 1)
            if not secrets.compare_digest(attempt.encode(), self._password):
                self._failures.append(now)
                return None
        expires = int(now) + SESSION_DAYS * 86400
        return f"{expires}.{self._sign(expires)}"

    def valid(self, token):
        expires, _, signature = (token or "").partition(".")
        if not expires.isdigit() or int(expires) < self._clock():
            return False
        return secrets.compare_digest(signature, self._sign(int(expires)))

    def _sign(self, expires):
        return hmac.new(self._key, str(expires).encode(), hashlib.sha256).hexdigest()
