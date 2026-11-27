"""Solo review (docs/adr/0023-solo-review.md), the parts both sides share (R3): @pm (server/pm_solo.py) and an
agent adapter (adapters/agents/solo_github.py) read and write exactly these.

A room's switch lives in its own Matrix state (SOLO_EVENT), written only by @pm after its checks, and it
counts only if @pm wrote it (like the room's repo and mode: room_state.binding_in). Off by default.
"""
from nio import AsyncClient, RoomGetStateResponse

from common import room_state

SOLO_EVENT = "xyz.hagencylabs.solo_review"
TASK_KIND = "solo_ruleset"     # @pm → the room owner's agent: let repo admins merge past GitHub's review rule
# GitHub's "Repository admin" role (actor_id 5), allowed past a ruleset only when merging a pull request.
ADMIN_BYPASS = {"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "pull_request"}
NO_BYPASS = "GitHub's review rule doesn't let this account merge past it"   # in a failed merge's error
BYPASS_STEPS = ("**To let it merge:** on GitHub open **{repo} → Settings → Rules → Rulesets**, open the rule that "
                "requires a pull request review, and under **Bypass list** add **Repository admin** with "
                "**For pull requests only**. Or switch Solo review off and on again in the panel: the room owner's "
                "agent adds it for you when its GitHub account is the repo's admin.")


def content(on: bool, added: list[int] | None = None) -> dict:
    """added: the rulesets whose admin bypass Hagency itself added (only those are taken out again)."""
    return {"on": on is True, "bypass_added": sorted({int(i) for i in (added or [])})}


def state_in(events: list, pm: str) -> dict:
    """The room's SOLO_EVENT content, only if @pm wrote it (anything else: off)."""
    for ev in events:
        if ev.get("type") == SOLO_EVENT and ev.get("state_key") == "" and ev.get("sender") == pm:
            c = ev.get("content")
            if isinstance(c, dict):
                added = c.get("bypass_added")
                ids = [i for i in added if isinstance(i, int)] if isinstance(added, list) else []
                return content(c.get("on") is True, ids)
    return content(False)


async def read(client: AsyncClient, room_id: str) -> dict:
    """This room's solo review state now; unreadable reads as off (fail closed)."""
    r = await client.room_get_state(room_id)
    if not isinstance(r, RoomGetStateResponse):
        return content(False)
    return state_in(r.events, room_state.pm_id(client))


async def enabled(client: AsyncClient, room_id: str) -> bool:
    return (await read(client, room_id))["on"]


async def still_allowed(client: AsyncClient, room_id: str) -> bool:
    """An admin merge may still go ahead: the room is Autonomous and solo review is on, read now."""
    return await enabled(client, room_id) and await room_state.level(client, room_id) == "autonomous"


PASSED = ("SUCCESS", "NEUTRAL", "SKIPPED")
WAITING = ("QUEUED", "IN_PROGRESS", "PENDING", "WAITING", "REQUESTED", "EXPECTED")


def checks_state(rollup: object) -> str:
    """"passed", "pending" or "failed" for a PR's statusCheckRollup (none at all: passed). A merge past the review
    rule (--admin) must never skip a red or unfinished check."""
    state = "passed"
    for c in rollup if isinstance(rollup, list) else [None]:
        if not isinstance(c, dict):
            return "failed"
        if c.get("conclusion") in PASSED or c.get("state") == "SUCCESS":
            continue
        if c.get("status") in WAITING or c.get("state") in WAITING or (c.get("status") and not c.get("conclusion")
                                                                       and c.get("status") != "COMPLETED"):
            state = "pending"
        else:
            return "failed"
    return state


def is_admin_bypass(actor: object) -> bool:
    """An entry that already lets repository admins past (pull requests only, or always)."""
    return (isinstance(actor, dict) and actor.get("actor_type") == ADMIN_BYPASS["actor_type"]
            and actor.get("actor_id") == ADMIN_BYPASS["actor_id"] and actor.get("bypass_mode") in ("pull_request", "always"))
