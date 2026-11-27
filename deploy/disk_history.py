"""The disk's daily history, for guard.py: what grew, and by how much.

Each run appends one JSON line to state/disk-history.jsonl (root only, never mounted into a container):
the date, the disk's used and total bytes, and the size of each area below. 90 days are kept.
Only measures: nothing is deleted, and the sandbox is read for its total size only (du -s).
"""
import datetime
import json
import shutil
import subprocess
from pathlib import Path

import hostfiles

HERE = Path(__file__).resolve().parent
HOME = HERE.parents[2]                                    # ~/hagency/app/deploy -> the ubuntu user's home
HISTORY = HERE / "state" / "disk-history.jsonl"
KEEP_DAYS = 90
GB = 10 ** 9
DAILY_GROWTH_WARN = 3 * GB
TOP_CONTRIBUTORS = 3
DU_TIMEOUT = 120                                          # seconds per folder: six folders, 12 min at worst
MAX_BYTES = 2 ** 60                                       # a figure no disk here reaches: a damaged row
AREAS = {                                                 # label in the message -> the folders it adds up
    "sandbox": [HOME / "sandbox"],
    "Docker images": [Path("/var/lib/containerd"), Path("/var/lib/docker")],
    "system": [Path("/usr")],
    "hagency": [HOME / "hagency"],
    "logs": [Path("/var/log")],                           # the journal lives in /var/log/journal
}


def du(path: Path) -> int:
    """Bytes used under `path` on its own filesystem; 0 for a folder that isn't there."""
    if not path.exists():
        return 0
    out = subprocess.run(["du", "-sx", "--block-size=1", str(path)], capture_output=True, text=True,
                         errors="replace", timeout=DU_TIMEOUT)          # file names need not be UTF-8
    try:
        if out.returncode == 0:                           # a partial total would show as false growth tomorrow
            return int(out.stdout.split()[0])
    except (IndexError, ValueError):
        pass
    raise OSError(f"du {path}: {out.stderr.strip()[:200] or f'exit {out.returncode}'}")


def measure_areas() -> tuple[dict[str, int | None], list[str]]:
    """Each area's bytes (None if du failed) and what failed; one failure never stops the others."""
    sizes, failed = {}, []
    for label, paths in AREAS.items():
        try:
            sizes[label] = sum(du(p) for p in paths)
        except (OSError, subprocess.TimeoutExpired) as e:
            sizes[label] = None
            failed.append(f"{label} ({'timed out' if isinstance(e, subprocess.TimeoutExpired) else e})")
    return sizes, failed


def valid(entry: object) -> bool:
    """A whole row: a date, used and total bytes, and each area's bytes (or None)."""
    def size(v: object) -> bool:
        return isinstance(v, int) and 0 <= v < MAX_BYTES

    try:                                                  # dates sort as text: only the YYYY-MM-DD spelling
        return (datetime.date.fromisoformat(entry["date"]).isoformat() == entry["date"]
                and size(entry["used"]) and size(entry["total"]) and entry["total"] > 0
                and all(v is None or size(v) for v in entry["sizes"].values()))
    except (TypeError, KeyError, ValueError, AttributeError):
        return False


def load(path: Path | None = None) -> list[dict]:
    """The saved entries; a damaged row is skipped (the next write drops it), not fatal. Only a
    regular file is read (never a link or a FIFO); anything else is an OSError."""
    path = path or HISTORY                                # looked up at call time (tests point it elsewhere)
    if not path.exists() and not path.is_symlink():
        return []
    if path.parent.is_symlink() or path.is_symlink() or not path.is_file():
        raise OSError(f"{path} isn't a regular file in a real folder")
    entries = []
    for line in path.read_text(errors="replace").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if valid(entry):
            entries.append(entry)
    return entries


def trim(entries: list[dict], today: datetime.date) -> list[dict]:
    """The last KEEP_DAYS days, one entry per date (a second run that day replaces the first), oldest first."""
    oldest = today - datetime.timedelta(days=KEEP_DAYS)
    by_date = {e["date"]: e for e in entries if oldest < datetime.date.fromisoformat(e["date"]) <= today}
    return [by_date[d] for d in sorted(by_date)]


def save(entries: list[dict], path: Path | None = None) -> None:
    path = path or HISTORY
    path.parent.mkdir(mode=0o700, exist_ok=True)
    if path.parent.is_symlink():
        raise OSError(f"{path.parent} is a link")
    hostfiles.replace(path, "".join(json.dumps(e, sort_keys=True) + "\n" for e in entries), 0o600)


def before(entries: list[dict], day: datetime.date) -> dict | None:
    """The latest entry on or before `day`."""
    older = [e for e in entries if datetime.date.fromisoformat(e["date"]) <= day]
    return older[-1] if older else None


def contributors(now: dict, then: dict) -> list[tuple[str, int]]:
    """Areas that changed, biggest change first (either way), the ones that round to 0.0 GB left out."""
    changes = [(label, size - then["sizes"][label]) for label, size in now["sizes"].items()
               if size is not None and then.get("sizes", {}).get(label) is not None]
    changes = [c for c in changes if round(c[1] / GB, 1) != 0]
    return sorted(changes, key=lambda c: abs(c[1]), reverse=True)[:TOP_CONTRIBUTORS]


def gb(n: int, sign: bool = True) -> str:
    return f"{n / GB:+.1f} GB" if sign else f"{n / GB:.0f} GB"


def change_text(now: dict, then: dict, since: str = "") -> str:
    """'+2.1 GB{since}: sandbox +1.8 GB, Docker images +0.3 GB'"""
    parts = ", ".join(f"{label} {gb(d)}" for label, d in contributors(now, then))
    return gb(now["used"] - then["used"]) + since + (f": {parts}" if parts else "")


def period(then: dict, today: datetime.date) -> str:
    """'This week', or the real start when the figure from a week ago is missing ('Since Nov 2')."""
    start = datetime.date.fromisoformat(then["date"])
    return "This week" if start == today - datetime.timedelta(days=7) else f"Since {start:%b} {start.day}"


def notes(entries: list[dict], today: datetime.date, over: bool) -> list[str]:
    """What @pm tells the operator about the disk today (nothing on a quiet day). `entries` ends with today's."""
    now = entries[-1]
    lines = []
    week_ago = before(entries, today - datetime.timedelta(days=7))
    week = f"{period(week_ago, today)} {change_text(now, week_ago)}." if week_ago else "No figure from a week ago yet."
    if over:
        lines.append(week)                                # after guard's "disk is 81% full" warning
    elif today.weekday() == 0:                            # Mondays: the weekly summary
        lines.append(f"Disk {now['used'] / now['total']:.0%} ({gb(now['used'], sign=False)}). {week}")
    day_before = (today - datetime.timedelta(days=1)).isoformat()
    yesterday = next((e for e in entries if e["date"] == day_before), None)
    if yesterday and now["used"] - yesterday["used"] > DAILY_GROWTH_WARN:
        lines.append(f"⚠️ The disk grew {change_text(now, yesterday, ' since yesterday')}.")
    return lines


def record(today: datetime.date, over: bool) -> tuple[list[str], list[str]]:
    """Measure and save today's entry: (lines for @pm, problems). Never raises; a problem is a line
    for the journal, and goes to the operator only with a message that is sent anyway."""
    usage = shutil.disk_usage("/")
    sizes, failed = measure_areas()
    entry = {"date": today.isoformat(), "used": usage.used, "total": usage.total, "sizes": sizes}
    print("disk areas: " + ", ".join(f"{k} {'?' if v is None else gb(v, sign=False)}" for k, v in sizes.items()))
    problems = [f"Couldn't measure: {', '.join(failed)}."] if failed else []
    try:
        saved = load()
    except OSError as e:                                  # today's figures still count; nothing is overwritten
        return notes([entry], today, over), problems + [f"Couldn't read the disk history: {e}"]
    entries = trim(saved + [entry], today)
    try:
        save(entries)
    except OSError as e:                                  # today's messages still go out
        problems.append(f"Couldn't save the disk history: {e}")
    return notes(entries, today, over), problems
