"""The Hagency panel's "Project repo" card actions (console.act hands them here): choose the room's
repo, remove it (pm_unbind.py: the page asked twice first), and check that it is ready; and the Mode
card's Solo review toggle (pm_solo.py), which also changes how the repo's review rule is met. Each goes
through @pm's own checks, and choosing or removing is also answered in the chat.
"""
from server import github_setup
from common import room_state

ACTIONS = ("repo", "unrepo", "check", "solo")


async def act(loop, room, user: str, action: str, value: str) -> str:
    if action == "solo":                  # pm_solo.py: the Mode card's Solo review toggle (on: moderators)
        return await loop.solo_from_panel(room, user, value)
    if action == "repo":
        return await loop.set_repo(room, user, value.strip())
    if action == "unrepo":
        seen = value.removeprefix("sure:")    # the repo the page showed (GPT review: never a newer one)
        return await loop.remove_repo(room, user, seen) if value.startswith("sure:") and seen else \
            "click Remove repo twice to remove it"
    return await check_repo(loop, room, user)


async def check_repo(loop, room, user: str) -> str:
    """Is this room's repo ready for the loop? Read-only: changing the repo's settings stays with its
    GitHub owner (github_setup.py ruleset), never with @pm."""
    if not await room_state.is_moderator(loop.client, room, user):
        return "only the room owner or a maintainer can run the check"
    repo = await room_state.repo_of(loop.client, room.room_id)
    if repo is None:
        return "this room has no repo yet"
    agents = [a.split(":")[0].lstrip("@") for a in loop.agents_in(room)]
    if not agents:
        return "no agents in this room yet (each person's agent joins with them)"
    _, problems = await github_setup.readiness(repo, agents, detail=False)
    if not problems:
        return f"✓ {repo} is ready: {', '.join(agents)} can write, and the agent-ok label exists."
    return "\n".join([f"{repo} isn't ready yet:", *(f"✗ {p}" for p in problems),
                      f"The repo's owner adds the agent-ok label on GitHub (or runs python bin/github_setup.py "
                      f"ruleset --repo {repo}, which also adds GitHub's review rule)."])
