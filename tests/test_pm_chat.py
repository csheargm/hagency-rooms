"""Unit tests: talking to @pm in plain words. Run: .venv/bin/python -m unittest tests.test_pm_chat"""
# TODO(R4, 366 lines): split Brain + BrainParse (line 230 on) into test_pm_brain.py, importing Talk.
import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from nio import RoomGetStateResponse, RoomPutStateResponse

from common import github
from common import intents
from server import pm_brain
from common import room_state
from server.loop import Loop

ANNA, BETTY = "@anna:bridge.localhost", "@betty:bridge.localhost"     # anna owns the room
PM = f"@{github.PM_BOT}:bridge.localhost"


class Intents(unittest.TestCase):
    def test_plain_words(self):
        cases = {"do this project all the way to the end": intents.AUTONOMOUS,
                 "just get it done": intents.AUTONOMOUS, "get all done": intents.AUTONOMOUS, "finish all": intents.AUTONOMOUS,
                 "build it but let us approve each change": intents.ASSIST,
                 "check with us first": intents.ASSIST,
                 "stop": intents.OFF, "turn it all off": intents.OFF, "kill all the AI": intents.OFF,
                 "pause everything": intents.OFF,
                 "where are we?": intents.STATUS, "what's left": intents.STATUS,
                 "let's build it": intents.PLAN, "that's great! let's do it!": intents.PLAN,
                 "go for it": intents.PLAN, "can you reiterate teh plan": intents.SHOW_PLAN,
                 "show me the plan": intents.SHOW_PLAN, "let me know your plan": intents.SHOW_PLAN,
                 "what's your model?": intents.ABOUT, "help": intents.HELP, "hello there": intents.UNKNOWN}
        for text, want in cases.items():
            self.assertEqual(intents.classify(text), want, text)

    def test_mixed_signals_take_the_safer_meaning(self):
        self.assertEqual(intents.classify("just get it done but check with us before merging"), intents.ASSIST)
        self.assertEqual(intents.classify("take it all the way, actually no, stop"), intents.OFF)

    def test_any_ok_emoji_confirms(self):
        """Seen live: anna reacted 👌, which wasn't accepted."""
        for key in ("✅", "👌", "👍🏽", "✔️", "☑️", "🆗"):
            self.assertTrue(intents.is_confirm_reaction(key), key)
        for key in ("❤️", "👎", "😂"):
            self.assertFalse(intents.is_confirm_reaction(key), key)

    def test_yes(self):
        for t in ("yes", "Yes!", "✅", "ok", "do it"):
            self.assertTrue(intents.is_yes(t), t)
        self.assertFalse(intents.is_yes("yes but only the readme"))

    def test_an_approval_in_a_sentence(self):
        """the owner: "ok and ok go ahead is the same meaning in this context"."""
        for t in ("ok go ahead", "Okay, go ahead!", "sounds good, let's build it", "yes please", "LGTM 👍",
                  "looks great, ship it", "that's fine, go", "ok let\u2019s do it", "approved"):
            self.assertTrue(intents.is_approval(t), t)
        for t in ("go ahead but skip #2", "not yet", "ok, wait", "no", "don't go ahead", "ok but only the readme",
                  "go ahead with issue 2", "let's talk about it", "hold on", "ok 👎", ""):
            self.assertFalse(intents.is_approval(t), t)


class Talk(unittest.TestCase):
    """The phrase rules (the understanding step off; see Brain for the model path)."""
    def setUp(self):
        for target, name, value in ((pm_brain, "BRAIN", "off"),
                                    (room_state, "bound", mock.AsyncMock(return_value=True)),
                                    (room_state, "repo_of", mock.AsyncMock(return_value="your-name/hagency-hello")),
                                    (room_state, "binding_of", mock.AsyncMock(return_value=("your-name/hagency-hello", None)))):   # a project room
            p = mock.patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.lp = Loop.__new__(Loop)
        self.lp.room_repos = {}
        self.lp.user_id, self.lp.pending, self.lp.boards = PM, {}, {}
        self.lp.generation, self.lp.busy = {}, set()
        self.said, self.levels = [], []

        async def say(room_id, body, mention=None, task=None, **kw):
            self.said.append(body)
            return f"$m{len(self.said)}"

        async def put_state(room_id, kind, content):
            self.levels.append(content["level"])
            return RoomPutStateResponse("$s", room_id)
        create = RoomGetStateResponse([{"type": "m.room.create", "sender": ANNA, "content": {"room_version": "12"}}], "!r")
        self.lp.say = say
        self.lp.client = SimpleNamespace(room_put_state=put_state, room_get_state=mock.AsyncMock(return_value=create))
        self.room = SimpleNamespace(room_id="!r", users={ANNA: None, BETTY: None}, invited_users={},
                                    power_levels=SimpleNamespace(get_user_level=lambda u: 0))

    def test_go_back_to_the_official_bridge(self):
        """Through @pm (official code), so it works even when Betty's own version is broken."""
        with mock.patch("common.customize.go_back", return_value=True) as back:
            self.tell(BETTY, "@pm go back to the official bridge")
        back.assert_called_once_with("betty")
        self.assertIn("going back to the official version", self.said[-1])

    def tell(self, sender, body, mention=True, reply_to=None):
        content = {"body": body, "m.mentions": {"user_ids": [PM]} if mention else {}}
        if reply_to:
            content["m.relates_to"] = {"m.in_reply_to": {"event_id": reply_to}}
        ev = SimpleNamespace(sender=sender, body=body, source={"content": content})

        async def go():                       # decisions run in the background: wait for them too
            await self.lp.on_human(self.room, ev)
            await asyncio.gather(*self.lp.busy)
        asyncio.run(go())

    def react(self, sender, key, to):
        asyncio.run(self.lp.on_reaction(self.room, SimpleNamespace(sender=sender, key=key, reacts_to=to)))

    def test_stopping_an_unbound_room_doesnt_bind_it(self):
        """GPT review 9: a contributor's "off" wrote the repo binding."""
        with mock.patch.object(room_state, "binding_of", mock.AsyncMock(return_value=(None, False))):
            self.tell(BETTY, "@pm stop")
        self.assertEqual(self.levels, [])

    def test_anyone_can_stop_at_once(self):
        self.tell(BETTY, "@pm stop everything")
        self.assertEqual(self.levels, ["off"])
        self.assertIn("Stopped", self.said[-1])

    def test_owner_says_take_it_all_the_way_and_it_happens(self):
        """the owner: "I already said go all the way, why need another ok reaction?" """
        self.tell(ANNA, "@pm take it all the way")
        self.assertEqual(self.levels, ["autonomous"])
        self.assertIn("Running on my own now", self.said[-1])

    def test_a_stray_yes_doesnt_approve_someone_elses_plan(self):
        self.room.power_levels = SimpleNamespace(get_user_level=lambda u: 50)    # betty is a maintainer too
        self.lp.pending["!r"] = {"kind": "plan", "event_id": "$p", "until": time.time() + 60, "by": ANNA,
                                 "items": [], "agent": "@anna-agent:x"}
        self.tell(BETTY, "yes", mention=False)
        self.react(BETTY, "✅", "$p")
        self.assertIn("!r", self.lp.pending)

    def test_ok_addressed_to_pm_approves_the_plan(self):
        """Seen live: "@Product Manager: ok" was read as a change request and re-planned."""
        filed = []

        async def file_plan(room, pending, approver):
            filed.append(approver)
        self.lp.file_plan = file_plan
        self.lp.pending["!r"] = {"kind": "plan", "event_id": "$p", "until": time.time() + 60, "by": ANNA,
                                 "items": [], "agent": "@anna-agent:x"}
        self.tell(ANNA, "Product Manager: ok")
        self.assertEqual(filed, [ANNA])

    def test_ok_go_ahead_approves_the_plan(self):
        """the owner: "ok go ahead" started a NEW plan ("go ahead" = plan this) instead of approving."""
        filed, planned = [], []

        async def file_plan(room, pending, approver):
            filed.append(approver)
        self.lp.file_plan, self.lp.start_plan = file_plan, mock.AsyncMock(side_effect=lambda *a: planned.append(a))
        for words, kw in (("@pm ok go ahead", {}), ("sounds good, go ahead", {"mention": False, "reply_to": "$p"})):
            self.lp.pending["!r"] = {"kind": "plan", "event_id": "$p", "until": time.time() + 60, "by": ANNA,
                                     "items": [], "agent": "@anna-agent:x"}
            self.tell(ANNA, words, **kw)
        self.assertEqual((filed, planned), ([ANNA, ANNA], []))

    def test_go_ahead_but_change_something_is_not_an_approval(self):
        filed = []

        async def file_plan(room, pending, approver):
            filed.append(approver)
        self.lp.file_plan, self.lp.start_plan = file_plan, mock.AsyncMock()
        self.lp.pending["!r"] = {"kind": "plan", "event_id": "$p", "until": time.time() + 60, "by": ANNA,
                                 "items": [], "agent": "@anna-agent:x"}
        self.tell(ANNA, "sounds good but drop the toggle", mention=False, reply_to="$p")
        self.tell(BETTY, "sounds good", mention=False)          # people chatting: not addressed, not a bare ok
        self.assertEqual(filed, [])

    def spoken(self, words):
        ev = SimpleNamespace(sender=ANNA, body=words, voice=True, event_id="$v",
                             source={"content": {"body": words, "m.mentions": {"user_ids": [PM]}}})
        asyncio.run(self.lp.on_human(self.room, ev))

    def test_spoken_words_never_approve_or_change_settings_by_themselves(self):
        """GPT review 14: a transcript may hold someone else's voice or a misheard word."""
        self.lp.file_plan, self.lp.offer_thumb = mock.AsyncMock(), mock.AsyncMock()
        self.lp.set_follow = mock.AsyncMock()
        self.lp.pending["!r"] = {"kind": "plan", "event_id": "$p", "until": time.time() + 60, "by": ANNA,
                                 "items": [], "agent": "@anna-agent:x"}
        self.spoken("okay, go ahead")
        self.assertIn("click 👍", self.said[-1])
        self.lp.file_plan.assert_not_called()
        self.spoken("always bring my agent")                    # a plan is waiting: one question at a time
        self.assertIn("answer my question above first", self.said[-1])
        self.lp.pending.clear()
        self.spoken("always bring my agent")
        self.assertIn("Bring your agent along", self.said[-1])
        self.lp.set_follow.assert_not_called()

    def test_the_retired_web_switch_is_explained_never_set(self):
        """ADR 0006: the owner's agent always has the web when they ask it; there's no switch."""
        self.lp.offer_thumb, self.lp.client.room_put_state = mock.AsyncMock(), mock.AsyncMock()
        self.tell(ANNA, '@pm let my agent search the web here')
        self.assertIn("no web switch", self.said[-1])
        self.lp.client.room_put_state.assert_not_called()
        self.lp.offer_thumb.assert_not_called()

    def test_ok_in_a_reply_to_the_agents_message_approves(self):
        """Seen live: anna replied "@Product Manager: ok" to her agent's message; Element puts the
        quoted message into the body, so it didn't read as ok."""
        filed = []

        async def file_plan(room, pending, approver):
            filed.append(approver)
        self.lp.file_plan = file_plan
        self.lp.pending["!r"] = {"kind": "plan", "event_id": "$p", "until": time.time() + 60, "by": ANNA,
                                 "items": [], "agent": "@anna-agent:x"}
        self.tell(ANNA, "> <@anna-agent:bridge.localhost> I sent @pm a one-item plan\n\nProduct Manager: ok")
        self.assertEqual(filed, [ANNA])

    def test_ok_to_an_already_approved_plan_says_so(self):
        """the owner: "it was a previous message, I ok'ed it, it should say the tasks are all done"."""
        self.lp.agents_in = lambda room: []
        self.lp.client.rooms = {"!r": self.room}
        self.lp.level = mock.AsyncMock(return_value="autonomous")
        self.lp.note_outcome("$old", "approved, and I opened #7")
        self.tell(ANNA, "ok", reply_to="$old")
        self.assertIn("already taken care of: approved, and I opened #7", self.said[-1])
        self.assertNotIn("didn't catch", self.said[-1])

    def test_ok_to_pm_with_nothing_waiting_says_so(self):
        self.lp.agents_in = lambda room: []
        self.lp.client.rooms = {"!r": self.room}
        self.lp.level = mock.AsyncMock(return_value="off")
        self.tell(ANNA, "@pm ok")
        self.assertIn("Nothing is waiting for your OK", self.said[-1])

    def test_a_bare_ok_between_people_is_left_alone(self):
        self.tell(BETTY, "ok", mention=False)
        self.assertEqual(self.said, [])

    def test_stop_cancels_a_waiting_plan_approval_too(self):
        self.lp.pending["!r"] = {"kind": "autonomous", "event_id": "$p", "until": time.time() + 60, "by": ANNA}
        self.tell(BETTY, "@pm wait, stop")
        self.assertNotIn("!r", self.lp.pending)
        self.assertEqual(self.levels, ["off"])

    def test_contributors_cannot_change_the_mode(self):
        self.tell(BETTY, "@pm take it all the way")
        self.tell(BETTY, "@pm let us approve each change")
        self.assertEqual(self.levels, [])
        self.assertIn("Only the room owner or a maintainer", self.said[-1])

    def test_a_contributor_can_ask_the_owner_to_start(self):
        """Seen in the first live demo: betty said "@pm let's do it!"."""
        started = []

        async def start_plan(room, sender):
            started.append(sender)
        self.lp.start_plan = start_plan
        self.tell(BETTY, "@pm that's great! let's do it!")
        self.assertIn("betty would like to turn this discussion into work", self.said[-1])
        self.react(BETTY, "✅", "$m1")                      # betty can't approve her own request
        self.assertEqual(started, [])
        self.react(ANNA, "✅", "$m1")
        self.assertEqual(started, [ANNA])                   # planned with the owner's agent

    def test_assist_needs_no_check(self):
        self.tell(ANNA, "@pm let us approve each change")
        self.assertEqual(self.levels, ["assist"])

    def test_messages_not_for_pm_are_ignored(self):
        self.tell(ANNA, "betty, should we stop for lunch?", mention=False)
        self.assertEqual((self.levels, self.said), ([], []))

    def test_feedback_on_a_waiting_plan_revises_it(self):
        """Seen live: anna told @pm what to change while the plan waited, and got "didn't catch that"."""
        started = []

        async def start_plan(room, sender):
            started.append(sender)
        self.lp.start_plan = start_plan
        self.lp.pending["!r"] = {"kind": "plan", "event_id": "$p", "until": time.time() + 60, "by": ANNA}
        self.tell(ANNA, "@pm the languages should all be on one page, sized by population")
        self.assertEqual(started, [ANNA])
        self.assertIn("Updating the plan", self.said[-1])
        self.assertNotIn("!r", self.lp.pending)

    def test_a_longer_note_without_a_plan_is_acknowledged(self):
        self.tell(BETTY, "@pm the greeting colours should match each country's flag")
        self.assertIn("Noted", self.said[-1])

    def test_unknown_gets_a_hint(self):
        self.tell(ANNA, "@pm what's the weather")
        self.assertIn("show the plan", self.said[-1])

    def test_level_shortcut_still_works(self):
        self.tell(ANNA, "!level autonomous", mention=False)
        self.assertEqual(self.levels, ["autonomous"])

    def test_agents_are_not_people(self):
        self.assertFalse(self.lp.is_person(self.room, "@anna-agent:bridge.localhost"))
        self.assertTrue(self.lp.is_person(self.room, ANNA))


class Brain(Talk):
    """The understanding step decides the action from context; code still checks permissions."""
    def setUp(self):
        super().setUp()
        self.decision = None
        self.calls = []

        async def understand(ctx):
            self.calls.append(ctx)
            return self.decision
        for target, name, value in ((pm_brain, "BRAIN", "codex"), (pm_brain, "understand", understand)):
            p = mock.patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.lp.level = mock.AsyncMock(return_value="autonomous")
        self.lp.discussion = lambda room_id, include_self=False: "anna: hello page\nbetty: in many languages"
        self.lp.client.room_typing = mock.AsyncMock()
        self.lp.client.room_get_event = mock.AsyncMock(return_value=None)
        self.lp.client.rooms = {"!r": self.room}
        self.lp.agents_in = lambda room: []

    def test_a_question_gets_the_models_answer_with_the_conversation_as_context(self):
        self.decision = ("none", "Yes — everything is merged; the site is live.")
        self.tell(ANNA, "@pm is everything finished?")
        self.assertEqual(self.said[-1], "Yes — everything is merged; the site is live.")
        self.assertIn("in many languages", self.calls[0]["transcript"])
        self.assertEqual(self.calls[0]["role"], "owner or maintainer")

    def test_the_models_action_still_goes_through_permissions(self):
        self.decision = ("autonomous", "On it!")
        self.tell(BETTY, "@pm please just finish everything yourself")
        self.assertEqual(self.levels, [])
        self.assertIn("Only the room owner or a maintainer", self.said[-1])

    def test_actions_are_confirmed_by_code_not_by_the_model(self):
        self.decision = ("autonomous", "Done, merged everything!")      # the model can't claim results
        self.tell(ANNA, "@pm ok, take it all the way")
        self.assertEqual(self.levels, ["autonomous"])
        self.assertIn("Running on my own now", self.said[-1])
        self.assertNotIn("merged everything", " ".join(self.said))

    def test_a_setting_the_rules_miss_is_asked_with_one_click(self):
        """the owner: "the agent should understand me" — not "say it in so many words"."""
        self.decision = ("follow_always", "Sure")
        followed = []
        self.lp.set_follow = mock.AsyncMock(side_effect=lambda room, who, value: followed.append((who, value)))
        self.lp.offer_thumb = mock.AsyncMock()
        self.tell(BETTY, "@pm my agent should be with me wherever I work")
        self.assertIn("Bring your agent along", self.said[-1])
        self.assertEqual(followed, [])                      # not before the person's own 👍
        self.react(BETTY, "👍", self.lp.pending["!r"]["event_id"])
        self.assertEqual(followed, [(BETTY, "always")])

    def test_stop_never_waits_for_the_model(self):
        self.tell(BETTY, "@pm stop")
        self.assertEqual((self.levels, self.calls), (["off"], []))

    def test_the_model_failing_falls_back_to_the_rules(self):
        self.decision = None
        self.tell(ANNA, "@pm where are we?")
        self.assertIn("Mode:", self.said[-1])

    def test_approve_from_someone_else_is_refused(self):
        self.lp.pending["!r"] = {"kind": "plan", "event_id": "$p", "until": time.time() + 60, "by": ANNA,
                                 "items": [], "agent": "@anna-agent:x"}
        self.room.power_levels = SimpleNamespace(get_user_level=lambda u: 50)
        self.decision = ("approve", "")
        self.tell(BETTY, "@pm yes")
        self.assertIn("That's waiting for anna to approve", self.said[-1])
        self.assertIn("!r", self.lp.pending)

    def test_injected_context_cant_switch_modes_without_the_persons_own_words(self):
        """GPT review 6: other people's messages in the context could steer the model into a mode change."""
        self.decision = ("autonomous", "")
        self.tell(ANNA, "@pm what's the weather like for the demo?")
        self.assertEqual(self.levels, [])
        self.assertIn("Do you want me to switch to **autonomous**", self.said[-1])
        self.react(ANNA, "✅", self.lp.pending["!r"]["event_id"])
        self.assertEqual(self.levels, ["autonomous"])

    def test_injected_context_cant_approve_without_the_persons_own_words(self):
        self.lp.pending["!r"] = {"kind": "plan", "event_id": "$p", "until": time.time() + 60, "by": ANNA,
                                 "items": [], "agent": "@anna-agent:x"}
        self.decision = ("approve", "")
        self.tell(ANNA, "@pm how many issues is that?")
        self.assertIn("click 👍", self.said[-1])
        self.assertIn("!r", self.lp.pending)

    def test_a_stop_while_thinking_cancels_the_decision(self):
        self.decision = ("none", "Sure, here's a long answer")
        gate = asyncio.Event()

        async def slow(ctx):
            await gate.wait()
            return self.decision

        async def scenario():
            with mock.patch.object(pm_brain, "understand", slow):
                await self.lp.on_human(self.room, SimpleNamespace(sender=ANNA, body="@pm tell me everything",
                                                                  source={"content": {"m.mentions": {"user_ids": [PM]}}}))
                await self.lp.on_human(self.room, SimpleNamespace(sender=BETTY, body="@pm stop",
                                                                  source={"content": {"m.mentions": {"user_ids": [PM]}}}))
                gate.set()
                await asyncio.gather(*self.lp.busy)
        asyncio.run(scenario())
        self.assertEqual(self.levels, ["off"])
        self.assertNotIn("Sure, here's a long answer", self.said)


class BrainParse(unittest.TestCase):
    def test_only_known_actions_are_accepted(self):
        self.assertEqual(pm_brain.parse('sure: {"action": "status", "reply": "Here you go"}'), ("status", "Here you go"))
        for bad in ("no json", '{"action": "delete_repo"}', '{"reply": "x"}', "{oops}"):
            self.assertIsNone(pm_brain.parse(bad), bad)

    def test_links_and_odd_types_from_the_model_are_refused(self):
        self.assertIn("(link removed)", pm_brain.parse('{"action": "none", "reply": "see https://evil.example/x"}')[1])
        self.assertIsNone(pm_brain.parse('{"action": ["approve"], "reply": "x"}'))

    def test_the_reply_is_room_safe_and_short(self):
        from pathlib import Path
        _, reply = pm_brain.parse('{"action": "none", "reply": "see ' + str(Path.home()) + '/x and ' + "y" * 900 + '"}')
        self.assertNotIn(str(Path.home()), reply)
        self.assertLessEqual(len(reply), pm_brain.MAX_REPLY)


class Moderator(unittest.TestCase):
    def check(self, sender, version, pl=0):
        state = RoomGetStateResponse([{"type": "m.room.create", "sender": ANNA, "state_key": "",
                                       "content": {"room_version": version}}], "!r")
        client = SimpleNamespace(room_get_state=mock.AsyncMock(return_value=state))
        r = SimpleNamespace(room_id="!r", power_levels=SimpleNamespace(get_user_level=lambda u: pl))
        return asyncio.run(room_state.is_moderator(client, r, sender))

    def test_v12_creator_is_moderator_without_pl_entry(self):
        self.assertTrue(self.check(ANNA, "12"))

    def test_non_creator_without_pl_is_not(self):
        self.assertFalse(self.check(BETTY, "12"))

    def test_old_room_creator_needs_pl(self):
        self.assertFalse(self.check(ANNA, "10"))

    def test_pl_50_is_moderator(self):
        self.assertTrue(self.check(BETTY, "12", pl=50))


if __name__ == "__main__":
    unittest.main()
