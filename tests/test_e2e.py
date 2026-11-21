"""End-to-end test of the real user flow: humans @mention agents in #bridge-test over the
Matrix client API and we check that the right agent replies (and only that agent).

Needs: stack up (./start.sh) and both agent adapters running. Run: .venv/bin/python -m tests.test_e2e
"""
import json
import time
import urllib.parse
import urllib.request

from common.setup_accounts import HS, SERVER_NAME, TEST_ROOM_ALIAS, ensure_room, load_env, login

REPLY_TIMEOUT_S = 300


def test_room() -> str:
    """The tests' own room, so their messages and fake links never show up in the Lounge."""
    room_id = ensure_room(load_env(), TEST_ROOM_ALIAS, "Bridge tests")
    time.sleep(3)                          # running agent adapters accept the agents' invites
    return room_id


def api(method, path, token, body=None):
    req = urllib.request.Request(HS + path, method=method, headers={"Authorization": f"Bearer {token}",
                                 "Content-Type": "application/json"},
                                 data=json.dumps(body).encode() if body is not None else None)
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


def send(token, room_id, body, mention):
    txn = f"t{time.time_ns()}"
    content = {"msgtype": "m.text", "body": body, "m.mentions": {"user_ids": [mention]}}
    return api("PUT", f"/_matrix/client/v3/rooms/{room_id}/send/m.room.message/{txn}", token, content)["event_id"]


def answers_after(msgs, since_event, from_user):
    """from_user's plain messages after since_event, oldest first (answers come without a quote
    unless others spoke in between; threads and edits aren't answers)."""
    out = []
    for ev in msgs:                       # newest first
        if ev.get("event_id") == since_event:
            return out[::-1]
        rel = ev.get("content", {}).get("m.relates_to", {})
        if ev.get("sender") == from_user and rel.get("rel_type") not in ("m.replace", "m.thread"):
            out.append(ev)
    return []                             # since_event not in view: nothing known to follow it


def wait_reply(token, room_id, since_event, from_user):
    """Poll room messages until from_user replies to since_event; also report typing seen."""
    deadline, saw_typing = time.time() + REPLY_TIMEOUT_S, False
    while time.time() < deadline:
        msgs = api("GET", f"/_matrix/client/v3/rooms/{room_id}/messages?dir=b&limit=30", token)["chunk"]
        found = answers_after(msgs, since_event, from_user)
        if found:
            return found[0]["content"]["body"], saw_typing
        sync = api("GET", "/_matrix/client/v3/sync?timeout=0&filter=" + urllib.parse.quote(
            json.dumps({"room": {"timeline": {"limit": 0}, "rooms": [room_id]}})), token)
        eph = sync.get("rooms", {}).get("join", {}).get(room_id, {}).get("ephemeral", {}).get("events", [])
        saw_typing |= any(from_user in e.get("content", {}).get("user_ids", []) for e in eph if e["type"] == "m.typing")
        time.sleep(1)
    raise AssertionError(f"no reply from {from_user} within {REPLY_TIMEOUT_S}s")


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" — {detail}" if detail else ""))
    if not cond:
        raise SystemExit(1)


def main():
    env = load_env()
    anna, betty = login("anna", env["PW_ANNA"]), login("betty", env["PW_BETTY"])
    room_id = test_room()
    anna_agent, betty_agent = f"@anna-agent:{SERVER_NAME}", f"@betty-agent:{SERVER_NAME}"

    t0 = time.time()
    ev = send(anna, room_id, "anna-agent: my favourite colour is teal. Reply with just the word OK.", anna_agent)
    body, typing = wait_reply(anna, room_id, ev, anna_agent)
    check("anna-agent answers anna's @mention", bool(body.strip()), f"{time.time()-t0:.1f}s: {body[:80]!r}")
    check("typing indicator shown while working", typing)

    t0 = time.time()
    ev = send(betty, room_id, "betty-agent: what is 17*3? Answer with just the number.", betty_agent)
    body, _ = wait_reply(betty, room_id, ev, betty_agent)
    check("betty-agent answers betty's @mention", "51" in body, f"{time.time()-t0:.1f}s: {body[:80]!r}")

    t0 = time.time()
    ev = send(anna, room_id, "anna-agent: what is my favourite colour? One word.", anna_agent)
    body, _ = wait_reply(anna, room_id, ev, anna_agent)
    check("anna-agent keeps per-room context (session resume)", "teal" in body.lower(),
          f"{time.time()-t0:.1f}s: {body[:80]!r}")

    msgs = api("GET", f"/_matrix/client/v3/rooms/{room_id}/messages?dir=b&limit=30", anna)["chunk"]
    stray = answers_after(msgs, ev, betty_agent)
    check("betty-agent stays quiet when not mentioned", not stray)
    check_owner_only(betty, room_id, anna_agent)
    check_encrypted_room_warning(anna, anna_agent)
    print("ALL PASS")


def check_owner_only(betty, room_id, anna_agent):
    """By default anna's agent (anna's file access) won't work for betty, and says so once."""
    ev = send(betty, room_id, "anna-agent: what is 2+2?", anna_agent)
    time.sleep(15)  # a Codex turn takes ~5 s; give it 3x before concluding it stayed quiet
    msgs = api("GET", f"/_matrix/client/v3/rooms/{room_id}/messages?dir=b&limit=10", betty)["chunk"]
    replied = [m["content"]["body"] for m in answers_after(msgs, ev, anna_agent)]
    check("anna-agent declines betty by default (owner-only), saying why",
          replied == ["I only take requests from anna."], repr(replied))


def check_encrypted_room_warning(token, agent):
    """The agent adapter has no E2EE: in an encrypted room it must say so instead of going silent."""
    room_id = api("POST", "/_matrix/client/v3/createRoom", token, {
        "name": "E2EE probe", "preset": "private_chat", "invite": [agent],
        "initial_state": [{"type": "m.room.encryption", "state_key": "",
                           "content": {"algorithm": "m.megolm.v1.aes-sha2"}}]})["room_id"]
    time.sleep(3)  # let the agent adapter auto-join
    fake = {"algorithm": "m.megolm.v1.aes-sha2", "ciphertext": "AwgA", "device_id": "PROBE",
            "sender_key": "probe", "session_id": "probe"}
    api("PUT", f"/_matrix/client/v3/rooms/{room_id}/send/m.room.encrypted/t{time.time_ns()}", token, fake)
    warned = False
    for _ in range(15):
        time.sleep(1)
        msgs = api("GET", f"/_matrix/client/v3/rooms/{room_id}/messages?dir=b&limit=10", token)["chunk"]
        warned = any(m["sender"] == agent and "encrypted" in m["content"].get("body", "") for m in msgs)
        if warned:
            break
    api("POST", f"/_matrix/client/v3/rooms/{room_id}/leave", token, {})
    check("agent warns in an encrypted room (no E2EE support)", warned)


if __name__ == "__main__":
    main()
