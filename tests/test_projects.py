"""Unit tests: project rooms — one room per repo, visibility, "make me a project room", "find a
project" (pm_projects.py, project_rooms.py, room_state.py). Run: .venv/bin/python -m unittest tests.test_projects"""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from nio import RoomGetStateResponse, RoomPutStateResponse

from common import github
from server import pm_projects
from server import project_rooms
from common import registry
from common import room_state
from server.loop import Loop

S = "bridge.localhost"
PM, ANNA, BETTY = f"@pm:{S}", f"@anna:{S}", f"@betty:{S}"
REPO = "your-name/hagency-hello"


def project(rid="!a", name="Alpha", *members, mods=(ANNA,)):
    users = {m: None for m in (PM, ANNA, BETTY, *members)}
    return SimpleNamespace(room_id=rid, display_name=name, users=users, invited_users={}, canonical_alias=None,
                           power_levels=SimpleNamespace(get_user_level=lambda u: 100 if u in mods else 0))


def run(coro):
    return asyncio.run(coro)


class Base(unittest.TestCase):
    def setUp(self):
        path = Path(tempfile.mkdtemp()) / "agents.json"
        path.write_text(json.dumps({"anna": {"agent": "hermes"}}))
        for p in (mock.patch.object(registry, "PATH", path),
                  mock.patch.object(github, "ALLOWED_REPOS", frozenset({REPO, "a/public-one", "a/secret-one"}))):
            p.start()
            self.addCleanup(p.stop)
        self.lp = Loop.__new__(Loop)
        self.lp.user_id, self.lp.busy, self.lp.dm_rooms, self.lp.room_repos = PM, set(), {}, {}
        self.lp.started_ms = 0
        self.lp._join_requests, self.lp._lobby_greeted, self.lp._discussions, self.lp._direct_chats = {}, {}, {}, {}
        self.said, self.buttons, self.state = [], [], {}         # state: room → {event type: (content, sender)}
        self.members = {}                                         # (room, user) → membership, as the server says
        self.member_ids, self.mods = {}, {}                       # (room, user) → its event id; room → moderators
        self.lp.lock = asyncio.Lock()

        async def say(room_id, body, *a, **k):
            self.said.append((room_id, body))
            return f"$e{len(self.said)}"

        async def thumb(room_id, event_id, key="👍"):
            self.buttons.append((room_id, event_id, key))

        async def get_state(rid):
            events = [{"type": t, "state_key": "", "sender": who, "content": c} for t, (c, who) in self.state.get(rid, {}).items()]
            events += [{"type": "m.room.member", "state_key": u, "event_id": self.member_ids.get((r, u), "$m"),
                        "content": {"membership": m}} for (r, u), m in self.members.items() if r == rid]
            if rid in self.mods:              # the server's own power levels (not the cached room object)
                events.append({"type": "m.room.power_levels", "state_key": "", "content": {"users": {u: 100 for u in self.mods[rid]}}})
            return RoomGetStateResponse(events, rid)

        async def get_state_event(rid, kind, key):
            if kind == "m.room.member":
                return SimpleNamespace(content={"membership": self.members.get((rid, key))})
            return SimpleNamespace(content=self.state.get(rid, {}).get(kind, ({}, PM))[0])

        async def put_state(rid, kind, content, state_key=""):
            self.state.setdefault(rid, {})[kind] = (content, PM)
            return RoomPutStateResponse("$s", rid)
        self.lp.say, self.lp.offer_thumb = say, thumb
        self.lp.client = mock.Mock(user_id=PM, rooms={}, access_token="tok", room_get_state=get_state,
                                   room_put_state=put_state, room_get_state_event=get_state_event)
        self.lp.client.send = mock.AsyncMock(return_value=SimpleNamespace(status=200, release=lambda: None,
                                                                          json=mock.AsyncMock(return_value={})))
        self.lp.private_room = mock.AsyncMock(side_effect=lambda who: f"!dm-{who}")
        self.lp.kick = lambda: None

    def bind(self, rid, repo=REPO, runs_in=None):
        self.state.setdefault(rid, {})[github.LEVEL_EVENT] = (room_state.content("off", repo, runs_in is not None), PM)
        self.lp.room_repos[rid] = repo
        if runs_in:
            self.lp._discussions[rid] = runs_in


# TODO(R4): over 300 lines; split on its classes (RoomPerRepo / visibility / find-a-project) into their own files.
class RoomPerRepo(Base):
    def setUp(self):
        super().setUp()
        self.a, self.b = project("!a", "Alpha"), project("!b", "Beta")
        self.lp.client.rooms = {"!a": self.a, "!b": self.b}
        self.bind("!a")
        for target, value in (("server.pm_settings.may_write", mock.AsyncMock(return_value=True)),
                              ("common.github_app.installed", mock.AsyncMock(return_value=True)),
                              ("server.project_rooms.repo_is_private", mock.AsyncMock(return_value=True))):
            q = mock.patch(target, value)
            q.start()
            self.addCleanup(q.stop)
        self.lp.lock, self.lp.install_watches, self.lp.state = asyncio.Lock(), {}, {}
        self.lp.pending, self.lp.plans, self.lp.last_plans = {}, {}, {}
        for rid in ("!a", "!b"):                                            # Anna is joined there, a moderator
            self.members[(rid, ANNA)], self.mods[rid] = "join", {ANNA}

    def test_a_second_room_on_the_same_repo_gets_two_choices(self):
        answer = run(self.lp.set_repo(self.b, ANNA, REPO))
        self.assertIn("already the project of another room", answer)
        self.assertNotIn("Alpha", answer)                                   # people here may not know that room
        self.assertEqual([k for r, _, k in self.buttons], ["🙋", "➕"])
        self.assertNotIn(github.LEVEL_EVENT, self.state.get("!b", {}))      # nothing bound yet

    def test_a_separate_room_discusses_but_never_runs_the_loop(self):
        run(self.lp.set_repo(self.b, ANNA, REPO))
        offer = self.buttons[0][1]
        click = SimpleNamespace(sender=ANNA, key="➕", reacts_to=offer)
        self.assertTrue(run(self.lp.on_conflict_reaction(self.b, click)))
        content = self.state["!b"][github.LEVEL_EVENT][0]
        self.assertEqual((room_state.is_discussion(content), room_state.level_of({**content, "level": "autonomous"})), (True, "off"))
        self.assertNotIn("!a", json.dumps(content))                         # the other room's id is never in this room's state
        self.assertEqual(self.lp._discussions["!b"], "!a")
        self.assertIn("build loop runs in the room that already works on it", self.said[-1][1])
        refused = run(self.lp.change_level("!b", "autonomous"))           # assist/autonomous refused there
        self.assertEqual(refused, pm_projects.RUN_ELSEWHERE)
        self.assertIsNone(run(self.lp.change_level("!b", "off")))
        self.assertTrue(room_state.is_discussion(self.state["!b"][github.LEVEL_EVENT][0]))   # kept on a stop

    def test_a_stop_racing_a_rebind_never_writes_the_old_repo_back(self):
        """GPT review: binding writes read and write under one lock."""
        order = []
        real_put = self.lp.client.room_put_state

        async def slow_put(rid, kind, content, state_key=""):
            order.append(content.get("repo"))
            await asyncio.sleep(0.01)
            return await real_put(rid, kind, content, state_key)
        self.lp.client.room_put_state = slow_put
        self.lp.level = mock.AsyncMock(return_value="off")

        async def race():
            await asyncio.gather(self.lp.write_level("!a", "off", "your-name/other"), self.lp.change_level("!a", "off"))
        with mock.patch.object(github, "ALLOWED_REPOS", frozenset({REPO, "your-name/other"})):
            run(race())
            self.assertEqual(room_state.repo_in(self.state["!a"][github.LEVEL_EVENT][0]), "your-name/other")
        self.assertEqual(order, ["your-name/other", "your-name/other"])   # the stop read after the rebind wrote

    def test_only_a_moderator_chooses_and_ask_to_join_goes_to_the_other_rooms_owners(self):
        run(self.lp.set_repo(self.b, ANNA, REPO))
        offer = self.buttons[0][1]
        self.assertTrue(run(self.lp.on_conflict_reaction(self.b, SimpleNamespace(sender=BETTY, key="➕", reacts_to=offer))))
        self.assertNotIn(github.LEVEL_EVENT, self.state.get("!b", {}))      # betty isn't a moderator: no effect
        self.a.users.pop(ANNA)
        self.a.power_levels = SimpleNamespace(get_user_level=lambda u: 100 if u == BETTY else 0)
        run(self.lp.on_conflict_reaction(self.b, SimpleNamespace(sender=ANNA, key="🙋", reacts_to=offer)))
        asks = [(r, b) for r, b in self.said if r == f"!dm-{BETTY}"]
        self.assertEqual(len(asks), 1)
        self.assertIn(f"{ANNA} asks to join **Alpha**", asks[0][1])

    def test_two_rooms_choosing_at_once_never_both_run_the_repo(self):
        """GPT review: the check and the bind are one step under the lock."""
        self.state.pop("!a")
        self.lp.room_repos.clear()
        async def both():
            return await asyncio.gather(self.lp.set_repo(self.a, ANNA, REPO), self.lp.set_repo(self.b, ANNA, REPO))
        answers = run(both())
        self.assertEqual(sum("now works on" in a for a in answers), 1)
        self.assertEqual(sum("another room" in a for a in answers), 1)

    def test_right_after_a_start_bound_rooms_are_still_found(self):
        self.lp.room_repos.clear()                                        # the poll hasn't run yet
        self.assertIn("another room", run(self.lp.set_repo(self.b, ANNA, REPO)))

    def test_an_old_question_is_dropped_once_the_room_binds(self):
        run(self.lp.set_repo(self.b, ANNA, REPO))
        offer = self.buttons[0][1]
        self.state.pop("!a")
        self.lp.room_repos.pop("!a")
        run(self.lp.set_repo(self.b, ANNA, REPO))                         # !a let go: !b now runs it
        self.assertFalse(run(self.lp.on_conflict_reaction(self.b, SimpleNamespace(sender=ANNA, key="➕", reacts_to=offer))))

    def test_a_rebind_never_brings_back_a_mode_a_stop_just_turned_off(self):
        """GPT re-check: the level kept on a same-repo rebind is read under the binding lock."""
        self.state["!a"][github.LEVEL_EVENT] = (room_state.content("autonomous", REPO), PM)
        self.lp.level = lambda rid: room_state.level(self.lp.client, rid)

        async def race():
            await asyncio.gather(self.lp.change_level("!a", "off"), self.lp.bind("!a", REPO))
        run(race())
        self.assertEqual(self.state["!a"][github.LEVEL_EVENT][0]["level"], "off")

    def test_an_unreadable_room_never_lets_a_second_room_run_the_repo(self):
        """GPT re-check: a failed read of the primary must not look like "no primary"."""
        real = self.lp.client.room_get_state

        async def flaky(rid):
            return SimpleNamespace() if rid == "!a" else await real(rid)
        self.lp.client.room_get_state = flaky
        self.assertIn("couldn't check the other rooms", run(self.lp.set_repo(self.b, ANNA, REPO)))
        self.assertNotIn(github.LEVEL_EVENT, self.state.get("!b", {}))

    def test_a_question_answered_after_the_room_bound_gets_no_buttons(self):
        answer = self.lp.conflict_answer("!b", ANNA, REPO, "!a")
        self.lp.drop_conflict_offers("!b")                                 # the room bound meanwhile
        run(self.lp.offer_conflict_buttons("!b", answer, "$late"))
        self.assertEqual(self.buttons, [])

    def test_running_work_refuses_before_anything_changes(self):
        self.lp.state = {"k": {"room": "!b", "repo": "a/other", "stage": "building"}}
        self.state["!b"] = {"m.room.join_rules": ({"join_rule": "public"}, ANNA)}
        self.state.pop("!a")
        self.lp.room_repos.clear()
        self.assertIn("still running", run(self.lp.set_repo(self.b, ANNA, REPO)))
        self.assertEqual(self.state["!b"]["m.room.join_rules"][0], {"join_rule": "public"})   # not closed either

    def test_a_discussion_room_is_never_the_repos_primary(self):
        self.bind("!b", runs_in="!a")
        self.assertEqual(run(self.lp.primary_room_for(REPO, exclude="!a")), None)
        self.assertEqual(run(self.lp.primary_room_for(REPO.upper())), "!a")


class VisibilitySetup(Base):
    """Room !a, anna moderating, a public repo; no tests of its own, so Panel doesn't run Visibility's twice."""
    def setUp(self):
        super().setUp()
        self.room = project("!a", "Alpha")
        self.lp.client.rooms = {"!a": self.room}
        self.members[("!a", ANNA)], self.members[("!a", BETTY)], self.mods["!a"] = "join", "join", {ANNA}
        self.private = mock.patch("server.project_rooms.repo_is_private", mock.AsyncMock(return_value=False))
        self.private.start()
        self.addCleanup(self.private.stop)


class Visibility(VisibilitySetup):
    def test_each_visibility_sets_its_join_rule_and_directory_entry(self):
        for value, rule, listed in (("listed", "knock", "public"), ("open", "public", "public"), ("private", "invite", "private")):
            answer = run(self.lp.set_visibility(self.room, ANNA, value))
            self.assertTrue(answer.startswith(f"This room is now {value}"), answer)
            self.assertEqual(self.state["!a"]["m.room.join_rules"][0], {"join_rule": rule})
            listing = [c.args for c in self.lp.client.send.call_args_list if "/directory/list/" in c.args[1]][-1]
            self.assertEqual(json.loads(listing[2]), {"visibility": listed})
            self.assertEqual(run(project_rooms.visibility(self.lp.client, "!a")), value)

    def test_only_moderators_and_never_open_on_a_private_repo(self):
        self.assertIn("Only the room owner", run(self.lp.set_visibility(self.room, BETTY, "open")))
        self.bind("!a")
        with mock.patch("server.project_rooms.repo_is_private", mock.AsyncMock(return_value=True)):
            self.assertEqual(run(self.lp.set_visibility(self.room, ANNA, "open")), project_rooms.NOT_OPEN)
            self.assertTrue(run(self.lp.set_visibility(self.room, ANNA, "listed")).startswith("This room is now listed"))
        self.assertNotIn("public", json.dumps(self.state["!a"]["m.room.join_rules"][0]))

    def test_an_open_room_is_closed_before_a_private_repo_is_bound(self):
        """GPT review: by its real join rule (not my record), and no bind if closing fails."""
        self.state["!a"] = {"m.room.join_rules": ({"join_rule": "public"}, ANNA)}   # set by hand in Element
        with mock.patch("server.project_rooms.repo_is_private", mock.AsyncMock(return_value=True)):
            refused, note = run(self.lp.close_for_private("!a", REPO))
            self.assertIsNone(refused)
            self.assertIn("now listed instead of open", note)
            self.assertEqual(self.state["!a"]["m.room.join_rules"][0], {"join_rule": "knock"})
            self.state["!a"]["m.room.join_rules"] = ({"join_rule": "public"}, ANNA)
            self.lp.client.send = mock.AsyncMock(return_value=SimpleNamespace(status=500, release=lambda: None))
            refused, _ = run(self.lp.close_for_private("!a", REPO))
        self.assertIn("I haven't set the repo", refused)

    def test_an_unreadable_binding_never_counts_as_no_repo(self):
        self.lp.client.room_get_state = mock.AsyncMock(return_value=SimpleNamespace())
        self.assertIn("couldn't read this room's settings", run(self.lp.set_visibility(self.room, ANNA, "open")))
        self.assertNotIn("m.room.join_rules", self.state.get("!a", {}))

    def test_someone_elses_record_is_ignored_and_front_doors_have_no_setting(self):
        self.state["!a"] = {project_rooms.VISIBILITY_EVENT: ({"visibility": "open"}, ANNA)}
        self.assertEqual(run(project_rooms.visibility(self.lp.client, "!a")), "private")
        lobby = project("!l", "Lobby")
        lobby.canonical_alias = project_rooms.LOBBY_ALIAS
        self.assertEqual(run(self.lp.set_visibility(lobby, ANNA, "private")), "Only project rooms have this setting.")

    def test_a_failure_at_any_step_is_said_and_the_record_is_written_last(self):
        """GPT review: opening sets the join rule last, closing first: a failure never leaves it more open."""
        ok = self.lp.client.send
        self.lp.client.send = mock.AsyncMock(return_value=SimpleNamespace(status=403, release=lambda: None))
        self.assertIn("couldn't update the room directory", run(self.lp.set_visibility(self.room, ANNA, "listed")))
        self.assertNotIn("m.room.join_rules", self.state.get("!a", {}))     # still private: not opened halfway
        self.lp.client.send = ok
        run(self.lp.set_visibility(self.room, ANNA, "open"))
        self.lp.client.send = mock.AsyncMock(return_value=SimpleNamespace(status=403, release=lambda: None))
        self.assertIn("couldn't update the room directory", run(self.lp.set_visibility(self.room, ANNA, "private")))
        self.assertEqual(self.state["!a"]["m.room.join_rules"][0], {"join_rule": "invite"})   # closed first
        self.assertEqual(self.state["!a"][project_rooms.VISIBILITY_EVENT][0], {"visibility": "open"})   # record last

    def test_an_opening_that_fails_at_the_join_rule_takes_the_listing_back_down(self):
        """GPT re-check: never recorded or listed as open when anyone-can-join didn't happen."""
        real = self.lp.client.room_put_state

        async def refuse_rule(rid, kind, content, state_key=""):
            return SimpleNamespace(status_code="M_FORBIDDEN") if kind == "m.room.join_rules" else await real(rid, kind, content, state_key)
        self.lp.client.room_put_state = refuse_rule
        self.assertIn("couldn't change who can join", run(self.lp.set_visibility(self.room, ANNA, "open")))
        self.assertNotIn(project_rooms.VISIBILITY_EVENT, self.state.get("!a", {}))
        self.assertEqual(json.loads(self.lp.client.send.call_args.args[2]), {"visibility": "private"})   # unlisted again

    def test_a_network_failure_while_opening_is_handled_and_a_failed_rollback_is_said(self):
        async def broken_rule(rid, kind, content, state_key=""):
            raise OSError("connection reset")
        listed = iter([SimpleNamespace(status=200, release=lambda: None), SimpleNamespace(status=500, release=lambda: None)])
        self.lp.client.send = mock.AsyncMock(side_effect=lambda *a, **k: next(listed))
        self.lp.client.room_put_state = broken_rule
        answer = run(self.lp.set_visibility(self.room, ANNA, "listed"))
        self.assertIn("couldn't reach the server", answer)
        self.assertIn("set it to private again", answer)

    def test_a_private_repos_room_never_has_public_history(self):
        self.bind("!a")
        self.state["!a"]["m.room.history_visibility"] = ({"history_visibility": "world_readable"}, ANNA)
        with mock.patch("server.project_rooms.repo_is_private", mock.AsyncMock(return_value=True)):
            run(self.lp.set_visibility(self.room, ANNA, "listed"))
            self.assertEqual(self.state["!a"]["m.room.history_visibility"][0], {"history_visibility": "shared"})
            self.state["!a"]["m.room.history_visibility"] = ({"history_visibility": "world_readable"}, ANNA)
            self.assertEqual(run(self.lp.close_for_private("!a", REPO)), (None, ""))
        self.assertEqual(self.state["!a"]["m.room.history_visibility"][0], {"history_visibility": "shared"})

    def test_unreadable_repos_count_as_private(self):
        self.private.stop()
        with mock.patch("common.github.run_json", mock.AsyncMock(side_effect=github.GitHubError("no"))):
            self.assertTrue(run(project_rooms.repo_is_private(REPO)))
        with mock.patch("common.github.run_json", mock.AsyncMock(return_value={"private": False})):
            self.assertFalse(run(project_rooms.repo_is_private(REPO)))
        self.private.start()


class MakeAndFind(Base):
    def test_make_me_a_project_room(self):
        self.lp.client.room_create = mock.AsyncMock(return_value=SimpleNamespace(room_id="!new"))
        origin = project("!o", "Lobby")
        self.lp.addressed_text = lambda event, room=None: event.body
        with mock.patch("server.panel.add_to_room", mock.AsyncMock(return_value=True)) as panel:
            ok = run(self.lp.on_project_text(origin, SimpleNamespace(sender=ANNA, body="make me a project room called Rocket")))
        self.assertTrue(ok)
        kw = self.lp.client.room_create.call_args.kwargs
        self.assertEqual((kw["name"], kw["invite"], kw["power_level_override"]), ("Rocket", [ANNA], {"users": {ANNA: 100}}))
        panel.assert_awaited_once()
        self.assertIn("1. Pick its GitHub repo", self.said[0][1])             # the next step, in the room
        self.assertIn("an invite link from the panel", self.said[0][1])       # live test: links were left out
        self.assertIn("accept its invite", self.said[1][1])                   # and where the room is
        for _ in range(3):
            run(self.lp.make_project_room(origin, ANNA, ""))
        self.assertIn("enough new rooms", self.said[-1][1])                   # a few an hour

    def test_a_new_room_says_comes_along_only_for_an_agent_that_can_join(self):
        """Screen test: "Your agent comes along" to someone whose agent never connected on their computer."""
        self.lp.client.room_create = mock.AsyncMock(return_value=SimpleNamespace(room_id="!new"))
        self.lp.addressed_text = lambda event, room=None: event.body
        registry.PATH.write_text(json.dumps({"anna": {"agent": "hermes"}, "betty": {"agent": registry.REMOTE}}))
        self.lp._seen = {}
        with mock.patch("server.panel.add_to_room", mock.AsyncMock(return_value=True)):
            for who in (ANNA, BETTY):
                run(self.lp.on_project_text(project("!o", "Lobby"), SimpleNamespace(sender=who, body="make me a project room")))
            self.lp._seen = {registry.agents_of(BETTY)[0]["localpart"]: {"ever": True}}
            run(self.lp.on_project_text(project("!o", "Lobby"), SimpleNamespace(sender=BETTY, body="make me a project room")))
        made = [b for _, b in self.said if b.startswith("Made")]
        self.assertIn("Your agent comes along when you join.", made[0])                    # run by this server
        self.assertIn("Your agent joins once it's connected on your computer.", made[1])   # never connected
        self.assertNotIn("comes along", made[1])
        self.assertIn("Your agent comes along when you join.", made[2])                    # connected once
        for follow, note in (("ask", " When you join, I'll ask whether to bring your agent."), ("never", "")):
            registry.PATH.write_text(json.dumps({"anna": {"agent": "hermes", "follow": follow}}))
            self.assertEqual(self.lp.agent_note(ANNA), note)                               # GPT review: their setting


class Panel(VisibilitySetup):
    def test_the_panel_switch_goes_through_the_same_checks_and_says_it_in_the_room(self):
        from server import console
        self.lp.is_person = lambda room, user: True
        self.assertTrue(run(console.act(self.lp, "!a", ANNA, "visibility", "listed")).startswith("This room is now listed"))
        self.assertEqual(self.said[-1][0], "!a")
        self.assertIn("Only the room owner", run(console.act(self.lp, "!a", BETTY, "visibility", "open")))

    def test_the_panel_shows_where_a_discussion_rooms_repo_is_run(self):
        from server import console
        self.lp.client.rooms["!b"] = project("!b", "Beta")
        self.bind("!b", runs_in="!a")
        shown = run(console.runs_in(self.lp, "!b", ANNA))
        self.assertEqual(shown["name"], "Alpha")
        self.assertTrue(shown["link"].endswith("/#/room/%21a"))
        self.assertEqual(run(console.runs_in(self.lp, "!b", "@carl:x")), {"name": "another room", "link": ""})
        self.assertIsNone(run(console.runs_in(self.lp, "!a", ANNA)))

    def test_room_names_are_shown_as_text_never_as_links_or_instructions(self):
        self.assertEqual(project_rooms.plain("[Approve](http://x)\n**now**"), "\\[Approve\\]\\(http://x\\) \\*\\*now\\*\\*")


if __name__ == "__main__":
    unittest.main()
