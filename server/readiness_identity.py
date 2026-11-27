"""Each agent's GitHub account for the "Check readiness" (github_setup.readiness), as far as @pm can know it.

An agent on a person's computer signs in there (ADR 0021: its own gh folder, never on this server) and
reports its state to @pm with a real token probe (common/github_login.STATE_TYPE, kept as registry.GITHUB).
So that report is what counts, never `gh` run here under its name: on the server that folder doesn't exist,
and an agent that just opened a PR was called "not signed in" (live test 2026-11-27). Its Write access is
read as the server's GitHub App (or the host's gh in local dev). What can't be read is said as unknown,
never as "not signed in". Only an agent the server itself runs (older local test agents) is asked with gh.
"""
from common import github
from common import github_app
from common import registry
from common.github import GitHubError
from server import console_update

NO_REPORT = "{agent}: hasn't reported its GitHub sign-in yet (it's offline or needs Update)"
SIGNED_OUT = "{agent}: not signed in to GitHub: click Sign in to GitHub in My agents"
NO_GH = ("{agent}: its computer needs the GitHub CLI (https://cli.github.com) first, then click Sign in to GitHub "
         "in My agents")
HERMES = "{agent}: couldn't check its GitHub sign-in (a Hermes agent signs in on its own computer and doesn't report it)"
NO_LOGIN = ("{agent}: says it is signed in to GitHub but not as which account, so its Write access couldn't be "
            "checked: click Sign in again in My agents")
WRITERS = ("admin", "maintain", "write")


def entry_of(agent: str) -> dict | None:
    """Its registry entry (by localpart), or None when it isn't one registered agent."""
    owner = registry.owner_of(agent)
    found = registry.find(owner, agent) if owner else None
    return found if found and found["localpart"] == agent else None


async def can_write(repo: str, login: str) -> bool | None:
    """`login` may push to `repo` (True/False), or None when GitHub couldn't be asked."""
    try:
        if github_app.configured():
            level = await github_app.permission(repo, login)
        else:                                 # local dev: the host's own gh login reads it
            level = (await github.run_json(["api", f"repos/{repo}/collaborators/{login}/permission"])).get("permission")
    except (GitHubError, AttributeError):     # unknown, said as "couldn't check" (evaluate), never as "can't write"
        return None
    return level in WRITERS


async def reported(agent: str, found: dict, repo: str) -> dict:
    """{"login", "push", "reported": True}, or {"problem": what to do} from the agent's own report."""
    said = found.get(registry.GITHUB)
    if said is None:
        return {"problem": (HERMES if console_update.is_hermes(found) else NO_REPORT).format(agent=agent)}
    if said["state"] != "signed_in":
        return {"problem": (NO_GH if said["state"] == "no_gh" else SIGNED_OUT).format(agent=agent)}
    if not said.get("login"):
        return {"problem": NO_LOGIN.format(agent=agent)}
    return {"login": said["login"], "push": await can_write(repo, said["login"]), "reported": True}
