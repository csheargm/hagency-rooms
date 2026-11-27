"""pm's polling half: pick up the next issue and move each item on once GitHub's state allows.

Mixed into loop.Loop (which supplies client, state, save, say, dispatch, level, agents_in).
"""
import logging
import os
import time

from nio import MatrixRoom

from adapters.agents import agents
from common import github
from common import room_state
from common.redact import markup_safe as safe
from server import routing
from server.pm_paused import approve_steps, finish_merged, next_steps, num, requeue  # noqa: F401 - num: other modules import it from here

log = logging.getLogger("loop")

# Stages in which an item is still in flight (anything else is done/stopped).
ACTIVE = ("building", "reviewing", "fixing", "approved", "ready", "merging", "merge_sent",
          "verifying", "confirmed_done", "close_ready", "closing", "close_sent")
CLOSE_STAGES = ("confirmed_done", "close_ready", "close_sent")
AGENT_STAGES = ("building", "reviewing", "fixing", "verifying", "merging", "closing")   # waiting on an agent
GITHUB_STAGES = ("approved", "merge_sent", "close_sent")                    # waiting on GitHub
# ("ready" / "close_ready" wait on a person in Assist: no timeout.)
# A step's answer is overdue once the agent's own turn limit plus its gh/git calls have surely passed.
STEP_TIMEOUT_S = float(os.environ.get("LOOP_STEP_TIMEOUT_S", agents.TURN_TIMEOUT_S + 600))
GITHUB_WAIT_S = float(os.environ.get("LOOP_GITHUB_WAIT_S", 900))
MAX_SENDS = 2                    # first send + one resend, then a person takes over
MAX_FIX_ROUNDS = 2               # times a builder revises its PR after "changes requested"


def issue_key(repo: str, n: int) -> str:
    return f"{repo}#{n}"


def migrate(state: dict) -> dict:
    """Keys are "owner/name#7" (one room, one repo; two repos can both have an issue #7). Older
    entries were keyed by number alone and belonged to the then server-wide repo."""
    out = {}
    for key, item in state.items():
        repo = item.get("repo") or github.REPO
        out[key if "#" in key else f"{repo}#{num(key)}"] = {**item, "repo": repo}
    return out


class Progress:
    def key_for_result(self, room_id: str, result: dict) -> str | None:
        """The item a result answers: this room's, this issue, and one of the task_ids it was sent."""
        for key, item in self.state.items():
            if (item.get("room") == room_id and str(num(key)) == str(result.get("issue"))
                    and result.get("task_id") in item.get("task_ids", [item.get("task_id")])):
                return key
        return None

    # --- polling: pick up issues, check GitHub's merge verdict -------------------------
    async def tick(self) -> None:
        issues_of: dict[str, list] = {}               # one GitHub call per repo per poll
        for room_id, room in list(self.client.rooms.items()):   # a room joined mid-tick waits for the next one
            repo = await room_state.repo_of(self.client, room_id)
            await self.follow_repo(room_id, repo)   # pm_solo_rules.py: a new repo (or none) never inherits solo review
            if repo is None:
                self.room_repos.pop(room_id, None)
                self.open_issues.pop(room_id, None)
                continue                      # a room without a repo never sees any repo's issues
            self.room_repos[room_id] = repo
            if repo not in issues_of:
                try:
                    issues_of[repo] = await github.run_json(["issue", "list", "-R", repo, "--label", github.LABEL,
                                                             "--state", "open", "--json", "number,title", "--limit", "50"])
                except github.GitHubError as e:     # one unreadable repo never stops the other rooms
                    log.warning("can't list issues on %s: %s", repo, str(e)[:200])
                    issues_of[repo] = None
            if issues_of[repo] is None:
                self.open_issues.pop(room_id, None)   # never show another repo's queue under this one
                continue
            self.open_issues[room_id] = sorted((i for i in issues_of[repo] if issue_key(repo, i["number"]) not in self.state),
                                               key=lambda i: i["number"])   # "Next" on the board, even while stopped
            await finish_merged(self, room_id, repo)   # a paused PR a person merged: done
            lvl = await self.level(room_id)
            if lvl == "off":
                continue
            active = [(k, v) for k, v in self.state.items()     # only the room's current repo's items
                      if v["room"] == room_id and v.get("repo") == repo and v["stage"] in ACTIVE]
            for key, item in active:
                if item.get("held"):          # a step decided while stopped: send it now
                    await self.dispatch(room_id, item, item["agent"], item["body"], item["task"])
                    continue
                if not await self.watchdog(room_id, key, item):
                    before = item["stage"]
                    await self.advance(room_id, key, item, lvl)        # GitHub's latest state first...
                    await self.github_timeout(room_id, key, item, before)   # ...then the timeout
            if not active:
                await self.claim_next(room, lvl)
            await self.announce_done(room_id)

    async def announce_done(self, room_id: str) -> None:
        """Once nothing is left (none active, none queued), say so once, with where to try it."""
        if self.open_issues.get(room_id) or any(v["room"] == room_id and v["stage"] in ACTIVE
                                                for v in self.state.values()):
            return
        board = self.boards.setdefault(room_id, {})
        since = board.get("announced", 0)
        repo = self.room_repos.get(room_id)
        done = sorted((k for k, v in self.state.items() if v["room"] == room_id and v.get("repo") == repo and v["stage"] == "done"
                       and v.get("ended", 0) > since), key=num)
        if not done:
            return
        board["announced"] = time.time()
        self.save_boards()
        stopped = [k for k, v in self.state.items() if v["room"] == room_id and v.get("repo") == repo and v["stage"] == "stopped"
                   and v.get("ended", 0) > since]
        extra = (f" ({', '.join(f'#{num(k)}' for k in stopped)} paused: say \"@pm retry #N\" or handle it on GitHub.)"
                 if stopped else "")
        await self.say(room_id, f"🎉 All done: {', '.join(f'#{num(k)}' for k in done)} merged.{extra} Try it: "
                                f"{await self.where_to_try(self.room_repos.get(room_id, ''))}. Tell me what to change, "
                                "then say \"let's build it\".")

    async def where_to_try(self, repo: str) -> str:
        """The live site if the repo publishes one (GitHub Pages), else the repo and its README."""
        try:
            site = await github.run_json(["api", f"repos/{repo}/pages"])
            if site.get("html_url"):
                return f"{site['html_url']} (live site; updates a minute after each merge)"
        except github.GitHubError:
            pass                              # no Pages site: point at the repo instead
        return f"https://github.com/{repo} (see its README for how to run it)"

    async def watchdog(self, room_id: str, key: str, item: dict) -> bool:
        """Never wait forever: resend a step with no answer once, and stop items GitHub keeps blocked.
        Returns True if it acted on the item this tick."""
        now = time.time()
        if item.get("seen_stage") != item["stage"]:
            item.update(seen_stage=item["stage"], since=now)
            self.save()
        if item["stage"] in AGENT_STAGES and now - (item.get("sent_at") or item["since"]) > STEP_TIMEOUT_S:
            who = str(item.get("agent", "the agent")).split(":")[0].lstrip("@")
            if item.get("sends", 1) >= MAX_SENDS or "task" not in item:
                return await self.stop(room_id, key, item, f"no answer from {who} on #{num(key)}")
            await self.dispatch(room_id, item, item["agent"], item["body"], item["task"], resend=True)
            await self.say(room_id, f"no answer from {who} on #{num(key)} yet; asked again")
            return True
        return False

    async def github_timeout(self, room_id: str, key: str, item: dict, before: str) -> None:
        """Stop an item GitHub has kept blocked too long, judged AFTER advance() saw GitHub's state."""
        if item["stage"] == before and before in GITHUB_STAGES and time.time() - item["since"] > GITHUB_WAIT_S:
            await self.stop(room_id, key, item, f"GitHub still blocks #{num(key)} after {GITHUB_WAIT_S / 60:.0f} min")

    async def after_review_step(self, room_id: str, item: dict, key: str, result: dict) -> None:
        """A review approves (→ GitHub's gate) or asks for changes (→ the builder fixes); a fix goes
        back to the same reviewer, whose new review is pinned to the new commit."""
        if result["kind"] == "fix":
            item.update(stage="reviewing", fix_sha=result.get("sha"))
            await self.dispatch(room_id, item, item["reviewer"],
                                f"{item['reviewer']}: review PR #{item['pr']} again (after fixes) against #{num(key)}",
                                routing.check_task(item, num(key)))
            return
        item["sha"] = str(result["sha"])
        if result.get("verdict") == "approve":
            item["stage"] = "approved"
        else:
            await self.request_fix(room_id, item, key, str(result.get("reasons", "")))

    async def request_fix(self, room_id: str, item: dict, key: str, reasons: str) -> None:
        """Changes requested: the builder revises its own PR (up to MAX_FIX_ROUNDS), then a person."""
        rounds = item.get("fix_rounds", 0)
        if rounds >= MAX_FIX_ROUNDS:
            await self.stop(room_id, key, item, f"PR #{item['pr']} still has changes requested after "
                                                f"{rounds} rounds of fixes")
            return
        item.update(stage="fixing", fix_rounds=rounds + 1)
        # The task first (stage and dispatch are recorded together); the announcement can't block it.
        await self.dispatch(room_id, item, item["builder"], f"{item['builder']}: fix PR #{item['pr']} for #{num(key)} "
                            "after the review", {"kind": "fix", "issue": num(key), "pr": item["pr"], "feedback": reasons})
        try:
            await self.say(room_id, f"{item['reviewer'].split(':')[0][1:]} asked for changes on PR #{item['pr']}; "
                                    f"{item['builder'].split(':')[0][1:]} revises it (round {rounds + 1}/{MAX_FIX_ROUNDS}).")
        except RuntimeError as e:             # loop.SendError: just the announcement; the fix is already on its way
            log.warning("couldn't announce the fix round in %s: %s", room_id, e)

    async def stop(self, room_id: str, key: str, item: dict, why: str, steps: str | None = None) -> bool:
        item.update(stage="stopped", why=why)
        self.save()
        await self.say(room_id, f"⏸ #{num(key)} is paused: {safe(why).rstrip('.')}.\n\n"
                                f"{steps or next_steps(num(key), item.get('agent'))}")
        return True

    async def paused_here(self, rid: str) -> tuple[str | None, list[str]]:
        """The room's repo, and its issues paused after an agent couldn't finish them (stage "stopped")."""
        repo = self.room_repos.get(rid) or await room_state.repo_of(self.client, rid)
        return repo, sorted((k for k, v in self.state.items() if v["room"] == rid and v.get("repo") == repo
                             and v["stage"] == "stopped"), key=num)

    async def retry(self, room: MatrixRoom, sender: str, n: int | None) -> None:
        """"@pm retry #9": a paused issue goes back in the queue (owner or maintainer)."""
        rid = room.room_id
        if not await room_state.is_moderator(self.client, room, sender):
            await self.say(rid, "Only the room's owner or a maintainer can restart paused work.")
            return
        async with self.lock:                   # never between a poll and its results
            repo, paused = await self.paused_here(rid)
            key = paused[0] if n is None and len(paused) == 1 else (issue_key(repo, n) if n and repo else None)
            if key not in paused:
                listed = ", ".join(f"#{num(k)}" for k in paused)
                await self.say(rid, f"Paused here: {listed}. Say which one, e.g. \"@pm retry #{num(paused[0])}\"."
                               if paused and n is None else
                               "Nothing is paused here." if not paused else f"#{n} isn't paused. Paused here: {listed}.")
                return
            item = self.state[key]
        if item.get("no_checker") or item.get("resume"):   # pm_solo.py: back at the check, or the merge, PR kept
            return await self.check_again(room, key, item)
        if why := await requeue(self, key, item, repo):   # GitHub is asked outside the lock (pm_paused)
            await self.say(rid, why)
            return
        await self.say(rid, f"OK, #{num(key)} goes back in the queue; I'll pick it up within a minute.")

    async def pr_open(self, pr: int, repo: str) -> bool:
        try:
            return (await github.run_json(["pr", "view", str(pr), "-R", repo, "--json", "state"])).get("state") == "OPEN"
        except github.GitHubError:
            return True               # can't tell: assume it's open (never start a second PR blind)

    async def claim_next(self, room: MatrixRoom, lvl: str) -> None:
        fresh = self.open_issues.get(room.room_id, [])
        agents = self.agents_in(room)
        if not fresh or not agents:
            return
        issue = fresh[0]
        n = issue["number"]
        builder = agents[n % len(agents)]
        repo = self.room_repos[room.room_id]
        item = self.state[issue_key(repo, n)] = {"room": room.room_id, "stage": "building", "builder": builder,
                                                 "title": issue["title"], "repo": repo}
        self.open_issues[room.room_id] = fresh[1:]
        await self.say(room.room_id, f"Starting #{n} {issue['title']} with {builder.split(':')[0][1:]}.")
        await self.dispatch(room.room_id, item, builder, f"{builder}: build #{n} {issue['title']} ({lvl})",
                            {"kind": "build", "issue": n})

    async def advance(self, room_id: str, key: str, item: dict, lvl: str) -> None:
        if item["stage"] in CLOSE_STAGES:
            await self.advance_close(room_id, key, item, lvl)
            return
        if item["stage"] not in ("approved", "ready", "merge_sent"):
            return                                   # waiting on an agent's result message
        if item.get("solo") is True and item["stage"] != "merge_sent" and await self.solo_withdrawn(room_id, key, item):
            return                                   # pm_solo.py: checked solo, but solo review is off now
        ok, info = await github.merge_ready(item["pr"], repo=item["repo"])
        if info.get("state") == "MERGED":
            by = "the loop" if item["stage"] == "merge_sent" else "a person"
            item["stage"] = "done"
            await self.say(room_id, f"PR #{item['pr']} merged by {by}; #{num(key)} done")
        elif item["stage"] == "merge_sent":
            pass                                     # merge requested; wait for GitHub to show MERGED
        elif item["stage"] == "approved" and info.get("mergeable") == "CONFLICTING":
            await self.stop(room_id, key, item, f"PR #{item['pr']} has merge conflicts")   # don't block the queue
        elif ok and info.get("headRefOid") and info["headRefOid"] != item.get("sha"):
            item["stage"] = "reviewing"       # the PR moved since it was approved: review what's there now
            await self.dispatch(room_id, item, item["reviewer"],
                                f"{item['reviewer']}: review PR #{item['pr']} again (it changed) against #{num(key)}",
                                routing.check_task(item, num(key)))
        elif ok and lvl == "autonomous":
            item["stage"] = "merging"
            await self.dispatch(room_id, item, item["builder"], f"{item['builder']}: merge PR #{item['pr']}",
                                {"kind": "merge", "issue": num(key), "pr": item["pr"], "sha": item["sha"]})
        elif ok and item["stage"] == "approved":
            item["stage"] = "ready"
            await self.say(room_id, f"PR #{item['pr']} ready — a person merges")
        elif not ok and item.get("solo") is True and info.get("reviewDecision") == "REVIEW_REQUIRED" \
                and item["stage"] in ("approved", "ready"):   # pm_solo.py: GitHub won't count your own agent's review
            await self.past_review_rule(room_id, key, item, info, lvl)
        elif not ok and item["stage"] in ("approved", "ready") and info.get("reviewDecision") in ("REVIEW_REQUIRED", "CHANGES_REQUESTED"):
            await self.stop(room_id, key, item, f"the repo's rules need a GitHub approval of PR #{item['pr']} from an "
                            "account other than the one that opened it", approve_steps(item["repo"], item["pr"]))
        self.save()

    async def advance_close(self, room_id: str, key: str, item: dict, lvl: str) -> None:
        """An already-done issue, confirmed by a second agent: Autonomous closes it, Assist asks a person."""
        issue = await github.run_json(["issue", "view", str(num(key)), "-R", item["repo"], "--json", "state"])
        if issue.get("state") == "CLOSED":
            by = "the loop" if item["stage"] == "close_sent" else "a person"
            item["stage"] = "done"
            await self.say(room_id, f"#{num(key)} closed by {by} (already done)")
        elif item["stage"] == "confirmed_done" and lvl == "autonomous":
            item["stage"] = "closing"
            await self.dispatch(room_id, item, item["builder"], f"{item['builder']}: close #{num(key)} (already done)",
                                {"kind": "close", "issue": num(key), "verifier": item["reviewer"]})
        elif item["stage"] == "confirmed_done":
            item["stage"] = "close_ready"
            await self.say(room_id, f"#{num(key)} is already done (confirmed by {item['reviewer'].split(':')[0][1:]}) — a person closes")
        self.save()
