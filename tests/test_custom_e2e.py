"""Live: a person changes their agent's code from their private room, uses it, then goes back.
Betty's agent is our bridge (Codex/Claude Code); Anna's is Hermes (CUSTOM_OWNER=anna). Needs ./start.sh
running. Takes a few minutes (a real agent edit). It starts by deleting that person's private copy
(custom/<owner>): a test install, not someone's real one.
Run: .venv/bin/python -m tests.test_custom_e2e      or: CUSTOM_OWNER=anna .venv/bin/python -m tests.test_custom_e2e"""
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from common import customize
from common import registry
from common.setup_accounts import SERVER_NAME, load_env, login
from tests.test_e2e import api, check, send

OWNER = os.environ.get("CUSTOM_OWNER", "betty")
AGENT = f"@{OWNER}-agent:{SERVER_NAME}"
REQUEST = ("change your code so the voice transcript caption starts with \"🎙 Heard:\" instead of "
           "\"📝 Transcript:\"")
WAIT_S = 900


def running_dir(owner: str) -> Path:
    """The folder the agent's code really runs from (not just what the state file says): the bridge's
    folder, or for Hermes the folder of the hagency_mcp.py it started."""
    if registry.load().get(registry.person_id(owner), {}).get("agent") == "hermes":
        cmds = subprocess.run(["pgrep", "-fl", "hagency_mcp.py"], capture_output=True, text=True).stdout.split()
        return Path(next(c for c in cmds if c.endswith("hagency_mcp.py"))).resolve().parent
    pid = subprocess.run(["pgrep", "-f", f"bridge.py {owner} --agent"], capture_output=True, text=True).stdout.split()[0]
    out = subprocess.run(["lsof", "-a", "-p", pid, "-d", "cwd", "-Fn"], capture_output=True, text=True).stdout
    return Path(next(line[1:] for line in out.splitlines() if line.startswith("n"))).resolve()


def private_room(token: str) -> str:
    room = api("POST", "/_matrix/client/v3/createRoom", token, {
        "preset": "trusted_private_chat", "is_direct": True, "invite": [AGENT], "name": f"{OWNER.capitalize()}'s code (test)"})["room_id"]
    for _ in range(30):
        members = api("GET", f"/_matrix/client/v3/rooms/{room}/joined_members", token)["joined"]
        if AGENT in members:
            return room
        time.sleep(1)
    raise AssertionError(f"{AGENT} didn't join the private room")


def wait_for(token: str, room: str, since: str, needle: str, timeout: float = WAIT_S) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        msgs = api("GET", f"/_matrix/client/v3/rooms/{room}/messages?dir=b&limit=50", token)["chunk"]
        for ev in msgs:
            if ev.get("event_id") == since:
                break
            if ev.get("sender") == AGENT and needle in ev.get("content", {}).get("body", ""):
                return ev
        time.sleep(3)
    raise AssertionError(f"no {needle!r} from {AGENT} within {timeout:.0f}s")


def click(token: str, room: str, event_id: str, key: str) -> None:
    api("PUT", f"/_matrix/client/v3/rooms/{room}/send/m.reaction/r{time.time_ns()}", token,
        {"m.relates_to": {"rel_type": "m.annotation", "event_id": event_id, "key": key}})


def main() -> None:
    shutil.rmtree(customize.COPIES / OWNER, ignore_errors=True)   # start from a fresh private copy
    subprocess.run(["git", "-C", str(customize.ROOT), "worktree", "prune"], check=True)
    subprocess.run(["git", "-C", str(customize.ROOT), "branch", "-D", f"custom/{OWNER}"], capture_output=True)
    token = login(OWNER, load_env()[f"PW_{OWNER.upper()}"])
    room = private_room(token)
    asked = send(token, room, REQUEST, AGENT)
    offer = wait_for(token, room, asked, "Your version is ready")
    check("the agent offers a changed version", True, offer["content"]["body"].splitlines()[0])
    check("the official files are untouched", "📝 Transcript" in (customize.ROOT / "server" / "pm_voice.py").read_text())
    if customize.why_not_use(OWNER):    # this computer runs other people's agents too: PR or discard only
        check("no 'Use it myself' on a shared computer", "Using it here isn't possible" in offer["content"]["body"])
        click(token, room, offer["event_id"], "🗑️")
        wait_for(token, room, offer["event_id"], "Discarded", timeout=120)
        check("discarded", True)
        print(json.dumps({"room": room}))
        return
    click(token, room, offer["event_id"], "👍")
    done = wait_for(token, room, offer["event_id"], "tests pass", timeout=600)
    check("its tests pass and it switches", "Switching" in done["content"]["body"])
    time.sleep(10)
    live, sha = customize.code_dir(OWNER)
    check("the agent runs their version", bool(sha) and running_dir(OWNER) == live.resolve(), (sha or "")[:7])
    asked = send(token, room, "go back to the official version", AGENT)
    wait_for(token, room, asked, "Going back", timeout=120)
    time.sleep(10)
    check("back on the official version", OWNER not in customize.in_use() and running_dir(OWNER) == customize.ROOT.resolve())
    print(json.dumps({"room": room}))


if __name__ == "__main__":
    main()
