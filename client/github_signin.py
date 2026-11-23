"""Sign a just-connected agent in to GitHub, right in the connect line's Terminal (owner's ask, 2026-11-23);
the same sign-in then counts for its owner too (github_check.py, ADR 0017).

Live test: an agent could plan but not open issues or pull requests until someone ran `gh auth login`
for it by hand (common/github.NO_IDENTITY), and nothing during setup said so. Now connect.py offers it
once the agent is connected, while its owner is at the keyboard: GitHub's own device sign-in (`gh auth
login --web`), into that agent's own gh folder (.gh/<agent>, as github.run reads it), never the
owner's usual gh login. --insecure-storage keeps the token in that folder (mode 700), not the keychain:
gh's keychain entry is shared by every gh folder, so a second agent's sign-in could make the first one, or
the owner, act as the second (GPT review); a background service can't always reach the keychain either.
Git settings gh may offer to change land in that folder too, never the owner's. Skipped when it is already signed in, when gh isn't installed (said, with the
command for later), or when the answer is no. Never fatal: the agent works without GitHub.
"""
import os
import shutil
import subprocess
import sys

from client import github_check
from client.ask_line import ask_line
from common import github

LOGIN = ["gh", "auth", "login", "--hostname", "github.com", "--git-protocol", "https", "--web", "--insecure-storage"]
LATER = ("  Later, in {app}: GH_CONFIG_DIR=.gh/{agent} GIT_CONFIG_GLOBAL=.gh/{agent}/gitconfig GIT_CONFIG_NOSYSTEM=1 "
         + " ".join(LOGIN))


def offer(agent: str, app, ask=input, run=subprocess.run, which=shutil.which, interactive=None,
          creds: dict | None = None, server: str | None = None, prove=github_check.prove) -> bool:
    """True if the agent is signed in to GitHub when this returns, and then (given creds and server) its
    account is linked to the person too: one sign-in for both (ADR 0017). Asks only at a terminal
    (connect.sh gives it one); with nobody to answer, it says how to do it later."""
    if github.identity_dir(agent) is not None:
        return link(creds, server, prove)
    if which("gh") is None:
        print("note: to let your agent open issues and pull requests, install the GitHub CLI "
              "(https://cli.github.com), then sign it in:\n" + LATER.format(app=app, agent=agent))
        return False
    if not (sys.stdin.isatty() if interactive is None else interactive):
        print("note: to let your agent open issues and pull requests, sign it in to GitHub:\n" + LATER.format(app=app, agent=agent))
        return False
    try:
        answer = ask_line("Sign in to GitHub? You and your agent will both use this account. [Y/n]", ask).lower()
    except (EOFError, OSError):               # Ctrl-D, a lost terminal: a no (GPT review: never keeps the agent from starting)
        answer = "n"
    if answer not in ("", "y", "yes"):
        print(LATER.format(app=app, agent=agent))
        return False
    folder = github.IDENTITY_ROOT / agent
    env = {k: v for k, v in os.environ.items() if k not in github.SCRUBBED_ENV}   # never another login's token
    env.update(GH_CONFIG_DIR=str(folder), GIT_CONFIG_GLOBAL=str(folder / "gitconfig"), GIT_CONFIG_NOSYSTEM="1")
    try:
        folder.mkdir(parents=True, exist_ok=True)
        os.chmod(folder, 0o700)               # its GitHub token lives here
        done = run(LOGIN, env=env, check=False).returncode == 0
    except OSError as e:                      # said, with the command for later: GitHub is optional
        print(f"note: couldn't sign it in here ({e})")
        done = False
    if done and github.identity_dir(agent) is not None:
        return link(creds, server, prove)
    print("note: your agent isn't signed in to GitHub yet." + "\n" + LATER.format(app=app, agent=agent))
    return False


def link(creds: dict | None, server: str | None, prove) -> bool:
    """The agent is signed in: link its account to the person as well (one sign-in for both)."""
    login = prove(creds, server) if creds and server else None
    print(f"✓ Signed in to GitHub as @{login}. You and your agent both use it." if login else
          "✓ your agent is signed in to GitHub")
    return True
