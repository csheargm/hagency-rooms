"""End-to-end: code changes and crashes don't lose work. Runs supervise.py (real Matrix, real Codex,
fake GitHub). Stop local/start.sh first — this starts its own agent adapters. Run: .venv/bin/python -m tests.test_restart_e2e

A. Live code change: anna asks anna-agent two things around a code save. Both are answered,
   each exactly once, across the restart (the second may arrive while the old process drains).
B. Crash mid-step: the builder's agent adapter is SIGKILLed right after it claims an issue. The
   supervisor revives it, the loop's watchdog resends the step, and the issue still gets merged.
"""
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from tests import test_loop_e2e
from common.setup_accounts import SERVER_NAME, load_env, login
from tests.test_e2e import api, check, test_room
from tests.test_loop_e2e import make_fake_github, say, wait_for
from tests import ROOT

STEP_TIMEOUT_S = 60
test_loop_e2e.ISSUES = {"210": {"title": "docs: add docs/RESTART.md", "state": "OPEN", "labels": ["agent-ok"],
                                "body": "Acceptance criteria:\n- New file docs/RESTART.md whose only line is exactly "
                                        "'# Restart'.\n- No other files change."}}


def replies(token, room_id, sender, word, since_ts):
    evs = api("GET", f"/_matrix/client/v3/rooms/{room_id}/messages?dir=b&limit=50", token)["chunk"]
    return [e for e in evs if e["sender"] == sender and e.get("origin_server_ts", 0) >= since_ts
            and word in e.get("content", {}).get("body", "")]


def main():
    anna = login("anna", load_env()["PW_ANNA"])
    room_id = test_room()
    tmp = Path(tempfile.mkdtemp(prefix="restart-e2e-"))
    env = {**os.environ, **make_fake_github(tmp), "LOOP_STEP_TIMEOUT_S": str(STEP_TIMEOUT_S)}
    out = open(tmp / "supervise.log", "w")
    sup = subprocess.Popen([str(ROOT / ".venv/bin/python"), "bin/supervise.py"], cwd=ROOT, env=env,
                           stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
    agent = lambda who: f"@{who}-agent:{SERVER_NAME}"
    bot = f"@pm:{SERVER_NAME}"
    try:
        time.sleep(8)                                   # @pm online before the first command
        say(anna, room_id, "!level off")
        time.sleep(2)
        # A. two questions around a code save
        ts = int(time.time() * 1000)
        say(anna, room_id, "@anna-agent reply with just the word ALPHA")
        time.sleep(2)
        os.utime(ROOT / "common" / "lifecycle.py")      # "save" a code file in a package (ADR 0011)
        time.sleep(1.5)
        say(anna, room_id, "@anna-agent reply with just the word BRAVO")
        deadline = time.time() + 300
        while time.time() < deadline and not (replies(anna, room_id, agent("anna"), "ALPHA", ts)
                                              and replies(anna, room_id, agent("anna"), "BRAVO", ts)):
            time.sleep(3)
        time.sleep(10)                                  # room for a duplicate to show up
        a, b = (len(replies(anna, room_id, agent("anna"), w, ts)) for w in ("ALPHA", "BRAVO"))
        check("code save restarted the processes", "code changed (lifecycle.py)" in (tmp / "supervise.log").read_text())
        check("question before the restart answered once", a == 1, f"{a} replies")
        check("question during the restart answered once", b == 1, f"{b} replies")

        # B. builder crashes right after claiming; watchdog resends; issue still merged
        ts = int(time.time() * 1000)
        say(anna, room_id, "!level autonomous")
        wait_for(anna, room_id, lambda s, body: s == agent("anna") and "claimed #210" in body, "claim", ts)
        pid = subprocess.run(["pgrep", "-f", "bridge.py anna "], capture_output=True, text=True).stdout.split()
        os.kill(int(pid[0]), signal.SIGKILL)
        check("builder agent adapter killed mid-step", True, f"pid {pid[0]}")
        body = wait_for(anna, room_id, lambda s, b: s == bot and ("asked again" in b or "stopped" in b), "resend", ts)
        check("loop resends the step that got no answer", "asked again" in body, body[:100])
        body = wait_for(anna, room_id, lambda s, b: s == agent("anna") and b.startswith(("merged", "⚠️")), "merge", ts)
        check("issue still built, reviewed and merged", body.startswith("merged PR #301"), body[:100])
        print("ALL PASS")
    finally:
        say(anna, room_id, "!level off")
        time.sleep(2)
        os.killpg(sup.pid, signal.SIGINT)
        sup.wait(timeout=30)
        print(f"supervisor log: {tmp / 'supervise.log'}", file=sys.stderr)


if __name__ == "__main__":
    main()
