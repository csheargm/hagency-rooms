"""Who brought which agent: agents.json (git-ignored), written by onboard.py and remote_agent.py,
read by the supervisor and setup. One line per person, keyed by their FULL Matrix ID, e.g.
{"@anna:this.server": {"agent": "hermes", "dm": "!room:server"}, "@alice:matrix.org": {"agent": "remote"}}.
Older files keyed by bare names ("anna") mean people on this server: load() reads them as full IDs.

A person may have several agents (ADR 0005). The line's own fields are their FIRST agent, as they
always were (so every older file and writer stays valid: nothing to migrate on disk); further ones
are listed under "more", each {"label", "localpart", "agent" (kind), "created", "dm", "follow"}.
agents_of() gives all of them as one list, the first first. Once the first agent is removed for good,
the line stays with "first_removed" (its derived account name can never be registered again), so
further agents, with new names, are all the person can add."""
import json
import os
import re
import threading
import time
from pathlib import Path

from common import pm_kinds
from common import bridge_config
from common import identity
from common.bridge_config import SERVER_NAME
from common import paths

PATH = Path(os.environ.get("HAGENCY_AGENTS", paths.ROOT / "agents.json"))
DEFAULT = {"anna": {}, "betty": {}}
NAME_RE = identity.NAME_RE          # a person here who can have an agent (anna, newbie1)
REMOTE = "remote"                    # their agent runs on their own computer (connect.py), not here
FIRST_LABEL = "main"                 # what the first agent is called when its person never named it
MORE = "more"                        # a person's further agents, in their line
FIRST_GONE = "first_removed"         # their first agent was removed for good: only named ones from now on
REPORTED = "reported"                # the AI its computer says it runs (pm_kind_reports.py)
PER_PERSON_DEFAULT = 3
LOCALPART_RE = re.compile(r"[a-z0-9._=-]{1,120}")   # a recorded agent account name (Matrix localpart characters)
LOCK = threading.RLock()             # @pm's read-modify-writes of this file (its loop and set-up threads)


def load() -> dict:
    try:
        data = json.loads(PATH.read_text())
    except (OSError, ValueError):
        data = DEFAULT                    # not onboarded yet (or a damaged file): the two demo people
    if not isinstance(data, dict):
        data = DEFAULT                    # a saved {} (everyone removed or pruned) stays empty (GPT review)
    # Keep only well-formed entries: a damaged line must never crash @pm or confuse ownership. An old
    # bare key and its full ID together: merged, the full ID's fields winning.
    people = {}
    for p, v in sorted(data.items(), key=lambda kv: str(kv[0]).startswith("@")):
        if isinstance(p, str) and p and (p.startswith("@") or ":" not in p):
            people[person_id(p)] = {**people.get(person_id(p), {}), **(v if isinstance(v, dict) else {})}
    return people


def person_id(who: str) -> str:
    """A full Matrix ID; a bare name is a person on this server."""
    return identity.person_id(who, SERVER_NAME)


def is_local(person: str) -> bool:
    return identity.is_local(person_id(person), SERVER_NAME)


def local_name(person: str) -> str | None:
    """The bare name of a person on this server (what the supervisor and Hermes run by), else None."""
    return identity.split(person_id(person))[0] if is_local(person) else None


def save(data: dict) -> None:
    tmp = PATH.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")   # @pm and onboard.py both write
    tmp.write_text(json.dumps(data, indent=1))
    os.replace(tmp, PATH)


# --- ownership: this server created every agent account (onboarding, or @pm for "bring your own",
# also for people from other servers), so this mapping is authoritative here. (Agent accounts on
# other servers would need a two-sided claim: the owner names the agent AND the agent names the
# owner. Future work.)
FOLLOW = ("always", "ask", "never")


def agent_of(person: str) -> str:
    """The localpart of `person`'s agent on this server (identity.agent_localpart unless recorded)."""
    person, people = person_id(person), load()
    if first_removed(person, people) and agents_of(person, people):   # their first was removed: their first live one
        return agents_of(person, people)[0]["localpart"]
    return people.get(person, {}).get("agent_user") or identity.agent_localpart(person, SERVER_NAME)


def owner_of(agent: str) -> str | None:
    """The full ID of the person whose agent `agent` (a localpart here) is, or None if it isn't a
    registered agent. Compare it with a full ID: never a bare name taken from another server."""
    found = claimants(agent)
    mine = bridge_config.CREDENTIALS          # a person's own computer: no agents.json, just connect.py's file
    if not found and mine and mine["user_id"] == f"@{agent}:{SERVER_NAME}":
        return person_id(mine["owner"])
    return found[0] if len(found) == 1 else None   # claimed twice (a bad record): nobody's, never the first


def claimants(agent: str) -> list[str]:
    """Everyone whose record names agent localpart `agent`, among all their agents (more than one: a bad record)."""
    people = load()
    return [p for p, v in people.items() if any(a["localpart"] == agent for a in _agents(p, v))]


def owns(person: str, agent: str) -> bool:
    """`person` (a full ID, or a bare name here) is the registered owner of agent localpart `agent`."""
    return owner_of(agent) == person_id(person)


def follow(person: str, agent: str | None = None) -> str:
    """Bring this agent of the person's (default: their first) along when they join a project room:
    always, ask, never. The first agent always comes by default; further ones only when invited."""
    found = find(person, agent) if agent else None
    if found:
        return found["follow"]
    value = load().get(person_id(person), {}).get("follow", "always")
    return value if value in FOLLOW else "always"


def set_follow(person: str, value: str, agent: str | None = None) -> None:
    if value not in FOLLOW:
        raise ValueError(f"follow must be one of {FOLLOW}")
    update(person, agent, follow=value)


def from_elsewhere(people: dict) -> dict:
    """The people (registry.load()) from other servers: whom the hourly cap is for."""
    return {p: v for p, v in people.items() if not is_local(p)}


# --- several agents per person ---
def per_person() -> int:
    """HAGENCY_AGENTS_PER_PERSON: how many agents one person may have (a typo: the default, never unlimited)."""
    try:
        return max(1, int(os.environ.get("HAGENCY_AGENTS_PER_PERSON", str(PER_PERSON_DEFAULT))))
    except ValueError:
        return PER_PERSON_DEFAULT


def clean_label(text: str) -> str | None:
    """A label as a person types it ("My Laptop") made into one (mylaptop), or None if nothing fits."""
    word = re.sub(r"[^a-z0-9]", "", str(text).lower()).lstrip("0123456789")[:9]
    return word if identity.LABEL_RE.fullmatch(word) else None


def _text(value, ok=lambda v: True) -> str | None:
    """A field of agents.json only if it is a string that passes `ok` (a damaged file: ignored)."""
    return value if isinstance(value, str) and value and ok(value) else None


def _agents(person: str, info: dict) -> list[dict]:
    first = {"label": _text(info.get("label"), identity.LABEL_RE.fullmatch) or FIRST_LABEL, "first": True,
             "agent": _text(info.get("agent")) or "server", "created": info.get("created"), "dm": _text(info.get("dm")),
             "localpart": _text(info.get("agent_user"), LOCALPART_RE.fullmatch) or identity.agent_localpart(person, SERVER_NAME),
             "ready": info.get("ready") is True,
             "follow": info.get("follow") if info.get("follow") in FOLLOW else "always",
             "disconnected": info.get("disconnected"), "display": _text(info.get("display")),   # pm_rename.py
             "stopped": info.get("stopped") is True,   # pm_stop.py
             **_picked(info)}                 # the AI it runs (pm_first.py), as a further agent's
    found, labels = ([], set()) if info.get(FIRST_GONE) is True else ([first], {first["label"]})
    for e in info.get(MORE) if isinstance(info.get(MORE), list) else []:
        label = e.get("label") if isinstance(e, dict) else None
        if not isinstance(label, str) or not identity.LABEL_RE.fullmatch(label) or label in labels:
            continue                          # a damaged or repeated entry: never an agent
        labels.add(label)
        found.append({"label": label, "agent": _text(e.get("agent")) or REMOTE, "first": False, "created": e.get("created"),
                      "localpart": identity.agent_localpart(person, SERVER_NAME, label), "dm": _text(e.get("dm")),
                      "follow": e.get("follow") if e.get("follow") in FOLLOW else "never",
                      "disconnected": e.get("disconnected"), "ready": e.get("ready") is True, "stopped": e.get("stopped") is True,
                      "display": _text(e.get("display")), **_picked(e)})
    return found


def _picked(entry: dict) -> dict:
    """The AI an agent runs (pm_kinds.py), as picked, and as its computer reported it ("reported":
    pm_kind_reports.py, shown only, never offered back); a damaged record: none."""
    found = pm_kinds.choice(*(entry.get(k) if isinstance(entry.get(k), str) else "" for k in pm_kinds.FIELDS))
    said = pm_kinds.reported(entry.get(REPORTED))
    return {**(found if isinstance(found, dict) else {}), **({REPORTED: said} if said else {})}


def name_of(agent: dict) -> str:
    """What its person calls it: the name they gave (its label is then only the account's slot)."""
    return agent.get("display") or agent["label"]


def called(agent: dict, word: str) -> bool:
    """`word` names this agent: its account, its slot or the name its person gave it (GPT review: every
    lookup by a typed name uses this one rule, and no two of a person's agents share any of them)."""
    return str(word).lower() in (agent["localpart"], agent["label"], name_of(agent).lower())


def agents_of(person: str, people: dict | None = None) -> list[dict]:
    """All of a person's agents, their first one first; [] if they have none."""
    person = person_id(person)
    people = load() if people is None else people
    return _agents(person, people[person]) if person in people else []


def first_removed(person: str, people: dict | None = None) -> bool:
    """Their first agent was removed for good: a new one gets a new name (a further agent's)."""
    people = load() if people is None else people
    return (people.get(person_id(person)) or {}).get(FIRST_GONE) is True


def may_add_more(person: str) -> bool:
    """`person` may add a named agent: their first one is theirs, or it was removed for good."""
    person, people = person_id(person), load()
    have = agents_of(person, people)
    return first_removed(person, people) or bool(have) and owns(person, have[0]["localpart"])


def find(person: str, agent: str | None) -> dict | None:
    """The person's agent with localpart (or label) `agent`, or their first when None."""
    found = agents_of(person)
    if agent is None:
        return found[0] if found else None
    return (next((a for a in found if agent in (a["localpart"], a["label"])), None)   # its own names first
            or next((a for a in found if a.get("display") and a["display"].lower() == agent.lower()), None))


def update(person: str, which: str | None, **fields) -> None:
    """Change fields of one of the person's agents, `which` by localpart or label (None or unknown:
    the line's own fields, their first agent)."""
    with LOCK:
        _update(person_id(person), which, fields)


def _update(person: str, which: str | None, fields: dict) -> None:
    data = load()
    info = data.setdefault(person, {})
    found = find(person, which) if which is not None else None
    if which is not None and found is None:
        raise ValueError(f"{person} has no agent {which}")   # never the first agent's fields by mistake
    if found and not found["first"]:
        more = info[MORE]
        more[next(i for i, e in enumerate(more) if isinstance(e, dict) and e.get("label") == found["label"])].update(fields)
    else:
        info.update(fields)
    save(data)


def add_agent(person: str, label: str, kind: str = REMOTE, picked: dict | None = None) -> dict:
    """Reserve a further agent for `person` (counted before its account exists, so a failed set-up
    can't dodge the cap), called `label` by them; picked: the AI it runs (pm_kinds.choice).
    Raises ValueError with a sentence to show them."""
    with LOCK:
        return _add_agent(person_id(person), label, kind, picked or {})


def free_label(person: str, word: str, data: dict) -> str:
    """The account slot for a new agent the person calls `word`: `word` itself, or (that name's account
    was closed for good, or is someone else's) word2, word3 … so a removed name can be used again."""
    have = agents_of(person, data)            # never another agent's slot or given name (GPT re-check)
    gone = set(data[person].get(REMOVED) or []) | {a["label"] for a in have} | {name_of(a).lower() for a in have}
    for n in range(1, 100):
        label = word if n == 1 else f"{word[:identity.LABEL_MAX - len(str(n))]}{n}"
        if label not in gone and not claimants(identity.agent_localpart(person, SERVER_NAME, label)):
            return label
    raise ValueError(f"I couldn't find a free account name for {word}; please choose another name")


def _add_agent(person: str, label: str, kind: str, picked: dict) -> dict:
    data = load()
    have = agents_of(person, data)
    if not have and not first_removed(person, data):
        raise ValueError("you don't have an agent here yet: get your first one with 🔌 Bring your own")
    if not isinstance(label, str) or not identity.LABEL_RE.fullmatch(label):
        raise ValueError("an agent's name is one short word: letters and digits, up to 9, starting with a letter")
    if any(called(a, label) for a in have):   # names are unique among their agents NOW only
        raise ValueError(f"you already have an agent called {label}")
    if len(have) >= per_person():
        raise ValueError(f"you have {len(have)} agents, the most one person can have here")
    slot = free_label(person, label, data)    # a closed account's name never comes back: a new slot
    if not isinstance(data[person].get(MORE), list):
        data[person][MORE] = []               # a damaged list: _agents() never read it anyway
    data[person][MORE].append({"label": slot, "agent": kind, "created": time.time(), **picked,
                               **({"display": label} if slot != label else {})})
    save(data)
    return find(person, slot)


# --- removing agents (forget_agent, prune: registry_removal.py) ---
REMOVED = "removed"                  # labels of further agents removed for good (their accounts can't come back)
