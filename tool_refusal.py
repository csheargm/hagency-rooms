"""The plain-words line under an agent's reply when Claude Code refused a tool in a chat turn.

People read it in a chat room, so it names the tool in everyday words ("Gmail", "web search") and
says what to do in one sentence; the settings file, rule syntax and exact tool names are on the
docs page it links (docs/AGENT_TOOLS.md). Only tool names reach here, never their inputs.
"""
import logging
import re

import owner_cli

log = logging.getLogger("bridge")

HOW_TO_URL = "https://github.com/csheargm/hagency-rooms/blob/master/docs/AGENT_TOOLS.md"
MAX_SHOWN = 4
BUILT_IN = {"WebSearch": "web search", "WebFetch": "web pages", "Edit": "file editing",
            "Write": "file writing", "Bash": "the command line", "Read": "file reading"}
MCP_RE = re.compile(r"^mcp__(?:claude_ai_)?(?P<server>[^_].*?)__")
# Hagency allows the first kind itself, so a setting on their computer refused it (GPT review: say
# so neutrally, the guide explains deny rules); anything else needs their OK given ahead of time.
BLOCKED = "⚠️ I wasn't allowed to use {what}: a setting on your computer blocks it. How to change it: " + HOW_TO_URL
NEEDS_OK = ("⚠️ I wasn't allowed to use {what} from this chat: it needs your OK first, and I can't ask "
            "for it here. How to allow it: " + HOW_TO_URL)


def friendly(tool: str) -> str:
    """"mcp__claude_ai_Gmail__search_threads" → "Gmail"; "WebSearch" → "web search"."""
    if tool in BUILT_IN:
        return BUILT_IN[tool]
    m = MCP_RE.match(tool)
    return m.group("server").replace("_", " ") if m else tool


def _names(tools: list[str]) -> str:
    names = list(dict.fromkeys(friendly(t) for t in tools))
    shown = names[:MAX_SHOWN] + (["more"] if len(names) > MAX_SHOWN else [])
    return shown[0] if len(shown) == 1 else ", ".join(shown[:-1]) + " or " + shown[-1]


def note(denied: tuple[str, ...]) -> str:
    """The line(s) added under the reply, or "" when nothing was refused."""
    if denied:                                # exact names for the docs page's steps (names only, checked)
        log.warning("refused tools this turn: %s", ", ".join(denied))
    blocked = [d for d in denied if d in owner_cli.CLAUDE_OWNER_TOOLS]   # allowed by us: their deny rule
    needs_ok = [d for d in denied if d not in owner_cli.CLAUDE_OWNER_TOOLS]
    lines = ([BLOCKED.format(what=_names(blocked))] if blocked else []) + \
            ([NEEDS_OK.format(what=_names(needs_ok))] if needs_ok else [])
    return "\n\n" + "\n".join(lines) if lines else ""
