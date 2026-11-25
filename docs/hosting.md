# Hosting Sellbird at your own URL

Sellbird normally runs on your machine and is reached at `127.0.0.1` with a
token link. `sellbird serve` runs it on a server instead, at a name of your
own, behind a username and password.

**Read this first.** A hosted Sellbird holds mailbox OAuth tokens that can
send as you, a Claude session, your API keys and every prospect's contact
details, and it is reachable by anyone who can resolve its name. If you only
need it from your own devices, an SSH tunnel to a loopback dashboard is
stronger than anything a password gives you, and needs none of this:

```bash
ssh -L 5555:127.0.0.1:5555 you@your-server    # then open the local link
```

Host it only if you actually need to reach it from anywhere.

## What you need

- A Linux server you control.
- A name pointing at it (`sellbird.example.com`, an `A` record to its IP).
- A TLS proxy in front. HTTPS is not optional here: the password crosses that
  connection, and the session cookie is `Secure`, so it is never sent over
  plain HTTP. `serve` refuses to start with an `http://` URL.
- A firewall that allows only 80 and 443 from the internet, and SSH from you.

## Set it up

```bash
# 1. An operator account. No default exists; the server won't start without one.
sellbird server set-password

# 2. Run it. It binds loopback only -- the proxy is what faces the internet.
sellbird serve --url https://sellbird.example.com
```

Then put a proxy in front. Caddy is two lines and gets its certificate itself:

```caddy
sellbird.example.com {
    reverse_proxy 127.0.0.1:5555
}
```

Caddy passes the original `Host` through, which is what Sellbird checks
against the `--url` you gave it. Nothing else needs configuring.

Sign in at `https://sellbird.example.com/login`.

## Keeping it running

A systemd unit, so it restarts with the machine:

```ini
[Unit]
Description=Sellbird
After=network-online.target

[Service]
User=sellbird
WorkingDirectory=/home/sellbird/sellbird
ExecStart=/home/sellbird/sellbird/.venv/bin/sellbird serve --url https://sellbird.example.com
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Run it as an ordinary user, never root.

## What protects it

| | |
|---|---|
| Password | Hashed with scrypt (memory-hard), salted per account, never stored or logged in readable form |
| Sessions | Server-side and stored hashed, so signing out really ends them and a stolen database copy is not a set of live sessions |
| Cookie | `HttpOnly`, `Secure`, `SameSite=Strict` |
| Cross-site requests | Refused three ways: SameSite, an `Origin` / `Sec-Fetch-Site` check, and a JSON-only rule for anything that changes something |
| Wrong address | A request whose `Host` is not your name is refused before anything else is read |
| Guessing | Five failures locks for fifteen minutes, counted per account **and** per address |
| Enumeration | A wrong password and an unknown username give the same message and take the same time |
| Sessions left open | Twelve hours at most, two hours idle |
| Everything else | The same security headers as the local dashboard, plus HSTS |

## Changing the password

```bash
sellbird server set-password      # asks twice, never takes it as an argument
```

Every existing session is signed out, so a changed password really ends the
old ones. `sellbird server show` says who the operator is and when the
account was made.

## What this does not do yet

- **Two-factor.** The single best thing to add next: a stolen password is
  currently enough. Until it exists, use a long random password from a
  password manager and nothing else.
- **More than one user.** One operator account. Several people means a users
  table and roles, which is a different piece of work.
- **Password reset by email.** Change it on the server; for one operator
  that is the safer answer anyway.

## If something goes wrong

- *"Not started: No operator account yet"* — run `sellbird server set-password`.
- *"The public URL must start with https://"* — it must. See above.
- *"This server doesn't answer to that address"* — the `Host` reaching
  Sellbird is not the `--url` you gave it. Check the proxy passes the
  original `Host`.
- The sign-in form reloads instead of letting you in — the cookie is `Secure`
  and the connection was not HTTPS end to end.
- Locked out — wait fifteen minutes, or restart the service: the counters are
  in memory.
