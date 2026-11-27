"""The panel's "My agents" card, its data and its Add button (console.view and console.act use these).

The AI picker shows for a person's FIRST agent too (screen test: a newcomer could only get Codex or
Claude Code): while they have no agent, or none of theirs has ever connected, Add sets up their
first agent with the AI they chose (pm_first.py, the same path as 🔌). After that, "Add an agent"
adds a further one (pm_agents.py), as before.
"""
import time

from common import pm_kinds
from server import console_github
from server import console_auto_update
from server import ownership
from common import registry
from server import remote_people
from server.pm_first import BRING
from common.setup_accounts import SERVER_NAME


def my_agents(loop, room, room_id: str, user: str) -> list[dict]:
    """"My agents": each agent registered to exactly `user` (by full ID), the first first."""
    found = []
    for a in registry.agents_of(user):
        if registry.owns(user, a["localpart"]):
            aid = f"@{a['localpart']}:{SERVER_NAME}"
            found.append({"name": a["localpart"], "label": a["label"], "first": a["first"], "follow": a["follow"],
                          "display": a.get("display") or "", "runs": badge(a),   # pm_rename, pm_kinds
                          "remote": a["agent"] == registry.REMOTE, "online": loop.agent_online(a["localpart"]),
                          "stopped": a["stopped"], "hermes": a.get("agent_kind") == "hermes",   # pm_stop.py
                          "connected": loop.ever_connected(a["localpart"]),   # pm_presence.py: online at least once
                          "here": ownership.joined(room, aid), "invited": aid in getattr(room, "invited_users", {}),
                          **console_auto_update.status(loop, a),   # Up to date / Update available / Auto-update (ADRs 0020, 0024)
                          **console_github.status(loop, a)})   # Sign in to GitHub (ADR 0021)
    return found


def badge(agent: dict) -> str:
    """The AI it runs (pm_kinds.describe); for an agent on a person's computer whose AI isn't known yet
    ("the one on my computer", not reported since: kind_report.py), pm_kinds.NOT_REPORTED; else ""."""
    return pm_kinds.describe(agent) or (pm_kinds.NOT_REPORTED if agent.get("agent") == registry.REMOTE else "")


def can_add(user: str, mine: list) -> bool:
    """"Add an agent" shows: one of theirs has connected (as ➕ in @pm's chat: pm_agents.send_code), or they
    removed their last (a new name then), and there's room for more."""
    working = any(a["connected"] or not a["remote"] for a in mine)
    return (working or registry.first_removed(user)) and len(registry.agents_of(user)) < registry.per_person()


def first_pick(loop, user: str) -> bool:
    """The picker is for their first agent: none yet, or only never-connected ones."""
    if registry.first_removed(user):
        return False                          # removed their first: a further one, by name (can_add)
    return not any(a["agent"] != registry.REMOTE or loop.ever_connected(a["localpart"]) for a in registry.agents_of(user))


async def add_agent(loop, user: str, value: str) -> str:
    """Add an agent: a name, or the picker's JSON with the AI it runs (pm_kinds.from_panel); their first
    one while first_pick and no name was typed (pm_first.choose: its Connect page in our private chat)."""
    name, picked = pm_kinds.from_panel(value)
    if isinstance(picked, str):
        return picked
    dm = await loop.private_room(user)
    if not dm:
        return "I couldn't open a private chat with you"
    if not name and first_pick(loop, user):
        if not loop.may_have_agent(user):
            return f"Sorry, {remote_people.OFF}."
        if user in loop.__dict__.get("_provisioning", ()):   # GPT review: a second click promised a page too
            return "I'm already setting up an agent for you; one moment."
        if picked is None and value.lstrip().startswith("{"):
            picked = {}                       # the picker's "the one on my computer": clears an earlier choice
        await loop.choose(dm, user, BRING, picked)
        return "Setting up your agent: its Connect page is coming in our private chat."
    return await loop.start_add(dm, user, name or None, picked)


FRESH = "A fresh Connect page and code are on their way to our private chat (the code works once, within 30 minutes)."
BUSY = "I'm already setting up an agent for you; one moment, then try again."
TOO_MANY = "That's several codes this hour; the newest one still works. Try again later."
UPDATE = ("Product Manager is sending you its Connect page in your private chat with it. Run the command it "
          "shows on the computer where your agent runs, then make your card again.")   # GPT review: "our chat" was vague


async def fresh_code(loop, user: str, agent: str | None, update: bool = False) -> str:
    """The panel's "Get a fresh code": a new Connect page and one-time code for one of their agents that
    has never connected, as 🔁 does (pm_presence.py); with `update`, also for one that has (its code is old). Screen test: the panel told people to type
    "resend me the code", or "add another agent called pi" (which reads like adding a new one)."""
    found = registry.find(user, agent)
    if found is None or not registry.owns(user, found["localpart"]) or found["agent"] != registry.REMOTE:
        return "that isn't one of your agents on your own computer"
    connected = loop.ever_connected(found["localpart"])
    if connected and not update:
        return "it has connected before: run its connect line again on its computer"
    if user in loop.__dict__.get("_provisioning", ()):   # GPT review: a second click mid-setup sent nothing
        return BUSY
    from server.pm_welcome import CODES_PER_HOUR   # here: pm_welcome's imports reach console, which imports this
    recent = [t for t in loop.__dict__.setdefault("_codes_sent", {}).get(user, []) if time.time() - t < 3600]
    if len(recent) >= CODES_PER_HOUR:     # the same budget as typing "resend me the code" (pm_welcome.py)
        return TOO_MANY
    loop._codes_sent[user] = [*recent, time.time()]
    dm = await loop.private_room(user)
    if dm is None:
        return "I couldn't open a private chat with you; please try again"
    if connected:   # "Update my agent" (Your cards, old code): its Connect page again, as 🔁 does (pm_presence.py)
        try:
            await loop.send_code(user, None if found["first"] else found["localpart"])
        except RuntimeError:
            return "I couldn't open a private chat with just you; please try again"   # said here: the click is theirs
        return UPDATE
    if found["first"]:
        await loop.choose(dm, user, BRING)    # pm_first.bring_own: (re)uses their first agent, then its code
    else:   # add_agent finishes one whose setup failed halfway (GPT review: a bare code for it never redeemed),
        busy = loop.__dict__.setdefault("_provisioning", set())   # then sends its Connect page; a failure is said there
        busy.add(user)
        try:
            await loop.add_agent(dm, user, found["label"])
        finally:
            busy.discard(user)
    return FRESH
