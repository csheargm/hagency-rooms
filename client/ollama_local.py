"""Ollama on this computer, for connect.py's ollama kind: is it running, which models are pulled,
and which one the agent should use (docs/adr/0008-agent-kinds.md).

Only http://localhost:11434 is asked (Ollama's own address; Codex's built-in "ollama" provider uses
the same), and only GET /api/tags. Nothing is pulled or installed.
"""
import json
import sys
import urllib.error
import urllib.request

from common import agent_kinds
from client.ask_line import ask_line

OLLAMA_URL = "http://localhost:11434"
TIMEOUT_S = 5
MAX_SHOWN = 30


class OllamaError(RuntimeError):
    pass


def installed_models(opener=urllib.request.urlopen) -> list[dict]:
    """[{"name", "cloud", "chat"}] for each model Ollama has, in its order. Raises OllamaError
    with what to do when Ollama isn't running here."""
    try:
        with opener(f"{OLLAMA_URL}/api/tags", timeout=TIMEOUT_S) as r:
            data = json.loads(r.read())
    except (OSError, ValueError) as e:        # URLError is an OSError
        raise OllamaError("Ollama isn't running on this computer (nothing answers at "
                          f"{OLLAMA_URL}): install it from ollama.com and start it, then run this again") from e
    models = data.get("models") if isinstance(data, dict) else None
    out = []
    for m in models if isinstance(models, list) else []:
        name = m.get("name") if isinstance(m, dict) else None
        if not agent_kinds.valid_model(name):
            continue                          # an odd name never reaches a prompt or argv
        caps = m.get("capabilities")
        # Codex gives the model tools (shell, edits): a model without tool calling can't be an agent.
        chat = not isinstance(caps, list) or {"completion", "tools"} <= set(caps)   # older Ollama: none listed
        out.append({"name": name, "cloud": bool(m.get("remote_host")), "chat": chat})
    return out


def describe(model: dict) -> str:
    notes = [n for n, on in (("runs on ollama.com", model["cloud"]), ("no tool calling", not model["chat"])) if on]
    return model["name"] + (f"  ({', '.join(notes)})" if notes else "")


def choose(given: str | None, models: list[dict], ask=input, tty=None) -> str:
    """The model to use: `given` when Ollama has it; else asked at the terminal; with no terminal,
    refused with what to pass. Only chat models with tool calling can be chosen."""
    usable = [m["name"] for m in models if m["chat"]]
    if given:
        if given in usable:
            return given
        raise OllamaError(f"Ollama here has no chat model with tool calling called {given!r}: "
                          f"run `ollama pull {given}` first, or choose another"
                          + (f" (it has: {', '.join(usable[:MAX_SHOWN])})" if usable else ""))
    if not usable:
        raise OllamaError("Ollama here has no chat model with tool calling yet: pull one (e.g. `ollama pull qwen3`), "
                          "then run this again")
    if not (sys.stdin.isatty() if tty is None else tty):
        raise OllamaError("which Ollama model should your agent use? Run this again with --model <name> "
                          f"(you have: {', '.join(usable[:MAX_SHOWN])})")
    shown = [m for m in models if m["chat"]][:MAX_SHOWN]
    print("Your Ollama models:")
    for i, m in enumerate(shown, 1):
        print(f"  {i}. {describe(m)}")
    while True:
        answer = ask_line(f"Which one should your agent use (1-{len(shown)})?", ask)
        if answer.isdigit() and 1 <= int(answer) <= len(shown):
            return shown[int(answer) - 1]["name"]
        if answer in usable:
            return answer
        print(f"Please type a number from 1 to {len(shown)}.")
