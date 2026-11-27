"""Unit tests: the trial agent answers only its visitors, only where they brought it, within its caps,
and never takes part in the loop (ADR 0025). Run: .venv/bin/python -m unittest tests.test_trial_agent"""
import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from nio import JoinResponse

from common import github
from common import registry
from server import ownership
from server import trial_agent
from server import trial_budget
from server import trial_limits
from server import trial_turn
from server.trial_model import TrialModelError

S = "bridge.localhost"
NEW, OTHER, ANNA, PM = f"@newbie1:{S}", f"@newbie2:{S}", f"@anna:{S}", f"@pm:{S}"
ME = f"@{trial_agent.LOCALPART}:{S}"
NOW_MS = 10**13


def room(room_id, *members, invited=()):
    return SimpleNamespace(room_id=room_id, users={m: None for m in (ME, *members, *invited)},
                           invited_users={m: None for m in invited})


async def hear(agent, room, event):
    """A message as sync delivers it (the room in the client's view), then its answer finished."""
    agent.client.rooms.setdefault(room.room_id, room)
    await agent.on_message(room, event)
    await agent.drain()


def message(sender, body, mentions=None, ts=NOW_MS):
    content = {"body": body, **({"m.mentions": {"user_ids": mentions}} if mentions else {})}
    return SimpleNamespace(sender=sender, body=body, source={"content": content}, server_timestamp=ts, event_id="$e")


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        env = mock.patch.dict(os.environ, {"OLLAMA_API_KEY": "k" * 20, "TRIAL_PER_VISITOR": "2", "TRIAL_PER_SERVER": "100"})
        env.start()
        self.addCleanup(env.stop)
        self.posts, self.asked, self.eligible = [], [], {NEW, OTHER}

        async def send(room_id, kind, content):
            self.posts.append((room_id, content))
            return SimpleNamespace(event_id=f"$p{len(self.posts)}")
        self.client = SimpleNamespace(
            user_id=ME, rooms={}, room_send=send, room_typing=mock.AsyncMock(), room_leave=mock.AsyncMock(),
            join=mock.AsyncMock(side_effect=lambda rid: JoinResponse(rid)),
            room_create=mock.AsyncMock(return_value=SimpleNamespace(room_id="!trialdm")))

        async def ask(chat, count=None, model=None):
            self.asked.append(chat)
            return "An answer."
        self.balance = None                                              # /api/balance unreadable by default
        self.budget = trial_budget.Budget(self.dir / "trial-budget.json", fetch=self.fetch_balance)
        self.agent = trial_agent.TrialAgent(self.client, lambda who: who in self.eligible, ask=ask, data_dir=self.dir,
                                            budget=self.budget)
        self.agent.started_ms = 0
        repo = mock.patch.object(trial_turn.room_state, "repo_of", mock.AsyncMock(return_value="acme/app"))
        repo.start()
        self.addCleanup(repo.stop)

    async def fetch_balance(self):
        return self.balance

    def run_(self, coro):
        return asyncio.run(coro)

    def said(self):
        return [c["body"] for _, c in self.posts]

    def enrol(self, who=NEW):
        self.client.rooms["!trialdm"] = room("!trialdm", who)
        self.assertEqual(self.run_(self.agent.enroll(who)), "!trialdm")


class Enrolling(Base):
    def test_enrolling_opens_a_private_labelled_chat_with_a_plain_hello(self):
        self.enrol()
        kw = self.client.room_create.call_args.kwargs
        self.assertEqual((kw["is_direct"], kw["invite"], kw["name"]), (True, [NEW], "Trial agent"))
        self.assertEqual(kw["power_level_override"], {"invite": 100})    # only it adds people
        hello = self.said()[0]
        for words in ("shared, limited", "can only talk", "Ollama Cloud", "bring my own", "2 messages a day"):
            self.assertIn(words, hello)
        self.assertEqual(self.posts[0][1]["msgtype"], "m.notice")        # agents don't read notices
        self.assertNotIn("formatted_body", self.posts[0][1])
        self.assertEqual(json.loads((self.dir / "trial.json").read_text())[NEW]["dm"], "!trialdm")   # on disk

    def test_enrolling_twice_reuses_the_chat(self):
        self.enrol()
        self.enrol()
        self.assertEqual(self.client.room_create.await_count, 1)


class Answering(Base):
    def test_its_visitor_gets_an_answer_in_their_private_chat(self):
        self.enrol()
        self.run_(hear(self.agent, self.client.rooms["!trialdm"], message(NEW, "What's a good first issue?")))
        self.assertEqual(self.said()[-1], "An answer.\n\n— Trial agent · glm-5.3")   # every reply names its model
        self.assertEqual(self.asked[0][-1], {"role": "user", "content": "What's a good first issue?"})

    def test_it_never_answers_agents_bots_pm_itself_or_strangers(self):
        self.enrol()
        dm = room("!trialdm", NEW, "@anna-agent:" + S, "@admin-bot:" + S, PM, ANNA)
        for sender in ("@anna-agent:" + S, "@admin-bot:" + S, PM, ME, ANNA):
            self.run_(hear(self.agent, dm, message(sender, "Trial agent, hello", mentions=[ME])))
        self.assertEqual((len(self.posts), self.asked), (1, []))          # only the hello

    def test_another_agent_enrolled_by_mistake_is_still_ignored(self):
        bad = "@anna-agent:" + S
        self.agent.visitors[bad] = {"dm": "!x"}
        self.agent.rooms["!x"] = [bad]
        self.eligible.add(bad)
        self.run_(hear(self.agent, room("!x", bad), message(bad, "loop?")))
        self.assertEqual(self.asked, [])

    def test_in_a_project_room_only_a_mention_by_the_visitor_who_brought_it(self):
        self.enrol()
        self.enrol(OTHER)
        project = room("!proj", NEW, OTHER)
        self.run_(self.agent.on_invite(project, SimpleNamespace(state_key=ME, membership="invite", sender=NEW)))
        self.run_(hear(self.agent, project, message(NEW, "just chatting")))           # not for it
        self.run_(hear(self.agent, project, message(OTHER, "Trial agent, hi", [ME])))   # didn't bring it here
        self.assertEqual(self.asked, [])
        self.run_(hear(self.agent, project, message(NEW, "Trial agent (shared, limited): hi", [ME])))
        self.assertEqual(len(self.asked), 1)

    def test_only_in_rooms_the_visitor_is_in(self):
        self.enrol()
        self.agent.rooms["!gone"] = [NEW]
        self.run_(hear(self.agent, room("!gone", ANNA, invited=(NEW,)), message(NEW, "hi", [ME])))
        self.assertEqual(self.asked, [])

    def test_old_messages_are_not_answered(self):
        self.enrol()
        self.agent.started_ms = NOW_MS
        self.run_(hear(self.agent, self.client.rooms["!trialdm"], message(NEW, "hi", ts=NOW_MS - 60_000)))
        self.assertEqual(self.asked, [])

    def test_a_model_failure_is_said_plainly(self):
        self.enrol()

        async def broken(chat, count=None, model=None):
            raise TrialModelError("the AI service didn't answer in time")
        self.agent.ask_model = broken
        self.run_(hear(self.agent, self.client.rooms["!trialdm"], message(NEW, "hi")))
        self.assertIn("couldn't answer just now: the AI service didn't answer in time", self.said()[-1])

    def test_a_visitor_who_connected_their_own_agent_is_pointed_to_it_once(self):
        self.enrol()
        self.eligible.discard(NEW)
        for _ in range(2):
            self.run_(hear(self.agent, self.client.rooms["!trialdm"], message(NEW, "hi")))
        self.assertEqual(self.said()[1:], [trial_turn.HAS_OWN])
        self.assertEqual(self.asked, [])


class Capped(Base):
    def test_the_visitor_cap_is_said_once_with_the_reset_and_how_to_bring_their_own(self):
        self.enrol()
        dm = self.client.rooms["!trialdm"]
        for n in range(4):
            self.run_(hear(self.agent, dm, message(NEW, f"question {n}")))
        self.assertEqual(len(self.asked), 2)
        capped = self.said()[3:]
        self.assertEqual(len(capped), 1)                                   # then quiet until the reset
        for words in ("today's 2 trial messages", "00:00 UTC", "bring my own"):
            self.assertIn(words, capped[0])

    def test_the_server_cap(self):
        with mock.patch.dict(os.environ, {"TRIAL_PER_SERVER": "1"}):
            self.enrol()
            self.client.rooms["!dm2"] = room("!dm2", OTHER)
            self.agent.visitors[OTHER] = {"dm": "!dm2"}
            self.agent.rooms["!dm2"] = [OTHER]
            self.run_(hear(self.agent, self.client.rooms["!trialdm"], message(NEW, "one")))
            self.run_(hear(self.agent, self.client.rooms["!dm2"], message(OTHER, "two")))
        self.assertEqual(len(self.asked), 1)
        self.assertIn("all of this server's trial messages", self.said()[-1])

    def test_counts_are_kept_on_disk(self):
        self.enrol()
        self.run_(hear(self.agent, self.client.rooms["!trialdm"], message(NEW, "one")))
        again = trial_agent.TrialAgent(self.client, lambda who: True, data_dir=self.dir, budget=self.budget)
        self.assertEqual(again.usage.used(NEW), 1)
        self.assertIn(NEW, again.visitors)


class Rooms(Base):
    def test_an_invite_from_anyone_but_its_visitors_is_declined(self):
        for sender in (ANNA, "@anna-agent:" + S, PM):
            self.run_(self.agent.on_invite(room("!r"), SimpleNamespace(state_key=ME, membership="invite", sender=sender)))
        self.client.join.assert_not_awaited()
        self.assertEqual(self.client.room_leave.await_count, 3)

    def test_it_leaves_when_the_last_visitor_who_brought_it_leaves(self):
        self.enrol()
        self.enrol(OTHER)
        project = room("!proj", NEW, OTHER)
        for who in (NEW, OTHER):
            self.run_(self.agent.on_invite(project, SimpleNamespace(state_key=ME, membership="invite", sender=who)))
        self.run_(self.agent.on_member(project, SimpleNamespace(state_key=NEW, membership="leave")))
        self.client.room_leave.assert_not_awaited()
        self.run_(self.agent.on_member(project, SimpleNamespace(state_key=OTHER, membership="leave")))
        self.client.room_leave.assert_awaited_once_with("!proj")

    def test_invites_sent_while_it_was_away_get_the_same_check(self):
        self.enrol()
        self.client.invited_rooms = {"!a": SimpleNamespace(room_id="!a", inviter=NEW),
                                     "!b": SimpleNamespace(room_id="!b", inviter=ANNA)}
        self.run_(self.agent.invites_while_away())
        self.client.join.assert_awaited_once_with("!a")
        self.client.room_leave.assert_awaited_once_with("!b")


class NeverInTheLoop(Base):
    def test_it_is_nobodys_agent_so_the_loop_never_picks_it(self):
        path = self.dir / "agents.json"
        path.write_text(json.dumps({"anna": {"agent": "hermes"}}))
        with mock.patch.object(registry, "PATH", path):
            project = room("!proj", ANNA, "@anna-agent:" + S, NEW)
            self.assertIsNone(registry.owner_of(trial_agent.LOCALPART))
            self.assertEqual(ownership.agents_in(project), ["@anna-agent:" + S])

    def test_its_name_can_never_be_a_persons_agent(self):
        from common import identity
        self.assertFalse(identity.NAME_RE.match(trial_agent.LOCALPART.removesuffix("-agent")))
        self.assertTrue(trial_agent.LOCALPART.endswith(identity.AGENT_SUFFIX))   # every agent ignores "-agent"s
        self.assertIn(".", trial_agent.LOCALPART)                                # no agent localpart has a "."

    def test_it_never_posts_a_task_or_a_result(self):
        self.enrol()
        self.run_(hear(self.agent, self.client.rooms["!trialdm"], message(NEW, "please merge PR #3")))
        for _, content in self.posts:
            self.assertFalse({github.TASK_KEY, github.RESULT_KEY} & set(content))


class Start(unittest.TestCase):
    def test_no_key_means_no_trial_agent(self):
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(trial_agent, "sign_in", mock.AsyncMock()) as sign_in:
            self.assertIsNone(asyncio.run(trial_agent.start(lambda who: True)))
        sign_in.assert_not_awaited()

    def test_a_failed_sign_in_leaves_it_off(self):
        with mock.patch.dict(os.environ, {"OLLAMA_API_KEY": "k" * 20}), \
                mock.patch.object(trial_agent, "sign_in", mock.AsyncMock(side_effect=OSError("down"))):
            self.assertIsNone(asyncio.run(trial_agent.start(lambda who: True)))

    def test_defaults_are_ollamas_cloud(self):
        self.assertEqual(trial_limits.DEFAULT_URL, "https://ollama.com/api/chat")


if __name__ == "__main__":
    unittest.main()
