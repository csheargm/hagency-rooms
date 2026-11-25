"""The "card" task (ADR 0019): make, or change, one Hagency panel card for this agent's owner.

A read-only turn in its own empty folder: the agent writes nothing and runs nothing; its answer is one
HTML document (common/cards.py), which @pm checks again before the panel shows it.
"""
from pathlib import Path

from adapters.agents import agents
from common import cards


async def card(agent: str, backend: str, base: Path, task: dict) -> dict:
    request = cards.clean_request(task.get("request", ""))
    if request is None:
        raise cards.CardError("the request was empty or too long")
    now_html = task.get("html") if isinstance(task.get("html"), str) else ""
    title, html = cards.parse_reply(await agents.run_turn(backend, base / "card", cards.card_prompt(request, now_html),
                                                          fresh=True))
    return {"kind": "card", "title": title, "html": html, "card": task.get("card") or "",
            "line": f"panel card ready: {title}"}
