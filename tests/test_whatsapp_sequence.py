"""WhatsApp phase 2: a sequence like email's, and parameters Claude writes.

One test per acceptance criterion in
build-docs/features/whatsapp-channel.md, "Phase 2". Nothing here touches the
network: the provider and the brain are fakes.
"""

import datetime

import pytest

from sellbird.gate import template_param_problem
from tests.test_whatsapp_send import (  # noqa: F401  (state fixture)
    FakeWasi,
    TEMPLATE_BODY,
    WA,
    Env,
    _config,
    _prospect,
    _sender,
    state,
)

FOLLOW_BODY = "Hi {{customer_name}}, still worth a look at {{practice_name}}?"
LAST_BODY = "Hi {{customer_name}}, closing the loop."


def _now():
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def _sequence_config(**whatsapp):
    """The usual config plus two approved follow-up templates."""
    base = {
        "templates": [
            {"name": "cold_opener", "category": "Marketing", "language": "en_US",
             "body": TEMPLATE_BODY},
            {"name": "nudge", "category": "Marketing", "language": "en_US",
             "body": FOLLOW_BODY},
            {"name": "breakup", "category": "Marketing", "language": "en_US",
             "body": LAST_BODY},
        ],
        "follow_ups": [{"template": "nudge", "delay_days": 3},
                       {"template": "breakup", "delay_days": 4}],
    }
    base.update(whatsapp)
    return _config(whatsapp=base)


class FakeBrain:
    """Answers think_json with whatever it was given."""

    def __init__(self, answer):
        self.answer = answer
        self.prompts: list[str] = []

    def load_prompt(self, *a, **k):
        return ""

    def load_skills_for_agent(self, *a, **k):
        return ""

    def load_skill(self, *a, **k):
        return ""

    async def think_json(self, prompt, **kwargs):
        self.prompts.append(prompt)
        return self.answer


# ── 1. the sequence is staged ──

@pytest.mark.asyncio
async def test_follow_ups_are_staged_as_steps_at_their_delays(state):
    """Criterion 1."""
    pid = await _prospect(state)
    sender = _sender(state, _sequence_config(), FakeWasi())
    item_id, why = await sender.stage_whatsapp_opener(pid)
    assert item_id and not why

    rows = sorted(await state.get_outbox(channel="whatsapp", limit=50),
                  key=lambda r: r["step"])
    assert [r["step"] for r in rows] == [1, 2, 3]
    assert [r["subject"] for r in rows] == ["cold_opener", "nudge", "breakup"]
    assert all(r["status"] == "pending_review" for r in rows)
    # The opener goes now; the delays are cumulative, so +3 then +4 more.
    at = [datetime.datetime.fromisoformat(r["send_at"]) for r in rows]
    assert abs((at[0] - _now()).total_seconds()) < 60
    assert [round((t - at[0]).total_seconds() / 86400) for t in at] == [0, 3, 7]


@pytest.mark.asyncio
async def test_no_follow_ups_configured_stages_one_message(state):
    pid = await _prospect(state)
    sender = _sender(state, _config(), FakeWasi())
    item_id, why = await sender.stage_whatsapp_opener(pid)
    assert item_id and not why
    assert len(await state.get_outbox(channel="whatsapp", limit=50)) == 1


@pytest.mark.asyncio
async def test_a_follow_up_naming_an_unknown_template_stages_nothing(state):
    """Half a sequence on someone's phone is worse than none."""
    pid = await _prospect(state)
    config = _sequence_config(follow_ups=[{"template": "ghost", "delay_days": 3}])
    item_id, why = await _sender(state, config, FakeWasi()).stage_whatsapp_opener(pid)
    assert item_id == ""
    assert "ghost" in why and "follow_ups step 2" in why
    assert await state.get_outbox(channel="whatsapp", limit=50) == []


# ── 2 & 3. a follow-up waits for the step before it ──

@pytest.mark.asyncio
async def test_a_follow_up_does_not_send_while_the_opener_waits(state):
    """Criterion 2: the opener is still pending_review, so step 2 holds."""
    pid = await _prospect(state)
    provider = FakeWasi()
    sender = _sender(state, _sequence_config(), provider)
    await sender.stage_whatsapp_opener(pid)
    rows = {r["step"]: r for r in await state.get_outbox(channel="whatsapp", limit=50)}
    # Approve step 2 only, and make it due now: the opener is still waiting.
    await state.update_outbox_item(rows[2]["id"], status="approved",
                                   send_at=_now().isoformat())
    await sender._drain_whatsapp()
    assert provider.sent == []
    assert (await state.get_outbox_item(rows[2]["id"]))["status"] == "approved"


@pytest.mark.asyncio
async def test_a_follow_up_is_cancelled_when_the_opener_never_sent(state):
    """Criterion 3."""
    pid = await _prospect(state)
    provider = FakeWasi()
    sender = _sender(state, _sequence_config(), provider)
    await sender.stage_whatsapp_opener(pid)
    rows = {r["step"]: r for r in await state.get_outbox(channel="whatsapp", limit=50)}
    await state.update_outbox_item(rows[1]["id"], status="failed", error="no")
    await state.update_outbox_item(rows[2]["id"], status="approved",
                                   send_at=_now().isoformat())
    await sender._drain_whatsapp()
    row = await state.get_outbox_item(rows[2]["id"])
    assert row["status"] == "cancelled"
    assert "step 1 wasn't sent" in row["error"]
    assert provider.sent == []


@pytest.mark.asyncio
async def test_a_follow_up_sends_once_the_opener_has_gone_out(state):
    pid = await _prospect(state)
    provider = FakeWasi()
    sender = _sender(state, _sequence_config(), provider)
    await sender.stage_whatsapp_opener(pid)
    rows = {r["step"]: r for r in await state.get_outbox(channel="whatsapp", limit=50)}
    long_ago = (_now() - datetime.timedelta(days=30)).isoformat()
    await state.update_outbox_item(rows[1]["id"], status="sent", sent_at=long_ago)
    await state.update_outbox_item(rows[2]["id"], status="approved",
                                   send_at=_now().isoformat())
    await sender._drain_whatsapp()
    assert len(provider.sent) == 1
    assert provider.sent[0]["template"] == "nudge"


# ── 4. a reply stops the rest ──

@pytest.mark.asyncio
async def test_a_reply_stops_the_remaining_steps(state):
    """Criterion 4: stop-on-reply, the same rule email follows."""
    pid = await _prospect(state)
    provider = FakeWasi()
    sender = _sender(state, _sequence_config(), provider)
    await sender.stage_whatsapp_opener(pid)
    rows = {r["step"]: r for r in await state.get_outbox(channel="whatsapp", limit=50)}
    long_ago = (_now() - datetime.timedelta(days=30)).isoformat()
    await state.update_outbox_item(rows[1]["id"], status="sent", sent_at=long_ago)
    await state.update_outbox_item(rows[2]["id"], status="approved",
                                   send_at=_now().isoformat())
    await state.update_prospect_status(pid, "replied")
    await sender._drain_whatsapp()
    assert provider.sent == []
    assert (await state.get_outbox_item(rows[2]["id"]))["status"] == "cancelled"


# ── 5. Claude writes the values ──

@pytest.mark.asyncio
async def test_claude_writes_the_values_and_they_are_what_is_sent(state):
    """Criterion 5: into the Outbox, approved, then out unchanged."""
    pid = await _prospect(state)
    provider = FakeWasi()
    config = _sequence_config(follow_ups=[], ai_params=["practice_name"])
    sender = _sender(state, config, provider)
    sender.brain = FakeBrain({"practice_name": "the Acme clinic on Oak St"})

    item_id, why = await sender.stage_whatsapp_opener(pid)
    assert item_id and not why
    row = await state.get_outbox_item(item_id)
    # What a human reads in the Outbox carries Claude's value...
    assert "the Acme clinic on Oak St" in row["body"]
    # ...and the first name still comes from the configured expression.
    assert row["body"].startswith("Hi Dana,")

    await state.update_outbox_item(item_id, status="approved")
    await sender._drain_whatsapp()
    assert len(provider.sent) == 1
    assert provider.sent[0]["params"]["practice_name"] == "the Acme clinic on Oak St"
    assert provider.sent[0]["params"]["customer_name"] == "Dana"


@pytest.mark.asyncio
async def test_the_prompt_carries_the_template_and_the_facts(state):
    pid = await _prospect(state)
    config = _sequence_config(follow_ups=[], ai_params=["practice_name"])
    sender = _sender(state, config, FakeWasi())
    brain = FakeBrain({"practice_name": "Acme"})
    sender.brain = brain
    await sender.stage_whatsapp_opener(pid)
    prompt = brain.prompts[0]
    assert TEMPLATE_BODY in prompt          # the words it may not change
    assert "practice_name" in prompt        # what it is filling
    assert "Acme" in prompt                 # the facts it must stay inside


@pytest.mark.asyncio
async def test_no_brain_falls_back_to_the_configured_expression(state):
    pid = await _prospect(state)
    config = _sequence_config(follow_ups=[], ai_params=["practice_name"])
    sender = _sender(state, config, FakeWasi())      # brain=None
    item_id, why = await sender.stage_whatsapp_opener(pid)
    assert item_id and not why
    assert (await state.get_outbox_item(item_id))["body"] == \
        "Hi Dana, saw Acme has no online booking."


@pytest.mark.asyncio
async def test_a_brain_that_raises_does_not_lose_the_message(state):
    pid = await _prospect(state)
    config = _sequence_config(follow_ups=[], ai_params=["practice_name"])
    sender = _sender(state, config, FakeWasi())

    class Boom(FakeBrain):
        async def think_json(self, prompt, **kwargs):
            raise RuntimeError("claude is down")
    sender.brain = Boom({})
    item_id, why = await sender.stage_whatsapp_opener(pid)
    assert item_id and not why
    assert "Acme" in (await state.get_outbox_item(item_id))["body"]


# ── 6. Meta's parameter rules ──

@pytest.mark.parametrize("value, word", [
    ("two\nlines", "line break"),
    ("a\tb", "tab"),
    ("a     b", "four spaces"),
    ("x" * 200, "characters"),
    ("", "empty"),
    ("{{1}}", "placeholder"),
])
def test_values_meta_would_reject_are_named(value, word):
    """Criterion 6, the rule itself."""
    assert word in template_param_problem(value)


def test_an_ordinary_value_is_fine():
    assert template_param_problem("the Acme clinic on Oak St") == ""
    assert template_param_problem("a    b") == ""      # four is allowed


@pytest.mark.asyncio
async def test_a_value_with_a_line_break_never_reaches_the_provider(state):
    """Criterion 6: Claude's answer is checked, and the fallback is used."""
    pid = await _prospect(state)
    provider = FakeWasi()
    config = _sequence_config(follow_ups=[], ai_params=["practice_name"])
    sender = _sender(state, config, provider)
    sender.brain = FakeBrain({"practice_name": "Acme\nClinic"})
    item_id, why = await sender.stage_whatsapp_opener(pid)
    assert item_id and not why
    body = (await state.get_outbox_item(item_id))["body"]
    assert "\n" not in body
    assert body == "Hi Dana, saw Acme has no online booking."   # the expression


@pytest.mark.asyncio
async def test_a_row_whose_stored_value_is_unsendable_is_held(state):
    """A row edited in the database, or queued before the rule existed."""
    import json

    pid = await _prospect(state)
    provider = FakeWasi()
    sender = _sender(state, _config(), provider)
    item_id, _ = await sender.stage_whatsapp_opener(pid)
    await state.update_outbox_item(item_id, status="approved")
    async with state._connect() as db:      # noqa: SLF001  (a hand-edited row)
        await db.execute(
            "UPDATE outbox SET params_json = ? WHERE id = ?",
            (json.dumps({"customer_name": "Dana", "practice_name": "A\tB"}), item_id))
        await db.commit()
    await sender._drain_whatsapp()
    assert provider.sent == []
    row = await state.get_outbox_item(item_id)
    assert row["status"] == "pending_review" and "tab" in row["error"]


# ── 7. a follow-up must be Marketing too ──

@pytest.mark.asyncio
async def test_a_utility_follow_up_is_refused_at_send(state):
    """Criterion 7: someone who never replied is still cold."""
    pid = await _prospect(state)
    provider = FakeWasi()
    config = _sequence_config(
        templates=[
            {"name": "cold_opener", "category": "Marketing", "language": "en_US",
             "body": TEMPLATE_BODY},
            {"name": "nudge", "category": "Utility", "language": "en_US",
             "body": FOLLOW_BODY},
        ],
        follow_ups=[{"template": "nudge", "delay_days": 3}],
    )
    sender = _sender(state, config, provider)
    await sender.stage_whatsapp_opener(pid)
    rows = {r["step"]: r for r in await state.get_outbox(channel="whatsapp", limit=50)}
    long_ago = (_now() - datetime.timedelta(days=30)).isoformat()
    await state.update_outbox_item(rows[1]["id"], status="sent", sent_at=long_ago)
    await state.update_outbox_item(rows[2]["id"], status="approved",
                                   send_at=_now().isoformat())
    await sender._drain_whatsapp()
    assert provider.sent == []
    row = await state.get_outbox_item(rows[2]["id"])
    assert row["status"] == "pending_review"
    assert "Marketing" in row["error"]


# ── 8. the double-send guard WhatsApp never had ──

@pytest.mark.asyncio
async def test_queueing_the_same_prospect_twice_queues_one_message(state):
    """Criterion 8."""
    pid = await _prospect(state)
    sender = _sender(state, _config(), FakeWasi())
    first, why = await sender.stage_whatsapp_opener(pid)
    assert first and not why
    second, why = await sender.stage_whatsapp_opener(pid)
    assert second == "" and why == "already queued"
    assert len(await state.get_outbox(channel="whatsapp", limit=50)) == 1


@pytest.mark.asyncio
async def test_a_finished_sequence_does_not_block_a_new_one(state):
    """The guard covers what is still waiting, not what already went."""
    pid = await _prospect(state)
    sender = _sender(state, _config(), FakeWasi())
    first, _ = await sender.stage_whatsapp_opener(pid)
    await state.update_outbox_item(first, status="sent", sent_at=_now().isoformat())
    second, why = await sender.stage_whatsapp_opener(pid)
    assert second and not why
    assert len(await state.get_outbox(channel="whatsapp", limit=50)) == 2
