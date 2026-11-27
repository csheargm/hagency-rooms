"""Removing a room's GitHub repo, as the owner does it: "Remove repo" in the Hagency panel (console.act,
after the page's two clicks) or "@pm remove the repo" and a ✅ in the room (pm_unbind.py).
Run: .venv/bin/python -m unittest tests.test_remove_repo"""
import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from nio import RoomSendResponse

from server import console
from common import github
from server import pm_settings
from server import pm_unbind
from adapters.agents import room_context
from common import room_state
from tests.test_projects import ANNA, BETTY, PM, REPO, Base, project, run

SURE = f"sure:{REPO}"                         # the page's second click names the repo it showed


class Setup(Base):
    def setUp(self):
        super().setUp()
        self.a, self.b = project("!a", "Alpha"), project("!b", "Beta")
        self.lp.client.rooms = {"!a": self.a, "!b": self.b}
        self.bind("!a")
        for target, value in (("server.pm_settings.may_write", mock.AsyncMock(return_value=True)),
                              ("common.github_app.installed", mock.AsyncMock(return_value=True)),
                              ("server.project_rooms.repo_is_private", mock.AsyncMock(return_value=False))):
            q = mock.patch(target, value)
            q.start()
            self.addCleanup(q.stop)
        self.lp.install_watches, self.lp.state, self.lp.open_issues = {}, {}, {"!a": [{"number": 1, "title": "x"}]}
        self.lp.pending, self.lp.plans, self.lp.last_plans, self.lp.generation = {}, {}, {}, {}
        self.lp.boards, self.lp.board_file = {}, Path(tempfile.mkdtemp()) / "boards.json"
        self.lp.client.room_send = mock.AsyncMock(side_effect=lambda rid, kind, content: RoomSendResponse("$b", rid))
        for rid in ("!a", "!b"):
            self.members[(rid, ANNA)], self.mods[rid] = "join", {ANNA}
        self.members[("!a", BETTY)] = "join"

    def binding(self, rid="!a") -> dict:
        return self.state.get(rid, {}).get(github.LEVEL_EVENT, ({}, PM))[0]

    def tell(self, sender, body, room=None):
        ev = SimpleNamespace(sender=sender, body=f"@pm {body}", source={"content": {"body": f"@pm {body}"}})
        return run(self.lp.on_project_text(room or self.a, ev))

    def click(self, sender, key="✅"):
        question = [e for r, e, k in self.buttons if k == "✅"][-1]
        return run(self.lp.on_project_reaction(self.a, SimpleNamespace(sender=sender, key=key, reacts_to=question)))

    def assert_removed(self):
        self.assertIsNone(room_state.repo_in(self.binding()))
        self.assertEqual(room_state.level_of({**self.binding(), "level": "autonomous"}), "off")
        self.assertEqual(self.said[-1], ("!a", pm_unbind.DONE))


class RemoveRepo(Setup):
    def test_the_owner_removes_it_from_the_panel(self):
        self.state["!a"][github.LEVEL_EVENT] = (room_state.content("assist", REPO), PM)
        answer = run(console.act(self.lp, "!a", ANNA, "unrepo", SURE))
        self.assertEqual(answer, pm_unbind.DONE)
        self.assert_removed()
        self.assertEqual(room_state.level_of(self.binding()), "off")       # mode is off after
        self.assertEqual(run(self.lp.change_level("!a", "autonomous")), pm_settings.NO_REPO)   # and stays off
        self.assertNotIn("!a", self.lp.room_repos)
        self.assertNotIn("!a", self.lp.open_issues)

    def test_the_panel_needs_its_second_click(self):
        self.assertNotEqual(run(console.act(self.lp, "!a", ANNA, "unrepo", "")), pm_unbind.DONE)
        self.assertEqual(room_state.repo_in(self.binding()), REPO)

    def test_the_owner_removes_it_in_the_chat_with_a_check_mark(self):
        self.assertTrue(self.tell(ANNA, "remove the repo"))
        self.assertIn(f"Remove this room's GitHub repo, **{REPO}**?", self.said[-1][1])
        self.assertEqual(room_state.repo_in(self.binding()), REPO)         # nothing yet: it asks first
        self.assertFalse(self.click(BETTY))                                 # someone else's ✅ does nothing
        self.assertEqual(room_state.repo_in(self.binding()), REPO)
        self.assertTrue(self.click(ANNA))
        self.assert_removed()

    def test_this_room_has_no_repo_says_the_same(self):
        for words in ("this room has no repo", "This room has no GitHub repo now.", "please remove the repo"):
            self.assertTrue(pm_unbind.UNREPO_RE.match(words), words)
        for words in ("remove the repo's README", "don't remove the repo", "this room works on a/b"):
            self.assertFalse(pm_unbind.UNREPO_RE.match(words), words)
        self.assertTrue(self.tell(ANNA, "this room has no repo"))
        self.assertTrue(self.click(ANNA))
        self.assert_removed()

    def test_a_contributor_is_refused(self):
        self.assertTrue(self.tell(BETTY, "remove the repo"))
        self.assertEqual(self.said[-1][1], pm_unbind.NOT_MODERATOR)
        self.assertEqual(run(console.act(self.lp, "!a", BETTY, "unrepo", SURE)), pm_unbind.NOT_MODERATOR)
        self.assertEqual(room_state.repo_in(self.binding()), REPO)

    def test_a_moderator_who_left_since_asking_is_refused(self):
        self.tell(ANNA, "remove the repo")
        self.members[("!a", ANNA)] = "leave"                                # read now, at the write
        self.click(ANNA)
        self.assertEqual(self.said[-1][1], pm_unbind.NOT_MODERATOR)
        self.assertEqual(room_state.repo_in(self.binding()), REPO)

    def test_running_work_is_refused_until_stopped(self):
        self.lp.state = {f"{REPO}#3": {"room": "!a", "repo": REPO, "stage": "building"}}
        self.assertEqual(run(console.act(self.lp, "!a", ANNA, "unrepo", SURE)), pm_settings.BUSY)
        self.assertEqual(room_state.repo_in(self.binding()), REPO)
        self.lp.state[f"{REPO}#3"]["stage"] = "stopped"
        self.assertEqual(run(console.act(self.lp, "!a", ANNA, "unrepo", SURE)), pm_unbind.DONE)

    def test_the_freed_repo_can_be_taken_by_another_room(self):
        self.assertIn("already the project of another room", run(self.lp.set_repo(self.b, ANNA, REPO)))
        run(console.act(self.lp, "!a", ANNA, "unrepo", SURE))
        self.assertIn(f"now works on **{REPO}**", run(self.lp.set_repo(self.b, ANNA, REPO)))
        self.assertEqual(room_state.repo_in(self.binding("!b")), REPO)
        self.assertFalse(room_state.is_discussion(self.binding("!b")))

    def test_rooms_records_and_waits_for_the_repo_go(self):
        self.lp._discussions.update({"!c": "!a", "!a": "!z", "!d": "!b"})
        self.lp.pending["!a"] = {"kind": "plan", "event_id": "$p"}
        watch = mock.Mock(done=lambda: False)
        self.lp.install_watches["!a"] = (REPO, watch)
        run(console.act(self.lp, "!a", ANNA, "unrepo", SURE))
        self.assertEqual(self.lp._discussions, {"!d": "!b"})
        self.assertNotIn("!a", self.lp.pending)
        watch.cancel.assert_called_once()

    def test_the_status_board_names_no_repo(self):
        self.lp.boards["!a"] = {"event_id": "$old", "text": "📋 board", "pinned": True, "panel": True}
        run(console.act(self.lp, "!a", ANNA, "unrepo", SURE))
        self.assertIn("no repo yet", self.lp.boards["!a"]["text"])
        self.assertTrue(self.lp.boards["!a"]["panel"])
        status = self.state["!a"][github.STATUS_EVENT][0]
        self.assertEqual(status["repo"], "")
        with mock.patch.object(room_context, "api", lambda *a, **k: [{"type": github.STATUS_EVENT, "state_key": "",
                                                                     "sender": room_context.PM_ID, "content": status}]):
            self.assertEqual(room_context.status("t", "!a", None), "")

    def test_each_agents_copy_of_the_repo_is_pruned_on_its_next_turn(self):
        run(console.act(self.lp, "!a", ANNA, "unrepo", SURE))
        event = {"sender": room_context.PM_ID, "content": self.binding()}  # what an agent reads (bridge.answer)
        with mock.patch.object(room_context, "api", lambda *a, **k: event):
            repo = room_context.room_repo("agent-token", "!a")
        self.assertIsNone(repo)
        projects = Path(tempfile.mkdtemp()) / "projects"
        (projects / room_context.repo_dir(REPO)).mkdir(parents=True)
        room_context.prune_projects(projects, repo and room_context.repo_dir(repo))   # as bridge.answer calls it
        self.assertEqual(list(projects.iterdir()), [])

    def test_nothing_to_remove_says_so(self):
        run(console.act(self.lp, "!a", ANNA, "unrepo", SURE))
        self.assertEqual(run(console.act(self.lp, "!a", ANNA, "unrepo", SURE)), pm_unbind.NO_REPO_HERE)

    def test_a_listed_room_keeps_its_address_rules(self):
        self.state["!a"]["xyz.hagencylabs.visibility"] = ({"visibility": "listed"}, PM)
        with mock.patch("server.room_address.follow", mock.AsyncMock(return_value="")) as follow:
            run(console.act(self.lp, "!a", ANNA, "unrepo", SURE))
        follow.assert_awaited_once_with(self.lp.client, "!a", True, "Alpha", None)

    def test_a_stop_after_removal_never_writes_the_repo_back(self):
        run(console.act(self.lp, "!a", ANNA, "unrepo", SURE))
        self.assertIsNone(run(self.lp.change_level("!a", "off")))
        self.assertIsNone(room_state.repo_in(self.binding()))


class ReviewFixes(Setup):
    """GPT review of the batch: each adopted point, as a test."""

    def test_a_confirmation_never_removes_a_repo_bound_since(self):
        self.tell(ANNA, "remove the repo")                                   # asked about REPO
        self.bind("!a", "a/public-one")                                     # another maintainer rebinds
        self.click(ANNA)
        self.assertEqual(self.said[-1][1], pm_unbind.CHANGED)
        self.assertEqual(run(console.act(self.lp, "!a", ANNA, "unrepo", SURE)), pm_unbind.CHANGED)   # the page too
        self.assertEqual(room_state.repo_in(self.binding()), "a/public-one")

    def test_a_demotion_while_waiting_for_the_binding_lock_counts(self):
        async def go():
            lock = self.lp.__dict__.setdefault("_binding_lock", asyncio.Lock())
            async with lock:                                                # a mode write in progress
                job = asyncio.create_task(console.act(self.lp, "!a", ANNA, "unrepo", SURE))
                await asyncio.sleep(0.01)
                self.mods["!a"] = set()                                     # demoted meanwhile
            return await job
        self.assertEqual(run(go()), pm_unbind.NOT_MODERATOR)
        self.assertEqual(room_state.repo_in(self.binding()), REPO)

    def test_cancelling_keeps_the_poll_lock_until_the_removal_is_done(self):
        async def go():
            job = asyncio.create_task(self.lp.remove_repo(self.a, ANNA, REPO))
            await asyncio.sleep(0)
            job.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await job
            return self.lp.lock.locked(), room_state.repo_in(self.binding())
        self.assertEqual(run(go()), (False, None))                          # finished whole, then released

    def test_a_board_edit_that_failed_is_retried_by_the_poll(self):
        self.lp.boards["!a"] = {"event_id": "$old", "text": "📋 board", "pinned": True}
        self.lp.client.room_send = mock.AsyncMock(return_value=SimpleNamespace())   # the edit and repost fail
        run(console.act(self.lp, "!a", ANNA, "unrepo", SURE))
        self.assertTrue(self.lp.boards["!a"]["unbound"])
        self.lp.client.room_send = mock.AsyncMock(side_effect=lambda rid, kind, content: RoomSendResponse("$b", rid))
        self.lp.ensure_panel = mock.AsyncMock()
        self.lp.level = mock.AsyncMock(return_value="off")
        run(self.lp.refresh_boards())
        self.assertNotIn("unbound", self.lp.boards["!a"])
        self.assertIn("no repo yet", self.lp.boards["!a"]["text"])

    def test_a_reposted_board_that_couldnt_be_pinned_is_retried(self):
        """GPT re-check: the repost succeeded, the pin didn't, and the retry mark was dropped."""
        self.lp.boards["!a"] = {"event_id": "$old", "text": "📋 board", "pinned": True}
        self.lp.show = mock.AsyncMock(return_value="$new")                 # the edit failed: posted anew
        self.lp.pin = mock.AsyncMock(return_value=False)
        run(console.act(self.lp, "!a", ANNA, "unrepo", SURE))
        self.assertTrue(self.lp.boards["!a"]["unbound"])
        self.lp.pin = mock.AsyncMock(return_value=True)
        run(self.lp.blank_board("!a"))
        self.assertNotIn("unbound", self.lp.boards["!a"])
        self.assertEqual(self.lp.boards["!a"]["event_id"], "$new")
        self.lp.pin.assert_awaited_once_with("!a", "$new", "$old")              # in the old board's place


if __name__ == "__main__":
    import unittest
    unittest.main()
