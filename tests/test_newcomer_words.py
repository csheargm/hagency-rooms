"""Words a newcomer meets on the way to their first change (overnight live test, 2026-11-23).

- A new agent failed with "has no GitHub identity (.gh/<agent>/ not signed in)": nothing to act on.
- An approved plan in a stopped room said "until the owner picks assist or autonomous": jargon.
"""
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from common import github
from common.redact import room_safe
from server import pm_plan


class NoGitHubIdentity(unittest.TestCase):
    def test_it_says_what_to_run_where_and_survives_the_rooms_path_filter(self):
        with mock.patch.object(github, "IDENTITY_ROOT", Path(tempfile.mkdtemp())), \
                self.assertRaises(github.GitHubError) as e:
            asyncio.run(github.run(["issue", "list"], agent="nt-agent"))
        said = room_safe(e.exception)                    # what the room shows (local paths cut)
        self.assertIn("isn't signed in to GitHub yet", said)
        self.assertIn("GH_CONFIG_DIR=.gh/nt-agent gh auth login --web", said)
        self.assertIn("@pm retry", said)
        self.assertNotIn("<local path>", said)


class PlanWords(unittest.TestCase):
    def test_a_stopped_room_names_the_phrases_to_start(self):
        off = pm_plan.AFTER_APPROVAL["off"]
        self.assertIn("let us approve each change", off)
        self.assertIn("take it all the way", off)
        self.assertNotIn("assist or autonomous", off)


if __name__ == "__main__":
    unittest.main()
