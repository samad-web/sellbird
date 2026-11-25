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

All 18 findings from both passes are fixed. Two are worth remembering as
decisions rather than patches:

- **14** `CLAUDE.md` used to state flatly that Sellbird never reads LinkedIn
  because their terms forbid scraping, while the repo ships a 433-line
  Playwright scraper. The claim was corrected rather than the module deleted,
  so LinkedIn prospecting still exists: off by default, against LinkedIn's
  terms, and Claude is told never to enable it for the user.

### 2026-11-25 - The LinkedIn module stays
The user decided to keep `sellbird/integrations/linkedin.py` rather than delete
it. Don't propose deleting it again. The guardrails stand: off by default, and
Claude never enables it for them.
- **15** `--max-spend` now predicts the next query's cost as the worse of its
  estimate and what queries in this run have actually cost. The old check
  looked only at money already spent, so a provider that billed more than it
  estimated finished one whole query past the cap — $4.00 against a $3.00 cap
  in the repo's own test, which asserted the stop message but never the spend.

### 2026-11-25 — WhatsApp reached parity with email, and reply handling was already there
Phase 2 added the outbound sequence (`follow_ups`) and per-prospect template
parameters (`ai_params`). Reading replies needed nothing: `_process_reply` and
`_queue_native_reply` were channel-aware from phase 1. A cold WhatsApp
follow-up needs its own approved **Marketing** template, because someone who
never replied is still outside Meta's 24-hour window — that is a Meta rule, not
a Sellbird choice, so don't "simplify" it away.

### 2026-11-25 — Multi-project was already possible; only the ergonomics were missing
`SELLBIRD_HOME` has always isolated a project completely. `--workspace` is a
name-to-path registry in front of it, and the important part is the
`[workspace: …]` line every command prints: the risk with several projects is
not that they share data, it's acting on the wrong one.

Two traps worth remembering:
- Reading the registry imports `sellbird.paths`, which settles `PROJECT_ROOT` at
  import. Anything that resolves a workspace must recompute `paths.PROJECT_ROOT`
  after setting `SELLBIRD_HOME`. This only works because `cli.py` imports
  nothing from `sellbird` at module level — keep it that way.
- A test that calls `workspaces.remove(name)` without `path=` edits the user's
  real registry. Always pass a tmp path.

### 2026-11-25 — Email provider: Microsoft 365 over Graph, not Resend
The operator asked for Resend, already verified on their sending domain. Three
findings, in the order they mattered:

1. **Resend's Acceptable Use Policy names cold outreach as prohibited
   spamming**, alongside purchased lists and scraped data; mail must go to
   recipients who explicitly opted in. Sellbird is a cold outreach tool, so
   the first campaign would breach it. Stated remedy: account paused or
   terminated, no refunds.
2. **Resend has no IMAP.** Its inbound is webhook-based, and Sellbird runs
   locally behind NAT — the same wall the WhatsApp channel hit. Sending would
   work; reading replies would not.
3. **The reply mailbox is Microsoft 365** (root MX is Outlook), and Microsoft
   has turned off password sign-in for IMAP in Exchange Online. So even
   "Resend for sending, IMAP for replies" could not read that mailbox.

Decision: `channels.email.provider: microsoft` (Graph, OAuth). Resend stays
for whatever transactional mail it already does; Sellbird does not use it.

**Their Resend DNS is correct**, for the record, in case it is ever revisited:
DKIM at `resend._domainkey` on the root (so it aligns with a root From), SPF
and the bounce MX on the `send.` subdomain, so the root's `-all` doesn't break
it — SPF is checked against the envelope sender.

**Against advice, cold email goes out from the operator's main company
domain**, not a dedicated secondary one. They were told the risk (a complaint
spiral takes their real correspondence down with it) and chose it anyway.
Don't silently change it; do keep volume low and watch the bounce rate.

**No Reply-To support exists** in the codebase (`persona` has no `reply_to`,
and nothing sets the header). It was scoped as a small change if Resend-plus-
another-mailbox is ever wanted, and was not built.

### 2026-11-25 — Sender: a named person's own M365 mailbox
Briefly sent through a shared Google Workspace mailbox (Gmail provider, verified
end to end). The operator then chose their own named Microsoft 365 mailbox on
the same domain, for credibility: a person, not a support inbox. Provider back
to `microsoft`; persona and the email-rules sign-off use their name.
- M365 DKIM is **not enabled** for the domain (no `selector1._domainkey`), and
  enabling it needs a tenant admin. Not a blocker at this volume: SPF includes
  Outlook, so DMARC passes on SPF alignment. Enable it when an admin can.
- Cold emails offer a **call first**; the 7-day trial is given after the call,
  so cold emails link only the booking page (operator's decision).

### 2026-11-25 — What Sellbird sells here: Auratel
Auratel is the product, not the operator's agency. Train on Auratel's own site
so the emails pitch it. The agency domain is only the sending identity.
