"""Sign-in for a hosted Sellbird (build-docs/features/hosted-login.md).

One test per acceptance criterion, plus the things that would be quiet
failures: a session id stored in the clear, a cross-site post that works, a
message that says whether a username exists.

scrypt's cost is the point in production and a waste in tests, so the work
factor is turned down here. Everything else is the real code.
"""

import asyncio
import json
import os
import tempfile
import time

import pytest
from fastapi.testclient import TestClient

from sellbird import server_auth
from sellbird.server import build_server_app
from sellbird.server_auth import (
    AccountError,
    Throttle,
    check_password,
    public_url_host,
    read_account,
    session_hash,
    write_account,
)
from sellbird.state import StateManager

HOST = "sellbird.example.com"
URL = f"https://{HOST}"
USER = "operator"
PASSWORD = "a-long-enough-password"


@pytest.fixture(autouse=True)
def cheap_scrypt(monkeypatch):
    """The real algorithm, a work factor that doesn't make the suite crawl."""
    monkeypatch.setattr(server_auth, "SCRYPT_N", 1 << 10)


@pytest.fixture
def home():
    with tempfile.TemporaryDirectory() as tmpdir:
        data = os.path.join(tmpdir, "data")
        os.makedirs(data)
        state = StateManager(os.path.join(data, "t.db"))
        asyncio.get_event_loop_policy().new_event_loop().run_until_complete(state.init_db())
        yield {"data": data, "state": state}


@pytest.fixture
def client(home):
    write_account(home["data"], USER, PASSWORD)
    app = build_server_app(public_url=URL, state=home["state"], data_dir=home["data"])
    with TestClient(app, base_url=URL) as http:
        yield http


def _sign_in(http, username=USER, password=PASSWORD, **kwargs):
    return http.post("/api/login", json={"username": username, "password": password},
                     **kwargs)


# ── 1 & 2. no session, then a session ──

def test_no_session_reaches_no_data(client):
    """Criterion 1."""
    for path in ("/api/stats", "/api/prospects", "/api/outbox", "/api/settings"):
        r = client.get(path)
        assert r.status_code == 401, path
        assert r.json()["error"] == "signin_required"
        assert "prospect" not in r.text.lower()


def test_a_signed_in_session_gets_through(client):
    """Criterion 2."""
    assert _sign_in(client).status_code == 200
    r = client.get("/api/stats")
    assert r.status_code == 200
    assert isinstance(r.json(), dict)


def test_the_shell_and_login_page_need_no_session(client):
    """They carry no data, so they load before anyone signs in."""
    assert client.get("/login").status_code == 200
    assert "password" in client.get("/login").text
    assert client.get("/").status_code == 200


# ── 3. a wrong password and an unknown name are indistinguishable ──

def test_a_wrong_password_and_an_unknown_user_answer_identically(client):
    """Criterion 3: the wording must not be a list of real usernames."""
    wrong = _sign_in(client, password="not-the-password-at-all")
    unknown = _sign_in(client, username="someone-else", password=PASSWORD)
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()
    assert wrong.json()["message"] == server_auth.SIGNIN_FAILED


def test_an_unknown_account_still_does_the_work():
    """No account at all must not answer faster than a wrong password: the
    gap is itself an answer."""
    assert check_password(None, "whoever", "whatever") is False


def test_the_username_is_compared_too(home):
    write_account(home["data"], USER, PASSWORD)
    record = read_account(home["data"])
    assert check_password(record, USER, PASSWORD) is True
    assert check_password(record, "OTHER", PASSWORD) is False


# ── 4. lockout ──

def test_failures_lock_the_account_and_the_address(client):
    """Criterion 4."""
    for _ in range(server_auth.MAX_FAILURES):
        assert _sign_in(client, password="wrong-one-here").status_code == 401
    locked = _sign_in(client)           # the CORRECT password, during lockout
    assert locked.status_code == 429
    assert "retry-after" in {k.lower() for k in locked.headers}


def test_the_lockout_is_counted_per_key_not_globally():
    """Per account and per address, so one attacker can't lock the operator
    out from a single address, and a botnet can't spread guesses."""
    throttle = Throttle(max_failures=2, window=100, lockout=100)
    now = 1000.0
    throttle.record_failure("user:a", now)
    throttle.record_failure("user:a", now)
    assert throttle.locked_for("user:a", now) > 0
    assert throttle.locked_for("user:b", now) == 0
    assert throttle.locked_for("ip:1.2.3.4", now) == 0


def test_old_failures_fall_out_of_the_window():
    throttle = Throttle(max_failures=2, window=10, lockout=10)
    throttle.record_failure("user:a", 1000.0)
    throttle.record_failure("user:a", 1001.0)
    assert throttle.locked_for("user:a", 1002.0) > 0
    assert throttle.locked_for("user:a", 1100.0) == 0


def test_a_good_password_clears_the_count(client):
    for _ in range(server_auth.MAX_FAILURES - 1):
        _sign_in(client, password="wrong-one-here")
    assert _sign_in(client).status_code == 200
    for _ in range(server_auth.MAX_FAILURES - 1):
        assert _sign_in(client, password="wrong-one-here").status_code == 401


# ── 5 & 6. session lifecycle ──

def test_logging_in_replaces_the_session(client):
    """Criterion 5: no fixation. The id presented is destroyed."""
    assert _sign_in(client).status_code == 200
    first = client.cookies.get(server_auth.COOKIE_NAME)
    assert _sign_in(client).status_code == 200
    second = client.cookies.get(server_auth.COOKIE_NAME)
    assert first and second and first != second

    fresh = TestClient(client.app, base_url=URL)
    fresh.cookies.set(server_auth.COOKIE_NAME, first)
    assert fresh.get("/api/stats").status_code == 401


def test_logging_out_revokes_immediately(client):
    """Criterion 6: server-side, not just a cleared cookie."""
    _sign_in(client)
    taken = client.cookies.get(server_auth.COOKIE_NAME)
    assert client.get("/api/stats").status_code == 200
    assert client.post("/api/logout").status_code == 200

    fresh = TestClient(client.app, base_url=URL)
    fresh.cookies.set(server_auth.COOKIE_NAME, taken)
    assert fresh.get("/api/stats").status_code == 401


def test_an_expired_session_is_refused_and_deleted(client, home):
    """Criterion 7."""
    state = home["state"]
    sid = "expired-session-id"

    async def seed():
        await state.create_session(session_hash(sid), USER, "2000-01-01T00:00:00")

    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(seed())
    client.cookies.set(server_auth.COOKIE_NAME, sid)
    assert client.get("/api/stats").status_code == 401

    async def gone():
        _, idle = server_auth.session_deadlines()
        return await state.read_session(session_hash(sid), idle)

    assert asyncio.get_event_loop_policy().new_event_loop().run_until_complete(gone()) is None


def test_the_session_id_is_never_stored_in_the_clear(client, home):
    """Criterion 8: a stolen database copy is not a set of live sessions."""
    _sign_in(client)
    cookie = client.cookies.get(server_auth.COOKIE_NAME)

    async def rows():
        async with home["state"]._connect() as db:   # noqa: SLF001
            async with db.execute("SELECT id_hash FROM sessions") as cur:
                return [r[0] for r in await cur.fetchall()]

    stored = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(rows())
    assert stored and cookie not in stored
    assert session_hash(cookie) in stored


def test_a_cookie_set_by_hand_is_not_a_session(client):
    client.cookies.set(server_auth.COOKIE_NAME, "made-up-value")
    assert client.get("/api/stats").status_code == 401


# ── 9 & 10. the two things a cookie gives away ──

def test_a_cross_origin_post_with_a_valid_cookie_is_refused(client):
    """Criterion 9: CSRF. The cookie rides along automatically, so the
    request has to be refused on where it came from."""
    _sign_in(client)
    assert client.get("/api/stats").status_code == 200
    r = client.post("/api/sending/pause", json={},
                    headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    assert r.json()["error"] == "cross_origin"


def test_a_cross_site_fetch_with_a_valid_cookie_is_refused(client):
    _sign_in(client)
    r = client.get("/api/stats", headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403


def test_a_foreign_host_is_refused_before_anything_else(client):
    """Criterion 10: DNS rebinding and poisoned absolute links."""
    _sign_in(client)
    r = client.get("/api/stats", headers={"Host": "attacker.example"})
    assert r.status_code == 400
    assert r.json()["error"] == "bad_host"


def test_a_form_post_without_json_is_refused(client):
    """A cross-site form needs no preflight, so the body rule is a real
    defence here, not housekeeping."""
    _sign_in(client)
    r = client.post("/api/sending/pause", content="a=b",
                    headers={"content-type": "application/x-www-form-urlencoded"})
    assert r.status_code == 415


# ── the cookie itself ──

def test_the_cookie_is_httponly_secure_and_strict(client):
    r = _sign_in(client)
    cookie = r.headers["set-cookie"]
    assert "HttpOnly" in cookie
    assert "Secure" in cookie
    assert "SameSite=Strict" in cookie


def test_responses_carry_hsts_and_the_security_headers(client):
    r = client.get("/login")
    assert "strict-transport-security" in {k.lower() for k in r.headers}
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["x-content-type-options"] == "nosniff"


# ── 11. it refuses to start wrong ──

def test_a_non_https_url_is_refused(home):
    """Criterion 11: a Secure cookie would never come back, and the password
    would cross in the clear."""
    write_account(home["data"], USER, PASSWORD)
    for bad in ("http://sellbird.example.com", "sellbird.example.com", ""):
        with pytest.raises(AccountError):
            build_server_app(public_url=bad, state=home["state"], data_dir=home["data"])


def test_no_account_means_no_server(home):
    """Criterion 11: there is no window where it is up and open."""
    with pytest.raises(AccountError) as e:
        build_server_app(public_url=URL, state=home["state"], data_dir=home["data"])
    assert "set-password" in str(e.value)


def test_the_public_host_is_read_from_the_url():
    assert public_url_host("https://sellbird.example.com/") == "sellbird.example.com"
    assert public_url_host("https://sellbird.example.com:8443") == "sellbird.example.com:8443"


# ── 12. the password on disk ──

def test_the_password_is_not_stored_and_the_file_is_private(home):
    """Criterion 12."""
    path = write_account(home["data"], USER, PASSWORD)
    raw = path.read_text(encoding="utf-8")
    assert PASSWORD not in raw
    record = json.loads(raw)
    assert record["algorithm"] == "scrypt"
    assert record["username"] == USER
    assert len(record["salt"]) == server_auth.SALT_BYTES * 2
    if os.name != "nt":      # POSIX bits mean nothing on Windows
        assert oct(path.stat().st_mode)[-3:] == "600"


def test_the_same_password_hashes_differently_every_time(home):
    first = server_auth.hash_password(PASSWORD)
    second = server_auth.hash_password(PASSWORD)
    assert first["salt"] != second["salt"]
    assert first["hash"] != second["hash"]


def test_a_short_password_is_refused(home):
    with pytest.raises(AccountError) as e:
        write_account(home["data"], USER, "short")
    assert str(server_auth.MIN_PASSWORD_CHARS) in str(e.value)


def test_a_corrupt_account_file_is_no_account(home):
    write_account(home["data"], USER, PASSWORD)
    server_auth.account_path(home["data"]).write_text("{not json", encoding="utf-8")
    assert read_account(home["data"]) is None


# ── 13. the local dashboard is untouched ──

def test_the_local_dashboard_still_uses_its_token():
    """Criterion 13: this feature adds a guard, it doesn't change the old one."""
    import sellbird.dashboard as dash
    from sellbird import dashboard_auth

    assert any(m.cls is dashboard_auth.DashboardGuard for m in dash.app.user_middleware)
    with TestClient(dash.app, base_url="http://127.0.0.1:5555") as http:
        assert http.get("/api/stats").status_code == 401


def test_the_local_guard_still_refuses_a_public_host():
    import sellbird.dashboard as dash

    with TestClient(dash.app, base_url="https://sellbird.example.com") as http:
        assert http.get("/api/stats").status_code == 400


def test_a_session_cookie_never_survives_a_log_line():
    """It is as good as the password: whoever has it is signed in."""
    from sellbird.logsafety import redact

    line = "GET /api/stats cookie: sellbird_session=abcdefghijklmnop1234 host=x"
    cleaned = redact(line)
    assert "abcdefghijklmnop1234" not in cleaned
    assert "sellbird_session=***" in cleaned
