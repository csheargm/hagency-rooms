"""Your agent as a background service on your own computer, so it doesn't need an open Terminal.

connect.py installs it after pairing: on macOS a launchd LaunchAgent
(~/Library/LaunchAgents/xyz.hagencylabs.agent.<localpart>.plist), on Linux a systemd --user unit
(~/.config/systemd/user/hagency-agent-<localpart>.service). It starts at login, restarts if it
crashes, logs to ~/.hagency/logs/, and reads the sign-in from ~/.hagency/credentials.json: no secret
is ever on a command line or in the service file. It stays stopped after a clean exit (the server
disconnected the agent). `connect.py --stop` removes it. Neither available: connect.py runs it in
the foreground as before. On Windows, connect.ps1 registers the Scheduled Task "Hagency agent
<name>" (running connect_win.py; it needs PowerShell's task cmdlets): here it is only started again
and, on --stop, ended and removed, with its Startup-folder fallback.
"""
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path, PureWindowsPath
from xml.sax.saxutils import escape

from common import paths

LABEL = "xyz.hagencylabs.agent"
RESTART_S = 30
WINDOWS = "schtasks"
WINDOWS_KEEPERS = ("connect_win.py", "bridge.py")   # what a Python of ours on Windows runs
# TODO(remove after 2027-02-01): keepers started before the launchers moved into bin/
# (docs/adr/0012-launchers-in-bin.md) name WINDOWS_KEEPERS at the installation's root: keeper_paths()
# still recognises them; client/service_migration.py rewrites the old services.

# launchctl bootout returns before the job is gone; a bootstrap meanwhile fails with error 5
GONE_TIMEOUT_S = 10.0                         # how long to wait for a booted-out job to go
GONE_POLL_S = 0.25
BOOTSTRAP_TRIES = 3
BOOTSTRAP_RETRY_S = 1.0
LAUNCHD_BUSY = 5                              # "Bootstrap failed: 5: Input/output error"
# how a reload is run: its output as text, never undecodable bytes that would skip the put-back (GPT review)
MANAGER_OUTPUT = {"capture_output": True, "text": True, "errors": "replace"}


def windows_task(agent_localpart: str) -> str:
    """The Scheduled Task (and Startup shortcut) name: "Hagency agent anna" for anna-agent."""
    return f"Hagency agent {agent_localpart.removesuffix('-agent')}"


def label(agent_localpart: str) -> str:
    return f"{LABEL}.{agent_localpart}"


def keeper_paths(app: Path) -> list[Path]:
    """The files a Python running this installation's Windows keeper or agent adapter names: in bin/,
    or at the root for one started before the update (TODO above)."""
    return [p for n in WINDOWS_KEEPERS for p in (paths.launcher(app, n), app / n)]


def command(app: Path, owner: str, backend: str) -> list[str]:
    """The agent adapter running this kind's CLI; for hermes, hermes_local.py (its two gateways, ADR 0008)."""
    if backend == "hermes":
        return [sys.executable, str(paths.launcher(app, "hermes_local.py"))]
    return [sys.executable, str(paths.launcher(app, "bridge.py")), owner, "--agent", backend]


def plist(app: Path, owner: str, backend: str, agent: str, creds: Path, logs: Path) -> str:
    args = "".join(f"<string>{escape(a)}</string>" for a in command(app, owner, backend))
    env = {"HAGENCY_CREDENTIALS": str(creds), "PATH": os.environ.get("PATH", "/usr/bin:/bin")}   # where codex/claude are
    env_xml = "".join(f"<key>{escape(k)}</key><string>{escape(v)}</string>" for k, v in env.items())
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
            '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n<plist version="1.0"><dict>'
            f"<key>Label</key><string>{escape(label(agent))}</string>"
            f"<key>ProgramArguments</key><array>{args}</array>"
            f"<key>WorkingDirectory</key><string>{escape(str(app))}</string>"
            f"<key>EnvironmentVariables</key><dict>{env_xml}</dict>"
            "<key>RunAtLoad</key><true/><key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>"
            f"<key>ThrottleInterval</key><integer>{RESTART_S}</integer>"
            f"<key>StandardOutPath</key><string>{escape(str(logs / f'{agent}.log'))}</string>"
            f"<key>StandardErrorPath</key><string>{escape(str(logs / f'{agent}.log'))}</string>"
            "</dict></plist>\n")


def _quote(a: str) -> str:
    """One systemd argument: it expands %specifiers and $VARIABLES even inside quotes, so both are doubled."""
    return '"' + a.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$") + '"'


def unit(app: Path, owner: str, backend: str, agent: str, creds: Path, logs: Path) -> str:
    return ("[Unit]\nDescription=Hagency Rooms agent " + agent + "\nAfter=network-online.target\n\n[Service]\n"
            f"ExecStart={' '.join(_quote(a) for a in command(app, owner, backend))}\n"
            f"WorkingDirectory={app}\nEnvironment={_quote('HAGENCY_CREDENTIALS=' + str(creds))}\n"
            f"Environment={_quote('PATH=' + os.environ.get('PATH', '/usr/bin:/bin'))}\n"
            f"Restart=on-failure\nRestartSec={RESTART_S}\nStandardOutput=append:{logs / (agent + '.log')}\n"
            f"StandardError=append:{logs / (agent + '.log')}\n\n[Install]\nWantedBy=default.target\n")


def kind() -> str | None:
    """"launchd", "systemd", "schtasks" or None (no per-user service manager here)."""
    if platform.system() == "Windows" and shutil.which("schtasks"):
        return WINDOWS
    if platform.system() == "Darwin" and shutil.which("launchctl"):
        return "launchd"
    if platform.system() == "Linux" and shutil.which("systemctl"):
        ok = subprocess.run(["systemctl", "--user", "is-system-running"], capture_output=True, text=True)
        return "systemd" if ok.stdout.strip() in ("running", "degraded", "starting") else None
    return None


def service_file(how: str, agent: str, home: Path | None = None) -> Path:
    home = home or Path.home()
    if how == "launchd":
        return home / "Library" / "LaunchAgents" / f"{label(agent)}.plist"
    return home / ".config" / "systemd" / "user" / f"hagency-agent-{agent}.service"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text)
    tmp.chmod(0o644)                          # names and paths only, never a secret
    os.replace(tmp, path)


def install(app: Path, owner: str, backend: str, agent: str, creds: Path, run=subprocess.run,
            sleep=time.sleep, clock=time.monotonic) -> str | None:
    """Install (or replace) and start the service; the service manager's name, or None if none."""
    how = kind()
    if how is None:
        return None
    if how == WINDOWS:                        # made by connect.ps1; none yet: run it here
        task = windows_task(agent)
        if run(["schtasks", "/Query", "/TN", task], capture_output=True).returncode != 0:
            return None
        r = run(["schtasks", "/Run", "/TN", task], capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"Task Scheduler wouldn't start it: {(r.stderr or r.stdout or '').strip()[:200]}")
        return how
    logs = creds.parent / "logs"
    logs.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = service_file(how, agent)
    if how == "launchd":                      # an older one is replaced
        _write(path, plist(app, owner, backend, agent, creds, logs))
        r = load_again(path, label(agent), run, sleep, clock)
    else:
        _write(path, unit(app, owner, backend, agent, creds, logs))
        r = run(["systemctl", "--user", "daemon-reload"], capture_output=True, text=True)
        for step in (["enable", "--now"], ["restart"]):   # restart: a new sign-in or CLI in a running one
            if r.returncode == 0:
                r = run(["systemctl", "--user", *step, "--", path.name], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"{how} wouldn't start it: {(r.stderr or r.stdout or '').strip()[:200]}")
    return how


def _ours(pid: str, app: Path, run) -> bool:
    """Is this PID still a Python running this installation's keeper or agent adapter? (A stale agent.pid
    may name a reused number: only a command line naming our own files counts.)"""
    ask = (f"$p = Get-CimInstance Win32_Process -Filter 'ProcessId = {int(pid)}'; "
           '"$($p.ExecutablePath)|$($p.CommandLine)"')
    out = run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ask], capture_output=True, text=True).stdout
    exe, _, line = out.strip().lower().partition("|")
    return (PureWindowsPath(exe).name.startswith("python")      # the program itself, not text in its arguments
            and any(str(f).lower() in line for f in keeper_paths(app)))


def _stop_windows(agent: str, home: Path, run, app: Path) -> bool:
    """End and remove the task and the Startup shortcut; end a keeper started from Startup."""
    task = windows_task(agent)
    existed = run(["schtasks", "/Query", "/TN", task], capture_output=True).returncode == 0
    if existed:
        run(["schtasks", "/End", "/TN", task], capture_output=True)
        run(["schtasks", "/Delete", "/TN", task, "/F"], capture_output=True)
    startup = Path(os.environ.get("APPDATA", home)) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    link = startup / f"{task}.lnk"
    existed |= link.exists()
    link.unlink(missing_ok=True)
    pids = home / "agent.pid"
    try:
        numbers = pids.read_text().split()
    except OSError:                           # none, or the keeper just removed it on its way out
        numbers = []
    ended = [run(["taskkill", "/PID", pid, "/T", "/F"], capture_output=True).returncode == 0
             for pid in numbers if pid.isdigit() and _ours(pid, app, run)]
    if all(ended):                            # kept while one of ours survived, for the next stop
        pids.unlink(missing_ok=True)
    return existed


def stop(agent: str, run=subprocess.run, home: Path | None = None, app: Path | None = None) -> bool:
    """Stop and remove the service; True if there was one. Windows: `home` is the sign-in folder,
    `app` the installation whose keeper may be ended."""
    how = kind()
    if how is None:
        return False
    if how == WINDOWS:
        return _stop_windows(agent, home or Path(os.environ.get("HAGENCY_DIR", Path.home() / ".hagency")), run,
                             app or paths.ROOT.resolve())
    path = service_file(how, agent)
    if how == "launchd":
        run(["launchctl", "bootout", f"gui/{os.getuid()}/{label(agent)}"], capture_output=True)
    else:
        run(["systemctl", "--user", "disable", "--now", "--", path.name], capture_output=True)
    existed = path.exists()
    path.unlink(missing_ok=True)
    return existed


def _wait_gone(target: str, run, sleep, clock, end: float) -> None:
    """Wait, until `end` at most, until launchd no longer knows the job (gui/<uid>/<label>)."""
    while run(["launchctl", "print", target], capture_output=True).returncode == 0 and clock() < end:
        sleep(GONE_POLL_S)


def _busy(r) -> bool:
    """launchd refused a load because the old job is still being torn down."""
    return r.returncode == LAUNCHD_BUSY or "Input/output error" in (r.stderr or "")


def load_again(path: Path, job: str, run=subprocess.run, sleep=time.sleep, clock=time.monotonic):
    """Unload the launchd job (if loaded), wait until it is gone, and load its file again; launchd keeps
    the arguments it loaded. bootout returns before the job is gone, so the wait (GONE_TIMEOUT_S in all,
    for every try) comes first and a load refused while it is still going is tried again."""
    domain = f"gui/{os.getuid()}"
    target = f"{domain}/{job}"
    run(["launchctl", "bootout", target], capture_output=True)
    end = clock() + GONE_TIMEOUT_S
    for attempt in range(BOOTSTRAP_TRIES):
        if attempt:
            sleep(BOOTSTRAP_RETRY_S)
        _wait_gone(target, run, sleep, clock, end)
        r = run(["launchctl", "bootstrap", domain, str(path)], **MANAGER_OUTPUT)
        if not _busy(r):
            break
    return r
