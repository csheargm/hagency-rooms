"""The sandbox wrapper (common/sandbox.py, ADR 0027): the egress probe fails closed, a dirty environment
is refused, the E2B calls ask for what the ADR says (checked against a stand-in for the pinned SDK),
and the reaper kills what is old (a fake clock). Run: .venv/bin/python -m unittest tests.test_trial_sandbox"""
import asyncio
import datetime
import sys
import types
import unittest
from unittest import mock

from common import sandbox


def run(coro):
    return asyncio.run(coro)


class Checks(unittest.TestCase):
    def test_a_blocked_host_that_answers_refuses_the_sandbox(self):
        box = run(sandbox.FakeSandboxes(egress_open=True).create(sandbox.BUILD, "j"))
        with self.assertRaises(sandbox.SandboxError) as e:
            run(sandbox.check_egress(box))
        self.assertIn("safety", str(e.exception))

    def test_a_closed_or_hanging_probe_passes(self):
        run(sandbox.check_egress(run(sandbox.FakeSandboxes().create(sandbox.BUILD, "j"))))

        class Hangs:
            async def run(self, cmd, timeout=0):
                raise asyncio.TimeoutError()
        run(sandbox.check_egress(Hangs()))

    def test_the_environment_holds_nothing_beyond_plain_settings(self):
        clean = run(sandbox.FakeSandboxes(environ="PATH=/usr/bin\nHOME=/home/user\nLANG=C.UTF-8\n").create(sandbox.TEST, "j"))
        run(sandbox.check_clean(clean))
        for dirty in ("PATH=/bin\nE2B_API_KEY=x\n", "PATH=/bin\nGITHUB_TOKEN=x\n", "PATH=/bin\nMY_THING=1\n"):
            box = run(sandbox.FakeSandboxes(environ=dirty).create(sandbox.TEST, "j"))
            with self.assertRaises(sandbox.SandboxError):
                run(sandbox.check_clean(box))

    def test_a_listing_that_isnt_ours_is_refused(self):
        for bad in ("nope", '{"files": []}', '{"files": {"a": 1}, "links": [], "other": []}'):
            with self.assertRaises(sandbox.SandboxError):
                sandbox.parse_listing(bad)
        self.assertEqual(sandbox.parse_listing('{"files": {"a": "h"}, "links": ["l"], "other": []}')["links"], ["l"])


class Reaper(unittest.TestCase):
    def test_only_old_sandboxes_are_reaped_and_zero_means_all(self):
        now = [1000.0]
        boxes = sandbox.FakeSandboxes(clock=lambda: now[0])
        old = run(boxes.create(sandbox.BUILD, "a"))
        now[0] += sandbox.REAP_AFTER_S - 60
        young = run(boxes.create(sandbox.BUILD, "b"))
        now[0] += 120
        self.assertEqual(run(boxes.reap()), 1)
        self.assertEqual((old.killed, young.killed), (True, False))
        self.assertEqual(run(boxes.reap(older_than_s=0)), 1)      # the kill switch
        self.assertTrue(young.killed)


class FakeE2B:
    """Stands in for the pinned e2b SDK's surface we use (e2b==2.66.0: AsyncSandbox.create/list/kill)."""
    def __init__(self):
        self.created, self.killed = [], []
        e2b = types.ModuleType("e2b")
        outer = self

        class AsyncSandbox:
            def __init__(self, **kw):
                self.sandbox_id, self.kw = f"s{len(outer.created)}", kw

            @classmethod
            async def create(cls, **kw):
                outer.created.append(kw)
                return cls(**kw)

            @staticmethod
            def list(query=None, **kw):
                old = types.SimpleNamespace(sandbox_id="old", started_at=datetime.datetime.fromtimestamp(0, datetime.timezone.utc))
                new = types.SimpleNamespace(sandbox_id="new", started_at=datetime.datetime.fromtimestamp(10**10, datetime.timezone.utc))
                pages = types.SimpleNamespace(has_next=True, query=query)

                async def next_items():
                    pages.has_next = False
                    return [old, new]
                pages.next_items = next_items
                return pages

            @staticmethod
            async def kill(sandbox_id, **kw):
                outer.killed.append(sandbox_id)
                return True
        e2b.AsyncSandbox, e2b.SandboxQuery = AsyncSandbox, lambda metadata: ("query", metadata)
        self.module = e2b


class E2BCalls(unittest.TestCase):
    def setUp(self):
        self.fake = FakeE2B()
        p = mock.patch.dict(sys.modules, {"e2b": self.fake.module})
        p.start()
        self.addCleanup(p.stop)
        self.boxes = sandbox.E2BSandboxes("e2b_key", "hagency-trial-python", 900)

    def test_a_build_sandbox_allows_only_the_package_indexes(self):
        run(self.boxes.create(sandbox.BUILD, "j1"))
        kw = self.fake.created[0]
        self.assertEqual(kw["network"]["allow_out"], sandbox.EGRESS_ALLOW)
        self.assertEqual(kw["network"]["deny_out"](types.SimpleNamespace(all_traffic="0.0.0.0/0")), ["0.0.0.0/0"])
        self.assertEqual((kw["envs"], kw["timeout"], kw["cpu_count"], kw["memory_mb"]), ({}, 900, 2, 1024))
        self.assertEqual(kw["metadata"]["hagency"], "trial-build")

    def test_a_test_sandbox_has_no_network_at_all(self):
        run(self.boxes.create(sandbox.TEST, "j1"))
        kw = self.fake.created[0]
        self.assertIs(kw["allow_internet_access"], False)
        self.assertIsNone(kw["network"])

    def test_the_reaper_kills_ours_that_are_old(self):
        self.assertEqual(run(self.boxes.reap(now=10**6)), 1)
        self.assertEqual(self.fake.killed, ["old"])

    def test_a_refused_create_says_so_without_the_key(self):
        async def refuse(**kw):
            raise RuntimeError("401 for key e2b_key")
        self.fake.module.AsyncSandbox.create = refuse
        with self.assertRaises(sandbox.SandboxError) as e:
            run(self.boxes.create(sandbox.BUILD, "j"))
        self.assertNotIn("e2b_key", str(e.exception))


if __name__ == "__main__":
    unittest.main()
