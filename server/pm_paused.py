"""Paused issues when a room's mode changes. An issue is paused (stage "stopped") after an agent couldn't
finish it, so the loop never retries it forever; "@pm retry #N" puts one back (loop_progress.py).

Screen test: the owner asked @pm to drive all issues to the end (chat, then the panel), @pm answered
"Running on my own now", and nothing happened: #7 and #9 were paused. So, from every way the mode
changes (chat, a ✅-confirmed switch, !level, the panel: after_switch):
  - autonomous (the owner or a maintainer asked to drive everything): each paused issue goes back in
    the queue, unless its PR may still be open (a fresh build would race it): that one is named;
  - assist (a person merges each change): the paused issues are named, with how to go on.
"""
from common.redact import room_safe, secret_safe

MAX_REASON = 200      # characters of an agent's own error kept in the pause reason


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
    open_prs = {k for k, item in seen.items() if item.get("pr") and await loop.pr_open(item["pr"], repo)}
    async with loop.lock:                          # never between a poll and its results
        if await loop.level(rid) != "autonomous":  # GPT review: someone said "stop" (or assist) meanwhile
            return ""
        same = [k for k, item in seen.items() if loop.state.get(k) is item and item["stage"] == "stopped"]
        back = [k for k in same if k not in open_prs]
        for k in back:
            del loop.state[k]
        if back:
            loop.save()
    held = {k: seen[k]["pr"] for k in same if k in open_prs}
    parts = []
    if back:
        listed = " and ".join(f"#{num(k)}" for k in back) if len(back) <= 2 else _listed([num(k) for k in back])[0]
        parts.append(f"I put {listed} back in the queue: {'it was' if len(back) == 1 else 'they were'} paused "
                     "after an agent couldn't finish.")
    for k, pr in held.items():
        parts.append(f"#{num(k)} stays paused: its PR #{pr} may still be open. Merge or close it on GitHub, "
                     f"then say \"@pm retry #{num(k)}\".")
    return "\n\n" + " ".join(parts) if parts else ""
