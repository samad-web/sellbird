"""Generic SMTP + IMAP mailbox provider.

Works with any real mailbox: AgentMail (their IMAP/SMTP relay), Fastmail,
a Google Workspace account with an app password, or self-hosted mail.

.env:
    SMTP_HOST=            IMAP_HOST=
    SMTP_PORT=587         IMAP_PORT=993
    SMTP_USERNAME=        IMAP_USERNAME=   (defaults to SMTP_USERNAME)
    SMTP_PASSWORD=        IMAP_PASSWORD=   (defaults to SMTP_PASSWORD)

Sends as the configured persona (name + email). IMAP polling fetches
UNSEEN inbox messages and marks them seen after retrieval.
"""

import asyncio
import email
import email.policy
import imaplib
import logging
from dataclasses import asdict, dataclass, field
from email.message import EmailMessage
from email.utils import formataddr, make_msgid, parseaddr

import aiosmtplib

from sellbird.integrations.mail_provider import (
    InboundMessage,
    MailIdentity,
    MailProvider,
    SendResult,
    identity_from_config,
    looks_like_bounce,
)
from sellbird.integrations.signature import SignatureError, load_signature, set_body

_AUTO_REPLY_HEADERS = ("Auto-Submitted", "X-Autoreply", "X-Autorespond", "X-Autoresponse",
                       "Precedence")
logger = logging.getLogger("sellbird.smtp")


# Refused or never connected: the server definitely has no copy. Anything else
# (a drop or timeout mid-conversation, an unknown error) might have come after
# DATA was accepted, so the outcome is uncertain.
_DEFINITE_SMTP_ERRORS = (
    aiosmtplib.SMTPConnectError, aiosmtplib.SMTPConnectTimeoutError,
    aiosmtplib.SMTPAuthenticationError, aiosmtplib.SMTPHeloError,
    aiosmtplib.SMTPNotSupported, aiosmtplib.SMTPRecipientsRefused,
    aiosmtplib.SMTPSenderRefused, aiosmtplib.SMTPDataError,
)


@dataclass
class SmtpSettings:
    """Another inbox's server settings, for a caller that keeps them itself.
    Store the password somewhere safe; only public() is fit for a database."""

    host: str
    username: str
    password: str = field(repr=False)
    port: int = 587
    imap_host: str = ""
    imap_port: int = 993

    def public(self) -> dict:
        data = asdict(self)
        data.pop("password")
        return data


class SmtpImapProvider(MailProvider):
    name = "smtp"
    carries_headers = True

    def __init__(self, config, env, *, identity: MailIdentity | None = None,
                 smtp: SmtpSettings | None = None):
        self.config = config
        # IMAP ids read but not yet flagged \Seen (see confirm_read).
        self._pending_seen: list[bytes] = []
        # Settable: a mailbox transport points it at the inbox it sends from.
        self.identity = identity or identity_from_config(config)
        if smtp is not None:  # another inbox: its own settings, not the .env's
            self.smtp_host, self.smtp_port = smtp.host, int(smtp.port or 587)
            self.smtp_user, self.smtp_pass = smtp.username, smtp.password
            self.imap_host = smtp.imap_host or smtp.host
            self.imap_port = int(smtp.imap_port or 993)
            self.imap_user, self.imap_pass = smtp.username, smtp.password
            return
        self.smtp_host = getattr(env, "smtp_host", "")
        self.smtp_port = int(getattr(env, "smtp_port", 587) or 587)
        self.smtp_user = getattr(env, "smtp_username", "")
        self.smtp_pass = getattr(env, "smtp_password", "")
        self.imap_host = getattr(env, "imap_host", "") or self.smtp_host
        self.imap_port = int(getattr(env, "imap_port", 993) or 993)
        self.imap_user = getattr(env, "imap_username", "") or self.smtp_user
        self.imap_pass = getattr(env, "imap_password", "") or self.smtp_pass

    def is_configured(self) -> bool:
        return bool(self.smtp_host and self.smtp_user and self.smtp_pass)

    async def send_email(
        self,
        to_email: str,
        subject: str,
        body: str,
        thread_ref: str = "",
        in_reply_to: str = "",
        headers=(),
    ) -> SendResult:
        try:
            sig = load_signature(self.config)
        except SignatureError as e:
            return SendResult(ok=False, error=f"signature: {e}"[:300])
        address = self.identity.address or self.smtp_user
        msg = EmailMessage()
        msg["To"] = to_email
        msg["From"] = formataddr((self.identity.display_name, address))
        msg["Subject"] = subject
        for name, value in headers:
            msg[name] = value
        message_id = make_msgid(domain=address.split("@")[-1])
        msg["Message-ID"] = message_id
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
            msg["References"] = in_reply_to
        set_body(msg, body, sig)

        try:
            await aiosmtplib.send(
                msg,
                hostname=self.smtp_host,
                port=self.smtp_port,
                username=self.smtp_user,
                password=self.smtp_pass,
                start_tls=(self.smtp_port == 587),
                use_tls=(self.smtp_port == 465),
                timeout=30,
            )
        except Exception as e:
            logger.error(f"SMTP send failed to {to_email}: {e}")
            return SendResult(ok=False, error=str(e)[:300],
                              uncertain=not isinstance(e, _DEFINITE_SMTP_ERRORS))
        return SendResult(ok=True, message_id=message_id, thread_ref=message_id)

    async def get_replies(self, limit: int = 50) -> list[InboundMessage]:
        """The unseen inbox, or raise, like Gmail and Graph: an unread reply
        can't have stopped its follow-ups, so a failed read holds sending."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._fetch_unseen, limit)

    def _fetch_unseen(self, limit: int) -> list[InboundMessage]:
        """Read the unseen inbox WITHOUT consuming it.

        BODY.PEEK[] rather than RFC822: a plain RFC822 fetch sets \\Seen as a
        side effect, which would make the *read* consume the message instead
        of handling it. The Handler relies on the opposite — a reply it could
        not handle (Claude down) is deliberately left unprocessed so the next
        cycle reads it again — and a message already flagged \\Seen never comes
        back from an UNSEEN search. It was lost, and its stop-on-reply with it.
        confirm_read() flags them once the Handler says it is done.
        """
        out: list[InboundMessage] = []
        read_ids: list[bytes] = []
        conn = imaplib.IMAP4_SSL(self.imap_host, self.imap_port)
        try:
            conn.login(self.imap_user, self.imap_pass)
            status, _ = conn.select("INBOX")
            if status != "OK":
                raise imaplib.IMAP4.error(f"couldn't open INBOX ({status})")
            status, data = conn.search(None, "UNSEEN")
            if status != "OK":
                raise imaplib.IMAP4.error(f"couldn't search the inbox ({status})")
            ids = data[0].split()[-limit:]
            for msg_id in ids:
                status, parts = conn.fetch(msg_id, "(BODY.PEEK[])")
                if status != "OK":
                    raise imaplib.IMAP4.error(f"couldn't read message {msg_id!r} ({status})")
                if not parts or not isinstance(parts[0], tuple):
                    continue  # expunged since the search
                parsed = self._parse_rfc822(parts[0][1], uid=msg_id)
                if parsed:
                    out.append(parsed)
                    read_ids.append(msg_id)
        finally:
            try:
                conn.logout()
            except Exception:
                pass
        self._pending_seen = read_ids
        return out

    def confirm_read(self) -> None:
        """Everything the last get_replies returned was handled: flag it read.

        A failure here is logged, not raised: the messages were handled, and
        the Handler dedups on provider_id, so the worst case is re-reading a
        message that is then skipped — never a lost reply.
        """
        ids, self._pending_seen = self._pending_seen, []
        if not ids:
            return
        try:
            conn = imaplib.IMAP4_SSL(self.imap_host, self.imap_port)
        except Exception as e:
            logger.warning(f"IMAP: couldn't mark {len(ids)} message(s) read: {e}")
            return
        try:
            conn.login(self.imap_user, self.imap_pass)
            conn.select("INBOX")
            conn.store(b",".join(ids), "+FLAGS", "\\Seen")
        except Exception as e:
            logger.warning(f"IMAP: couldn't mark {len(ids)} message(s) read: {e}")
        finally:
            try:
                conn.logout()
            except Exception:
                pass

    @staticmethod
    def _parse_rfc822(raw: bytes, uid: bytes = b"") -> InboundMessage | None:
        try:
            msg = email.message_from_bytes(raw, policy=email.policy.default)
        except Exception:
            return None
        from_email = parseaddr(str(msg.get("From", "")))[1].lower()
        if not from_email:
            return None
        subject = str(msg.get("Subject", ""))

        body = ""
        try:
            part = msg.get_body(preferencelist=("plain", "html"))
            if part is not None:
                body = part.get_content()
                if part.get_content_type() == "text/html":
                    import re
                    body = re.sub(r"<[^>]+>", " ", body)
        except Exception:
            pass

        message_id = str(msg.get("Message-ID", "")).strip()
        # Dedup key. Mail without a Message-ID is rare but real, and
        # "{sender}:{subject}" collided for two replies in one thread — the
        # second was dropped as already processed, and a person's second
        # message is usually the one that says yes. The mailbox UID is unique;
        # the date is the fallback when even that is missing.
        fallback = ":".join(filter(None, [
            from_email, subject, (uid or b"").decode("ascii", "replace"),
            str(msg.get("Date", "")).strip(),
        ]))
        return InboundMessage(
            provider_id=message_id or fallback,
            from_email=from_email,
            subject=subject,
            body=(body or "")[:5000],
            message_id=message_id,
            in_reply_to=str(msg.get("In-Reply-To", "")).strip(),
            thread_ref=str(msg.get("In-Reply-To", "")).strip(),
            date=str(msg.get("Date", "")),
            is_bounce=looks_like_bounce(from_email, subject),
            # Hosted H7: what tells an automatic reply from a person's.
            headers={name.lower(): str(msg.get(name, "")) for name in _AUTO_REPLY_HEADERS
                     if msg.get(name)},
        )

    async def test_connection(self) -> tuple[bool, str]:
        if not self.is_configured():
            return False, "SMTP_HOST / SMTP_USERNAME / SMTP_PASSWORD missing from .env"
        # SMTP handshake
        try:
            smtp = aiosmtplib.SMTP(
                hostname=self.smtp_host, port=self.smtp_port,
                start_tls=(self.smtp_port == 587),
                use_tls=(self.smtp_port == 465), timeout=15,
            )
            await smtp.connect()
            await smtp.login(self.smtp_user, self.smtp_pass)
            await smtp.quit()
        except Exception as e:
            return False, f"SMTP login failed: {str(e)[:200]}"
        # IMAP handshake
        loop = asyncio.get_event_loop()

        def _imap_check():
            conn = imaplib.IMAP4_SSL(self.imap_host, self.imap_port)
            try:
                conn.login(self.imap_user, self.imap_pass)
            finally:
                try:
                    conn.logout()
                except Exception:
                    pass

        try:
            await loop.run_in_executor(None, _imap_check)
        except Exception as e:
            return False, f"SMTP OK but IMAP login failed: {str(e)[:200]}"
        return True, f"SMTP + IMAP connected as {self.smtp_user}"
