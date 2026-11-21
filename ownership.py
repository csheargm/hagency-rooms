"""Whose agent is whose, in a room: an agent only counts (gets work, may report results, may be
picked as reviewer) while its registered owner is in the room too, and a reviewer must belong to
a DIFFERENT person than the builder. The mapping comes from registry.py (onboarding)."""
from nio import MatrixRoom

import registry
from bridge_config import SERVER_NAME


def split(user_id: str) -> tuple[str, str]:
    local, _, server = user_id.lstrip("@").partition(":")
    return local, server


def joined(room: MatrixRoom, user_id: str) -> bool:
    return user_id in room.users and user_id not in room.invited_users


def owner_id(agent_id: str) -> str | None:
    """The owner's full ID (any server) for a registered agent on this server, else None."""
    local, server = split(agent_id)
    return registry.owner_of(local) if server == SERVER_NAME else None


def agent_id(person_id: str) -> str:
    """The person's agent account: always on this server, whichever server the person is on."""
    return f"@{registry.agent_of(person_id)}:{SERVER_NAME}"


def own_agent_id(person_id: str) -> str | None:
    """The person's agent account, only if it is registered to exactly them (by full ID)."""
    agent = agent_id(person_id)
    return agent if registry.owns(person_id, split(agent)[0]) else None


def agents_in(room: MatrixRoom) -> list[str]:
    """Registered agents in the room whose owner is in the room too."""
    return sorted(u for u in room.users if joined(room, u) and (o := owner_id(u)) and joined(room, o))
