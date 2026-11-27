#!/bin/sh
# The trial-worker container (docker-compose.yml, ADR 0027): trial builds, with deploy/trial.env only.
# Not configured (no E2B key): it exits at once and stays stopped (restart: on-failure), and nothing
# changes for anyone. Configured, or switched off with TRIAL_BUILD=off (which still kills our sandboxes):
# the pinned requirements go into /venv (a volume, kept between starts), then the worker runs.
set -eu
umask 077
if [ -z "${E2B_API_KEY:-}" ]; then
  python -m server.trial_build_config check || true      # says what is missing (names only)
  exit 0
fi
[ -x /venv/bin/python ] || python -m venv /venv
/venv/bin/pip install -q --disable-pip-version-check --no-cache-dir -r requirements-trial.txt
exec /venv/bin/python -m server.trial_worker
