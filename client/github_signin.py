"""Sign a just-connected agent in to GitHub, right in the connect line's Terminal (owner's ask, 2026-11-23).

Live test: an agent could plan but not open issues or pull requests until someone ran `gh auth login`
for it by hand (common/github.NO_IDENTITY), and nothing during setup said so. Now connect.py offers it
once the agent is connected, while its owner is at the keyboard: GitHub's own device sign-in (`gh auth
login --web`), into that agent's own gh folder (.gh/<agent>, as github.run reads it), never the
owner's usual gh login. Skipped when it is already signed in, when gh isn't installed (said, with the
command for later), or when the answer is no. Never fatal: the agent works without GitHub.
"""
import os
import shutil
import subprocess
import sys

from client.ask_line import ask_line
from common import github

LATER = ("  Later, in {app}: GH_CONFIG_DIR=.gh/{agent} gh auth login --hostname github.com "
         "--git-protocol https --web")


def offer(agent: str, app, ask=input, run=subprocess.run, which=shutil.which, interactive=None) -> bool:
    """True if the agent is signed in to GitHub when this returns. Asks only at a terminal (connect.sh
    gives it one); with nobody to answer, it says how to do it later."""
    if github.identity_dir(agent) is not None:
        return True
    if which("gh") is None:
        print("note: to let your agent open issues and pull requests, install the GitHub CLI "
              "(https://cli.github.com), then sign it in:\n" + LATER.format(app=app, agent=agent))
        return False
    if not (sys.stdin.isatty() if interactive is None else interactive):
        print("note: to let your agent open issues and pull requests, sign it in to GitHub:\n" + LATER.format(app=app, agent=agent))
        return False
    answer = ask_line("Sign your agent in to GitHub now, so it can open issues and pull requests? "
                      "Use the GitHub account it should work as. [Y/n]", ask).lower()
    if answer not in ("", "y", "yes"):
        print(LATER.format(app=app, agent=agent))
        return False
    folder = github.IDENTITY_ROOT / agent
    folder.mkdir(parents=True, exist_ok=True)
    os.chmod(folder, 0o700)                   # its GitHub token lives here
    env = {k: v for k, v in os.environ.items() if k not in github.SCRUBBED_ENV}   # never another login's token
    env["GH_CONFIG_DIR"] = str(folder)
    try:
        done = run(["gh", "auth", "login", "--hostname", "github.com", "--git-protocol", "https", "--web"],
                   env=env, check=False).returncode == 0
    except OSError as e:                      # said, with the command for later: GitHub is optional
        print(f"note: couldn't start gh ({e})")
        done = False
    if done and github.identity_dir(agent) is not None:
        print("✓ your agent is signed in to GitHub")
        return True
    print("note: your agent isn't signed in to GitHub yet." + "\n" + LATER.format(app=app, agent=agent))
    return False
