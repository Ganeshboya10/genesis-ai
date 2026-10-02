import os, sys
from pathlib import Path
os.environ["LLM_MODE"] = "echo"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pytest
from fastapi.testclient import TestClient
import auth
import config
from main import app

USERS = ["kwilliams@coolcomforts.co", "iwilliams@coolcomforts.co"]


@pytest.fixture
def locked(monkeypatch):
    monkeypatch.setattr(config, "APP_PASSWORD", "s3cret")
    monkeypatch.setattr(config, "APP_USERNAMES", USERS)
    monkeypatch.setattr(auth.time, "sleep", lambda s: None)
    auth._failures.clear()
    return TestClient(app)


def login(client, user, pw):
    return client.post("/api/login", json={"username": user, "password": pw})


def test_open_when_no_password(monkeypatch):
    monkeypatch.setattr(config, "APP_PASSWORD", None)
    c = TestClient(app)
    assert c.get("/health").status_code == 200
    assert c.get("/login", follow_redirects=False).headers["location"] == "/"


def test_signed_out_is_redirected_or_rejected(locked):
    assert locked.get("/", follow_redirects=False).headers["location"] == "/login"
    assert locked.get("/login").status_code == 200
    r = locked.post("/api/chat", json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 401


def test_wrong_password(locked):
    r = login(locked, USERS[0], "nope")
    assert r.status_code == 401 and "don't match" in r.json()["detail"]
    assert auth.COOKIE_NAME not in locked.cookies


def test_both_users_can_sign_in_case_insensitive(locked):
    for user in ["KWilliams@CoolComforts.co", USERS[1]]:
        c = TestClient(app)
        assert login(c, user, "s3cret").status_code == 200
        assert c.get("/health").status_code == 200
        assert c.get("/", follow_redirects=False).status_code == 200


def test_unknown_user_rejected(locked):
    assert login(locked, "someone@coolcomforts.co", "s3cret").status_code == 401


def test_sign_out(locked):
    login(locked, USERS[0], "s3cret")
    locked.get("/logout", follow_redirects=False)
    assert locked.get("/", follow_redirects=False).headers["location"] == "/login"


def test_tampered_or_expired_cookie(locked, monkeypatch):
    assert auth.session_user("garbage") is None
    token = auth.make_session(USERS[0])
    assert auth.session_user(token) == USERS[0]
    monkeypatch.setattr(config, "APP_USERNAMES", [USERS[1]])  # user removed from .env
    assert auth.session_user(token) is None
    monkeypatch.setattr(config, "SESSION_HOURS", -1)
    assert auth.session_user(auth.make_session(USERS[1])) is None


def test_blocks_after_too_many_failures(locked):
    for _ in range(auth.MAX_FAILURES):
        login(locked, USERS[0], "nope")
    assert login(locked, USERS[0], "s3cret").status_code == 429
