# Testing

## Strategy

- Every feature's tests map to the acceptance criteria and edge cases in its feature doc,
  named so the mapping is obvious.
- `pytest` from the repo root. Needs a venv with the project installed
  (`pip install -e ".[dev]"`).
- A failing test is fixed at the root cause in the feature, never weakened.
- After a change, the feature's own tests **and** the tests of everything in its impact
  map run again.

## Results

### 2026-11-24 — baseline before any change

Full suite, Windows 11, Python 3.13.7, venv without Playwright:

- **3536 passed, 192 skipped, 1 failed**, 5m41s.
- Failure: `tests/test_sending_mode.py::test_the_writer_keeps_a_symlinked_config_a_symlink`
  — `OSError: [WinError 1314] A required privilege is not held by the client`.
  Creating a symlink on Windows needs Developer Mode or elevation. Not a product bug; the
  test is missing the `skipif(sys.platform == "win32")` the repo uses elsewhere.
  See `build-docs/AUDIT.md` finding 1.
- One deprecation warning: `datetime.utcnow()` in `sellbird/agents/analyst.py:27`
  (finding 3).

Nothing browser-driven ran: Playwright was not installed, so the LinkedIn integration and
any Playwright-backed check were not exercised.

### 2026-11-24 — Wasi API, live

Mapped by probing the live API. Every schema probe used an invalid recipient
(`"not-a-number"`), so nothing reached a person; Meta's own `#131009` rejection confirmed
the required phone format.

One real send, at the user's request, to a test number they gave:

- `POST /api/v1/messages` with `type: "template"`, `template: "test_message"`,
  `params: {"1": "<code>"}` → **accepted** (`status: "sent"`, Meta `wamid` returned).
- `GET /api/v1/conversations/<id>/messages` → **delivered 3.8s later**
  (`status: "delivered"`, `delivered_at` set, no `meta_error_code`).
- Three further attempts with the wrong params shape (`params` as an array,
  `variables`, `body_params`) were all refused by validation, so exactly **one**
  message was sent.

Proves end to end: auth, the send path, the template path, the polling read path and
delivery receipts. Not yet exercised: an **inbound** message from a prospect (needs
someone to reply), a free-text send inside an open 24-hour window, and the
`not-on-WhatsApp` error path.

No test number, template name or message id from this run belongs in the repo — this
entry deliberately records counts and timings only.

### 2026-11-24 — Phase A (audit fixes)

Full suite: **3540 passed, 191 skipped, 0 failed**.

- Finding 1 fixed: the symlink test now skips on Windows, so the suite is green on
  a supported platform and commits are possible again.
- Finding 2 fixed with two new regression tests, both of which caught real bugs in
  the first version of the fix:
  - bounces and sends were being compared across different time formats
    (`outbox.sent_at` has a "T" and microseconds, `actions.created_at` has a space
    and neither), so every bounce landed before the cutoff;
  - with fewer than `BOUNCE_RATE_WINDOW` sends there was no cutoff at all, so
    *every* bounce ever still counted — the exact bug being fixed.
- Findings 3-8: covered by the existing suite continuing to pass.

### 2026-11-24 — Phase B (WhatsApp channel)

| Step | Tests | Suite |
|---|---|---|
| B1 channel column, E.164, gate | 67 new | 3607 passed |
| B2 send through Wasi | 19 new | 3626 passed |
| B3 inbound polling | 11 new | 3637 passed |
| B4 silent opt-outs | 3 new | — |
| B5 CLI, dashboard, docs | 7 new | 3644 passed |

Test names map to the acceptance criteria in `features/whatsapp-channel.md`.

**Bugs the tests caught while the feature was being written** (each now has a
regression test):

1. `_BRACKETED_TRUNK` stripped a legitimate leading zero from a subscriber number:
   `+442079460018` became `+44279460018`, a different person's line. A bracketed
   trunk zero has to be recognised *before* separators are stripped, while it is
   still distinguishable from a real digit.
2. Suffix-matching a local number to its E.164 form needed a nine-digit floor.
   Below that, a 7-digit local number matches any longer number ending the same
   way — and used to attribute an inbound reply, that stops the wrong sequence.
3. `template_mismatch` passed an unfilled `{{placeholder}}`, because the
   placeholder is itself text and satisfied the wildcard.

**Verified against the live Wasi API** (the user's own service):

- Schema probes used an invalid recipient throughout, so nothing reached a person.
- One real send, at the user's request, to a test number they provided: accepted,
  then **delivered in 3.8s**, confirmed through the polling read path.
- `sellbird whatsapp test` and `sellbird whatsapp templates` run clean against the
  live account (read-only).

**Not exercised**: an inbound message from a real prospect, a free-text send inside
an open 24-hour window, a real Block, and a live late-failure — all need another
person or a dud number. Everything in that list is covered by unit tests with a
fake provider shaped on the real API's observed answers.

## WhatsApp phase 2 — sequence and intelligence (2026-11-25)

`tests/test_whatsapp_sequence.py` (23) and `tests/test_dashboard_whatsapp.py` (7),
one test per acceptance criterion in the feature doc.

| Criterion | Test |
|---|---|
| 1 three rows at steps 1-3, cumulative delays | `test_follow_ups_are_staged_as_steps_at_their_delays` |
| 1 no follow-ups = one message | `test_no_follow_ups_configured_stages_one_message` |
| 1 unknown template stages nothing | `test_a_follow_up_naming_an_unknown_template_stages_nothing` |
| 2 step 2 holds while step 1 waits | `test_a_follow_up_does_not_send_while_the_opener_waits` |
| 3 step 2 cancelled when step 1 failed | `test_a_follow_up_is_cancelled_when_the_opener_never_sent` |
| 2/3 step 2 goes once step 1 has | `test_a_follow_up_sends_once_the_opener_has_gone_out` |
| 4 a reply stops the rest | `test_a_reply_stops_the_remaining_steps` |
| 5 Claude's values reach Outbox and Meta unchanged | `test_claude_writes_the_values_and_they_are_what_is_sent` |
| 5 the prompt carries the template and the facts | `test_the_prompt_carries_the_template_and_the_facts` |
| 5 no brain / a raising brain still sends | `test_no_brain_falls_back_to_the_configured_expression`, `test_a_brain_that_raises_does_not_lose_the_message` |
| 6 Meta's parameter rules | 6 parametrised cases + `test_an_ordinary_value_is_fine` |
| 6 a bad value never reaches the provider | `test_a_value_with_a_line_break_never_reaches_the_provider` |
| 6 a hand-edited row is held | `test_a_row_whose_stored_value_is_unsendable_is_held` |
| 7 a Utility follow-up is refused | `test_a_utility_follow_up_is_refused_at_send` |
| 8 the double-send guard | `test_queueing_the_same_prospect_twice_queues_one_message` |
| 8 a finished sequence doesn't block a new one | `test_a_finished_sequence_does_not_block_a_new_one` |
| 9 the dashboard card | 7 tests in `test_dashboard_whatsapp.py`, including no key in the answer |

**Failures found while building, and the fixes:**

- The first `uq_outbox_whatsapp_step` had no status scope, so it blocked a
  prospect for ever once any WhatsApp row existed. `test_whatsapp_does_not_eat_the_email_allowance`
  (an existing test) caught it. Scoped to the steps still waiting.
- My own criterion-1 test asserted `.days` on a timedelta that was a fraction
  of a second negative for step 1. The test was wrong, not the code: it now
  compares each step against step 1's time.

**Regression run:** 3718 passed, 192 skipped, 0 failed (was 3686 before this
feature). Email's sending path is the one that shared code with these changes
(`_follow_up_order`, `add_outbox_item`, the gate) and is unchanged.

**Live, read-only, against the real number:** connection ok, quality rating
GREEN, tier TIER_250 with 249 of the 24-hour allowance left, 23 approved
templates (18 Utility, 4 Marketing, 1 Authentication). Nothing was sent.

**Local run:** the dashboard served `/` (200), the page carried the WhatsApp
card, and `/api/whatsapp` without a token returned 401.

## Multiple projects (2026-11-25)

`tests/test_workspaces.py` (35), one or more per acceptance criterion in
`build-docs/features/multi-project.md`.

| Criterion | Test |
|---|---|
| 1 a name resolves to its folder | `test_a_registered_name_resolves_to_its_folder` |
| 2 an unknown name stops, listing the names | `test_an_unknown_name_stops_and_lists_the_names` |
| 3 a bare folder works unregistered | `test_a_folder_resolves_without_being_registered` |
| 5 the env var, and the flag beating it | `test_the_environment_variable_is_the_fallback` |
| 7 add stores an absolute path, remove keeps the folder | `test_add_stores_an_absolute_path`, `test_remove_forgets_the_name_and_leaves_the_folder` |
| 8 a corrupt registry reads as empty | 4 parametrised cases + `test_junk_entries_are_dropped_not_fatal` |
| 9 two projects share nothing | `test_two_workspaces_never_share_a_database` (runs the real CLI twice) |
| — names that could be mistaken for paths | 8 parametrised cases |
| — a registered folder that moved | `test_a_registered_folder_that_moved_is_not_silently_repaired` |

**Problems found while building, and the fixes:**

- **`--workspace` didn't switch anything at first.** Reading the registry
  imported `sellbird.paths`, which settles `PROJECT_ROOT` at import — so the
  home was decided before the flag was applied. Caught by running the command,
  not by a test: `workspace which` reported the checkout. Fixed by recomputing
  `paths.PROJECT_ROOT` after setting `SELLBIRD_HOME`, which is safe only
  because `cli.py` imports nothing from `sellbird` at module level.
- **One of my own tests wrote to the real registry.** A leftover line called
  `workspaces.remove("acme")` with no `path=`, which deleted the live entry I
  had just added for the manual check. Both entries were throwaway test ones,
  so nothing of the user's was lost. Every test now passes an explicit tmp
  registry path.

**Verified by hand, end to end:** two projects registered, `--workspace`,
`SELLBIRD_WORKSPACE`, and the flag beating the variable all switched the home;
a prospect inserted into one project's database was invisible to the other and
to the checkout; an unknown name stopped the command and listed the known
names; the `[workspace: …]` line appeared only when a workspace was named.

**Regression run:** 362 passed across the dashboard, home, workspace,
packaging and Windows suites after the change.

**Found by the full suite, after the targeted runs passed:**
`test_mcp_server.py::test_instance_needs_no_token` failed. I had added the
workspace name *and the home folder's absolute path* to `/api/instance`, which
is deliberately reachable with no token so a client can find the dashboard.
The test pinned the response shape, and it was right to: an unauthenticated
endpoint should say only enough to identify the dashboard. The name of
somebody's project is not that, and the path to their folder certainly isn't.
`/api/instance` is back to exactly what it was, and the tab title now comes
from `/api/sellbird/status`, which is behind the token and already called on
every page load. Two tests pin that: one asserts `/api/instance` has the single
key, one asserts the authenticated route carries the name.

**Regression run:** 1 failed, 3752 passed before the fix; 37 workspace tests and
307 dashboard/MCP tests pass after it. Full suite re-run below.

## Hosted sign-in (2026-11-25)

`tests/test_server_auth.py` (31), one or more per acceptance criterion in
`build-docs/features/hosted-login.md`.

| Criterion | Test |
|---|---|
| 1 no session reaches no data | `test_no_session_reaches_no_data` |
| 2 a session gets through | `test_a_signed_in_session_gets_through` |
| 3 wrong password == unknown user | `test_a_wrong_password_and_an_unknown_user_answer_identically` |
| 4 lockout, per account and per address | `test_failures_lock_the_account_and_the_address`, `test_the_lockout_is_counted_per_key_not_globally` |
| 5 a new session id on login | `test_logging_in_replaces_the_session` |
| 6 logout revokes server-side | `test_logging_out_revokes_immediately` |
| 7 an expired session is refused and deleted | `test_an_expired_session_is_refused_and_deleted` |
| 8 the id is never stored in the clear | `test_the_session_id_is_never_stored_in_the_clear` |
| 9 cross-origin POST refused (CSRF) | `test_a_cross_origin_post_with_a_valid_cookie_is_refused` |
| 10 a foreign Host refused first | `test_a_foreign_host_is_refused_before_anything_else` |
| 11 refuses to start wrong | `test_a_non_https_url_is_refused`, `test_no_account_means_no_server` |
| 12 the password is not stored or logged | `test_the_password_is_not_stored_and_the_file_is_private`, `test_a_session_cookie_never_survives_a_log_line` |
| 13 the local dashboard is unchanged | `test_the_local_dashboard_still_uses_its_token`, `test_the_local_guard_still_refuses_a_public_host` |

**Mutation-tested, because 31 tests passing first time on security code is not
evidence of anything.** Three load-bearing lines were broken on purpose and
the suite re-run:

| Mutation | Result |
|---|---|
| `if session is None:` → `if False:` (session not required) | 5 tests fail |
| the `Origin` check removed (no CSRF defence) | `test_a_cross_origin_post_...` fails |
| any `Host` accepted | `test_a_foreign_host_...` fails |

Restored, 31 pass again. A test that passes against broken code is worse than
no test, and this is the file where that matters most.

**Regression run:** 3785 passed, 192 skipped. That run began before the
`logsafety.py` change, so the suites covering it were re-run separately: 314
passed, 10 skipped.

**Not covered by tests, and worth knowing:** the lockout counters are in
memory, so restarting the process forgives them. Right trade for one
operator, but it means someone who can crash the process gets fresh attempts.

## Fix: offer preview and operator email rules (2026-11-25)

Three tests in `tests/test_offer.py`: the preview writes with the operator's
rules; without rules it uses the built-in shape; an unreadable rules file is
reported rather than silently replaced. The first was confirmed to fail with
the old line put back. On the way, my new helper reused the name of an
existing `_config()` in the same file and broke two old tests — caught on the
first run, renamed. Full suite: 3789 passed, 192 skipped.

## CSV import (2026-11-25)

- `tests/test_importer.py`: 8 tests, one per acceptance criterion (AC1–AC7) plus the row cap. All pass.
- Found while writing them: async fixtures need `pytest_asyncio.fixture` (strict mode). Fixed in the test setup, not the feature.

## Signature footer (2026-11-25)

- AC1 `test_cold_footer_sits_under_the_logo_behind_a_rule`; AC2
  `test_reply_without_the_footer_has_no_rule`; AC3
  `test_footer_text_mid_body_is_left_alone`; AC4 `test_accent_must_be_a_hex_colour`.
- `tests/test_signature.py`, `test_microsoft.py`, `test_config.py`: 116 passed.

## Branded email style (2026-11-26)

- AC1 `test_ac1_branded_wraps_the_letter_in_header_card_and_dark_band`; AC2
  `test_ac2_button_on_cold_email_only`, `test_ac2_no_button_label_means_no_button`; AC3
  `test_ac3_button_without_an_https_booking_url_is_refused` (blank, http, javascript:),
  `test_ac3_button_is_ignored_in_letter_style`; AC4 `test_ac4_style_is_letter_or_branded`;
  edge `test_branded_without_a_company_shows_the_logo_alone`.
- `tests/test_signature.py`, `test_microsoft.py`, `test_config.py`: 125 passed. No failures found.
- Fix (2026-11-26): the button read `config.offer`, which `SellbirdConfig` doesn't have
  (it's `config.product.offer`); tests used a fake config with the wrong shape, so a live
  send found it. Fixed the path; tests now build `product.offer` and
  `test_button_reads_the_booking_url_from_the_real_config_shape` pins the real shape.

## Deploy script service unit (2026-11-26)
- `bash -n deploy/install.sh`: syntax OK. There's no unit test for the script.
- Live check on a shared Ubuntu 24.04 server, with the same unit settings set by hand:
  - the service starts, and `/login` returns 200;
  - `claude -p` run under `systemd-run` with the unit's sandbox (ProtectHome=read-only,
    ReadWritePaths=home, ProtectSystem=full, PrivateTmp, NoNewPrivileges) answers.
- Not covered: install.sh still assumes Caddy. On a server already running nginx it
  must not run as-is. Set up an nginx site and `certbot --nginx` by hand.
