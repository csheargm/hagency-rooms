"""The hermes kind on a person's own computer (docs/adr/0008-agent-kinds.md): connect.py's set-up of
the two Hermes profiles (ADR 0006), and what its background service runs.

  python hermes_local.py --setup   # connect.py: write the profiles from the saved sign-in
  python hermes_local.py           # the service: both gateways + the code-change buttons, kept running

Both read the sign-in from HAGENCY_CREDENTIALS (never from argv). As on the server
(supervise.hermes_children), "<name>room" obeys only its owner and "<name>loop" only @pm's loop
tasks, logged in as the same agent account; the hagency MCP server they run signs in with the same
file. A clean exit of the buttons agent adapter (the server disconnected the agent) stops all of it, so the
service stays stopped. macOS and Linux only: Hermes itself runs on Windows only inside WSL.
"""
import argparse
import asyncio
import os
import shutil
from pathlib import Path

from adapters.agents import cli_path

INVITE_PATCH = "docs/hermes-matrix-invite-allowlist.patch"
INVITE_MARK = "_invite_allowed"              # what the patch adds to Hermes's Matrix adapter
ADAPTER = Path("plugins") / "platforms" / "matrix" / "adapter.py"
CONNECT_AGAIN = "connect.py again (no code needed)"


def hermes_home() -> Path:
    return Path.home() / ".hermes"


def hermes_source() -> Path:
    """Hermes Agent's own code: the installer's ~/.hermes/hermes-agent, or HAGENCY_HERMES_SRC."""
    return Path(os.environ.get("HAGENCY_HERMES_SRC") or hermes_home() / "hermes-agent")


def problem() -> str | None:
    """What this computer still needs before a Hermes agent can run here, or None."""
    if os.name == "nt":
        return "Hermes runs on macOS and Linux (on Windows only inside WSL): choose codex, claude, ollama or remote here"
    if not cli_path.startable("hermes"):
        return "install Hermes Agent first (see hermes-agent.nousresearch.com), then set a model: hermes model"
    cfg = hermes_home() / "config.yaml"
    if not cfg.exists() or "provider:" not in cfg.read_text(errors="replace"):
        return "Hermes has no model yet: run `hermes model` (or `hermes setup`) first"
    adapter = hermes_source() / ADAPTER
    if not adapter.exists():
        return (f"can't find Hermes's Matrix adapter to check the invite patch ({adapter}); "
                "set HAGENCY_HERMES_SRC to the hermes-agent folder")
    if INVITE_MARK not in adapter.read_text(errors="replace"):
        return (f"Hermes would join a room anyone invites it to: apply Hagency's patch first, "
                f"patch -p1 -d {hermes_source()} < <app>/{INVITE_PATCH}")
    return None


def _who() -> tuple[dict, str, str]:
    """(credentials, the agent's handle, its owner's full ID), from HAGENCY_CREDENTIALS."""
    from common import bridge_config                      # here: it reads HAGENCY_CREDENTIALS when imported
    from common import identity
    creds = bridge_config.CREDENTIALS
    if not creds:
        raise SystemExit("✗ run this through connect.py (no saved sign-in in HAGENCY_CREDENTIALS)")
    owner_id = identity.person_id(creds["owner"], bridge_config.SERVER_NAME)
    return creds, bridge_config.agent_handle(owner_id), owner_id


def setup() -> tuple[str, str]:
    """Write both profiles for this sign-in; their names."""
    why = problem()
    if why:
        raise SystemExit(f"✗ {why}")
    path = os.environ["HAGENCY_CREDENTIALS"]
    if "'" in path or "\n" in path:           # it goes into the profile's YAML, single-quoted
        raise SystemExit("✗ the sign-in folder's path has a quote in it; use another folder (--home)")
    from adapters.agents import hermes_profile
    creds, person, owner_id = _who()
    if not all(hermes_profile.one_line(v) for v in creds.values() if isinstance(v, str)):
        raise SystemExit("✗ the saved sign-in has a line break in it; ask @pm for a new code")   # before any write
    try:
        hermes_profile.configure_both(person, owner=person, owner_id=owner_id, agent=creds["user_id"][1:].partition(":")[0],
                                      password=creds["password"], home_room=None, mcp_env={"HAGENCY_CREDENTIALS": path})
    except ValueError as e:                   # a line break in a server-sent value: never written into .env
        raise SystemExit(f"✗ {e}; ask @pm for a new code") from e
    return hermes_profile.profile_names(person)


def keep_running() -> None:
    from client import supervise
    creds, person, _ = _who()
    if shutil.which("hermes") is None:
        raise SystemExit("✗ hermes isn't on this service's PATH any more; run " + CONNECT_AGAIN)
    kids = supervise.hermes_children(person, creds["owner"], CONNECT_AGAIN)
    if len(kids) < 3:
        raise SystemExit("✗ the Hermes profiles aren't set up; run " + CONNECT_AGAIN)

    async def go() -> None:
        done = asyncio.Event()
        kids[-1].on_clean_exit = done         # the buttons agent adapter exits 0 when the server disconnects us
        await supervise.run(kids, done)
    asyncio.run(go())


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--setup", action="store_true", help="write the two Hermes profiles (connect.py runs this)")
    if ap.parse_args().setup:
        personal, loop = setup()
        print(f"✓ Hermes profiles ready: '{personal}' (only you) and '{loop}' (only @pm's loop tasks)")
        return
    keep_running()


if __name__ == "__main__":
    main()
