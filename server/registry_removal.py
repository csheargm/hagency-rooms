"""Removing agents from agents.json (registry.py's format): one agent at a time when its person
removes it (agent_removal.remove), and everyone whose accounts here are all deactivated (prune).
Split from registry.py (R4: its size budget); behaviour unchanged."""
import time

from common import registry
from common.bridge_config import SERVER_NAME

# When removed agents were made, kept a while: remote_people's hourly cap counts them, so removing and
# adding again never makes more accounts an hour than it allows (GPT re-check, batch 5+6).
GONE_CREATED = "gone_created"
KEEP_CREATED_S = 24 * 3600           # longer than any quota window (remote_people: one hour)


def _gone_created(info: dict, agents: list[dict]) -> list[float]:
    old = info.get(GONE_CREATED) if isinstance(info.get(GONE_CREATED), list) else []
    times = [*old, *(a.get("created") for a in agents)]
    return [t for t in times if isinstance(t, (int, float)) and t > time.time() - KEEP_CREATED_S]


def forget_agent(person: str, localpart: str) -> None:
    """Drop one agent of the person's from agents.json, freeing its slot: a further one leaves "more"
    (its label is kept in "removed"); the first one only when it is their last, leaving their line
    marked registry.FIRST_GONE (GPT review, batch 5+6: else "bring my own" re-registered its closed name)."""
    with registry.LOCK:
        person, data = registry.person_id(person), registry.load()
        have = registry.agents_of(person, data)
        found = next((a for a in have if a["localpart"] == localpart), None)
        if found is None:
            raise ValueError(f"{person} has no agent {localpart}")
        if found["first"] and len(have) > 1:
            raise ValueError("remove the other agents first")
        if found["first"]:
            data[person] = _tombstone(data[person], have)
        else:
            info = data[person]
            info[registry.MORE] = [e for e in info[registry.MORE] if not (isinstance(e, dict) and e.get("label") == found["label"])]
            old = info.get(registry.REMOVED) if isinstance(info.get(registry.REMOVED), list) else []
            info[registry.REMOVED] = sorted({*old, found["label"]})
            info[GONE_CREATED] = _gone_created(info, [found])
        registry.save(data)


def _tombstone(info: dict, agents: list[dict]) -> dict:
    """What stays of a person whose agents are all gone: the names that can't come back. "remote":
    so the supervisor and setup_accounts never start or make a server-run agent for the line."""
    old = info.get(registry.REMOVED) if isinstance(info.get(registry.REMOVED), list) else []
    return {"agent": registry.REMOTE, registry.FIRST_GONE: True, GONE_CREATED: _gone_created(info, agents),
            registry.REMOVED: sorted({*old, *(a["label"] for a in agents if not a["first"])})}


def prune(dead: set[str]) -> list[str]:
    """Forget the people whose every account here is deactivated (`dead`: full IDs, from Synapse's own
    list): all their agents, and the person too when they are from this server. Someone from another
    server (whose own account may be live) keeps a registry.FIRST_GONE line, so they can add a new agent. Anyone
    with one account that isn't explicitly deactivated stays as they are. Returns whom it forgot."""
    with registry.LOCK:
        data, gone = registry.load(), []
        for p in list(data):
            agents = registry.agents_of(p, data)
            if not all(f"@{a['localpart']}:{SERVER_NAME}" in dead for a in agents):
                continue
            if p in dead:
                del data[p]
            elif registry.is_local(p) or not agents:   # a live person here, or nothing left to forget
                continue
            else:
                data[p] = _tombstone(data[p], agents)
            gone.append(p)
        if gone:
            registry.save(data)
        return gone
