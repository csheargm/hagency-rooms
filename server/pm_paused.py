"""Paused issues when a room's mode changes. An issue is paused (stage "stopped") after an agent couldn't
finish it, so the loop never retries it forever; "@pm retry #N" puts one back (loop_progress.py).

Screen test: the owner asked @pm to drive all issues to the end (chat, then the panel), @pm answered
"Running on my own now", and nothing happened: #7 and #9 were paused. So, from every way the mode
changes (chat, a ✅-confirmed switch, !level, the panel: after_switch):
  - autonomous (the owner or a maintainer asked to drive everything): each paused issue goes back in
    the queue, unless its PR may still be open (a fresh build would race it): that one is named;
  - assist (a person merges each change): the paused issues are named, with how to go on.

Both ways back in the queue (this and "@pm retry #N") first ask GitHub for the issue's open PR (open_prs):
the one recorded, or one on an agent's branch for the issue that was never recorded (the agent crashed
after pushing; a retry then built a second, duplicate PR).
"""
import re

from common import github
from common.redact import room_safe, secret_safe

MAX_REASON = 200      # characters of an agent's own error kept in the pause reason
MAX_OPEN_PRS = 1000   # open PRs read in one look (the GitHub App read's own ceiling)
AGENT_BRANCH = re.compile(r"agent/[^/]+/issue-([1-9][0-9]*)")   # adapters/agents/tasks.py names it so


def num(key: str) -> int:
    """The issue number in a loop-state key ("owner/name#7" → 7)."""
    return int(str(key).rsplit("#", 1)[-1])


def failure(agent: str, kind: str, error: object) -> str:
    """Why an issue paused when its agent failed: the agent's own reason, shortened (screen test: "cx-agent
    couldn't build it" alone left people guessing). The text is the agent's: escaped where it's shown."""
    reason = " ".join(room_safe(secret_safe(error or "")).split())     # a custom agent may send anything
    reason = reason if len(reason) <= MAX_REASON else reason[:MAX_REASON - 1] + "…"
    return f"{agent} couldn't {kind} it" + (f": {reason}" if reason else "")


def next_steps(n: int, agent: str | None = None) -> str:
    """What a person can do about a paused issue, said every time one pauses (screen test: the owner
    asked how anyone could act on "couldn't build it, needs a person")."""
    name = agent.split(":")[0].lstrip("@") if agent else None
    steps = [f"- **Try again:** the room's owner or a maintainer says **@pm retry #{n}**. This often works "
             "when the problem was passing, like the agent's computer being asleep.",
             f"- **Make the issue clearer** on GitHub (what to change, and where), then say **@pm retry #{n}**."]
    if name:
        steps.append(f"- **See the details:** {name}'s owner can read {name}.log on that computer (in "
                     "~/.hagency).")
    steps.append(f"- **Do it yourself:** make the change on GitHub and close #{n}. I'll move on.")
    return "**What you can do:**\n\n" + "\n".join(steps)


async def agent_prs(repo: str) -> dict[int, int]:
    """Open PRs on an agent's branch for an issue (issue number → PR number), recorded or not. Raises
    github.GitHubError when GitHub can't say. A fork's PR doesn't count: agents push to the repo itself."""
    prs = await github.run_json(["pr", "list", "-R", repo, "--state", "open", "--json",
                                 "number,headRefName,isCrossRepository", "--limit", str(MAX_OPEN_PRS)])
    if not isinstance(prs, list):
        raise github.GitHubError("GitHub's list of open PRs wasn't a list")
    found: dict[int, int] = {}
    for pr in prs:
        m = AGENT_BRANCH.fullmatch(str(pr.get("headRefName") or "")) if isinstance(pr, dict) else None
        if m and not pr.get("isCrossRepository") and isinstance(pr.get("number"), int):
            found.setdefault(int(m.group(1)), pr["number"])
    return found


async def open_prs(loop, items: dict[str, dict], repo: str) -> dict[str, int | None]:
    """The paused issues a fresh build would race, each with its open PR (key → PR number; None when
    GitHub can't tell: that one stays paused too, never a second PR blind). The others may go back."""
    try:
        found: dict[int, int] | None = await agent_prs(repo)
    except github.GitHubError:
        found = None          # can't tell: each stays paused, and still_open() says why to the person
    held: dict[str, int | None] = {}
    for k, item in items.items():
        if item.get("pr") and await loop.pr_open(item["pr"], repo):      # fail-safe: True when unsure
            held[k] = item["pr"]
        elif found is None or num(k) in found:
            held[k] = None if found is None else found[num(k)]
    return held


def still_open(n: int, pr: int | None) -> str:
    """Why "@pm retry #N" left #N paused."""
    if pr is None:
        return (f"I couldn't reach GitHub to check whether #{n} already has an open PR, so it stays paused. "
                f"Say \"@pm retry #{n}\" again in a minute.")
    return f"PR #{pr} for #{n} is still open. Merge or close it on GitHub, then say \"@pm retry #{n}\" again."


def _listed(numbers: list[int]) -> tuple[str, str, str]:
    """"#7, #9", and the verb and pronoun that go with how many there are."""
    return ", ".join(f"#{n}" for n in numbers), *(("is", "it") if len(numbers) == 1 else ("are", "them"))


async def after_switch(loop, rid: str, level: str) -> str:
    """What to add to "mode changed" (the switch itself already succeeded)."""
    if level == "autonomous":
        return await retry_all(loop, rid)
    return await note(loop, rid) if level == "assist" else ""


async def note(loop, rid: str) -> str:
    """For assist mode: the paused issues, and how to go on ("" when none)."""
    _, paused = await loop.paused_here(rid)
    if not paused:
        return ""
    listed, verb, them = _listed([num(k) for k in paused])
    return (f"\n\n{listed} {verb} paused because an agent couldn't finish {them}, so I won't start {them} "
            f"on my own. To try again, say \"@pm retry #{num(paused[0])}\".")


async def retry_all(loop, rid: str) -> str:
    """For autonomous mode: every paused issue back in the queue, said in one line ("" when none).
    GitHub is asked about open PRs outside the loop's lock (GPT review: slow calls would hold up every
    room), then each issue is re-checked under the lock before it moves."""
    async with loop.lock:
        repo, paused = await loop.paused_here(rid)
        seen = {k: loop.state[k] for k in paused}   # these very pauses: a retried-and-paused-again one is new
    held_prs = await open_prs(loop, seen, repo)
    async with loop.lock:                          # never between a poll and its results
        if await loop.level(rid) != "autonomous":  # GPT review: someone said "stop" (or assist) meanwhile
            return ""
        same = [k for k, item in seen.items() if loop.state.get(k) is item and item["stage"] == "stopped"]
        back = [k for k in same if k not in held_prs]
        for k in back:
            del loop.state[k]
        if back:
            loop.save()
    held = {k: held_prs[k] for k in same if k in held_prs}
    parts = []
    if back:
        listed = " and ".join(f"#{num(k)}" for k in back) if len(back) <= 2 else _listed([num(k) for k in back])[0]
        parts.append(f"I put {listed} back in the queue: {'it was' if len(back) == 1 else 'they were'} paused "
                     "after an agent couldn't finish.")
    for k, pr in held.items():
        parts.append(f"#{num(k)} stays paused: its PR #{pr} may still be open. Merge or close it on GitHub, "
                     f"then say \"@pm retry #{num(k)}\"." if pr else
                     f"#{num(k)} stays paused: I couldn't reach GitHub to check for its open PR. Say "
                     f"\"@pm retry #{num(k)}\" in a minute.")
    return "\n\n" + " ".join(parts) if parts else ""
