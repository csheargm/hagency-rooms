"""Panel cards (ADR 0019): the contract, the agent's task, @pm's checks and the panel's flow.
Run: .venv/bin/python -m unittest tests.test_cards"""
import asyncio
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from adapters.agents import bridge, card_task, tasks
from common import cards
from server import pm_cards
from tests import ROOT

S = "bridge.localhost"
ANNA, BETTY = f"@anna:{S}", f"@betty:{S}"
ANNA_AGENT, BETTY_AGENT = f"@anna-agent:{S}", f"@betty-agent:{S}"
CARD = "<style>b{color:red}</style><div id=x>no data yet</div><script>hagency.onData(d => x.textContent = d.room)</script>"
REPLY = f"TITLE: Who built what\n```html\n<!DOCTYPE html>{CARD}\n```"


class Contract(unittest.TestCase):
    def test_a_reply_gives_its_title_and_html_without_a_doctype(self):
        self.assertEqual(cards.parse_reply(REPLY), ("Who built what", CARD))

    def test_bare_html_and_a_missing_title_still_make_a_card(self):
        self.assertEqual(cards.parse_reply(CARD), ("My card", CARD))
        self.assertEqual(cards.parse_reply(f"```html\n{CARD}\n```")[0], "My card")

    def test_an_answer_without_a_card_or_too_big_is_refused(self):
        for text in ("Sorry, I can't do that.", f"```html\n{'x' * (cards.MAX_HTML + 1)}\n```", "```html\n  \n```"):
            with self.assertRaises(cards.CardError):
                cards.parse_reply(text)

    def test_size_counts_as_sent_so_any_language_fits_a_matrix_event(self):
        with self.assertRaises(cards.CardError):
            cards.clean_html("<p>" + "构建" * (cards.MAX_HTML // 10) + "</p>")   # 8k characters, 48k escaped
        self.assertTrue(cards.clean_html("<p>" + "构建" * 100 + "</p>"))

    def test_titles_are_short_plain_text(self):
        self.assertEqual(cards.clean_title("  **A‮ very  long title that goes on and on and on and on**"),
                         "A very long title that goes on and on an")

    def test_requests_are_one_short_line(self):
        self.assertEqual(cards.clean_request("  a chart\n of   builds "), "a chart of builds")
        for bad in ("", "   ", "x" * (cards.MAX_REQUEST + 1), "chart‮"):
            self.assertIsNone(cards.clean_request(bad))

    def test_the_frame_blocks_the_network_before_the_card_runs(self):
        doc = cards.frame_doc("<!doctype html>" + CARD)
        self.assertTrue(doc.startswith("<!doctype html>"))
        self.assertEqual(doc.lower().count("<!doctype"), 1)
        policy = re.search(r'http-equiv="Content-Security-Policy" content="([^"]+)"', doc).group(1)
        self.assertIn("default-src 'none'", policy)
        self.assertNotIn("connect-src", policy)
        self.assertLess(doc.index("Content-Security-Policy"), doc.index(CARD))
        self.assertIn("e.source !== parent", doc)                    # only the panel's data counts

    def test_the_prompt_tells_the_agent_the_data_and_the_rules(self):
        prompt = cards.card_prompt("a chart of builds")
        for text in ("a chart of builds", cards.DATA_DOC, "TITLE:", "```html", "network", "don't repeat it inside"):
            self.assertIn(text, prompt)
        self.assertNotIn("The card is now", prompt)
        self.assertIn("The card is now:\n```html\n<b>old</b>", cards.card_prompt("bigger", "<b>old</b>"))

    def test_the_data_described_is_the_data_given(self):
        """DATA_DOC (what the agent is told) names exactly card_data's fields, plus mode from the panel."""
        loop = Fake()
        given = set(pm_cards.card_data(loop, loop.room, ANNA)) | {"mode"}
        told = set(re.findall(r"\bd\.([a-z]+)", cards.DATA_DOC))
        self.assertEqual(given, told)


class AgentTask(unittest.TestCase):
    def test_the_agent_answers_with_a_card_in_a_read_only_fresh_turn(self):
        turn = mock.AsyncMock(return_value=REPLY)
        with mock.patch.object(card_task.agents, "run_turn", turn):
            r = asyncio.run(tasks.handle("anna-agent", "codex", Path("/tmp/w"),
                                         {"kind": "card", "task_id": "t1", "request": "who built what", "card": ""}))
        self.assertEqual((r["kind"], r["title"], r["html"], r["task_id"]), ("card", "Who built what", CARD, "t1"))
        self.assertEqual(r["line"], "panel card ready: Who built what")
        args, kw = turn.call_args
        self.assertEqual(args[1], Path("/tmp/w/card"))
        self.assertEqual(kw, {"fresh": True})                         # never write=True: it runs nothing

    def test_a_change_sends_the_card_as_it_is(self):
        turn = mock.AsyncMock(return_value=REPLY)
        with mock.patch.object(card_task.agents, "run_turn", turn):
            asyncio.run(tasks.handle("anna-agent", "codex", Path("/tmp/w"),
                                     {"kind": "card", "task_id": "t1", "request": "bigger", "html": "<b>old</b>", "card": "c1"}))
        self.assertIn("<b>old</b>", turn.call_args[0][2])

    def test_a_bad_answer_comes_back_as_an_error_result(self):
        with mock.patch.object(card_task.agents, "run_turn", mock.AsyncMock(return_value="no idea")):
            r = asyncio.run(tasks.handle("anna-agent", "codex", Path("/tmp/w"), {"kind": "card", "task_id": "t1", "request": "x"}))
        self.assertEqual((r["kind"], r["task_id"]), ("card", "t1"))
        self.assertIn("held no card", r["error"])

    def test_a_card_works_in_a_room_with_no_repo(self):
        b = bridge.Bridge("anna", "codex")
        b.send, b.keep_typing = mock.AsyncMock(), mock.AsyncMock()
        b.client = SimpleNamespace(room_typing=mock.AsyncMock())
        handle = mock.AsyncMock(return_value={"kind": "card", "line": "panel card ready: X", "task_id": "t1"})
        with mock.patch.object(bridge.room_state, "repo_of", mock.AsyncMock(return_value=None)), \
                mock.patch.object(bridge.tasks, "handle", handle):
            asyncio.run(b.run_task("!r", {"kind": "card", "task_id": "t1", "repo": "", "request": "x"}))
        handle.assert_awaited_once()
        self.assertIn("no-repo", str(handle.call_args[0][2]))
        self.assertEqual(b.send.call_args_list[0][0][1], "making a panel card…")


class Fake(pm_cards.Cards):
    """Just enough of the Loop for cards: anna and betty with their agents in room !r."""
    def __init__(self):
        self.room = SimpleNamespace(room_id="!r", display_name="Hello", users={ANNA: 0, BETTY: 0, ANNA_AGENT: 0}, invited_users={})
        self.said, self.online = [], True
        self.client = SimpleNamespace(rooms={"!r": self.room})
        self.room_repos, self.boards = {"!r": "you/hello"}, {"!r": {"text": "**Now**\n- #7 building"}}
        self.state = {"you/hello#7": {"room": "!r", "stage": "building", "builder": ANNA_AGENT, "title": "Add a test"}}
        self.open_issues, self.last_plans = {"!r": [{"number": 9, "title": "Docs"}]}, {}

    async def say(self, rid, body, mention=None, task=None, **kw):
        self.said.append((body, mention, task))

    def own_agent(self, room, person):
        mine = {ANNA: ANNA_AGENT, BETTY: BETTY_AGENT}.get(person)
        return mine if mine in room.users else None

    def agent_online(self, agent):
        return self.online

    def agents_in(self, room):
        return [u for u in room.users if u.split(":")[0].endswith("-agent")]


class PmFlow(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(pm_cards, "CARDS_FILE", Path(tempfile.mkdtemp()) / "data" / "cards.json")
        p.start()
        self.addCleanup(p.stop)
        self.loop = Fake()

    def act(self, user, action, value):
        return asyncio.run(pm_cards.act(self.loop, self.loop.room, user, action, value))

    def view(self, user=ANNA):
        return pm_cards.view(self.loop, self.loop.room, user)

    def answer(self, sender=ANNA_AGENT, **result):
        task = self.loop.said[-1][2]
        asyncio.run(self.loop.on_card_result(self.loop.room, sender, {"kind": "card", "task_id": task["task_id"], **result}))

    def test_make_view_and_show_a_card(self):
        """The user flow: ask in the panel → their agent gets the task → the card shows in their panel only."""
        self.assertEqual(self.act(ANNA, "card_make", "who built what"), "ok")
        body, mention, task = self.loop.said[-1]
        self.assertEqual((mention, task["kind"], task["request"], task["repo"]), (ANNA_AGENT, "card", "who built what", "you/hello"))
        self.assertEqual(body, f"{ANNA_AGENT}, please make a Hagency panel card for anna")
        self.assertEqual(self.view()["making"], "who built what")
        self.assertEqual(self.act(ANNA, "card_make", "another"), pm_cards.BUSY)
        self.answer(title="Who built what", html=CARD)
        v = self.view()
        self.assertIsNone(v["making"])
        [item] = v["items"]
        self.assertEqual((item["title"], item["request"]), ("Who built what", "who built what"))
        self.assertNotIn("html", item)                               # fetched once, not every 3 s
        doc = self.act(ANNA, "card_html", item["id"])["doc"]
        self.assertTrue(doc.startswith(cards.FRAME_HEAD) and doc.endswith(CARD))
        self.assertEqual(v["data"]["work"], [{"issue": 7, "title": "Add a test", "stage": "building", "builder": "anna-agent",
                                              "reviewer": None, "pr": None, "ended": None}])
        self.assertEqual(v["data"]["queued"], [{"issue": 9, "title": "Docs"}])
        self.assertEqual(self.view(BETTY)["items"], [])               # only the person who asked sees it
        self.assertEqual(self.act(BETTY, "card_html", item["id"]), pm_cards.GONE)

    def test_change_keeps_the_card_in_place_and_remove_takes_it_away(self):
        self.act(ANNA, "card_make", "first")
        self.answer(title="One", html=CARD)
        self.act(ANNA, "card_make", "second")
        self.answer(title="Two", html="<p>2</p>")
        first, second = self.view()["items"]
        self.assertEqual(self.act(ANNA, "card_change", first["id"] + "\nmake it blue"), "ok")
        task = self.loop.said[-1][2]
        self.assertEqual((task["html"], task["card"], task["request"]), (CARD, first["id"], "make it blue"))
        self.assertEqual(self.loop.said[-1][0], f"{ANNA_AGENT}, please change a Hagency panel card for anna")
        self.assertEqual(self.view()["changing"], first["id"])
        self.answer(title="One, blue", html="<p>blue</p>", card=first["id"])
        items = self.view()["items"]
        self.assertEqual([i["title"] for i in items], ["One, blue", "Two"])
        self.assertEqual(items[0]["id"], first["id"])
        self.assertNotEqual(items[0]["v"], first["v"])                 # the panel redraws that frame
        self.assertTrue(self.act(ANNA, "card_html", first["id"])["doc"].endswith("<p>blue</p>"))
        self.assertEqual(self.act(ANNA, "card_remove", second["id"]), "ok")
        self.assertEqual([i["title"] for i in self.view()["items"]], ["One, blue"])
        self.assertEqual(self.act(ANNA, "card_remove", second["id"]), pm_cards.GONE)
        self.assertEqual(self.act(ANNA, "card_change", "nope\nbigger"), pm_cards.GONE)

    def test_a_card_removed_while_it_was_being_changed_stays_removed(self):
        self.act(ANNA, "card_make", "first")
        self.answer(title="One", html=CARD)
        cid = self.view()["items"][0]["id"]
        self.act(ANNA, "card_change", cid + "\nbigger")
        self.act(ANNA, "card_remove", cid)
        self.answer(title="One", html=CARD, card=cid)
        self.assertEqual(self.view()["items"], [])

    def test_kept_on_disk_across_a_restart(self):
        self.act(ANNA, "card_make", "first")
        self.answer(title="One", html=CARD)
        self.assertEqual([i["title"] for i in pm_cards.view(Fake(), self.loop.room, ANNA)["items"]], ["One"])

    def test_only_the_agent_asked_answers(self):
        self.act(ANNA, "card_make", "first")
        self.answer(sender=BETTY_AGENT, title="Evil", html="<p>x</p>")
        asyncio.run(self.loop.on_card_result(self.loop.room, ANNA_AGENT, {"kind": "card", "task_id": "other", "html": CARD}))
        self.assertEqual(self.view()["items"], [])
        self.assertEqual(self.view()["making"], "first")

    def test_what_comes_back_is_checked_again(self):
        self.act(ANNA, "card_make", "first")
        self.answer(title="Huge", html="x" * (cards.MAX_HTML + 1))
        v = self.view()
        self.assertEqual(v["items"], [])
        self.assertIn("too big", v["error"])
        self.assertIsNone(v["making"])

    def test_failures_say_what_to_do(self):
        self.act(ANNA, "card_make", "first")
        self.answer(error="unknown task kind 'card'")                # an agent adapter from before cards
        self.assertEqual(self.view()["error"], pm_cards.OLD_CODE)
        self.act(ANNA, "card_make", "again")
        self.assertIsNone(self.view()["error"])                       # a new ask clears the old failure
        self.answer(error="codex timed out after 600s")
        self.assertEqual(self.view()["error"], "Your agent couldn't make it: codex timed out after 600s")

    def test_an_agent_that_never_answers_is_given_up_on(self):
        self.act(ANNA, "card_make", "first")
        self.loop.card_jobs()[pm_cards.key_of("!r", ANNA)]["at"] -= pm_cards.WAIT_S + 1
        v = self.view()
        self.assertEqual((v["making"], v["error"]), (None, pm_cards.NO_ANSWER))
        self.assertEqual(self.act(ANNA, "card_make", "again"), "ok")

    def test_refusals_say_why(self):
        self.assertEqual(self.act(ANNA, "card_make", "  "), pm_cards.BAD_REQUEST)
        self.assertEqual(self.act(BETTY, "card_make", "chart"), pm_cards.NO_AGENT)     # betty's agent isn't here
        self.assertFalse(self.view(BETTY)["agent_here"])
        self.loop.online = False
        self.assertEqual(self.act(ANNA, "card_make", "chart"), pm_cards.OFFLINE)
        self.assertEqual(self.loop.said, [])

    def test_limits(self):
        self.loop.card_store()[pm_cards.key_of("!r", ANNA)] = [
            {"id": str(i), "title": "t", "request": "r", "html": "h", "made": 0} for i in range(pm_cards.MAX_PER_PERSON)]
        self.assertEqual(self.act(ANNA, "card_make", "one more"), pm_cards.FULL)
        self.assertEqual(self.act(ANNA, "card_change", "0\nbigger"), "ok")        # changing one is fine
        with mock.patch.object(pm_cards, "MAX_ALL", 6):
            self.assertEqual(self.act(BETTY, "card_make", "x"), pm_cards.SERVER_FULL)
        self.loop._card_makes = {ANNA: [time.time()] * pm_cards.MAKES_PER_HOUR}
        self.loop.card_jobs().clear()
        self.assertEqual(self.act(ANNA, "card_change", "0\nbigger"), pm_cards.TOO_MANY)

    def test_cards_of_people_who_left_go_and_each_person_has_a_cap(self):
        """Review: one person could fill the server's cap; cards of rooms people left counted forever."""
        store = self.loop.card_store()
        store[pm_cards.key_of("!gone", ANNA)] = [{"id": "g", "title": "t", "request": "r", "html": "h", "made": 0}]
        store[pm_cards.key_of("!r", "@left:" + S)] = [{"id": "l", "title": "t", "request": "r", "html": "h", "made": 0}]
        self.assertEqual(self.act(ANNA, "card_make", "first"), "ok")
        self.assertEqual(list(store), [])
        self.loop.card_jobs().clear()
        with mock.patch.object(pm_cards, "MAX_PERSON_ALL", 0):
            self.assertEqual(self.act(ANNA, "card_make", "x"), pm_cards.PERSON_FULL)

    def test_a_stopped_agent_is_named_before_anything_is_sent(self):
        with mock.patch.object(pm_cards.registry, "agents_of", lambda u: [{"localpart": "anna-agent", "stopped": True}]):
            self.assertEqual(self.act(ANNA, "card_make", "chart"), pm_cards.STOPPED)
            self.assertEqual(self.view()["unable"], pm_cards.STOPPED)          # the panel says so instead of the box
        self.assertEqual(self.loop.said, [])

    def test_a_late_answer_after_giving_up_does_not_count(self):
        """GPT review: only view() expired a job, so a late answer counted or not depending on a poll."""
        self.act(ANNA, "card_make", "first")
        self.loop.card_jobs()[pm_cards.key_of("!r", ANNA)]["at"] -= pm_cards.WAIT_S + 1
        self.answer(title="Late", html=CARD)
        self.assertEqual((self.view()["items"], self.view()["error"]), ([], pm_cards.NO_ANSWER))   # still told

    def test_nothing_is_kept_for_someone_who_left_or_is_over_their_cap(self):
        """GPT re-check: a result re-created cards for a person who left; two rooms' makes passed the cap."""
        self.act(ANNA, "card_make", "first")
        del self.loop.room.users[ANNA]
        self.answer(title="One", html=CARD)
        self.assertEqual(dict(self.loop.card_store()), {})
        self.loop.room.users[ANNA] = 0
        self.act(ANNA, "card_make", "again")
        with mock.patch.object(pm_cards, "MAX_PERSON_ALL", 0):
            self.answer(title="One", html=CARD)
        self.assertEqual((self.view()["items"], self.view()["error"]), ([], pm_cards.PERSON_FULL))

    def test_a_send_matrix_refused_leaves_nothing_waiting(self):
        """GPT review: a failed dispatch left the job busy for 15 minutes and used up an attempt."""
        async def refused(*a, **k):
            raise RuntimeError("could not post")
        self.loop.say = refused
        self.assertEqual(self.act(ANNA, "card_make", "first"), pm_cards.NOT_SENT)
        self.assertIsNone(self.view()["making"])
        self.assertEqual(self.loop._card_makes[ANNA], [])

    def test_hermes_agents_are_told_plainly(self):
        """GPT review: Hermes' tools have no card task; it would answer 'unsupported' and could hide older tasks."""
        with mock.patch.object(pm_cards.registry, "agents_of", lambda u: [{"localpart": "anna-agent", "agent_kind": "hermes"}]):
            self.assertEqual(self.act(ANNA, "card_make", "chart"), pm_cards.HERMES)
        self.assertEqual(self.loop.said, [])

    def test_the_server_cap_is_checked_again_when_a_card_arrives(self):
        self.act(ANNA, "card_make", "first")
        with mock.patch.object(pm_cards, "MAX_ALL", 0):
            self.answer(title="One", html=CARD)
        self.assertEqual((self.view()["items"], self.view()["error"]), ([], pm_cards.SERVER_FULL))

    def test_each_card_ask_is_its_own_step_for_the_agent(self):
        """GPT review: a retry with a new task_id was taken for a resend of the one still running."""
        one, two = ({"kind": "card", "repo": "", "task_id": t} for t in ("a", "b"))
        self.assertNotEqual(bridge.step_key(one), bridge.step_key(two))
        self.assertEqual(bridge.step_key({"kind": "build", "issue": 7, "task_id": "a"}),
                         bridge.step_key({"kind": "build", "issue": 7, "task_id": "b"}))


class PanelScript(unittest.TestCase):
    JS = (ROOT / "static" / "panel_cards.js").read_text()

    def test_cards_run_in_a_scripts_only_sandbox(self):
        self.assertEqual(re.findall(r'sandbox="([^"]*)"', self.JS), ["allow-scripts"])
        self.assertNotIn("allow-same-origin", self.JS)
        self.assertIn("f.frame.contentWindow === ev.source", self.JS)   # heights only from our own frames

    def test_a_card_that_leaves_its_document_gets_no_more_data(self):
        self.assertIn("entry.loads === 1", self.JS)                    # data only to the srcdoc document
        self.assertNotIn(" hidden", self.JS)                           # .field{display:flex} beats [hidden] (GPT review)

    def test_the_panel_keeps_card_frames_outside_what_it_redraws(self):
        page = (ROOT / "static" / "panel.html").read_text()
        self.assertIn('<div id="cards" class="stack"></div>', page)
        self.assertIn("showCards(s)", page)
        self.assertIn("showCards(null)", page)                          # My Hagency and signing out: none

    def test_the_script_parses(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("node not installed")
        done = subprocess.run([node, "--check", str(ROOT / "static" / "panel_cards.js")], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr[-500:])


if __name__ == "__main__":
    unittest.main()
