"""End-to-end: a newcomer signs up, @pm welcomes them, they bring their own agent and talk to it.
Needs the stack running (./start.sh) after `python local_signup.py setup`. Run: .venv/bin/python -m tests.test_welcome_e2e
(E2E_AGENT=claude to use Claude Code instead of Codex.) It creates a fresh account newbieNNNN each run.
Against the public server: HAGENCY_HOMESERVER=https://matrix.<domain> HAGENCY_SERVER_NAME=<server>
E2E_CONSOLE=https://rooms.<domain> E2E_TOKEN=<invite token> E2E_NAME=pubtest2.

1. sign up through the client API with an invite token, the way Element does;
2. the new account is in #welcome, and @pm's private chat with the 🔌 button is already joined;
3. no 🏠 server agent is offered (typing it says it comes later); 🔌 creates <them>-agent and sends one install line with a code;
4. connect.py (this checkout, a temporary ~/.hagency) trades the code; the code is then dead;
5. their agent, running on "their computer", answers "who are you?" in its private chat.
"""
import json
import os
import re
import secrets
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from common import local_signup
from common.setup_accounts import HS, SERVER_NAME
from tests.test_e2e import api, check, send, wait_reply
from tests import ROOT

PM = f"@pm:{SERVER_NAME}"
CONSOLE = os.environ.get("E2E_CONSOLE", "http://localhost:8650")
STEP_S = 60


def register(name: str, password: str, token: str) -> str:
    """Element's sign-up: the registration-token stage, then the dummy stage."""
    def post(body):
        req = urllib.request.Request(HS + "/_matrix/client/v3/register", method="POST", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            return json.loads(e.read())
    session = post({})["session"]
    base = {"username": name, "password": password, "initial_device_display_name": "e2e"}
    r = post({**base, "auth": {"type": "m.login.registration_token", "token": token, "session": session}})
    if "access_token" not in r:
        r = post({**base, "auth": {"type": "m.login.dummy", "session": session}})
    if "access_token" not in r:
        raise AssertionError(f"sign-up failed: {r.get('errcode')} {r.get('error')}")
    return r["access_token"]


def until(what, fn, timeout=STEP_S):
    deadline = time.time() + timeout
    while time.time() < deadline:
        found = fn()
        if found:
            return found
        time.sleep(1)
    raise AssertionError(f"timed out waiting for: {what}")


def joined_chat_with(token, me, sender):
    """My private chat with `sender`, already joined (no invite to accept: hagency_auto_accept.py) and
    listed as a direct chat, which is what Element's People list and the home page's link use."""
    try:
        direct = api("GET", f"/_matrix/client/v3/user/{urllib.parse.quote(me)}/account_data/m.direct", token)
    except urllib.error.HTTPError as e:
        if e.code == 404:                     # no direct chats yet: keep waiting
            return None
        raise
    joined = set(api("GET", "/_matrix/client/v3/joined_rooms", token)["joined_rooms"])
    return next((room for room in direct.get(sender, []) if room in joined), None)


def invite_from(token, sender, _since={}):
    """A room `sender` invited me to. Incremental syncs: Synapse caches a repeated initial sync."""
    since = _since.get(token)
    url = "/_matrix/client/v3/sync?timeout=1000" + (f"&since={urllib.parse.quote(since)}" if since else "")
    sync = api("GET", url, token)
    _since[token] = sync["next_batch"]
    seen = _since.setdefault((token, "invites"), {})
    seen.update(sync.get("rooms", {}).get("invite", {}))
    for room_id, info in seen.items():
        if any(e.get("sender") == sender and e.get("type") == "m.room.member" for e in info["invite_state"]["events"]):
            return room_id
    return None


def messages(token, room_id):
    return api("GET", f"/_matrix/client/v3/rooms/{urllib.parse.quote(room_id)}/messages?dir=b&limit=50", token)["chunk"]


def pm_message(token, room_id, words):
    return next((e for e in messages(token, room_id) if e["sender"] == PM and words in e["content"].get("body", "")), None)


def react(token, room_id, event_id, key):
    api("PUT", f"/_matrix/client/v3/rooms/{urllib.parse.quote(room_id)}/send/m.reaction/r{time.time_ns()}", token,
        {"m.relates_to": {"rel_type": "m.annotation", "event_id": event_id, "key": key}})


def welcome_and_code(me, name):
    """Steps 1-3: returns (the agent's chat, the install line's code)."""
    welcome = api("GET", "/_matrix/client/v3/directory/room/" + urllib.parse.quote(local_signup.WELCOME_ALIAS), me)["room_id"]
    check("the new account is in #welcome", welcome in api("GET", "/_matrix/client/v3/joined_rooms", me)["joined_rooms"])
    me_id = f"@{name}:{SERVER_NAME}"
    dm = until("@pm's private chat, already joined", lambda: joined_chat_with(me, me_id, PM))
    pointer = until("the pointer in Welcome", lambda: pm_message(me, welcome, "private message"))
    check("Welcome names me and points to my chat with @pm", f"@{name}:" in pointer["content"]["body"])
    title = api("GET", f"/_matrix/client/v3/rooms/{urllib.parse.quote(dm)}/state/m.room.name/", me).get("name")
    check("@pm's chat is named Product Manager", title == "Product Manager", title)
    offer = until("the welcome message", lambda: pm_message(me, dm, "Get your agent"))
    keys = until("the 🔌 button", lambda: {e["content"]["m.relates_to"]["key"] for e in messages(me, dm)
                                           if e["type"] == "m.reaction" and e["sender"] == PM} or None)
    check("@pm welcomes privately with 🔌 only (no server agent yet)", keys == {"🔌"} and "🏠" not in offer["content"]["body"],
          offer["content"]["body"][:70])
    api("PUT", f"/_matrix/client/v3/rooms/{urllib.parse.quote(dm)}/send/m.room.message/r{time.time_ns()}", me,
        {"msgtype": "m.text", "body": "server agent"})
    check("typing server agent says it comes later", True,
          until("server agent reply", lambda: pm_message(me, dm, "server agent, one that runs on this server for you, isn't here yet"))["content"]["body"][:60])
    react(me, dm, offer["event_id"], "🔌")
    line = until("the install line", lambda: pm_message(me, dm, "connect.sh"))["content"]["body"]
    found = re.search(r"curl -fsSL (\S+)/connect\.sh \| sh -s -- ([A-Z2-9]{5}-[A-Z2-9]{5})", line)
    check("🔌 sends one install line with a code", found and found.group(1) == CONSOLE)   # the code is never printed
    # Seen live: typed in @pm's chat without "@pm", this got no answer.
    api("PUT", f"/_matrix/client/v3/rooms/{urllib.parse.quote(dm)}/send/m.room.message/r{time.time_ns()}", me,
        {"msgtype": "m.text", "body": "resend me the code"})
    codes = lambda: {m.group(1) for e in messages(me, dm) if e["sender"] == PM
                     for m in [re.search(r"connect\.sh \| sh -s -- ([A-Z2-9]{5}-[A-Z2-9]{5})", e["content"].get("body", ""))] if m}
    fresh = until("a fresh code without mentioning @pm", lambda: len(codes()) >= 2 or None)
    check("\"resend me the code\" in @pm's chat sends a new one", fresh)
    found = re.search(r"curl -fsSL (\S+)/connect\.sh \| sh -s -- ([A-Z2-9]{5}-[A-Z2-9]{5})",
                      next(e["content"]["body"] for e in messages(me, dm) if e["sender"] == PM and "connect.sh" in e["content"].get("body", "")))
    # Only @pm's chat opens joined: the agent's stays an invite (-agent names aren't reserved at sign-up).
    agent_dm = until("the agent's chat invite", lambda: invite_from(me, f"@{name}-agent:{SERVER_NAME}"))
    api("POST", f"/_matrix/client/v3/join/{urllib.parse.quote(agent_dm)}", me, {})
    send(me, dm, f"make me a project room called e2e-{name}", PM)       # my chat with @pm: no "@pm" needed
    project = until("the project room's invite", lambda: invite_from(me, PM))
    api("POST", f"/_matrix/client/v3/join/{urllib.parse.quote(project)}", me, {})      # Accept, as in Element
    title = api("GET", f"/_matrix/client/v3/rooms/{urllib.parse.quote(project)}/state/m.room.name/", me).get("name")
    check("@pm makes a project room and invites me", title == f"e2e-{name}", title)
    members = api("GET", f"/_matrix/client/v3/rooms/{urllib.parse.quote(welcome)}/joined_members", me)["joined"]
    print(f"note: shared-secret accounts auto-joined to #welcome: {f'@{name}-agent:{SERVER_NAME}' in members}")
    return agent_dm, found.group(2)


def main():
    name = os.environ.get("E2E_NAME") or f"newbie{secrets.randbelow(9000) + 1000}"
    password = secrets.token_urlsafe(18)
    me = register(name, password, os.environ.get("E2E_TOKEN") or local_signup.new_token(1))
    check(f"signed up as {name} with an invite token", bool(me))
    agent_dm, code = welcome_and_code(me, name)
    home = Path(tempfile.mkdtemp(prefix="hagency-home-"))
    log = open(home / "connect.log", "w")
    backend = os.environ.get("E2E_AGENT", "codex")
    proc = subprocess.Popen([str(ROOT / ".venv/bin/python"), "connect.py", code, "--agent", backend, "--server", CONSOLE,
                             "--home", str(home), "--foreground"], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
    try:
        until("the agent online", lambda: "online" in (home / "connect.log").read_text(), timeout=90)
        creds = home / "credentials.json"
        check("the sign-in is saved for me only", oct(creds.stat().st_mode & 0o777) == "0o600")
        again = subprocess.run(["curl", "-s", "-XPOST", "-H", "Content-Type: application/json", "-d",
                                json.dumps({"code": code}), CONSOLE + "/connect"], capture_output=True, text=True).stdout
        check("the code works only once", "password" not in again)              # the answer is never printed
        agent = f"@{name}-agent:{SERVER_NAME}"
        ev = send(me, agent_dm, "who are you?", agent)
        body, _ = wait_reply(me, agent_dm, ev, agent)
        check("my own agent answers from my computer", bool(body.strip()), body[:120])
    finally:
        proc.terminate()
        proc.wait(timeout=30)
    print("ALL PASS")


if __name__ == "__main__":
    main()
