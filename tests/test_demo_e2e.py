"""End-to-end: the demo's product cycle, driven only by people talking. Real Matrix, real Codex
agents (anna-agent, betty-agent), fake GitHub starting with NO issues. Stop local/start.sh first.
Run: .venv/bin/python -m tests.test_demo_e2e           (both agents Codex, via the agent adapter)
     DEMO_ANNA=hermes .venv/bin/python -m tests.test_demo_e2e   (anna's agent is Hermes, as in the demo)

1. anna and betty agree on a tiny project in plain chat.
2. "@pm let's build it" → anna's own agent drafts the plan → anna ✅ → issues opened (as anna-agent).
3. "@pm take it all the way" → ✅ → agents build, review and merge; the file is on main.
4. They iterate: one more requirement, "@pm let's build it" again → only the NEW item is planned.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path

from adapters.agents import hermes_profile
from tests import test_loop_e2e
from common.setup_accounts import SERVER_NAME, load_env, login
from tests.test_e2e import api, check, send, test_room
from tests.test_loop_e2e import make_fake_github, say, wait_for
from tests import ROOT

PM = f"@pm:{SERVER_NAME}"
test_loop_e2e.ISSUES = {}                       # the project starts from nothing but the discussion
test_loop_e2e.STEP_TIMEOUT_S = 600


def confirm_latest(anna, room_id, words, ts):
    """anna reacts ✅ to @pm's newest message containing `words`."""
    wait_for(anna, room_id, lambda s, b: s == PM and words in b, words, ts)
    evs = api("GET", f"/_matrix/client/v3/rooms/{urllib.parse.quote(room_id)}/messages?dir=b&limit=20", anna)["chunk"]
    target = next(e for e in evs if e["sender"] == PM and words in e.get("content", {}).get("body", ""))
    api("PUT", f"/_matrix/client/v3/rooms/{urllib.parse.quote(room_id)}/send/m.reaction/r{time.time_ns()}", anna,
        {"m.relates_to": {"rel_type": "m.annotation", "event_id": target["event_id"], "key": "✅"}})
    return target["content"]["body"]


def start_anna(env: dict, pw: dict, logs) -> list[subprocess.Popen]:
    """anna's agent: the Codex agent adapter, or (DEMO_ANNA=hermes) Hermes through the hagency MCP server."""
    if os.environ.get("DEMO_ANNA") != "hermes":
        return [subprocess.Popen([str(ROOT / ".venv/bin/python"), "bin/bridge.py", "anna"], cwd=ROOT, env=env,
                                 stdout=logs, stderr=logs)]
    gh_env = {k: env[k] for k in ("FAKE_GH_STATE", "GH_BIN", "GH_IDENTITY_ROOT", "LOOP_REPO")}
    profiles = hermes_profile.configure_both("annademo", owner="anna", agent="anna-agent",
                                             password=pw["PW_ANNA_AGENT"], home_room=None, mcp_env=gh_env)
    for profile in profiles:                 # anna's own profile and @pm's loop one: no memory of earlier runs
        for stale in ("sessions", "state.db", "state.db-wal", "state.db-shm"):
            path = profile / stale
            shutil.rmtree(path) if path.is_dir() else path.unlink(missing_ok=True)
    return [subprocess.Popen(["hermes", "-p", p.name, "gateway", "run"], stdout=logs, stderr=logs) for p in profiles]


def main():
    pw = load_env()
    anna, betty = login("anna", pw["PW_ANNA"]), login("betty", pw["PW_BETTY"])
    room_id = test_room()
    tmp = Path(tempfile.mkdtemp(prefix="demo-e2e-"))
    env = {**os.environ, **make_fake_github(tmp)}
    py, logs = str(ROOT / ".venv/bin/python"), open(tmp / "procs.log", "w")
    procs = [subprocess.Popen([py, *a], cwd=ROOT, env=env, stdout=logs, stderr=logs)
             for a in (["bin/bridge.py", "betty"], ["bin/loop.py"])]
    procs += start_anna(env, pw, logs)
    gh = lambda: json.loads((tmp / "gh.json").read_text())
    try:
        time.sleep(25 if os.environ.get("DEMO_ANNA") == "hermes" else 8)
        say(anna, room_id, "!level off")
        time.sleep(2)
        t0 = ts = int(time.time() * 1000)
        say(anna, room_id, "Let's make a tiny Hello project. First thing: a file docs/HELLO.md whose only line is exactly '# Hello'.")
        say(betty, room_id, "Agreed, just that file for now, nothing else.")
        send(anna, room_id, "@pm let's build it", PM)
        plan = confirm_latest(anna, room_id, "👍 to open these", ts)
        check("anna's agent drafts a plan from the discussion", "HELLO.md" in plan, plan[:120].replace("\n", " "))
        body = wait_for(anna, room_id, lambda s, b: s == PM and b.startswith(("Opened", "⚠️")), "issues opened", ts)
        issues = [i for i in gh()["issues"].values() if i["state"] == "OPEN"]
        check("✅ opens the issues on GitHub, as anna's agent", body.startswith("Opened") and issues
              and all(i["author"] == "anna-agent" for i in issues), body[:80])

        send(anna, room_id, "@pm take it all the way", PM)
        wait_for(anna, room_id, lambda s, b: s == PM and "Running on my own now" in b, "autonomous", ts)
        body = wait_for(anna, room_id, lambda s, b: s.endswith("-agent:" + SERVER_NAME) and b.startswith(("merged", "⚠️")), "merge", ts)
        main_file = subprocess.run(["git", "--git-dir", str(tmp / "remote.git"), "show", "main:docs/HELLO.md"],
                                   capture_output=True, text=True).stdout
        check("agents build, review and merge it", body.startswith("merged") and main_file.strip() == "# Hello",
              f"{(time.time()*1000 - t0)/1000:.0f}s")

        ts = int(time.time() * 1000)
        say(betty, room_id, "Tried it, nice. One more thing: add docs/BYE.md whose only line is exactly '# Bye'.")
        say(anna, room_id, "Yes, add that too.")
        send(anna, room_id, "@pm let's build it", PM)
        plan = confirm_latest(anna, room_id, "👍 to open these", ts)
        check("iteration plans only the new item", "BYE" in plan and "HELLO" not in plan, plan[:120].replace("\n", " "))
        wait_for(anna, room_id, lambda s, b: s.endswith("-agent:" + SERVER_NAME) and "merged" in b and "#" in b, "merge 2", ts)
        bye = subprocess.run(["git", "--git-dir", str(tmp / "remote.git"), "show", "main:docs/BYE.md"],
                             capture_output=True, text=True).stdout
        check("the change is built and merged too", bye.strip() == "# Bye", f"{(time.time()*1000 - t0)/1000:.0f}s total")
        print("ALL PASS")
    finally:
        say(anna, room_id, "!level off")
        time.sleep(2)
        for p in procs:
            p.terminate()
        print(f"process log: {tmp / 'procs.log'}", file=sys.stderr)


if __name__ == "__main__":
    main()
