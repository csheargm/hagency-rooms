"""The demo repos trial builds work on (ADR 0027): a fixed pool in the hagency-trials org, made by the
owner from a template with seeded `agent-ok` issues (docs/TRIAL_BUILDER_SETUP.md). No repo is ever
created at runtime, so the Trial App needs no Administration permission.

Each visitor (by GitHub account id) leases one repo; their builds branch from its default branch. A
lease ends after TRIAL_LEASE_DAYS, or once every PR it opened is closed or merged; the next visitor
who needs that repo gets it recycled first: trial PRs closed, trial branches deleted, the default
branch reset to the template commit (the commit the repo had when the pool first saw it).
State: pool.json in the worker's shared folder ({repo: {"template", "github_id", "since", "prs"}}).
"""
import logging
import time
from pathlib import Path

from common import github
from common.saved_dict import SavedDict

log = logging.getLogger("trial-build")

IDLE_S = 3600                        # a lease with no PR: well past any build's 15 minutes (GPT review)


class NoRepoFree(RuntimeError):
    """Every demo repo is leased to someone else right now."""


class Pool:
    def __init__(self, path: Path, repos: list[str], lease_days: int, clock=time.time) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.state, self.repos, self.lease_s, self.clock = SavedDict(path), list(repos), lease_days * 86400, clock

    def entry(self, repo: str) -> dict:
        return dict(self.state.get(repo) or {})

    def mine(self, github_id: int) -> str | None:
        for repo in self.repos:
            e = self.entry(repo)
            if e.get("github_id") == github_id and self.clock() - float(e.get("since") or 0) < self.lease_s:
                return repo
        return None

    async def ended(self, gh, token: str, repo: str) -> bool:
        """Free to lease: never leased, expired, or every PR its visitor's builds opened is closed."""
        e = self.entry(repo)
        if e.get("github_id") is None or self.clock() - float(e.get("since") or 0) >= self.lease_s:
            return True
        for number in e.get("prs") or []:
            if await gh.pr_state(token, repo, number) == "open":
                return False
        if e.get("prs"):
            return True
        return self.clock() - float(e.get("since") or 0) >= IDLE_S   # nothing opened: theirs only while building

    async def lease(self, gh, github_id: int) -> str:
        """Their repo (recycled first if someone else had it), or NoRepoFree. A repo GitHub refuses is skipped
        and logged for the operator, never a failure for everyone (independent review)."""
        if mine := self.mine(github_id):
            return mine
        for repo in self.repos:
            try:
                e = await self.free_entry(gh, repo)
                if e is None:
                    continue
                if e.get("github_id") is not None:
                    await self.recycle(gh, repo, e["template"])
            except github.GitHubError as err:
                log.warning("demo repo %s skipped: %s", repo, err)
                continue
            self.state[repo] = {**e, "github_id": github_id, "since": self.clock(), "prs": []}
            return repo
        raise NoRepoFree()

    async def free_entry(self, gh, repo: str) -> dict | None:
        """`repo`'s record when it may be leased now (its template commit known), else None."""
        token = await gh.token(repo)                      # read only: most repos are just looked at
        try:
            if not await self.ended(gh, token, repo):
                return None
            e = self.entry(repo)
            if not e.get("template"):
                e["template"] = (await gh.base(token, repo))[1]   # first sight: this commit is the template
            return e
        finally:
            await gh.revoke(token)

    async def status(self, gh) -> tuple[int, list[int]]:
        """(repos free now, GitHub ids holding a lease): @pm counts a build only when one is free or theirs.
        A repo GitHub refuses counts as not free."""
        free, holders = 0, []
        for repo in self.repos:
            e = self.entry(repo)
            try:
                if e.get("github_id") is None or self.clock() - float(e.get("since") or 0) >= self.lease_s:
                    free += 1
                    continue
                token = await gh.token(repo)
                try:
                    ended = await self.ended(gh, token, repo)
                finally:
                    await gh.revoke(token)
            except github.GitHubError as err:
                log.warning("demo repo %s unreadable: %s", repo, err)
                continue
            free += ended
            if not ended:
                holders.append(e["github_id"])
        return free, holders

    async def recycle(self, gh, repo: str, template: str) -> None:
        """A write token only for this (GPT review: one was minted for every repo looked at)."""
        token = await gh.token(repo, write=True)
        try:
            await gh.recycle(token, repo, template)
        finally:
            await gh.revoke(token)

    def opened(self, repo: str, number: int) -> None:
        e = self.entry(repo)
        self.state[repo] = {**e, "prs": [*(e.get("prs") or []), number]}
