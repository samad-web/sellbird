#!/usr/bin/env bash
# Put a hosted Sellbird on a fresh Debian/Ubuntu server.
#
#   sudo ./deploy/install.sh sellbird.example.com
#
# Safe to run twice: every step checks before it acts. It does NOT do the
# three things that need a human at a browser -- signing in to Claude, setting
# the operator password, and the Microsoft mailbox OAuth -- and it prints them
# at the end.
#
# Read docs/hosting.md first. A hosted Sellbird holds mailbox tokens that can
# send as you, a Claude session, your API keys and every prospect's details.

set -euo pipefail

DOMAIN="${1:-}"
APP_USER="${SELLBIRD_USER:-sellbird}"
REPO="${SELLBIRD_REPO:-https://github.com/ethanplusai/sellbird.git}"
BRANCH="${SELLBIRD_BRANCH:-main}"
PORT="${SELLBIRD_PORT:-5555}"
HOME_DIR="/home/${APP_USER}"
APP_DIR="${HOME_DIR}/sellbird"

say() { printf '\n  %s\n' "$*"; }
die() { printf '\n  %s\n\n' "$*" >&2; exit 1; }

[ -n "$DOMAIN" ] || die "Usage: sudo ./deploy/install.sh <your.domain>"
[ "$(id -u)" -eq 0 ] || die "Run this with sudo: it installs packages and a service."
command -v apt-get >/dev/null || die "This script is for Debian/Ubuntu."

# ── 1. The firewall, FIRST ────────────────────────────────────────────────
# Before anything listens. SSH is allowed before enabling, because enabling
# ufw without it locks you out of your own machine and there is no undo from
# here.
say "Firewall"
if ! command -v ufw >/dev/null; then
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq ufw >/dev/null
fi
ufw allow OpenSSH >/dev/null
ufw allow 80/tcp >/dev/null
ufw allow 443/tcp >/dev/null
if ufw status | head -1 | grep -q inactive; then
    ufw --force enable >/dev/null
fi
echo "    SSH, 80 and 443 allowed; everything else refused."

# ── 2. Packages ───────────────────────────────────────────────────────────
say "Packages"
DEBIAN_FRONTEND=noninteractive apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    python3-venv python3-pip git curl ca-certificates debian-keyring \
    debian-archive-keyring apt-transport-https gnupg >/dev/null
echo "    python3-venv, git, curl."

# ── 3. Caddy, for TLS ─────────────────────────────────────────────────────
# Certificates are got and renewed automatically. HTTPS is not optional: the
# password crosses this connection and the session cookie is Secure, so
# Sellbird refuses to start on an http:// URL.
say "Caddy (HTTPS)"
if ! command -v caddy >/dev/null; then
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
        | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
        > /etc/apt/sources.list.d/caddy-stable.list
    DEBIAN_FRONTEND=noninteractive apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq caddy >/dev/null
fi
cat > /etc/caddy/Caddyfile <<CADDY
# Sellbird. Caddy terminates TLS and passes the original Host through, which
# is what Sellbird checks against its --url. Sellbird itself only ever binds
# loopback, so a firewall mistake cannot expose it directly.
${DOMAIN} {
    reverse_proxy 127.0.0.1:${PORT}
}
CADDY
systemctl enable caddy >/dev/null 2>&1 || true
systemctl restart caddy
echo "    Serving ${DOMAIN}; certificate on first request."

# ── 4. The user it runs as ────────────────────────────────────────────────
say "User"
if ! id -u "$APP_USER" >/dev/null 2>&1; then
    adduser --disabled-password --gecos "" "$APP_USER" >/dev/null
fi
echo "    ${APP_USER} (never root)."

# ── 5. The application ────────────────────────────────────────────────────
say "Sellbird"
if [ -d "${APP_DIR}/.git" ]; then
    sudo -u "$APP_USER" git -C "$APP_DIR" fetch --quiet origin "$BRANCH"
    sudo -u "$APP_USER" git -C "$APP_DIR" checkout --quiet "$BRANCH"
    sudo -u "$APP_USER" git -C "$APP_DIR" reset --hard --quiet "origin/${BRANCH}"
else
    sudo -u "$APP_USER" git clone --quiet --branch "$BRANCH" "$REPO" "$APP_DIR"
fi
if [ ! -x "${APP_DIR}/.venv/bin/sellbird" ]; then
    sudo -u "$APP_USER" python3 -m venv "${APP_DIR}/.venv"
fi
sudo -u "$APP_USER" "${APP_DIR}/.venv/bin/pip" install --quiet --upgrade pip
sudo -u "$APP_USER" "${APP_DIR}/.venv/bin/pip" install --quiet -e "$APP_DIR"
sudo -u "$APP_USER" test -e "${APP_DIR}/.env" \
    || sudo -u "$APP_USER" cp "${APP_DIR}/.env.example" "${APP_DIR}/.env"
chmod 600 "${APP_DIR}/.env"
echo "    Installed at ${APP_DIR}."

# ── 6. The service ────────────────────────────────────────────────────────
# Written but NOT started: it would refuse to start without an operator
# account anyway, and a service in a restart loop hides that reason.
say "Service"
cat > /etc/systemd/system/sellbird.service <<UNIT
[Unit]
Description=Sellbird
After=network-online.target
Wants=network-online.target

[Service]
User=${APP_USER}
WorkingDirectory=${APP_DIR}
ExecStart=${APP_DIR}/.venv/bin/sellbird serve --url https://${DOMAIN} --port ${PORT}
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=read-only
ReadWritePaths=${APP_DIR}

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable sellbird >/dev/null 2>&1 || true
echo "    sellbird.service written and enabled (not started yet)."

# ── What a script cannot do ───────────────────────────────────────────────
cat <<NEXT

  Three steps need you, at a browser:

  1. Sign in to Claude, which is what Sellbird thinks with:
         sudo -u ${APP_USER} -i
         npm install -g @anthropic-ai/claude-code    # if it isn't there
         claude login                                # prints a URL to open

  2. Set the operator password (asked twice, never an argument):
         sudo -u ${APP_USER} ${APP_DIR}/.venv/bin/sellbird server set-password

  3. Start it, and connect the mailbox:
         sudo systemctl start sellbird
         # from your own machine, so the OAuth redirect can land:
         ssh -L 8766:127.0.0.1:8766 ${APP_USER}@${DOMAIN}
         ${APP_DIR}/.venv/bin/sellbird microsoft auth

  Then sign in at https://${DOMAIN}/login

  Check on it with:  systemctl status sellbird  ·  journalctl -u sellbird -f

NEXT
