"""Codex with another model provider: how the ollama and remote kinds run (docs/adr/0008-agent-kinds.md).

Both are `codex exec` exactly as for the codex kind (the owner's config and powers, ADR 0006, or the
lockdown for locked turns), plus `-c` overrides that pick the provider and `-m <model>`:
  - ollama: Codex's built-in "ollama" provider (http://localhost:11434/v1, Responses API).
  - remote: our own provider entry with the person's base_url, wire_api "responses", and
    env_key HAGENCY_REMOTE_API_KEY. The key goes to Codex ONLY in that environment variable: never
    in argv, a log or a config file of Codex's; Codex's own shells get it blanked
    (shell_environment_policy.set), so a command the model runs can't print it, and it is cut out
    of every reply and error (scrub).
The key lives in remote.env (mode 600) beside the agent's credentials.json, written by connect.py
from a getpass prompt, TOGETHER with the address it was typed for: a turn whose base_url isn't that
address gets no key (GPT review: a reconnect could otherwise send a new key to an old address).
Model and base_url come from the credentials file (checked again here).
"""
import hashlib
import json
import os
import tempfile
from pathlib import Path

from common import agent_kinds

KEY_ENV = "HAGENCY_REMOTE_API_KEY"
URL_ENV = "HAGENCY_REMOTE_BASE_URL"          # in remote.env only: the address the key belongs to
KEY_FILE = "remote.env"
PROVIDER_ID = "hagency_remote"               # a TOML bare key; Codex reserves openai, ollama, lmstudio...
OLLAMA_PROVIDER = "ollama"
MAX_KEY_CHARS = 4096


class ProviderError(RuntimeError):
    pass


def valid_key(key: object) -> bool:
    """A single line of printable characters (it goes into an env file and an HTTP header)."""
    return (isinstance(key, str) and 0 < len(key) <= MAX_KEY_CHARS and key.isprintable()
            and not any(c.isspace() for c in key))


def key_path(folder: Path) -> Path:
    return folder / KEY_FILE


def save_key(folder: Path, key: str, base_url: str) -> Path:
    """The key and its address in one 0600 file (a new temp file: no symlink games), swapped in whole."""
    if not valid_key(key):
        raise ProviderError("that API key has spaces or odd characters in it; paste it again")
    if not agent_kinds.valid_base_url(base_url):
        raise ProviderError("the service's address doesn't look right")
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".remote-")
    with os.fdopen(fd, "w") as f:
        f.write(f"{URL_ENV}={base_url}\n{KEY_ENV}={key}\n")
    path = key_path(folder)
    os.replace(tmp, path)
    return path


def load_key(folder: Path) -> tuple[str, str] | None:
    """(key, the address it belongs to), or None when there is none. Raises ProviderError when
    others can read the file (then it's not used)."""
    path = key_path(folder)
    try:
        if os.name != "nt" and path.stat().st_mode & 0o077:
            raise ProviderError(f"{path} can be read by others; run connect.py again to save the key afresh")
        text = path.read_text()
    except FileNotFoundError:
        return None
    found = {}
    for line in text.splitlines():
        name, sep, value = line.partition("=")
        if sep:
            found[name.strip()] = value.strip()
    key, url = found.get(KEY_ENV), found.get(URL_ENV)
    return (key, url) if valid_key(key) and agent_kinds.valid_base_url(url) else None


def _setting(creds: dict, key: str, check) -> str:
    value = creds.get(key)
    if not check(value):
        raise ProviderError(f"no usable {key} saved for this agent; run connect.py again")
    return value


def args(kind: str, creds: dict) -> list[str]:
    """The `codex exec` options that point it at this kind's provider and model (no secret)."""
    model = _setting(creds, "model", agent_kinds.valid_model)
    if kind == "ollama":
        return ["-c", f'model_provider="{OLLAMA_PROVIDER}"', "-m", model]
    if kind != "remote":
        raise ProviderError(f"{kind} isn't run through a Codex model provider")
    url = _setting(creds, "base_url", agent_kinds.valid_base_url)
    p = f"model_providers.{PROVIDER_ID}"
    return ["-c", f'model_provider="{PROVIDER_ID}"', "-c", f'{p}.name="Hagency remote"',
            "-c", f'{p}.base_url="{url}"', "-c", f'{p}.env_key="{KEY_ENV}"', "-c", f'{p}.wire_api="responses"',
            "-c", f'shell_environment_policy.set.{KEY_ENV}=""', "-m", model]


def env(kind: str, creds_path: str | None, creds: dict) -> dict:
    """What the Codex process needs in its environment for this kind: for remote, the key, only
    when it was typed for this very base_url."""
    if kind != "remote":
        return {}
    saved = load_key(Path(creds_path).parent) if creds_path else None
    if not saved:
        raise ProviderError("no API key saved for the remote service; run connect.py again to enter it")
    key, url = saved
    if url != creds.get("base_url"):
        raise ProviderError("the saved API key belongs to another service address; run connect.py again")
    return {KEY_ENV: key}


def session_tag(kind: str, creds: dict) -> str:
    """Saved sessions are per service address: a conversation never resumes at another provider."""
    url = creds.get("base_url")
    return ":" + hashlib.sha256(url.encode()).hexdigest()[:12] if kind == "remote" and isinstance(url, str) else ""


def scrub(text: str, extra_env: dict) -> str:
    """A reply or error with the key (should the service or the CLI ever echo it) cut out."""
    key = extra_env.get(KEY_ENV)
    if not key:
        return text
    for spelling in (key, json.dumps(key)[1:-1]):   # also as a JSON error would spell it (GPT re-check)
        text = text.replace(spelling, "***")
    return text
