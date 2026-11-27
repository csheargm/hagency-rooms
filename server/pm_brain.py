"""@pm's understanding step: what does this person want, given the conversation so far?

@pm stays a coordinator: the model only *interprets*. It gets the room's recent conversation,
the message being replied to, what is waiting for approval, the plan and the status board, and
answers with ONE action from a fixed list plus a short sentence for the room. Code then checks
who may do what (pm_chat.py), so wording can't produce anything outside the list, and "stop"
never waits for a model. The model runs as Claude Code with NO tools at all (it can't read a file
or run anything: an injected "print ~/.ssh/…" has nothing to print), no MCP, no project settings,
in an empty folder with a minimal environment. If it fails or is slow, @pm uses its phrase rules
(intents.py). PM_BRAIN=codex is possible but weaker: Codex's read-only sandbox can still read files.
PM_BRAIN=owner asks the room owner's own agent instead (pm_owner_brain.py, ADR 0022), with the same
prompt and parser (common/pm_actions.py).
"""
import asyncio
import logging
import os

from adapters.agents import agents
from common import lifecycle
from common.bridge_config import ROOT
from common.pm_actions import ACTIONS, MAX_REPLY, parse, prompt  # noqa: F401 - shared with the owner's agent (R3)

BRAIN = os.environ.get("PM_BRAIN", "claude")   # claude (no tools) | codex | owner (pm_owner_brain.py) | off
WORKDIR = ROOT / "rooms" / "pm-brain"
TIMEOUT_S = float(os.environ.get("PM_BRAIN_TIMEOUT_S", "45"))
log = logging.getLogger("loop")
ABOUT = ("I'm the Product Manager: a coordinator{how} I turn your discussion into work and keep it moving; "
         "the thinking and building are done by your own agents, on their owners' models and accounts.")
HOW = {"owner": ", with no AI model of my own. To understand requests, I share recent messages here with the room "
                "owner's agent, on their own model and account, including messages that may not be for me (when "
                "it's offline, I go by simple phrases).",
       "off": ", with no AI model of my own. I go by simple phrases.",
       "host": ". A model on this server only reads what you ask me."}


def about() -> str:
    """What @pm says about itself, true for how this server runs its understanding step."""
    return ABOUT.format(how=HOW.get(BRAIN, HOW["host"]))


async def run_model(text: str) -> str:
    """One answer from the model, with no tools and nothing of the host's but the login."""
    if BRAIN == "codex":
        return await agents.run_turn("codex", WORKDIR, text, fresh=True)
    if BRAIN != "claude":
        raise agents.AgentError(f"unknown PM_BRAIN {BRAIN!r}")
    WORKDIR.mkdir(parents=True, exist_ok=True)
    env = {k: os.environ[k] for k in ("PATH", "HOME", "USER", "LANG", "TMPDIR") if k in os.environ}
    proc = await asyncio.create_subprocess_exec(
        "claude", "-p", "--output-format", "json", "--tools", "", "--strict-mcp-config", "--setting-sources", "user",
        cwd=WORKDIR, env=env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, _ = await lifecycle.communicate(proc, text.encode(), TIMEOUT_S)
    return agents.BACKENDS["claude"][1](out.decode(errors="replace")).text


async def understand(ctx: dict) -> tuple[str, str] | None:
    """(action, reply) from the model, or None (off, failed, slow, or nonsense): then the rules."""
    if BRAIN == "off":
        return None
    try:
        out = await asyncio.wait_for(run_model(prompt(ctx)), TIMEOUT_S)
    except (agents.AgentError, asyncio.TimeoutError, OSError, ValueError) as e:
        log.warning("@pm's understanding step failed, using the phrase rules: %s", e)
        return None
    decision = parse(out)
    if decision is None:
        log.warning("@pm's understanding step gave no usable answer, using the phrase rules")
    return decision
