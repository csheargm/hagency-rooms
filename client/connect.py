"""Connect your own agent to Hagency Rooms, from your own computer: Codex, Claude Code, Hermes, a
model in Ollama, or any OpenAI-compatible service (the kinds: agent_kinds.py, docs/adr/0008-agent-kinds.md).

  python bin/connect.py CODE [--agent KIND] [--model M] [--base-url URL]   # once, with the code @pm sent you
  python bin/connect.py                               # later: start it again with the saved sign-in
  python bin/connect.py --stop                        # stop it and remove the background service
  python bin/connect.py --name cx-codex-agent [--stop] # the same for a further agent of yours

It trades @pm's one-time code for your agent's sign-in (POST <server>/connect), saves it in
~/.hagency/credentials.json (mode 600, never in this folder; signin_place.py: never over another
agent's, which a second person pairing on this computer would otherwise do), and runs the agent as a
background service: bridge.py (the agent adapter), or hermes_local.py for Hermes (agent_service.py:
launchd on macOS, systemd --user on Linux; --foreground, or
neither available, runs it here instead). It works on this computer and obeys only you. A further
agent of yours (one @pm made with ➕) keeps its own sign-in, log and service in ~/.hagency/<its name>/,
so several run side by side. The kind is --agent, else the one @pm's connect info names, else the
last one, else Codex or Claude Code (whichever is installed). connect_kinds.py checks what each kind
needs and asks for its settings (an Ollama model, a remote service's address, model and API key:
the key only here, never echoed, kept in remote.env, mode 600).
connect.sh (served by the Hagency console at /connect.sh) installs this and starts it.
"""
import argparse
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from common import agent_kinds
from client import agent_service
from adapters.agents import cli_path
from client import connect_kinds
from client import service_migration
from client import signin_place
from client.signin_place import agent_name, name_arg, read_saved
from common import identity
from client.ask_line import ask_line
from common.bridge_config import credentials_match
from common import paths

APP = paths.ROOT.resolve()
CONNECT = paths.launcher(APP, "connect.py")    # how people run this again
BACKENDS = agent_kinds.LEGACY                 # what an agent with no kind runs, as before
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")
TIMEOUT_S = 30
USER_RE = re.compile(r"^@[a-z0-9-]{2,80}-agent:[A-Za-z0-9.-]+(:\d+)?$")


def safe_url(url: str) -> bool:
    """https, or plain http only to this computer (a password travels over it)."""
    parts = urllib.parse.urlsplit(url)
    return bool(parts.hostname) and (parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in LOCAL_HOSTS))


def redeem(server: str, code: str) -> dict:
    """The agent's sign-in, from a one-time code (the server forgets the code once used)."""
    req = urllib.request.Request(server.rstrip("/") + "/connect", method="POST", data=json.dumps({"code": code}).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            reason = json.loads(e.read()).get("error")
        except ValueError:
            reason = None
        raise SystemExit(f"✗ {reason or f'the server said {e.code}'}") from e
    except (OSError, ValueError) as e:
        raise SystemExit(f"✗ couldn't reach {server}: {e}") from e
    owner = data.get("owner") if isinstance(data, dict) else None
    if not (isinstance(data, dict) and USER_RE.match(str(data.get("user_id", ""))) and safe_url(str(data.get("homeserver", "")))
            and isinstance(owner, str) and (identity.USER_RE.match(owner) or identity.NAME_RE.match(owner))
            and credentials_match(data["user_id"], owner, data.get("label")) and isinstance(data.get("password"), str)):
        raise SystemExit("✗ the server's answer doesn't look like an agent's sign-in; nothing was saved")
    try:
        offer = agent_kinds.from_connect_info(data)      # agent_kind / model / base_url, when @pm names them
    except agent_kinds.BadConnectInfo as e:
        offer = {"offer_error": str(e)}             # the sign-in is still saved first (the code is used up)
    return {**{k: data[k] for k in ("homeserver", "user_id", "password", "owner", "label") if k in data}, **offer}


def agent_dir(home: Path, creds: dict) -> Path:
    """Where this agent's sign-in, log and service live: ~/.hagency for a first agent, its own
    folder for a further one (its name was checked: [a-z0-9-] only)."""
    return home / agent_name(creds) if creds.get("label") else home


def save(path: Path, data: dict) -> None:
    """Written as 0600 (in a 0700 folder, whatever the umask made it) and swapped in whole."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name != "nt" and path.parent.stat().st_mode & 0o077:   # Windows: no mode bits; connect.ps1 sets the ACL
        print(f"note: {path.parent} can be read by others; the sign-in file itself is only yours", file=sys.stderr)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".credentials-")   # 0600, a new file: no symlink games
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def pick_backend(given: str | None, saved: str | None, ask=input) -> str:
    """--agent, else the last choice, else the one CLI installed here (or ask when both are)."""
    for choice in (given, saved):
        if choice in BACKENDS:
            if not cli_path.startable(choice):
                raise SystemExit(f"✗ {choice} isn't installed here (or not on PATH)")
            return choice
    found = [b for b in BACKENDS if cli_path.startable(b)]
    if len(found) == 1:
        return found[0]
    if not found:
        raise SystemExit("✗ install Codex (npm i -g @openai/codex) or Claude Code (npm i -g @anthropic-ai/claude-code) first")
    if not sys.stdin.isatty():
        raise SystemExit("✗ both Codex and Claude Code are installed: choose one with --agent codex or --agent claude")
    while (choice := ask_line("Which one should be your agent, codex or claude?", ask).lower()) not in BACKENDS:
        print("Please type codex or claude.")
    return choice


def background(creds: dict, path: Path, home: Path) -> bool:
    """Start the agent as a background service; False when this computer has none (run it here)."""
    try:
        how = agent_service.install(APP, creds["owner"], creds["agent"], agent_name(creds), path)
    except (OSError, RuntimeError) as e:     # said plainly, then it runs in the foreground instead
        print(f"note: couldn't start it in the background ({e}); running it here instead", file=sys.stderr)
        return False
    if how is None:
        return False
    print(f"✓ Your agent now runs in the background ({how}): you can close this Terminal. It starts again when\n"
          f"  you log in. Its log: {path.parent / 'logs' / (agent_name(creds) + '.log')}\n"
          f"  To stop it: {sys.executable} {CONNECT} {name_arg(home, path)}--stop")
    return True


def stop(saved: dict, home: Path) -> None:
    name = agent_name(saved)
    if not name:
        raise SystemExit("✗ not connected on this computer: nothing to stop")
    print(f"✓ stopped {name} and removed its background service" if agent_service.stop(name, home=home, app=APP)
          else f"✓ {name} had no background service here (if it runs in a Terminal, press Ctrl-C there)")


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("code", nargs="?", help="the one-time code from @pm (not needed once connected)")
    ap.add_argument("--agent", choices=agent_kinds.KINDS, help="which kind of agent (default: what @pm set up)")
    ap.add_argument("--model", help="ollama / remote: the model to use")
    ap.add_argument("--base-url", help="remote: the OpenAI-compatible service's address (https://…/v1)")
    ap.add_argument("--new-key", action="store_true", help="remote: type the service's API key again")
    ap.add_argument("--server", default=os.environ.get("HAGENCY_CONSOLE", "http://localhost:8650"),
                    help="the Hagency console that sent the code")
    ap.add_argument("--home", type=Path, default=Path(os.environ.get("HAGENCY_DIR", Path.home() / ".hagency")),
                    help="where the sign-in is kept (default ~/.hagency)")
    ap.add_argument("--pair-only", action="store_true", help="save the sign-in, don't start the agent")
    ap.add_argument("--foreground", action="store_true", help="run here (Ctrl-C stops it), not as a background service")
    ap.add_argument("--stop", action="store_true", help="stop the agent and remove its background service")
    ap.add_argument("--name", help="a further agent of yours, by its name (e.g. cx-codex-agent)")
    args = ap.parse_args()
    if args.name and not USER_RE.match(f"@{args.name}:x"):
        raise SystemExit(f"✗ {args.name} isn't an agent's name (like cx-codex-agent)")
    return args


def check_first(args) -> None:
    """Before the one-time code is used up: the chosen kind (or at least some agent CLI) is here."""
    why = connect_kinds.problem(args.agent) if args.agent else None
    if why:
        raise SystemExit(f"✗ {why}")
    if args.code and not args.agent and not connect_kinds.anything_installed():
        raise SystemExit("✗ install your agent first: Codex (npm i -g @openai/codex), Claude Code "
                         "(npm i -g @anthropic-ai/claude-code) or Hermes Agent")


def pick_kind(args, offered: dict, saved: dict) -> str:
    """The kind this agent runs: --agent, @pm's, the last one, a further agent's label ("codex"), or
    Codex / Claude Code as before."""
    kind = connect_kinds.choose(args.agent, offered.get("agent_kind"), saved.get("agent"))
    label = saved.get("label")
    if kind is None and label in agent_kinds.LEGACY and cli_path.startable(label):
        kind = label
    return kind or pick_backend(None, None)


def connect_here(args, home: Path, path: Path, saved: dict) -> tuple[dict, Path]:
    """Redeem the code if there is one, then choose the kind and its settings. Saved at each step,
    so a rerun without a code picks up where this one stopped."""
    offered = {}
    if args.code:
        if not safe_url(args.server):
            raise SystemExit(f"✗ {args.server}: use https (plain http only for this computer)")
        fresh = redeem(args.server, args.code)
        offered = {k: fresh.pop(k) for k in agent_kinds.OFFER_KEYS if k in fresh}
        bad_offer = fresh.pop("offer_error", None)
        path = signin_place.place(home, agent_dir(home, fresh), fresh)   # never over another agent's
        before = read_saved(path)
        saved = {**fresh, **{k: before[k] for k in ("agent", *connect_kinds.KIND_SETTINGS) if k in before}}
        save(path, saved)                                         # the code is used up: keep the sign-in
        if bad_offer and not args.agent:
            raise SystemExit(f"✗ {bad_offer}.\n  Your sign-in is saved: run {sys.executable} {CONNECT} "
                             f"{name_arg(home, path)}--agent KIND (no code needed)")
    kind = pick_kind(args, offered, saved)
    pending = connect_kinds.pending_for(kind, saved, offered)
    save(path, pending)                                           # chosen: a rerun asks only what's missing
    why = connect_kinds.problem(kind)
    if why:
        raise SystemExit(f"✗ {why}\n  Your sign-in is saved; then run {sys.executable} {CONNECT} "
                         f"{name_arg(home, path)}(no code needed)")
    creds = connect_kinds.settings(kind, pending, args, path.parent)
    save(path, creds)
    if kind == "hermes":
        connect_kinds.setup_hermes(APP, path)
    return creds, path


def migrate_services() -> None:
    """Services written before the launchers moved into bin/ (ADR 0012) run them from there again.
    What moved and what didn't is said, and the connect goes on: it installs this agent's own service
    afresh anyway."""
    try:
        moved, failed = service_migration.migrate(APP), {}
    except service_migration.MigrationError as e:
        moved, failed = e.moved, e.failed
    except (OSError, RuntimeError) as e:
        print(f"note: couldn't update your older background agents ({e}); run this again", file=sys.stderr)
        return
    name = service_migration.agent_of
    if moved:
        print(f"✓ updated {len(moved)} background agent(s) to this version: {', '.join(map(name, moved))}")
    if failed:
        print(f"note: couldn't update {len(failed)} background agent(s) yet: {', '.join(map(name, failed))}. They may stay "
              f"offline until you run this same connect line again.\n  why: {'; '.join(failed.values())}",
              file=sys.stderr)


def main() -> None:
    args = parse_args()
    migrate_services()
    home = args.home.resolve()
    path = (home / args.name if args.name else home) / "credentials.json"
    saved = read_saved(path)
    if args.stop:
        stop(saved, path.parent)
        return
    if not args.code and not saved:
        raise SystemExit("✗ not connected yet: run this with the code @pm sent you")
    check_first(args)
    creds, path = connect_here(args, home, path, saved)
    for agent in signin_place.stranded(home, skip=agent_name(creds)):
        print(f"✗ {signin_place.REPLACED.format(agent)}", file=sys.stderr)
    print(f"✓ connected as {creds['user_id']} ({creds['agent']}); only {creds['owner']} can direct it")
    if args.pair_only:
        if signin_place.kept_aside(home, path) and os.name == "nt":   # connect.ps1 registers only the first agent's task
            raise SystemExit(f"✓ saved. On Windows this agent runs in a window for now: "
                             f"{sys.executable} {CONNECT} {name_arg(home, path)}--foreground")
        return
    if not args.foreground and background(creds, path, home):
        return
    print("Your agent is online while this runs (Ctrl-C stops it).", flush=True)
    os.chdir(APP)
    os.execve(sys.executable, agent_service.command(APP, creds["owner"], creds["agent"]),
              {**os.environ, "HAGENCY_CREDENTIALS": str(path)})


if __name__ == "__main__":
    main()
