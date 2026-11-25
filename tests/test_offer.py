"""Designing the first-email offer: suggestions, a preview, saving it."""

from types import SimpleNamespace

import pytest
import yaml

from sellbird.offer import save_offer, suggest_offers


def _config():
    return SimpleNamespace(
        product=SimpleNamespace(name="Brightline", description="Dental websites", pricing="$6,500",
                                key_benefits=["Online booking"], offer=SimpleNamespace(entry="", entry_ideas=[])),
        icp=SimpleNamespace(titles=["Practice owner"], industries=["Dental"], company_size="1-20"))


class Brain:
    def __init__(self, answer):
        self.answer, self.prompts = answer, []

    async def think_json(self, prompt, **kw):
        self.prompts.append(prompt)
        return self.answer


@pytest.mark.asyncio
async def test_suggestions_come_with_their_finding_and_why():
    brain = Brain([{"offer": "a homepage mockup with booking", "finding": "no online booking", "why": "they see it"},
                   {"offer": ""}, "junk"])
    ideas = await suggest_offers(brain, _config())
    assert ideas == [{"offer": "a homepage mockup with booking", "finding": "no online booking", "why": "they see it"}]
    assert "never a discount" in brain.prompts[0] and "Dental websites" in brain.prompts[0]


@pytest.mark.asyncio
async def test_suggestions_survive_a_wrapped_answer():
    ideas = await suggest_offers(Brain({"offers": [{"offer": "a ranking report"}]}), _config())
    assert [i["offer"] for i in ideas] == ["a ranking report"]


def test_saving_writes_the_offer_and_keeps_everything_else(tmp_path):
    f = tmp_path / "sellbird.local.yaml"
    f.write_text("persona:\n  name: Maya\nproduct:\n  name: Brightline\n  offer:\n    goal: book_call\n")
    save_offer(f, "  a written plan   for page one ", ["a plan", "", "a mockup"])
    data = yaml.safe_load(f.read_text())
    assert data["product"]["offer"] == {"goal": "book_call", "entry": "a written plan for page one",
                                        "entry_ideas": ["a plan", "a mockup"]}
    assert data["persona"]["name"] == "Maya"
    assert (tmp_path / "sellbird.local.yaml.bak").read_text().startswith("persona:")


# ── The preview is the email that would actually be sent ──

def _preview_fakes(monkeypatch, seen):
    import sellbird.offer as offer
    from sellbird.agents.writer import Writer

    async def write(self, prospect):
        seen["rules"] = self.email_rules
        return {"subject": "s", "body": "b"}

    async def keep(self, draft, step, names=()):
        return draft

    async def a_prospect(state):
        return SimpleNamespace(full_name=lambda: "Pat Doe", title="Owner", company="Example Co",
                               first_name="Pat", last_name="Doe")

    monkeypatch.setattr(Writer, "_write_personal_email", write)
    monkeypatch.setattr(Writer, "_keep_to_rules", keep)
    monkeypatch.setattr(offer, "preview_prospect", a_prospect)
    return offer


def _preview_config(email_rules: str):
    return SimpleNamespace(product=SimpleNamespace(offer=SimpleNamespace(entry="")),
                           writer=SimpleNamespace(email_rules=email_rules))


@pytest.mark.asyncio
async def test_the_preview_writes_with_the_operators_email_rules(tmp_path, monkeypatch):
    """With writer.email_rules set, the operator's rules replace the built-in
    shape. A preview written without them showed an email that is never sent:
    it used to blank them on purpose."""
    rules = tmp_path / "email_rules.md"
    rules.write_text("Sign off as Team Example. One link only.", encoding="utf-8")
    seen = {}
    offer = _preview_fakes(monkeypatch, seen)
    brain = SimpleNamespace(load_skills_for_agent=lambda name: "")

    result = await offer.preview_email(brain, None, _preview_config(str(rules)), "a free audit")
    assert result["ok"]
    assert seen["rules"] == "Sign off as Team Example. One link only."


@pytest.mark.asyncio
async def test_without_email_rules_the_preview_uses_the_built_in_shape(monkeypatch):
    seen = {}
    offer = _preview_fakes(monkeypatch, seen)
    brain = SimpleNamespace(load_skills_for_agent=lambda name: "")
    result = await offer.preview_email(brain, None, _preview_config(""), "a free audit")
    assert result["ok"]
    assert seen["rules"] == ""


@pytest.mark.asyncio
async def test_unreadable_email_rules_say_so_instead_of_previewing_the_wrong_shape(
        tmp_path, monkeypatch):
    """The Writer refuses to write when the rules file is set but missing, so
    the preview must not quietly fall back to a shape that would never send."""
    seen = {}
    offer = _preview_fakes(monkeypatch, seen)
    brain = SimpleNamespace(load_skills_for_agent=lambda name: "")
    missing = str(tmp_path / "nope.md")
    result = await offer.preview_email(brain, None, _preview_config(missing), "a free audit")
    assert result["ok"] is False
    assert "doesn't exist" in result["message"]
    assert "rules" not in seen          # never got as far as writing
