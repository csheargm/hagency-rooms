"""Who builds and who checks: @pm's task routing (loop.py). A person may have several agents (ADR
0005): any agent of theirs that is here and not known to be offline may build, but the checker
(review, verify) must belong to a DIFFERENT PERSON, decided by the owners' full Matrix IDs, never by
agent names: two agents of one person never check each other's work. Pure functions."""
from typing import Callable


def localpart(user_id: str) -> str:
    return user_id.split(":")[0].lstrip("@")


def available(agents: list[str], online: Callable[[str], bool | None]) -> list[str]:
    """The agents not known to be offline (None: never checked, e.g. one this server runs)."""
    return [a for a in agents if online(localpart(a)) is not False]


def checkers_for(agents: list[str], builder: str, owner_of: Callable[[str], str | None]) -> list[str]:
    """Agents that may check `builder`'s work: registered to a person other than the builder's owner.
    An agent without a registered owner (or a builder without one) never qualifies."""
    mine = owner_of(builder)
    if not mine:
        return []
    return [a for a in agents if a != builder and (theirs := owner_of(a)) and theirs != mine]
