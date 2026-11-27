"""Solo review's GitHub side, as @pm asks it (docs/adr/0023-solo-review.md): the room owner's agent, signed in
to GitHub as the owner (the repo's admin), lets repo admins merge past the repo's review rule when solo review
is switched on, and takes out again only what Hagency added when it is switched off
(adapters/agents/solo_github.ruleset does the GitHub calls).

Every ask is kept on disk by its task id, so a late answer (after a restart, or after the switch moved) is
still recorded, and the ruleset follows the switch: GPT review, a bypass Hagency added must never be lost.
Mixed into loop.Loop through pm_solo.Solo (client, say, room_repos, agents_in, agent_online, write_solo).
"""
import time
import uuid

from nio import MatrixRoom

from common import code_report, registry, room_state, solo_review
from common.redact import room_safe
from common.saved_dict import SavedDict
from server import console_auto_update
from server import ownership
from server.pm_cards import cannot, local

KEEP_JOBS_S = 7 * 24 * 3600   # an unanswered ask is forgotten after this
NO_OWNER_AGENT = "No agent of the room owner's is here to update GitHub's review rule on {repo}. "
NOT_WANTED = "If you don't want it anymore, take **Repository admin** out of the rule's Bypass list on GitHub."
NEEDS_UPDATE = "{agent} needs an update first: it runs code from before solo review. "   # live test 2026-11-27
ASKED = "I've asked it to update. Once My agents shows it Up to date, switch Solo review {again}."
BY_HAND = "Update it in My agents (in your chat with Product Manager), then switch Solo review {again}."
AGAIN_ON, AGAIN_OFF = "off and on again", "on and off again"   # off: the bypass stays until then (review)
AGAIN = (" Or bring the room owner's agent into the room and switch Solo review off and on again: it adds it for you "
         "when its GitHub account is the repo's admin.")


class SoloRules:
    def solo_jobs(self) -> dict:
        """task_id → a ruleset ask {room, agent, on, by, at}, on disk next to the boards."""
        if "_solo_jobs" not in self.__dict__:
            path = getattr(self, "board_file", None)
            self._solo_jobs = SavedDict(path.with_name(path.stem + "-solo-jobs.json")) if path else {}
        return self._solo_jobs

    async def ruleset_agent(self, room: MatrixRoom, sender: str) -> tuple[str | None, tuple | None]:
        """The room owner's agent that can run the ruleset task (the asker's first, when they are an owner), and
        else (owner, agent) of the first one whose code is too old for it (code_report.runs_task), or None."""
        owners = await room_state.owners(self.client, room)
        here, old = self.agents_in(room), None
        for owner in sorted(owners, key=lambda o: o != sender):
            mine = [a for a in here if ownership.owner_id(a) == owner]
            main = ownership.own_agent_id(owner)
            for agent in sorted(mine, key=lambda a: a != main):
                if cannot(owner, agent) is None and self.agent_online(local(agent)) is not False:
                    if code_report.runs_task((registry.find(owner, local(agent)) or {}).get(registry.CODE), solo_review.TASK_KIND):
                        return agent, None
                    old = old or (owner, agent)
        return None, old

    async def needs_update(self, rid: str, owner: str, agent: str, on: bool = True) -> None:
        """Said plainly (never "unknown task kind"), and the agent asked to update (console_auto_update). Switching
        off, the bypass stays on GitHub meanwhile: said, with how to take it out by hand."""
        asked = await console_auto_update.update_for_task(self, owner, local(agent))
        then = (ASKED if asked else BY_HAND).format(again=AGAIN_ON if on else AGAIN_OFF)
        await self.say(rid, NEEDS_UPDATE.format(agent=local(agent)) + then + ("" if on else " " + NOT_WANTED))

    async def ask_ruleset(self, room: MatrixRoom, sender: str, on: bool, ids: list[int]) -> None:
        """Ask the room owner's agent to let admins merge past the review rule (on), or to take the entries
        Hagency added (ids) out again. Never silent: what can't be done is said, with what to change."""
        rid = room.room_id
        repo = await room_state.repo_of(self.client, rid)
        if not repo:                          # solo is only switched on with a repo; its removal says what's left
            return
        agent, old = await self.ruleset_agent(room, sender)
        if agent is None and old:             # an owner's agent is here, on code from before the task
            await self.needs_update(rid, *old, on)
            return
        if agent is None:
            await self.say(rid, NO_OWNER_AGENT.format(repo=repo)
                           + (solo_review.BYPASS_STEPS.format(repo=repo) + AGAIN if on else NOT_WANTED))
            return
        jobs, task_id, now = self.solo_jobs(), uuid.uuid4().hex, time.time()
        for old in [t for t, j in jobs.items() if now - j.get("at", 0) > KEEP_JOBS_S]:
            jobs.pop(old, None)
        jobs[task_id] = {"room": rid, "agent": agent, "on": on, "by": sender, "at": now, "repo": repo}
        what = "let repo admins merge past" if on else "take the admin bypass Hagency added out of"
        await self.say(rid, f"{agent}: {what} GitHub's review rule on {repo}", agent,
                       {"kind": solo_review.TASK_KIND, "task_id": task_id, "repo": repo, "on": on, "rulesets": ids})

    async def take_out(self, room: MatrixRoom, sender: str, state: dict) -> None:
        """Hagency's recorded bypass out again: by the owner's agent while it is still the room's repo, else said
        plainly with how to do it by hand (review: a removed or changed repo left it silently)."""
        if state["repo"] and state["repo"] == state["bound"]:
            await self.ask_ruleset(room, sender, False, state["bypass_added"])
        else:
            await self.left_on(room.room_id, state)

    async def left_on(self, rid: str, state: dict, lead: str = "") -> None:
        """Say that the bypass stays on the old repo, and forget it (nothing here can reach that repo anymore)."""
        err, _ = await self.write_solo(rid, None, lambda s: set(), ok=False)
        where = state["repo"] or "the room's earlier repo"
        await self.say(rid, lead + (f"{err} " if err else "") + f"Hagency's admin bypass is still in the review rule "
                       f"on {where}. " + solo_review.TAKE_OUT.format(repo=where))

    async def follow_repo(self, rid: str, repo: str | None) -> None:
        """loop_progress.tick, when a room's repo is new to me: solo review was switched on for another repo (or
        the repo was removed), so it is off now, and what Hagency left on the old repo is said (review: a new repo
        must never inherit solo review without its own 👍)."""
        seen = self.__dict__.setdefault("_solo_seen", {})
        if rid not in seen or seen[rid] == repo:   # first sight (a start): nothing changed that I saw. The switch
            seen[rid] = repo                         # itself counts only for its own repo (solo_review.live), and
            return                                   # solo off still says what's left (take_out)
        try:
            state = await solo_review.read(self.client, rid, strict=True)
        except room_state.StateUnreadable:
            return                            # asked again at the next poll
        seen[rid] = repo
        if not state["repo"] or state["repo"] == repo or not (state["on"] or state["bypass_added"]):
            return
        lead = f"This room's repo changed (solo review was on for {state['repo']}), so solo review is off now. "
        if not state["bypass_added"]:
            err, _ = await self.write_solo(rid, False, repo=None, ok=False)
            await self.say(rid, err or lead)
            return
        err, _ = await self.write_solo(rid, False, ok=False)
        await self.left_on(rid, state, lead if not err else f"{err} ")

    async def on_solo_result(self, room: MatrixRoom, sender: str, result: dict) -> None:
        """The asked agent's answer, however late: what it changed is recorded (also on a partial failure),
        what went wrong is said with what to change, and a switch flipped meanwhile is followed."""
        rid, task_id = room.room_id, str(result.get("task_id"))
        job = self.solo_jobs().get(task_id)
        if not job or job["room"] != rid or job["agent"] != sender:
            return
        repo = job.get("repo") or "the repo"
        changed = ids_in(result, "changed")
        try:
            bound = (await solo_review.read(self.client, rid, strict=True))["bound"]
        except room_state.StateUnreadable:
            await self.say(rid, f"⚠️ I couldn't read this room's settings, so I couldn't note which review rules Hagency "
                                f"changed on {repo}: {sorted(changed) or 'none'}.")
            return
        if job.get("repo") != bound:          # review: never into another repo's record
            self.solo_jobs().pop(task_id, None)
            if job["on"] and changed:
                await self.say(rid, f"Hagency's admin bypass was added to the review rule on {repo} after this room "
                                    "moved to another repo. " + solo_review.TAKE_OUT.format(repo=repo))
            return
        gone = changed | ids_in(result, "gone")   # off: one deleted on GitHub (or not there) drops from the record too
        ok = job["on"] and bool(changed or ids_in(result, "already")) and not result.get("error")   # all of it, or not ok
        err, now = await self.write_solo(rid, None, (lambda s: s | changed) if job["on"] else (lambda s: s - gone),
                                         ok=ok if job["on"] else False)
        if err:                               # the ask stays on record: GitHub was changed, and that must be said
            await self.say(rid, f"{err} (I couldn't note which review rules Hagency changed on {repo}: "
                                f"{sorted(changed) or 'none'}.)")
            return
        self.solo_jobs().pop(task_id, None)
        recorded = sorted(set(now["bypass_added"]) | changed if job["on"] else set(now["bypass_added"]) - gone)
        if job["on"] and not now["on"] and recorded:   # switched off while it was added: take it out again
            await self.ask_ruleset(room, job.get("by", sender), False, recorded)
        elif not job["on"] and now["on"]:             # switched on again while it was taken out: put it back
            await self.ask_ruleset(room, job.get("by", sender), True, [])
        if code_report.UNKNOWN_KIND in str(result.get("error")):   # older code after all: plain words, and an update
            await self.needs_update(rid, ownership.owner_id(sender), sender, job["on"])
        elif result.get("error"):             # the follow-up first (GPT review): a failed announcement can't skip it
            await self.say(rid, f"⚠️ {local(sender)} couldn't update GitHub's review rule on {repo}: "
                                f"{room_safe(result['error'])}\n\n"
                                + (solo_review.BYPASS_STEPS.format(repo=repo) if job["on"] else NOT_WANTED))
        else:
            await self.say(rid, ruleset_line(repo, job["on"], bool(changed), bool(result.get("already"))))


def ids_in(result: dict, key: str) -> set[int]:
    return {i for i in result.get(key) or [] if isinstance(i, int)}


def ruleset_line(repo: str, on: bool, changed: bool, already: bool) -> str:
    if on and changed:
        return (f"✓ GitHub's review rule on {repo} now lets repo admins merge pull requests past it. I'll take that "
                "out again when solo review is switched off.")
    if on:
        return (f"✓ GitHub's review rule on {repo} already lets repo admins merge pull requests past it." if already
                else f"✓ {repo} has no GitHub review rule to change.")
    return (f"✓ I took the admin bypass Hagency added out of GitHub's review rule on {repo}." if changed
            else f"Nothing to take out of GitHub's review rule on {repo}.")
