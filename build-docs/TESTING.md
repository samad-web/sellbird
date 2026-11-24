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
