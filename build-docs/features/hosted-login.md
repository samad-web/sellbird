# A hosted Sellbird with its own sign-in

## Purpose

Run Sellbird on a server and reach it at a real URL from any device, with a
username and password instead of a token link that only works on one machine.

## The decision behind it

The operator was offered three ways to reach a hosted dashboard: an SSH tunnel
(no code, nothing exposed), an identity proxy in front (Cloudflare Access or
Tailscale, no code), or a login page built into Sellbird. They chose the login
page, having been told it is the most work and the most risk. This document is
written so that risk is handled, not accepted.

**What is now true that was not before:** Sellbird holds mailbox OAuth tokens
(Microsoft Graph, `Mail.Send`), a Claude Max session, API keys, and every
prospect's contact details. Until now the only way to reach it was to already
be on the machine. After this, anyone on the internet can reach the front door.
Everything below follows from that one sentence.

## What exists, and what must not be touched

`dashboard_auth.DashboardGuard` does five things in order: a loopback-only Host
check, a browser-provenance check (`Origin` and `Sec-Fetch-Site`), a public-path
allowance for the shell, a constant-time bearer-token check with a separate
agent principal, and a JSON-only rule for request bodies. It also attaches the
security headers to every response.

`dashboard.build_app(guard=...)` exists precisely so a server can supply its
own sign-in *in place of* that guard — "a server's own sign-in instead of the
dashboard token", in its own docstring. So:

- **`dashboard_auth.py` is not edited.** The local dashboard keeps working
  exactly as it does, loopback-only, for anyone who never hosts anything.
- The new guard **reimplements the checks that still apply** (provenance,
  public paths, JSON bodies, security headers) rather than dropping them. The
  only check being *replaced* is Host + bearer token.

## Architecture

**`sellbird/server_auth.py`** — a `SessionGuard` ASGI middleware plus the
account and session stores.

| Concern | Decision | Why |
|---|---|---|
| TLS | Required. Caddy terminates and proxies to `127.0.0.1:5555` on the same host; Sellbird itself never binds a public interface | A login page over HTTP hands the password to anyone on the path. Caddy gets Let's Encrypt certificates automatically |
| Host check | Exact match against the configured public hostname (plus loopback, so the health check and proxy still work) | The loopback rule is what is being replaced; "any host" would allow DNS rebinding and Host-header poisoning |
| Password storage | `hashlib.scrypt`, n=2^15, r=8, p=1, 16-byte salt, in `data/operator.json` at mode 600 | Standard library, no new dependency, memory-hard. The file lives with the other secrets and is written the same careful way the token files are |
| Sessions | Server-side: a 32-byte random id, stored **hashed** in a new `sessions` table with an expiry; the cookie carries only the id | Server-side means logout and "sign out everywhere" actually revoke. Hashing means a stolen database copy is not a set of live sessions |
| Cookie | `HttpOnly`, `Secure`, `SameSite=Strict`, `Path=/`, no `Domain` | Strict is the main CSRF defence; HttpOnly keeps it away from any injected script; Secure keeps it off plaintext |
| CSRF | SameSite=Strict **and** the inherited `Origin` / `Sec-Fetch-Site` checks **and** the inherited JSON-only rule for mutations | Three independent layers, because a cookie is sent automatically and a bearer token was not. This is the single biggest thing cookie auth gives away |
| Session fixation | A new session id is issued on every successful login; the old one is deleted | An attacker who can set a cookie must not be able to pre-choose the session that later becomes authenticated |
| Login throttling | Per-account and per-IP: a delay that grows with consecutive failures, then a lockout window. In memory, single process | A password is one guess away from everything here |
| Timing | `hmac.compare_digest` for session ids; scrypt runs even for an unknown username | No user enumeration, no timing oracle |
| Error text | One message for every failure: "That username and password don't match" | Same reason |
| Agent token | **Off** in hosted mode unless explicitly enabled | The coding agent is not on this machine any more, so the second credential is attack surface with no user |
| Logging | Login attempts logged with the IP and the outcome, never the password or session id | `logsafety.py` already redacts bearer tokens and vendor keys; the session cookie is added to it |
| Session lifetime | 12 hours absolute, 2 hours idle, both configurable | A browser left open in a cafe is the realistic threat, not cryptanalysis |

**`sellbird serve`** — the hosted entry point: builds the app with
`build_app(guard=...)`, binds loopback, refuses to start unless an operator
account exists and a public URL is configured.

**`sellbird server set-password`** — creates or changes the single operator
account, reading the password from a prompt, never an argument (arguments land
in shell history and `ps`). There is no default account and no default
password; the server will not start without one.

**Migration v19** — a `sessions` table: `id_hash`, `username`, `created_at`,
`expires_at`, `last_seen_at`, `ip`. Expired rows are deleted on read.

**The login page** — `sellbird/web/login.html`, served at `/login` with no
data on it, posting JSON to `/api/login`. `/logout` clears the session.

## Impact map

| Touched | What could break |
|---|---|
| `sellbird/server_auth.py` (new) | everything, if it is wrong. This is the file to review line by line |
| `dashboard.py` | **not edited.** The hosted app is built through the existing `build_app` seam |
| `dashboard_auth.py` | **not edited.** Local dashboards behave identically |
| `state.py` | one new table; no existing column or query changes |
| `cli.py` | two new commands; no existing command changes |
| `web/` | one new page; `app.js` gains a redirect to `/login` on a 401 |
| `logsafety.py` | one more redaction pattern |
| Deployment | Caddy, a DNS record, and a firewall group on a VPS that currently has none |

## Edge cases and failure modes

- No operator account → `sellbird serve` refuses to start and says how to make
  one. It never starts open.
- No TLS / the configured URL is not `https://` → refuses to start. A Secure
  cookie would never come back and the login would silently loop.
- Request arrives with a Host that is not the configured one → 400 before
  anything else, as today.
- Correct password, expired session later → 401 JSON for API calls, a redirect
  to `/login` for a navigation. The page must not show half-loaded data.
- Database unreadable → fail closed (503), never fail open. Same rule the token
  store already follows.
- Clock moving backwards → expiry is compared against stored absolute times, so
  a session cannot be extended by moving a clock.
- Many failed logins → lockout, and the lockout is per-account **and** per-IP so
  one attacker cannot lock the operator out from a single address.
- Proxy removed or misconfigured so the app is reached directly over HTTP →
  the Host check still applies, and the Secure cookie never arrives, so no
  session is ever established. It fails closed rather than downgrading.
- `X-Forwarded-*` headers → read only when the connection comes from loopback,
  because only the local proxy can be trusted to set them.

## Acceptance criteria

1. A request with no session, to any API path, gets 401 and no data.
2. A request with a correct session gets through, and `principal` is set.
3. A wrong password gives the same message and timing as an unknown username.
4. N consecutive failures lock the account for the window, per account and
   per IP, and a correct password during the lockout still fails.
5. Logging in issues a new session id; the previous one stops working.
6. Logging out makes that session id stop working immediately.
7. An expired session is refused and its row is gone.
8. A session id is never stored in the database in plain form.
9. A cross-origin POST with a valid cookie is refused (CSRF).
10. A request with a foreign `Host` is refused before the session is read.
11. `serve` refuses to start with no account, or with a non-HTTPS public URL.
12. The password is never in an argument, a log line, or the database in plain
    form.
13. The local `sellbird dashboard` behaves exactly as before, with its token.

## Out of scope (say so, don't silently skip)

- Multiple users, roles, invitations. One operator account, until asked.
- Two-factor authentication. **Recommended as the next step**, and genuinely
  the single best addition once this works: a TOTP secret and a six-digit code
  turns a stolen password into nothing.
- Password reset by email. Changing it is `sellbird server set-password` on
  the server, which is the safer answer for one operator anyway.
- Anything multi-tenant. `Home` supports it; this feature does not use it.
