"""Keep credentials out of Sellbird's log.

Some vendors take the API key in the query string (Hunter, Reoon,
ZeroBounce), and httpx logs every request URL at INFO. A log the dashboard
shows (data/sellbird.log) would put a key in a URL on screen. Two layers: httpx and httpcore only log warnings, and the
log file's handler masks any query value whose name looks like a secret.
"""

from __future__ import annotations

import logging
import re

# Loggers that print full request URLs at INFO/DEBUG.
NOISY_HTTP_LOGGERS = ("httpx", "httpcore")

# ?api_key=..., &apiKey=..., &token=..., &client_secret=..., &password=...
_SECRET_PARAM = re.compile(
    r"([?&][^=&\s#\"'<>]*(?:key|token|secret|password)[^=&\s#\"'<>]*=)[^&\s#\"'<>]+",
    re.IGNORECASE,
)

# Bearer tokens and vendor-prefixed keys, which travel in a header rather
# than a URL. httpx does not log headers, but an exception, a traceback or a
# well-meant debug line can still carry one, and a key in the log file is a
# key on the dashboard's screen.
_BEARER = re.compile(r"(Bearer\s+)[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE)
_VENDOR_KEY = re.compile(r"\b(wasi|sk|pk|key)_[A-Za-z0-9]{12,}\b", re.IGNORECASE)
# A hosted Sellbird's session cookie: whoever has it is signed in, so it is
# exactly as sensitive as the token above and reaches logs the same ways.
_SESSION_COOKIE = re.compile(r"(sellbird_session=)[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE)

MASK = "***"


def redact(text: str) -> str:
    """Mask secret-looking query parameters, bearer tokens, API keys and the
    hosted sign-in cookie."""
    text = _SECRET_PARAM.sub(lambda m: m.group(1) + MASK, text)
    text = _BEARER.sub(lambda m: m.group(1) + MASK, text)
    text = _SESSION_COOKIE.sub(lambda m: m.group(1) + MASK, text)
    return _VENDOR_KEY.sub(lambda m: m.group(1) + "_" + MASK, text)


def quiet_http_loggers() -> None:
    for name in NOISY_HTTP_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


class RedactSecrets(logging.Filter):
    """Rewrites each record so no secret query value reaches the handler."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True  # a broken record is logging's problem, not ours
        cleaned = redact(message)
        if cleaned != message:
            record.msg, record.args = cleaned, None
        if record.exc_info and not record.exc_text:
            # httpx errors carry the full URL in their text.
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        if record.stack_info:
            record.stack_info = redact(record.stack_info)
        return True
