"""Why a room can't use a repo, in plain words that name the one missing step (owner decision, batch
12–14). Any repo works now: the room owner or a maintainer names it, the Hagency Rooms GitHub App is on
it, and their connected GitHub account can write there. An operator may still restrict the choice
(HAGENCY_REPOS: github.allowed_repo).

  - not on the operator's list (only when one is set): NOT_LISTED. The list is the server's own
    setting, so saying so tells nothing about GitHub.
  - the App isn't on it: INSTALL with the one-click link (pm_settings.choose_repo then waits for the
    install). GitHub answers "not installed" alike for a repo that doesn't exist and a private one
    without the App, so this never confirms a repo.
  - no write access: NO_WRITE, naming their account, for a public repo only. For a private repo (or
    when GitHub can't say), the INSTALL words again, and the wait stays quiet unless it can bind:
    any other answer would tell anyone in any room that a private repo of that name exists and has
    our App (GPT review, batch 12–14; REVIEWS.md).
"""
import logging

from common import github
from common import github_app
from server import github_link

log = logging.getLogger("loop")
NOT_LISTED = "This server only allows certain repos, and {repo} isn't one of them."
INSTALL = ("Install the Hagency Rooms app on {repo} (read-only): {url}. I'll finish setting up this room as soon as "
           "it's installed.")
NO_WRITE = "Your GitHub account @{login} can't write to {repo}, so this room can't use it."
NO_WRITE_HERE = "Your agent's GitHub account can't write to {repo}, so this room can't use it."   # no App: local gh
LINK_CHANGED = "Your GitHub connection changed while I was checking; please ask again."


def not_listed(repo: str) -> str:
    return NOT_LISTED.format(repo=repo)


def install(repo: str) -> str:
    return INSTALL.format(repo=repo, url=github_app.install_url())


async def cant_write(user: str, repo: str) -> str | None:
    """The answer when `user` can't write to `repo`, for a public repo; None for a private one (or when
    GitHub couldn't tell): the caller then says INSTALL, as for a repo without the App."""
    if not github_app.configured():
        return NO_WRITE_HERE.format(repo=repo)
    login = (github_link.link_of(user) or {}).get("login") or "?"
    try:
        info = await github_app.get(repo, f"/repos/{repo}")
    except github.GitHubError as e:           # not known to be public: the words that confirm nothing
        log.warning("couldn't read %s for its refusal: %s", repo, str(e)[:160])
        info = None
    public = isinstance(info, dict) and info.get("private") is False
    return NO_WRITE.format(login=login, repo=repo) if public else None
