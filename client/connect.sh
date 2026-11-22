#!/bin/sh
# Hagency Rooms: connect your own agent from this computer: Codex, Claude Code, Hermes, a model in
# Ollama or any OpenAI-compatible service (docs/adr/0008-agent-kinds.md).
#   curl -fsSL __HAGENCY_CONSOLE__/connect.sh | sh -s -- CODE [--agent KIND] [--model M] [--base-url URL]
# Questions (an Ollama model, a service's API key: never echoed) are asked at this terminal.
# Installs (or updates) Hagency Rooms in ~/.hagency/app, then runs connect.py with @pm's one-time
# code. Your agent's sign-in is kept in ~/.hagency/credentials.json (only you can read it).
# It then runs in the background (launchd / systemd --user); run this again any time, it's safe.
# To stop it: ~/.hagency/app/.venv/bin/python ~/.hagency/app/connect.py --stop
set -eu
CONSOLE="${HAGENCY_CONSOLE:-__HAGENCY_CONSOLE__}"
REPO="${HAGENCY_REPO:-https://github.com/csheargm/hagency-rooms}"
APP="${HAGENCY_APP:-$HOME/.hagency/app}"     # HAGENCY_APP=/path/to/a/checkout uses that one as it is

need() { command -v "$1" >/dev/null 2>&1 || { echo "✗ please install $1 first"; exit 1; }; }
need git
PY=""
for p in python3.13 python3.12 python3.11 python3; do      # macOS's own python3 may be too old
  if command -v "$p" >/dev/null 2>&1 && "$p" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then PY="$p"; break; fi
done
[ -n "$PY" ] || { echo "✗ needs Python 3.11 or newer (e.g. brew install python@3.12)"; exit 1; }

umask 077                                    # ~/.hagency and the sign-in: only you
if [ -n "${HAGENCY_APP:-}" ]; then
  echo "Using $APP"
elif [ -d "$APP/.git" ]; then
  echo "Updating $APP"; git -C "$APP" pull -q --ff-only
else
  echo "Installing Hagency Rooms in $APP"; mkdir -p "$(dirname "$APP")"; git clone -q --depth 1 "$REPO" "$APP"
fi
if [ -x "$APP/.venv/bin/python" ] && ! "$APP/.venv/bin/python" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
  "$PY" -m venv --clear "$APP/.venv"           # an older Python's venv: rebuilt with this one
fi
[ -x "$APP/.venv/bin/python" ] || "$PY" -m venv "$APP/.venv"
"$APP/.venv/bin/pip" install -q --disable-pip-version-check -r "$APP/requirements.txt"

# Piped from curl, this script's stdin is the download: read questions from the terminal if there is one.
if (: </dev/tty) 2>/dev/null; then
  exec "$APP/.venv/bin/python" "$APP/connect.py" --server "$CONSOLE" "$@" </dev/tty
fi
exec "$APP/.venv/bin/python" "$APP/connect.py" --server "$CONSOLE" "$@"
