"""Unit tests for disk_history.py and guard.py's disk messages and timer.
Run from deploy/: python3 -m unittest discover -p 'test_*.py'"""
import datetime
import json
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
            with path.open("ab") as f:                                      # GPT review: whole rows, any bytes
                f.write(b'{not json\n{"date": "never"}\n{"date": "2026-11-22", "sizes": {}}\n\xff\xfe\n'
                        b'{"date": "20261122", "used": 1, "total": 2, "sizes": {}}\n'          # sorts after today
                        b'{"date": "2026-11-21", "used": 1e400, "total": 2, "sizes": {}}\n'
                        b'{"date": "2026-11-21", "used": 99999999999999999999999, "total": 2, "sizes": {}}\n')
            self.assertEqual(dh.load(path), [entry(MON, 20, sandbox=1)])
            self.assertEqual(dh.load(Path(d) / "missing"), [])

    def test_a_linked_history_is_neither_read_nor_written_through(self):
        with tempfile.TemporaryDirectory() as d:
            victim = Path(d) / "victim"
            victim.write_text("keep")
            (Path(d) / "state").mkdir()
            path = Path(d) / "state" / "h.jsonl"
            path.symlink_to(victim)
            with self.assertRaises(OSError):
                dh.load(path)
            (Path(d) / "real").mkdir()
            (Path(d) / "real" / "h.jsonl").write_text("")
            (Path(d) / "via").symlink_to(Path(d) / "real")
            with self.assertRaises(OSError):
                dh.load(Path(d) / "via" / "h.jsonl")                        # a linked state folder too
            (Path(d) / "elsewhere").mkdir()
            (Path(d) / "linked").symlink_to(Path(d) / "elsewhere")
            with self.assertRaises(OSError):
                dh.save([entry(MON, 20)], Path(d) / "linked" / "h.jsonl")
            self.assertEqual((victim.read_text(), list((Path(d) / "elsewhere").iterdir())), ("keep", []))

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

    def test_a_missing_week_ago_figure_names_the_real_start(self):
        entries = self.week(MON)
        del entries[1]                                                      # 7 days ago is missing: 8 days ago is used
        self.assertEqual(dh.notes(entries, MON, over=False)[0],
                         "Disk 59% (26 GB). Since Nov 15 +3.1 GB: sandbox +2.8 GB, Docker images +0.3 GB.")

    def test_a_new_history_says_so_on_monday(self):
        self.assertEqual(dh.notes([entry(MON, 26.1)], MON, over=False),
                         ["Disk 59% (26 GB). No figure from a week ago yet."])


class Measure(unittest.TestCase):
    def test_one_failed_du_never_stops_the_others(self):
        def run(cmd, **kw):
            self.assertEqual(cmd[:3], ["du", "-sx", "--block-size=1"])      # one total, one filesystem
            if cmd[-1].endswith("sandbox"):
                raise subprocess.TimeoutExpired(cmd, kw["timeout"])
            self.assertEqual(kw["errors"], "replace")                       # any file name in stderr
            if cmd[-1] == "/usr":
                return mock.Mock(stdout="", stderr="du: broken", returncode=1)
            if cmd[-1] == "/var/log":                                       # a partial total is no figure
                return mock.Mock(stdout=f"{GB}\t/var/log\n", stderr="du: vanished", returncode=1)
            return mock.Mock(stdout=f"{GB}\t{cmd[-1]}\n", returncode=0)
        with mock.patch.object(dh.subprocess, "run", side_effect=run), \
                mock.patch.object(Path, "exists", return_value=True):
            sizes, failed = dh.measure_areas()
        self.assertEqual((sizes["sandbox"], sizes["system"], sizes["Docker images"]), (None, None, 2 * GB))
        self.assertEqual(failed, ["sandbox (timed out)", "system (du /usr: du: broken)",
                                  "logs (du /var/log: du: vanished)"])

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
            self.assertTrue(lines[0].startswith("Disk "))                    # the Monday note still goes out
            self.assertEqual(problems[-1], "Couldn't save the disk history: read-only")
            with mock.patch.object(dh, "load", side_effect=OSError("a link")), mock.patch.object(dh, "save") as save:
                lines, problems = dh.record(MON, over=False)
            save.assert_not_called()                                        # a history it couldn't read is kept as is
            self.assertEqual((lines[0][:5], problems[-1]), ("Disk ", "Couldn't read the disk history: a link"))


class GuardDisk(unittest.TestCase):
    def setUp(self):
        mock.patch("builtins.print").start()
        self.addCleanup(mock.patch.stopall)
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        mock.patch.object(guard, "FLAG", Path(d.name) / "flags" / "media-paused").start()
        mock.patch.object(guard, "measure", return_value=(0.59, 0, None)).start()
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


class GuardWithoutVnstat(unittest.TestCase):
    """GPT whole-batch review: a vnstat failure exited before the disk history, so no disk alert went out."""
    ROUTE = "Iface\tDestination\nens3\t00000000\n"

    def setUp(self):
        mock.patch("builtins.print").start()
        self.addCleanup(mock.patch.stopall)
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        mock.patch.object(guard, "FLAG", Path(d.name) / "flags" / "media-paused").start()
        mock.patch.object(guard.shutil, "disk_usage", return_value=mock.Mock(used=90, total=100)).start()
        mock.patch.object(guard.Path, "read_text", return_value=self.ROUTE).start()

    def test_the_disk_is_recorded_and_warned_about_with_the_traffic_problem(self):
        vnstat = mock.Mock(returncode=1, stderr="vnstatd not running")
        alert = mock.Mock(returncode=0)
        with mock.patch.object(guard.subprocess, "run", side_effect=[vnstat, alert]) as run, \
                mock.patch.object(guard.disk_history, "record", return_value=(["Disk grew 4 GB this week."], [])) as record:
            self.assertEqual(guard.main(MON), 1)                         # the run still shows failed
        record.assert_called_once_with(MON, over=True)
        text = run.call_args.kwargs["input"]
        for words in ("90% full", "Disk grew 4 GB", "vnstatd not running"):
            self.assertIn(words, text)

    def test_no_vnstat_at_all_or_an_unwatched_interface_is_said_too(self):
        for effect in (FileNotFoundError("vnstat"), mock.Mock(returncode=0, stdout='{"interfaces": []}')):
            with self.subTest(effect=effect), \
                    mock.patch.object(guard.subprocess, "run", side_effect=[effect, mock.Mock(returncode=0)]) as run, \
                    mock.patch.object(guard.disk_history, "record", return_value=([], [])):
                self.assertEqual(guard.main(MON), 1)
                self.assertIn("traffic" if isinstance(effect, Exception) else "vnstat doesn't watch ens3",
                              run.call_args.kwargs["input"])

    def test_valid_json_of_the_wrong_shape_still_records_the_disk(self):
        """GPT review: "interfaces": null or a month without "tx" raised before the disk was recorded."""
        month = {"date": {"year": datetime.date.today().year, "month": datetime.date.today().month}}
        for payload in ({"interfaces": None}, {"interfaces": [{"name": "ens3", "traffic": {"month": [month]}}]},
                        {"interfaces": [{"name": "ens3", "traffic": {"month": [{**month, "tx": float("inf")}]}}]}):
            vnstat = mock.Mock(returncode=0, stdout=json.dumps(payload))
            with self.subTest(payload=payload), \
                    mock.patch.object(guard.subprocess, "run", side_effect=[vnstat, mock.Mock(returncode=0)]) as run, \
                    mock.patch.object(guard.disk_history, "record", return_value=([], [])) as record:
                self.assertEqual(guard.main(MON), 1)
                record.assert_called_once_with(MON, over=True)
                self.assertIn("90% full", run.call_args.kwargs["input"])
                self.assertIn("can't read this month's traffic", run.call_args.kwargs["input"])


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
