"""Keep your agent running on Windows: what the Scheduled Task "Hagency agent <name>" runs.

  pythonw bin/connect_win.py [--home DIR]  # run the agent hidden, and again whenever it fails
  python bin/connect_win.py --task-name    # print the task's name (connect.ps1 uses it)

connect.ps1 has connect.py save the sign-in first (--pair-only), then registers this to run at
logon: the Windows counterpart of agent_service.py (launchd / systemd on macOS / Linux). It reads
that sign-in (~/.hagency/credentials.json), runs the agent adapter as a hidden child with
HAGENCY_CREDENTIALS pointing at the file (so no secret is ever on a command line), and starts it
again when it fails, waiting longer each time it fails quickly. A clean exit (the server
disconnected the agent) stops it, as with the other services. Only one runs per sign-in folder
(agent.lock), and everything it starts ends with it (a Windows job), so a stopped or restarted
task never leaves a second agent adapter behind. Output goes to ~/.hagency/logs/<agent>.log.
"""
import argparse
import ctypes
import os
import subprocess
import sys
import time
from pathlib import Path

from client import agent_service
from common import bridge_config
from common import paths

APP = paths.ROOT.resolve()
FIRST_DELAY_S = 5
MAX_DELAY_S = 300
HEALTHY_RUN_S = 3600                          # ran this long: the next failure starts the backoff over
LOG_MAX_BYTES = 5 * 1024 * 1024
CREATE_NO_WINDOW = 0x08000000                 # Windows: the agent adapter gets no console window
JOB_KILL_ON_CLOSE = 0x2000                    # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
JOB_EXTENDED_LIMITS = 9                       # JobObjectExtendedLimitInformation


class _JobLimits(ctypes.Structure):
    """JOBOBJECT_EXTENDED_LIMIT_INFORMATION (basic limits, I/O counters, memory limits)."""
    _fields_ = [("user_time", ctypes.c_int64), ("job_user_time", ctypes.c_int64), ("flags", ctypes.c_uint32),
                ("min_ws", ctypes.c_size_t), ("max_ws", ctypes.c_size_t), ("processes", ctypes.c_uint32),
                ("affinity", ctypes.c_size_t), ("priority", ctypes.c_uint32), ("scheduling", ctypes.c_uint32),
                ("io", ctypes.c_uint64 * 6), ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t)]


def tie_children_to_me() -> None:
    """Windows: put this process in a job that ends everything it starts (the agent adapter, its CLI) when
    this process ends, however it ends. Raises OSError if Windows refuses."""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = ctypes.c_void_p
    k32.GetCurrentProcess.restype = ctypes.c_void_p
    k32.SetInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    k32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    job = k32.CreateJobObjectW(None, None)    # the handle stays open for this process's life, on purpose
    limits = _JobLimits(flags=JOB_KILL_ON_CLOSE)
    if not (job and k32.SetInformationJobObject(job, JOB_EXTENDED_LIMITS, ctypes.byref(limits), ctypes.sizeof(limits))
            and k32.AssignProcessToJobObject(job, k32.GetCurrentProcess())):
        raise OSError(ctypes.get_last_error(), "couldn't tie the agent to this process")


def tie_or_say(home: Path, tie=tie_children_to_me) -> None:
    """The job is a safety net (the lock already keeps one keeper): if Windows refuses it, the agent
    still runs, and the log says a stopped task may leave its agent adapter running until it exits."""
    try:
        tie()
    except OSError as e:
        try:
            with open_log(home / "logs" / "connect_win.log") as log:
                log.write(f"[connect_win] no job for the agent ({e}); stopping this may leave the agent adapter running\n".encode())
        except OSError:
            pass                              # can't even say so: running the agent matters more than the note


def hold_lock(path: Path):
    """The open lock file while this is the only keeper for this sign-in folder, else None."""
    f = open(path, "a+b")
    try:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:                                 # only for tests on macOS / Linux
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:                           # another keeper holds it: the caller stops quietly
        f.close()
        return None
    return f


def agent_name(creds: dict) -> str:
    """"anna-agent", from "@anna-agent:server" (connect.py checked it is [a-z0-9-])."""
    return creds["user_id"].lstrip("@").partition(":")[0]


def task_name(creds: dict) -> str:
    return agent_service.windows_task(agent_name(creds))


def child_python() -> str:
    """python.exe beside pythonw.exe (the agent adapter's output must go somewhere); else this one."""
    exe = Path(sys.executable)
    console = exe.with_name("python.exe")
    return str(console) if exe.name.lower() == "pythonw.exe" and console.exists() else str(exe)


def bridge_command(creds: dict, creds_path: Path) -> tuple[list[str], dict]:
    """argv and environment for the agent adapter: the sign-in travels by file path, never in argv."""
    argv = [child_python(), str(paths.launcher(APP, "bridge.py")), creds["owner"], "--agent", creds.get("agent") or "codex"]
    # utf-8: its log is a file, which on Windows would otherwise be cp1252 and choke on a check mark
    return argv, {**os.environ, "HAGENCY_CREDENTIALS": str(creds_path), "PYTHONIOENCODING": "utf-8"}


def next_delay(delay: float, ran_s: float) -> float:
    """How long to wait before starting again: doubles on quick failures, resets after a good run."""
    if ran_s >= HEALTHY_RUN_S:
        return FIRST_DELAY_S
    return min(MAX_DELAY_S, max(FIRST_DELAY_S, delay * 2))


def open_log(path: Path):
    """The agent's log, with the previous one kept as <log>.1 once it grows past LOG_MAX_BYTES."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size > LOG_MAX_BYTES:
        os.replace(path, path.with_name(path.name + ".1"))
    return open(path, "ab")


def open_log_or_nothing(path: Path):
    """A log that can't be opened (full disk, no permission) must not keep the agent from running."""
    try:
        return open_log(path)
    except OSError:
        return open(os.devnull, "ab")


def keep_running(home: Path, runs: int | None = None, spawn=subprocess.Popen, sleep=time.sleep,
                 clock=time.monotonic) -> None:
    """Run the agent adapter, and again each time it fails (`runs` limits it, for tests)."""
    creds_path = home / "credentials.json"
    delay = 0.0
    while runs is None or runs > 0:
        creds = bridge_config.load_credentials(str(creds_path))   # each time: a new code may have replaced it
        argv, env = bridge_command(creds, creds_path)
        started = clock()
        with open_log_or_nothing(home / "logs" / f"{agent_name(creds)}.log") as log:
            proc = spawn(argv, cwd=str(APP), env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                         creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0)
            (home / "agent.pid").write_text(f"{os.getpid()}\n{proc.pid}\n")
            code = proc.wait()
            if code == 0:                     # disconnected by the server (bridge.py): stay stopped
                log.write(b"[connect_win] the agent stopped cleanly; run the connect line again to restart it\n")
                return
            delay = next_delay(delay, clock() - started)
            log.write(f"[connect_win] the agent stopped (exit {code}); starting again in {delay:.0f}s\n".encode())
        if runs is not None:
            runs -= 1
        sleep(delay)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--home", type=Path, default=Path(os.environ.get("HAGENCY_DIR", Path.home() / ".hagency")),
                    help="where connect.py keeps the sign-in (default ~/.hagency)")
    ap.add_argument("--task-name", action="store_true", help="print the Scheduled Task's name and stop")
    args = ap.parse_args()
    home = args.home.resolve()
    if args.task_name:
        print(task_name(bridge_config.load_credentials(str(home / "credentials.json"))))
        return
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = hold_lock(home / "agent.lock")
    if lock is None:
        return                                # another keeper already runs this agent: nothing to do
    with lock:                                # held for as long as this keeper runs
        try:
            if os.name == "nt":
                tie_or_say(home)
            keep_running(home)
        except (SystemExit, OSError) as e:    # pythonw has no console: say why in the log, then stop
            with open_log_or_nothing(home / "logs" / "connect_win.log") as log:
                log.write(f"[connect_win] {e}\n".encode())
            raise SystemExit(1) from e
        finally:
            (home / "agent.pid").unlink(missing_ok=True)   # no stale numbers for a later stop


if __name__ == "__main__":
    main()
