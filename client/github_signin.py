"""Sign a just-connected agent in to GitHub, right in the connect line's Terminal (owner's ask, 2026-11-23);
the same sign-in then counts for its owner too (github_check.py, ADR 0017).

Live test: an agent could plan but not open issues or pull requests until someone ran `gh auth login`
for it by hand (common/github.NO_IDENTITY), and nothing during setup said so. Now connect.py offers it
once the agent is connected, while its owner is at the keyboard: GitHub's own device sign-in (`gh auth
login --web`), into that agent's own gh folder (.gh/<agent>, as github.run reads it), never the
owner's usual gh login: common/github_login.py, the one recipe (folder, environment, flags, and the
command said for later), which the panel's "Sign in to GitHub" uses too (ADR 0021). Skipped when it is already signed in, when gh isn't installed (said, with the
command for later), or when the answer is no. Never fatal: the agent works without GitHub.
"""
import shutil
import subprocess
import sys

from client import github_check
from client.ask_line import ask_line
from common import github
from common import github_login

LATER = "  Later, in {app}: {command}"


def later(app, agent: str) -> str:
    return LATER.format(app=app, command=github_login.command(agent))


def offer(agent: str, app, ask=input, run=subprocess.run, which=shutil.which, interactive=None,
          creds: dict | None = None, server: str | None = None, prove=github_check.prove) -> bool:
    """True if the agent is signed in to GitHub when this returns, and then (given creds and server) its
    account is linked to the person too: one sign-in for both (ADR 0017). Asks only at a terminal
    (connect.sh gives it one); with nobody to answer, it says how to do it later."""
    if github.identity_dir(agent) is not None:   # signed in before: linked to the person only on their yes
        if creds and server and (sys.stdin.isatty() if interactive is None else interactive) and _yes(
                "Your agent is signed in to GitHub. Use that account for you too? [Y/n]", ask):
            return link(creds, server, prove)
        return True
    if which("gh") is None:
        print("note: to let your agent open issues and pull requests, install the GitHub CLI "
              "(https://cli.github.com), then sign it in:\n" + later(app, agent))
        return False
    if not (sys.stdin.isatty() if interactive is None else interactive):
        print("note: to let your agent open issues and pull requests, sign it in to GitHub:\n" + later(app, agent))
        return False
    if not _yes("Sign in to GitHub? You and your agent will both use this account. [Y/n]" if creds and server else
                "Sign your agent in to GitHub, so it can open issues and pull requests? [Y/n]", ask):
        print(later(app, agent))
        return False
    try:
        env = github_login.env_for(github_login.prepare(agent))   # never another login's token
        if not github_login.supported(run(["gh", *github_login.LOGIN[:2], "--help"], env=env, capture_output=True, text=True,
                                          check=False).stdout):   # older gh (review): say so, never a cryptic failure
            print("note: your GitHub CLI is too old for this; update it (https://cli.github.com), then:\n"
                  + later(app, agent))
            return False
        done = run(["gh", *github_login.LOGIN], env=env, check=False).returncode == 0
    except (OSError, KeyboardInterrupt) as e:  # said, with the command for later: GitHub is optional
        print(f"\nnote: couldn't sign it in here ({type(e).__name__})")
        done = False
    if done and github.identity_dir(agent) is not None:
        return link(creds, server, prove)
    print("note: your agent isn't signed in to GitHub yet." + "\n" + later(app, agent))
    return False


def link(creds: dict | None, server: str | None, prove) -> bool:
    """The agent is signed in: link its account to the person as well (one sign-in for both)."""
    try:
        login = prove(creds, server) if creds and server else None
    except KeyboardInterrupt:                 # Ctrl-C during the check: the agent still starts (GPT re-check)
        print()
        login = None
    print(f"✓ Signed in to GitHub as @{login}. You and your agent both use it." if login else
          "✓ your agent is signed in to GitHub")
    return True


def _yes(question: str, ask) -> bool:
    try:
        return ask_line(question, ask).lower() in ("", "y", "yes")
    except (EOFError, OSError, KeyboardInterrupt):   # Ctrl-D/Ctrl-C, a lost terminal: a no (the agent still starts)
        print()
        return False
