# PLAN — audit fixes + WhatsApp channel (Wasi)

Status: **done** (approved and built 2026-11-24). Phase A and B1-B5 are committed on `fix/audit-findings` and `feat/whatsapp-channel`; results in `TESTING.md`, per-commit summary in `CHANGELOG.md`.

## Goal and scope

Two pieces of work, in this order:

1. **Fix what the audit found** (`build-docs/AUDIT.md`), starting with the one failing
   test, because the project rule is never to commit with a red suite.
2. **Add a WhatsApp channel** so Sellbird can reach a prospect on WhatsApp as well as by
   email, and keep a conversation going in whichever channel they answered in. Messages
   go through **Wasi** (`wasi.sirahagents.com`), the user's own WhatsApp CRM on Meta's
   Cloud API. Design: `build-docs/features/whatsapp-channel.md`.

**Out of scope:** SMS, Instagram/Messenger, voice, a WhatsApp inbox UI (Wasi already is
one), WhatsApp group messaging, media attachments, and any unofficial/QR WhatsApp
transport.

## Project memory this plan respects

From `build-docs/MEMORY.md`:

- Sellbird runs on the user's machine and **cannot receive webhooks** → inbound WhatsApp
  is polled, like Gmail history and IMAP.
- Nothing goes out without a human unless autopilot is explicitly on → WhatsApp uses the
  same outbox ladder, with `require_approval: true` by default.
- The repo is public → no real numbers, names or businesses anywhere in code, tests or
  docs. Test fixtures use `+15550100`-style numbers.
- Code and docs change in the same commit.

**What this plan changes:** it adds a second channel to a product whose config, gate,
sender and handler were written for one. That is an accepted widening of
`channels` from "email plus a disabled LinkedIn stub" to a real multi-channel surface.

## The disagreement, recorded

The user chose **cold first-touch on WhatsApp**. Advice given, and declined: Meta allows
a first message only as a pre-approved template, and unsolicited cold outreach gets the
number — and the WhatsApp Business Account behind it — banned. The user's decision stands
and is built. The design makes the risk slow and visible rather than preventing it
(template-only cold sends, a verified template match, a low daily cap, its own warmup
ramp, every send logged with its template). See the feature doc.

## Build order

Dependency-ordered. Each step is its own commit with its own tests.

### Phase A — audit fixes (no new features)

| # | Fix | Finding |
|---|---|---|
| A1 | Skip the symlink test on Windows | 1 |
| A2 | Count bounces from `actions`, over the same window as sends; fix the misleading config comment | 2, and part of 5 |
| A3 | `datetime.utcnow()` → `_utcnow()` in the Analyst | 3 |
| A4 | Correct the `dashboard_auth` docstring to point at `agent_policy` | 4 |
| A5 | De-duplicate `KILL_SWITCH_KEY`, `BOUNCE_COUNT_KEY`, `EMAIL_RE` and the deliverable-status set; delete the dead constant and the unused `brain` parameter; drop or fix the quiet-hours log helper | 5, 6, 7, 8 |

A1 ships first and alone, because every later commit depends on a green suite.

### Phase B — WhatsApp channel

| # | Step | Needs |
|---|---|---|
| B1 | `sellbird/phone.py` (E.164), `outbox.channel` column, `channels.whatsapp` config, channel-aware `pre_send_check`, phone entries in `do_not_contact` | nothing — no network, fully unit-testable |
| B2 | `WasiProvider.send` + channel routing in the Sender. Outbound only, template-verified | **Wasi send endpoint + auth** |
| B3 | `WasiProvider.get_replies` + Handler wiring + stop-on-reply across both channels | **Wasi polling endpoint** |
| B4 | Opt-out and suppression by phone number; "not on WhatsApp" handled like a bounce | B3 |
| B5 | Dashboard channel badges and filter, `sellbird whatsapp test/templates`, docs | B2, B3 |

B1 can start the moment this plan is approved. **B2 onwards is blocked** on the Wasi API
details listed at the end of the feature doc — above all whether Wasi can be *polled*
for inbound messages. Without that, Sellbird can send on WhatsApp but never hear back,
and finding 9's phone opt-out can't work either.

## What I need from you before B2

1. Wasi base URL and auth (header name, token format, where a key is issued).
2. Send a template message: method, path, request body, response (message id?).
3. Send a free-text session message: the same.
4. **Read inbound messages by polling** — e.g. `GET /messages?since=<cursor>`. If this
   doesn't exist, it's the one thing Wasi needs added.
5. List approved templates (name, language, body with placeholders).
6. Delivery/error statuses, and the error code for "number not on WhatsApp".
7. Rate limits, and whether there's a test number or sandbox.
8. Phone format expected (E.164 with or without `+`).

A `curl` example of a successful send and one inbound payload would cover most of this.

---

## Deliberately not built

Named here so nobody has to guess whether they were forgotten:

- **Cohort-wide WhatsApp queueing.** Openers are queued one prospect at a time
  (`sellbird whatsapp queue --prospect <id>`). Fanning a template out across a
  cohort is one command away, and it is the command that would get a number banned,
  so it waits for the user to ask.
- **WhatsApp in the Writer.** A cold opener is an approved template with its
  placeholders filled, so there is nothing for the model to write. Free-text
  *replies* already go through the Writer's reply path.
- **Media, buttons and list messages.** Wasi supports interactive sends
  (`type: "interactive"`, 1-3 buttons, or a list); Sellbird sends text and templates
  only.
- **A WhatsApp tab on the dashboard.** The Outbox, Conversations and Activity tabs
  now mark the channel; Wasi is itself a WhatsApp inbox, so a second one here would
  be a worse copy of it.
- **Audit finding 10** (Playwright as a hard dependency). Left alone: `CLAUDE.md`
  relies on Playwright being present so Claude can open a prospect's site and check
  a draft's claims. Moving it to an extra would need the setup wizard to install it
  by default, which is a bigger change than the finding is worth.

## Still open

- **The one thing only the user can do:** get an approved **Marketing** template
  suitable for a cold opener. All four current Marketing templates are post-demo or
  post-sale follow-ups (missed-call, post-demo, won,
  lost), so `cold_first_touch` cannot be used yet — by design, since
  the Sender refuses a non-Marketing template for a first message.
- Three gaps in Wasi itself, listed at the end of `features/whatsapp-channel.md`:
  no `updated_after` filter on `/conversations` (so nothing past the newest 100 is
  readable), template parameters not validated before the Meta call, and
  `client_id` required in the send body although the API key already identifies the
  client.
