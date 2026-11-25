"""The dashboard's WhatsApp card (criterion 9).

Read-only: it reports what is configured and what Meta currently allows, and
it never carries a key. Turning the channel on is a config-file decision.
"""

import pytest

import sellbird.dashboard as dash
from sellbird.integrations import mail_provider
from tests.test_dashboard_live_status import client, env  # noqa: F401  (fixtures)

CONFIG = """
persona:
  name: Alex
  email: alex@example.com
  company: Testco
  role: BD
  linkedin: ""
  tone: direct
product:
  name: P
  description: d
  pricing: "$1"
  key_benefits: [b]
  objection_responses: {}
icp:
  industries: [dentistry]
  titles: [Owner]
  company_size: "1-10"
  geography: ["Denver, CO"]
channels:
  whatsapp:
    enabled: true
    cold_first_touch: true
    cold_template: cold_opener
    max_daily_sends: 20
    ai_params: [practice_name]
    templates:
      - name: cold_opener
        category: Marketing
        language: en_US
        body: "Hi {{customer_name}}, saw {{practice_name}} has no online booking."
      - name: nudge
        category: Utility
        language: en_US
        body: "Hi {{customer_name}}, still worth a look?"
    template_params:
      customer_name: "{{first_name}}"
    follow_ups:
      - template: nudge
        delay_days: 3
"""


@pytest.fixture
def configured(tmp_path, monkeypatch):
    path = tmp_path / "sellbird.yaml"
    path.write_text(CONFIG, encoding="utf-8")
    monkeypatch.setattr(dash, "_resolve_config_file", lambda: path)
    return path


class FakeWasi:
    def __init__(self, account=None, raises=None):
        self._account = account or {}
        self._raises = raises

    async def account(self):
        if self._raises:
            raise self._raises
        return self._account


def test_the_card_says_off_when_the_channel_is_off(client):
    """The repo's own config: WhatsApp is off, and nothing is called."""
    data = client.get("/api/whatsapp").json()
    assert data["enabled"] is False
    assert data["number"] == {}
    assert data["problem"] == ""


def test_it_reports_the_sequence_and_which_steps_can_send(client, configured, monkeypatch):
    monkeypatch.setattr(mail_provider, "get_whatsapp_provider",
                        lambda c, e: FakeWasi({
                            "display_phone_number": "+1 555 010 0123",
                            "verified_name": "Testco",
                            "quality_rating": "GREEN",
                            "messaging_limit": {"tier": "TIER_1K", "remaining": 940},
                        }))
    data = client.get("/api/whatsapp").json()
    assert data["enabled"] and data["configured"] and data["cold_first_touch"]
    assert data["number"]["quality_rating"] == "GREEN"
    assert data["number"]["limit_remaining"] == 940
    assert data["ai_params"] == ["practice_name"]
    steps = data["sequence"]
    assert [s["step"] for s in steps] == [1, 2]
    assert steps[0]["category"] == "Marketing" and steps[0]["delay_days"] == 0
    # The follow-up is a Utility template, which can't open a cold thread.
    # The card shows the category so the user can see that before sending.
    assert steps[1]["category"] == "Utility" and steps[1]["delay_days"] == 3
    assert all(s["approved"] for s in steps)


def test_a_step_with_no_stored_template_is_marked_unapproved(client, configured,
                                                             monkeypatch):
    monkeypatch.setattr(mail_provider, "get_whatsapp_provider",
                        lambda c, e: FakeWasi({"quality_rating": "GREEN"}))
    text = configured.read_text(encoding="utf-8").replace(
        "cold_template: cold_opener", "cold_template: missing_one")
    configured.write_text(text, encoding="utf-8")
    steps = client.get("/api/whatsapp").json()["sequence"]
    assert steps[0]["template"] == "missing_one"
    assert steps[0]["approved"] is False and steps[0]["category"] == ""


def test_wasi_being_unreachable_is_a_problem_not_an_error(client, configured,
                                                          monkeypatch):
    monkeypatch.setattr(mail_provider, "get_whatsapp_provider",
                        lambda c, e: FakeWasi(raises=RuntimeError("timed out")))
    data = client.get("/api/whatsapp").json()
    assert data["enabled"] and data["configured"]
    assert "timed out" in data["problem"]
    assert data["number"] == {}          # everything else still readable
    assert data["sequence"][0]["template"] == "cold_opener"


def test_no_key_says_so_without_calling_anything(client, configured, monkeypatch):
    monkeypatch.setattr(mail_provider, "get_whatsapp_provider", lambda c, e: None)
    data = client.get("/api/whatsapp").json()
    assert data["configured"] is False
    assert "WASI_API_KEY" in data["problem"]


def test_the_answer_carries_no_key(client, configured, monkeypatch):
    monkeypatch.setattr(mail_provider, "get_whatsapp_provider",
                        lambda c, e: FakeWasi({"quality_rating": "GREEN"}))
    body = client.get("/api/whatsapp").text.lower()
    assert "wasi_" not in body and "api_key" not in body and "token" not in body


def test_the_coding_agent_may_read_it():
    from sellbird.agent_policy import agent_may

    assert agent_may("GET", "/api/whatsapp")
