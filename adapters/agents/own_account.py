"""A reviewing agent on the same GitHub account as the PR's author (adapters/agents/tasks.submit_review).

Full-loop live test 2026-11-23: both agents used one GitHub account, and GitHub refuses (HTTP 422) an approval
of a PR its own account opened. The owner decided one account must not block the loop (ADR 0016): the review is
posted as a comment carrying the verdict, and a repo that requires no reviews merges on that verdict.
"""
from common.github import GitHubError, run_json

AS_COMMENT = "Verdict: {verdict} (posted as a comment: GitHub doesn't let an account approve its own pull request)\n\n"
# Solo review (ADR 0023): the author's own other agent checked it, so GitHub must never count it as an approval.
AS_SOLO = "Verdict: {verdict} (solo review: the author's own other agent checked it, so it is posted as a comment)\n\n"


async def opened_it(agent: str, pr: int, repo: str) -> bool:
    """Whether the reviewing agent's GitHub account is the PR's author (False when GitHub can't say)."""
    try:
        me = (await run_json(["api", "user"], agent))["login"]
        author = (await run_json(["pr", "view", str(pr), "-R", repo, "--json", "author"], agent))["author"]["login"]
    except (GitHubError, KeyError, TypeError):   # unknown: the original error is raised as it was
        return False
    return me.lower() == author.lower()
