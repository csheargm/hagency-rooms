"""connect.py's part for each agent kind (agent_kinds.py, docs/adr/0008-agent-kinds.md): which kind
to run, what this computer still needs for it, and the kind's own settings.

  - codex / claude: as before (the CLI must be installed).
  - hermes: Hermes installed with a model and the invite patch; hermes_local.py writes its profiles.
  - ollama: Codex, and Ollama running here with the chosen model pulled (picked at the terminal
    when @pm didn't name one).
  - remote: Codex, a base_url and model, and the API key: typed here only (getpass, never echoed),
    saved in remote.env (mode 600, codex_provider.py), never sent to @pm or shown. A key is asked
    for again whenever the service's address changes, so a saved key never goes to a new address.
"""
import getpass
import os
import subprocess
import sys
from pathlib import Path

from common import agent_kinds
from adapters.agents import cli_path
from adapters.agents import codex_provider
from client import ollama_local
from client.ask_line import ask_line

INSTALL = {"codex": "install Codex first: npm i -g @openai/codex",
           "claude": "install Claude Code first: npm i -g @anthropic-ai/claude-code"}
STARTABLE = ("codex", "claude", "hermes")    # programs any kind starts
KIND_SETTINGS = ("model", "base_url")         # belong to the kind they were chosen for


def problem(kind: str) -> str | None:
    """What this computer still needs before `kind` can run here, or None."""
    if kind == "hermes":
        from client import hermes_local                   # (no bridge import: cheap)
        return hermes_local.problem()
    program = agent_kinds.program(kind)
    if not cli_path.startable(program):
        need = INSTALL[program]
        return need if kind == program else f"the {kind} kind runs through Codex: {need}"
    if kind == "ollama":
        try:
            ollama_local.installed_models()
        except ollama_local.OllamaError as e:
            return str(e)
    return None


def anything_installed() -> bool:
    return any(cli_path.startable(p) for p in STARTABLE)


def choose(flag: str | None, offered: str | None, saved: str | None) -> str | None:
    """--agent (the person's own choice here), else the kind @pm's connect info names, else the
    last one; None: none of these (connect.py then picks Codex or Claude Code as before)."""
    if flag and offered and flag != offered:
        print(f"note: @pm set this agent up as {offered}; running it as {flag}, as you asked", file=sys.stderr)
    return flag or offered or (saved if saved in agent_kinds.KINDS else None)


def _ask_until(question: str, ok, ask) -> str:
    while not ok(answer := ask_line(question, ask)):
        print("That doesn't look right; please try again.")
    return answer


def _model(kind: str, given: str | None, ask, tty: bool) -> str:
    if kind == "ollama":
        try:
            return ollama_local.choose(given, ollama_local.installed_models(), ask=ask, tty=tty)
        except ollama_local.OllamaError as e:
            raise SystemExit(f"✗ {e}") from e
    if given:
        return given
    if not tty:
        raise SystemExit("✗ which model should your agent use? Run this again with --model <name>")
    return _ask_until("Which model should your agent use (as the service names it)? ", agent_kinds.valid_model, ask)


def _base_url(given: str | None, ask, tty: bool) -> str:
    if given:
        return given.rstrip("/")
    if not tty:
        raise SystemExit("✗ which service? Run this again with --base-url https://…/v1")
    return _ask_until("The service's address (like https://api.example.com/v1): ", agent_kinds.valid_base_url, ask).rstrip("/")


def _key(folder: Path, url: str, new_key: bool, secret, tty: bool) -> None:
    """Saves the API key with its address, unless the one saved was typed for this very address."""
    try:
        have = codex_provider.load_key(folder)
    except codex_provider.ProviderError:
        have = None                           # readable by others: replaced below
    if have and have[1] == url and not new_key:
        return
    if not tty:
        raise SystemExit("✗ your agent needs the service's API key: run this in a Terminal to type it in")
    print(f"Your API key stays on this computer and is sent only to {url}.")
    while True:
        key = secret("API key (not shown as you type): ").strip()
        try:
            codex_provider.save_key(folder, key, url)
            return
        except codex_provider.ProviderError as e:
            print(e)


def pending_for(kind: str, saved: dict, offered: dict) -> dict:
    """The sign-in with `kind` and only its own settings: the last ones if it ran this kind before,
    @pm's when @pm named this kind (or none); never another kind's (GPT review)."""
    keep = saved if saved.get("agent") == kind else {k: v for k, v in saved.items() if k not in KIND_SETTINGS}
    offer = offered if offered.get("agent_kind") == kind else {}   # a model only with its kind (GPT re-check)
    return {**keep, **{k: offer[k] for k in KIND_SETTINGS if k in offer}, "agent": kind}


def settings(kind: str, pending: dict, flags, folder: Path, ask=None, secret=None, tty: bool | None = None) -> dict:
    """The credentials to save for `kind` (pending_for's, with flags first): model and base_url for
    ollama / remote, asked when missing; remote's key is saved in its own file."""
    tty = sys.stdin.isatty() if tty is None else tty
    ask, secret = ask or input, secret or getpass.getpass     # looked up now: stdin's own prompts
    clean = {k: v for k, v in pending.items() if k not in KIND_SETTINGS}
    if kind != "remote":
        codex_provider.key_path(folder).unlink(missing_ok=True)   # no key left behind for another kind
    if kind not in agent_kinds.MODEL_KINDS:
        return {**clean, "agent": kind}
    for k, check in (("model", agent_kinds.valid_model), ("base_url", agent_kinds.valid_base_url)):
        if getattr(flags, k, None) and not check(getattr(flags, k)):
            raise SystemExit(f"✗ --{k.replace('_', '-')} doesn't look right")
    pick = lambda k: getattr(flags, k, None) or pending.get(k)
    url = _base_url(pick("base_url"), ask, tty) if kind == "remote" else None
    out = {**clean, "agent": kind, "model": _model(kind, pick("model"), ask, tty), **({"base_url": url} if url else {})}
    if url:
        _key(folder, url, getattr(flags, "new_key", False), secret, tty)
    return out


def setup_hermes(app: Path, creds_path: Path, run=None) -> None:
    """Write the Hermes profiles in a process of its own: it reads the sign-in from the file."""
    r = (run or subprocess.run)([sys.executable, str(app / "hermes_local.py"), "--setup"], cwd=app,
            env={**os.environ, "HAGENCY_CREDENTIALS": str(creds_path)})
    if r.returncode != 0:
        raise SystemExit("✗ Hermes isn't set up for your agent yet (see above); your sign-in is saved: "
                         "run connect.py again (no code needed)")
