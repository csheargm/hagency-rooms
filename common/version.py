"""Which code runs here: a commit and its time (docs/adr/0020-versions-and-agent-self-update.md).

@pm's folder on the server is a `git archive` copy with no .git: deploy/deploy.sh writes REVISION
there (line 1 the commit, line 2 its commit time in Unix seconds). A git checkout (an agent's
~/.hagency/app, a developer's copy) has no such file, so git is asked. The panel's footer shows the
server's; My agents compares each agent's with it.

The release number ("1.0.0") is the repo's VERSION file, the one place it is kept: bumped in the pull
request that makes a release (CONTRIBUTING.md), and copied to the server with the other files.
"""
import datetime
import re
import subprocess
from pathlib import Path

REVISION_FILE = "REVISION"
NUMBER_FILE = "VERSION"
NUMBER_RE = re.compile(r"[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}")   # "1.0.0": major.minor.patch
SHORT = 7
GIT_TIMEOUT_S = 10
TIME_RE = re.compile(r"[0-9]{1,10}")   # up to the year 2286: a date always formats (GPT review)
SHA_RE = re.compile(r"^[0-9a-f]{40}$")          # a full commit id (customize.py and self_update.py too)


def parse(commit: object, when: object) -> dict | None:
    """{"commit", "time"} when commit is a full id and time a whole number of seconds; else None."""
    if not (isinstance(commit, str) and SHA_RE.match(commit)):
        return None
    text = str(when) if isinstance(when, (str, int)) and not isinstance(when, bool) else ""
    if not TIME_RE.fullmatch(text):
        return None                           # never "+5", "1_000" or " 5" (int() would take those)
    return {"commit": commit, "time": int(text)} if int(text) > 0 else None


def _from_git(root: Path) -> dict | None:
    try:
        out = subprocess.run(["git", "-C", str(root), "log", "-1", "--format=%H%n%ct"], capture_output=True,
                             text=True, timeout=GIT_TIMEOUT_S, check=True).stdout.split()
    except (OSError, subprocess.SubprocessError):
        return None                           # no git, or not a checkout: the version is simply unknown (shown as such)
    return parse(*out) if len(out) == 2 else None


def number(root: Path) -> str | None:
    """The release number in `root`'s VERSION file, or None (no file, or not major.minor.patch)."""
    try:
        text = (root / NUMBER_FILE).read_text().strip()
    except OSError:
        return None                           # code from before release numbers: shown without one
    return text if NUMBER_RE.fullmatch(text) else None


def of(root: Path) -> dict | None:
    """The code at `root`: its REVISION file if it has one, else its git HEAD; None if neither says.
    With its release number as "number" when its VERSION file gives one."""
    try:
        lines = (root / REVISION_FILE).read_text().split()
        found = parse(*lines[:2]) if len(lines) >= 2 else None
    except OSError:
        found = _from_git(root)               # no file: a checkout
    num = number(root) if found else None
    return {**found, "number": num} if num else found


def label(v: dict | None) -> str:
    """"v1.0.0 · abc1234 · 2026-11-25" (the commit's date in UTC; no number for code from before
    VERSION), or "" when unknown."""
    if not v:
        return ""
    day = datetime.datetime.fromtimestamp(v["time"], datetime.timezone.utc).date().isoformat()
    return " · ".join([*([f"v{v['number']}"] if v.get("number") else []), v["commit"][:SHORT], day])


def current(agent: dict | None, server: dict | None) -> bool:
    """An agent's code is up to date: the server's own commit, or a later one (both are on master)."""
    return bool(agent and server) and (agent["commit"] == server["commit"] or agent["time"] >= server["time"])
