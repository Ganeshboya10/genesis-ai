"""Sign-in for Genesis AI: email + shared password on a branded login page.

POST /api/login {username, password} checks the details and sets a signed
session cookie; every other page and API call needs that cookie (main.py).
Login is only switched on when APP_PASSWORD is set in .env.
"""
import base64
import hashlib
import hmac
import secrets
import time

import config

COOKIE_NAME = "genesis_session"
MAX_FAILURES = 10           # wrong attempts per visitor...
FAILURE_WINDOW = 15 * 60    # ...within 15 minutes, before they're blocked

# Without SESSION_SECRET, everyone is signed out whenever the server restarts.
_SECRET = (config.SESSION_SECRET or secrets.token_hex(32)).encode()
_failures: dict[str, list[float]] = {}


class AuthError(Exception):
    def __init__(self, message: str, status: int = 401):
        super().__init__(message)
        self.status = status


def enabled() -> bool:
    return bool(config.APP_PASSWORD)


def check_login(username: str, password: str, client_id: str) -> str:
    """Return the signed-in user's email, or raise AuthError."""
    now = time.time()
    recent = [t for t in _failures.get(client_id, []) if now - t < FAILURE_WINDOW]
    if len(recent) >= MAX_FAILURES:
        raise AuthError("Too many attempts. Wait 15 minutes and try again.", 429)

    user = username.strip().lower()
    user_ok = any(secrets.compare_digest(user, u) for u in config.APP_USERNAMES)
    pw_ok = secrets.compare_digest(password, config.APP_PASSWORD or "")
    if not (user_ok and pw_ok):
        _failures[client_id] = recent + [now]
        time.sleep(0.5)  # slow down guessing
        raise AuthError("That email and password don't match.")
    _failures.pop(client_id, None)
    return user


def _sign(payload: str) -> str:
    return hmac.new(_SECRET, payload.encode(), hashlib.sha256).hexdigest()


def make_session(user: str) -> str:
    payload = f"{user}|{int(time.time()) + config.SESSION_HOURS * 3600}"
    return base64.urlsafe_b64encode(f"{payload}|{_sign(payload)}".encode()).decode()


def session_user(token: str | None) -> str | None:
    """The email in a valid, unexpired session cookie; otherwise None."""
    if not token:
        return None
    try:
        user, expires, sig = base64.urlsafe_b64decode(token.encode()).decode().rsplit("|", 2)
    except Exception:
        return None
    if not hmac.compare_digest(sig, _sign(f"{user}|{expires}")) or int(expires) < time.time():
        return None
    return user if user in config.APP_USERNAMES else None  # removed users lose access
