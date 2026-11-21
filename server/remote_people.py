"""People from other Matrix servers (e.g. @alice:matrix.org) can get an agent here too.

They never registered on this server, so @pm can't put them in Welcome: they DM @pm, or say
"@pm I want an agent" in a room @pm is in, and get the same offer (pm_welcome.py). Their agent
account is made here, named from their full Matrix ID (identity.py), and obeys only that full ID.

Limits, since anyone on any server can ask:
  - HAGENCY_ALLOW_REMOTE=0 turns it off (default on);
  - at most HAGENCY_AGENTS_PER_PERSON agents per full Matrix ID (registry.py; names derived from it);
  - at most HAGENCY_REMOTE_PER_HOUR new ones per hour, for everyone together (default 10), a
    person's further agents included;
  - @pm joins at most DM_JOINS_PER_HOUR private chats people from other servers open with it
    (DM_JOINS_PER_PERSON from any one of them), and leaves one that isn't just the two of them.
"""
import os
import time

from server.registry_removal import GONE_CREATED
from common.setup_accounts import AccountError

HOUR_S = 3600
DM_JOINS_PER_HOUR = 30
DM_JOINS_PER_PERSON = 3
FULL = "too many people from other servers asked for an agent in the last hour; please try again later"
OFF = "this server isn't making agents for people from other servers right now"


def allowed() -> bool:
    return os.environ.get("HAGENCY_ALLOW_REMOTE", "1").strip().lower() not in ("0", "off", "false", "no")


def per_hour() -> int:
    try:
        return max(0, int(os.environ.get("HAGENCY_REMOTE_PER_HOUR", "10")))
    except ValueError:                        # a typo in the setting: the default, never unlimited
        return 10


def now() -> float:
    return time.time()


def created_times(people: dict) -> list[float]:
    """When each agent in `people` (registry.load()) was made, first agents and further ones alike,
    and the removed ones too (registry_removal.GONE_CREATED)."""
    times = []
    for v in people.values():
        more = v.get("more") if isinstance(v.get("more"), list) else []
        times += [e.get("created") for e in [v, *more] if isinstance(e, dict)]
        times += v.get(GONE_CREATED) if isinstance(v.get(GONE_CREATED), list) else []
    return [t for t in times if isinstance(t, (int, float))]


def check_new(people: dict, person: str, at: float | None = None, further: bool = False) -> None:
    """Raise AccountError unless `person` (from another server) may get or keep setting up an agent
    now; `further`: one more agent besides theirs. `people` is registry.load(): each remote agent
    records when it was "created"."""
    if not allowed():
        raise AccountError(OFF)
    if person in people and not further:      # theirs already: never counted twice
        return
    at = now() if at is None else at
    if sum(t > at - HOUR_S for t in created_times(people)) >= per_hour():
        raise AccountError(FULL)


class DmJoins:
    """Private chats from other servers @pm joined in the last hour (in memory): in all, and per person."""
    def __init__(self, limit: int = DM_JOINS_PER_HOUR, each: int = DM_JOINS_PER_PERSON) -> None:
        self.limit, self.each, self.times = limit, each, []

    def take(self, sender: str, at: float | None = None) -> bool:
        at = now() if at is None else at
        self.times = [(t, s) for t, s in self.times if t > at - HOUR_S]
        if len(self.times) >= self.limit or sum(s == sender for _, s in self.times) >= self.each:
            return False
        self.times.append((at, sender))
        return True
