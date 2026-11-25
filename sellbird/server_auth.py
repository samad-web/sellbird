"""Sign-in for a hosted Sellbird: one operator, a password, a session cookie.

The local dashboard is reached only from the machine it runs on, so a token in
a link is enough. A hosted one is reachable by anyone who can resolve its
name, while holding mailbox OAuth tokens with Mail.Send, a Claude session, API
keys and every prospect's details. Everything here follows from that.

This file does **not** change ``dashboard_auth``. ``dashboard.build_app`` takes
a guard that stands in its place, so a local install behaves exactly as it did
and this is the only thing a hosted one adds.

What it keeps from the local guard, because it still applies:

- the browser-provenance checks (``Origin``, ``Sec-Fetch-Site``),
- the public-path allowance for the shell, which carries no data,
- the JSON-only rule for request bodies,
- the security headers on every response.

What it replaces:

- the loopback ``Host`` check becomes an exact match on the configured public
  host. Not "any host": a wrong Host is how rebinding and poisoned absolute
  links get in.
- the bearer token becomes a session cookie. A cookie is sent by the browser
  automatically where a bearer token was not, so **CSRF is a new risk here
  that the local guard never had**. Three independent answers, because one is
  not enough: ``SameSite=Strict``, the inherited provenance checks, and the
  inherited JSON-only rule for mutations.

Other decisions worth knowing before changing anything:

- Passwords are hashed with ``hashlib.scrypt`` (standard library, memory-hard).
  An unknown username still runs a full hash, so a wrong name and a wrong
  password cost the same time and give the same message.
- Session ids are stored hashed. A stolen copy of the database is then a list
  of useless rows rather than a set of live sessions.
- A new id is issued on every login and the old one deleted, so nobody can
  pre-set a cookie that later becomes authenticated.
- Failures are counted per account **and** per address, so one attacker can't
  lock the operator out by guessing from one address.
- It fails closed. No account, no HTTPS, unreadable database: refuse, never
  serve.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import stat
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from sellbird.dashboard_auth import (
    SECURITY_HEADERS,
    _is_json,
    _UNSAFE_METHODS,
    parse_host,
)

logger = logging.getLogger("sellbird.server_auth")

COOKIE_NAME = "sellbird_session"
ACCOUNT_FILE = "operator.json"

# scrypt: ~32 MB and ~100 ms per attempt on a small VPS. The cost is the point
# -- it is what makes a stolen hash expensive to attack offline.
SCRYPT_N = 1 << 15
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
SCRYPT_MAXMEM = 128 * 1024 * 1024
SALT_BYTES = 16

SESSION_ID_BYTES = 32
SESSION_HOURS = 12          # absolute: a session dies this long after login
SESSION_IDLE_HOURS = 2      # and sooner if untouched

MAX_FAILURES = 5
FAILURE_WINDOW_SECONDS = 15 * 60
LOCKOUT_SECONDS = 15 * 60

MIN_PASSWORD_CHARS = 12

# Said for every failed sign-in, whatever went wrong. A message that
# distinguishes "no such user" from "wrong password" hands an attacker the
# list of real usernames.
SIGNIN_FAILED = "That username and password don't match."


class AccountError(Exception):
    """Something the operator must fix, said in words they can act on."""


# ── The operator account ──────────────────────────────────────────────────

def account_path(data_dir: Path | str) -> Path:
    return Path(data_dir) / ACCOUNT_FILE


def hash_password(password: str, salt: bytes | None = None) -> dict:
    salt = secrets.token_bytes(SALT_BYTES) if salt is None else salt
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P,
        dklen=SCRYPT_DKLEN, maxmem=SCRYPT_MAXMEM)
    return {"algorithm": "scrypt", "n": SCRYPT_N, "r": SCRYPT_R, "p": SCRYPT_P,
            "salt": salt.hex(), "hash": digest.hex()}


def _hash_with(password: str, record: dict) -> bytes:
    """Hash using the parameters a stored record was made with, so an account
    written under older settings still verifies."""
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=bytes.fromhex(str(record.get("salt") or "")),
        n=int(record.get("n") or SCRYPT_N),
        r=int(record.get("r") or SCRYPT_R),
        p=int(record.get("p") or SCRYPT_P),
        dklen=SCRYPT_DKLEN, maxmem=SCRYPT_MAXMEM)


def write_account(data_dir: Path | str, username: str, password: str) -> Path:
    """Create or replace the single operator account. Owner-only."""
    username = (username or "").strip()
    if not username:
        raise AccountError("A username is needed.")
    if len(password or "") < MIN_PASSWORD_CHARS:
        raise AccountError(
            f"That password is {len(password or '')} characters. Use at least "
            f"{MIN_PASSWORD_CHARS}: this one is reachable from the internet.")
    path = account_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"username": username, "created_at": _now().isoformat(),
              **hash_password(password)}
    # Written to a private temporary file and moved into place, so the real
    # path is never a half-written or world-readable file even for an instant.
    temp = path.with_suffix(".json.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(temp, flags, stat.S_IRUSR | stat.S_IWUSR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(record, fh, indent=2)
    except Exception:
        temp.unlink(missing_ok=True)
        raise
    os.replace(temp, path)
    return path


def read_account(data_dir: Path | str) -> dict | None:
    """The stored account, or None when there isn't a usable one.

    A malformed file reads as None rather than raising: the server then
    refuses to start and says to set a password, which is the same outcome
    and a message the operator can act on.
    """
    path = account_path(data_dir)
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict):
        return None
    if not all(record.get(k) for k in ("username", "salt", "hash")):
        return None
    return record


def check_password(record: dict | None, username: str, password: str) -> bool:
    """Whether this is the operator. Always does the full work.

    An unknown username is hashed against a throwaway salt so that a wrong
    name and a wrong password take the same time: otherwise the response time
    is a list of which usernames exist.
    """
    if record is None:
        hashlib.scrypt(b"-", salt=b"-" * SALT_BYTES, n=SCRYPT_N, r=SCRYPT_R,
                       p=SCRYPT_P, dklen=SCRYPT_DKLEN, maxmem=SCRYPT_MAXMEM)
        return False
    try:
        computed = _hash_with(password or "", record)
        stored = bytes.fromhex(str(record.get("hash") or ""))
    except ValueError:
        return False
    name_ok = hmac.compare_digest((username or "").strip().encode("utf-8"),
                                  str(record.get("username") or "").encode("utf-8"))
    hash_ok = hmac.compare_digest(computed, stored)
    return name_ok and hash_ok


# ── Sessions ──────────────────────────────────────────────────────────────

def new_session_id() -> str:
    return secrets.token_urlsafe(SESSION_ID_BYTES)


def session_hash(session_id: str) -> str:
    """What goes in the database. The id itself never does."""
    return hashlib.sha256((session_id or "").encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def session_deadlines(hours: int = SESSION_HOURS,
                      idle_hours: int = SESSION_IDLE_HOURS) -> tuple[str, str]:
    """(expires_at, idle_cutoff) as the database stores them."""
    now = _now()
    return ((now + timedelta(hours=hours)).isoformat(),
            (now - timedelta(hours=idle_hours)).isoformat())


def cookie_header(session_id: str, max_age: int) -> str:
    """The Set-Cookie value. Secure, so it only ever travels over HTTPS;
    HttpOnly, so no script can read it; Strict, so another site's request
    never carries it -- the main defence against cross-site requests."""
    return (f"{COOKIE_NAME}={session_id}; Path=/; HttpOnly; Secure; "
            f"SameSite=Strict; Max-Age={max_age}")


CLEAR_COOKIE = (f"{COOKIE_NAME}=; Path=/; HttpOnly; Secure; SameSite=Strict; "
                "Max-Age=0")


def read_cookie(header_values: list[str]) -> str:
    """The session id out of a Cookie header, or ""."""
    for header in header_values:
        for part in header.split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE_NAME and value:
                return value
    return ""


# ── Throttling ────────────────────────────────────────────────────────────

@dataclass
class Throttle:
    """Consecutive failures, counted per key, in memory.

    Per account *and* per address: counting only the account lets anyone lock
    the operator out, and counting only the address lets a botnet spread its
    guesses. One process, so a plain dict; a restart forgives, which is the
    right trade for a single-operator server.
    """

    max_failures: int = MAX_FAILURES
    window: int = FAILURE_WINDOW_SECONDS
    lockout: int = LOCKOUT_SECONDS
    _failures: dict = field(default_factory=dict)

    def _recent(self, key: str, now: float) -> list:
        return [t for t in self._failures.get(key, []) if now - t < self.window]

    def locked_for(self, key: str, now: float | None = None) -> int:
        """Seconds left on the lockout, 0 when not locked."""
        now = time.monotonic() if now is None else now
        recent = self._recent(key, now)
        if len(recent) < self.max_failures:
            return 0
        return max(0, int(self.lockout - (now - max(recent))) + 1)

    def record_failure(self, key: str, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        self._failures[key] = self._recent(key, now) + [now]

    def clear(self, key: str) -> None:
        self._failures.pop(key, None)


# ── The guard ─────────────────────────────────────────────────────────────

def public_url_host(url: str) -> str:
    """The host a public URL serves, checked. Raises AccountError otherwise.

    HTTPS is required, not preferred: the password crosses this connection,
    and a Secure cookie would never come back over plain HTTP, so an http://
    deployment would loop at the login screen while leaking the password.
    """
    parts = urlsplit((url or "").strip())
    if parts.scheme != "https":
        raise AccountError(
            f"The public URL must start with https:// (got {url!r}). The "
            "password crosses this connection, and the session cookie is "
            "only ever sent over HTTPS.")
    host = parse_host(parts.netloc)
    if not host:
        raise AccountError(f"Couldn't read a hostname from {url!r}.")
    return parts.netloc.lower()


def is_public_path(method: str, path: str) -> bool:
    """Paths served before anyone signs in: the shell, the login page, and
    the sign-in call itself. None of them carries data."""
    if method == "POST" and path == "/api/login":
        return True
    if method not in ("GET", "HEAD"):
        return False
    if path in ("/", "/login", "/api/instance"):
        return True
    if not path.startswith("/static/"):
        return False
    return all(seg not in ("", ".", "..") for seg in path[len("/static/"):].split("/"))


class SessionGuard:
    """Every request to a hosted Sellbird passes through this.

    Order matters and mirrors the local guard: Host, then provenance, then
    the public allowance, then identity, then the body rule.
    """

    def __init__(self, app, *, host: str, state, data_dir: Path | str,
                 throttle: Throttle | None = None):
        self.app = app
        self.host = (host or "").lower()
        self.state = state
        self.data_dir = Path(data_dir)
        self.throttle = throttle or Throttle()

    def _allowed_host(self, host_header: str) -> bool:
        """The configured name, or loopback for the proxy's own health check."""
        value = host_header.lower()
        if value == self.host:
            return True
        host = parse_host(value)
        if host is None:
            return False
        from sellbird.dashboard_auth import is_loopback
        return is_loopback(host)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            return await self.app(scope, receive, send)
        if scope["type"] != "http":
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            return

        headers: dict[str, list[str]] = {}
        for k, v in scope.get("headers") or []:
            headers.setdefault(k.decode("latin-1").lower(), []).append(v.decode("latin-1"))

        async def send_secured(message):
            if message["type"] == "http.response.start":
                existing = [(k, v) for k, v in message.get("headers", [])
                            if k.lower() not in {h for h, _ in _HOSTED_HEADERS}]
                message = {**message, "headers": existing + _HOSTED_HEADERS}
            await send(message)

        async def deny(status: int, code: str, message: str, extra=()):
            body = json.dumps({"error": code, "message": message}).encode()
            await send_secured({"type": "http.response.start", "status": status,
                                "headers": [(b"content-type", b"application/json"),
                                            (b"content-length", str(len(body)).encode()),
                                            *extra]})
            await send_secured({"type": "http.response.body", "body": body})

        async def redirect(location: str):
            await send_secured({"type": "http.response.start", "status": 303,
                                "headers": [(b"location", location.encode()),
                                            (b"content-length", b"0")]})
            await send_secured({"type": "http.response.body", "body": b""})

        method = scope.get("method", "GET").upper()
        path = scope.get("path", "")

        # 1. Host, before anything else.
        hosts = headers.get("host", [])
        if len(hosts) != 1 or not self._allowed_host(hosts[0]):
            return await deny(400, "bad_host",
                              "This server doesn't answer to that address.")
        host_header = hosts[0].lower()

        # 2. Browser provenance. A top-level navigation carries no data, so it
        #    is the one thing allowed to arrive from somewhere else.
        navigation = (method in ("GET", "HEAD")
                      and headers.get("sec-fetch-mode", [""])[0] == "navigate"
                      and headers.get("sec-fetch-dest", [""])[0] == "document")
        origins = headers.get("origin", [])
        if origins and not navigation:
            if len(origins) != 1 or origins[0].lower() != f"https://{host_header}":
                return await deny(403, "cross_origin", "Cross-origin requests are refused.")
        site = headers.get("sec-fetch-site", [])
        if site and site[0].lower() not in ("same-origin", "none") and not navigation:
            return await deny(403, "cross_origin", "Cross-site requests are refused.")

        # 3. The shell and the sign-in call, before anyone has a session.
        if is_public_path(method, path):
            scope.setdefault("state", {})["guard"] = self
            return await self.app(scope, receive, send_secured)

        # 4. Identity. Fail closed: a database that can't be read is not a
        #    reason to serve somebody's mailbox.
        session_id = read_cookie(headers.get("cookie", []))
        session = None
        if session_id:
            _, idle_cutoff = session_deadlines()
            try:
                session = await self.state.read_session(session_hash(session_id), idle_cutoff)
            except Exception as e:
                logger.error(f"Couldn't read the session store: {e}")
                return await deny(503, "store_unavailable",
                                  "Sign-in is unavailable right now.")
        if session is None:
            if navigation:
                return await redirect("/login")
            # A code of its own, not the local guard's "unauthorized": the
            # page shows a token lock screen for that one, and here the right
            # answer is the sign-in form.
            return await deny(401, "signin_required", "Sign in to continue.")
        scope.setdefault("state", {})["principal"] = "user"
        scope["state"]["username"] = session.get("username", "")
        scope["state"]["guard"] = self

        # 5. Mutations carry JSON or nothing. With a cookie this is not
        #    housekeeping: it stops a cross-site form post, which needs no
        #    preflight, from reaching a route.
        if method in _UNSAFE_METHODS:
            length = headers.get("content-length", ["0"])[0].strip()
            has_body = bool(headers.get("transfer-encoding")) or length not in ("", "0")
            if has_body and not _is_json(headers.get("content-type", [""])[0]):
                return await deny(415, "unsupported_media_type",
                                  "Request bodies must be application/json.")

        return await self.app(scope, receive, send_secured)


# HSTS on top of the local headers: once a browser has seen this, it refuses
# to speak plain HTTP to the name again, which closes the downgrade window.
_HOSTED_HEADERS = SECURITY_HEADERS + [
    (b"strict-transport-security", b"max-age=31536000; includeSubDomains"),
]
