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

from common import room_state, solo_review
from common.redact import room_safe
from common.saved_dict import SavedDict
from server import ownership
from server.pm_cards import cannot, local

KEEP_JOBS_S = 7 * 24 * 3600   # an unanswered ask is forgotten after this
NO_OWNER_AGENT = "No agent of the room owner's is here to update GitHub's review rule on {repo}. "
NOT_WANTED = "If you don't want it anymore, take **Repository admin** out of the rule's Bypass list on GitHub."


class SoloRules:
    def solo_jobs(self) -> dict:
        """task_id → a ruleset ask {room, agent, on, by, at}, on disk next to the boards."""
        if "_solo_jobs" not in self.__dict__:
            path = getattr(self, "board_file", None)
            self._solo_jobs = SavedDict(path.with_name(path.stem + "-solo-jobs.json")) if path else {}
        return self._solo_jobs

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
        """Ask the room owner's agent to let admins merge past the review rule (on), or to take the entries
        Hagency added (ids) out again. Never silent: what can't be done is said, with what to change."""
        rid = room.room_id
        repo = self.room_repos.get(rid) or await room_state.repo_of(self.client, rid)
        if not repo:
            return                            # nothing on GitHub yet; a merge says what to change if needed
        agent = await self.ruleset_agent(room, sender)
        if agent is None:
            await self.say(rid, NO_OWNER_AGENT.format(repo=repo)
                           + (solo_review.BYPASS_STEPS.format(repo=repo) if on else NOT_WANTED))
            return
        jobs, task_id, now = self.solo_jobs(), uuid.uuid4().hex, time.time()
        for old in [t for t, j in jobs.items() if now - j.get("at", 0) > KEEP_JOBS_S]:
            jobs.pop(old, None)
        jobs[task_id] = {"room": rid, "agent": agent, "on": on, "by": sender, "at": now}
        what = "let repo admins merge past" if on else "take the admin bypass Hagency added out of"
        await self.say(rid, f"{agent}: {what} GitHub's review rule on {repo}", agent,
                       {"kind": solo_review.TASK_KIND, "task_id": task_id, "repo": repo, "on": on, "rulesets": ids})

    async def on_solo_result(self, room: MatrixRoom, sender: str, result: dict) -> None:
        """The asked agent's answer, however late: what it changed is recorded (also on a partial failure),
        what went wrong is said with what to change, and a switch flipped meanwhile is followed."""
        rid, task_id = room.room_id, str(result.get("task_id"))
        job = self.solo_jobs().get(task_id)
        if not job or job["room"] != rid or job["agent"] != sender:
            return
        self.solo_jobs().pop(task_id, None)
        repo = self.room_repos.get(rid) or await room_state.repo_of(self.client, rid) or "the repo"
        changed = {i for i in result.get("changed") or [] if isinstance(i, int)}
        err, now = await self.write_solo(rid, None, (lambda s: s | changed) if job["on"] else (lambda s: s - changed))
        if err:
            await self.say(rid, f"{err} (I couldn't note which review rules Hagency changed on {repo}.)")
        elif result.get("error"):
            await self.say(rid, f"⚠️ {local(sender)} couldn't update GitHub's review rule on {repo}: "
                                f"{room_safe(result['error'])}\n\n"
                                + (solo_review.BYPASS_STEPS.format(repo=repo) if job["on"] else NOT_WANTED))
        else:
            await self.say(rid, ruleset_line(repo, job["on"], bool(changed), bool(result.get("already"))))
        recorded = sorted(set(now["bypass_added"]) | changed if job["on"] else set(now["bypass_added"]) - changed)
        if job["on"] and not now["on"] and recorded:   # switched off while it was added: take it out again
            await self.ask_ruleset(room, job.get("by", sender), False, recorded)
        elif not job["on"] and now["on"]:             # switched on again while it was taken out: put it back
            await self.ask_ruleset(room, job.get("by", sender), True, [])


def ruleset_line(repo: str, on: bool, changed: bool, already: bool) -> str:
    if on and changed:
        return (f"✓ GitHub's review rule on {repo} now lets repo admins merge pull requests past it. I'll take that "
                "out again when solo review is switched off.")
    if on:
        return (f"✓ GitHub's review rule on {repo} already lets repo admins merge pull requests past it." if already
                else f"✓ {repo} has no GitHub review rule to change.")
    return (f"✓ I took the admin bypass Hagency added out of GitHub's review rule on {repo}." if changed
            else f"Nothing to take out of GitHub's review rule on {repo}.")
