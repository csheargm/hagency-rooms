"""Background services written before the launchers moved into bin/ (docs/adr/0012-launchers-in-bin.md)
run them from there again: connect.py calls migrate() first, on every run.

TODO(remove after 2027-02-01): this module and OLD_ROOT_LAUNCHERS, with the rest ADR 0012 lists.
"""
import subprocess
import time
from pathlib import Path
from xml.sax.saxutils import escape

from client import agent_service
from client.agent_service import LABEL, _quote, _write, load_again, service_file
from common import paths

OLD_ROOT_LAUNCHERS = ("bridge.py", "hermes_local.py")   # where older services name them: the root


class MigrationError(RuntimeError):
    """Some services couldn't be moved: `moved` the file names that were, `failed` {file name: why}."""
    def __init__(self, moved: list[str], failed: dict[str, str]):
        super().__init__("; ".join(f"{name}: {why}" for name, why in failed.items()))
        self.moved, self.failed = moved, failed


def agent_of(service_name: str) -> str:
    """The agent a service file is for: anna-agent for xyz.hagencylabs.agent.anna-agent.plist."""
    for start, end in ((f"{LABEL}.", ".plist"), ("hagency-agent-", ".service")):
        if service_name.startswith(start) and service_name.endswith(end):
            return service_name[len(start):-len(end)]
    return service_name


def _moved(text: str, app: Path, how: str) -> str:
    """The service file's text with each old root launcher of `app` named at its place in bin/."""
    token = (lambda p: f"<string>{escape(str(p))}</string>") if how == "launchd" else (lambda p: _quote(str(p)))
    for name in OLD_ROOT_LAUNCHERS:
        text = text.replace(token(app / name), token(paths.launcher(app, name)))
    return text


def _move_one(path: Path, app: Path, how: str, run, sleep, clock) -> str | None:
    """Rewrite one service file; launchd: load it again, the file put back as it was if that fails (so
    the next connect run tries it again, GPT review). The old text if rewritten, None if nothing to do."""
    text = path.read_text()
    new = _moved(text, app, how)
    if new == text:
        return None
    _write(path, new)
    if how == "launchd":
        try:
            _loaded(load_again(path, path.stem, run, sleep, clock), "launchd wouldn't load it again")
        except (OSError, RuntimeError) as e:
            raise RuntimeError(_put_back(path, text, e)) from e
    return text


def _put_back(path: Path, text: str, why: Exception) -> str:
    """Write the file's old text back, so the next run tries it again; why it failed, and if even
    that write failed, that too (GPT review: one unwritable file mustn't stop the others)."""
    try:
        _write(path, text)
    except OSError as e:
        return f"{why}; and it couldn't be put back as it was ({e})"
    return str(why)


def migrate(app: Path, run=subprocess.run, home: Path | None = None, sleep=time.sleep,
            clock=time.monotonic) -> list[str]:
    """Rewrite this installation's background services (every agent's, further ones too) that still
    run a launcher from the root, and load them again; the names of those rewritten. Windows: none
    here (connect.ps1 moves its task). One failing doesn't stop the others: each failed file is put
    back as it was, for the next connect run, and MigrationError names them all at the end."""
    how = agent_service.kind()          # looked up here, so tests can stand in for it
    if how not in ("launchd", "systemd"):
        return []
    folder = service_file(how, "x", home).parent
    pattern = f"{LABEL}.*.plist" if how == "launchd" else "hagency-agent-*.service"
    done, failed, pending = [], {}, {}        # pending (systemd): rewritten, reloaded together below
    for path in sorted(folder.glob(pattern)):
        try:
            old = _move_one(path, app, how, run, sleep, clock)
        except (OSError, RuntimeError, UnicodeDecodeError) as e:   # unreadable: GPT review
            failed[path.name] = str(e)
            continue
        if old is not None and how == "launchd":
            done.append(path.name)
        elif old is not None:
            pending[path] = old
    if pending:                               # systemd reads ExecStart at the next start
        try:
            _loaded(run(["systemctl", "--user", "daemon-reload"], capture_output=True, text=True), "systemd wouldn't reload")
            done += [path.name for path in pending]
        except (OSError, RuntimeError) as e:
            for path, text in pending.items():
                failed[path.name] = _put_back(path, text, e)
    if failed:
        raise MigrationError(done, failed)
    return done


def _loaded(r, what: str) -> None:
    if r.returncode != 0:
        raise RuntimeError(f"{what}: {(r.stderr or r.stdout or '').strip()[:200]}")
