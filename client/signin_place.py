"""Where connect.py keeps each agent's sign-in, so a new pairing never replaces another agent's.

A computer's first agent keeps its sign-in in ~/.hagency/credentials.json, a further one in
~/.hagency/<its name>/ (ADR 0005). Two people may pair on the same computer and user account: the
second one's first agent would then land on the first one's file. place() puts it in its own
folder instead, exactly as a further agent (its own log, service and HAGENCY_CREDENTIALS);
re-pairing the same agent replaces its own file as before. stranded() finds the first agents whose
sign-in an older connect.py already replaced, so connect.py can say what to do (docs/adr/0005).
"""
import json
import os
import plistlib
import shlex
from xml.parsers.expat import ExpatError
from pathlib import Path

from client import agent_service
from client import service_migration
from common import identity

FILE = "credentials.json"
LAUNCHER = "bridge.py"                       # its first argument is the agent's person
REPLACED = ("Your agent {} can't start: its sign-in was replaced. "
            "Ask @pm for a new code and run the line it gives you.")


def read_saved(path: Path) -> dict:
    try:
        saved = json.loads(path.read_text()) if path.exists() else {}
    except (OSError, ValueError):
        saved = {}                            # damaged: a new code replaces it
    return saved if isinstance(saved, dict) else {}


def agent_name(creds: dict) -> str:
    return str(creds.get("user_id", "")).lstrip("@").partition(":")[0]


def _person(who: str, user_id: str) -> str:
    """`who` (a full ID or a bare name on the agent's server) as a full ID."""
    return identity.person_id(who, user_id.partition(":")[2])


def same_agent(saved: dict, fresh: dict) -> bool:
    """The same agent account, for the same person."""
    uid = str(fresh.get("user_id", ""))
    return (saved.get("user_id") == uid and isinstance(saved.get("owner"), str)
            and _person(saved["owner"], uid) == _person(str(fresh.get("owner", "")), uid))


def _free_for(path: Path, fresh: dict) -> bool:
    """Nothing there (or a damaged file), or this same agent's own sign-in."""
    saved = read_saved(path)
    return not saved.get("user_id") or same_agent(saved, fresh)


def _unreadable(path: Path) -> bool:
    """There, but can't be read: whose it is can't be told, so it is never replaced (GPT review:
    read_saved gives {} and os.replace would still overwrite it)."""
    try:
        path.read_bytes()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return False


def _refuse(where: Path) -> SystemExit:
    return SystemExit(f"✗ Another agent already uses {where} on this computer, so nothing was replaced and this "
                      "agent wasn't set up. Please tell @pm.")


def place(home: Path, folder: Path, fresh: dict) -> Path:
    """The file a new pairing `fresh` is saved in. `folder` is where it belongs (connect.agent_dir):
    ~/.hagency for a first agent. If another agent's sign-in is there, its own folder
    ~/.hagency/<its name>/ instead (where an earlier pairing may already have put it). An agent of
    the same name from another server is refused: both would get one service name, so the first
    one's would be replaced (GPT review)."""
    own = home / agent_name(fresh) / FILE
    for there in (home / FILE, own):
        saved = read_saved(there)
        if _unreadable(there) or (agent_name(saved) == agent_name(fresh) and not same_agent(saved, fresh)):
            raise _refuse(there)
    path = folder / FILE
    if folder != home:                        # a further agent: always its own folder
        if not _free_for(path, fresh):
            raise _refuse(path)
        return path
    if same_agent(read_saved(own), fresh):
        return own                            # moved aside before: it stays there
    if _free_for(path, fresh):
        return path
    return own                                # free: the check above found no other agent there


def kept_aside(home: Path, path: Path) -> bool:
    """Is this sign-in in an agent's own folder (a further agent, or one moved aside)?"""
    return path.parent != home


def name_arg(home: Path, path: Path) -> str:
    """What to add to connect.py's command line to reach this agent again."""
    return f"--name {path.parent.name} " if kept_aside(home, path) else ""


def _service(path: Path, how: str) -> tuple[str | None, list[str]]:
    """(its HAGENCY_CREDENTIALS or None, its command line), from a launchd plist or systemd unit."""
    if how == "launchd":
        job = plistlib.loads(path.read_bytes())
        env = job.get("EnvironmentVariables", {}) if isinstance(job, dict) else None
        argv = job.get("ProgramArguments", []) if isinstance(job, dict) else None
        words = [*argv, *env.values()] if isinstance(env, dict) and isinstance(argv, list) else [None]
        if not all(isinstance(w, str) for w in words):
            raise ValueError(f"{path.name} isn't shaped like a service file")   # skipped by stranded()
        return env.get("HAGENCY_CREDENTIALS"), argv
    env, argv = None, []
    for line in path.read_text().splitlines():
        key, _, value = line.partition("=")
        words = [w.replace("%%", "%").replace("$$", "$") for w in shlex.split(value)] if value else []
        if key == "ExecStart":
            argv = words
        elif key == "Environment" and words and words[0].startswith("HAGENCY_CREDENTIALS="):
            env = words[0].partition("=")[2]
    return env, argv


def _owner_arg(argv: list[str]) -> str | None:
    """The person a bridge.py service runs for (its first argument); None for other launchers."""
    at = next((i for i, a in enumerate(argv) if Path(a).name == LAUNCHER), None)
    return argv[at + 1] if at is not None and at + 1 < len(argv) else None


def _replaced(path: Path, how: str, first: Path, creds: dict, default: Path) -> bool:
    """Does this service read the first agent's file, and is the sign-in there not its own?"""
    env, argv = _service(path, how)
    if Path(env or default).resolve() != first.resolve():
        return False                          # its own folder, or another installation's
    owner = _owner_arg(argv)
    return (service_migration.agent_of(path.name) != agent_name(creds)
            or (owner is not None and _person(owner, creds["user_id"]) != _person(creds["owner"], creds["user_id"])))


def stranded(home: Path, skip: str = "", user_home: Path | None = None) -> list[str]:
    """The first agents here whose service reads ~/.hagency/credentials.json but whose sign-in in it
    was replaced by another agent's (by a connect.py before this check). `skip`: the one just paired."""
    first = home / FILE
    creds = read_saved(first)
    how = agent_service.kind()
    if not (isinstance(creds.get("user_id"), str) and isinstance(creds.get("owner"), str)) or how not in (
            "launchd", "systemd"):
        return []
    user_home = user_home or Path.home()
    default = Path(os.environ.get("HAGENCY_DIR", user_home / ".hagency")) / FILE   # older services set none
    folder = agent_service.service_file(how, "x", user_home).parent
    found = []
    for path in sorted(folder.glob(f"{agent_service.LABEL}.*.plist" if how == "launchd" else "hagency-agent-*.service")):
        agent = service_migration.agent_of(path.name)
        try:
            if agent != skip and _replaced(path, how, first, creds, default):
                found.append(agent)
        except (OSError, ValueError, ExpatError):   # unreadable (bad XML, quoting): not ours to judge, and
            continue                          # the migration just before this names the files it couldn't read
    return found
