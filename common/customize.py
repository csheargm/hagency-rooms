"""Your own version of Hagency Rooms, made by your agent from your private room (bridge_custom.py).

Folders per owner under custom/<owner>/ (git-ignored), all in the official folder (HAGENCY_HOME):
  work/          a git worktree on branch custom/<owner>: the agent edits only here, in its sandbox.
  live-<commit>/ one checkout per commit the owner chose with "Use it myself", never changed after;
                 supervise.py runs the owner's bridge from the selected one.
Nothing the agent wrote runs here before the owner clicks. "Use it myself" then runs that code with
this computer account's access, so it is only offered where the owner is the only person whose agent
runs on this computer (or the operator opts in, HAGENCY_CUSTOM_SHARED_OK=1). "Send as pull request"
works everywhere: GitHub tests it, not this computer.
State (data/custom-use.json) is read by supervise.py, @pm and the console, which run the official
code, so "go back" works even when a custom version is broken.
"""
import asyncio
import json
import os
import re
import shutil
import signal
from pathlib import Path

from common import github
from common import registry
from common.saved_dict import SavedDict
from common import paths

ROOT = Path(os.environ.get("HAGENCY_HOME") or paths.ROOT)   # official folder, even inside live-*/
COPIES = ROOT / "custom"
USE_FILE = ROOT / "data" / "custom-use.json"     # owner → commit their bridge runs (absent: official)
UPSTREAM = os.environ.get("HAGENCY_UPSTREAM", "csheargm/hagency-rooms")
PY = str(ROOT / ".venv" / "bin" / "python")
TEST_TIMEOUT_S = 600
MAX_DIFF_FILES = 15
# Code that decides who the agent obeys and what it may do: changes here get an extra red flag.
SENSITIVE = {"bridge.py", "bridge_voice.py", "bridge_custom.py", "agents.py", "cli_path.py", "redact.py",
             "safe_fetch.py", "supervise.py", "customize.py", "lifecycle.py", "ownership.py", "github.py",
             "room_context.py", "room_state.py", "tasks.py", "registry.py", "saved_dict.py", "console.py",
             "pm_chat.py", "onboard.py", "setup_accounts.py", "panel.py", "hagency_mcp.py", "mcp_core.py",
             "mcp_custom.py", "hermes_profile.py", "bridge_config.py", "pairing.py", "remote_agent.py",
             "pm_welcome.py", "connect.py", "connect.sh", "connect.ps1", "connect_win.py", "connect_page.py",
             "connect.js", "connect.html", "local_signup.py", "agent_removal.py", "pm_remove.py",
             "requirements.txt", ".gitignore", "start.sh",
             "__init__.py", "paths.py"}   # package init code; where the repo is (docs/adr/0011-repo-layout.md)
# Git-ignored things a live version shares with the official folder (passwords, sessions, state).
SHARED = (".env", ".venv", ".gh", "data", "rooms", "logs", "agents.json")
OWNER_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
GO_BACK = re.compile(r"\b(go|switch) back to the official\b", re.I)   # typed to the agent or to @pm


class CustomError(RuntimeError):
    pass


def _owner(owner: str) -> str:
    if not OWNER_RE.match(owner):
        raise CustomError(f"bad owner name {owner!r}")
    return owner


def _sha(sha: str) -> str:
    if not isinstance(sha, str) or not SHA_RE.match(sha):
        raise CustomError("bad commit")
    return sha


def work_dir(owner: str) -> Path:
    return COPIES / _owner(owner) / "work"


def live_dir(owner: str, sha: str) -> Path:
    return COPIES / _owner(owner) / f"live-{_sha(sha)[:12]}"


def why_not_use(owner: str) -> str | None:
    """Why "Use it myself" isn't offered here, or None. Custom code runs as this computer's account,
    so on a computer that also runs other people's agents it could read their passwords."""
    others = [p for p in registry.load() if registry.local_name(p) != owner]
    if others and os.environ.get("HAGENCY_CUSTOM_SHARED_OK") != "1":
        return ("this computer also runs other people's agents, and your version would run with access to "
                "their passwords too. Send it as a pull request instead, or run your agent on your own computer")
    return None


def in_use() -> dict[str, str]:
    """owner → commit, for everyone running their own version (fresh read: other processes write it)."""
    try:
        data = json.loads(USE_FILE.read_text())
    except (OSError, ValueError):
        return {}                             # no file (nobody customised) or damaged: everyone official
    return {o: c for o, c in data.items() if isinstance(c, str) and OWNER_RE.match(o) and SHA_RE.match(c)}


def code_dir(owner: str) -> tuple[Path, str | None]:
    """Where the owner's bridge runs from, and which commit: their chosen version, or official (None)."""
    sha = in_use().get(owner)
    if sha and why_not_use(owner):            # the operator took the opt-in back: official until it's given again
        return ROOT, None
    if sha and (live_dir(owner, sha) / "bridge.py").exists():
        return live_dir(owner, sha), sha
    return ROOT, None


def go_back(owner: str, only_if: str | None = None) -> bool:
    """Run the official version again (only if `only_if` is still the chosen commit, when given)."""
    use = SavedDict(USE_FILE)
    if owner not in use or (only_if is not None and use.get(owner) != only_if):
        return False
    use.pop(owner)
    return True


async def _git(args: list[str], cwd: Path, work: Path | None = None) -> str:
    """git with no hooks, filters, fsmonitor or user config. For the agent-edited copy (work), its
    metadata is named explicitly, never found through the copy's own (agent-writable) .git file."""
    safe = ["-c", f"core.hooksPath={os.devnull}", "-c", "core.fsmonitor=false", "-c", "commit.gpgsign=false",
            "-c", f"core.attributesFile={os.devnull}", "-c", "user.name=Hagency Rooms",
            "-c", "user.email=agent@hagency.invalid"]
    if work is not None:
        gitdir = Path((work.parent / "gitdir").read_text().strip())
        safe += [f"--git-dir={gitdir}", f"--work-tree={work}"]
    return (await github.run([*safe, *args], None, cwd, program="git", extra_env=github.ISOLATED_GIT_ENV)).strip()


async def prepare(owner: str) -> Path:
    """The owner's private copy: created from the official version, then kept up to date with it."""
    work = work_dir(owner)
    if work.exists() and not (work.parent / "gitdir").exists():
        raise CustomError("your private copy is from an older version of this feature; ask me to start over "
                          "after removing custom/" + owner)
    if not (work.parent / "gitdir").exists():
        work.parent.mkdir(parents=True, exist_ok=True)
        await _git(["worktree", "add", "--force", "-B", f"custom/{owner}", str(work), "HEAD"], ROOT)
        gitdir = (work / ".git").read_text().split("gitdir:", 1)[1].strip()   # ours, before the agent ran
        (work.parent / "gitdir").write_text(gitdir)
        (work.parent / "base").write_text(await _git(["rev-parse", "HEAD"], ROOT))
        return work
    official = await _git(["rev-parse", "HEAD"], ROOT)
    if (work.parent / "base").read_text().strip() != official:   # the official version moved on: follow it
        try:
            await _git(["rebase", "-q", official], work, work)
        except github.GitHubError as e:
            await _git(["rebase", "--abort"], work, work)
            raise CustomError("your earlier changes clash with the new official version; "
                              "discard them (🗑️) and ask again") from e
        (work.parent / "base").write_text(official)
    return work


def check_tree(work: Path) -> None:
    """Refuse what git or Python could act on by itself: links (to files outside) and attributes."""
    for dirpath, dirs, files in os.walk(work):
        dirs[:] = [d for d in dirs if not (dirpath == str(work) and d == ".git")]
        for name in dirs + files:
            path = Path(dirpath) / name
            if path.is_symlink() or name == ".gitattributes" or name.endswith(".pth"):
                raise CustomError(f"the change adds {path.relative_to(work)}, a kind of file I don't accept")


async def commit(owner: str, request: str) -> str | None:
    """Commit what the agent changed in the copy; None if it changed nothing."""
    work = work_dir(owner)
    check_tree(work)
    await _git(["add", "-A"], work, work)
    if not await _git(["status", "--porcelain"], work, work):
        return None
    await _git(["commit", "-q", "-m", f"custom: {request.splitlines()[0][:72]}"], work, work)
    return await _git(["rev-parse", "HEAD"], work, work)


async def changed_files(owner: str) -> list[str]:
    base = (work_dir(owner).parent / "base").read_text().strip()
    out = await _git(["diff", "--name-only", f"{base}..HEAD"], work_dir(owner), work_dir(owner))
    return [f for f in out.splitlines() if f]


def sensitive(files: list[str]) -> list[str]:
    return [f for f in files if Path(f).name in SENSITIVE or f.startswith((".github/", "deploy/"))]


async def run_tests(cwd: Path) -> tuple[bool, str]:
    """That version's unit tests, with no environment to speak of; (passed, our own summary)."""
    proc = await asyncio.create_subprocess_exec(
        PY, "-m", "unittest", "discover", "-p", "test_*.py", cwd=cwd, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE, start_new_session=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(cwd), "LANG": "C.UTF-8"})
    try:
        _, err = await asyncio.wait_for(proc.communicate(), TEST_TIMEOUT_S)
    except asyncio.TimeoutError:
        os.killpg(proc.pid, signal.SIGKILL)   # the whole process group, not just the first process
        await proc.wait()
        return False, f"they took longer than {TEST_TIMEOUT_S // 60} minutes"
    ran = re.findall(rb"^Ran (\d+) tests? in", err[-4000:], re.M)
    n = int(ran[-1]) if ran else 0
    if proc.returncode != 0 or n == 0:
        return False, "no tests ran" if n == 0 else f"{n} tests ran and some failed"
    return True, f"all {n} tests pass"


async def _checkout(path: Path, sha: str) -> None:
    if (path / ".git").exists():
        await _git(["checkout", "-q", "--force", "--detach", sha], path)
    else:
        await _git(["worktree", "add", "--force", "--detach", str(path), sha], ROOT)


async def use(owner: str, sha: str) -> tuple[bool, str]:
    """Test that commit and, if it passes, check it out in its own live folder and choose it."""
    reason = why_not_use(owner)
    if reason:
        raise CustomError(reason)
    trial = COPIES / _owner(owner) / "trial"
    await _checkout(trial, _sha(sha))     # its own folder, without the passwords or state
    ok, summary = await run_tests(trial)
    if not ok:
        return False, summary
    live = live_dir(owner, sha)
    await _checkout(live, sha)            # a new folder per commit: the running version is never changed
    for name in SHARED:
        link, target = live / name, ROOT / name
        if target.exists() and not link.is_symlink():
            link.symlink_to(target)
    previous = in_use().get(owner)
    SavedDict(USE_FILE)[owner] = sha
    await prune(owner, keep={sha, previous})
    return True, summary


async def prune(owner: str, keep: set) -> None:
    """Remove live folders of versions no longer chosen (the one being replaced stays until next time)."""
    keep_dirs = {live_dir(owner, s).name for s in keep if s}
    for path in (COPIES / _owner(owner)).glob("live-*"):
        if path.name not in keep_dirs:
            try:
                await _git(["worktree", "remove", "--force", str(path)], ROOT)
            except github.GitHubError:
                shutil.rmtree(path, ignore_errors=True)   # not a worktree any more: just the folder


async def send_pr(owner: str, sha: str, title: str, body: str) -> str:
    """Open a pull request to the official repo from the owner's agent's GitHub identity."""
    _sha(sha)
    try:                                      # a commit of their own copy, and not already official
        await _git(["merge-base", "--is-ancestor", sha, f"refs/heads/custom/{_owner(owner)}"], ROOT)
    except github.GitHubError as e:
        raise CustomError("that change was updated since; use my newest offer") from e
    try:
        await _git(["merge-base", "--is-ancestor", sha, "HEAD"], ROOT)
    except github.GitHubError:
        pass                                  # not in the official version: theirs to send
    else:
        raise CustomError("that's already part of the official version")
    agent = f"{owner}-agent"
    login = (await github.run(["api", "user", "-q", ".login"], agent)).strip()
    if not re.fullmatch(r"[A-Za-z0-9-]{1,39}", login):
        raise CustomError("GitHub returned an odd account name")
    up_owner, name = UPSTREAM.split("/")
    if login.lower() != up_owner.lower():
        await github.run(["repo", "fork", UPSTREAM, "--clone=false"], agent)   # no-op when it exists
    branch = f"custom/{owner}-{sha[:7]}"
    await github.git(["push", f"https://github.com/{login}/{name}.git", f"{sha}:refs/heads/{branch}"], agent, ROOT)
    out = await github.run(["pr", "create", "-R", UPSTREAM, "--head", f"{login}:{branch}",
                            "--title", title[:100], "--body", body], agent)
    url = next((w for w in out.split() if w.startswith("https://github.com/")), "")
    if not url:
        raise CustomError("GitHub didn't return the pull request's link")
    return url


async def discard(owner: str, sha: str) -> bool:
    """Undo that change in the private copy, if it's still the newest one there."""
    work = work_dir(owner)
    if await _git(["rev-parse", "HEAD"], work, work) != _sha(sha):
        return False
    await _git(["reset", "-q", "--hard", "HEAD~1"], work, work)
    return True
