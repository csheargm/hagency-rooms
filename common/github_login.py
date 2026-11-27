"""An agent's own GitHub sign-in, the one recipe (docs/adr/0021-agent-github-signin.md): the connect line's
Terminal (client/github_signin.py), the panel's "Sign in to GitHub" (adapters/agents/agent_github.py) and
the command people type themselves (command(): the panel and docs/GITHUB_SETUP.md show it) all use it.

GitHub's device sign-in (`gh auth login --web`) into that agent's own gh folder, .gh/<agent> (mode 700),
never the owner's usual gh login. --insecure-storage keeps the token in that folder, not the keychain:
gh's keychain entry is shared by every gh folder (ADR 0017's GPT review). Git settings gh may change land
in that folder too (GIT_CONFIG_GLOBAL), and no system git config is read (GIT_CONFIG_NOSYSTEM).
Also the words and fixed result list both sides of the panel's sign-in share.
"""
import os
import re
import shlex
from pathlib import Path

from common import github
from common import kind_report

NEEDED_FLAG = "--insecure-storage"            # older gh lacks it: said, never a cryptic failure
LOGIN = ["auth", "login", "--hostname", "github.com", "--git-protocol", "https", "--web", NEEDED_FLAG]
DEVICE_PAGE = "https://github.com/login/device"   # where a person enters the code (also server/github_link.py)
USER_CODE_RE = re.compile(r"^[A-Z0-9]{4}-[A-Z0-9]{4}$")   # GitHub's one-time user codes
NO_BROWSER = "cmd /c rem" if os.name == "nt" else "true"   # the panel's sign-in: nothing opens on that computer

# Panel sign-in messages (to-device; Synapse sets the sender, ADR 0020's pattern).
REQUEST_TYPE = "xyz.hagencylabs.agent.github"          # @pm → agent: {"id"} (an owner's click)
ANSWER_TYPE = "xyz.hagencylabs.agent.github.result"    # agent → @pm: {"id", "code"}, then {"id", "result", "login"?}
STATE_TYPE = "xyz.hagencylabs.agent.github.state"      # agent → @pm at each start: {"state", "login"?}
RESULTS = ("signed_in", "failed", "timeout", "no_gh", "too_old_gh", "already")
STATES = ("signed_in", "none", "no_gh")
# How long each side waits for gh's code. @pm waits longer than the agent's worst case (its --help, its wait
# for the code, the send), so a code the agent sent is never refused as late (review, 2026-11-26).
HELP_S = 30                                   # `gh auth login --help`, and gh's exit after the sign-in
CODE_WAIT_S = 60                              # gh prints its code at once; nothing by then: it failed
PM_CODE_WAIT_S = HELP_S + CODE_WAIT_S + 2 * kind_report.SEND_TIMEOUT_S


def folder(agent: str) -> Path:
    return github.IDENTITY_ROOT / agent


def prepare(agent: str) -> Path:
    """Its gh folder, made private (its GitHub token lives here)."""
    d = folder(agent)
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    return d


def env_for(d: Path, quiet: bool = False) -> dict:
    """gh's environment: no other login's token, only this folder's config; `quiet`: no browser opens."""
    env = {k: v for k, v in os.environ.items() if k not in github.SCRUBBED_ENV and not (quiet and k == "CLICOLOR_FORCE")}
    env.update(GH_CONFIG_DIR=str(d), GIT_CONFIG_GLOBAL=str(d / "gitconfig"), GIT_CONFIG_NOSYSTEM="1")
    if quiet:                                 # plain text too: a colored code would not match (GPT review)
        env.update(BROWSER=NO_BROWSER, GH_BROWSER=NO_BROWSER, GH_NO_UPDATE_NOTIFIER="1", GH_PROMPT_DISABLED="1", NO_COLOR="1")
    return env


def command(agent: str) -> str:
    """The same sign-in as one shell line, for a terminal in the agent's Hagency folder (~/.hagency/app):
    the same folder, no other login's token (gh refuses to store one while GH_TOKEN is set: GPT review)."""
    d = f'"$PWD/{github.IDENTITY_DIR}/"{shlex.quote(agent)}'
    unset = " ".join(f"-u {k}" for k in github.SCRUBBED_ENV)
    return (f"mkdir -p {d} && chmod 700 {d} && env {unset} GH_CONFIG_DIR={d} GIT_CONFIG_GLOBAL={d}/gitconfig "
            f"GIT_CONFIG_NOSYSTEM=1 gh {' '.join(LOGIN)}")


def supported(help_text: object) -> bool:
    """`gh auth login --help` names the flag this needs."""
    return isinstance(help_text, str) and NEEDED_FLAG in help_text


USER_LINE_RE = re.compile(r"^\s+user:\s*([A-Za-z0-9-]{1,39})\s*$")


def login_of(agent: str) -> str | None:
    """The GitHub username its folder is signed in as on github.com (the `user:` line under `github.com:` in
    hosts.yml), validated; nothing else of that file (its token) is ever read out."""
    try:
        lines = (folder(agent) / "hosts.yml").read_text(errors="replace").splitlines()
    except OSError:                           # not signed in, or unreadable: no name to show (the state says which)
        return None
    host = None
    for line in lines:
        if line[:1] not in ("", " ", "\t", "#"):
            host = line.strip().removesuffix(":")   # a host's section: only github.com's user counts (GPT review)
        m = USER_LINE_RE.match(line)
        if m and host == "github.com" and github.LOGIN_RE.fullmatch(m.group(1)):
            return m.group(1)
    return None


def state(content: object) -> dict | None:
    """An agent's state report as @pm keeps it ({"state", "login"}), or None for anything else."""
    if not isinstance(content, dict) or not set(content) <= {"state", "login"} or content.get("state") not in STATES:
        return None
    login = content.get("login")
    if login is not None and not (isinstance(login, str) and github.LOGIN_RE.fullmatch(login)):
        return None
    return {"state": content["state"], "login": login if content["state"] == "signed_in" else None}
