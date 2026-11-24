"""Wasi — WhatsApp through Meta's Cloud API, as a MailProvider.

Wasi (wasi.sirahagents.com) is a WhatsApp CRM on Meta's official Cloud API.
Sellbird talks to its REST API, mapped in
build-docs/features/whatsapp-channel.md:

    POST /api/v1/messages                      send (text | template)
    GET  /api/v1/conversations                 threads, with last_inbound_at
    GET  /api/v1/conversations/{id}/messages   a thread's messages
    GET  /api/v1/account                       Meta's limit and quality rating
    GET  /api/v1/contacts                      opt-in / opt-out / blocked

It is a MailProvider because everything Sellbird does around sending — the
outbox ladder, the pre-send gate, the claim, stop-on-reply, suppressions — is
about reaching a person, not about email. Only the medium differs, so the
channel is an adapter rather than a second pipeline.

Two things Wasi does that email providers don't, and that the design leans on:

- It enforces Meta's 24-hour window itself. Free text to someone who hasn't
  messaged in 24 hours comes back `session_window_closed`, so Sellbird never
  has to track the window: it tries, and holds the row on that code.
- It reports Meta's live messaging limit and quality rating, so the daily cap
  is a real number rather than a guessed warmup curve.

No webhook: Sellbird runs on the user's machine, behind NAT, and cannot
receive one. Inbound is polled, exactly as Gmail history and IMAP are.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Sequence

import httpx

from sellbird.integrations.mail_provider import (
    InboundMessage,
    MailProvider,
    SendResult,
)

logger = logging.getLogger("sellbird.wasi")

DEFAULT_BASE_URL = "https://wasi.sirahagents.com"
API_PREFIX = "/api/v1"

MAX_RETRIES = 3
BACKOFF_BASE = 1.0
BACKOFF_CAP = 20.0
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=10.0)

# Wasi caps this, so asking for more is a validation error, not more rows.
MAX_PAGE = 100

# Wasi's own error codes that mean something specific to Sellbird.
WINDOW_CLOSED = "session_window_closed"
TEMPLATE_NOT_FOUND = "template_not_found"
# Meta error codes that mean "this number will never receive a message".
# 131009 is a malformed number; 131026 is "message undeliverable" (not a
# WhatsApp user). These are the WhatsApp equivalent of a hard bounce.
DEAD_NUMBER_META_CODES = {131009, 131026, 131047, 131051}
# Quality ratings Meta reports. Anything but GREEN means people are blocking
# or reporting the number, which is the warning before it is restricted.
GOOD_QUALITY = "GREEN"


class WasiError(Exception):
    """A Wasi call failed in a way the caller should see."""


class WasiResponseError(WasiError):
    """Wasi answered with {"error": {...}}: a decision, not a transport fault."""

    def __init__(self, error, status: int = 0):
        self.code = str((error or {}).get("code") or "error") if isinstance(error, dict) else "error"
        if isinstance(error, dict):
            detail = str(error.get("message") or "")
            details = error.get("details")
            if isinstance(details, list) and details:
                detail = f"{detail} ({'; '.join(str(d) for d in details[:3])})"
            meta = error.get("metaError")
            if isinstance(meta, dict):
                self.meta_code = meta.get("code")
                extra = (meta.get("error_data") or {}).get("details") if isinstance(
                    meta.get("error_data"), dict) else ""
                if extra:
                    detail = f"{detail} — {extra}"
            else:
                self.meta_code = None
        else:
            detail, self.meta_code = str(error), None
        self.detail = detail or self.code
        self.status = status
        super().__init__(f"{self.code}: {self.detail}")

    @property
    def window_closed(self) -> bool:
        return self.code == WINDOW_CLOSED

    @property
    def dead_number(self) -> bool:
        return self.meta_code in DEAD_NUMBER_META_CODES


class WasiProvider(MailProvider):
    """One WhatsApp sender (one Meta phone number), as Wasi exposes it."""

    name = "wasi"
    channel = "whatsapp"
    # WhatsApp has no headers. List-Unsubscribe and friends don't exist here;
    # Meta puts a Block button on every message instead.
    carries_headers = False

    def __init__(self, config, env):
        self.config = config
        self.env = env
        self.api_key = (getattr(env, "wasi_api_key", "") or "").strip()
        base = (getattr(env, "wasi_api_base_url", "") or "").strip() or DEFAULT_BASE_URL
        self.base_url = base.rstrip("/") + API_PREFIX
        # Wasi wants client_id in the send body although the key already
        # identifies the client. Read once from /account and kept.
        self._client_id: str = ""
        self._account: dict = {}
        # Conversation ids whose messages the last get_replies() returned, so
        # confirm_read() can move the cursor only once they are all handled.
        self._pending_cursor: str = ""
        self._cursor: str = ""

    # ── plumbing ──

    def is_configured(self) -> bool:
        return bool(self.api_key)

    def _redact(self, text: str) -> str:
        if self.api_key and text:
            return text.replace(self.api_key, "***")
        return text or ""

    @staticmethod
    def _retry_delay(attempt: int, response: httpx.Response | None) -> float:
        if response is not None:
            retry_after = response.headers.get("Retry-After")
            if retry_after:
                try:
                    return min(float(retry_after), BACKOFF_CAP) + random.uniform(0, 1)
                except ValueError:
                    pass
        return min(BACKOFF_BASE * (2 ** attempt), BACKOFF_CAP) * random.uniform(0.5, 1.5)

    async def _request(self, method: str, path: str, json: dict | None = None,
                       params: dict | None = None, retries: int = MAX_RETRIES):
        """An authenticated call. Returns the parsed body, or raises WasiError.

        A POST is never retried on a network error: Wasi may already have
        handed the message to Meta, and a retry would send it twice. Reads
        are retried freely.
        """
        if not self.api_key:
            raise WasiError("WASI_API_KEY is not set in .env.")
        url = self.base_url + path
        headers = {"Authorization": f"Bearer {self.api_key}",
                   "Content-Type": "application/json"}
        safe_to_retry = method.upper() == "GET"
        last = ""
        async with httpx.AsyncClient(timeout=TIMEOUT, trust_env=False) as client:
            for attempt in range(retries + 1):
                if attempt:
                    await asyncio.sleep(self._retry_delay(attempt, None))
                try:
                    response = await client.request(method, url, headers=headers,
                                                    json=json, params=params)
                except httpx.HTTPError as e:
                    last = f"{type(e).__name__}: {self._redact(str(e))}"
                    if not safe_to_retry or attempt == retries:
                        raise WasiError(last) from e
                    continue
                if response.status_code in RETRYABLE_STATUS and safe_to_retry:
                    last = f"HTTP {response.status_code}"
                    if attempt < retries:
                        await asyncio.sleep(self._retry_delay(attempt, response))
                        continue
                try:
                    body = response.json()
                except ValueError:
                    raise WasiError(
                        f"Wasi returned HTTP {response.status_code} with a body that "
                        f"isn't JSON.") from None
                if isinstance(body, dict) and body.get("error"):
                    raise WasiResponseError(body["error"], response.status_code)
                if response.status_code >= 400:
                    raise WasiError(f"Wasi returned HTTP {response.status_code}.")
                return body
        raise WasiError(last or "Wasi could not be reached.")

    # ── account ──

    async def account(self, refresh: bool = False) -> dict:
        """GET /account: whether the number is connected, Meta's quality
        rating for it, and how much of Meta's 24-hour allowance is left."""
        if self._account and not refresh:
            return self._account
        body = await self._request("GET", "/account")
        self._account = body if isinstance(body, dict) else {}
        self._client_id = str(self._account.get("client_id") or "")
        return self._account

    async def client_id(self) -> str:
        if not self._client_id:
            await self.account()
        if not self._client_id:
            raise WasiError("Wasi did not report a client_id for this account.")
        return self._client_id

    async def remaining_today(self) -> int | None:
        """What Meta still allows in the next 24 hours, or None when Wasi
        doesn't say (then only Sellbird's own cap applies)."""
        limit = (await self.account(refresh=True)).get("messaging_limit") or {}
        if limit.get("unlimited"):
            return None
        remaining = limit.get("remaining")
        return int(remaining) if isinstance(remaining, (int, float)) else None

    async def quality_problem(self) -> str:
        """Why cold sending should stop, or "". A rating that is not GREEN
        means people are blocking the number: the last warning before Meta
        restricts it."""
        account = await self.account(refresh=True)
        if not account.get("connected", True):
            return f"the WhatsApp number is not connected (status {account.get('status')!r})"
        rating = str(account.get("quality_rating") or "").upper()
        if rating and rating != GOOD_QUALITY:
            return (f"Meta's quality rating for this number is {rating}, not "
                    f"{GOOD_QUALITY} — people are blocking or reporting it")
        return ""

    async def test_connection(self) -> tuple[bool, str]:
        try:
            account = await self.account(refresh=True)
        except WasiResponseError as e:
            return False, e.detail
        except WasiError as e:
            return False, str(e)
        limit = account.get("messaging_limit") or {}
        parts = [f"connected as {account.get('display_name') or '?'}",
                 f"quality {account.get('quality_rating') or '?'}"]
        if limit.get("unlimited"):
            parts.append("no 24h limit")
        elif limit.get("remaining") is not None:
            parts.append(f"{limit['remaining']} of {limit.get('limit_per_24h')} left in 24h")
        return bool(account.get("connected")), ", ".join(parts)

    # ── templates ──

    async def templates(self) -> list[dict]:
        """Every template Meta has approved for this number."""
        body = await self._request("GET", "/templates")
        return [t for t in (body or []) if isinstance(t, dict)]

    # ── sending ──

    async def send_email(
        self,
        to_email: str,
        subject: str,
        body: str,
        thread_ref: str = "",
        in_reply_to: str = "",
        headers: Sequence[tuple[str, str]] = (),
        template: str = "",
        params: dict | None = None,
    ) -> SendResult:
        """Send one WhatsApp message.

        Keeps the MailProvider signature so the Sender's drain loop is the
        same loop for both channels. ``to_email`` is an E.164 number and
        ``subject`` is unused (WhatsApp has no subject line).

        ``template`` names a Meta-approved template and ``params`` fills its
        placeholders: that is the only form a first message may take. Without
        a template this is a free-text message, which Meta allows only inside
        24 hours of the person's own last message — Wasi enforces that and
        says ``session_window_closed`` when it has passed.
        """
        payload: dict = {"to": to_email}
        if template:
            payload.update(type="template", template=template, params=params or {})
        else:
            payload.update(type="text", body=body)
        try:
            payload["client_id"] = await self.client_id()
            sent = await self._request("POST", "/messages", json=payload)
        except WasiResponseError as e:
            # A refusal Wasi made itself: nothing was handed to Meta, so the
            # message did not go out and the row is safe to hold or fail.
            return SendResult(ok=False, error=f"{e.code}: {e.detail}"[:300],
                              uncertain=False)
        except WasiError as e:
            # The request may have arrived: never retried, never resent.
            return SendResult(ok=False, error=self._redact(str(e))[:300], uncertain=True)
        if not isinstance(sent, dict):
            return SendResult(ok=False, error="Wasi returned no message record.",
                              uncertain=True)
        return SendResult(
            ok=True,
            message_id=str(sent.get("meta_message_id") or sent.get("id") or ""),
            thread_ref=str(sent.get("chat_id") or ""),
        )

    # ── inbound ──

    async def get_replies(self, limit: int = 50) -> list[InboundMessage]:
        """Inbound WhatsApp messages since the last confirmed read.

        Two steps, because Wasi has no "messages since X" endpoint: list the
        threads (they carry ``last_inbound_at``), then read the messages of
        the ones that have heard from the person since the cursor. The cursor
        only moves when the Handler confirms it handled everything, so a
        message is read again rather than lost.
        """
        threads = await self._request(
            "GET", "/conversations", params={"limit": min(int(limit), MAX_PAGE)})
        rows = [t for t in (threads or []) if isinstance(t, dict)]
        if len(rows) >= MAX_PAGE:
            # Wasi ignores offset/since, so this is the whole page there is.
            logger.warning(
                "Wasi returned the maximum %s conversations: any thread with new "
                "inbound beyond that is not visible this cycle.", MAX_PAGE)
        fresh = [t for t in rows
                 if str(t.get("last_inbound_at") or "") > self._cursor]
        inbound: list[InboundMessage] = []
        newest = self._cursor
        for thread in sorted(fresh, key=lambda t: str(t.get("last_inbound_at") or "")):
            chat_id = str(thread.get("id") or "")
            number = str(thread.get("phone") or "")
            if not chat_id or not number:
                continue
            if not number.startswith("+"):
                # Wasi stores the wa_id, which is E.164 without the "+".
                number = "+" + number
            messages = await self._request("GET", f"/conversations/{chat_id}/messages")
            for message in (messages or []):
                if not isinstance(message, dict):
                    continue
                if str(message.get("direction") or "").lower() != "in":
                    continue
                sent_at = str(message.get("sent_at") or "")
                if sent_at <= self._cursor:
                    continue
                inbound.append(InboundMessage(
                    provider_id=str(message.get("id") or ""),
                    from_email=number,
                    subject="",
                    body=str(message.get("body") or ""),
                    message_id=str(message.get("meta_message_id") or ""),
                    thread_ref=chat_id,
                    date=sent_at,
                    # A failed outbound is how WhatsApp reports a dead number,
                    # and those are direction "out", so they never land here.
                    is_bounce=False,
                ))
                newest = max(newest, sent_at)
        self._pending_cursor = newest
        return inbound

    def confirm_read(self) -> None:
        """Everything the last get_replies returned was handled: move on."""
        if self._pending_cursor:
            self._cursor = self._pending_cursor
            self._pending_cursor = ""

    async def failed_sends(self, limit: int = MAX_PAGE) -> list[dict]:
        """Outbound messages Meta rejected, with their error codes.

        WhatsApp has no bounce message: a dead number shows up as an outbound
        row that failed. Returns the raw rows so the Handler can map a code to
        "this number will never work" and suppress it.
        """
        threads = await self._request(
            "GET", "/conversations", params={"limit": min(int(limit), MAX_PAGE)})
        failures: list[dict] = []
        for thread in (threads or []):
            if not isinstance(thread, dict) or not thread.get("id"):
                continue
            messages = await self._request(
                "GET", f"/conversations/{thread['id']}/messages")
            for message in (messages or []):
                if not isinstance(message, dict):
                    continue
                if str(message.get("status") or "").lower() != "failed":
                    continue
                failures.append({**message, "phone": thread.get("phone") or ""})
        return failures
