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
    """Live test 2026-11-26: the words were a shell command, no use on a phone (ADR 0021)."""
    def test_a_long_agent_name_still_fits_pms_cut(self):
        said = room_safe(github.NO_IDENTITY.format(agent="alice-at-matrix-org-a5829e99c7-agent"))
        self.assertLessEqual(len(said), 200, said)        # pm_plan.on_plan_result shows 200 characters
        self.assertTrue(said.endswith("then ask @pm again."), said)
        self.assertNotIn("retry", said)                  # review: retry resumes only a paused issue (test_planning)

    def test_it_points_at_the_button_and_survives_the_rooms_path_filter(self):
        with mock.patch.object(github, "IDENTITY_ROOT", Path(tempfile.mkdtemp())), \
                self.assertRaises(github.GitHubError) as e:
            asyncio.run(github.run(["issue", "list"], agent="nt-agent"))
        said = room_safe(e.exception)                    # what the room shows (local paths cut)
        self.assertIn("nt-agent isn't signed in to GitHub", said)
        self.assertIn("My agents → Sign in to GitHub", said)
        self.assertIn("ask @pm again", said)
        self.assertNotIn("<local path>", said)
        self.assertNotIn("gh auth", said)                # the command is for its owner, in the panel


class PlanWords(unittest.TestCase):
    def test_a_stopped_room_names_the_phrases_to_start(self):
        off = pm_plan.AFTER_APPROVAL["off"]
        self.assertIn("let us approve each change", off)
        self.assertIn("take it all the way", off)
        self.assertNotIn("assist or autonomous", off)


if __name__ == "__main__":
    unittest.main()
