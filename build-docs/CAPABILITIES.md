# What Sellbird can do

Everything in this list is built and in the repo today. Commands assume
`source .venv/bin/activate` first. Anything marked **off by default** needs the
user to turn it on by hand.

---

## 1. Find companies

| Capability | How |
|---|---|
| Find businesses with no account or spend | `sellbird discover` (OpenStreetMap, free, unlimited) |
| Find local trades, clinics, contractors with phone + domain + rating | `discover --provider dataforseo_listings` ($0.372/1k) |
| Find businesses with **no website at all** | same provider, its no-website filter |
| Rank-based prospecting (positions 11-30 are the sweet spot) | `--provider dataforseo_serp` ($0.006/search at depth 30) |
| Map-pack rank, rating, claimed status | `--provider dataforseo_maps` ($0.002/search) |
| Easiest paid source to try (2,500 free, no card) | `--provider serper` |
| Free web search, no rank data | `--provider tinyfish` (30/min, 500/hr) |
| Google Maps listings + organic rank from one key | `--provider scavio_maps` / `scavio_search` (~$0.03 at depth 30) |
| Estimate spend before anything is called | `discover --estimate`, or the dashboard's Discover tab |
| Hard spend cap mid-run | `--max-spend 2.00` (stops before the query that would cross it) |
| Kill a running discovery | `POST /api/discover/stop`, or the dashboard button |
| YC-backed startups, from their own site | `discover --preset yc` |
| Techstars / Antler / EF / 500 / SPC / Neo / AI Grant / a16z speedrun / Plug and Play | `--preset accelerator` |
| Companies that announced a raise in the last 18 months | `--preset funding` |
| Companies hiring the roles you sell into | `--preset hiring` |
| Companies actively hiring, from job boards | `pip install python-jobspy` (optional) |
| Control the search radius per city | `icp.geo_coordinates: {"Denver, CO": "39.7392,-104.9903,50"}` |
| More search phrases per industry | `icp.keyword_variants` |

Cities geocode automatically and cache forever. Entity resolution is the
normalised domain, falling back to the provider's id for businesses with no site.

## 2. Learn about them (all free, no model calls)

- **Profile a company from 3 HTTP requests** — `sellbird profile`: homepage,
  robots.txt, sitemap.xml. No browser, no Claude.
- **29 signals**, each one proposed and inert until a human confirms it
  (`sellbird signals --confirm free`). Among them: `SERP_RANK`, `MAPS_RANK`,
  `IN_LOCAL_PACK`, `REVIEW_RATING`, `REVIEW_COUNT`, `UNCLAIMED_LISTING`,
  `NO_WEBSITE`, `NO_ONLINE_BOOKING`, `NO_SCHEMA_MARKUP`, `BLOG_STALE`,
  `SITE_PAGE_COUNT`, `RUNNING_GOOGLE_ADS`, `RUNNING_META_ADS`,
  `INCUMBENT_AGENCY`, `TECH_STACK`, `HIRING_ROLE`, `OPEN_ROLES`, `YC_BACKED`,
  `ACCELERATOR`, `FUNDING_ROUND`, `REGISTRY_VERIFIED`, `BLOCKS_AI_CRAWLERS`.
  Only three cost money.
- **Signals are observations, not columns** — a new signal needs no migration,
  re-observing over time is a free time series, and confidence + provenance
  travel with each fact.
- **Tech stack from the site's own HTML** — ~35 tools, zero extra requests.
- **Open roles from the real ATS feed** — Greenhouse, Lever, Ashby, Workable.
  One request, size-capped, never a guessed token. A board with no matching
  role records `HIRING_ROLE = 0`, so closed roles stop looking current.
- **Find the owner of a small business** — reads the homepage and follows the
  site's own menu to team/about/meet-the-doctor pages (6 max). Picks up owners
  from team cards, prose ("owned and operated by…") and schema.org, in English
  **and French**.
- **Build a cohort as a query over confirmed signals** — pick what a prospect
  must have, what disqualifies them, and ranges for numeric signals. Counts
  matches live; `state.cohort()` does the intersection in SQL.
- **One 0-100 fit score per company, with its reasons** — computed on read, so
  it is never stale.

## 3. Get contactable

- **Pattern-first email finding**: cache → scraped mailto (including addresses
  hidden from scrapers) → free Hunter domain lookup → default pattern, then
  verify *one* candidate instead of guessing many.
- **Four verifiers**: Reoon (600 free/mo), ZeroBounce (100 free/mo, best for
  M365/Workspace catch-alls), Hunter (50 free/mo), SMTP probe.
- **Every address is tagged** `verified` / `risky` (catch-all) / `guess` /
  `invalid`. **A `guess` is never sent.**
- **Phone numbers normalised to E.164**, including bracketed trunk zeros
  (`+44 (0) 20 …`). `sellbird whatsapp backfill-phones` fixes stored ones.
- **The prospect list is a product on its own** — `sellbird export` writes a CSV
  that drops straight into Instantly or Smartlead (deliverable only; `--all` for
  everything). Spreadsheet-formula injection is neutralised on write.
- **The company register** — `sellbird companies`: every company found, its fit
  score, and *why it was never contacted*.

## 4. Write

- **3-email sequences**: an observation, a new angle, a short breakup
  (<75 / <75 / <40 words), with a hard ban list on AI-sounding language.
- **Hard style checks in code**, not just in the prompt (`email_style.py`).
- **Your own email shape** — `skills/email_rules.md` + `writer.email_rules`
  replaces the built-in shape entirely.
- **Every rule is a markdown file you can edit**: `skills/` (10 of them: email
  frameworks, objection handling, lead qualification, sales methodology, offer
  strategy, signal playbook, selling to startups, prospecting tactics, account
  navigation, LinkedIn outreach) and `prompts/`.
- **The offer is the user's, not Claude's** — `offer_suggest` → `offer_preview`
  → `offer_propose`, then only the user activates it in the dashboard.
- **Startup-aware writing** — a confirmed `YC_BACKED` / `ACCELERATOR` /
  `FUNDING_ROUND` brings in `skills/selling_to_startups.md`. A raise older than
  18 months, or undated, is not written to as news.
- **Humanizer pass** — rewrites drafts through an installed Humanizer skill.
- **Claude-written drafts can be fact-checked in a real browser** — site claims
  ("no online booking") come from one automated page read, and Playwright is
  installed to verify them.
- **Runs on ChatGPT instead, if you want** — `brain.provider: codex` with Codex
  CLI 0.159.3, isolated, text-only, no tools. macOS/Linux only. Claude is the
  default and Sellbird never switches on its own.

## 5. Send — email

- **Four providers**: Gmail/Workspace (recommended, ~$7/mo), Microsoft 365 via
  Graph, any SMTP+IMAP mailbox, and Instantly (legacy).
- **Approval by default** — every email waits in the Outbox. `sellbird outbox`,
  `--approve <id>`, `--approve-all`, `--reject <id>`, or the dashboard tab where
  you can also **edit** a draft before approving.
- **Autopilot** — `require_approval: false`. Still bound by the daily cap, quiet
  hours and opt-outs, and it *still* holds any email that links to a domain
  that isn't yours or that quotes Sellbird's own instructions.
- **A deterministic pre-send gate** that no model can talk its way past:
  suppression list (address / domain / phone), email status, kill switch, daily
  cap, quiet hours, length, template match.
- **Send in the recipient's time zone** — `channels.email.send_window`.
- **Human-like pacing** when draining the queue, not a burst.
- **Several sending inboxes** — `sellbird inboxes`: each with its own daily cap
  and warmup ramp; new threads go to the inbox with the most room; once a thread
  starts, it never changes address; a paused inbox moves its unsent first emails
  elsewhere. Credentials live in the system keychain, never in SQLite.
- **One global kill switch** — `sellbird sending pause|resume`.
- **Bounce handling** — marks the address invalid, cancels that prospect's queued
  sends, and trips the kill switch past a trailing bounce-rate threshold
  (measured over the newest 200 sends, not all time).
- **Domain readiness, all free and read-only** — `sellbird warmup`: SPF, DKIM,
  DMARC, domain age via RDAP, DNS blocklists, and the ramp schedule.
- **Real warmup numbers** — TrulyInbox connected, `sellbird warmup` reads actual
  warmup days and spam rate. `sellbird trulyinbox other` if you use something else.
- **Per-recipient cold-email law check** (**off by default**) — Precleared tells
  you which rules apply to each new address across 14 countries before the first
  email. Anything not `ok_to_email` waits in the Outbox with the reason.
  `sellbird precleared-key` gets a free key without printing it.
- **Extension hooks on the send decision** — a plugin can add its own rule or
  its own headers (`send_hooks.py`).

## 6. Send — WhatsApp (**off by default**)

- Through **Wasi**, on Meta's official Cloud API. `docs/whatsapp.md` leads with
  what gets your number banned, not with setup.
- **Read Meta's own limits** — `sellbird whatsapp test` shows the live messaging
  limit and quality rating. Anything below GREEN pauses cold sending.
- **Templates** — `sellbird whatsapp templates` lists what Meta approved. A cold
  first touch *must* use an approved **Marketing** template; Sellbird refuses to
  send a message whose params don't match the template.
- **Same ladder as email** — Outbox approval (`sellbird outbox --channel
  whatsapp`), the pre-send gate, suppressions, its own daily cap.
- **Inbound by polling**, because Sellbird runs behind NAT and cannot take a
  webhook. No new Wasi endpoint needed.
- **Dead-number detection** — Meta's 131009/131026/131047/131051 mark a number
  unreachable instead of retrying it.
- **Cross-channel stop-on-reply** — a reply on either channel cancels queued
  messages on **both**. An opt-out on one suppresses the other.
- **`cold_first_touch` is a separate switch** and Claude is told never to turn it
  on for you.

## 7. Handle replies

- Polls the provider every cycle, dedups (Message-ID, falling back to
  sender + subject + IMAP UID + date), classifies intent, advances the
  conversation stage, and queues the response through the same approval ladder.
- **Reads without consuming** — `BODY.PEEK[]`, then flags `\Seen` only once the
  reply is actually handled. A reply Sellbird couldn't process is read again
  next cycle instead of being lost.
- **Stages**: `initial_outreach → engaged → qualifying → presenting →
  negotiating → closing → closed_won / closed_lost`.
- **Auto-reply detection** from headers, so an out-of-office isn't treated as
  interest.
- **Stop-on-reply at company level** — a reply from anyone at a company stops
  the sequence to everyone there.
- **"Said yes to your offer"** gets its own treatment: follow-ups stop, a short
  reply is drafted, and `offers_accepted` lists them.
- **Hand the yes to whatever delivers it** — one tool on another MCP server
  (`handoff` in config); whatever it returns, such as a link, goes into the draft.
- **Booking** — calendar link, suggested times, or ask-their-preference, from
  `product.offer`.
- **Everything from outside is fenced as data in the prompt** (`untrusted.py`),
  so a planted instruction on a website or in a reply is not an instruction.

## 8. Run it

- **Heartbeat loop** — `sellbird run`: quiet hours → budget → decide → act →
  log → sleep. Priority: send > write > prospect > idle (analyst).
- **Throttled against your real Claude quota** — reads subscription utilization
  the same way `/usage` does and leaves headroom for your own interactive use
  (`usage.max_daily_claude_percent`).
- **Per-call token attribution** for Sellbird's *own* calls only — it never
  scans your other Claude Code sessions. `sellbird usage`.
- **Dashboard** — `sellbird dashboard`, token in the URL fragment. Today,
  Signals, Companies, Contacts, Campaigns, Outbox, Conversations, Discover,
  Inboxes, Activity, Usage, Settings. Plain HTML/CSS/JS from disk, no build step.
- **MCP server for your coding agent** — `sellbird mcp`, 24 tools (today, setup
  status, stats, activity, usage, campaigns, prospects, companies, contacts, profiles,
  conversations, outbox, signals, discovery providers + estimate, sending status
  + pause, company, offer suggest/preview/propose, offers accepted, inboxes,
  inbox pause). Read-and-propose; the destructive paths stay out of its reach
  (`agent_policy.py`).
- **Menu-bar app (macOS) / tray app (Windows)**, with start-at-login.
- **Docker** — `docs/docker.md`.
- **Multi-workspace** — one process can serve several homes.
- **Extensions** — a Python entry point adds dashboard routes, heartbeat steps,
  CLI subcommands and mailbox connection types without patching Sellbird.
- **Analyst on idle cycles** — writes `data/analytics.json` with pipeline stats
  and insights.
- **Everything is in a local SQLite file** you can open with any tool:
  `data/sellbird.db`.
- **Local-network boundary on every outbound request to an untrusted site** —
  each request, redirects included, re-resolves and checks every address.
- **Secrets in the system keychain**, with `.env` as a read-only fallback. Logs
  redact bearer tokens and vendor keys.
- **Train on a product website** — `sellbird train <url>` writes
  `sellbird.yaml`, `skills/product_knowledge.md`, `skills/competitive_intel.md`.
  Optional Cloudflare key for deep JS crawling.
- **Harvey installs upgrade themselves** — `harvey.local.yaml` and
  `data/harvey.db` are renamed on the next command.

## 9. LinkedIn (**off by default**, and against LinkedIn's terms)

`channels.linkedin.enabled` + `LINKEDIN_EMAIL`/`LINKEDIN_PASSWORD` turns on a
Playwright module that signs in as you and runs people searches. Automated
searching and profile-viewing is what gets accounts restricted. Claude is told
never to enable it for you. Cookies are stored owner-only.
**Kept on purpose** — see MEMORY.md, 2026-11-25.

---

## What it deliberately will not do

- Scrape ycombinator.com, accelerator directories, Crunchbase or PitchBook —
  a company is tagged from what its **own** site says.
- Send an address tagged `guess`.
- Collect a signal a human hasn't confirmed.
- Switch brain providers on its own.
- Spend money on discovery without estimating first.
- Send a cold WhatsApp message without an approved Marketing template.
