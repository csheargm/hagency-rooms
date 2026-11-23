"""A reviewing agent on the same GitHub account as the PR's author (adapters/agents/tasks.submit_review).

Full-loop live test 2026-11-23: both agents used one GitHub account, and GitHub refuses (HTTP 422) an approval
of a PR its own account opened. The loop merges only on GitHub's own approval from another account
(github.merge_ready), so the fix is a second account, said plainly instead of "gh api -X failed: … (HTTP 422)".
"""
from common.github import GitHubError, run_json

SAME_ACCOUNT = ("{agent} is signed in to the same GitHub account that opened PR #{pr}, and GitHub doesn't let an "
                "account approve its own pull request. Sign {agent} in with a different GitHub account")


async def opened_it(agent: str, pr: int, repo: str) -> bool:
    """Whether the reviewing agent's GitHub account is the PR's author (False when GitHub can't say)."""
    try:
        me = (await run_json(["api", "user"], agent))["login"]
        author = (await run_json(["pr", "view", str(pr), "-R", repo, "--json", "author"], agent))["author"]["login"]
    except (GitHubError, KeyError, TypeError):   # unknown: the original error is raised as it was
        return False
    return me.lower() == author.lower()
