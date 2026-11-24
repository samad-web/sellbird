"""WhatsApp channel, phase B3: reading inbound, and what a reply stops.

Covers acceptance criteria 7, 8 and 12 from
build-docs/features/whatsapp-channel.md, plus the polling cursor, which is
the part that loses messages if it is wrong.
"""

import datetime
import os
import tempfile

import pytest
import pytest_asyncio

from sellbird.integrations.mail_provider import InboundMessage
from sellbird.models.prospect import Prospect
from sellbird.state import StateManager

WA = "+15550100123"


def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).isoformat()


class FakeWasi:
    name = "wasi"
    channel = "whatsapp"
    carries_headers = False

    def __init__(self, inbound=None, fail=False):
        self.inbound = inbound or []
        self.fail = fail
        self.reads = 0
        self.confirmed = 0

    def is_configured(self):
        return True

    async def get_replies(self, limit=50):
        self.reads += 1
        if self.fail:
            raise RuntimeError("Wasi unreachable")
        return self.inbound

    def confirm_read(self):
        self.confirmed += 1

    async def remaining_today(self):
        return 50

    async def quality_problem(self):
        return ""

    # Housekeeping sweeps: quiet by default, overridden where they matter.
    async def contacts(self, limit=100):
        return []

    async def failed_messages(self, chat_ids):
        return []


class StubBrain:
    def __init__(self, intent="question", reply="Happy to help."):
        self._intent, self._reply = intent, reply

    def load_skills_for_agent(self, *_):
        return ""

    def load_prompt(self, *_a, **_k):
        return ""

    async def think(self, prompt, session_id=None, agent="", task=""):
        if task == "classify_intent" or "category" in prompt.lower():
            return self._intent
        return self._reply


def _config():
    from sellbird.config import WhatsAppChannelConfig

    class Cfg:
        class persona:
            name = "Alex"; email = "alex@example.com"; company = "Testco"
            role = "BD"; tone = "direct"

        class product:
            name = "P"; description = "d"; pricing = "$"; key_benefits = ["b"]
            objection_responses = {}

        class compliance:
            postal_address = ""
            opt_out_line = ""
            do_not_contact = []

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
    cfg.channels.whatsapp = WhatsAppChannelConfig(enabled=True, require_approval=True)
    return cfg


class Env:
    instantly_api_key = ""
    wasi_api_key = "k"
    wasi_api_base_url = ""


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = StateManager(os.path.join(tmpdir, "t.db"))
        await store.init_db()
        yield store


def _handler(state, provider, intent="question"):
    from sellbird.agents.handler import Handler
    handler = Handler(brain=StubBrain(intent=intent), state=state,
                      config=_config(), env=Env())
    handler.whatsapp = provider
    handler.provider = None
    return handler


async def _prospect(state, status="contacted"):
    return await state.add_prospect(Prospect(
        first_name="Dana", last_name="Doe", title="Owner", company="Acme",
        email="dana@example.com", email_status="verified", phone=WA, status=status,
    ))


def _msg(body, mid="in-1", sent_at="2026-11-24T10:00:00.000Z"):
    return InboundMessage(provider_id=mid, from_email=WA, subject="", body=body,
                          message_id=f"wamid.{mid}", thread_ref="chat-1", date=sent_at)


# ── reading ──

@pytest.mark.asyncio
async def test_an_inbound_message_finds_its_prospect_by_number(state):
    pid = await _prospect(state)
    provider = FakeWasi([_msg("Sounds interesting, tell me more")])
    handler = _handler(state, provider, intent="interested")

    assert await handler.run() is True

    assert (await state.get_prospect(pid)).status == "replied"
    convos = await state.get_conversations_by_status("open")
    assert len(convos) == 1
    assert convos[0].channel == "whatsapp"
    assert provider.confirmed == 1


@pytest.mark.asyncio
async def test_a_reply_cancels_queued_messages_on_both_channels(state):
    """Criterion 7, through the real Handler this time."""
    pid = await _prospect(state)
    for channel, recipient in (("whatsapp", WA), ("email", "dana@example.com")):
        await state.add_outbox_item(
            prospect_id=pid, to_email=recipient, subject="s", body="b",
            send_at=_now_iso(), status="approved", channel=channel,
            campaign_id=f"c-{channel}", step=1,
        )
    handler = _handler(state, FakeWasi([_msg("not now thanks")]), intent="not_interested")

    await handler.run()

    assert await state.get_outbox(status="approved") == []


@pytest.mark.asyncio
async def test_the_drafted_answer_is_a_whatsapp_row_with_no_subject(state):
    await _prospect(state)
    handler = _handler(state, FakeWasi([_msg("what does it cost?")]), intent="question")

    await handler.run()

    queued = await state.get_outbox(status="pending_review", channel="whatsapp")
    assert len(queued) == 1
    row = queued[0]
    assert row["kind"] == "reply"
    assert row["to_email"] == WA
    # Empty, because the Sender reads a value here as "this is a template".
    assert row["subject"] == ""
    assert row["thread_ref"] == "chat-1"


@pytest.mark.asyncio
async def test_a_message_from_an_unknown_number_is_ignored_safely(state):
    provider = FakeWasi([InboundMessage(
        provider_id="in-x", from_email="+15550199999", body="who is this")])
    handler = _handler(state, provider)

    assert await handler.run() is True
    assert await state.get_outbox() == []


# ── the cursor and the read contract ──

@pytest.mark.asyncio
async def test_a_failed_read_holds_sending_and_says_why(state):
    """Criterion 8: an unread message can't have stopped its follow-ups."""
    await _prospect(state)
    provider = FakeWasi(fail=True)
    handler = _handler(state, provider)

    assert await handler.run() is False
    assert "WhatsApp" in handler.last_read_problem
    assert provider.confirmed == 0


@pytest.mark.asyncio
async def test_the_same_message_is_not_handled_twice(state):
    await _prospect(state)
    provider = FakeWasi([_msg("tell me more")])
    handler = _handler(state, provider, intent="interested")

    await handler.run()
    await handler.run()      # the provider hands back the same message

    assert len(await state.get_outbox(channel="whatsapp")) == 1


# ── opt-out crosses channels ──

@pytest.mark.asyncio
async def test_stop_on_whatsapp_suppresses_the_email_address_too(state):
    """Criterion 12: "stop messaging me" is not permission to email instead."""
    pid = await _prospect(state)
    handler = _handler(state, FakeWasi([_msg("stop")]), intent="unsubscribe")

    await handler.run()

    assert (await state.get_prospect(pid)).status == "opted_out"
    assert await state.is_suppressed(WA) == "opted_out"
    assert await state.is_suppressed("dana@example.com") == "opted_out"
    # And nothing was drafted back.
    assert await state.get_outbox(channel="whatsapp") == []


@pytest.mark.asyncio
async def test_an_email_opt_out_suppresses_their_whatsapp_number_too(state):
    """The same rule in the other direction."""
    from sellbird.agents.handler import Handler
    pid = await _prospect(state)
    handler = Handler(brain=StubBrain(intent="unsubscribe"), state=state,
                      config=_config(), env=Env())
    handler.whatsapp = None
    handler.provider = None

    await handler._process_reply("dana@example.com", "unsubscribe", reply_uuid="")

    assert (await state.get_prospect(pid)).status == "opted_out"
    assert await state.is_suppressed("dana@example.com") == "opted_out"
    assert await state.is_suppressed(WA) == "opted_out"


@pytest.mark.asyncio
async def test_a_suppressed_number_is_never_messaged_again(state):
    """The Sender's own check, on the suppression the Handler just wrote."""
    pid = await _prospect(state)
    await state.add_suppression("phone", WA, reason="opted_out")
    item = await state.add_outbox_item(
        prospect_id=pid, to_email=WA, subject="", body="hi",
        send_at=_now_iso(), status="approved", channel="whatsapp", provider="wasi",
    )
    from sellbird.agents.sender import Sender
    sender = Sender(brain=None, state=state, config=_config(), env=Env())
    sender.whatsapp = FakeWasi()
    sender.send_pacing = False

    await sender._drain_whatsapp()

    row = await state.get_outbox_item(item)
    assert row["status"] == "cancelled"
    assert "suppressed" in row["error"]


# ── the provider's own polling logic ──

@pytest.mark.asyncio
async def test_the_provider_only_returns_messages_newer_than_the_cursor():
    """Wasi has no "since" filter, so the cursor is ours to get right."""
    from sellbird.integrations.wasi import WasiProvider

    class Env2:
        wasi_api_key = "k"
        wasi_api_base_url = ""

    provider = WasiProvider(config=None, env=Env2())
    threads = [{"id": "chat-1", "phone": "15550100123",
                "last_inbound_at": "2026-11-24T10:00:00.000Z"}]
    messages = [
        {"id": "m1", "direction": "in", "body": "first",
         "sent_at": "2026-11-24T09:00:00.000Z", "meta_message_id": "w1"},
        {"id": "m2", "direction": "in", "body": "second",
         "sent_at": "2026-11-24T10:00:00.000Z", "meta_message_id": "w2"},
        {"id": "m3", "direction": "out", "body": "ours",
         "sent_at": "2026-11-24T09:30:00.000Z", "meta_message_id": "w3"},
    ]

    async def fake_request(method, path, json=None, params=None, retries=3):
        return threads if path == "/conversations" else messages

    provider._request = fake_request

    first = await provider.get_replies()
    # Both inbound, neither outbound. The "+" is restored from the wa_id.
    assert [m.body for m in first] == ["first", "second"]
    assert first[0].from_email == "+15550100123"
    assert first[0].thread_ref == "chat-1"

    # Without confirm_read the cursor stays put: nothing is lost on a crash.
    assert [m.body for m in await provider.get_replies()] == ["first", "second"]

    provider.confirm_read()
    assert await provider.get_replies() == []

    # A newer message is picked up after the cursor moved.
    messages.append({"id": "m4", "direction": "in", "body": "third",
                     "sent_at": "2026-11-24T11:00:00.000Z", "meta_message_id": "w4"})
    threads[0]["last_inbound_at"] = "2026-11-24T11:00:00.000Z"
    assert [m.body for m in await provider.get_replies()] == ["third"]


@pytest.mark.asyncio
async def test_the_provider_maps_wasis_error_shape():
    from sellbird.integrations.wasi import WasiResponseError

    window = WasiResponseError({"code": "session_window_closed",
                                "message": "This chat is outside the 24-hour window"})
    assert window.window_closed and not window.dead_number

    meta = WasiResponseError({
        "code": "send_failed", "message": "(#131009) Parameter value is not valid",
        "metaError": {"code": 131009,
                      "error_data": {"details": "The phone number is malformed"}}})
    assert meta.dead_number
    assert "malformed" in meta.detail

    validation = WasiResponseError({"code": "validation_failed",
                                    "message": "Request validation failed.",
                                    "details": ["to: Required"]})
    assert "to: Required" in validation.detail
    assert not validation.dead_number


# ── B4: what happens with no reply at all ──

@pytest.mark.asyncio
async def test_a_block_inside_whatsapp_is_honoured_without_any_reply(state):
    """Tapping Block sends nothing we could read, so Wasi's state is read."""
    pid = await _prospect(state)
    await state.add_outbox_item(
        prospect_id=pid, to_email=WA, subject="", body="hi",
        send_at=_now_iso(), status="approved", channel="whatsapp", provider="wasi",
    )
    provider = FakeWasi([])
    provider.contact_rows = [
        {"phone": "15550100123", "whatsapp_blocked_at": "2026-11-24T09:00:00.000Z"}]

    async def contacts(limit=100):
        return provider.contact_rows
    provider.contacts = contacts

    handler = _handler(state, provider)
    assert await handler.run() is True

    assert await state.is_suppressed(WA) == "opted_out"
    assert await state.get_outbox(status="approved", channel="whatsapp") == []
    # Email is a separate permission: a block in one app is not an unsubscribe
    # from the other.
    assert await state.is_suppressed("dana@example.com") is None


@pytest.mark.asyncio
async def test_a_send_rejected_after_being_accepted_is_caught(state):
    """Meta can accept a send and fail it minutes later."""
    pid = await _prospect(state)
    item = await state.add_outbox_item(
        prospect_id=pid, to_email=WA, subject="", body="hi",
        send_at=_now_iso(), status="approved", channel="whatsapp", provider="wasi",
    )
    await state.update_outbox_item(
        item, status="sent", sent_at=_now_iso(),
        message_id="wamid.LATE", thread_ref="chat-1")

    provider = FakeWasi([])

    async def contacts(limit=100):
        return []

    async def failed_messages(chat_ids):
        assert list(chat_ids) == ["chat-1"]      # only threads we sent into
        return [{"meta_message_id": "wamid.LATE", "status": "failed",
                 "meta_error_code": 131026, "error_reason": "Message undeliverable",
                 "chat_id": "chat-1"}]
    provider.contacts = contacts
    provider.failed_messages = failed_messages

    handler = _handler(state, provider)
    await handler.run()

    row = await state.get_outbox_item(item)
    assert row["status"] == "failed"
    assert "after accepting it" in row["error"]
    assert await state.is_suppressed(WA) == "bounced"


@pytest.mark.asyncio
async def test_housekeeping_failures_never_hold_sending(state):
    """A broken sweep must not look like an unread reply."""
    await _prospect(state)
    provider = FakeWasi([])

    async def boom(*_a, **_k):
        raise RuntimeError("Wasi hiccup")
    provider.contacts = boom
    provider.failed_messages = boom

    handler = _handler(state, provider)
    assert await handler.run() is True        # still a clean read
