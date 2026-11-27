"""Solo review on GitHub (docs/adr/0023-solo-review.md), run by an agent as ITS OWN GitHub account.

The live test: all of the owner's agents used one GitHub account, which also opened the PRs, and the repo's
ruleset required an approving review with no bypass. GitHub never lets an account approve its own PR, so in
solo mode the reviewing agent posts its verdict as a comment (tasks.submit_review) and the merge goes past the
review rule as a repo admin (`gh pr merge --admin`).

  - ruleset: the room owner's agent adds (or takes out again) {"Repository admin", pull requests only} on the
    rulesets that require a review on the default branch. Only `bypass_actors` is sent: every rule and every
    other bypass entry stay; only an entry Hagency added is ever taken out (@pm records which).
  - admin_merge: the builder's merge in solo mode. --admin goes past the WHOLE ruleset (GPT review), so it runs
    only when the only rule it would skip is the review: every other rule type on the branch is one a squash
    merge of a PR into its own base meets anyway, and every required check passed.
"""
import asyncio
import json
import os
import tempfile
import urllib.parse

from common import github
from common.github import GitHubError, run, run_json
from common.solo_review import ADMIN_BYPASS, NO_BYPASS, TASK_KIND, checks_state, is_admin_bypass

NOT_ADMIN = ("your GitHub account can't change {repo}'s rules (it must be the repo's admin; GitHub said: {why}). "
             "A repo admin adds the bypass on GitHub instead")
ORG_RULE = "an organization ruleset requires the review on {repo}; an organization owner adds the bypass there"
CAN_BYPASS = ("always", "pull_requests_only")
ACTOR_FIELDS = ("actor_id", "actor_type", "bypass_mode")
PAGE = 100
# Rule types a squash merge of a PR into its own base meets anyway; anything else (a merge queue, code scanning,
# deployments, ...) would be skipped by --admin, so then the merge is a person's.
SAFE_RULES = {"pull_request", "deletion", "non_fast_forward", "required_linear_history", "creation",
              "required_status_checks"}
MERGE_STATES = ("BLOCKED", "CLEAN", "HAS_HOOKS")   # never BEHIND, DIRTY, UNSTABLE, UNKNOWN or DRAFT
CHECK_WAIT_S, CHECK_POLL_S = 600, 20


def _repo(task: dict) -> str:
    repo = task.get("repo")
    if not isinstance(repo, str) or not github.REPO_RE.match(repo):
        raise ValueError(f"task has no valid repo: {repo!r}")
    return repo


async def _default_branch(agent: str, repo: str) -> str:
    return (await run_json(["repo", "view", repo, "--json", "defaultBranchRef"], agent))["defaultBranchRef"]["name"]


async def branch_rules(agent: str, repo: str, branch: str) -> list[dict]:
    """Every active rule on the branch, from every ruleset (GitHub's own list)."""
    rules = await run_json(["api", f"repos/{repo}/rules/branches/{urllib.parse.quote(branch, safe='')}?per_page={PAGE}"],
                           agent)
    if not isinstance(rules, list) or len(rules) >= PAGE:
        raise ValueError(f"couldn't read every rule on {repo}'s {branch} branch")
    return [r for r in rules if isinstance(r, dict)]


async def review_rulesets(agent: str, repo: str) -> list[tuple[int, str]]:
    """(ruleset id, its source type) for every rule that requires a pull request review on the default branch."""
    found: dict[int, str] = {}
    for r in await branch_rules(agent, repo, await _default_branch(agent, repo)):
        if r.get("type") == "pull_request" and isinstance(r.get("ruleset_id"), int):
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


async def _admin_of(agent: str, repo: str) -> bool:
    """This account still administers the repo (review: GitHub also says 404 for a ruleset it may no longer see)."""
    try:
        info = await run_json(["api", f"repos/{repo}"], agent)
    except GitHubError:
        return False                          # can't tell: the ruleset is kept on record, never dropped blind
    return isinstance(info, dict) and (info.get("permissions") or {}).get("admin") is True


async def _change_one(agent: str, repo: str, rid: int, on: bool) -> str:
    """"changed", "already" or "" (nothing of Hagency's there: "gone" when taking it out) for one ruleset; raises
    ValueError with what is wrong."""
    try:
        rs = await run_json(["api", f"repos/{repo}/rulesets/{rid}"], agent)
    except GitHubError as e:
        if not on and "404" in str(e) and await _admin_of(agent, repo):   # deleted (or another repo's): nothing left
            return "gone"
        raise
    if not isinstance(rs, dict) or not isinstance(rs.get("bypass_actors"), list):   # shown to admins only
        raise ValueError(NOT_ADMIN.format(repo=repo, why="the ruleset's bypass list isn't readable"))
    actors = [{k: a.get(k) for k in ACTOR_FIELDS} for a in rs["bypass_actors"] if isinstance(a, dict)]
    if on and any(is_admin_bypass(a) for a in actors):
        return "already"
    if on and (more := beyond_review(rs.get("rules") or [], checks_ok=False)):   # GPT review: a bypass covers the WHOLE ruleset
        raise ValueError(f"the ruleset \"{rs.get('name', rid)}\" on {repo} also requires {', '.join(more)}, which a "
                         "bypass would skip too. Put the review rule in a ruleset of its own, then switch solo review "
                         "off and on again")
    if not on and ADMIN_BYPASS not in actors:
        return "gone"
    await _put_bypass(agent, repo, rid, [*actors, ADMIN_BYPASS] if on else [a for a in actors if a != ADMIN_BYPASS])
    return "changed"


async def ruleset(agent: str, backend: str, base, task: dict) -> dict:
    """On: let repo admins merge past every review ruleset on the default branch. Off: take Hagency's entry out of
    exactly the rulesets @pm recorded (GPT review: also one that no longer applies to the branch). One ruleset that
    fails never stops the others (review: a deleted one blocked every later removal); what was done is reported
    with the errors, so @pm keeps track of it."""
    repo, on = _repo(task), task.get("on") is True
    if on:
        targets = await review_rulesets(agent, repo)
    else:
        targets = [(i, "Repository") for i in task.get("rulesets") or [] if isinstance(i, int)]
    done: dict[str, list[int]] = {"changed": [], "already": [], "gone": []}
    errors: list[str] = []
    for rid, source in targets:
        try:
            if source != "Repository":
                raise ValueError(ORG_RULE.format(repo=repo))
            if outcome := await _change_one(agent, repo, rid, on):
                done[outcome].append(rid)
        except (ValueError, GitHubError) as e:
            errors.append(str(e)[:300])
    if errors and not any(done.values()):
        raise ValueError("; ".join(dict.fromkeys(errors)))
    if errors:
        return {"kind": TASK_KIND, "on": on, **done, "error": "; ".join(dict.fromkeys(errors))[:600],
                "line": f"⚠️ changed only part of {repo}'s rules"}
    word = "let repo admins merge past" if on else "took the admin bypass out of"
    return {"kind": TASK_KIND, "on": on, **done, "line": f"{word} {len(done['changed'])} review rule(s) on {repo}"
            if done["changed"] else f"no review rule to change on {repo}"}


async def can_bypass(agent: str, repo: str, rules: list[dict]) -> bool:
    """This account may merge past every review ruleset on the branch (GitHub's own answer)."""
    for rid in sorted({r["ruleset_id"] for r in rules if r.get("type") == "pull_request" and isinstance(r.get("ruleset_id"), int)}):
        rs = await run_json(["api", f"repos/{repo}/rulesets/{rid}"], agent)
        if not isinstance(rs, dict) or rs.get("current_user_can_bypass") not in CAN_BYPASS:
            return False
    return True


def beyond_review(rules: list, checks_ok: bool = True) -> list[str]:
    """What --admin (or an admin bypass) would skip besides the approval itself; [] when nothing (GPT review).
    checks_ok False: required checks count too (a bypass would let an admin merge past red CI by hand)."""
    allowed = SAFE_RULES if checks_ok else SAFE_RULES - {"required_status_checks"}
    more = sorted({str(r.get("type")) for r in rules if isinstance(r, dict)} - allowed)
    for r in rules:
        params = (r.get("parameters") or {}) if isinstance(r, dict) else {}
        if r.get("type") == "pull_request" and params.get("required_review_thread_resolution") is True:
            more.append("resolved conversations")
        if r.get("type") == "pull_request" and params.get("require_code_owner_review") is True:
            more.append("a code owner's review")          # GPT review: the bypass would waive it too
        if r.get("type") == "pull_request" and params.get("required_reviewers"):
            more.append("approval from designated reviewers")
        if r.get("type") == "pull_request" and params.get("allowed_merge_methods") not in (None, []) \
                and "squash" not in params["allowed_merge_methods"]:
            more.append("a merge method other than squash")
        if r.get("type") == "required_status_checks" and any(
                isinstance(c, dict) and c.get("integration_id") for c in params.get("required_status_checks") or []):
            more.append("checks from one particular app")
    return more


def required_checks_passed(rules: list[dict], rollup: object, classic: list[str] = ()) -> bool:
    """Each check a ruleset (or classic branch protection) requires is there and passed (--admin would skip a
    missing one)."""
    ran = {c.get("name") or c.get("context"): c for c in rollup if isinstance(c, dict)} if isinstance(rollup, list) else {}
    needed = [need.get("context") for r in rules if r.get("type") == "required_status_checks"
              for need in (r.get("parameters") or {}).get("required_status_checks") or [] if isinstance(need, dict)]
    return all(name in ran and checks_state([ran[name]]) == "passed" for name in [*needed, *classic])


CLASSIC_EXTRA = {"required_signatures": "signed commits", "required_conversation_resolution": "resolved conversations",
                 "lock_branch": "a locked branch", "block_creations": "blocked creations"}


async def classic_protection(agent: str, repo: str, branch: str) -> dict:
    """The branch's classic protection ({} when it has none). Unreadable (not admin): ValueError."""
    try:
        info = await run_json(["api", f"repos/{repo}/branches/{urllib.parse.quote(branch, safe='')}/protection"], agent)
    except GitHubError as e:
        if "404" in str(e):                   # "Branch not protected": rulesets only (or nothing)
            return {}
        raise ValueError(f"couldn't read {repo}'s branch protection, so a person merges ({str(e)[:120]})") from e
    return info if isinstance(info, dict) else {}


async def classic_checks(agent: str, repo: str, branch: str) -> list[str]:
    """The checks classic branch protection requires on the branch. Raises ValueError when it also requires what
    --admin would skip beyond reviews and checks (review: signed commits, resolved conversations, push limits)."""
    prot = await classic_protection(agent, repo, branch)
    extra = [word for key, word in CLASSIC_EXTRA.items() if isinstance(prot.get(key), dict) and prot[key].get("enabled")]
    extra += ["limits on who may push"] if prot.get("restrictions") else []
    extra += ["a code owner's review"] if (prot.get("required_pull_request_reviews") or {}).get(
        "require_code_owner_reviews") is True else []      # GPT review: --admin skips it as well
    if extra:
        raise ValueError(f"{repo}'s branch protection also requires {', '.join(extra)}, which --admin would skip; "
                         "a person merges")
    req = prot.get("required_status_checks") or {}
    checks = [c for c in req.get("checks") or [] if isinstance(c, dict)]
    if any(c.get("app_id") not in (None, -1) for c in checks):     # GPT review: a namesake from another app
        raise ValueError(f"{repo}'s branch protection requires checks from one particular app; a person merges")
    return [*(req.get("contexts") or []), *(c.get("context") for c in checks)]


async def _ready_pr(agent: str, repo: str, pr: int) -> dict:
    """The PR once every check finished (waits up to CHECK_WAIT_S); raises ValueError when it can't merge."""
    for _ in range(CHECK_WAIT_S // CHECK_POLL_S):
        info = await run_json(["pr", "view", str(pr), "-R", repo, "--json", "state,mergeable,mergeStateStatus,"
                               "headRefOid,baseRefName,reviewDecision,statusCheckRollup"], agent)
        if checks_state(info.get("statusCheckRollup")) != "pending":
            return info
        await asyncio.sleep(CHECK_POLL_S)
    raise ValueError(f"PR #{pr}'s checks didn't finish in {CHECK_WAIT_S // 60} min; once they pass, a repo admin "
                     "merges it on GitHub")


async def admin_merge(agent: str, repo: str, n: int, pr: int, sha: str, still=None) -> dict:
    """Solo mode's merge past the review rule as an admin: the reviewed commit, into the default branch, every
    check green, and only when the review is the only rule --admin would skip. still: the agent adapter's own
    re-read of the room (Autonomous, solo review on), asked again right before the merge (GPT review: the wait
    for checks may be long)."""
    branch = await _default_branch(agent, repo)
    info = await _ready_pr(agent, repo, pr)
    if (info.get("state") != "OPEN" or info.get("mergeable") != "MERGEABLE" or info.get("headRefOid") != sha
            or info.get("baseRefName") != branch or info.get("mergeStateStatus") not in MERGE_STATES):
        raise ValueError(f"GitHub says PR #{pr} isn't ready at the reviewed commit into {branch}")
    if info.get("reviewDecision") == "CHANGES_REQUESTED" or checks_state(info.get("statusCheckRollup")) != "passed":
        raise ValueError(f"PR #{pr} has changes requested, or a check that hasn't passed")
    rules = await branch_rules(agent, repo, branch)
    if other := beyond_review(rules):
        raise ValueError(f"{repo}'s rules also require {', '.join(other)}, which --admin would skip; a person merges")
    if not required_checks_passed(rules, info.get("statusCheckRollup"), await classic_checks(agent, repo, branch)):
        raise ValueError(f"a check {repo}'s rules require hasn't passed on PR #{pr}")
    if not await can_bypass(agent, repo, rules):
        raise ValueError(NO_BYPASS)
    if callable(still) and not await still():
        raise ValueError("the room stopped, or solo review was switched off, while the checks ran")
    await run(["pr", "merge", str(pr), "-R", repo, "--squash", "--admin", "--match-head-commit", sha], agent)
    return {"kind": "merge", "issue": n, "pr": pr, "line": f"merged PR #{pr} (closes #{n}; solo review)"}
