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
