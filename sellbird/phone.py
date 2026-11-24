"""Phone numbers in E.164, the one form WhatsApp accepts.

Meta's API wants ``+<country><subscriber>`` and says so when it doesn't get
it: "The phone number is malformed: Please use the format: +1234567890."
Numbers reach Sellbird from company listings and scraped pages in every
local shape there is, so they are normalised once, here, before anything is
queued.

shortcut: this does not validate a national number against its country's
numbering plan (area-code lengths, valid prefixes, mobile vs landline). It
normalises shape, not existence. That is enough because nothing is sent on
trust: Meta rejects a number that isn't real and the Handler then marks the
phone invalid, exactly as it does for an email bounce. Install
``phonenumbers`` and swap ``normalize`` over if users start sending to
well-shaped numbers that don't exist.
"""

from __future__ import annotations

import re
import unicodedata

# E.164: "+", then 1-15 digits, the first of which is never 0.
E164_RE = re.compile(r"^\+[1-9]\d{0,14}$")
# The shortest number worth treating as a phone number at all. Below this it
# is an extension, a house number or an order id.
MIN_DIGITS = 7
# What people put between the digits: spaces, dashes of every width, dots,
# brackets, and the non-breaking spaces that come out of scraped HTML.
_SEPARATORS = re.compile(r"[\s.\-‐-―−()\[\]/]+")
# "+44 (0) 20..." — a trunk zero in brackets after the country code, which is
# how British and Dutch sites print numbers. It is not part of the number.
# It must be recognised while the brackets are still there: once they are
# stripped, "+44(0)20 7946 0018" and the real "+44 207 946 0018" look
# identical, and dropping a zero from the second one dials a stranger.
_BRACKETED_TRUNK = re.compile(r"(?<=\+)(\d{1,3})\s*\(\s*0\s*\)")
# Text that means "extension" and whatever follows it: never dialled. "x99"
# has no word boundary after the x, so that form is matched without one.
_EXTENSION = re.compile(
    r"(?:\b(?:ext|extn|extension|poste|anexo)\b\.?|\bx|#)\s*\d+\s*$", re.IGNORECASE)
# Characters that survive NFKC and .strip() but would break the "+" test.
_INVISIBLE = re.compile("[­​-‏⁠﻿]")


def _clean(raw) -> str:
    """The characters of a phone number, without the decoration."""
    text = _INVISIBLE.sub("", unicodedata.normalize("NFKC", str(raw or "")))
    text = _EXTENSION.sub("", text).strip()
    # "tel:+1555..." from a scraped mailto-style link.
    text = re.sub(r"^(?:tel|callto|phone|whatsapp|wa)\s*:\s*", "", text, flags=re.IGNORECASE)
    text = _BRACKETED_TRUNK.sub(r"\1", text)   # while the brackets still exist
    return _SEPARATORS.sub("", text)


def normalize(raw, default_code: str = "") -> str:
    """``raw`` as an E.164 number, or "" when it isn't one.

    ``default_code`` is a dialling prefix such as "+91", used for a number
    written the way it would be dialled locally. It is a prefix rather than
    an ISO country code on purpose: a prefix needs no 240-row lookup table
    and leaves no doubt what gets prepended.

    Handles a leading "+", an international "00" prefix, a bracketed trunk
    zero after the country code, and a national number with or without its
    own trunk zero. Anything still ambiguous comes back "".
    """
    text = _clean(raw)
    if not text:
        return ""
    if text.startswith("+"):
        candidate = text       # _clean already removed any bracketed trunk zero
    elif text.startswith("00"):
        candidate = "+" + text[2:]
    else:
        code = _clean(default_code)
        if not code.startswith("+") or not E164_RE.match(code):
            # No country to assume, so a local number cannot be resolved.
            # A bare international number without its "+" is not guessed
            # either: "441632960001" could be the UK or a US number.
            return ""
        # A national number is written with a trunk prefix in most of the
        # world ("020 7946 0018", "089 1234567"); it is dropped when the
        # country code goes on. India and the US don't use one.
        candidate = code + text.lstrip("0")
    if not candidate[1:].isdigit():
        return ""
    if len(candidate) - 1 < MIN_DIGITS:
        return ""
    return candidate if E164_RE.match(candidate) else ""


def is_e164(value) -> bool:
    """``value`` is already a well-formed E.164 number."""
    text = str(value or "").strip()
    return bool(E164_RE.match(text)) and len(text) - 1 >= MIN_DIGITS


def looks_like_phone(raw) -> bool:
    """``raw`` is a phone number rather than an address or a domain.

    Used where a configured entry could be either (compliance.do_not_contact),
    so it must never claim a domain: anything with an "@", a letter or a "/"
    is not a phone number.
    """
    text = _INVISIBLE.sub("", unicodedata.normalize("NFKC", str(raw or ""))).strip()
    if not text or "@" in text or "/" in text:
        return False
    body = text[1:] if text.startswith("+") else text
    if re.search(r"[^\W\d_]", body):   # any letter, in any script
        return False
    digits = re.sub(r"\D", "", body)
    return len(digits) >= MIN_DIGITS


# A suffix match needs at least this many digits in the shorter number. A
# subscriber number is 9-10 digits almost everywhere, and without this floor
# a 7-digit local number would match any longer number ending the same way —
# which, used to find the prospect a reply came from, is the wrong person.
_SUFFIX_MATCH_DIGITS = 9


def same_number(a, b) -> bool:
    """Two numbers are the same subscriber.

    Exact on the digits, or — when the shorter is a full subscriber number —
    the longer ending with it, so a listing's local "020 7946 0018" is
    recognised as the same line as "+442079460018". A leading trunk zero is
    dropped first, since that is what the country code replaces.
    """
    def digits(value) -> str:
        return re.sub(r"\D", "", str(value or "")).lstrip("0")

    da, db = digits(a), digits(b)
    if len(da) < MIN_DIGITS or len(db) < MIN_DIGITS:
        return False
    if da == db:
        return True
    shorter, longer = (da, db) if len(da) <= len(db) else (db, da)
    return len(shorter) >= _SUFFIX_MATCH_DIGITS and longer.endswith(shorter)
