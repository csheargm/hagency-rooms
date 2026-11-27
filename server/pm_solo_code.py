"""Solo review with agents on older code (docs/adr/0023-solo-review.md, review of PR #74).

Code from before solo review was on by default reads only an explicit @pm event, on and for the room's very
repo (its solo_review.live), so a room that never set it is "off" there: @pm said on, the agent refused "solo
review is off in this room" and the item paused for a reason the panel contradicted. So before a step that the
agent re-checks (an admin merge, the ruleset task) @pm writes the switch out for the room's repo (record_on),
and an agent whose code predates solo review itself (code_report.TASK_SINCE[SOLO_STEP]) never gets a solo
step: the room is told it needs an update first, and it is asked to update (console_auto_update).
Mixed into loop.Loop through pm_solo_rules.SoloRules (client, write_solo, drop_other_repo).
"""
from common import code_report, registry, room_state, solo_review
from server import console_auto_update, ownership
from server.pm_cards import local

NEEDS_UPDATE = "{agent} needs an update first: it runs code from before solo review. "   # live test 2026-11-27
THEN_ASKED = "I've asked it to update. Once My agents shows it Up to date, say **@pm retry #{n}**."
THEN_BY_HAND = "Update it in My agents (in your chat with Product Manager), then say **@pm retry #{n}**."
UNREADABLE = "⚠️ I couldn't read this room's settings just now, so I changed nothing. Please try again in a minute."


class SoloCode:
    def runs_solo(self, agent: str) -> bool:
        """`agent` (a Matrix id) runs code that does solo steps: a solo check, an admin merge."""
        owner = ownership.owner_id(agent)
        code = (registry.find(owner, local(agent)) or {}).get(registry.CODE) if owner else None
        return code_report.runs_task(code, code_report.SOLO_STEP)

    async def ask_update(self, agent: str) -> bool:
        """A solo step `agent`'s code predates: it is asked to update, when its owner allows (True)."""
        return await console_auto_update.update_for_task(self, ownership.owner_id(agent), local(agent))

    async def record_on(self, rid: str) -> str | None:
        """Before an agent re-checks the switch: on, written out for the room's repo (older agent code reads only
        that). None when it is there (or was written); else the line to say. Off is never written as on."""
        try:
            state = await solo_review.read(self.client, rid, strict=True)
        except room_state.StateUnreadable:
            return UNREADABLE
        bound = state["bound"]
        if not solo_review.live(state, bound) or state["repo"] == bound:
            return None                       # off: the agent refuses anyway; for this repo: already explicit
        await self.drop_other_repo(rid, state, bound)   # another repo's GitHub part never carries over
        return (await self.write_solo(rid, None, repo=bound, for_repo=bound))[0]


def update_line(agent: str, asked: bool, n: int) -> str:
    """The room's words for it (agent: a localpart), and "@pm retry #n" once it is up to date."""
    return NEEDS_UPDATE.format(agent=agent) + (THEN_ASKED if asked else THEN_BY_HAND).format(n=n)
