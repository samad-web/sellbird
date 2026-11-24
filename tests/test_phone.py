"""E.164 normalisation — the one form WhatsApp accepts.

Maps to the WhatsApp channel's "Input" edge cases: bare local numbers,
extensions, landlines, scraped decoration, and anything that is not a number
at all. Every number here is in a reserved test range or RFC-style fictional.
"""

import pytest

from sellbird.phone import (
    is_e164, looks_like_phone, normalize, same_number,
)


@pytest.mark.parametrize("raw, expected", [
    # Already E.164, with the decoration people actually type
    ("+15550100123", "+15550100123"),
    ("+1 555 0100 123", "+15550100123"),
    ("+1 (555) 0100-123", "+15550100123"),
    ("+1–555–0100123", "+15550100123"),      # en dash
    ("​+15550100123 ", "+15550100123"),           # zero-width junk
    ("tel:+15550100123", "+15550100123"),
    ("whatsapp: +15550100123", "+15550100123"),
    # International prefix instead of "+"
    ("0015550100123", "+15550100123"),
    ("00 44 1632 960001", "+441632960001"),
    # A bracketed trunk zero after the country code (UK/NL style)
    ("+44 (0) 1632 960001", "+441632960001"),
    ("+44(0)20 7946 0018", "+442079460018"),
    # ...but a real leading zero in the subscriber number is NOT a trunk zero.
    # Stripping it here would dial a different person.
    ("+442079460018", "+442079460018"),
    ("+39 06 1234 5678", "+390612345678"),      # Italy keeps its leading zero
    # Extensions are never dialled
    ("+15550100123 ext. 42", "+15550100123"),
    ("+15550100123 x99", "+15550100123"),
    ("+15550100123 #7", "+15550100123"),
])
def test_normalize_keeps_well_formed_numbers(raw, expected):
    assert normalize(raw) == expected


@pytest.mark.parametrize("raw, code, expected", [
    # National number plus a configured dialling prefix
    ("1632960001", "+44", "+441632960001"),
    ("020 7946 0018", "+44", "+442079460018"),     # trunk zero dropped
    ("089 1234567", "+49", "+49891234567"),
    ("(555) 0100-123", "+1", "+15550100123"),
])
def test_normalize_uses_the_default_dialling_code(raw, code, expected):
    assert normalize(raw, code) == expected


@pytest.mark.parametrize("raw", [
    "",
    None,
    "not a number",
    "jane@example.com",
    "example.com",
    "12345",                    # too short to be a phone number
    "+0123456789",              # E.164 never starts with 0
    "+1234567890123456",        # 16 digits, over the E.164 limit
    "555 0100",                 # local, and no default code given
    "441632960001",             # international without "+": never guessed
])
def test_normalize_refuses_what_it_cannot_resolve(raw):
    assert normalize(raw) == ""


def test_normalize_refuses_a_local_number_when_the_default_code_is_junk():
    assert normalize("1632960001", "44") == ""       # no leading +
    assert normalize("1632960001", "") == ""
    assert normalize("1632960001", "+") == ""


def test_is_e164():
    assert is_e164("+15550100123")
    assert not is_e164("15550100123")               # no "+"
    assert not is_e164("+1555")                     # too few digits
    assert not is_e164("+1555010012a")
    assert not is_e164("")


@pytest.mark.parametrize("raw, expected", [
    ("+15550100123", True),
    ("555 0100 123", True),
    ("(555) 0100-123", True),
    # Must never claim something that is an address or a domain
    ("jane@example.com", False),
    ("example.com", False),
    ("https://example.com/x", False),
    ("acme.example", False),
    ("+1555call now", False),
    ("", False),
    ("12345", False),
])
def test_looks_like_phone_never_claims_a_domain(raw, expected):
    assert looks_like_phone(raw) is expected


def test_same_number_matches_a_local_listing_to_its_e164_form():
    assert same_number("+442079460018", "02079460018")
    assert same_number("+15550100123", "+15550100123")
    assert not same_number("+15550100123", "+15550100124")
    assert not same_number("+15550100123", "")
    assert not same_number("123", "123")            # too short either way
