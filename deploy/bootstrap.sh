#!/usr/bin/env bash
# Runs ON the Oracle VM (Ubuntu 24.04) in ~/hagency/app/deploy, a checkout of one commit of master
# (deploy.sh, from the operator's computer, puts it there; nobody edits code on the server). @pm runs
# from ~/hagency/pm (that commit's files plus @pm's state). Safe to run again. .env is read as data
# by envfile.py (never sourced), and secrets are never printed or put on a command line.
set -euo pipefail
cd "$(dirname "$0")"
umask 077
PM_DIR="$(cd ../.. && pwd)/pm"
[ -f "$PM_DIR/bin/loop.py" ] || { echo "✗ $PM_DIR has no @pm code: run deploy.sh from your computer"; exit 1; }
get() { python3 envfile.py get "$1"; }

# 1. Docker, Compose and vnstat (traffic counts for guard.py) from Ubuntu's own archive.
pkgs=()
command -v docker >/dev/null || pkgs+=(docker.io)
docker compose version >/dev/null 2>&1 || sudo docker compose version >/dev/null 2>&1 || pkgs+=(docker-compose-v2)
command -v vnstat >/dev/null || pkgs+=(vnstat)
if [ ${#pkgs[@]} -gt 0 ]; then
  sudo apt-get update -qq
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${pkgs[@]}"
fi
# The journal stays small on the 45 GB disk (Docker's own logs rotate: docker-compose.yml).
journal=$'[Journal]\nSystemMaxUse=200M'
if [ "$(cat /etc/systemd/journald.conf.d/hagency.conf 2>/dev/null)" != "$journal" ]; then
  sudo mkdir -p /etc/systemd/journald.conf.d
  printf '%s\n' "$journal" | sudo tee /etc/systemd/journald.conf.d/hagency.conf >/dev/null
  sudo systemctl restart systemd-journald
fi
# Firewall: OCI's security list opens 80/443/8448. Oracle's host iptables reject other INPUT, but
# Docker's published ports go through its own FORWARD rules (deploy.sh checks them from outside).

# 2. Settings: created mode 600, secrets filled, every value validated.
[ "$(id -u)" != 0 ] || { echo "✗ run this as the deploy user, not root"; exit 1; }
python3 envfile.py check
[ "$(get PM_UID)" = "$(id -u)" ] || { echo "✗ PM_UID in .env isn't $(id -u), the user that owns ~/hagency/pm"; exit 1; }
SERVER_NAME=$(get SERVER_NAME)
MATRIX_DOMAIN=$(get MATRIX_DOMAIN)
# Caddy publishes its ports on the default route's own address, not 0.0.0.0: tailscale serve holds
# :443 on this VM's tailscale0 address, and after a reboot Docker failed "address already in use"
# for 0.0.0.0:443, so the site was down until Caddy was recreated (live, 2026-11-23).
BIND_IP=$(ip -4 route get 1.1.1.1 | awk '{for (i = 1; i < NF; i++) if ($i == "src") print $(i + 1)}')
[[ $BIND_IP =~ ^[0-9]+(\.[0-9]+){3}$ ]] || { echo "✗ no IPv4 address on the default route"; exit 1; }
dc() { sudo env -u PM_UID HAGENCY_BIND_IP="$BIND_IP" docker compose --env-file .env "$@"; }   # PM_UID from .env only
dc config --quiet                                        # the compose file and .env agree
dc pull --quiet                                          # fails here on a wrong tag or architecture
# The Caddyfile is checked before anything is replaced (a running Caddy keeps its old one otherwise).
sudo docker run --rm -v "$PWD/Caddyfile:/etc/caddy/Caddyfile:ro" -e ROOMS_DOMAIN="$(get ROOMS_DOMAIN)" \
  -e MATRIX_DOMAIN="$MATRIX_DOMAIN" -e ADMIN_DOMAIN="$(get ADMIN_DOMAIN)" caddy:2.11.9 \
  caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null 2>&1 \
  || { echo "✗ the Caddyfile doesn't validate; nothing was changed"; exit 1; }

# 3. Synapse's config: generated once, then configured; Synapse never starts without the marker.
if ! sudo test -f synapse/homeserver.yaml; then
  mkdir -p synapse
  dc run --rm --no-deps -e SYNAPSE_SERVER_NAME="$SERVER_NAME" -e SYNAPSE_REPORT_STATS=no synapse generate
fi
sudo python3 configure_synapse.py synapse/homeserver.yaml   # every run: idempotent, checks the name
sudo test -f synapse/.hagency-configured || { echo "✗ Synapse isn't configured; not starting it"; exit 1; }
# Restart Synapse whenever its config differs from what it last started with (a failed run included).
loaded=$(cat .synapse-loaded 2>/dev/null || true)
current=$(sudo cat synapse/homeserver.yaml synapse_modules/*.py | sha256sum | cut -d' ' -f1)   # config and its modules

# 4. @pm's copy of the registration secret, in a root-owned folder it gets read-only (it creates its
#    own account and people's agents). @pm never sees Synapse's config or the database password.
sudo python3 hostfiles.py secret synapse/homeserver.yaml pm-secret "$(id -u)"
mkdir -p "$PM_DIR/data"
chmod 700 "$PM_DIR" "$PM_DIR/data"
# The GitHub App's private key (docs/GITHUB_APP.md): the owner puts it here; @pm reads it read-only.
# envfile.py check (above, and in deploy.sh before @pm stops) refuses a missing or readable key.
mkdir -p secrets
chmod 700 secrets

# 5. Element points at our homeserver and shows Hagency's home page (element_home.py, served by Caddy
#    at /hagency/); Synapse Admin may only use the admin host.
python3 hostfiles.py public

# 6. Start or update (@pm starts on the new code), then load the current Caddyfile.
mkdir -p flags
chmod 755 flags
dc stop pm >/dev/null 2>&1 || true                       # its files changed under it (deploy.sh): a clean start
# Synapse reads its config (and modules) only at start: restart it BEFORE @pm starts, so the chats
# @pm opens on start already get the new modules (hagency_auto_accept: they open joined).
if [ "$loaded" != "$current" ]; then dc up -d synapse; dc restart synapse; fi
dc up -d --remove-orphans
dc up -d --no-deps --force-recreate element synapse-admin >/dev/null 2>&1   # their configs are new files
echo "$current" > .synapse-loaded
# A single-file bind mount keeps the OLD file after git replaces it (new inode), so a reload alone
# would load the old routes (seen live: /connect/start 404 after a deploy). Recreate Caddy when the
# file it sees differs from the checked-out one; otherwise a reload is enough.
host_sum=$(sha256sum Caddyfile | cut -d' ' -f1)
seen_sum=$(dc exec -T caddy sha256sum /etc/caddy/Caddyfile </dev/null 2>/dev/null | cut -d' ' -f1)
if [ "$host_sum" != "$seen_sum" ]; then
  dc up -d --no-deps --force-recreate caddy >/dev/null 2>&1
else
  dc exec -T caddy caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile
fi

# 7. The daily free-tier check (guard.py): a systemd timer, as root.
unit="[Unit]
Description=Hagency Rooms: disk and traffic within Oracle's free limits
[Service]
Type=oneshot
WorkingDirectory=$PWD
ExecStart=/usr/bin/python3 $PWD/guard.py"
timer="[Unit]
Description=Hagency Rooms: daily free-tier check
[Timer]
OnCalendar=daily
RandomizedDelaySec=1h
Persistent=true
[Install]
WantedBy=timers.target"
printf '%s\n' "$unit" | sudo tee /etc/systemd/system/hagency-guard.service >/dev/null
printf '%s\n' "$timer" | sudo tee /etc/systemd/system/hagency-guard.timer >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now hagency-guard.timer >/dev/null 2>&1

# 8. Done only when everything answers inside (deploy.sh then checks HTTPS from outside).
SIGNUP=$(python3 signup.py mode)                         # open or token: the mode configured above, no value
for _ in $(seq 1 100); do
  dc exec -T pm .venv/bin/python deploy/health.py --internal "$MATRIX_DOMAIN" "$SERVER_NAME" "$SIGNUP" >/dev/null 2>&1 && break
  sleep 3                                                # @pm installs its packages on a first start
done
dc exec -T pm .venv/bin/python deploy/health.py --internal "$MATRIX_DOMAIN" "$SERVER_NAME" "$SIGNUP"
# The owner's Synapse admin account (for Synapse Admin): its first password is random and kept
# nowhere; the owner sets their own with `invite.sh owner-password` (docs/DEPLOY_ORACLE.md).
dc exec -T pm .venv/bin/python deploy/admin_user.py ensure owner
