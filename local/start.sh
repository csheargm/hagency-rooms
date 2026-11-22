#!/usr/bin/env bash
# One command: start Synapse + Element Web (Docker, localhost only), create accounts/room
# on first run, then run every agent adapter and @pm under supervise.py (Ctrl-C stops them;
# saving a .py file restarts them by themselves).
# Usage: ./start.sh [codex|claude]      (agent CLI used by both agent adapters; default codex)
#        ALLOW_MEMBERS=1 ./start.sh      (any joined member may drive an agent; default owner only)
set -euo pipefail
cd "$(dirname "$0")"
# This server's own settings (which GitHub repos rooms may use): git-ignored, see local.env.example.
if [ -f local.env ]; then set -a; . ./local.env; set +a; fi
AGENT="${1:-codex}"
EXTRA=(); [ "${ALLOW_MEMBERS:-0}" = 1 ] && EXTRA=(--allow-members)
IMAGE=matrixdotorg/synapse:latest

if [ ! -f data/homeserver.yaml ]; then
  mkdir -p data
  docker run --rm -v "$PWD/data:/data" -e SYNAPSE_SERVER_NAME=bridge.localhost \
    -e SYNAPSE_REPORT_STATS=no "$IMAGE" generate
  cat synapse-overrides.yaml >> data/homeserver.yaml
fi
[ -d .venv ] || { python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt; }

docker compose up -d
echo -n "waiting for Synapse"
until curl -sf http://127.0.0.1:8648/_matrix/client/versions >/dev/null; do echo -n .; sleep 1; done; echo
.venv/bin/python setup_accounts.py

echo "Element Web: http://localhost:8649  (homeserver http://localhost:8648). Ctrl-C stops the agent adapters."
# Agent adapters + @pm under the supervisor: saving a .py file restarts them without losing messages.
exec .venv/bin/python supervise.py --agent "$AGENT" ${EXTRA[@]+"${EXTRA[@]}"}
