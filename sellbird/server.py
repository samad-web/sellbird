"""The hosted server: the sign-in routes and the app that uses them.

``sellbird dashboard`` serves the same screens to one machine behind a token.
``sellbird serve`` serves them to a name on the internet behind a password,
and the difference between the two lives in ``sellbird/server_auth.py``.

Nothing here is reachable from a local dashboard: the routes are added only
to the app this module builds.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.middleware import Middleware

from sellbird.dashboard_auth import is_loopback, parse_host
from sellbird.server_auth import (
    SESSION_HOURS,
    SIGNIN_FAILED,
    AccountError,
    CLEAR_COOKIE,
    SessionGuard,
    check_password,
    cookie_header,
    new_session_id,
    public_url_host,
    read_account,
    read_cookie,
    session_deadlines,
    session_hash,
)

logger = logging.getLogger("sellbird.server")

router = APIRouter()


def client_ip(request: Request) -> str:
    """Who is asking, as well as it can be known.

    ``X-Forwarded-For`` is trusted only when the connection itself comes from
    loopback, because only the proxy on this machine can be relied on to set
    it. Trusting it from anywhere else would let an attacker spread guesses
    across invented addresses and never trip the per-address lockout.
    """
    peer = getattr(request.client, "host", "") or ""
    if peer and is_loopback(parse_host(peer) or peer):
        forwarded = request.headers.get("x-forwarded-for", "")
        first = forwarded.split(",")[0].strip()
        if first:
            return first[:64]
    return peer[:64]


def _login_page() -> str:
    path = Path(__file__).resolve().parent / "web" / "login.html"
    return path.read_text(encoding="utf-8")


@router.get("/login", response_class=HTMLResponse)
async def login_page():
    return HTMLResponse(_login_page(), headers={"cache-control": "no-store"})


@router.post("/api/login")
async def sign_in(request: Request):
    """Check a username and password, and start a session.

    Every failure answers the same way: one message, and the same work done,
    so neither the wording nor the timing says whether the username exists.
    """
    guard: SessionGuard = request.scope.get("state", {}).get("guard")
    if guard is None:                     # only reachable if wired up wrongly
        return JSONResponse({"error": "unavailable", "message": "Sign-in is unavailable."},
                            status_code=503)

    try:
        body = json.loads(await request.body() or b"{}")
    except ValueError:
        body = {}
    username = str((body or {}).get("username") or "").strip()
    password = str((body or {}).get("password") or "")
    ip = client_ip(request)

    # Both keys, checked before any work is done. An account lock alone lets
    # anyone lock the operator out; an address lock alone lets a botnet in.
    keys = [f"user:{username.lower()}", f"ip:{ip}"]
    for key in keys:
        waiting = guard.throttle.locked_for(key)
        if waiting:
            logger.warning(f"Sign-in refused: too many attempts ({key.split(':')[0]}), "
                           f"{waiting}s left. from={ip}")
            return JSONResponse(
                {"error": "too_many_attempts",
                 "message": f"Too many attempts. Try again in {waiting // 60 + 1} minute(s)."},
                status_code=429, headers={"retry-after": str(waiting)})

    account = read_account(guard.data_dir)
    if not check_password(account, username, password):
        for key in keys:
            guard.throttle.record_failure(key)
        logger.warning(f"Sign-in failed for {username!r} from {ip}.")
        return JSONResponse({"error": "signin_failed", "message": SIGNIN_FAILED},
                            status_code=401)

    for key in keys:
        guard.throttle.clear(key)

    # A new id every time, and the presented one destroyed: nobody can plant a
    # cookie and wait for it to become signed in.
    old = read_cookie(request.headers.getlist("cookie"))
    if old:
        await guard.state.delete_session(session_hash(old))
    session_id = new_session_id()
    expires_at, _ = session_deadlines()
    await guard.state.create_session(
        session_hash(session_id), account["username"], expires_at, ip)
    logger.info(f"Signed in: {account['username']} from {ip}.")
    return JSONResponse(
        {"ok": True},
        headers={"set-cookie": cookie_header(session_id, SESSION_HOURS * 3600)})


@router.post("/api/logout")
async def sign_out(request: Request):
    """End this session now, server-side, not just in the browser."""
    guard: SessionGuard = request.scope.get("state", {}).get("guard")
    session_id = read_cookie(request.headers.getlist("cookie"))
    if guard is not None and session_id:
        await guard.state.delete_session(session_hash(session_id))
    return JSONResponse({"ok": True}, headers={"set-cookie": CLEAR_COOKIE})


def build_server_app(*, public_url: str, state, data_dir: Path | str):
    """The hosted app: the dashboard's routes behind a password.

    Refuses to build without an operator account, so there is no window in
    which the server is up and open.
    """
    from sellbird.dashboard import build_app

    host = public_url_host(public_url)
    if read_account(data_dir) is None:
        raise AccountError(
            "No operator account yet. Create one with:\n"
            "    sellbird server set-password\n"
            "The server will not start without it.")
    guard = Middleware(SessionGuard, host=host, state=state, data_dir=Path(data_dir))
    return build_app(guard=guard, routers=[router])
