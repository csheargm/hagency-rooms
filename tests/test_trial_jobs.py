"""Trial builds' shared queue (server/trial_jobs.py, ADR 0027): at most 5 waiting, one running server-wide,
one job per visitor, positions, the news @pm says once, and the worker's heartbeat.
Run: .venv/bin/python -m unittest tests.test_trial_jobs"""
import tempfile
import unittest
from pathlib import Path

from server import trial_jobs as tj


class Queue(unittest.TestCase):
    def setUp(self):
        self.now = [1000.0]
        self.jobs = tj.Jobs(Path(tempfile.mkdtemp()), clock=lambda: self.now[0])

    def add(self, n):
        self.now[0] += 1
        return self.jobs.enqueue(f"@v{n}:s", n, f"!dm{n}", 5)

    def test_positions_and_at_most_five_waiting(self):
        self.assertEqual([self.add(n)[2] for n in range(5)], [1, 2, 3, 4, 5])
        self.assertEqual(self.add(5)[0], tj.BUSY)
        self.jobs.claim()                                     # #1 runs: the others are behind it
        self.assertEqual(self.jobs.of("@v1:s")[1]["position"], 2)
        self.assertEqual(self.add(6)[:1], (tj.QUEUED,))       # a place came free

    def test_one_job_at_a_time_per_visitor(self):
        state, job_id, _ = self.add(1)
        self.assertEqual(self.jobs.enqueue("@v1:s", 1, "!dm1", 5), (tj.ALREADY, job_id, 1))
        self.jobs.claim()
        self.assertEqual(self.jobs.enqueue("@v1:s", 1, "!dm1", 5), (tj.ALREADY, job_id, 0))   # 0: running

    def test_one_running_server_wide(self):
        self.add(1), self.add(2)
        first = self.jobs.claim()
        self.assertIsNone(self.jobs.claim())
        self.jobs.finish(first[0], tj.DONE, {"pr": 1})
        self.assertEqual(self.jobs.claim()[1]["visitor"], "@v2:s")

    def test_news_is_said_once_each(self):
        _, job_id, _ = self.add(1)
        self.assertEqual(self.jobs.news(), [])                 # "queued" was said when it was queued
        self.jobs.claim()
        self.assertEqual([e for _, _, e in self.jobs.news()], ["started"])
        self.jobs.told(job_id, "started")
        self.jobs.finish(job_id, tj.FAILED, {"why": "tests_failed"})
        self.assertEqual([e for _, _, e in self.jobs.news()], [tj.FAILED])
        self.jobs.told(job_id, tj.FAILED)
        self.assertEqual(self.jobs.news(), [])

    def test_finished_jobs_are_pruned_after_a_week(self):
        _, job_id, _ = self.add(1)
        self.jobs.claim()
        self.jobs.finish(job_id, tj.DONE, {})
        self.now[0] += tj.KEEP_S + 1
        self.jobs.prune()
        self.assertEqual(self.jobs.all(), {})

    def test_heartbeat_freshness(self):
        self.assertIsNone(self.jobs.alive())
        self.jobs.beat({"TRIAL_BUILD_QUEUE": 5})
        self.assertEqual(self.jobs.alive(), {"TRIAL_BUILD_QUEUE": 5})
        self.now[0] += tj.HEARTBEAT_S + 1
        self.assertIsNone(self.jobs.alive())                   # a worker that stopped: builds aren't offered
        self.jobs.beat({})
        self.jobs.stop_beating()
        self.assertIsNone(self.jobs.alive())

    def test_a_damaged_file_is_no_jobs(self):
        self.jobs.folder.mkdir(parents=True, exist_ok=True)
        self.jobs.path.write_text("{nope")
        self.assertEqual(self.jobs.all(), {})


if __name__ == "__main__":
    unittest.main()
