"""Daily check against Oracle's Always Free limits (the hagency-guard systemd timer, from bootstrap.sh).

  python3 guard.py                  # in ~/hagency/app/deploy, as root
  python3 guard.py unit timer       # the systemd unit files bootstrap.sh installs (timer, service)

Disk over 80%, or this month's outbound traffic over 1 TB: @pm warns the operator (OPERATOR in .env)
in a private chat. Over 5 TB (half of the free 10 TB): media downloads are paused for the rest of the
month (flags/media-paused, which Caddy checks on every request) and resume by themselves next month.
Traffic comes from vnstat, which counts across reboots. Every finding also goes to the journal.
The disk's history (disk_history.py) adds the week's growth and what grew: with the 80% warning, every
Monday, and at once when the disk grew more than 3 GB in a day. It runs at 05:00 Pacific time.
"""
import datetime
import json
import shutil
import subprocess
import sys
from pathlib import Path

import disk_history

HERE = Path(__file__).resolve().parent
FLAG = HERE / "flags" / "media-paused"
DISK_WARN = 0.80
TB = 10 ** 12
TRAFFIC_WARN = 1 * TB
TRAFFIC_PAUSE = 5 * TB
SCHEDULE = "*-*-* 05:00:00 America/Los_Angeles"
ALERT = ["docker", "compose", "--env-file", ".env", "exec", "-T", "pm", ".venv/bin/python", "deploy/pm_alert.py"]


def default_iface(route_table: str) -> str | None:
    """The interface of the default route, from /proc/net/route (destination 00000000)."""
    for line in route_table.splitlines()[1:]:
        fields = line.split()
        if len(fields) > 1 and fields[1] == "00000000":
            return fields[0]
    return None


def month_tx(vnstat: dict, iface: str, today: datetime.date) -> int | None:
    """Bytes sent on `iface` this calendar month, from `vnstat --json m`; None when vnstat has no
    figure for it (not yet, or its database was reset). An interface vnstat doesn't watch is an error."""
    for entry in vnstat.get("interfaces", []):
        if entry.get("name") != iface:
            continue
        for month in entry.get("traffic", {}).get("month", []):
            if month.get("date", {}).get("year") == today.year and month["date"].get("month") == today.month:
                return int(month["tx"])
        return None
    raise SystemExit(f"✗ vnstat doesn't watch {iface}: sudo vnstat --add -i {iface}")


def paused_this_month(today: datetime.date) -> bool:
    """The flag names the month it was set in; a pause never outlives that month."""
    try:
        return FLAG.read_text().strip() == today.strftime("%Y-%m")
    except OSError:
        return False


def decide(disk_used: float, tx: int) -> tuple[list[str], bool]:
    """(warnings, pause media downloads?)"""
    warnings = []
    if disk_used > DISK_WARN:
        warnings.append(f"⚠️ The server's disk is {disk_used:.0%} full (warning above {DISK_WARN:.0%}).")
    pause = tx > TRAFFIC_PAUSE
    if pause:
        warnings.append(f"⚠️ {tx / TB:.2f} TB sent this month: media downloads are paused until next month "
                        f"(Oracle's free allowance is 10 TB).")
    elif tx > TRAFFIC_WARN:
        warnings.append(f"⚠️ {tx / TB:.2f} TB sent this month (warning above 1 TB; media pauses above 5 TB).")
    return warnings, pause


def unit_file(kind: str) -> str:
    """The systemd unit files: one source, so the tests read exactly what bootstrap.sh installs."""
    if kind == "service":
        return ("[Unit]\nDescription=Hagency Rooms: disk and traffic within Oracle's free limits\n"
                f"[Service]\nType=oneshot\nWorkingDirectory={HERE}\nExecStart=/usr/bin/python3 {HERE / 'guard.py'}\n")
    if kind == "timer":
        return ("[Unit]\nDescription=Hagency Rooms: daily free-tier check\n"
                f"[Timer]\nOnCalendar={SCHEDULE}\nPersistent=true\n[Install]\nWantedBy=timers.target\n")
    raise SystemExit(f"✗ no unit {kind!r}: service or timer")


def traffic() -> tuple[int | None, str | None]:
    """(bytes sent this month or None, why it couldn't be read: None when vnstat just has no figure yet)."""
    try:
        iface = default_iface(Path("/proc/net/route").read_text())
        out = subprocess.run(["vnstat", "--json", "m"], capture_output=True, text=True, timeout=60)
        if out.returncode != 0 or not iface:
            return None, f"can't read this month's traffic (vnstat: {out.stderr.strip()[:200]}, interface {iface})"
        return month_tx(json.loads(out.stdout), iface, datetime.date.today()), None
    except SystemExit as e:                   # month_tx: an interface vnstat doesn't watch; said in the alert
        return None, str(e).removeprefix("✗ ")
    except (OSError, ValueError, TypeError, KeyError, AttributeError, subprocess.TimeoutExpired) as e:   # no vnstat,
        return None, f"can't read this month's traffic ({type(e).__name__}: {str(e)[:200]})"   # bad JSON: in the alert


def measure() -> tuple[float, int | None, str | None]:
    """(disk use, this month's traffic or None, why traffic couldn't be read). The disk never waits on vnstat
    (GPT review: a vnstat failure skipped the disk history and its alerts)."""
    usage = shutil.disk_usage("/")
    return (usage.used / usage.total, *traffic())


def main(today: datetime.date | None = None) -> int:
    today = today or datetime.date.today()
    if FLAG.exists() and not paused_this_month(today):
        FLAG.unlink()                         # last month's pause ends even if measuring fails below
    disk_used, tx, unread = measure()
    if tx is None:                            # no figure: keep a pause set this month, don't invent zero
        warnings, pause = decide(disk_used, 0)
        pause = paused_this_month(today)
        warnings.append(f"⚠️ Traffic not checked: {unread}." if unread else
                        "⚠️ vnstat has no traffic figure for this month; check `vnstat -m` on the server.")
        tx = 0
    else:
        warnings, pause = decide(disk_used, tx)
    FLAG.parent.mkdir(exist_ok=True)
    if pause:
        FLAG.write_text(today.strftime("%Y-%m"))
    else:
        FLAG.unlink(missing_ok=True)
    print(f"disk {disk_used:.0%}, sent this month {tx / TB:.3f} TB, media {'paused' if pause else 'on'}")
    disk_notes, problems = disk_history.record(today, over=disk_used > DISK_WARN)
    for problem in problems:
        print(f"⚠ {problem}")
    if not warnings and not disk_notes:
        return 0
    text = "\n".join(warnings + disk_notes + problems)
    print(text)
    sent = subprocess.run(ALERT, cwd=HERE, input=text, text=True, timeout=120)
    return 0 if sent.returncode == 0 and not unread else 1   # a warning nobody got, or no traffic: the run shows failed


if __name__ == "__main__":
    if sys.argv[1:2] == ["unit"] and len(sys.argv) == 3:
        print(unit_file(sys.argv[2]), end="")
        sys.exit(0)
    sys.exit(main())
