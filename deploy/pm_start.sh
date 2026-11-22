#!/bin/sh
# @pm's container on the public server (docker-compose.yml runs it in ~/hagency/pm as /app).
# Installs the pinned requirements into /app/.venv (kept between starts), makes sure @pm's account
# exists (before anyone can sign up), then runs @pm. Its passwords stay in /app/.env (mode 600).
set -eu
umask 077
[ -x .venv/bin/python ] || python -m venv .venv
.venv/bin/pip install -q --disable-pip-version-check -r requirements.txt
until .venv/bin/python -c 'import os, urllib.request; urllib.request.urlopen(os.environ["HAGENCY_HOMESERVER"] + "/_matrix/client/versions", timeout=3)' 2>/dev/null; do
  echo "waiting for Synapse"; sleep 3
done
.venv/bin/python bin/setup_accounts.py --pm-only
exec .venv/bin/python bin/loop.py
