"""An agent updates its own code from the official repo, when its owner clicks Update in the Hagency
panel (docs/adr/0020-versions-and-agent-self-update.md; agent_update.py receives @pm's request).

apply() moves ~/.hagency/app forward to one commit of csheargm/hagency-rooms's master, and only:
  - when this process runs that official install (not the owner's own version, customize.py, and not a
    folder they chose with HAGENCY_APP);
  - when that install is a clean clone of the official repo on branch master (nothing edited);
  - to a commit on the official master, fetched from its URL, that is a descendant of what runs now.
The new commit's requirements are installed first, so a failed pip changes nothing. Git runs with
hooks off and never asks for a password. Every outcome is one word of code_report.RESULTS (what @pm may show).
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from common import paths
from common.version import SHA_RE

OFFICIAL = "https://github.com/csheargm/hagency-rooms"   # never from the environment (customize.UPSTREAM may be)
BRANCH = "master"
DEFAULT_APP = Path.home() / ".hagency" / "app"          # where connect.sh / connect.ps1 install it
GIT_TIMEOUT_S = 120
PIP_TIMEOUT_S = 600
GIT = ["git", "-c", "core.hooksPath=/dev/null", "-c", "protocol.file.allow=never", "-c", "core.fsmonitor=false"]
# No system or global git config (a url.insteadOf there could send the fetch elsewhere), no replace
# objects (they could swap a verified commit's files), never a password prompt (GPT review).
GIT_ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "", "SSH_ASKPASS": "", "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_NO_REPLACE_OBJECTS": "1"}
LOCK_NAME = "hagency-update.lock"             # in .git: several agents of one computer share the install


class Refused(Exception):
    """apply() stopped: str(e) is one of code_report.RESULTS."""


def same_repo(url: str) -> bool:
    return url.strip().removesuffix("/").removesuffix(".git").lower() == OFFICIAL.lower()


def _git(app: Path, *args: str, run=subprocess.run) -> subprocess.CompletedProcess:
    return run([*GIT, "-C", str(app), *args], capture_output=True, text=True, timeout=GIT_TIMEOUT_S, env=GIT_ENV)


def check_install(app: Path, code: Path, own: bool, run=subprocess.run) -> None:
    """Refused unless this process runs a clean, official clone on master at `app`."""
    if own or code.resolve() != app.resolve():
        raise Refused("own")                  # its owner's own version, or a folder they chose: never overwritten
    if not (app / ".git").is_dir():
        raise Refused("not_official")
    origin = _git(app, "remote", "get-url", "origin", run=run)
    branch = _git(app, "symbolic-ref", "-q", "--short", "HEAD", run=run)
    if origin.returncode or not same_repo(origin.stdout) or branch.returncode or branch.stdout.strip() != BRANCH:
        raise Refused("not_official")
    status = _git(app, "status", "--porcelain", "--untracked-files=no", run=run)
    hidden = _git(app, "ls-files", "-v", run=run)    # assume-unchanged (lower case) or skip-worktree (S): status hides those
    if status.returncode or status.stdout.strip() or hidden.returncode or any(
            line[:1].islower() or line[:1] == "S" for line in hidden.stdout.splitlines()):
        raise Refused("edited")


def _ancestor(app: Path, older: str, newer: str, run) -> bool:
    return _git(app, "merge-base", "--is-ancestor", older, newer, run=run).returncode == 0


def check_target(app: Path, target: str, run=subprocess.run) -> str:
    """Fetch the official master; "current" when `target` already runs (or is behind), else the old
    commit. Refused unless `target` is on that master and ahead of what runs now."""
    if not (isinstance(target, str) and SHA_RE.match(target)):
        raise Refused("not_newer")
    head = _git(app, "rev-parse", "HEAD", run=run).stdout.strip()
    if head == target:
        return "current"
    if _git(app, "fetch", "-q", "--no-tags", OFFICIAL, BRANCH, run=run).returncode:   # the URL, never a remote's name
        raise Refused("failed")
    if not _ancestor(app, target, "FETCH_HEAD", run):
        raise Refused("not_newer")            # not official master's
    if _ancestor(app, target, head, run):
        return "current"                      # already newer than what was asked
    if not _ancestor(app, head, target, run):
        raise Refused("not_newer")
    return head


def install_requirements(app: Path, target: str, run=subprocess.run) -> None:
    """The new commit's requirements, with this agent's own Python, before any code moves."""
    wanted = _git(app, "show", f"{target}:requirements.txt", run=run)
    if wanted.returncode:
        raise Refused("failed")
    with tempfile.TemporaryDirectory() as tmp:
        req = Path(tmp) / "requirements.txt"
        req.write_text(wanted.stdout)
        done = run([sys.executable, "-m", "pip", "install", "-q", "--disable-pip-version-check", "-r", str(req)],
                   capture_output=True, text=True, timeout=PIP_TIMEOUT_S)
    if done.returncode:
        raise Refused("failed")


class _Lock:
    """One update of this install at a time, across processes (each agent of this computer has its own)."""
    def __init__(self, app: Path) -> None:
        self.path = app / ".git" / LOCK_NAME

    def __enter__(self):
        self.file = open(self.path, "a+")
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise Refused("failed") from None   # another agent of this computer is updating it right now
        return self

    def __exit__(self, *exc) -> None:
        self.file.close()                     # closing releases the lock


def apply(target: str, own: bool, app: Path = DEFAULT_APP, code: Path = paths.ROOT, run=subprocess.run) -> str:
    """Update to `target`; one of code_report.RESULTS. "updated" only once the code moved: then restart."""
    try:
        check_install(app, code, own, run)
        with _Lock(app):
            if check_target(app, target, run) == "current":
                return "current"
            install_requirements(app, target, run)
            check_install(app, code, own, run)   # again: pip took a while, and an edit meanwhile is never overwritten
            if _git(app, "merge", "-q", "--ff-only", target, run=run).returncode:
                raise Refused("failed")
    except Refused as e:
        return str(e)
    except (OSError, subprocess.SubprocessError):
        return "failed"                       # no git, a timeout: said to @pm, which shows it; the agent keeps running
    return "updated"
