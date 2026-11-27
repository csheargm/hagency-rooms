"""A pull request's diff for the trial agent to talk about (ADR 0025): read-only, through the server's
GitHub App (github_app.get, which only reads), and only from the repo of the room it was asked in, so
it can never be used to read another repo the App is installed on. Nothing is ever written to GitHub:
the trial agent has no GitHub identity, so what it says about a PR is never a review that counts.
"""
import re

from common import github
from common import github_app
from common.redact import secret_safe
from server import trial_limits

MAX_DIFF_CHARS = 12000                # what fits next to the question
PR_RE = re.compile(r"\b(?:PR|pull request|pull)\s*#?\s*(\d{1,7})\b", re.I)
URL_RE = re.compile(r"github\.com/([A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100})/pull/(\d{1,7})\b")
NO_APP = "(This server can't read GitHub, so I can't see that pull request.)"


def pr_number(text: str, repo: str | None) -> int | None:
    """The PR this message asks about, in this room's repo only (a link to another repo: None)."""
    if not repo:
        return None
    if m := URL_RE.search(text):
        return int(m.group(2)) if m.group(1).lower() == repo.lower() else None
    m = PR_RE.search(text)
    return int(m.group(1)) if m else None


def shaped(repo: str, n: int, pr: dict, files: list) -> str:
    """Title, then each file's patch, cut at MAX_DIFF_CHARS; credentials scrubbed."""
    parts = [f"{repo} PR #{n}: {pr.get('title') or ''} ({pr.get('state') or ''})"]
    for f in files:
        if isinstance(f, dict):
            parts.append(f"--- {f.get('filename')} ({f.get('status')}, +{f.get('additions')} -{f.get('deletions')})\n"
                         f"{f.get('patch') or '(no text diff)'}")
    text = "\n".join(parts)
    if len(text) > MAX_DIFF_CHARS:
        text = text[:MAX_DIFF_CHARS] + "\n… (the rest of the diff was cut)"
    return secret_safe(text, trial_limits.api_key())


async def pr_diff(repo: str, n: int) -> str:
    """The diff to include, or a short note saying why there isn't one (never raises)."""
    if not github_app.configured():
        return NO_APP
    try:
        pr = await github_app.get(repo, f"/repos/{repo}/pulls/{n}")
        files = await github_app.get(repo, f"/repos/{repo}/pulls/{n}/files?per_page=100")
    except (github.GitHubError, OSError):   # not there, not installed, or the App's key unreadable: said, not hidden
        return f"(I couldn't read PR #{n} on {repo}: it may not exist, or this server's GitHub App can't see it.)"
    return shaped(repo, n, pr if isinstance(pr, dict) else {}, files if isinstance(files, list) else [])
