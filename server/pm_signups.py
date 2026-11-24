"""The server's operator hears about each new sign-up, privately (owner, 2026-11-23: "how do we know if the new
person signed up"). OPERATOR in deploy/.env (docker-compose passes HAGENCY_OPERATOR) names that account; @pm
tells them in its private chat with them, once per person, when it first welcomes a new account of this
server. Never other servers' people, agents or bots, and never the operator about themself.
"""
import asyncio
import logging
import os
import re

from common import identity
from common.bridge_config import SERVER_NAME

log = logging.getLogger("loop")
NOTICE = "👋 New sign-up: **{who}**."
LOCALPART_RE = re.compile(r"[a-z0-9._=/+-]{1,255}")   # Matrix's own localpart characters


def operator() -> str | None:
    """The operator's full ID, or None when OPERATOR is unset or isn't a plain localpart."""
    local = os.environ.get("HAGENCY_OPERATOR", "").strip()
    return f"@{local}:{SERVER_NAME}" if local and LOCALPART_RE.fullmatch(local) else None


async def tell_operator(loop, person: str) -> None:
    """One private line to the operator about `person`; a failure is logged, never in the newcomer's way."""
    boss, (local, server) = operator(), identity.split(person)
    if not boss or person == boss or server != SERVER_NAME or local.endswith(("-agent", "-bot")):
        return
    try:
        dm = await loop.private_room(boss)
        if dm is not None:
            await loop.say(dm, NOTICE.format(who=identity.plain(person)))
    except (RuntimeError, OSError) as e:      # loop.SendError, the DM record: the sign-up itself is unaffected
        log.warning("couldn't tell the operator about %s: %s", person, e)


def notify(loop, person: str) -> None:
    """tell_operator in the background: never holding up the welcome (GPT review); a restart lets it finish."""
    job = asyncio.create_task(tell_operator(loop, person))
    loop.busy.add(job)
    job.add_done_callback(loop.busy.discard)
