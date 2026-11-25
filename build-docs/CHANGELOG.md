# Build changelog

What was built, fixed and committed, per feature. The product-facing changelog is
`CHANGELOG.md` at the repo root; this one is the build log.

## Unreleased

- 2026-11-24 — `build-docs/` created. Baseline audit (`AUDIT.md`), project memory
  (`MEMORY.md`), plan (`PLAN.md`) and the WhatsApp channel design
  (`features/whatsapp-channel.md`) written. No code changed yet.

- 2026-11-24 — **Phase A: audit fixes** (`ffcf64c`, `36e9c58`). The Windows-only
  test failure that blocked every commit; the bounce kill switch that could pause
  all sending on the tenth native email after switching provider; the duplicated
  send rules, the dead constant, the unused parameter, the DST-wrong log helper,
  the deprecated `utcnow()`, and two docs that described code that didn't exist.
- 2026-11-24 — **Phase B: WhatsApp channel through Wasi** (`271bd20`, `195217b`,
  `2a2a319`, `38d331e`). One outbox column and one provider adapter, not a second
  pipeline. Off by default. Cold sends are template-only, verified against Meta's
  approved text, capped by Meta's own 24-hour allowance, and stopped when the
  number's quality rating drops. Opt-outs cross channels. 107 new tests.

## WhatsApp phase 2 — parity with email, and intelligence (2026-11-25)

**Built**
- `channels.whatsapp.follow_ups`: a cold sequence, each step its own approved
  Marketing template, staged with the opener at cumulative delays.
- `channels.whatsapp.ai_params`: the placeholders Claude writes per prospect,
  from the same facts the first email uses. Checked against Meta's parameter
  rules; falls back to the configured expression.
- `outbox.params_json` (migration v18): the values as approved are the values
  that go to Meta.
- `GET /api/whatsapp` and a WhatsApp card in dashboard Settings: the number's
  live quality rating and allowance, the caps, and the sequence with each
  template's category.
- `gate.template_param_problem()`: Meta's parameter rules as a pure function.
- `sellbird whatsapp queue` now builds a brain when `ai_params` is set, so the
  CLI path is personalised too, and reports how many follow-ups it staged.

**Fixed**
- WhatsApp rows had **no double-send guard**: `uq_outbox_campaign_step` only
  covers rows with a campaign id, and WhatsApp rows have none, so queueing a
  prospect twice queued two openers. `uq_outbox_whatsapp_step` covers the steps
  still waiting.
- `_follow_up_order` rescheduled into email's send window whatever the channel.

**Already at parity, so untouched:** reading replies. `_process_reply` and
`_queue_native_reply` were channel-aware from phase 1 — inbound WhatsApp is
classified, staged and answered by the same code that answers email.
