"""Bring your own agent from your own computer: the server side.

provision() makes the person's agent account here (identity.py names it from their FULL Matrix ID,
so a person from another server gets one too; generated password in the git-ignored .env, like
onboard.py), records it in agents.json as "remote" (the supervisor never runs it here) and has the
agent open a private chat with its person. remote_people.py limits people from other servers. A one-time code (pairing.py) then lets connect.py on the person's
computer fetch the agent's sign-in once, from POST /connect on the console server; GET /connect.sh
serves the installer. The password is never logged; only the code's hash is stored.
"""
import asyncio
import os
import secrets
import socket
import threading
import time
import urllib.parse

from aiohttp import web

from common import agent_kinds
from server import connect_status
from server import console
from common import identity
from common import local_signup
from server import pairing
from common import registry
from server import remote_people
from common import setup_accounts
from common.bridge_config import ROOT, SERVER_NAME
from common.setup_accounts import AccountError, api, load_env, mark_direct, try_login

PUBLIC_HOMESERVER = os.environ.get("HAGENCY_PUBLIC_HOMESERVER", "http://localhost:8648")   # what people's computers reach
CODES = pairing.Codes(ROOT / "data" / "pairing.json")
LIMIT = pairing.Limiter()
NO_STORE = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
INSTALLER = ROOT / "connect.sh"
CONSOLE_MARK = "__HAGENCY_CONSOLE__"
# Behind the public server's Caddy every request comes from Caddy; it sets X-Forwarded-For to the
# real client (Caddy trusts no incoming one). HAGENCY_TRUST_PROXY names the proxy (its Docker host
# name): the header counts only on a connection from that address, never from another container.
TRUST_PROXY = os.environ.get("HAGENCY_TRUST_PROXY", "")


def proxy_addresses() -> set[str]:
    try:
        return {info[4][0] for info in socket.getaddrinfo(TRUST_PROXY, None)} if TRUST_PROXY else set()
    except OSError:
        return set()                          # can't resolve the proxy: trust nobody's header


def client_of(request) -> str:
    """Who is asking, for the wrong-code limiter: the proxy's X-Forwarded-For when it came from the proxy."""
    peer = request.remote or "?"
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded and peer in proxy_addresses():
        return forwarded.split(",")[-1].strip()[:64] or peer
    return peer


def install_line(code: str, kind: str | None = None) -> str:
    """The line to paste on a Mac or Linux; with the agent's kind (agent_kinds.KINDS only), --agent KIND,
    as the Connect page's own line (static/connect.js commandFor)."""
    named = f" --agent {kind}" if kind in agent_kinds.KINDS else ""
    return f"curl -fsSL {console.public_url()}/connect.sh | sh -s -- {code}{named}"


def open_dm(agent: str, password: str, person: str) -> str:
    """The agent opens a private chat with its person (marked direct), so they can talk right away."""
    token = try_login(agent, password)
    if not token:
        raise AccountError(f"couldn't sign in as @{agent}")
    try:
        agent_id = f"@{agent}:{SERVER_NAME}"
        r = api("POST", "/_matrix/client/v3/createRoom", {"is_direct": True, "preset": "trusted_private_chat",
                                                         "invite": [person]}, token=token)
        if "room_id" not in r:
            raise AccountError(f"couldn't open a chat for @{agent}: {r.get('errcode')}")
        mark_direct(token, agent_id, person, r["room_id"])
        return r["room_id"]
    finally:
        api("POST", "/_matrix/client/v3/logout", {}, token=token)


WRITING = threading.Lock()                    # .env and agents.json are read-modify-write: one set-up at a time


def provision(person: str, picked: dict | None = None) -> str:
    """Create (or reuse) the agent of `person` (a full Matrix ID) as a remote agent; returns its
    localpart. picked: the AI it runs (pm_kinds.choice; pm_first.py passes one only while it has
    never connected), recorded as a further agent's is. Blocking I/O."""
    with WRITING:
        return _provision(registry.person_id(person), picked)


def _provision(person: str, picked: dict | None = None) -> str:
    local = registry.local_name(person)
    if local is not None and not registry.NAME_RE.match(local):
        raise AccountError("for now an agent needs an account name of lowercase letters and digits")
    people = registry.load()
    info = people.get(person, {})
    if registry.first_removed(person, people):   # its name is closed for good (pm_welcome.choose asks for a new one)
        raise AccountError("you removed your first agent; say \"add another agent\" to get one with a new name")
    if person in people and info.get("agent") != registry.REMOTE:    # no "agent": a server-run default one
        raise AccountError("you already have an agent here, run by this server")
    agent = registry.agent_of(person)
    if agent != identity.agent_localpart(person, SERVER_NAME) or registry.claimants(agent) not in ([], [person]):
        raise AccountError("your agent's account name is taken here; ask this server's operator")
    if local is None:
        remote_people.check_new(registry.from_elsewhere(people), person)   # other servers' people: allowed, not too many
        if person not in people:              # counted and reserved BEFORE the account exists: a failed
            registry.update(person, None, agent=registry.REMOTE, created=remote_people.now())   # chat can't dodge the cap
    password, _ = setup_accounts.ensure_user(load_env(), agent, setup_accounts.shared_secret(), verify=True)
    registry.update(person, None, ready=True)  # made or signed in with our password: ours (disconnect checks)
    if picked is not None:                    # every field, so an earlier choice's model or URL can't linger ({}: none)
        registry.update(person, None, **{k: picked.get(k, "") for k in agent_kinds.OFFER_KEYS})
    dm = info.get("dm") or open_dm(agent, password, person)
    registry.update(person, None, agent=registry.REMOTE, dm=dm)   # a fresh read under its lock: keeps the rest
    return agent


def provision_more(person: str, label: str, picked: dict | None = None) -> str:
    """A further agent of `person`, called `label` (registry.clean_label), as a remote agent; returns
    its localpart. Counted against their cap (and, from another server, the hourly cap) BEFORE its
    account exists. Raises AccountError with a sentence for them. Blocking I/O."""
    with WRITING:
        person = registry.person_id(person)
        if not registry.may_add_more(person):   # none yet (or their first was removed: then new names only)
            raise AccountError("you don't have an agent here yet: get your first one with 🔌 Bring your own")
        found = registry.find(person, label)
        if found is None or found["first"]:   # new (an unfinished one with this name is finished instead)
            if registry.local_name(person) is None:
                remote_people.check_new(registry.from_elsewhere(registry.load()), person, further=True)
            try:
                found = registry.add_agent(person, label, picked=picked)   # pm_kinds.py: the AI it runs (agent_kinds' fields)
            except ValueError as e:
                raise AccountError(str(e)) from e
        agent = found["localpart"]
        if found["agent"] != registry.REMOTE or registry.claimants(agent) != [person]:
            raise AccountError("that agent's account name is taken here; choose another name")
        if picked and not found["ready"]:     # finishing an unfinished one: the AI chosen now (GPT review);
            registry.update(person, agent, **{k: picked.get(k, "") for k in agent_kinds.OFFER_KEYS})   # a finished one keeps its own
        password, _ = setup_accounts.ensure_user(load_env(), agent, setup_accounts.shared_secret(), verify=True)
        registry.update(person, agent, ready=True)   # made or signed in with our password: ours (disconnect checks)
        if not found.get("dm"):
            registry.update(person, agent, dm=open_dm(agent, password, person))
        return agent


def disconnect(person: str, agent: str | None = None) -> None:
    """A new password for this remote agent of `person` (default: their first; saved first, put back
    if Synapse refuses or can't be asked) and every session signed out: the copy on their computer
    stops working until a new code. The caller revokes its codes first. Blocking I/O."""
    with WRITING:
        person, env = registry.person_id(person), load_env()
        agent = agent or registry.agent_of(person)
        found = registry.find(person, agent)
        if found is None or not registry.owns(person, agent):
            raise AccountError(f"@{agent} isn't your agent")
        key = setup_accounts.password_key(agent)
        old, new = env.get(key), secrets.token_urlsafe(18)
        if old is None or not (found["ready"] or _signs_in(agent, old)):   # never reset an account we
            raise AccountError(f"@{agent} was never set up here")   # don't hold (GPT review 24)
        registry.update(person, None if found["first"] else agent, disconnected=time.time())   # older codes: dead
        setup_accounts.save_env({**env, key: new})
        try:
            r = local_signup.admin_call("POST", f"/_synapse/admin/v1/reset_password/{urllib.parse.quote(f'@{agent}:{SERVER_NAME}')}",
                                        {"new_password": new, "logout_devices": True})
        except (SystemExit, OSError, ValueError) as e:   # admin-bot can't sign in, Synapse away: as a refusal
            r = {"_status": 0, "errcode": str(e)[:80]}
        if "_status" in r:
            if old is not None:                 # only our line, on a fresh read: other writers' lines stay
                setup_accounts.save_env({**load_env(), key: old})
            raise AccountError(f"couldn't give @{agent} a new password: {r.get('errcode')}")


def _signs_in(agent: str, password: str) -> bool:
    """An older record (no "ready"): our saved password really signs in as `agent`."""
    token = try_login(agent, password)
    if token:
        api("POST", "/_matrix/client/v3/logout", {}, token=token)
    return bool(token)


def locked_credentials(person: str, agent: str | None = None, issued: float | None = None) -> dict | None:
    """credentials_for, never in the middle of a set-up or a disconnect (.env read-modify-write)."""
    with WRITING:
        return credentials_for(person, agent, issued)


def credentials_for(person: str, agent: str | None = None, issued: float | None = None) -> dict | None:
    """What connect.py needs to run this agent of `person` (None: their first), or None if it isn't
    a remote agent of theirs any more, or the code (`issued` then) predates its disconnect: a code
    taken just before a disconnect never picks up the new password (GPT review 24)."""
    person = registry.person_id(person)       # codes issued before full IDs hold a bare name
    if agent is None and registry.first_removed(person):
        return None                           # a code for their first agent, which was removed (GPT re-check)
    found = registry.find(person, agent)
    if found is None or found["agent"] != registry.REMOTE or (agent and found["localpart"] != agent):
        return None
    cut = found.get("disconnected")
    if issued is not None and isinstance(cut, (int, float)) and issued <= cut:
        return None
    if not registry.local_name(person) and not remote_people.allowed():
        return None                           # turned off: no new sign-ins for other servers' people
    label = None if found["first"] else found["label"]
    agent = found["localpart"]
    if agent != identity.agent_localpart(person, SERVER_NAME, label) or not registry.owns(person, agent):
        return None                           # an odd record: connect.py checks the derived name, and only theirs
    password = load_env().get(setup_accounts.password_key(agent))
    if not password:
        return None
    owner = registry.local_name(person) or person   # a bare name here: what an older connect.py accepts
    return {"homeserver": PUBLIC_HOMESERVER, "user_id": f"@{agent}:{SERVER_NAME}", "password": password, "owner": owner,
            **({"label": label} if label else {}),
            **{k: found[k] for k in agent_kinds.OFFER_KEYS if found.get(k)}}   # the AI it runs (none: connect.py picks)


def routes() -> list:
    async def connect(request):
        client = client_of(request)
        if request.content_type != "application/json" or "Origin" in request.headers:
            return web.json_response({"error": "use connect.py"}, status=415, headers=NO_STORE)   # never a browser's
        if LIMIT.blocked(client):
            return web.json_response({"error": "too many wrong codes; wait 10 minutes"}, status=429, headers=NO_STORE)
        try:
            body = await request.json()
        except ValueError:                    # not JSON: the same answer as a wrong code
            body = {}
        person, agent, issued = CODES.redeem_agent(body.get("code") if isinstance(body, dict) else None)
        if person:                            # the code is gone now: the page keeps "waiting" until the answer
            connect_status.redeeming(body["code"])   # (no await in between; GPT review, batch 12-14)
        try:
            creds = await asyncio.to_thread(locked_credentials, person, agent, issued) if person else None
        finally:
            if person:
                connect_status.redeemed(body["code"])
        if not creds:
            LIMIT.failed(client)
            return web.json_response({"error": "that code is wrong, used or expired: ask @pm for a new one"},
                                     status=400, headers=NO_STORE)
        connect_status.mark_used(body["code"])   # the Connect page may now say "connected" (only that)
        return web.json_response(creds, headers=NO_STORE)

    async def installer(request):
        text = INSTALLER.read_text().replace(CONSOLE_MARK, console.public_url())
        return web.Response(text=text, content_type="text/plain", headers=NO_STORE)
    return [web.post("/connect", connect), web.get("/connect.sh", installer)]
