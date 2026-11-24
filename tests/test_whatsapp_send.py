"""WhatsApp channel, phase B2: queueing an opener and draining it through Wasi.

The fake provider mimics the real API's answers, including the ones that were
observed against it: `session_window_closed` for free text outside 24 hours,
and Meta's `131009`-class errors for a number that will never receive a
message. No test touches the network.
"""

import datetime
import os
import tempfile

import pytest
import pytest_asyncio

from sellbird.agents.sender import Sender
from sellbird.integrations.mail_provider import SendResult
from sellbird.models.prospect import Prospect
from sellbird.state import MAYBE_SENT_PREFIX, StateManager

WA = "+15550100123"
TEMPLATE_BODY = "Hi {{customer_name}}, saw {{practice_name}} has no online booking."
RENDERED = "Hi Dana, saw Acme has no online booking."


def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).isoformat()


class FakeWasi:
    name = "wasi"
    channel = "whatsapp"
    carries_headers = False

    def __init__(self, remaining=50, quality=""):
        self.remaining = remaining
        self.quality = quality
        self.sent: list[dict] = []
        self.result: SendResult | None = None

    def is_configured(self):
        return True

    async def remaining_today(self):
        return self.remaining

    async def quality_problem(self):
        return self.quality

    async def send_email(self, to_email, subject, body, thread_ref="", in_reply_to="",
                         headers=(), template="", params=None):
        self.sent.append({"to": to_email, "body": body, "template": template,
                          "params": params or {}})
        if self.result is not None:
            return self.result
        return SendResult(ok=True, message_id="wamid.TEST", thread_ref="chat-1")


def _config(**overrides):
    """A config shaped like the rest of the suite's, with a real
    WhatsAppChannelConfig so template lookup and validation are the real ones."""
    from sellbird.config import WhatsAppChannelConfig

    whatsapp = WhatsAppChannelConfig(**{
        "enabled": True,
        "cold_first_touch": True,
        "cold_template": "cold_opener",
        "templates": [{"name": "cold_opener", "category": "Marketing",
                       "language": "en_US", "body": TEMPLATE_BODY}],
        "template_params": {"customer_name": "{{first_name}}",
                            "practice_name": "{{company}}"},
        "require_approval": True,
        **overrides.pop("whatsapp", {}),
    })

    class Cfg:
        class persona:
            name = "Alex"; email = "alex@example.com"; company = "Testco"
            role = "BD"; tone = "direct"

        class product:
            name = "P"; description = "d"; pricing = "$"; key_benefits = ["b"]
            objection_responses = {}

        class channels:
            class email:
                enabled = False
                provider = "gmail"
                max_daily_sends = 50
                send_to_risky = False
                require_approval = True
                max_bounce_rate = 0.05

            class linkedin:
                enabled = False

    cfg = Cfg()
    cfg.channels.whatsapp = whatsapp
    return cfg


class Env:
    instantly_api_key = ""
    wasi_api_key = "test-key"
    wasi_api_base_url = ""


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = StateManager(os.path.join(tmpdir, "t.db"))
        await store.init_db()
        yield store


def _sender(state, config, provider):
    sender = Sender(brain=None, state=state, config=config, env=Env())
    sender.whatsapp = provider
    sender.send_pacing = False
    return sender


async def _prospect(state, phone=WA, status="new"):
    return await state.add_prospect(Prospect(
        first_name="Dana", last_name="Doe", title="Owner", company="Acme",
        email="dana@example.com", email_status="verified", phone=phone, status=status,
    ))


# ── queueing ──

@pytest.mark.asyncio
async def test_an_opener_is_queued_for_approval_not_sent(state):
    """Criterion 2: the ladder is the same one email uses."""
    pid = await _prospect(state)
    provider = FakeWasi()
    sender = _sender(state, _config(), provider)
    item_id, why = await sender.stage_whatsapp_opener(pid)
    assert item_id and not why
    row = await state.get_outbox_item(item_id)
    assert row["status"] == "pending_review"
    assert row["channel"] == "whatsapp"
    assert row["to_email"] == WA
    assert row["subject"] == "cold_opener"      # which template, not a subject line
    assert row["body"] == RENDERED
    assert provider.sent == []


@pytest.mark.asyncio
async def test_nothing_is_queued_while_the_channel_is_off(state):
    """Criterion 1."""
    pid = await _prospect(state)
    config = _config(whatsapp={"enabled": False})
    item_id, why = await _sender(state, config, FakeWasi()).stage_whatsapp_opener(pid)
    assert item_id == "" and "off" in why


@pytest.mark.asyncio
async def test_nothing_is_queued_without_cold_first_touch(state):
    pid = await _prospect(state)
    config = _config(whatsapp={"cold_first_touch": False, "cold_template": ""})
    item_id, why = await _sender(state, config, FakeWasi()).stage_whatsapp_opener(pid)
    assert item_id == "" and "cold first contact" in why


@pytest.mark.asyncio
async def test_a_prospect_with_no_usable_number_is_not_queued(state):
    pid = await _prospect(state, phone="call the shop")
    item_id, why = await _sender(state, _config(), FakeWasi()).stage_whatsapp_opener(pid)
    assert item_id == "" and "no usable phone number" in why


@pytest.mark.asyncio
async def test_a_suppressed_number_is_not_queued(state):
    pid = await _prospect(state)
    await state.add_suppression("phone", WA, reason="opted_out")
    item_id, why = await _sender(state, _config(), FakeWasi()).stage_whatsapp_opener(pid)
    assert item_id == "" and "suppressed" in why


@pytest.mark.asyncio
async def test_a_missing_template_parameter_is_named(state):
    pid = await _prospect(state)
    config = _config(whatsapp={"template_params": {"customer_name": "{{first_name}}"}})
    item_id, why = await _sender(state, config, FakeWasi()).stage_whatsapp_opener(pid)
    assert item_id == ""
    assert "practice_name" in why and "template_params" in why


# ── draining ──

async def _approved(state, sender, pid):
    item_id, why = await sender.stage_whatsapp_opener(pid)
    assert item_id, why
    await state.approve_outbox(item_id)
    return item_id


@pytest.mark.asyncio
async def test_an_approved_opener_goes_out_as_a_template(state):
    pid = await _prospect(state)
    provider = FakeWasi()
    sender = _sender(state, _config(), provider)
    item_id = await _approved(state, sender, pid)

    await sender._drain_whatsapp()

    assert len(provider.sent) == 1
    sent = provider.sent[0]
    assert sent["to"] == WA
    assert sent["template"] == "cold_opener"
    assert sent["params"] == {"customer_name": "Dana", "practice_name": "Acme"}
    row = await state.get_outbox_item(item_id)
    assert row["status"] == "sent"
    assert row["message_id"] == "wamid.TEST"
    assert row["thread_ref"] == "chat-1"
    assert (await state.get_prospect(pid)).status == "contacted"


@pytest.mark.asyncio
async def test_a_utility_template_cannot_open_a_cold_conversation(state):
    """Meta punishes a Utility template used for outreach."""
    pid = await _prospect(state)
    provider = FakeWasi()
    config = _config(whatsapp={
        "templates": [{"name": "cold_opener", "category": "Utility",
                       "language": "en_US", "body": TEMPLATE_BODY}]})
    sender = _sender(state, config, provider)
    item_id = await _approved(state, sender, pid)

    await sender._drain_whatsapp()

    assert provider.sent == []
    row = await state.get_outbox_item(item_id)
    assert row["status"] == "pending_review"
    assert "Marketing" in row["error"]


@pytest.mark.asyncio
async def test_text_changed_after_approval_waits_for_a_human_again(state):
    """What was approved is what goes, or nothing does."""
    pid = await _prospect(state)
    provider = FakeWasi()
    sender = _sender(state, _config(), provider)
    item_id = await _approved(state, sender, pid)
    # The user edits the template's stored copy after approving the message.
    sender.config.channels.whatsapp.templates[0].body = (
        "Hi {{customer_name}}, saw {{practice_name}} needs a website."
    )

    await sender._drain_whatsapp()

    assert provider.sent == []
    row = await state.get_outbox_item(item_id)
    assert row["status"] == "pending_review"
    assert "changed after this was approved" in row["error"]


@pytest.mark.asyncio
async def test_the_kill_switch_stops_whatsapp_too(state):
    pid = await _prospect(state)
    provider = FakeWasi()
    sender = _sender(state, _config(), provider)
    await _approved(state, sender, pid)
    await state.set_setting("sending_paused", "bounce rate")

    await sender._drain_whatsapp()
    assert provider.sent == []


@pytest.mark.asyncio
async def test_a_quality_rating_below_green_stops_cold_whatsapp(state):
    """A falling rating is the last warning before Meta restricts the number."""
    pid = await _prospect(state)
    provider = FakeWasi(quality="Meta's quality rating for this number is YELLOW")
    sender = _sender(state, _config(), provider)
    await _approved(state, sender, pid)

    await sender._drain_whatsapp()

    assert provider.sent == []
    held = await state.current_sends_held()
    assert held and "YELLOW" in held["reason"]


@pytest.mark.asyncio
async def test_metas_own_limit_caps_the_cycle(state):
    """Criterion 6: the cap is the lower of ours and Meta's."""
    provider = FakeWasi(remaining=1)
    sender = _sender(state, _config(), provider)
    for i in range(3):
        pid = await _prospect(state)
        prospect = await state.get_prospect(pid)
        prospect.id = ""
        prospect.email = f"dana{i}@example.com"
        prospect.phone = f"+1555010012{i}"
        new_id = await state.add_prospect(prospect)
        await _approved(state, sender, new_id)

    await sender._drain_whatsapp()
    assert len(provider.sent) == 1


@pytest.mark.asyncio
async def test_nothing_is_sent_when_metas_limit_cannot_be_read(state):
    """Rather than risk the number on a guess."""
    pid = await _prospect(state)
    provider = FakeWasi()
    sender = _sender(state, _config(), provider)
    await _approved(state, sender, pid)

    async def _boom():
        raise RuntimeError("Wasi unreachable")
    provider.remaining_today = _boom

    await sender._drain_whatsapp()
    assert provider.sent == []


@pytest.mark.asyncio
async def test_a_closed_24_hour_window_holds_the_message_and_explains(state):
    pid = await _prospect(state)
    provider = FakeWasi()
    provider.result = SendResult(
        ok=False, error="session_window_closed: This chat is outside the 24-hour window")
    sender = _sender(state, _config(), provider)
    item_id = await _approved(state, sender, pid)

    await sender._drain_whatsapp()

    row = await state.get_outbox_item(item_id)
    assert row["status"] == "pending_review"
    assert "24 hours" in row["error"]


@pytest.mark.asyncio
async def test_a_number_not_on_whatsapp_is_suppressed_and_email_is_untouched(state):
    """Criterion 9: WhatsApp's hard bounce."""
    pid = await _prospect(state)
    # An email sequence is already queued for the same person.
    email_row = await state.add_outbox_item(
        prospect_id=pid, to_email="dana@example.com", subject="s", body="b",
        send_at=_now_iso(), status="approved", campaign_id="c1", step=1,
    )
    provider = FakeWasi()
    provider.result = SendResult(
        ok=False,
        error="send_failed: (#131009) Parameter value is not valid — "
              "The phone number is malformed")
    sender = _sender(state, _config(), provider)
    item_id = await _approved(state, sender, pid)

    await sender._drain_whatsapp()

    assert (await state.get_outbox_item(item_id))["status"] == "failed"
    assert await state.is_suppressed(WA) == "bounced"
    # The email sequence is cancelled for this prospect by the cross-channel
    # cancel, but the address itself is never suppressed.
    assert await state.is_suppressed("dana@example.com") is None
    assert (await state.get_outbox_item(email_row))["status"] == "cancelled"


@pytest.mark.asyncio
async def test_an_uncertain_send_is_never_retried(state):
    pid = await _prospect(state)
    provider = FakeWasi()
    provider.result = SendResult(ok=False, error="timeout", uncertain=True)
    sender = _sender(state, _config(), provider)
    item_id = await _approved(state, sender, pid)

    await sender._drain_whatsapp()
    row = await state.get_outbox_item(item_id)
    assert row["status"] == "failed"
    assert row["error"].startswith(MAYBE_SENT_PREFIX)

    await sender._drain_whatsapp()
    assert len(provider.sent) == 1      # not sent a second time


@pytest.mark.asyncio
async def test_a_reply_before_the_drain_cancels_the_message(state):
    """The same stop-on-reply the email drain gets, via _blocked."""
    pid = await _prospect(state)
    provider = FakeWasi()
    sender = _sender(state, _config(), provider)
    item_id = await _approved(state, sender, pid)
    await state.update_prospect_status(pid, "replied")

    await sender._drain_whatsapp()

    assert provider.sent == []
    assert (await state.get_outbox_item(item_id))["status"] == "cancelled"


@pytest.mark.asyncio
async def test_whatsapp_runs_even_with_the_email_channel_off(state):
    """An install can use WhatsApp alone."""
    pid = await _prospect(state)
    provider = FakeWasi()
    sender = _sender(state, _config(), provider)
    await _approved(state, sender, pid)

    await sender.run()      # email is disabled in _config()
    assert len(provider.sent) == 1


@pytest.mark.asyncio
async def test_a_whatsapp_failure_never_stops_email(state):
    pid = await _prospect(state)
    sender = _sender(state, _config(), FakeWasi())

    async def _boom():
        raise RuntimeError("Wasi exploded")
    sender._drain_whatsapp = _boom

    await sender.run()      # must not raise
