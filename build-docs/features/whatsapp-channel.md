# Feature: WhatsApp channel (Wasi)

Status: **awaiting approval + Wasi API details**

## Purpose

Sellbird can only reach a prospect by email. A second channel lets it (a) open a
conversation on WhatsApp, where reply rates in trades, clinics and local services are
far above email, and (b) keep a conversation going in whichever channel the prospect
answered in. The channel sends through **Wasi** (`wasi.sirahagents.com`), the user's own
WhatsApp CRM on Meta's Cloud API.

## Decision the user already made, and its cost

The user chose **cold first-touch on WhatsApp**. Meta permits a first message only as a
pre-approved **template**; free text is allowed only inside a 24-hour window opened by
the prospect's own message. Unsolicited cold templates draw blocks, and enough blocks
cost the number and the WhatsApp Business Account. This design does not prevent that —
the user accepted the risk — but it makes it visible and slow:

- cold = template send only, never free text;
- the rendered text is checked against the configured template body before sending;
- WhatsApp gets its own daily cap (default 20, not 50) and its own warmup ramp;
- Sellbird records the template used on every cold send, so a block wave is traceable.

## Architecture

### Reuse, not a second pipeline

| Already exists | What WhatsApp uses it for |
|---|---|
| `outbox` table + status ladder | Every WhatsApp message queues, waits for approval, sends |
| `gate.pre_send_check` | Deterministic last check before a message leaves |
| `gate.autopilot_review` | Holds a message carrying a planted link / number / money ask |
| `suppressions`, `do_not_contact` | Opt-outs and the never-contact list, extended to phones |
| `Sender._drain_due` | Daily cap, pacing, claim-then-send, stop-on-reply |
| `Handler._read_inbox` / `_process_reply` | Intent, stage, auto-reply through the outbox |
| `conversations` | One thread per prospect, whatever channel it runs in |
| `prospects.phone`, `companies.phone` | The numbers; already collected by discovery |

### New code

```
sellbird/integrations/wasi.py      WasiProvider(MailProvider): send, poll, test
sellbird/phone.py                  E.164 normalisation + validation (no dependency)
```

### Changed code

- **`sellbird/state.py`** — migration: `ALTER TABLE outbox ADD COLUMN channel TEXT
  DEFAULT 'email'`. `add_outbox_item(channel=...)`, `get_outbox(channel=...)`,
  `count_outbox_sent_today(channel=...)`, new `get_prospect_by_phone()`.
  The partial unique index stays as it is; a WhatsApp opener is `kind='sequence'`,
  `step=1`, so a campaign cannot queue an email and a WhatsApp opener under the same
  (campaign, prospect, step) slot — see *Edge cases*.
- **`sellbird/config.py`** — `WhatsAppChannelConfig` under `channels.whatsapp`:
  `enabled` (False), `provider` ("wasi"), `max_daily_sends` (20), `require_approval`
  (True), `cold_first_touch` (False — the user turns it on deliberately),
  `templates` (`[{name, language, body}]`), `default_country` (for bare local numbers),
  `warmup`, `send_window`.
- **`sellbird/gate.py`** — `pre_send_check(..., channel="email")`. On `"whatsapp"`:
  recipient must be E.164; no postal-address requirement; body capped at 700 chars
  (Meta's limit is 4096, cold messages should be far shorter); banned phrases, merge
  tags, link budget, do-not-contact and HTML checks all still apply. New
  `template_mismatch(body, template)` for cold sends.
- **`sellbird/agents/sender.py`** — `self.providers: dict[str, MailProvider]` keyed by
  channel. `_drain_due` picks the provider by `item["channel"]`; the footer, signature,
  Precleared check and email warmup stay behind `channel == "email"`.
- **`sellbird/agents/handler.py`** — reads the WhatsApp provider alongside the mailboxes;
  an inbound message resolves its prospect by phone; a reply stops the sequence in
  **both** channels (stop-on-reply is per prospect and per company, not per channel).
- **`sellbird/dashboard.py` + `sellbird/web/`** — a channel badge on Outbox,
  Conversations and Activity rows, and a channel filter on Outbox. No new page.
- **`sellbird/cli.py`** — `sellbird whatsapp test` (connection), `sellbird whatsapp
  templates` (what Wasi has approved), `sellbird outbox --channel whatsapp`.

### Data flow (cold opener)

```
prospect with phone + confirmed signal
  → Writer renders the configured template's variables (no free text)
  → outbox row (channel='whatsapp', kind='sequence', step=1, status=pending_review)
  → user approves (or autopilot)
  → Sender: kill switch → stop-on-reply → gate → template match → Wasi send
  → Wasi → Meta Cloud API → prospect's WhatsApp
prospect replies
  → Handler polls Wasi → matches phone to prospect → classifies intent
  → cancels queued email AND WhatsApp follow-ups → drafts a reply (free text, inside
    the 24h window) → outbox (channel='whatsapp', kind='reply')
```

## Impact map

| Touched | What could break |
|---|---|
| `outbox` schema | Old rows get `channel='email'` by default. A downgrade to an older Sellbird ignores the column (SQLite tolerates it); the partial unique index is unchanged. |
| `gate.pre_send_check` | Its signature gains a keyword with an email default. Every existing caller (sender, handler, tests) keeps its behaviour. **Risk:** a missed caller sending WhatsApp through the email branch would reject a phone as an invalid address — fails closed, so safe. |
| `Sender._drain_due` | The hottest, most-tested path in the repo. **Risk:** a channel-keyed provider lookup that returns None must hold the row, never send it to the email provider. |
| `Handler._read_inbox` | A failing Wasi poll must mark the cycle incomplete exactly as a failing mailbox does, or follow-ups go out to someone who already replied. |
| `count_outbox_sent_today` | Called by the email daily cap and the warmup ramp. **Risk:** if WhatsApp sends are counted in the email cap, email volume silently drops. Must filter by channel; the unfiltered call stays the global ceiling. |
| `suppressions` | A new `kind='phone'`. `suppressed_among()` does domain-parent expansion on addresses; phones must not go through that path. |
| `do_not_contact` entries | `do_not_contact_entry()` currently returns "" for anything that isn't an address or domain, and `do_not_contact_entries()` **raises** on it. A phone number in the list would raise a config error today → it must be recognised, or existing configs break. |
| `export.py`, `sellbird companies` | CSV columns are a published contract. Adding phone/channel columns at the end only. |
| Precleared check | Email-only. WhatsApp must not consume Precleared allowance. |
| `agent_policy.py` | Any new dashboard route is the user's alone until explicitly listed. No new agent routes. |

## Edge cases and failure modes

**Input**
- Bare local number ("555 0142"), number with extension, number that is a landline:
  normalise with `default_country`; anything that isn't valid E.164 never queues, and the
  prospect's `phone_status` becomes `invalid`.
- A number that is not on WhatsApp: Wasi/Meta returns an error → mark `phone_status`
  `invalid`, cancel queued WhatsApp rows, leave email untouched.
- Same number on two prospects (shared company line): treat like the existing
  company-inbox prospect — one conversation per number, and a reply stops the whole
  company.

**Empty states**
- `channels.whatsapp.enabled: false` (the default): nothing in this feature runs, no
  Wasi call, no new outbox rows.
- No `WASI_API_KEY`: the channel reports not-configured and holds, exactly as an
  unconfigured mailbox does. It never falls back to email.
- No approved template: cold sending is refused with a message naming the fix. Replies
  (free text, inside the window) still work.

**Concurrency**
- `claim_outbox_item` already makes approved→sending atomic; a WhatsApp row is claimed
  the same way, so the dashboard and the loop cannot double-send.
- A reply arriving mid-drain: the existing post-claim re-check (`_blocked` with fresh
  data) covers it.
- Two channels, one prospect: a reply on either cancels queued rows on both. Needs the
  cancel query to stop filtering by channel — verify, don't assume.

**Network**
- Wasi down or 5xx: poll failure marks the cycle incomplete (sending held); send failure
  is treated as *maybe sent* and never retried, like an email send that raised.
- Wasi returns 200 but no message id: treat as uncertain, never retry.

**Security**
- The Wasi key lives in `.env` (`WASI_API_KEY`), never in the config file, never in
  SQLite, never logged. `logsafety` redaction list gains it.
- Inbound WhatsApp text is untrusted and reaches the model: it goes through the same
  `sellbird.untrusted` fencing and the same `autopilot_review` as email replies. A
  planted "message this number instead" holds the draft.
- Wasi is called through the normal integration client (a fixed, trusted API), not
  `public_http` — matching how Gmail and Instantly are called.

## Acceptance criteria

1. With `channels.whatsapp.enabled: false`, no Wasi request is made and no WhatsApp
   outbox row is created. (regression: the whole existing suite still passes)
2. A WhatsApp row queues as `pending_review` and is not sent until approved.
3. `pre_send_check` rejects a non-E.164 recipient on the whatsapp channel, and accepts a
   valid one without demanding a postal address.
4. `pre_send_check` on the email channel behaves byte-identically to today.
5. A cold WhatsApp send whose rendered body does not match a configured template is
   refused, with the template name in the reason.
6. WhatsApp sends count against `channels.whatsapp.max_daily_sends` and **not** against
   `channels.email.max_daily_sends`; the global ceiling still holds.
7. An inbound WhatsApp message matches its prospect by phone, cancels that prospect's
   queued rows **in both channels**, and drafts one reply for approval.
8. A failing Wasi poll holds sending for that cycle and says why on Today.
9. A bounce-equivalent (number not on WhatsApp) marks the phone invalid, cancels queued
   WhatsApp rows, and leaves the email sequence alone.
10. `WASI_API_KEY` never appears in a log line, an action row or a CSV export.
11. A phone number in `compliance.do_not_contact` is honoured and does not raise a
    config error.
12. An opt-out word in an inbound WhatsApp message suppresses the prospect across both
    channels.

## Build order

1. `sellbird/phone.py` + the `channel` column + config model + `gate` channel branch.
   (no network, fully unit-testable)
2. `WasiProvider.send` + Sender channel routing. Outbound only.
3. `WasiProvider.get_replies` + Handler wiring + cross-channel stop-on-reply.
4. Phone opt-out / suppression / do-not-contact by number.
5. Dashboard badges + CLI commands + docs.

## The Wasi API, as discovered 2026-11-24

Wasi publishes no API docs. This was mapped by probing the live API with the user's key.
Every probe used an invalid recipient (`"not-a-number"`), so no message reached anyone.

**Base:** `https://wasi.sirahagents.com/api/v1` · **Auth:** `Authorization: Bearer <key>`
(`WASI_API_KEY` in `.env`). Errors are `{"error":{"code","message","details"?}}`.
`GET /health` (unauthenticated) returns `{status, db, timestamp}`.

| Route | Returns / takes |
|---|---|
| `GET /account` | `client_id`, `connected`, `status`, `phone_number_id`, `waba_id`, **`quality_rating`**, **`messaging_limit`** `{tier, unlimited, limit_per_24h, used_last_24h, remaining, checked_at}` |
| `GET /templates` | 23 approved templates: `name`, `language` (`en_US`), `category` (Utility/Marketing/Authentication), `status`, `body`, `body_param_examples` (named keys), `buttons`, `header_*`, `footer_text`, `meta_template_id` |
| `GET /contacts` | `phone`, `wa_id`, `name`, `status`, **`opt_in_status`**, `opt_in_source`, `opt_in_at`, **`opt_out_at`**, **`whatsapp_blocked_at`** |
| `GET /conversations` | `id`, `contact_id`, `phone`, `last_message_at`, **`last_inbound_at`**, `last_agent_reply_at`, `unread_count`, `status`, `ai_muted` |
| `GET /conversations/{id}/messages` | `id`, `chat_id`, `direction`, `body`, `sent_at`, `meta_message_id`, `status`, `error_reason`, `meta_error_code`, `delivered_at`, `read_at`, `failed_at`, `source`, `reply_to_message_id` |
| `POST /messages` | `{client_id, to, type}` + per type: `type:"text"` needs `body`; `type:"template"` needs `template` (**the template *name*, a string**); `type:"interactive"` needs 1-3 `buttons`, or `body`+`button`+`sections`, never both |

**Phone format:** E.164 with a leading `+` — Meta's own error says
`"Please use the format: +1234567890."`

### What this changes in the design

1. **No webhook needed, and no new endpoint needed either.** The poll is
   `GET /conversations?limit=100`, keep the ones whose `last_inbound_at` is newer than a
   stored cursor, then `GET /conversations/{id}/messages` for each. The message rows map
   one-to-one onto `InboundMessage`: `provider_id`←`id`, `thread_ref`←`chat_id`,
   `message_id`←`meta_message_id`, `body`←`body`, direction `in` only.
2. **Wasi enforces Meta's 24-hour window itself.** A `type:"text"` send outside it is
   refused with `{"code":"session_window_closed"}`. Sellbird does not track the window:
   it sends free text, and on that code holds the row and says a template is needed.
   This deletes the whole window-tracking design.
3. **The daily cap comes from Meta, not from a config guess.** `GET /account` gives
   `messaging_limit.remaining` and `quality_rating`. The WhatsApp cap is
   `min(config cap, messaging_limit.remaining)`, and a `quality_rating` that is not
   `GREEN` pauses cold sending. This replaces the hand-rolled warmup ramp in the earlier
   draft — real numbers beat a guessed curve.
4. **Cold openers must use a `Marketing` template.** Only 4 of the 23 approved templates
   are Marketing; 18 are Utility. Sending cold outreach on a Utility template is a policy
   violation Meta punishes by re-categorising or rejecting the template. Sellbird refuses
   a cold send on a non-Marketing template, naming the category in the reason.
5. **Opt-in/opt-out is already tracked upstream.** `GET /contacts` carries
   `opt_in_status`, `opt_out_at` and `whatsapp_blocked_at`. The poll syncs those into
   Sellbird's existing `suppressions` table, so a block or opt-out in Wasi (or in
   WhatsApp itself) stops Sellbird's email too.
6. **Placeholders are named `{{customer_name}}`** — the same syntax `Sender._render`
   already fills. Template rendering reuses the existing renderer.
7. **Meta's errors come through raw**, as `{"code":"send_failed", "metaError":{code,
   error_data.details}}`. Map `131009` and the "number is malformed"/not-on-WhatsApp
   family onto the existing bounce path: mark the phone invalid, cancel queued WhatsApp
   rows, leave email alone.

### Template parameters — confirmed by a live send

`params`, an **object keyed by placeholder name**, alongside the template name:

```json
POST /api/v1/messages
{ "client_id": "<uuid>", "to": "+<E.164>", "type": "template",
  "template": "<approved name>", "params": { "customer_name": "Dana" } }
```

Positional placeholders use their number as the key (`{"1": "482913"}`). An array is
refused (`params: Expected object, received array`). Wasi does check arity for
Authentication templates (`auth_code_invalid`), but not for the others.

**Success response** is the full message row, which is what the provider needs:

```json
{ "id": "<uuid>", "chat_id": "<conversation uuid>", "direction": "out",
  "body": "[template: <name>]", "sent_at": "...", "status": "sent",
  "meta_message_id": "wamid....", "source": "api", "api_key_id": "<uuid>",
  "delivered_at": null, "read_at": null, "meta_error_code": null }
```

So `SendResult.message_id` ← `meta_message_id`, `SendResult.thread_ref` ← `chat_id`.
A send that Wasi accepts but Meta rejects comes back as
`{"code":"send_failed","metaError":{...}}` — that is the `uncertain`/failed path, never a
retry.

Note that an outbound template's stored `body` is the literal string
`"[template: <name>]"`, **not** the rendered text. So Sellbird cannot reconstruct what a
prospect actually read from Wasi's message rows; it must keep its own rendered copy in
the outbox row (which it already does). This also means the `OurText` quote-stripping the
Handler uses to recognise its own words in a reply must be fed from the outbox, not from
Wasi.

### Gaps worth fixing in Wasi itself (user's own product)

- `GET /conversations` ignores `since`, `after`, `updated_after` and `page`, and `offset`
  appears to be ignored too; `limit` caps at 100. So there is no way to read past the
  newest 100 conversations, and every poll re-fetches them all. An `updated_after`
  parameter would make Sellbird's poll (and any other integration's) cheap and complete.
- Template params are not validated against `body_param_examples` before the Meta call,
  so a missing parameter surfaces as a raw Meta `#132000`-class error after a failed send
  attempt rather than a clean `validation_failed`.
- `POST /messages` requires `client_id` in the body although the API key already
  identifies the client — easy to get wrong, and it is not obvious it must match the key.
