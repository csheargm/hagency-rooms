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
from server import console_auto_update, ownership, routing
from server.pm_cards import local
from server.pm_paused import num

NEEDS_UPDATE = "{agent} needs an update first: its code is older than solo review needs. "   # live test 2026-11-27
THEN_ASKED = "I've asked it to update. Once My agents shows it Up to date, say **@pm retry #{n}**."
THEN_BY_HAND = "Update it in My agents (in your chat with Product Manager), then say **@pm retry #{n}**."
NOT_ON = "Solo review is off here now, so I sent nothing that needs it."
UNREADABLE = "⚠️ I couldn't read this room's settings just now, so I changed nothing. Please try again in a minute."


class SoloCode:
    def runs_solo(self, agent: str, step: str = code_report.SOLO_STEP) -> bool:
        """`agent` (a Matrix id) runs code that does this solo step (a solo check; SOLO_MERGE: a solo merge)."""
        owner = ownership.owner_id(agent)
        code = (registry.find(owner, local(agent)) or {}).get(registry.CODE) if owner else None
        return code_report.runs_task(code, step)

    async def saved_merge_waits(self, room_id: str, key: str, item: dict) -> bool:
        """A saved solo merge about to go out again (held while stopped, or the watchdog's resend): the same gate
        (GPT review: a task saved before an update, or a builder rolled back, skipped it). True: paused."""
        task = item.get("task") or {}
        return task.get("kind") == "merge" and any(task.get(f) is True for f in ("admin", "solo")) \
            and await self.merge_waits_for_update(room_id, key, item)

    async def merge_waits_for_update(self, room_id: str, key: str, item: dict) -> bool:
        """A solo merge goes only to code that re-reads the switch at the merge and refuses code-owner rules
        (code_report.SOLO_MERGE; GPT review). Else it pauses, the builder is asked to update, and "@pm retry #N"
        tries the merge again, PR kept. True: paused."""
        builder = item["builder"]
        if self.runs_solo(builder, code_report.SOLO_MERGE):
            return False
        item["resume"] = "approved"
        item.pop("held", None)                # a held merge never goes out after the retry: the merge is decided again
        await self.stop(room_id, key, item, f"{local(builder)} runs code too old for a solo merge", "**What you can do:**"
                        "\n\n- " + update_line(local(builder), await self.ask_update(builder), num(key)))
        return True

    async def ask_update(self, agent: str) -> bool:
        """A solo step `agent`'s code predates: it is asked to update, when its owner allows (True)."""
        return await console_auto_update.update_for_task(self, ownership.owner_id(agent), local(agent))

    async def record_on(self, rid: str) -> str | None:
        """Before an agent re-checks the switch: on, written out for the room's repo (older agent code reads only
        that). None only when it is on and recorded for that repo; else the line to say (GPT review: never a
        step sent while off). Off is never written as on."""
        try:
            state = await solo_review.read(self.client, rid, strict=True)
        except room_state.StateUnreadable:
            return UNREADABLE
        bound = state["bound"]
        if not solo_review.live(state, bound):
            return NOT_ON
        if state["repo"] == bound:
            return None                       # already explicit for this repo
        await self.drop_other_repo(rid, state, bound)   # another repo's GitHub part never carries over
        err, before = await self.write_solo(rid, None, repo=bound, for_repo=bound)
        return err or (None if before["on"] else NOT_ON)   # switched off meanwhile: kept off, nothing sent

    async def review_again(self, room_id: str, key: str, item: dict, why: str) -> None:
        """A fresh review by the item's reviewer (after fixes, or the PR moved); a solo one only to code that does
        solo steps (GPT review: else it pauses, its update is asked, and "@pm retry #N" goes back to the check)."""
        reviewer = item["reviewer"]
        room = getattr(self.client, "rooms", {}).get(room_id) if item.get("solo") is True else None
        if room is not None and routing.checkers_for(self.agents_in(room), item["builder"], ownership.owner_id):
            return await self.recheck_with(room, key, item)   # GPT review: another person's agent is here now
        if item.get("solo") is True and not self.runs_solo(reviewer):
            item["no_checker"] = True
            await self.stop(room_id, key, item, f"{local(reviewer)} runs code from before solo review",
                            "**What you can do:**\n\n- " + update_line(local(reviewer), await self.ask_update(reviewer),
                                                                        num(key)))
            return
        item["stage"] = "reviewing"
        await self.dispatch(room_id, item, reviewer, f"{reviewer}: review PR #{item['pr']} again ({why}) against "
                            f"#{num(key)}", routing.check_task(item, num(key)))


    async def recheck_with(self, room, key: str, item: dict) -> None:
        """A solo item's next check goes through checker selection again (another person's agent first). If the
        step can't go out (Matrix refused), it pauses for "@pm retry #N" (GPT review: never left at "reviewing"
        with the fix task for the watchdog to resend), as check_again does."""
        item.update(stage="reviewing", task_ids=[])
        try:
            await self.ask_checker(room, item, key)
        except RuntimeError:                  # loop.SendError / GitHub: said by the caller's log
            item.update(stage="stopped", no_checker=True)
            raise
        finally:
            self.save()                       # GPT review: the pause (or the sent step) survives a restart


def update_line(agent: str, asked: bool, n: int) -> str:
    """The room's words for it (agent: a localpart), and "@pm retry #n" once it is up to date."""
    return NEEDS_UPDATE.format(agent=agent) + (THEN_ASKED if asked else THEN_BY_HAND).format(n=n)
