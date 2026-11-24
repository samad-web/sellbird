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
