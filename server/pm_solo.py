"""Solo review (docs/adr/0023-solo-review.md): who checks a build when a room's owner tests alone with several
of their OWN agents ("no need for a second person at this testing stage").

Off by default: the checker (review, verify) is another person's agent (routing.checkers_for). A moderator may
switch solo review on, always with a 👍 first (chat) or a click (the panel); then, only when no other person's
agent is here, another agent of the builder's owner checks it (routing.own_checkers; never the builder itself).
Switching it off is open to anyone, at once, like "stop".

GitHub never lets an account approve its own PR, and in the live test every agent used the owner's one account.
So a solo checker posts its verdict as a comment (adapters/agents/tasks.submit_review), and where the repo's
ruleset requires an approval the merge goes past it as the repo's admin (solo_github.admin_merge). Switching on
asks the room owner's agent to let repo admins past that rule (solo_github.ruleset); what Hagency added is
recorded and taken out again when it is switched off. Mixed into loop.Loop (client, say, dispatch, stop, ...).
"""
import asyncio
import time
import uuid

from nio import MatrixRoom

from common import github, intents, room_state, solo_review
from common.redact import room_safe
from server import ownership, routing
from server.pm_cards import cannot, local
from server.pm_chat import ONLY_MODERATORS
from server.pm_implicit import TO_CONFIRM
from server.pm_paused import num
from server.pm_settings import NEED_POWER

SOLO_TASK = solo_review.TASK_KIND
QUESTION = ("Turn on **solo review**? When no other person's agent is here, another of the builder's owner's own "
            "agents (never the builder itself) reviews and verifies the work; on GitHub its verdict is a comment. "
            "I'll also ask the room owner's agent to let repo admins merge past GitHub's review rule." + TO_CONFIRM)
ON = "Solo review is on: when no other person's agent is here, another of your own agents checks the work."
OFF = "Solo review is off: only another person's agent reviews and verifies work here."
ALREADY_ON = "Solo review is already on here."
HINT = ("**Testing alone?** Say **@pm allow solo review** (or switch on Solo review in the Hagency panel) to let your "
        "other agent check it, then **@pm retry #{n}**.")
NO_OTHER = ("Solo review is on, but none of the builder's owner's other agents is here (an agent never checks its own "
            "work). Bring a second agent of yours into the room, then say **@pm retry #{n}**.")
NO_OWNER_AGENT = "No agent of the room owner's is here to update GitHub's review rule on {repo}. "
SOLO_READY = ("PR #{pr} ready — a person merges. GitHub's review rule can't count a review by your own agent, so as "
              "the repo's admin use **Merge without waiting for requirements** on GitHub.")
WAIT_S = 600          # a ruleset task with no answer by then is forgotten (the panel's toggle may ask again)


class Solo:
    def solo_jobs(self) -> dict:
        """room → the ruleset task in flight {task_id, agent, on, at}."""
        return self.__dict__.setdefault("_solo_jobs", {})

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
        elif await self.solo_on(rid):
            if named:
                await self.say(rid, ALREADY_ON)
        else:
            await self.ask_once(rid, sender, gen, QUESTION, {"kind": "solo"}, named)   # pm_chat.confirm: solo_confirmed

    async def solo_from_panel(self, room: MatrixRoom, user: str, value: str) -> str:
        """The panel's toggle: a click is the moderator's confirmation; off is anyone's."""
        if value == "off":
            return await self.solo_off(room, user) or "ok"
        if value != "on":
            return "unknown value"
        if not await room_state.is_moderator(self.client, room, user):
            return "only the room owner or a maintainer can turn solo review on"
        return await self.solo_confirmed(room, user) or "ok"

    async def write_solo(self, rid: str, on: bool, added: list[int] | None = None) -> tuple[str | None, dict]:
        """(an error line or None, the state before). added: replaces the recorded rulesets (None keeps them)."""
        async with self.__dict__.setdefault("_solo_lock", asyncio.Lock()):
            before = await solo_review.read(self.client, rid)
            resp = await self.client.room_put_state(rid, solo_review.SOLO_EVENT, solo_review.content(
                on, before["bypass_added"] if added is None else added))
        if hasattr(resp, "event_id"):
            return None, before
        if getattr(resp, "status_code", "") == "M_FORBIDDEN":
            return NEED_POWER, before
        return f"⚠️ I couldn't save this room's settings: {room_safe(resp)}", before

    async def solo_confirmed(self, room: MatrixRoom, sender: str) -> str | None:
        """Switch it on (after the 👍 or the panel's click; the moderator was checked just now)."""
        rid = room.room_id
        err, _ = await self.write_solo(rid, True)
        if err:
            await self.say(rid, err)
            return err
        await self.say(rid, ON + self.waiting_for_checker(rid))
        await self.ask_ruleset(room, sender, True, [])
        return None

    async def solo_off(self, room: MatrixRoom, sender: str) -> str | None:
        rid = room.room_id
        if (self.pending.get(rid) or {}).get("kind") == "solo":
            self.pending.pop(rid, None)       # the question to switch it on is moot now
        err, before = await self.write_solo(rid, False)
        await self.say(rid, err or OFF)
        if not err and before["bypass_added"]:
            await self.ask_ruleset(room, sender, False, before["bypass_added"])
        return err

    def waiting_for_checker(self, rid: str) -> str:
        """The paused issues that only waited for a checker: each goes on with "@pm retry #N"."""
        waiting = sorted(num(k) for k, v in self.state.items()
                         if v.get("room") == rid and v["stage"] == "stopped" and v.get("no_checker"))
        return "".join(f" #{n} is waiting for a check: say \"@pm retry #{n}\"." for n in waiting)

    async def ruleset_agent(self, room: MatrixRoom, sender: str) -> str | None:
        """The room owner's agent that can run the ruleset task (the asker's first, when they are an owner)."""
        owners = await room_state.owners(self.client, room)
        here = self.agents_in(room)
        for owner in sorted(owners, key=lambda o: o != sender):
            mine = [a for a in here if ownership.owner_id(a) == owner]
            main = ownership.own_agent_id(owner)
            for agent in sorted(mine, key=lambda a: a != main):
                if cannot(owner, agent) is None and self.agent_online(local(agent)) is not False:
                    return agent
        return None

    async def ask_ruleset(self, room: MatrixRoom, sender: str, on: bool, ids: list[int]) -> None:
        """Ask the room owner's agent (GitHub's repo admin) to let admins merge past the review rule, or to take
        the entries Hagency added out again. Never silent: what can't be done is said, with what to change."""
        rid = room.room_id
        repo = self.room_repos.get(rid) or await room_state.repo_of(self.client, rid)
        if not repo:
            return                            # nothing on GitHub yet; the merge says what to change if needed
        agent = await self.ruleset_agent(room, sender)
        if agent is None:
            await self.say(rid, NO_OWNER_AGENT.format(repo=repo) + (solo_review.BYPASS_STEPS.format(repo=repo) if on else
                           "If you don't want it anymore, take **Repository admin** out of the rule's Bypass list on GitHub."))
            return
        task_id = uuid.uuid4().hex
        self.solo_jobs()[rid] = {"task_id": task_id, "agent": agent, "on": on, "at": time.time()}
        what = "let repo admins merge past" if on else "take the admin bypass Hagency added out of"
        await self.say(rid, f"{agent}: {what} GitHub's review rule on {repo}", agent,
                       {"kind": solo_review.TASK_KIND, "task_id": task_id, "repo": repo, "on": on, "rulesets": ids})

    async def on_solo_result(self, room: MatrixRoom, sender: str, result: dict) -> None:
        """The owner's agent's answer to ask_ruleset: recorded (what Hagency added) and said plainly."""
        rid = room.room_id
        job = self.solo_jobs().get(rid)
        if not job or job["task_id"] != result.get("task_id") or job["agent"] != sender or time.time() - job["at"] > WAIT_S:
            return
        self.solo_jobs().pop(rid, None)
        repo = self.room_repos.get(rid) or await room_state.repo_of(self.client, rid) or "the repo"
        if result.get("error"):
            fix = solo_review.BYPASS_STEPS.format(repo=repo) if job["on"] else \
                "Take **Repository admin** out of the rule's Bypass list on GitHub if you don't want it anymore."
            await self.say(rid, f"⚠️ {local(sender)} couldn't update GitHub's review rule on {repo}: "
                                f"{room_safe(result['error'])}\n\n{fix}")
            return
        changed = {i for i in result.get("changed") or [] if isinstance(i, int)}
        now = await solo_review.read(self.client, rid)
        added = set(now["bypass_added"]) | changed if job["on"] else set(now["bypass_added"]) - changed
        if added != set(now["bypass_added"]) and (err := (await self.write_solo(rid, now["on"], sorted(added)))[0]):
            await self.say(rid, f"{err} (I couldn't note which review rules Hagency changed on {repo}.)")
            return
        await self.say(rid, ruleset_line(repo, job["on"], bool(changed), bool(result.get("already"))))

    async def checkers(self, room: MatrixRoom, item: dict) -> tuple[list[str], bool]:
        """(who may check this item's work, whether that is solo review)."""
        agents = routing.available(self.agents_in(room), self.agent_online)
        others = routing.checkers_for(agents, item["builder"], ownership.owner_id)
        if others or not await self.solo_on(room.room_id):
            return others, False
        return routing.own_checkers(agents, item["builder"], ownership.owner_id), True

    def no_checker_steps(self, n: int, steps: str | None, solo: bool) -> str:
        """What to do when nobody may check #n: solo review (or another agent of yours), or the PR link."""
        first = (NO_OTHER if solo else HINT).format(n=n)
        return "**What you can do:**\n\n" + f"- {first}" + (f"\n- Or: {steps}" if steps else "")

    async def may_check(self, room: MatrixRoom, key: str, item: dict, sender: str) -> bool:
        """A checker's result counts: another person's agent, or (solo review, still on) the builder's owner's other
        agent. Solo switched off meanwhile: the item pauses, said plainly, instead of waiting for nothing."""
        if routing.checkers_for([sender], item["builder"], ownership.owner_id):
            return True
        if item.get("solo") is not True or not routing.own_checkers([sender], item["builder"], ownership.owner_id):
            return False
        if await self.solo_on(room.room_id):
            return True
        item["no_checker"] = True
        await self.stop(room.room_id, key, item, f"solo review was switched off while {local(sender)} checked it",
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
        build would race the open PR: pm_paused.requeue)."""
        async with self.lock:
            if self.state.get(key) is not item or item["stage"] != "stopped":
                await self.say(room.room_id, f"#{num(key)} isn't paused anymore (it changed meanwhile).")
                return
            item.pop("no_checker", None)
            item.pop("why", None)
            item["stage"] = "reviewing" if item.get("pr") else "verifying"
            await self.ask_checker(room, item, key)
            self.save()

    async def past_review_rule(self, room_id: str, key: str, item: dict, info: dict, lvl: str) -> None:
        """Solo review in a repo whose ruleset requires an approval GitHub won't take from the author's own
        account (loop_progress.advance): Autonomous merges as the repo's admin, Assist tells the person how."""
        if info.get("headRefOid") != item.get("sha"):
            item["stage"] = "reviewing"       # the PR moved since it was checked: check what's there now
            await self.dispatch(room_id, item, item["reviewer"], f"{item['reviewer']}: review PR #{item['pr']} again "
                                f"(it changed) against #{num(key)}", routing.check_task(item, num(key)))
        elif lvl == "autonomous" and info.get("mergeable") == "MERGEABLE" and await self.checks_green(room_id, key, item):
            item["stage"] = "merging"
            await self.dispatch(room_id, item, item["builder"], f"{item['builder']}: merge PR #{item['pr']} (solo review)",
                                {"kind": "merge", "issue": num(key), "pr": item["pr"], "sha": item["sha"], "admin": True})
        elif lvl != "autonomous" and item["stage"] == "approved":
            item["stage"] = "ready"
            await self.say(room_id, SOLO_READY.format(pr=item["pr"]))

    async def checks_green(self, room_id: str, key: str, item: dict) -> bool:
        """Every check on the PR passed; a failed one pauses the item (an unfinished one: wait for it)."""
        info = await github.run_json(["pr", "view", str(item["pr"]), "-R", item["repo"], "--json", "statusCheckRollup"])
        state = solo_review.checks_state(info.get("statusCheckRollup") if isinstance(info, dict) else None)
        if state == "failed":
            await self.stop(room_id, key, item, f"a check on PR #{item['pr']} didn't pass")
        return state == "passed"


def ruleset_line(repo: str, on: bool, changed: bool, already: bool) -> str:
    if on and changed:
        return (f"✓ GitHub's review rule on {repo} now lets repo admins merge pull requests past it. I'll take that "
                "out again when solo review is switched off.")
    if on:
        return (f"✓ GitHub's review rule on {repo} already lets repo admins merge pull requests past it." if already
                else f"✓ {repo} has no GitHub review rule to change.")
    return (f"✓ I took the admin bypass Hagency added out of GitHub's review rule on {repo}." if changed
            else f"Nothing to take out of GitHub's review rule on {repo}.")
