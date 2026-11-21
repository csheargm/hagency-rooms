"""The panel's "My agents" card, its data and its Add button (console.view and console.act use these).

The AI picker shows for a person's FIRST agent too (screen test: a newcomer could only get Codex or
Claude Code): while they have no agent, or none of theirs has ever connected, Add sets up their
first agent with the AI they chose (pm_first.py, the same path as 🔌). After that, "Add an agent"
adds a further one (pm_agents.py), as before.
"""
from common import pm_kinds
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
                          "display": a.get("display") or "", "runs": pm_kinds.describe(a),   # pm_rename, pm_kinds
                          "remote": a["agent"] == registry.REMOTE, "online": loop.agent_online(a["localpart"]),
                          "stopped": a["stopped"], "hermes": a.get("agent_kind") == "hermes",   # pm_stop.py
                          "connected": loop.ever_connected(a["localpart"]),   # pm_presence.py: online at least once
                          "here": ownership.joined(room, aid), "invited": aid in getattr(room, "invited_users", {})})
    return found


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
