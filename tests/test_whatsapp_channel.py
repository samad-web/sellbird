"""WhatsApp channel, phase B1: the channel column, the config, and the gate.

Test names map to the acceptance criteria in
build-docs/features/whatsapp-channel.md. Every number is in a reserved test
range; every domain is example.com-style.
"""

import datetime
import os
import tempfile

import pytest
import pytest_asyncio

from sellbird.config import ChannelsConfig, ComplianceConfig, WhatsAppChannelConfig
from sellbird.gate import pre_send_check, template_mismatch, template_placeholders
from sellbird.models.prospect import Prospect
from sellbird.state import StateManager

WA = "+15550100123"


def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).isoformat()


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = StateManager(os.path.join(tmpdir, "t.db"))
        await store.init_db()
        yield store


async def _prospect(state, phone=WA):
    return await state.add_prospect(Prospect(
        first_name="Dana", last_name="Doe", title="Owner", company="Acme",
        email="dana@example.com", email_status="verified", phone=phone,
    ))


# ── 1. off by default ──

def test_the_channel_is_off_until_it_is_turned_on():
    """Criterion 1: nothing runs, and nothing is sent, out of the box."""
    channels = ChannelsConfig()
    assert channels.whatsapp.enabled is False
    assert channels.whatsapp.cold_first_touch is False
    assert channels.whatsapp.require_approval is True


def test_cold_first_touch_without_a_template_is_refused():
    """Meta only delivers a first message as an approved template."""
    with pytest.raises(ValueError, match="cold_template"):
        WhatsAppChannelConfig(enabled=True, cold_first_touch=True)
    # ...and it is fine once one is named.
    assert WhatsAppChannelConfig(
        enabled=True, cold_first_touch=True, cold_template="cold_opener"
    ).cold_template == "cold_opener"


def test_a_dialling_prefix_must_look_like_one():
    with pytest.raises(ValueError, match=r"must start with"):
        WhatsAppChannelConfig(default_country_code="91")
    with pytest.raises(ValueError, match="not a dialling prefix"):
        WhatsAppChannelConfig(default_country_code="+notanumber")
    assert WhatsAppChannelConfig(default_country_code="+91").default_country_code == "+91"


def test_an_unsupported_provider_is_refused():
    with pytest.raises(ValueError, match="not supported"):
        WhatsAppChannelConfig(provider="twilio")


# ── 2. the approval ladder is shared ──

@pytest.mark.asyncio
async def test_a_whatsapp_row_waits_for_approval_like_an_email(state):
    """Criterion 2."""
    pid = await _prospect(state)
    item = await state.add_outbox_item(
        prospect_id=pid, to_email=WA, subject="", body="hi",
        send_at=_now_iso(), status="pending_review", channel="whatsapp",
        provider="wasi",
    )
    row = await state.get_outbox_item(item)
    assert row["channel"] == "whatsapp"
    assert row["status"] == "pending_review"
    # It is not in the queue the Sender drains.
    assert await state.get_outbox(status="approved", channel="whatsapp") == []


@pytest.mark.asyncio
async def test_an_existing_row_is_an_email_row(state):
    """The column defaults, so every row written before this feature is email."""
    pid = await _prospect(state)
    item = await state.add_outbox_item(
        prospect_id=pid, to_email="dana@example.com", subject="s", body="b",
        send_at=_now_iso(), status="approved", campaign_id="c1", step=1,
    )
    assert (await state.get_outbox_item(item))["channel"] == "email"


# ── 3 and 4. the gate knows which medium it is checking ──

@pytest.mark.asyncio
async def test_the_gate_wants_an_e164_number_on_whatsapp(state):
    """Criterion 3."""
    bad = pre_send_check("dana@example.com", "", "hi there", channel="whatsapp")
    assert not bad
    assert any("E.164" in r for r in bad.reasons)
    # A valid number passes, with no subject and no postal address.
    good = pre_send_check(WA, "", "Saw Acme has no online booking. Want a mockup?",
                          channel="whatsapp", postal_address="1 Test St, Testville")
    assert good, good.reasons


def test_the_gate_still_wants_an_address_on_email():
    """Criterion 4: the email branch is untouched."""
    bad = pre_send_check(WA, "Subject", "body", channel="email")
    assert not bad
    assert any("invalid recipient address" in r for r in bad.reasons)
    # And a cold email still needs its postal address.
    missing = pre_send_check("dana@example.com", "Subject", "body",
                             postal_address="1 Test St")
    assert not missing
    assert any("postal address" in r for r in missing.reasons)


def test_whatsapp_has_no_subject_but_still_has_a_length_limit():
    assert pre_send_check(WA, "", "a short message", channel="whatsapp")
    long_message = "word " * 200
    result = pre_send_check(WA, "", long_message, channel="whatsapp")
    assert not result
    assert any("too long" in r for r in result.reasons)


def test_the_shared_checks_apply_to_whatsapp_too():
    """A channel changes the medium, not the rules about content."""
    tags = pre_send_check(WA, "", "Hi {{first_name}}, about Acme", channel="whatsapp")
    assert not tags and any("merge tags" in r for r in tags.reasons)

    spam = pre_send_check(WA, "", "Act now for a limited time", channel="whatsapp")
    assert not spam and any("banned phrases" in r for r in spam.reasons)

    links = pre_send_check(
        WA, "", "see https://a.example and https://b.example", channel="whatsapp")
    assert not links and any("too many links" in r for r in links.reasons)

    empty = pre_send_check(WA, "", "", channel="whatsapp")
    assert not empty and any("empty body" in r for r in empty.reasons)


@pytest.mark.asyncio
async def test_the_recipient_must_be_the_prospects_own_number(state):
    pid = await _prospect(state)
    prospect = await state.get_prospect(pid)
    assert pre_send_check(WA, "", "hi", prospect=prospect, channel="whatsapp")
    other = pre_send_check("+15550100999", "", "hi", prospect=prospect, channel="whatsapp")
    assert not other
    assert any("does not match the prospect" in r for r in other.reasons)


@pytest.mark.asyncio
async def test_a_local_listing_number_is_the_same_line_as_its_e164_form(state):
    """A company listing publishes "020 7946 0018"; we send to +442079460018."""
    pid = await _prospect(state, phone="020 7946 0018")
    prospect = await state.get_prospect(pid)
    assert pre_send_check("+442079460018", "", "hi", prospect=prospect, channel="whatsapp")


# ── 5. a cold template must be the template Meta approved ──

def test_a_cold_message_must_match_its_approved_template():
    """Criterion 5."""
    body = "Hi {{customer_name}}, saw {{practice_name}} has no online booking."
    assert template_placeholders(body) == ["customer_name", "practice_name"]
    assert template_mismatch("Hi Dana, saw Acme has no online booking.", body) == ""
    # Whitespace is the one thing WhatsApp normalises.
    assert template_mismatch("Hi Dana,  saw Acme\nhas no online booking.", body) == ""
    # Anything else is not that template.
    assert "not this approved template" in template_mismatch("Hey, want a call?", body)
    # An unfilled placeholder would otherwise satisfy the wildcard.
    assert "never filled in" in template_mismatch(
        "Hi {{customer_name}}, saw Acme has no online booking.", body)
    # And a template whose approved text was never synced cannot be checked.
    assert "isn't stored" in template_mismatch("anything", "")


# ── 6. the caps are per channel ──

@pytest.mark.asyncio
async def test_whatsapp_sends_do_not_eat_the_email_cap(state):
    """Criterion 6."""
    pid = await _prospect(state)
    for channel, recipient in (("whatsapp", WA), ("email", "dana@example.com")):
        item = await state.add_outbox_item(
            prospect_id=pid, to_email=recipient, subject="s", body="b",
            send_at=_now_iso(), status="approved", channel=channel,
            campaign_id=f"c-{channel}", step=1,
        )
        await state.update_outbox_item(item, status="sent", sent_at=_now_iso())

    assert await state.count_outbox_sent_today(channel="whatsapp") == 1
    assert await state.count_outbox_sent_today(channel="email") == 1
    # No channel given: the install-wide ceiling, which counts everything.
    assert await state.count_outbox_sent_today() == 2


# ── 7. one reply stops every channel ──

@pytest.mark.asyncio
async def test_a_reply_cancels_queued_messages_in_both_channels(state):
    """Criterion 7: stop-on-reply is about the person, not the medium."""
    pid = await _prospect(state)
    for channel, recipient in (("whatsapp", WA), ("email", "dana@example.com")):
        await state.add_outbox_item(
            prospect_id=pid, to_email=recipient, subject="s", body="b",
            send_at=_now_iso(), status="approved", channel=channel,
            campaign_id=f"c-{channel}", step=1,
        )
    cancelled = await state.cancel_pending_outbox_for_prospect(pid, reason="replied")
    assert cancelled == 2
    assert await state.get_outbox(status="approved") == []


# ── 11. a phone number on the do-not-contact list ──

def test_a_phone_number_can_go_on_the_do_not_contact_list():
    """Criterion 11: this used to raise a config error."""
    compliance = ComplianceConfig(do_not_contact=["+1 555 0100 123", "acme.example"])
    assert "+15550100123" in compliance.do_not_contact
    assert "acme.example" in compliance.do_not_contact


def test_a_do_not_contact_number_blocks_a_whatsapp_message():
    result = pre_send_check(WA, "", "hi", channel="whatsapp",
                            do_not_contact=["+15550100123"])
    assert not result
    assert any("do-not-contact" in r for r in result.reasons)


def test_a_local_number_on_the_list_is_an_error_not_a_silent_no_op():
    """It names a different line in every country, so it is refused loudly."""
    with pytest.raises(ValueError, match="international form"):
        ComplianceConfig(do_not_contact=["020 7946 0018"])


def test_a_plus_tagged_address_is_still_an_address():
    """The "+" test must not steal an address with a numeric plus-tag."""
    compliance = ComplianceConfig(do_not_contact=["dana+1234567@example.com"])
    assert compliance.do_not_contact == ["dana+1234567@example.com"]


def test_a_number_entry_does_not_block_the_company_or_vice_versa():
    """A number names a person; it says nothing about their company's mail."""
    from sellbird.gate import do_not_contact_domain_match, do_not_contact_match
    assert do_not_contact_domain_match("example.com", ["+15550100123"]) == ""
    assert do_not_contact_match("dana@example.com", ["+15550100123"]) == ""
    assert do_not_contact_match(WA, ["dana@example.com", "example.com"]) == ""


# ── the normalised phone column ──

@pytest.mark.asyncio
async def test_a_prospect_is_found_by_their_number_exactly(state):
    pid = await _prospect(state, phone="+1 555 0100 123")
    found = await state.get_prospect_by_phone(WA)
    assert found is not None and found.id == pid
    # A local form is not resolved to a prospect by guesswork.
    assert await state.get_prospect_by_phone("555 0100 123") is None
    assert await state.get_prospect_by_phone("") is None


@pytest.mark.asyncio
async def test_a_number_that_only_resolves_with_a_dialling_code_is_backfilled(state):
    pid = await _prospect(state, phone="1632 960001")
    assert await state.get_prospect_by_phone("+441632960001") is None
    assert await state.backfill_phone_e164("+44") == 1
    found = await state.get_prospect_by_phone("+441632960001")
    assert found is not None and found.id == pid
    # Idempotent: a second run has nothing left to do.
    assert await state.backfill_phone_e164("+44") == 0


@pytest.mark.asyncio
async def test_setting_a_phone_keeps_what_the_listing_published(state):
    pid = await _prospect(state, phone="")
    e164 = await state.set_prospect_phone(pid, "020 7946 0018", "+44", verified=True)
    assert e164 == "+442079460018"
    prospect = await state.get_prospect(pid)
    assert prospect.phone == "020 7946 0018"      # what a human should read
    assert prospect.phone_verified is True
    assert (await state.get_prospect_by_phone("+442079460018")).id == pid


@pytest.mark.asyncio
async def test_a_number_that_cannot_be_normalised_is_still_kept_for_a_human(state):
    pid = await _prospect(state, phone="")
    assert await state.set_prospect_phone(pid, "call the shop") == ""
    assert (await state.get_prospect(pid)).phone == "call the shop"
    assert await state.get_prospect_by_phone("call the shop") is None


# ── B5: the CLI and the shipped config ──

def test_the_shipped_config_keeps_the_channel_off():
    """A clone must never message anyone on WhatsApp by default."""
    from sellbird.config import load_config
    config = load_config("sellbird.yaml")
    assert config.channels.whatsapp.enabled is False
    assert config.channels.whatsapp.cold_first_touch is False


@pytest.mark.parametrize("argv, expected", [
    (["whatsapp", "--help"], "backfill-phones"),
    (["outbox", "--help"], "--channel"),
])
def test_the_cli_registers_the_whatsapp_command(argv, expected, capsys, monkeypatch):
    """Through the real entry point: the parser is built inside main()."""
    import sellbird.cli as cli
    monkeypatch.setattr("sys.argv", ["sellbird", *argv])
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    assert exit_info.value.code == 0
    assert expected in capsys.readouterr().out


def test_the_docs_exist_and_say_the_dangerous_part():
    """CONTRIBUTING: docs and code change together. This one has to warn."""
    from pathlib import Path
    doc = Path("docs/whatsapp.md").read_text(encoding="utf-8")
    assert "off by default" in doc
    assert "Marketing" in doc           # the category a cold opener needs
    assert "24 hours" in doc            # the window
