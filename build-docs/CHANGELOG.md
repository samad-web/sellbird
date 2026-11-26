# Build changelog

What was built, fixed and committed, per feature. The product-facing changelog is
`CHANGELOG.md` at the repo root; this one is the build log.

## Unreleased

- 2026-11-26 — **fix(deploy):** `install.sh` now writes a service that can run
  `claude`. The CLI is put on the unit's PATH (`~/.npm-global/bin`), and
  `ReadWritePaths` covers the whole home, because claude writes `~/.claude` and
  `~/.claude.json`. The sign-in hint now says `npm config set prefix` and
  `claude auth login`.

- 2026-11-26 — **Branded email style** (`signature.style: branded`). Opt-in
  layout: accent strip, logo + company header, the letter, a booking button on
  cold emails (`signature.button` → `product.offer.booking_url`), dark footer band.
  Default `letter` unchanged. Design: `features/branded-email.md`.

- 2026-11-25 — **Signature footer** (`sellbird/integrations/signature.py`). With a
  logo set, a cold email's compliance footer sits under the logo behind a thin
  `accent`-coloured rule, in small grey type; body type refined (15px, 560px
  column). Design: `features/signature-footer.md`.

- 2026-11-25 — **CSV import** (`sellbird import`, `sellbird/importer.py`). Loads a
  lead list as companies + `new` prospects; addresses graded by the verifier, not the
  file; dedup in-file and against the DB; `--dry-run`. Design: `features/csv-import.md`.

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

## Multiple projects from one install (2026-11-25)

**Built**
- `sellbird/workspaces.py`: a registry of named projects (`workspaces.json` in
  the per-user app folder, outside every project).
- `--workspace NAME|PATH` on every command, plus `SELLBIRD_WORKSPACE`. The flag
  wins. Resolved before `sellbird.paths` is imported and applied by setting
  `SELLBIRD_HOME`, so the flag, the variable and Docker all take one path
  through the code.
- `sellbird workspace list | add | remove | which`.
- A `[workspace: name]` line on stderr for every command, whenever a workspace
  is named — the point of the feature, not decoration: `outbox --approve-all`
  in the wrong terminal approves the wrong company's email.
- `GET /api/instance` now reports the workspace, and the dashboard puts it in
  the browser tab title, so two dashboards on two ports are distinguishable.

**Not built, because it already existed:** the isolation itself.
`paths.resolve_home` has always honoured `SELLBIRD_HOME`, `workspace_home.Home`
already carries every per-project path in a ContextVar, `StateManager` already
reads `current_home().db_path`, and the dashboard already runs per-home. This
change adds the ergonomics that make the existing isolation safe to use, and no
new isolation.

**Known limit:** two projects draw on the same Claude subscription, so
`usage.max_daily_claude_percent` is a share of one quota in each config. Two
projects at 80% will exhaust it. Documented, not enforced.

## Hosted sign-in (2026-11-25)

**Built** — `sellbird serve`: the dashboard at a public URL behind a username
and password, for an operator who asked for that over an SSH tunnel or an
identity proxy, having been told it is the most work and the most risk.

- `sellbird/server_auth.py`: scrypt account (standard library), server-side
  sessions stored hashed, per-account and per-address lockout, and an ASGI
  guard that keeps the provenance, public-path and JSON-body rules of the
  local one while replacing its loopback Host check and bearer token.
- `sellbird/server.py`: `/login`, `/api/login`, `/api/logout`, and the app
  built through the existing `dashboard.build_app(guard=...)` seam.
- `sellbird/web/login.html`; `app.js` now sends a hosted 401 to the sign-in
  page instead of the token lock screen.
- Migration v19: a `sessions` table.
- `sellbird server set-password` / `show`; `docs/hosting.md`.
- `logsafety.py` redacts the session cookie.

**`dashboard_auth.py` and `dashboard.py` were not edited.** A local install
behaves exactly as before, and two tests assert it.

**New risk this introduces, named so it is not forgotten:** a cookie is sent
by the browser automatically where a bearer token was not, so CSRF is a real
risk here that the local dashboard never had. Three independent defences,
because one is not enough: `SameSite=Strict`, the inherited `Origin` /
`Sec-Fetch-Site` checks, and the JSON-only rule for mutations.

**Deliberately not built:** two-factor (recommended as the next step: a
stolen password is currently enough), more than one user, password reset by
email.

## Fix: the offer preview ignored the operator's email rules (2026-11-25)

`offer.preview_email` set `writer.email_rules = ""` on purpose, so with
`writer.email_rules` configured the dashboard previewed Sellbird's built-in
shape — an email that is never actually sent once the operator's rules
replace it. Found while previewing an operator's real rules. It now loads
them, and says so plainly when the file can't be read instead of quietly
previewing the wrong shape. Nothing tested `preview_email` before.
