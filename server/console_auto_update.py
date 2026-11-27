"""Auto-update in My agents (docs/adr/0024-automatic-agent-updates.md): the switch, each row's words, Update /
Retry update, and an update a task needs (pm_solo_rules). auto_update.py does the asking.

With Auto-update on, a row has no Update button: its version, "updates automatically" (Up to date) or
"Updating…", and after a failure Retry update with the reason. Code from before automatic updates (no
code_report.WAITS) keeps the Update button once. With it off: ADR 0020's Update button, as before.
"""
import time

from common import code_report
from common import registry
from server import auto_update
from server import console_update

SWITCH = ("on", "off")


def status(loop, a: dict) -> dict:
    """console_update.status, plus "auto_update" (the owner's switch), "code_auto" (this row updates itself)
    and "code_retry" (show Retry update), with the words for an automatic row."""
    if a.get("agent") == registry.REMOTE:
        auto_update.settle(loop, a["localpart"])
    shown = console_update.status(loop, a)
    owner = registry.owner_of(a["localpart"]) if a.get("agent") == registry.REMOTE else None
    on = bool(owner) and auto_update.is_on(owner)
    code = a.get(registry.CODE) or {}
    auto = on and shown["code"] in ("current", "behind") and bool(code.get(code_report.WAITS))
    out = {**shown, "auto_update": on, "code_auto": auto, "code_retry": False}
    if not on or shown["code"] != "behind":
        return out
    if not auto:
        return {**out, "code_says": shown["code_says"] or auto_update.ONCE}
    if shown["code_updating"]:
        return {**out, "code_says": auto_update.UPDATING}
    st = auto_update.state(loop).get(a["localpart"]) or {}
    reason = st.get("reason") if st.get("target") == console_update.SERVER["commit"] else None
    if reason == "busy":
        return {**out, "code_says": console_update.SAYS["busy"]}
    if reason:                                # edited / not_official: the Connect page, never a Retry that can't help
        return {**out, "code_says": console_update.SAYS.get(reason) or auto_update.WHY.get(reason, auto_update.NO_ANSWER),
                "code_retry": reason not in console_update.FALLBACK, "code_fallback": reason in console_update.FALLBACK}
    return {**out, "code_says": auto_update.SOON if loop.agent_online(a["localpart"]) is True else auto_update.OFFLINE}


async def request(loop, user: str, agent: str | None) -> str:
    """Update / Retry update: with Auto-update on, the agent still waits for no task running; the back-off
    starts over."""
    found = registry.find(user, agent)
    code = (found or {}).get(registry.CODE) or {}
    said = await console_update.request(loop, user, agent, auto=auto_update.is_on(user) and bool(code.get(code_report.WAITS)))
    if said == "ok":
        auto_update.state(loop).pop(found["localpart"], None)
    return said


async def toggle(loop, user: str, value: str) -> str:
    """The owner's Auto-update switch (on by default), for all their agents."""
    if value not in SWITCH:
        return "unknown action"
    if not registry.agents_of(user):
        return "You don't have an agent here yet."
    try:
        registry.update(user, None, **{auto_update.KEY: value == "on"})
    except OSError:                           # agents.json unwritable: said in the panel, nothing changed
        return auto_update.NOT_SAVED
    if value == "on":
        await auto_update.ask_next(loop, registry.person_id(user), time.time())
    return "ok"


async def update_for_task(loop, owner: str, agent: str) -> bool:
    """A task needs newer code than `agent` (a localpart) runs (code_report.runs_task): ask it to update to the
    server's now, as an automatic update. Only with its owner's Auto-update on and code that waits for its work
    (GPT review: a room action never overrides the owner's switch, nor updates under a running turn), and through
    auto_update's scheduler. True when it is asked or will be; else False, and the room is told how by hand."""
    found = registry.find(owner, agent) if owner and agent else None
    if found is None or not registry.owns(owner, agent) or not auto_update.is_on(owner):
        return False
    now = time.time()                         # through the scheduler: its back-off, cap and stagger (GPT re-check)
    await auto_update.ask_next(loop, registry.person_id(owner), now)
    return console_update.is_open(console_update.clicks(loop).get(agent), now) or auto_update.due(loop, found, now, None, any_time=True)
