"""Which AI a person's further agent runs: @pm's side of the picker in "Add an agent" (the panel and the
chat). @pm only records the choice and hands it to that agent's computer with its sign-in
(remote_agent.credentials_for: agent_kind, model, base_url, as ADR 0008's connect info); connect.py
there runs it. No kind: as before (connect.py picks the CLI it finds). A remote model's API key is
never asked for, typed or stored here: the person enters it on their own computer during Connect.
The kinds, their names and the model / address checks are agent_kinds.py's (one place, R3): this
module only reads what people say or click into that contract.
"""
import json
import re
import urllib.parse

from common import agent_kinds

# "remote" here is agent_kinds' OpenAI-compatible service (OpenRouter, Ollama Cloud, …); not agents.json's
# "agent": "remote" (runs on their computer), so the kind is kept in its own field, agent_kind.
FIELDS = agent_kinds.OFFER_KEYS                   # the names connect.py reads (ADR 0008)
SAID = {"codex": "codex", "claude": "claude", "claude code": "claude", "hermes": "hermes", "ollama": "ollama",
        "a local model": "ollama", "local model": "ollama", "local": "ollama", "remote": "remote",
        "a remote model": "remote", "remote model": "remote", "openai": "remote"}
KEY_RE = re.compile(r"\b(sk|rk|pk|gsk|xai|hf|or|nvapi)[-_][A-Za-z0-9_-]{12,}|\bbearer\s+\S{12,}", re.I)
NO_KEYS = ("That looks like an API key: never type one here, in any chat. You enter it on your own computer "
           "when you connect the agent, and it stays there. (If it was a real key, revoke it with its provider.)")
NOT_REPORTED = "AI: not reported yet"         # My agents, until the agent's computer says (kind_report.py)
HOW = ("Say which AI it runs, for example \"add another agent called pi using claude\", \"... using ollama "
       "llama3.2\" or \"... using remote https://openrouter.ai/api/v1 openai/gpt-4o\" (codex, claude, hermes, "
       "ollama [model], remote <base URL> <model>).")


def name(kind: str) -> str:
    """What people see for a kind (agent_kinds.ABOUT)."""
    return agent_kinds.ABOUT[kind]["name"]


def where(kind: str | None) -> str:
    """"On the computer where …": what that kind needs installed there (agent_kinds.ABOUT, e.g. "Ollama
    and Codex are installed"; the Connect page lists every need), or (no kind) either CLI, as before.
    Screen test: an Ollama agent's message said only "where Codex is installed"."""
    if kind not in agent_kinds.KINDS:
        return " or ".join(name(k) for k in agent_kinds.LEGACY) + " is installed"
    about = agent_kinds.ABOUT[kind]
    said = " and ".join(about["installed"]) + (" are" if len(about["installed"]) > 1 else " is") + " installed"
    return said + (f" ({about['also']})" if about.get("also") else "")


def systems(kind: str | None) -> str:
    """Where the Connect page's line runs (GPT review: Hermes's message said Windows too)."""
    return "Mac or Linux; on Windows only inside WSL" if kind in agent_kinds.NOT_ON_WINDOWS else "Mac, Windows or Linux"


LONG_RUN = 20                                 # a key-like piece: this long, letters and digits mixed,
LONGER_RUN = 32                               # or this long of either (no model name has such a word)


def looks_like_key(text: str) -> bool:
    """A key someone pasted: a known key prefix, or a long piece of mixed letters and digits (model names and
    URL paths are short words: llama3.2, openai/gpt-4o, api/v1). GPT review: plain keys had no prefix."""
    pieces = re.split(r"[/:._@+\-\s?=&]+", str(text))
    return bool(KEY_RE.search(text)) or any(len(p) >= LONGER_RUN or (len(p) >= LONG_RUN and re.search(r"[A-Za-z]", p)
                                                                      and re.search(r"\d", p)) for p in pieces)


def clean_url(text: str) -> str | None:
    """The base URL as connect.py will take it (agent_kinds.valid_base_url: https, or http on their own
    computer; no user, query or fragment), without a trailing "/"; or None."""
    url = str(text).strip().rstrip("/")
    return url if agent_kinds.valid_base_url(url) else None


def choice(kind: str, model: str = "", base_url: str = "") -> dict | str:
    """The recorded choice ({agent_kind, model?, base_url?}), or why not (a sentence)."""
    if kind not in agent_kinds.KINDS:
        return HOW
    if model and not agent_kinds.valid_model(model):
        return "A model's name is letters, digits and . : / @ + - only, for example llama3.2 or openai/gpt-4o."
    if kind == "remote":
        url = clean_url(base_url)
        if not url or not model:
            return "A remote model needs its base URL (https) and the model's name, for example https://openrouter.ai/api/v1 openai/gpt-4o."
        return {"agent_kind": kind, "model": model, "base_url": url}
    if base_url:
        return f"Only a remote model takes a base URL; {name(kind)} runs on your computer."
    if model and kind != "ollama":
        return f"{name(kind)} uses its own model settings on your computer; only Ollama and remote models take a model name here."
    return {"agent_kind": kind, **({"model": model} if model else {})}


def from_words(text: str) -> dict | str:
    """"claude", "ollama llama3.2", "remote https://… openai/gpt-4o" → the choice, or why not."""
    if looks_like_key(text):
        return NO_KEYS
    words = text.strip().rstrip(".!").split()
    for size in (3, 2, 1):                   # the longest name first: "a local model", "claude code"
        said = " ".join(words[:size]).lower()
        if said in SAID:
            rest = words[size:]
            kind = SAID[said]
            if kind == "remote":
                return choice(kind, rest[1] if len(rest) > 1 else "", rest[0] if rest else "") if len(rest) <= 2 else HOW
            return choice(kind, rest[0] if rest else "") if len(rest) <= 1 else HOW
    return HOW


def from_panel(value: str) -> tuple[str, dict | str | None]:
    """The panel's Add an agent: a plain name (no kind), or JSON {name, agent_kind, model, base_url}."""
    if not value.lstrip().startswith("{"):
        return value.strip(), None
    try:
        data = json.loads(value)
    except ValueError:
        return "", HOW
    if not isinstance(data, dict):
        return "", HOW
    text = {k: data.get(k) if isinstance(data.get(k), str) else "" for k in ("name", *FIELDS)}
    if set(data) - set(text) or any(looks_like_key(v) for v in text.values()):   # an api_key field included
        return text["name"].strip(), NO_KEYS
    if not text["agent_kind"]:
        return text["name"].strip(), None
    return text["name"].strip(), choice(text["agent_kind"], text["model"].strip(), text["base_url"].strip())


REPORT_KEYS = ("agent_kind", "model", "host")   # what an agent's computer reports (kind_report.py); never a key


def host_url(host: str) -> str:
    return "https://" + (f"[{host}]" if ":" in host else host)   # an IPv6 address in brackets


def valid_host(host: object) -> bool:
    """A bare host name (or IP) as agent_kinds.valid_base_url takes it: no scheme, port, user, path or query."""
    if not (isinstance(host, str) and host):
        return False
    return agent_kinds.valid_base_url(host_url(host)) and urllib.parse.urlsplit(host_url(host)).hostname == host.lower()


def reported(content: object) -> dict | None:
    """What an agent's computer says it runs, as @pm keeps it ({agent_kind, model?, host?}), or None: not a
    dict, a field other than REPORT_KEYS (an api_key), a non-string or key-like value, or a kind, model or
    host that choice / agent_kinds refuse. An Ollama model is from that computer's installed list, so only a
    remote model is checked for looking like a key (GPT review: long local names such as Qwen3CoderNextInstruct)."""
    if not isinstance(content, dict) or not set(content) <= set(REPORT_KEYS) or not all(isinstance(v, str) for v in content.values()):
        return None
    kind, model, host = (content.get(k, "") for k in REPORT_KEYS)
    if kind not in agent_kinds.KINDS or (host and not valid_host(host)) or looks_like_key(host):
        return None
    if kind == "remote" and looks_like_key(model):
        return None
    picked = choice(kind, model, host_url(host) if host else "")
    if not isinstance(picked, dict):
        return None
    return {"agent_kind": kind, **({"model": model} if model else {}), **({"host": host.lower()} if host else {})}


def describe(agent: dict) -> str:
    """What My agents shows: "Claude Code", "Local model (Ollama): llama3.2", "Remote: openai/gpt-4o at openrouter.ai".
    What its computer reported (agent["reported"]: reported()) wins over what was picked for it."""
    shown = agent.get("reported") or agent
    kind = shown.get("agent_kind")
    if kind not in agent_kinds.KINDS:
        return ""
    if kind == "remote":
        host = shown.get("host") or urllib.parse.urlsplit(shown.get("base_url") or "").hostname or "?"
        return f"Remote: {shown.get('model') or '?'} at {host}"
    return name(kind) + (f": {shown['model']}" if shown.get("model") else "")
