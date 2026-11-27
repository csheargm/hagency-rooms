"""Unit tests for disk_history.py and guard.py's disk messages and timer.
Run from deploy/: python3 -m unittest discover -p 'test_*.py'"""
import datetime
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import disk_history as dh
import guard

GB = dh.GB
MON = datetime.date(2026, 11, 23)
TUE = MON + datetime.timedelta(days=1)


def entry(day: datetime.date, used_gb: float, **sizes_gb: float | None) -> dict:
    sizes = {label: None if v is None else int(v * GB) for label, v in sizes_gb.items()}
    return {"date": day.isoformat(), "used": int(used_gb * GB), "total": 44 * GB, "sizes": sizes}


def days_ago(day: datetime.date, n: int) -> datetime.date:
    return day - datetime.timedelta(days=n)


class History(unittest.TestCase):
    def test_trim_keeps_90_days_and_one_entry_per_date(self):
        entries = [entry(days_ago(MON, n), 20) for n in (120, 91, 90, 89, 1)]
        entries.append({**entry(MON, 20), "used": 1})
        entries.append({**entry(MON, 20), "used": 2})                       # a second run today wins
        kept = dh.trim(entries, MON)
        self.assertEqual([e["date"] for e in kept], [days_ago(MON, 89).isoformat(), days_ago(MON, 1).isoformat(),
                                                     MON.isoformat()])
        self.assertEqual(kept[-1]["used"], 2)

    def test_save_and_load_round_trip_skipping_a_damaged_line(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state" / "disk-history.jsonl"
            dh.save([entry(MON, 20, sandbox=1)], path)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            path.write_text(path.read_text() + "{not json\n" + '{"date": "never"}\n')
            self.assertEqual(dh.load(path), [entry(MON, 20, sandbox=1)])
            self.assertEqual(dh.load(Path(d) / "missing"), [])

    def test_contributors_ranked_by_size_of_change_skipping_unknown_and_zero(self):
        then = entry(days_ago(MON, 7), 20, sandbox=5, logs=1, system=3, hagency=None, **{"Docker images": 8})
        now = entry(MON, 22.1, sandbox=6.8, logs=0.5, system=3.01, hagency=2, **{"Docker images": 8.3})
        self.assertEqual([label for label, _ in dh.contributors(now, then)], ["sandbox", "logs", "Docker images"])
        now["sizes"]["sandbox"] = None                                      # du failed today: left out
        self.assertNotIn("sandbox", dict(dh.contributors(now, then)))


class Notes(unittest.TestCase):
    def week(self, today, used_now=26.1):
        return [entry(days_ago(today, 8), 23, sandbox=1, **{"Docker images": 9}),
                entry(days_ago(today, 7), 24, sandbox=2, **{"Docker images": 9}),
                entry(days_ago(today, 1), 25.9, sandbox=3.7, **{"Docker images": 9.2}),
                entry(today, used_now, sandbox=3.8, **{"Docker images": 9.3})]

    def test_monday_summary_with_the_weeks_growth(self):
        self.assertEqual(dh.notes(self.week(MON), MON, over=False),
                         ["Disk 59% (26 GB). This week +2.1 GB: sandbox +1.8 GB, Docker images +0.3 GB."])

    def test_a_quiet_weekday_says_nothing(self):
        self.assertEqual(dh.notes(self.week(TUE), TUE, over=False), [])

    def test_over_80_percent_any_day_adds_the_weeks_growth(self):
        self.assertEqual(dh.notes(self.week(TUE), TUE, over=True),
                         ["This week +2.1 GB: sandbox +1.8 GB, Docker images +0.3 GB."])

    def test_more_than_3_gb_in_a_day_is_reported_at_once(self):
        entries = self.week(TUE, used_now=29.0)
        entries[-1]["sizes"]["sandbox"] = int(6.8 * GB)
        self.assertEqual(dh.notes(entries, TUE, over=False),
                         ["⚠️ The disk grew +3.1 GB since yesterday: sandbox +3.1 GB, Docker images +0.1 GB."])
        del entries[2]                                                      # no figure from yesterday: no daily note
        self.assertEqual(dh.notes(entries, TUE, over=False), [])

    def test_a_new_history_says_so_on_monday(self):
        self.assertEqual(dh.notes([entry(MON, 26.1)], MON, over=False),
                         ["Disk 59% (26 GB). No figure from a week ago yet."])


class Measure(unittest.TestCase):
    def test_one_failed_du_never_stops_the_others(self):
        def run(cmd, **kw):
            self.assertEqual(cmd[:3], ["du", "-sx", "--block-size=1"])      # one total, one filesystem
            if cmd[-1].endswith("sandbox"):
                raise subprocess.TimeoutExpired(cmd, kw["timeout"])
            if cmd[-1] == "/usr":
                return mock.Mock(stdout="", stderr="du: broken", returncode=1)
            return mock.Mock(stdout=f"{GB}\t{cmd[-1]}\n", returncode=0)
        with mock.patch.object(dh.subprocess, "run", side_effect=run), \
                mock.patch.object(Path, "exists", return_value=True):
            sizes, failed = dh.measure_areas()
        self.assertEqual((sizes["sandbox"], sizes["system"], sizes["Docker images"]), (None, None, 2 * GB))
        self.assertEqual(failed, ["sandbox (timed out)", "system (du /usr: du: broken)"])

    def test_record_saves_today_and_reports_problems_without_raising(self):
        with tempfile.TemporaryDirectory() as d, mock.patch("builtins.print"), \
                mock.patch.object(dh, "HISTORY", Path(d) / "state" / "h.jsonl"), \
                mock.patch.object(dh, "measure_areas", return_value=({"sandbox": GB}, ["logs (timed out)"])):
            lines, problems = dh.record(MON, over=False)
            self.assertEqual(problems, ["Couldn't measure: logs (timed out)."])
            self.assertTrue(lines[0].startswith("Disk "))
            self.assertEqual(dh.load(dh.HISTORY)[0]["sizes"], {"sandbox": GB})
            with mock.patch.object(dh, "save", side_effect=OSError("read-only")):
                lines, problems = dh.record(MON, over=False)
            self.assertEqual((lines, problems[-1]), ([], "Couldn't save the disk history: read-only"))


class GuardDisk(unittest.TestCase):
    def setUp(self):
        mock.patch("builtins.print").start()
        self.addCleanup(mock.patch.stopall)
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        mock.patch.object(guard, "FLAG", Path(d.name) / "flags" / "media-paused").start()
        mock.patch.object(guard, "measure", return_value=(0.59, 0)).start()
        self.sent = mock.patch.object(guard.subprocess, "run", return_value=mock.Mock(returncode=0)).start()

    def test_a_disk_note_is_sent_with_any_measuring_problem(self):
        with mock.patch.object(guard.disk_history, "record", return_value=(["Disk 59% (26 GB)."], ["Couldn't measure: x."])):
            self.assertEqual(guard.main(MON), 0)
        self.assertEqual(self.sent.call_args.kwargs["input"], "Disk 59% (26 GB).\nCouldn't measure: x.")

    def test_a_measuring_problem_alone_stays_in_the_journal(self):
        with mock.patch.object(guard.disk_history, "record", return_value=([], ["Couldn't measure: x."])) as record:
            self.assertEqual(guard.main(TUE), 0)
        self.sent.assert_not_called()
        record.assert_called_once_with(TUE, over=False)


class Timer(unittest.TestCase):
    def test_daily_at_5_pacific_and_catches_up_after_downtime(self):
        self.assertEqual(guard.unit_file("timer"),
                         "[Unit]\nDescription=Hagency Rooms: daily free-tier check\n[Timer]\n"
                         "OnCalendar=*-*-* 05:00:00 America/Los_Angeles\nPersistent=true\n"
                         "[Install]\nWantedBy=timers.target\n")
        self.assertIn(f"ExecStart=/usr/bin/python3 {guard.HERE / 'guard.py'}\n", guard.unit_file("service"))
        with self.assertRaises(SystemExit):
            guard.unit_file("other")

    def test_bootstrap_installs_the_units_from_guard_and_restarts_the_timer(self):
        boot = (Path(__file__).parent / "bootstrap.sh").read_text()
        self.assertIn("python3 guard.py unit timer | sudo tee /etc/systemd/system/hagency-guard.timer", boot)
        self.assertIn("python3 guard.py unit service | sudo tee /etc/systemd/system/hagency-guard.service", boot)
        self.assertLess(boot.index("daemon-reload"), boot.index("restart hagency-guard.timer"))
        self.assertNotIn("OnCalendar", boot)                                # one source: guard.py


if __name__ == "__main__":
    unittest.main()
