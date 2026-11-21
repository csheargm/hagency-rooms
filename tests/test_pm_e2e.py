"""End-to-end: people run the project by talking to @pm in plain words. Real Matrix, fake GitHub,
no agents needed. Stop ./start.sh first (this starts its own @pm). Run: .venv/bin/python -m tests.test_pm_e2e

- "where are we?" answers with the mode; anyone can ask.
- betty (a contributor) can't switch to autonomous; anna (the owner) can, by saying so.
- betty can stop everything at once.
"""
import os
import subprocess
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path

from common import github
from common.setup_accounts import SERVER_NAME, load_env, login
from tests.test_e2e import api, check, send, test_room
from tests.test_loop_e2e import make_fake_github, wait_for
from tests import ROOT

PM = f"@pm:{SERVER_NAME}"


def level(token, room_id):
    path = f"/_matrix/client/v3/rooms/{urllib.parse.quote(room_id)}/state/{github.LEVEL_EVENT}/"
    return api("GET", path, token).get("level")


def pm_says(token, room_id, words, ts):
    return wait_for(token, room_id, lambda s, b: s == PM and words in b, words, ts)


def main():
    pw = load_env()
    anna, betty = login("anna", pw["PW_ANNA"]), login("betty", pw["PW_BETTY"])
    room_id = test_room()
    tmp = Path(tempfile.mkdtemp(prefix="pm-e2e-"))
    env = {**os.environ, **make_fake_github(tmp)}
    pm = subprocess.Popen([str(ROOT / ".venv/bin/python"), "loop.py"], cwd=ROOT, env=env,
                          stdout=open(tmp / "pm.log", "w"), stderr=subprocess.STDOUT)
    try:
        time.sleep(6)
        ts = int(time.time() * 1000)
        send(betty, room_id, "@pm stop", PM)                      # start from a known mode
        pm_says(anna, room_id, "Stopped", ts)
        check("anyone can stop at once", level(anna, room_id) == "off")

        send(betty, room_id, "@pm where are we?", PM)
        check("status in plain words", True, pm_says(anna, room_id, "Mode: stopped", ts)[:80])

        send(betty, room_id, "@pm take it all the way to the end", PM)
        pm_says(anna, room_id, "Only the room owner or a maintainer", ts)
        check("a contributor can't switch to autonomous", level(anna, room_id) == "off")

        ts = int(time.time() * 1000)
        send(anna, room_id, "@pm take it all the way to the end", PM)
        pm_says(anna, room_id, "Running on my own now", ts)
        check("the owner's plain request switches to autonomous at once", level(anna, room_id) == "autonomous")

        ts = int(time.time() * 1000)
        send(betty, room_id, "@pm kill all the AI", PM)
        pm_says(anna, room_id, "Stopped", ts)
        check("betty stops it again", level(anna, room_id) == "off")
        print("ALL PASS")
    finally:
        pm.terminate()
        print(f"pm log: {tmp / 'pm.log'}", file=sys.stderr)


if __name__ == "__main__":
    main()
