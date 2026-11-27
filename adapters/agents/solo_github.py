"""Solo review on GitHub (docs/adr/0023-solo-review.md), run by an agent as ITS OWN GitHub account.

The live test: all of the owner's agents used one GitHub account, which also opened the PRs, and the repo's
ruleset required an approving review with no bypass. GitHub never lets an account approve its own PR, so in
solo mode the reviewing agent posts its verdict as a comment (tasks.submit_review) and the merge goes past the
review rule as a repo admin (`gh pr merge --admin`), only when the ruleset lets admins past and every check passed.

  - ruleset: the room owner's agent adds (or takes out again) {"Repository admin", pull requests only} on the
    rulesets that require a review on the default branch. Existing bypass entries and every rule are kept;
    only an entry Hagency added is ever taken out (@pm records which: common/solo_review.py).
  - admin_merge: the builder's merge in solo mode.
"""
import json
import os
import tempfile

from common import github
from common.github import GitHubError, run, run_json
from common.solo_review import ADMIN_BYPASS, NO_BYPASS, TASK_KIND, checks_state, is_admin_bypass

NOT_ADMIN = ("your GitHub account can't change {repo}'s rules (it must be the repo's admin; GitHub said: {why}). "
             "A repo admin adds the bypass on GitHub instead")
ORG_RULE = "an organization ruleset requires the review on {repo}; an organization owner adds the bypass there"
CAN_BYPASS = ("always", "pull_requests_only")
ACTOR_FIELDS = ("actor_id", "actor_type", "bypass_mode")


def _repo(task: dict) -> str:
    repo = task.get("repo")
    if not isinstance(repo, str) or not github.REPO_RE.match(repo):
        raise ValueError(f"task has no valid repo: {repo!r}")
    return repo


async def review_rulesets(agent: str, repo: str) -> list[tuple[int, str]]:
    """(ruleset id, its source type) for every rule that requires a pull request review on the default branch."""
    branch = (await run_json(["repo", "view", repo, "--json", "defaultBranchRef"], agent))["defaultBranchRef"]["name"]
    rules = await run_json(["api", f"repos/{repo}/rules/branches/{branch}"], agent)
    found: dict[int, str] = {}
    for r in rules if isinstance(rules, list) else []:
        if isinstance(r, dict) and r.get("type") == "pull_request" and isinstance(r.get("ruleset_id"), int):
            found.setdefault(r["ruleset_id"], str(r.get("ruleset_source_type") or "Repository"))
    return sorted(found.items())


async def _put_bypass(agent: str, repo: str, rid: int, actors: list[dict]) -> None:
    """Only bypass_actors is sent: GitHub keeps the ruleset's rules and conditions as they are."""
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump({"bypass_actors": actors}, f)
    try:
        await run(["api", "-X", "PUT", f"repos/{repo}/rulesets/{rid}", "--input", f.name], agent)
    except GitHubError as e:
        raise ValueError(NOT_ADMIN.format(repo=repo, why=str(e)[:200])) from e
    finally:
        os.unlink(f.name)


async def ruleset(agent: str, backend: str, base, task: dict) -> dict:
    """Let repo admins merge past the review rule (on), or take out the entries Hagency added (off)."""
    repo, on = _repo(task), task.get("on") is True
    ours = {i for i in task.get("rulesets") or [] if isinstance(i, int)}
    changed, already = [], []
    for rid, source in await review_rulesets(agent, repo):
        if source != "Repository":
            raise ValueError(ORG_RULE.format(repo=repo))
        rs = await run_json(["api", f"repos/{repo}/rulesets/{rid}"], agent)
        if not isinstance(rs, dict) or not isinstance(rs.get("bypass_actors"), list):   # shown to admins only
            raise ValueError(NOT_ADMIN.format(repo=repo, why="the ruleset's bypass list isn't readable"))
        actors = [{k: a.get(k) for k in ACTOR_FIELDS} for a in rs["bypass_actors"] if isinstance(a, dict)]
        if on and any(is_admin_bypass(a) for a in actors):
            already.append(rid)
            continue
        if not on and (rid not in ours or ADMIN_BYPASS not in actors):
            continue
        await _put_bypass(agent, repo, rid, [*actors, ADMIN_BYPASS] if on else [a for a in actors if a != ADMIN_BYPASS])
        changed.append(rid)
    word = "let repo admins merge past" if on else "took the admin bypass out of"
    return {"kind": TASK_KIND, "on": on, "changed": changed, "already": already,
            "line": f"{word} {len(changed)} review rule(s) on {repo}" if changed else f"no review rule to change on {repo}"}


async def can_bypass(agent: str, repo: str) -> bool:
    """This account may merge past every review rule on the default branch (GitHub's own answer)."""
    for rid, _ in await review_rulesets(agent, repo):
        rs = await run_json(["api", f"repos/{repo}/rulesets/{rid}"], agent)
        if not isinstance(rs, dict) or rs.get("current_user_can_bypass") not in CAN_BYPASS:
            return False
    return True


async def admin_merge(agent: str, repo: str, n: int, pr: int, sha: str) -> dict:
    """Solo mode's merge: the reviewed commit, mergeable, every check green, past the review rule as an admin."""
    info = await run_json(["pr", "view", str(pr), "-R", repo, "--json",
                           "state,mergeable,headRefOid,reviewDecision,statusCheckRollup"], agent)
    if info.get("state") != "OPEN" or info.get("mergeable") != "MERGEABLE" or info.get("headRefOid") != sha:
        raise ValueError(f"GitHub says PR #{pr} isn't ready at the reviewed commit")
    if info.get("reviewDecision") == "CHANGES_REQUESTED" or checks_state(info.get("statusCheckRollup")) != "passed":
        raise ValueError(f"PR #{pr} has changes requested, or a check that hasn't passed")
    if not await can_bypass(agent, repo):
        raise ValueError(NO_BYPASS)
    await run(["pr", "merge", str(pr), "-R", repo, "--squash", "--admin", "--match-head-commit", sha], agent)
    return {"kind": "merge", "issue": n, "pr": pr, "line": f"merged PR #{pr} (closes #{n}; solo review)"}
