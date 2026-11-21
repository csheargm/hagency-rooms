"""The kinds of agent a person can run on their own computer, and how the connect info names one
(docs/adr/0008-agent-kinds.md). Pure data and checks: the @pm side, the Connect page and connect.py
all read the list from here, so a kind is named in exactly one place.

The connect info (POST /connect's answer) may carry, besides the sign-in:
  "agent_kind": one of KINDS          absent: today's behaviour (Codex or Claude Code, picked here)
  "model":      a model name          ollama and remote only (MODEL_RE)
  "base_url":   an OpenAI-compatible  remote only: https, or http to this computer (no user:pass@,
                endpoint              no query or fragment)
Never an API key: a remote endpoint's key is typed only on the person's own computer.
"""
import re
import urllib.parse

KINDS = ("codex", "claude", "hermes", "ollama", "remote")
LEGACY = ("codex", "claude")                 # what an agent with no kind runs, as before
MODEL_KINDS = ("ollama", "remote")           # these need a model; remote also a base_url and a key
OFFER_KEYS = ("agent_kind", "model", "base_url")   # what the connect info may add to the sign-in
NOT_ON_WINDOWS = ("hermes",)                 # Hermes runs on macOS / Linux (on Windows only inside WSL)
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}$")   # qwen3:8b, hf.co/org/m:Q4_K_M, gpt-4.1
MAX_URL = 300
URL_CHARS_RE = re.compile(r"^[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+$")   # no quotes, spaces or backslashes

# What each kind is and needs on the person's computer: the Connect page's checklist, @pm's picker;
# "installed" (and "also") for @pm's Connect message: "On the computer where Ollama and Codex are installed".
ABOUT = {
    "codex": {"name": "Codex", "program": "codex", "installed": ["Codex"],
              "needs": ["Codex, installed and signed in (npm i -g @openai/codex)"]},
    "claude": {"name": "Claude Code", "program": "claude", "installed": ["Claude Code"],
               "needs": ["Claude Code, installed and signed in (npm i -g @anthropic-ai/claude-code)"]},
    "hermes": {"name": "Hermes", "program": "hermes", "installed": ["Hermes"],
               "also": "with Hagency's Matrix invite patch applied: the Connect page lists it",
               "needs": ["Hermes Agent, with a model set up (hermes model)",
                         "Hagency's Matrix invite patch applied to Hermes (docs/hermes-matrix-invite-allowlist.patch)",
                         "macOS or Linux (on Windows: inside WSL)"]},
    "ollama": {"name": "Ollama (a model on your computer)", "program": "codex", "installed": ["Ollama", "Codex"],
               "needs": ["Ollama, open and running, with a model downloaded that can use tools (ollama.com)",
                         "Codex (npm i -g @openai/codex): it runs the model as your agent; no OpenAI account needed"]},
    "remote": {"name": "Any OpenAI-compatible service", "program": "codex", "installed": ["Codex"],
               "also": "have the service's API key at hand: you type it there, never in a chat",
               "needs": ["The service's address and your API key for it (you type the key on this computer only)",
                         "A service that supports the OpenAI Responses API (/v1/responses)",
                         "Codex (npm i -g @openai/codex): it runs the model as your agent; no OpenAI account needed"]},
}


# Every kind in a few words, for welcome texts (pm_welcome, pm_first, Element's home page: a test keeps
# deploy/element_page/home.html saying exactly this).
IN_WORDS = "Codex, Claude Code, Hermes, a local Ollama model or a remote model"


class BadConnectInfo(ValueError):
    """The connect info names a kind, model or address this computer won't use."""


def kinds() -> list[dict]:
    """Every kind, in order, with its name and what it needs: what @pm and the Connect page show."""
    return [{"kind": k, "windows": k not in NOT_ON_WINDOWS, **ABOUT[k]} for k in KINDS]


def program(kind: str) -> str:
    """The CLI a kind runs (ollama and remote are Codex with another model provider)."""
    return ABOUT[kind]["program"]


def valid_model(model: object) -> bool:
    return isinstance(model, str) and bool(MODEL_RE.fullmatch(model)) and ".." not in model   # fullmatch: no "\n"


def valid_base_url(url: object) -> bool:
    """https, or plain http only to this computer (the API key travels over it); nothing that could
    break out of a TOML string or carry a password in the address."""
    if not (isinstance(url, str) and len(url) <= MAX_URL and URL_CHARS_RE.fullmatch(url)):
        return False
    try:
        parts = urllib.parse.urlsplit(url)    # malformed brackets raise here
        parts.port                            # a malformed port raises
    except ValueError:
        return False
    if parts.username is not None or parts.password is not None or parts.query or parts.fragment:
        return False
    return bool(parts.hostname) and (parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in LOCAL_HOSTS))


def from_connect_info(info: dict) -> dict:
    """{"agent_kind", "model", "base_url"}, each only when the connect info has it; raises
    BadConnectInfo for a value that is there but wrong (never silently dropped: the person would
    get another agent than the one they chose)."""
    out = {}
    kind = info.get("agent_kind")
    if kind is not None:
        if kind not in KINDS:
            raise BadConnectInfo(f"this computer's Hagency doesn't know the agent kind {str(kind)[:40]!r}; "
                                 "run the connect line again to update it")
        out["agent_kind"] = kind
    if info.get("model") is not None:
        if not valid_model(info["model"]):
            raise BadConnectInfo("the model name from @pm doesn't look like one")
        out["model"] = info["model"]
    if info.get("base_url") is not None:
        if not valid_base_url(info["base_url"]):
            raise BadConnectInfo("the service address from @pm must be https (plain http only to this computer)")
        out["base_url"] = info["base_url"].rstrip("/")
    return out
