"""Solo review (docs/adr/0023-solo-review.md): who checks a build when a room's owner tests alone with several
of their OWN agents ("no need for a second person at this testing stage").

On by default (owner decision; an explicit off stays off): another person's agent is still always preferred
as the checker (review, verify: routing.checkers_for); only when none is here, another agent of the builder's
owner checks it (routing.own_checkers; never the builder itself). Switching it off is open to anyone, at once,
like "stop"; switching it on again is a moderator's, with a 👍 (chat) or a click (the panel).

GitHub never lets an account approve its own PR, and in the live test every agent used the owner's one account.
So a solo checker posts its verdict as a comment (adapters/agents/tasks.submit_review), and where the repo's
ruleset requires an approval the merge goes past it as the repo's admin (solo_github.admin_merge). The repo's
ruleset is never changed just because the default is on: @pm asks the room's owner/moderator once with a 👍
first (pm_solo_rules.offer_bypass). Mixed into loop.Loop (client, say, dispatch, stop, pick_reviewer, lock, ...).
"""
import asyncio

from nio import MatrixRoom

from common import github, intents, room_state, solo_review
from common.redact import room_safe
from server import ownership, routing
from server.pm_cards import local
from server.pm_chat import ONLY_MODERATORS
from server.pm_implicit import TO_CONFIRM
from server.pm_paused import no_reviewer_steps, num
from server.pm_settings import NEED_POWER
from server.pm_solo_code import UNREADABLE, update_line
from server.pm_solo_rules import KEEP, REPO_CHANGED, SoloRules

SOLO_TASK = solo_review.TASK_KIND
QUESTION = ("Turn on **solo review**? When no other person's agent is here, another of the builder's owner's own "
            "agents (never the builder itself) reviews and verifies the work; on GitHub its verdict is a comment. "
            "I'll ask separately before anything changes GitHub's review rule." + TO_CONFIRM)
ON = "Solo review is on: when no other person's agent is here, another of your own agents checks the work."
OFF = "Solo review is off: only another person's agent reviews and verifies work here."
ALREADY_ON = "Solo review is already on here."
HINT = ("**Testing alone?** Say **@pm allow solo review** (or switch on Solo review in the Hagency panel) to let your "
        "other agent check it, then **@pm retry #{n}**.")
NO_OTHER = ("Solo review is on, but none of the builder's owner's other agents is here (an agent never checks its own "
            "work). Bring a second agent of yours into the room, then say **@pm retry #{n}**.")
OFFLINE = ("Solo review is on, but your other agent {names} is offline. Wake its computer (or start it in My agents), "
           "then say **@pm retry #{n}**.")
AWAY = ("Another person's agent, {names}, is here but offline, so it checks this rather than your own agent. Ask "
        "its owner to wake it, then say **@pm retry #{n}**.")
SOLO_READY = ("PR #{pr} ready — a person merges. GitHub's review rule can't count a review by your own agent, so as "
              "the repo's admin use **Merge without waiting for requirements** on GitHub.")
SOLO_READY_NO_BYPASS = ("PR #{pr} ready — a person merges. GitHub's review rule can't count a review by your own agent, "
                        "and it doesn't let repo admins past it yet. ")
NO_REPO_YET = "Pick this room's GitHub repo first: solo review is switched on for that repo's rules."
REPO_MOVED = "This room's repo changed since I asked, so solo review stays off. Ask me again for the new repo."
UNANSWERED = ("{agent} hasn't said yet whether it let repo admins past GitHub's review rule on {repo}. When it does, "
              "I'll ask it to take that out again. If no answer comes: ")
MERGE_FAILED = ("**What you can do:**\n\n- If GitHub's rules stopped it: {steps} Then say **@pm retry #{n}** and I'll "
                "try the merge again.\n- Or: {link}")


class Solo(SoloRules):
    async def solo_on(self, room_id: str) -> bool:
        return await solo_review.enabled(self.client, room_id)

    async def solo(self, room: MatrixRoom, sender: str, intent: str, gen: int | None = None, named: bool = True) -> None:
        """"@pm allow solo review" (asked with a 👍), "@pm solo review off" (at once, anyone)."""
        rid = room.room_id
        gen = self.generation.get(rid, 0) if gen is None else gen
        if intent == intents.SOLO_OFF:
            await self.solo_off(room, sender)
        elif not await room_state.is_moderator(self.client, room, sender):
            if named:
                await self.say(rid, ONLY_MODERATORS)
        elif not await room_state.repo_of(self.client, rid):
            if named:
                await self.say(rid, NO_REPO_YET)   # review: switched on with no repo, its bypass was never asked
        elif await self.solo_on(rid):
            if named:
                await self.say(rid, ALREADY_ON)
                await self.offer_bypass(rid, sender, again=True)   # pm_solo_rules.py: unless already allowed
        else:
            await self.ask_once(rid, sender, gen, QUESTION, {"kind": "solo", "repo": await room_state.repo_of(
                self.client, rid)}, named)   # pm_chat.confirm: solo_confirmed, for the repo asked about only

    async def solo_from_panel(self, room: MatrixRoom, user: str, value: str) -> str:
        """The panel's toggle: a click is the moderator's confirmation; off is anyone's."""
        if value == "off":
            return await self.solo_off(room, user) or "ok"
        if value != "on":
            return "unknown value"
        if not await room_state.is_moderator(self.client, room, user):
            return "only the room owner or a maintainer can turn solo review on"
        return await self.solo_confirmed(room, user) or "ok"

    async def write_solo(self, rid: str, on: bool | None, change=None, repo=KEEP, ok: bool | None = None,
                         asked=KEEP, for_repo: str | None = None, stale: str | None = None
                         ) -> tuple[str | None, dict | None]:
        """Read and write the switch under one lock: (an error line or None, the state before). on/ok None and
        repo/asked KEEP keep them; an on/off given is a person's choice (no longer the default); change: recorded
        rulesets → new ones; for_repo: written only while that is the room's repo and the record's (GPT review:
        never another repo's GitHub part); stale: only while the record is still that repo's and it isn't the
        room's repo (a drop never erases the current repo's record). An unreadable room is never written (review: the fail-closed "off,
        nothing recorded" read would erase the record)."""
        async with self.__dict__.setdefault("_solo_lock", asyncio.Lock()):
            try:
                before = await solo_review.read(self.client, rid, strict=True)
            except room_state.StateUnreadable:
                return UNREADABLE, None
            if (for_repo and (before["bound"] != for_repo or before["repo"] not in (None, for_repo))
                    or stale and (before["repo"] != stale or before["bound"] == stale)):
                return REPO_CHANGED, before
            added = set(before["bypass_added"])
            resp = await self.client.room_put_state(rid, solo_review.SOLO_EVENT, solo_review.content(
                before["on"] if on is None else on, sorted(change(added) if change else added),
                before["repo"] if repo is KEEP else repo, before["ok"] if ok is None else ok,
                before["default"] and on is None, before["asked"] if asked is KEEP else asked))
        if hasattr(resp, "event_id"):
            return None, before
        if getattr(resp, "status_code", "") == "M_FORBIDDEN":
            return NEED_POWER, before
        return f"⚠️ I couldn't save this room's settings: {room_safe(resp)}", before

    async def solo_confirmed(self, room: MatrixRoom, sender: str, asked: object = KEEP, bypass: bool = False
                             ) -> str | None:
        """Switch it on (after the 👍 or the panel's click; the moderator was checked just now) in a room with a
        repo; asked: the repo the 👍 question was about (review: a rebind in between needs a new 👍). bypass: the
        👍 answered the bypass question instead (pm_solo_rules.offer_bypass)."""
        if bypass:
            return await self.bypass_confirmed(room, sender, asked)
        rid = room.room_id
        repo = await room_state.repo_of(self.client, rid)
        err = NO_REPO_YET if not repo else REPO_MOVED if asked is not KEEP and asked != repo else \
            (await self.write_solo(rid, True))[0] or await self.record_on(rid)   # for its repo (pm_solo_code)
        if err:
            await self.say(rid, err)
            return err
        await self.say(rid, ON + self.waiting_for_checker(rid))
        await self.offer_bypass(rid, sender, again=True)   # its own 👍 before any ruleset change (owner decision)
        return None

    async def solo_off(self, room: MatrixRoom, sender: str) -> str | None:
        rid = room.room_id
        if (self.pending.get(rid) or {}).get("kind") == "solo":
            self.pending.pop(rid, None)       # the question to switch it on is moot now
        err, before = await self.write_solo(rid, False)   # "ok" stays until what Hagency added is out (on_solo_result)
        await self.say(rid, err or OFF)
        if not err and before["bypass_added"]:
            await self.take_out(room, sender, before)   # pm_solo_rules.py: by the owner's agent, or said plainly
        elif not err and (job := self.unanswered_add(rid)):   # GPT review: its answer may be lost; said plainly
            await self.say(rid, UNANSWERED.format(agent=local(job["agent"]), repo=job.get("repo"))
                           + solo_review.TAKE_OUT.format(repo=job.get("repo")))
        return err

    def waiting_for_checker(self, rid: str) -> str:
        """The paused issues that only waited for a checker: each goes on with "@pm retry #N"."""
        waiting = sorted(num(k) for k, v in self.state.items()
                         if v.get("room") == rid and v["stage"] == "stopped" and v.get("no_checker"))
        return "".join(f" #{n} is waiting for a check: say \"@pm retry #{n}\"." for n in waiting)

    async def checkers(self, room: MatrixRoom, item: dict) -> tuple[list[str], bool]:
        """(who may check this item's work, whether that is solo review)."""
        here = self.agents_in(room)
        agents = routing.available(here, self.agent_online)
        others = routing.checkers_for(agents, item["builder"], ownership.owner_id)
        if others or not await self.solo_on(room.room_id):
            return others, False
        if away := routing.checkers_for(here, item["builder"], ownership.owner_id):   # GPT review: another person's
            item["away_checker"] = [local(a) for a in away]   # agent, offline for now: waited for, never replaced
            return [], False
        own = routing.own_checkers(agents, item["builder"], ownership.owner_id)
        fit = [a for a in own if self.runs_solo(a)]   # pm_solo_code: never a solo check to code that predates it
        if own and not fit:                   # said by no_checker_steps, with "@pm retry #N"
            item["old_checker"] = [local(own[0]), await self.ask_update(own[0])]
        return fit, True

    def no_checker_steps(self, n: int, steps: str | None, solo: bool, room: MatrixRoom | None = None,
                         item: dict | None = None) -> str:
        """What to do when nobody may check #n: solo review (or wake / bring another agent of yours), or the PR link."""
        mine = ownership.owner_id(item["builder"]) if item else None
        asleep = [local(a) for a in (self.agents_in(room) if room and mine else [])
                  if a != item["builder"] and ownership.owner_id(a) == mine and self.agent_online(local(a)) is False]
        old = (item or {}).pop("old_checker", None) if solo else None   # pm_solo_code: code from before solo review
        away = (item or {}).pop("away_checker", None)                    # checkers: another person's, offline
        first = (update_line(old[0], old[1], n) if old else AWAY.format(names=", ".join(away), n=n) if away else
                 OFFLINE.format(names=", ".join(asleep), n=n) if solo and asleep
                 else (NO_OTHER if solo else HINT).format(n=n))   # review: in the room, but offline
        return "**What you can do:**\n\n" + f"- {first}" + (f"\n- Or: {steps}" if steps else "")

    def merge_failed_steps(self, item: dict, key: str, kind: str) -> str | None:
        """An admin merge (solo review) that GitHub's rules or a check stopped: how to let it merge, and "@pm retry #N"
        tries the merge again (review: the generic steps led to a retry that refuses an open PR). None otherwise."""
        if kind != "merge" or (item.get("task") or {}).get("admin") is not True:
            return None
        item["resume"] = "approved"
        return MERGE_FAILED.format(steps=solo_review.BYPASS_STEPS.format(repo=item["repo"]), n=num(key),
                                   link=no_reviewer_steps(num(key), item["repo"], item["pr"]))

    async def may_check(self, room: MatrixRoom, key: str, item: dict, sender: str) -> bool:
        """A checker's result counts: another person's agent, or (solo review, still on) the builder's owner's other
        agent. Solo switched off meanwhile: the item pauses, said plainly, instead of waiting for nothing."""
        if routing.checkers_for([sender], item["builder"], ownership.owner_id):
            return True
        if item.get("solo") is not True or not routing.own_checkers([sender], item["builder"], ownership.owner_id):
            return False
        on = await self.solo_on(room.room_id)
        if on and not routing.checkers_for(self.agents_in(room), item["builder"], ownership.owner_id):
            return True                       # GPT review: never while another person's agent is here (back) to check
        item["no_checker"] = True
        await self.stop(room.room_id, key, item, f"another person's agent is here to check it, so {local(sender)}'s "
                        "check doesn't count" if on else f"solo review was switched off while {local(sender)} checked it",
                        self.no_checker_steps(num(key), None, False))
        return False

    async def ask_checker(self, room: MatrixRoom, item: dict, key: str) -> None:
        """Send the review (stage "reviewing") or verify ("verifying") step to a checker, if there is one."""
        verify = item["stage"] == "verifying"
        what = f"verify #{num(key)} is already done" if verify else f"review PR #{item['pr']}"
        if await self.pick_reviewer(room, item, key, what):
            await self.dispatch(room.room_id, item, item["reviewer"], f"{item['reviewer']}: {what}"
                                + ("" if verify else f" against #{num(key)}"), routing.check_task(item, num(key)))

    async def check_again(self, room: MatrixRoom, key: str, item: dict) -> None:
        """"@pm retry #N" for an issue paused only for want of a checker: back at that step, its PR kept (a fresh
        build would race the open PR: pm_paused.requeue). If the step can't be handed out, it stays paused (GPT
        review: never left at "reviewing" with the previous step's task for the watchdog to resend)."""
        async with self.lock:
            if self.state.get(key) is not item or item["stage"] != "stopped" or not (
                    item.get("no_checker") or item.get("resume")):
                await self.say(room.room_id, f"#{num(key)} isn't waiting for a check anymore (it changed meanwhile).")
                return
            if item.pop("resume", None) == "approved":   # merge_failed_steps: GitHub's rules were fixed, merge again
                item.update(stage="approved", task_ids=[])
                self.save()
                await self.say(room.room_id, f"OK, I'll try merging PR #{item['pr']} for #{num(key)} again.")
                return
            item.pop("no_checker", None)
            item.update(stage="reviewing" if item.get("pr") else "verifying", task_ids=[])
            try:
                await self.ask_checker(room, item, key)
            except RuntimeError:              # loop.SendError / GitHub: paused as it was, said by the caller's log
                item.update(stage="stopped", no_checker=True)
                raise
            finally:
                self.save()

    async def solo_withdrawn(self, room_id: str, key: str, item: dict) -> bool:
        """An item checked in solo mode, before it merges: solo review is off now (or for another repo), so it pauses
        for another person's check, whatever GitHub's rules say (review: it merged on the solo verdict anyway)."""
        if await self.solo_on(room_id):
            return False
        item["no_checker"] = True
        await self.stop(room_id, key, item, f"solo review was switched off before PR #{item['pr']} merged",
                        self.no_checker_steps(num(key), no_reviewer_steps(num(key), item["repo"], item["pr"]), False))
        return True

    async def past_review_rule(self, room_id: str, key: str, item: dict, info: dict, lvl: str) -> None:
        """Solo review in a repo whose ruleset requires an approval GitHub won't take from the author's own
        account (loop_progress.advance): Autonomous merges as the repo's admin, Assist tells the person how."""
        state = await solo_review.read(self.client, room_id)
        if info.get("headRefOid") != item.get("sha"):
            await self.review_again(room_id, key, item, "it changed")   # the PR moved since it was checked
        elif lvl == "autonomous" and info.get("mergeable") == "MERGEABLE" and await self.checks_green(room_id, key, item):
            if not self.runs_solo(item["builder"]):   # pm_solo_code: its code would refuse (or not know) the admin merge
                item["resume"] = "approved"           # "@pm retry #N" tries the merge again, PR kept
                return await self.stop(room_id, key, item, f"{local(item['builder'])} runs code from before solo review",
                                       "**What you can do:**\n\n- " + update_line(local(item["builder"]), await self.ask_update(
                                           item["builder"]), num(key)))
            if err := await self.record_on(room_id):   # older agent code reads only an explicit "on" for this repo
                return await self.say(room_id, err)    # still "approved": tried again at the next poll
            item["stage"] = "merging"
            await self.dispatch(room_id, item, item["builder"], f"{item['builder']}: merge PR #{item['pr']} (solo review)",
                                {"kind": "merge", "issue": num(key), "pr": item["pr"], "sha": item["sha"], "admin": True})
        elif lvl != "autonomous" and item["stage"] == "approved":
            item["stage"] = "ready"           # review: the bypass button exists only once the rule lets admins past
            known = solo_review.bypass_ok(state, item["repo"])
            await self.say(room_id, SOLO_READY.format(pr=item["pr"]) if known else
                           SOLO_READY_NO_BYPASS.format(pr=item["pr"]) + solo_review.BYPASS_STEPS.format(repo=item["repo"]))
            if not known:                     # the first solo merge that needs it: asked once, with a 👍
                await self.offer_bypass(room_id)

    async def stopped_on_error(self, room: MatrixRoom, key: str, item: dict, kind: str, result: dict, why: str) -> None:
        """loop.on_result: a step failed. An admin merge GitHub's review rule refused (no bypass yet) is the first
        solo merge that needs the bypass: after the pause, @pm asks for it once with a 👍 (owner decision)."""
        await self.stop(room.room_id, key, item, why, self.merge_failed_steps(item, key, kind))
        if kind == "merge" and (item.get("task") or {}).get("admin") is True and \
                solo_review.NO_BYPASS in str(result.get("error")):
            await self.offer_bypass(room.room_id)

    async def checks_green(self, room_id: str, key: str, item: dict) -> bool:
        """No check on the PR failed or is still running (a failed one pauses the item). When @pm can't read the
        checks (the GitHub App's reads don't cover them), the merging agent's own check decides: it waits for
        unfinished checks and refuses a red one (solo_github.admin_merge)."""
        try:
            info = await github.run_json(["pr", "view", str(item["pr"]), "-R", item["repo"], "--json", "statusCheckRollup"])
        except github.GitHubError:
            return True                       # fail-safe: the agent re-checks every check itself before --admin
        state = solo_review.checks_state(info.get("statusCheckRollup") if isinstance(info, dict) else None)
        if state == "failed":
            await self.stop(room_id, key, item, f"a check on PR #{item['pr']} didn't pass")
        return state == "passed"
