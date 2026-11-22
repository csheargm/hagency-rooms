"""End-to-end test of the GitHub loop over the real Matrix server with real Codex agents and a
fake GitHub (tests/fake_gh.py + a local bare repo). Starts its own anna/betty agent adapters,
@pm — stop local/start.sh's agent adapters first. Run: .venv/bin/python -m tests.test_loop_e2e

Flow checked: Autonomous level → an already-satisfied issue is verified by the other agent and
closed (not stopped). Then: issue claimed → PR opened → review requested from the
other agent → approved → merged (file really lands on main). Assist level → second issue
reaches "ready — a person merges" and is NOT merged.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from common.setup_accounts import SERVER_NAME, load_env, login
from tests.test_e2e import api, check, test_room
from tests import ROOT

STEP_TIMEOUT_S = 420
ISSUES = {
    "200": {"title": "docs: README starts with '# sandbox'", "state": "OPEN", "labels": ["agent-ok"],
            "body": "Acceptance criteria:\n- README.md's first line is exactly '# sandbox'.\n- No other files change."},
    "201": {"title": "docs: add docs/HELLO.md", "state": "OPEN", "labels": ["agent-ok"],
            "body": "Acceptance criteria:\n- New file docs/HELLO.md whose only line is exactly '# Hello'.\n- No other files change."},
    "202": {"title": "docs: add docs/BYE.md", "state": "OPEN", "labels": ["agent-ok"],
            "body": "Acceptance criteria:\n- New file docs/BYE.md whose only line is exactly '# Bye'.\n- No other files change."},
}


def make_fake_github(tmp: Path) -> dict:
    remote, seed = tmp / "remote.git", tmp / "seed"
    subprocess.run(["git", "init", "--quiet", "--bare", "-b", "main", str(remote)], check=True)
    subprocess.run(["git", "clone", "--quiet", str(remote), str(seed)], check=True, capture_output=True)
    (seed / "README.md").write_text("# sandbox\n")
    for c in (["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@x", "commit", "--quiet", "-m", "init"],
              ["push", "--quiet", "origin", "HEAD:main"]):
        subprocess.run(["git", *c], cwd=seed, check=True)
    state = tmp / "gh.json"
    state.write_text(json.dumps({"remote": str(remote), "issues": ISSUES, "prs": {}, "next": 301, "protected": True}))
    ids = tmp / "ids"
    for agent in ("anna-agent", "betty-agent"):
        (ids / agent).mkdir(parents=True)
        (ids / agent / "hosts.yml").write_text("github.com: {}\n")   # fake identity marker only
    return {"FAKE_GH_STATE": str(state), "GH_BIN": str(ROOT / "tests" / "fake_gh.py"),
            "GH_IDENTITY_ROOT": str(ids), "LOOP_REPO": "test/sandbox", "LOOP_POLL_S": "3", "LOOP_STATE": str(tmp / "loop-state.json"),
            "HANDOFF_DIR": str(tmp / "handoff")}


def wait_for(token, room_id, pred, what, since_ts):
    deadline = time.time() + STEP_TIMEOUT_S
    while time.time() < deadline:
        for ev in api("GET", f"/_matrix/client/v3/rooms/{room_id}/messages?dir=b&limit=50", token)["chunk"]:
            body = ev.get("content", {}).get("body", "")
            if body.lstrip("* ").startswith("📋"):
                continue                     # the live status board repeats step words; not a step itself
            if ev.get("origin_server_ts", 0) >= since_ts and pred(ev["sender"], body):
                return body
        time.sleep(2)
    raise AssertionError(f"timed out waiting for: {what}")


def say(token, room_id, body):
    api("PUT", f"/_matrix/client/v3/rooms/{room_id}/send/m.room.message/t{time.time_ns()}", token,
        {"msgtype": "m.text", "body": body})


def main():
    env_pw = load_env()
    anna = login("anna", env_pw["PW_ANNA"])
    room_id = test_room()
    tmp = Path(tempfile.mkdtemp(prefix="loop-e2e-"))
    env = {**os.environ, **make_fake_github(tmp)}
    py = str(ROOT / ".venv" / "bin" / "python")
    logs = open(tmp / "procs.log", "w")
    procs = [subprocess.Popen([py, "bin/bridge.py", o], cwd=ROOT, env=env, stdout=logs, stderr=logs) for o in ("anna", "betty")]
    procs += [subprocess.Popen([py, f], cwd=ROOT, env=env, stdout=logs, stderr=logs) for f in ("bin/loop.py",)]
    agent, bot = lambda who: f"@{who}-agent:{SERVER_NAME}", f"@pm:{SERVER_NAME}"
    try:
        time.sleep(6)
        say(anna, room_id, "!level off")   # a previous run may have left the room on autonomous
        time.sleep(2)
        t0 = time.time()
        ts = int(t0 * 1000)
        say(anna, room_id, "!level autonomous")
        wait_for(anna, room_id, lambda s, b: s == bot and "loop level: autonomous" in b, "level set", ts)
        check("anna sets level autonomous", True, f"{time.time()-t0:.0f}s")
        # issue 200 is already satisfied: anna-agent builds nothing, betty-agent verifies, anna-agent closes
        body = wait_for(anna, room_id, lambda s, b: s == agent("anna") and b.startswith(("no change needed for #200", "⚠️")), "no-change", ts)
        check("anna-agent finds nothing to change for #200", body.startswith("no change"), f"{time.time()-t0:.0f}s: {body[:100]}")
        body = wait_for(anna, room_id, lambda s, b: s == agent("betty") and ("#200" in b) and not b.startswith("verify:"), "verify", ts)
        check("betty-agent confirms #200 is already done", body.startswith("confirmed #200"), f"{time.time()-t0:.0f}s: {body[:100]}")
        body = wait_for(anna, room_id, lambda s, b: s == bot and "#200 closed" in b, "closed", ts)
        state = json.loads((tmp / "gh.json").read_text())
        check("autonomous: #200 closed on GitHub by the loop", state["issues"]["200"]["state"] == "CLOSED" and "by the loop" in body,
              f"{time.time()-t0:.0f}s")
        # issue 201: 201 % 2 agents → betty-agent builds, anna-agent reviews
        wait_for(anna, room_id, lambda s, b: s == agent("betty") and "claimed #201" in b, "claim", ts)
        check("betty-agent claims #201", True, f"{time.time()-t0:.0f}s")
        body = wait_for(anna, room_id, lambda s, b: s == agent("betty") and b.startswith(("PR #", "⚠️")), "PR", ts)
        check("betty-agent opens a PR", "opened for #201" in body, f"{time.time()-t0:.0f}s: {body[:100]}")
        wait_for(anna, room_id, lambda s, b: s == bot and "review requested from anna-agent" in b, "review request", ts)
        check("review requested from the other person's agent", True, f"{time.time()-t0:.0f}s")
        body = wait_for(anna, room_id, lambda s, b: s == agent("anna") and b.startswith(("approved", "changes requested", "⚠️")), "verdict", ts)
        check("anna-agent approves PR #301", body.startswith("approved"), f"{time.time()-t0:.0f}s: {body[:100]}")
        body = wait_for(anna, room_id, lambda s, b: s == agent("betty") and b.startswith(("merged", "⚠️")), "merge", ts)
        check("autonomous: builder merges after GitHub says ready", body.startswith("merged PR #301"), f"{time.time()-t0:.0f}s")
        main_tree = subprocess.run(["git", "--git-dir", str(tmp / "remote.git"), "show", "main:docs/HELLO.md"],
                                   capture_output=True, text=True).stdout
        check("docs/HELLO.md is on main with the right content", main_tree.strip() == "# Hello", repr(main_tree))

        t1 = time.time()
        ts = int(t1 * 1000)
        say(anna, room_id, "!level assist")
        body = wait_for(anna, room_id, lambda s, b: s == bot and ("ready — a person merges" in b or "stopped" in b), "assist ready", ts)
        check("assist: #202 reaches 'ready — a person merges'", "PR #302 ready" in body, f"{time.time()-t1:.0f}s")
        time.sleep(8)
        state = json.loads((tmp / "gh.json").read_text())
        check("assist: bot did not merge", state["prs"]["302"]["state"] == "OPEN")
        print(f"ALL PASS ({time.time()-t0:.0f}s total)")
    finally:
        say(anna, room_id, "!level off")
        time.sleep(2)
        for p in procs:
            p.terminate()
        print(f"process log: {tmp / 'procs.log'}", file=sys.stderr)


if __name__ == "__main__":
    main()
