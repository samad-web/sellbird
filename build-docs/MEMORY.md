# Project memory — Sellbird

Read at the start of every session. Append decisions, don't rewrite.

## Constraints that shape every change

- **Public repo.** No real person's or business's name, email, phone or address in code,
  tests, docs, commits or PR text. Fictional `example.com` / E.164 `+1555...` data only.
  Live test results go in the chat as counts, never into a file. (`CONTRIBUTING.md`)
- **Sellbird runs locally**, on the user's machine, behind NAT. It cannot receive
  webhooks. Every inbound integration polls (Gmail history, IMAP, Instantly).
- **Nothing outbound without a human**, unless the user explicitly turned autopilot on.
  The outbox ladder (`pending_review → approved → sending → sent`) is the only send path.
- **Facts are observations, not columns** (`observations` table, governed `signal_codes`).
  A new signal needs no migration; only a human sets a signal to `confirmed`.
- Code and docs must agree in the same commit.

## Decisions

### 2026-11-24 — WhatsApp is a second channel on the existing outbox, not a parallel pipeline
The `outbox` table already carries recipient, body, status ladder, provider ids, thread
refs and the pre-send gate. A WhatsApp channel adds one column (`channel`) and one
provider adapter rather than a second Sender/Handler. Decided for the smallest complete
diff; revisit only if WhatsApp needs state email doesn't have.

### 2026-11-24 — Inbound WhatsApp must be polled, not pushed
Wasi's docs mention webhooks it configures with Meta. Sellbird has no public URL, so a
webhook cannot reach it. The `MailProvider.get_replies()` interface is already a poll.
**Resolved 2026-11-24:** no `since` filter exists, but the poll works without one:
`GET /api/v1/conversations?limit=100` (max 100, `offset`/`since`/`page` all ignored)
carries `last_inbound_at` per thread, so a stored cursor finds the threads with new
inbound, then `GET /api/v1/conversations/{id}/messages` reads them. A tunnel
(ngrok/cloudflared) was rejected: it puts a public ingress on a user's laptop.

### 2026-11-24 — User chose cold WhatsApp first-touch, against advice
Advised against it: Meta allows a first message only as a pre-approved *template*, and
unsolicited cold outreach gets the number and the WABA banned. User confirmed cold
first-touch anyway. Consequences accepted and designed for:
- the first message is a template send, never free text;
- the rendered text is verified against the configured template before it leaves;
- the cold opener must use a `Marketing`-category template, never a Utility one;
- the daily cap is `min(config cap, GET /account → messaging_limit.remaining)`, and a
  `quality_rating` other than GREEN pauses cold sending — real Meta numbers, so the
  hand-rolled warmup ramp in the first draft was dropped;
- every cold WhatsApp send is logged with the template used.

### 2026-11-24 — Phone numbers come from data Sellbird already collects
`prospects.phone`, `prospects.phone_verified` and `companies.phone` already exist, and
the DataForSEO Business Listings and OpenStreetMap discovery providers already fill
company phones. No new collector.

## Known issues (see build-docs/AUDIT.md)

- `bounce_count` is a lifetime counter compared against an outbox-only send count, so
  switching Instantly → a native provider can trip the global kill switch at the first
  10 sends.
- `KILL_SWITCH_KEY`, `BOUNCE_COUNT_KEY` and `EMAIL_RE` are each defined twice; the
  deliverable-email-status rule exists under two names in two modules.
- `dashboard_auth.py`'s docstring points at `dashboard.require_user`, which does not
  exist (the real enforcement is `sellbird/agent_policy.py`).
