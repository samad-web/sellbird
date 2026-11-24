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

`Verdict:` Healthy, and unusually careful — the sending path, the pre-send gate, the
dashboard's auth middleware and the outbox claim-then-send sequence are all written like
code that has already been burned once, with the reason recorded in a comment. Fix 1 first
(it blocks committing on Windows), then 2. Everything else is tidying.

`Lean: -~35 lines, -1 dependency (moved to an extra, not removed) possible.`

`Not checked:` I read the heartbeat loop, the whole Sender, the Handler's inbound and
bounce paths, the pre-send gate and autopilot review in full, the dashboard auth
middleware and the agent allowlist, the outbox half of `state.py`, and the Brain's
subprocess isolation. I did **not** read: `scout.py` (2236 lines), `writer.py`,
`trainer.py`, `codex.py` (1561), the collectors (`discover.py`, `people.py`,
`profile.py`, `ats.py`), `dashboard.py`'s 3000 lines of route bodies beyond the route
table and the auth layer, the web front-end (`sellbird/web/`), `mailboxes.py`,
`microsoft.py`, `gmail.py`, `smtp_mail.py`, `precleared.py`, or the 120 test files beyond
the one that failed. I did not run the app against a live mailbox, Instantly, Wasi or any
paid discovery provider, and I did not install Playwright, so nothing browser-driven ran.
