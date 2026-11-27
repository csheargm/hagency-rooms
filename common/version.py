"""Which code runs here: a commit and its time (docs/adr/0020-versions-and-agent-self-update.md).

@pm's folder on the server is a `git archive` copy with no .git: deploy/deploy.sh writes REVISION
there (line 1 the commit, line 2 its commit time in Unix seconds, then the commit's message: summary()). A git checkout (an agent's
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
NUMBER_KEY = "number"                           # its key in of()'s result and in code reports
NUMBER_RE = re.compile(r"[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}")   # "1.0.0": major.minor.patch
SHORT = 7
GIT_TIMEOUT_S = 10
TIME_RE = re.compile(r"[0-9]{1,10}")   # up to the year 2286: a date always formats (GPT review)
SHA_RE = re.compile(r"^[0-9a-f]{40}$")          # a full commit id (customize.py and self_update.py too)
MERGE_RE = re.compile(r"^Merge pull request #[0-9]+ ")   # GitHub's merge commit: its pull request's title is the next line
SUMMARY_MAX = 100
UNSAFE_RE = re.compile(r"[<>\[\]`*\\]")      # never markup in @pm's chat line (pm_auto_update.py)


def parse(commit: object, when: object) -> dict | None:
    """{"commit", "time"} when commit is a full id and time a whole number of seconds; else None."""
    if not (isinstance(commit, str) and SHA_RE.match(commit)):
        return None
    text = str(when) if isinstance(when, (str, int)) and not isinstance(when, bool) else ""
    if not TIME_RE.fullmatch(text):
        return None                           # never "+5", "1_000" or " 5" (int() would take those)
    return {"commit": commit, "time": int(text)} if int(text) > 0 else None


def _git(root: Path, *args: str) -> str | None:
    try:
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                              timeout=GIT_TIMEOUT_S, check=True).stdout
    except (OSError, ValueError, subprocess.SubprocessError):
        return None                           # no git, not a checkout, or not in that commit: simply unknown (shown as such)


def _from_git(root: Path) -> dict | None:
    out = (_git(root, "log", "-1", "--format=%H%n%ct") or "").split()
    found = parse(*out) if len(out) == 2 else None
    if found:                                 # VERSION as that commit has it, never an edited copy (GPT review)
        found = _numbered(found, _git(root, "show", f"{found['commit']}:{NUMBER_FILE}"))
    return found


def _numbered(found: dict | None, text: str | None) -> dict | None:
    """`found` with "number" when `text` (a VERSION file's) is major.minor.patch."""
    num = (text or "").strip()
    return {**found, NUMBER_KEY: num} if found and NUMBER_RE.fullmatch(num) else found


def _read(path: Path) -> str | None:
    try:
        return path.read_text()
    except (OSError, ValueError):             # ValueError: not text (UnicodeDecodeError, GPT review): as if absent
        return None


def of(root: Path) -> dict | None:
    """The code at `root`: its REVISION file if it has one, else its git HEAD; None if neither says.
    With its release number as "number" when its VERSION file gives one (in a checkout, HEAD's)."""
    text = _read(root / REVISION_FILE)
    if text is None:
        return _from_git(root)                # no file: a checkout
    lines = text.split()
    return _numbered(parse(*lines[:2]), _read(root / NUMBER_FILE)) if len(lines) >= 2 else None   # deploy.sh's copy


def summary(root: Path) -> str:
    """What the code at `root` changed, in one line: its commit's subject, or for GitHub's merge commit
    the pull request's title; "" when unknown. For @pm's line after an automatic update (ADR 0024)."""
    text = _read(root / REVISION_FILE)
    lines = (text.splitlines()[2:] if text is not None else (_git(root, "log", "-1", "--format=%s%n%b") or "").splitlines())
    lines = [line.strip() for line in lines if line.strip()]
    if not lines:
        return ""
    said = lines[1] if MERGE_RE.match(lines[0]) and len(lines) > 1 else lines[0]
    said = " ".join(UNSAFE_RE.sub("", said).split())
    return said if len(said) <= SUMMARY_MAX else said[:SUMMARY_MAX - 1].rstrip() + "…"


def name(v: dict | None) -> str:
    """"v1.0.0 · abc1234" (no number for code from before VERSION), or "" when unknown."""
    return " · ".join([*([f"v{v[NUMBER_KEY]}"] if v.get(NUMBER_KEY) else []), v["commit"][:SHORT]]) if v else ""


def iso(v: dict | None) -> str:
    """The commit's time as "2026-11-25T22:32:00Z", for the browser to show in its own time zone
    (panel_agents.js localTime), or "" when unknown."""
    return _utc(v).strftime("%Y-%m-%dT%H:%M:%SZ") if v else ""


def label(v: dict | None) -> str:
    """"v1.0.0 · abc1234 · 2026-11-25 22:32 UTC": the commit's time to the minute, in UTC (the
    panel shows it in the viewer's own time zone), or "" when unknown."""
    return f"{name(v)} · {utc(v)}" if v else ""


def utc(v: dict | None) -> str:
    """"2026-11-25 22:32 UTC": the commit's time to the minute (the words before the browser's own)."""
    return f"{_utc(v).strftime('%Y-%m-%d %H:%M')} UTC" if v else ""


def _utc(v: dict) -> datetime.datetime:
    return datetime.datetime.fromtimestamp(v["time"], datetime.timezone.utc)


def current(agent: dict | None, server: dict | None) -> bool:
    """An agent's code is up to date: the server's own commit, or a later one (both are on master)."""
    return bool(agent and server) and (agent["commit"] == server["commit"] or agent["time"] >= server["time"])
