"""Where a person's agent works: one folder per room under ~/.hagency/work/<owner>/, outside the
app's install. (It used to run inside the app checkout, at ROOT/rooms/<owner>/<room>, and could read
the app's own files and git log.) A folder from before moves here the first time the room is used,
with its saved session ids; Claude Code keys its sessions by folder, so its transcript is copied to
the new folder's entry too, or the session is dropped and the next turn starts a fresh one.
HAGENCY_DIR moves the whole ~/.hagency (as for connect.py's sign-in).
"""
import errno
import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path

WORK_HOME = (Path(os.environ.get("HAGENCY_DIR", Path.home() / ".hagency")).expanduser() / "work").absolute()
SESSION_FILE = ".bridge-session.json"      # agents.run_turn: {backend: session id}
SESSION_ID_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z-]{7,63}$")  # codex/claude session UUIDs

log = logging.getLogger("workdirs")


def room_key(room_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", room_id)[:40]
    digest = hashlib.sha256(room_id.encode()).hexdigest()[:12]   # sanitizing alone is lossy
    return f"{safe}-{digest}"


def room_dir(owner: str, room_id: str, legacy_root: Path | None = None) -> Path:
    """The owner's agent's folder for this room; an old one under legacy_root/rooms/ moves here."""
    new = WORK_HOME / owner / room_key(room_id)
    if legacy_root is not None:
        migrate(legacy_root / "rooms" / owner / room_key(room_id), new)
    return new


def hermes_dir(owner: str) -> Path:
    """Where the owner's Hermes gateway runs its terminal (hermes_profile.py)."""
    return WORK_HOME / owner / "hermes"


def claude_project(cwd: Path) -> Path:
    """Claude Code's per-folder session store: the absolute path with every other character as '-'."""
    home = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
    return home / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(cwd.resolve()))


def migrate(old: Path, new: Path) -> bool:
    """Move an old room folder to its new place, once. False: nothing moved (none, or already there)."""
    if new.exists() or old.is_symlink() or not old.is_dir():
        return False                              # (a symlink there isn't one of ours: never adopt it)
    transcript_from = claude_project(old)       # before the move: resolve() needs the old path's parents
    new.parent.mkdir(parents=True, exist_ok=True)
    try:
        _move(old, new)
    except OSError as e:
        # The agent still works: it starts a fresh conversation in the new folder, and the old
        # folder stays where it was for the owner to look at. Logged for the operator.
        log.warning("couldn't move %s to %s (starting fresh there): %s", old, new, e)
        return False
    _carry_claude_session(transcript_from, new)
    return True


def _move(old: Path, new: Path) -> None:
    try:
        os.rename(old, new)                       # same disk: atomic
        return
    except OSError as e:
        if e.errno != errno.EXDEV:
            raise
    stage = Path(tempfile.mkdtemp(dir=new.parent, prefix=f".{new.name}.moving-"))   # another disk:
    try:                                          # copy aside under a unique name, then swap in whole
        shutil.copytree(old, stage / "x", symlinks=True)
        os.rename(stage / "x", new)
    finally:
        shutil.rmtree(stage, ignore_errors=True)  # only our own staging folder; nothing else to keep
    shutil.rmtree(old, ignore_errors=True)        # a leftover old copy is harmless: never read again


def _carry_claude_session(transcript_from: Path, new: Path) -> None:
    state_file = new / SESSION_FILE
    try:
        state = json.loads(state_file.read_text()) if state_file.exists() else {}
    except (OSError, ValueError) as e:
        log.warning("unreadable %s after the move (the next turns start fresh): %s", state_file, e)
        state_file.unlink(missing_ok=True)
        return
    if not isinstance(state, dict):
        state_file.unlink(missing_ok=True)        # not ours: the next turns start fresh
        return
    sid = state.get("claude")
    if not (isinstance(sid, str) and SESSION_ID_RE.match(sid)):
        return
    src = transcript_from / f"{sid}.jsonl"
    try:
        dst = claude_project(new)
        dst.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst / src.name)
        if (transcript_from / sid).is_dir():       # the session's own folder (subagents, tool results)
            shutil.copytree(transcript_from / sid, dst / sid, symlinks=True, dirs_exist_ok=True)
    except OSError as e:
        log.info("no Claude Code transcript to carry for %s (%s): its next turn starts fresh", new, e)
        state.pop("claude")
        state_file.write_text(json.dumps(state))
