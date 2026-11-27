"""Automatic agent updates, @pm's side (docs/adr/0024-automatic-agent-updates.md).

Each owner has one Auto-update switch in My agents, on unless they switched it off (KEY on their line in
agents.json). With it on, @pm asks each of their agents that runs older code to update to this server's own
commit (console_update.ask with "auto": never newer than the server): every CHECK_S (pm_presence.check_agents,
so also right after a deploy started @pm) and when an agent reports its code (pm_kind_reports). The agent
waits until it has no task running (agent_update.py), then self_update.py moves it forward with ADR 0020's
checks, and it restarts. Only code that says it waits (code_report.WAITS) is asked; older code keeps the
Update button once.

- One ask per owner at a time, STAGGER_S apart: their agents may share one install (live test 2026-11-27).
- Back-off: a failure waits BASE_BACKOFF_S, doubling up to MAX_BACKOFF_S; after MAX_FAILURES, or a refusal
  that retrying can't change (STOP), only Retry update (or a newer server) asks again. "busy" isn't a failure.
- One line in the owner's private chat per update batch: which agents, the version, what changed; once none of
  theirs is still to update, or BATCH_S after the first finished.
State is in @pm's memory only: a restart (a deploy) starts over, once.
"""
import functools
import logging
import re
import time

from common import code_report
from common import paths
from common import registry
from common import version
from server import console_update

KEY = "auto_update"                  # a person's line in agents.json: False once they switched it off
BASE_BACKOFF_S, MAX_BACKOFF_S, MAX_FAILURES = 10 * 60, 6 * 3600, 3
BUSY_AGAIN_S = 10 * 60               # it had a task running the whole wait: asked again then, or at its next report
STAGGER_S = 90                       # an owner's asks this far apart (one install, several agents)
BATCH_S = 15 * 60                    # the line waits at most this long for the owner's other agents
STOP = ("own", "not_official", "edited", "not_newer")   # self_update's refusals that a retry can't change
MARKUP_RE = re.compile(r"([*_~])")    # escaped in the chat line: never emphasis (GPT review: "_fast_")
SOON = "Updates automatically once it has nothing running."
OFFLINE = "Updates automatically when it's back online."
UPDATING = "Updating automatically: it restarts on the new version once it has nothing running."
ONCE = "Click Update once; after that it updates itself automatically."
NO_ANSWER = "It didn't finish the update."
WHY = {"unreachable": "I couldn't reach it to ask."}   # failures of @pm's own (console_update.SAYS: the agent's)
NOT_SAVED = "I couldn't save that just now; please try again."
log = logging.getLogger("loop")


@functools.cache
def summary() -> str:
    """What this server's commit changed, in one line (asked once, when first needed: git in a checkout)."""
    return version.summary(paths.ROOT)


def is_on(person: str) -> bool:
    return (registry.load().get(registry.person_id(person)) or {}).get(KEY) is not False


def state(loop) -> dict:
    """agent localpart → {"target", "fails", "next", "reason"}: its automatic asks for this server's commit."""
    return loop.__dict__.setdefault("_auto_updates", {})


def _entry(loop, agent: str) -> dict:
    st = state(loop).get(agent)
    if st is None or st["target"] != console_update.SERVER["commit"]:   # a newer server: a fresh start
        st = state(loop)[agent] = {"target": console_update.SERVER["commit"], "fails": 0, "next": 0, "reason": None}
    return st


def settle(loop, agent: str, now: float | None = None) -> None:
    """An automatic ask's outcome, once known, into the back-off (its click is then done)."""
    now = time.time() if now is None else now
    click = console_update.clicks(loop).get(agent)
    if not click or not click.get("auto") or console_update.SERVER is None:
        return
    if console_update.is_open(click, now):
        return                            # its answer may still come, or it is finishing its turns
    console_update.clicks(loop).pop(agent, None)
    st = _entry(loop, agent)
    if click["result"] == "busy":
        st.update(next=now + BUSY_AGAIN_S, reason="busy")
        return
    failed(loop, agent, "no_answer" if click["result"] in (None, "updated") else click["result"], now)


def failed(loop, agent: str, word: str, now: float) -> None:
    """One more failure: the back-off doubles; a refusal that retrying can't change stops it at once."""
    st = _entry(loop, agent)
    st["fails"] = MAX_FAILURES if word in STOP else st["fails"] + 1
    st.update(reason=word, next=now + min(BASE_BACKOFF_S * 2 ** (st["fails"] - 1), MAX_BACKOFF_S))
    log.info("%s: automatic update %s (%d so far)", agent, word, st["fails"])


def _due(loop, a: dict, now: float, online: bool | None, any_time: bool = False) -> bool:
    """`a` should be asked now (any_time: will be, once its back-off and its owner's stagger allow)."""
    code = a.get(registry.CODE)
    if (a["agent"] != registry.REMOTE or not code or not code.get(code_report.WAITS) or code["own"]
            or console_update.is_hermes(a) or version.current(code, console_update.SERVER)):
        return False
    if (online if online is not None else loop.agent_online(a["localpart"])) is not True:
        return False
    st = _entry(loop, a["localpart"])
    return (not console_update.is_open(console_update.clicks(loop).get(a["localpart"]), now) and st["fails"] < MAX_FAILURES
            and (any_time or now >= st["next"]))


async def ask_next(loop, person: str, now: float, online: dict | None = None) -> None:
    """Ask the first of `person`'s agents that is due, unless one of theirs was asked under STAGGER_S ago or
    still has an ask open (their agents may share one install: one at a time, live test 2026-11-27)."""
    if console_update.SERVER is None or not is_on(person):
        return
    agents = [a for a in registry.agents_of(person) if registry.owns(person, a["localpart"])]
    for a in agents:
        settle(loop, a["localpart"], now)
    asked = loop.__dict__.setdefault("_auto_asked", {})
    if now - asked.get(person, 0) < STAGGER_S or any(console_update.is_open(console_update.clicks(loop).get(a["localpart"]), now)
                                                      for a in agents):
        return                            # a closed click (a refusal, an old one) never blocks the rest (GPT review)
    for a in agents:
        if _due(loop, a, now, (online or {}).get(a["localpart"])):
            asked[person] = now
            if not await console_update.ask(loop, a, auto=True):
                failed(loop, a["localpart"], "unreachable", now)   # counted too: never asked without end (GPT review)
            return


async def sweep(loop, now: float | None = None) -> None:
    """Every owner with Auto-update on (pm_presence.check_agents: each CHECK_S, the first right after a start)."""
    now = time.time() if now is None else now
    for person in list(registry.load()):
        await ask_next(loop, person, now)
        await tell(loop, person, now)


async def reported(loop, agent: str | None) -> None:
    """An agent's code report (pm_kind_reports): an automatic update finished, or it may be asked now."""
    owner = registry.owner_of(agent) if agent else None
    found = registry.find(owner, agent) if owner else None
    if found is None or console_update.SERVER is None:
        return
    code, click = found.get(registry.CODE), console_update.clicks(loop).get(agent)
    if click and click.get("auto") and code and code["commit"] != click["from"] and version.current(code, console_update.SERVER):
        console_update.clicks(loop).pop(agent, None)
        state(loop).pop(agent, None)
        finished(loop, owner, registry.name_of(found))
    elif (state(loop).get(agent) or {}).get("reason") == "busy":
        state(loop)[agent]["next"] = 0       # it restarted: asked again now ("busy" waits for the next report)
    await ask_next(loop, owner, time.time(), {agent: True})   # it just reported: it's online
    await tell(loop, owner, time.time())


def finished(loop, owner: str, name: str) -> None:
    """One of `owner`'s agents runs this server's code after an automatic update: noted for the batch's line."""
    batches = loop.__dict__.setdefault("_auto_batches", {})
    batch = batches.get(owner)
    if batch is None or batch["commit"] != console_update.SERVER["commit"]:
        batch = batches[owner] = {"commit": console_update.SERVER["commit"], "names": [], "since": time.time(), "said": False}
    if name not in batch["names"]:
        batch["names"].append(name)


def plain(text: str) -> str:
    """Text for the chat line, never markup: version.UNSAFE_RE's characters out, emphasis marks escaped."""
    return MARKUP_RE.sub(r"\\\1", " ".join(version.UNSAFE_RE.sub("", text).split()))


def line(names: list[str]) -> str:
    names = [f"**{plain(n) or 'agent'}**" for n in names]
    who = f"Your agent {names[0]}" if len(names) == 1 else f"Your agents {', '.join(names[:-1])} and {names[-1]}"
    said = plain(summary())
    return f"{who} updated automatically to {version.name(console_update.SERVER)}" + (f": {said}" if said else ".")


async def tell(loop, owner: str, now: float) -> None:
    """The batch's one line, once none of the owner's agents is still to update, or BATCH_S after the first
    finished. Later ones for the same version: silent (My agents shows them Up to date)."""
    batch = (loop.__dict__.get("_auto_batches") or {}).get(owner)
    if not batch or batch["said"] or not batch["names"]:
        return
    agents = [a for a in registry.agents_of(owner) if registry.owns(owner, a["localpart"])]
    if now - batch["since"] < BATCH_S and is_on(owner) and any(console_update.is_open(console_update.clicks(loop).get(a["localpart"]), now)
                                                               or _due(loop, a, now, None, any_time=True) for a in agents):
        return                                # another of theirs is on its way: one line for both
    batch["said"] = True
    try:
        dm = await loop.private_room(owner)
        if dm:
            await loop.say(dm, line(batch["names"]))
    except RuntimeError as e:             # loop.SendError. Silent on purpose: only a courtesy line; My agents shows Up to date
        log.error("couldn't tell %s their agents updated: %s", owner, e)
