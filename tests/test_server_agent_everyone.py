"""Flow tests: the server agent (ADR 0025 amendment, code name trial) is for everyone. A person with their
own connected agents starts it from @pm and the panel; in a project room where their own agent is too it
answers only when named; a reached personal cap says the limit and that more usage is coming with paid
plans; each person's tier record defaults to free and sets their limits (trial_tiers.py).
Run: .venv/bin/python -m unittest tests.test_server_agent_everyone"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from common import registry
from server import pm_trial
from server import trial_limits
from server import trial_tiers
from server import trial_turn
from tests.test_trial_agent import hear, message
from tests.test_trial_flow import ME, TrialBase, said_to
from tests.test_welcome import DM, NEW, S, reaction, text

OWN_AGENT = f"@newbie1-agent:{S}"
UPGRADE = "More usage is coming with paid plans."


class OwnerWithAgents(TrialBase):
    """NEW has their own agent, connected: nothing about the server agent is refused to them."""
    def setUp(self):
        super().setUp()
        path = Path(tempfile.mkdtemp()) / "agents.json"
        path.write_text(json.dumps({NEW: {"agent": "remote"}}))
        p = mock.patch.object(registry, "PATH", path)
        p.start()
        self.addCleanup(p.stop)
        self.lp._seen = {registry.agent_of(NEW): {"online": True, "ever": True}}
        self.assertTrue(self.lp.has_connected_agent(NEW))

    def accept(self):
        dm = SimpleNamespace(room_id="!trialdm", users={ME: None, NEW: None}, invited_users={})
        self.trial_client.rooms["!trialdm"] = dm
        return dm

    def test_an_owner_with_their_own_agent_can_start_it_and_it_answers(self):
        self.assertTrue(self.lp.trial_eligible(NEW))
        said = self.run_(self.lp.start_trial("!dm", NEW))
        self.assertTrue(said.startswith("🧪 Your server agent is ready"), said)
        self.assertIn("mention it by name", said)
        self.assertEqual(self.trial_client.room_create.call_args.kwargs["name"], "Server agent")
        self.run_(hear(self.lp.trial, self.accept(), message(NEW, "What is a pull request?")))
        self.assertEqual(self.trial_posts[-1][1], "A pull request is a proposed change.\n\n— Server agent · glm-5.3")

    def test_typing_server_agent_or_its_old_button_starts_it(self):
        self.lp._welcomes[NEW] = {"dm": "!dm", "offer": "$offer", "done": []}
        self.assertTrue(self.run_(self.lp.on_welcome_text(DM, text(NEW, "try the server agent"))))
        self.assertTrue(said_to(self, "!dm")[-1].startswith("🧪 Your server agent is ready"))
        self.run_(self.lp.on_welcome_reaction(DM, reaction(NEW, "🏠")))   # an old offer's 🏠: no more "not here yet"
        self.assertTrue(said_to(self, "!dm")[-1].startswith("🧪 Your server agent is ready"))

    def test_the_panel_card_shows_for_them_with_todays_usage(self):
        self.assertEqual(self.lp.trial_view(NEW)["on"], False)
        self.assertEqual(self.run_(self.lp.trial_action(NEW))["result"], "ok")
        self.run_(hear(self.lp.trial, self.accept(), message(NEW, "hi")))
        view = self.lp.trial_view(NEW)
        self.assertEqual({k: view[k] for k in ("on", "used", "per_day", "tier")}, {"on": True, "used": 1, "per_day": 2,
                                                                                   "tier": trial_tiers.FREE})

    def test_in_a_project_room_with_their_own_agent_it_answers_only_when_named(self):
        self.run_(self.lp.start_trial("!dm", NEW))
        project = SimpleNamespace(room_id="!proj", users={ME: None, NEW: None, OWN_AGENT: None}, invited_users={})
        self.lp.trial.add_sponsor("!proj", NEW)               # they invited it there
        before = len(self.trial_posts)
        self.run_(hear(self.lp.trial, project, message(NEW, "what does PR 3 change?")))   # for their own agent
        self.run_(hear(self.lp.trial, project, message(NEW, "newbie1-agent, build #4")))
        self.assertEqual((len(self.trial_posts), len(self.asked)), (before, 0))
        self.run_(hear(self.lp.trial, project, message(NEW, "Server agent, what is a rebase?")))
        self.assertEqual(self.trial_posts[-1][0], "!proj")
        self.run_(hear(self.lp.trial, project, message(NEW, "and a squash?", mentions=[ME])))   # a pill names it too
        self.assertEqual(len(self.asked), 2)

    def test_a_reached_cap_says_the_limit_paid_plans_and_their_own_agent(self):
        self.run_(self.lp.start_trial("!dm", NEW))
        dm = self.accept()
        for n in range(3):
            self.run_(hear(self.lp.trial, dm, SimpleNamespace(sender=NEW, body=f"q{n}", source={"content": {}},
                                                              server_timestamp=10**13, event_id=f"$q{n}")))
        last = self.trial_posts[-1][1]
        for words in ("today's 2 free messages", "00:00 UTC", UPGRADE, "bring my own", "the agent you have"):
            self.assertIn(words, last)
        self.assertEqual(len(self.asked), 2)


class Tiers(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ, {"TRIAL_PER_VISITOR": "7", "TRIAL_VISITOR_TOKENS": "900",
                                           "TRIAL_REMOTE_TOKENS": "300"})
        env.start()
        self.addCleanup(env.stop)
        self.dir = Path(tempfile.mkdtemp())

    def test_everyone_defaults_to_free_with_the_settings_limits(self):
        tiers = trial_tiers.Tiers(self.dir / "t.json")
        usage = trial_limits.Usage(self.dir / "u.json", tiers=tiers)
        self.assertEqual(tiers.tier(NEW), trial_tiers.FREE)
        self.assertEqual((usage.per_day(NEW), usage.allowance(NEW, False), usage.allowance(NEW, True)), (7, 900, 300))
        self.assertEqual(tiers.upgrade_line(NEW), UPGRADE)
        self.assertFalse((self.dir / "t.json").exists())     # a default is never written down
        self.assertEqual(trial_limits.Usage(self.dir / "v.json").per_day(NEW), 7)   # no record file: free too

    def test_a_record_is_kept_on_disk_and_an_unknown_tier_is_free(self):
        tiers = trial_tiers.Tiers(self.dir / "t.json")
        self.assertFalse(tiers.set_tier(NEW, "platinum"))
        self.assertTrue(tiers.set_tier(NEW, trial_tiers.FREE))
        self.assertEqual(json.loads((self.dir / "t.json").read_text())[NEW]["tier"], "free")
        (self.dir / "t.json").write_text(json.dumps({NEW: {"tier": "gone"}, "@x:y": "junk"}))
        again = trial_tiers.Tiers(self.dir / "t.json")
        self.assertEqual((again.tier(NEW), again.tier("@x:y")), (trial_tiers.FREE, trial_tiers.FREE))

    def test_a_later_tier_sets_its_own_limits_in_one_place(self):
        pro = {trial_tiers.MESSAGES: 500, trial_tiers.TOKENS: "TRIAL_VISITOR_TOKENS", trial_tiers.TOKENS_REMOTE: 10}
        with mock.patch.dict(trial_tiers.TIERS, {"pro": pro}):
            tiers = trial_tiers.Tiers(self.dir / "t.json")
            tiers.set_tier(NEW, "pro")
            usage = trial_limits.Usage(self.dir / "u.json", tiers=tiers)
            self.assertEqual((usage.per_day(NEW), usage.allowance(NEW, False), usage.allowance(NEW, True)), (500, 900, 10))
            self.assertEqual(usage.per_day("@other:" + S), 7)
            self.assertEqual(tiers.upgrade_line(NEW), "")    # no upgrade line on a tier without one


class ServerCapsAreNotTheirs(unittest.TestCase):
    def test_a_server_wide_cap_never_says_paid_plans(self):
        agent = SimpleNamespace(usage=trial_limits.Usage(Path(tempfile.mkdtemp()) / "u.json"), tiers=trial_tiers.Tiers())
        server = trial_turn.Turns.cap_words(agent, NEW, trial_limits.SERVER, 0)
        mine = trial_turn.Turns.cap_words(agent, NEW, trial_limits.TOKENS, 0)
        self.assertNotIn(UPGRADE, server)
        self.assertIn(UPGRADE, mine)
        self.assertIn("bring my own", server)


class Wording(unittest.TestCase):
    def test_it_is_called_the_server_agent_everywhere_it_is_shown(self):
        from server import trial_agent
        self.assertEqual((trial_agent.DISPLAY_NAME, trial_agent.CHAT_NAME), ("Server agent (free, limited)", "Server agent"))
        self.assertNotIn("No agent yet", pm_trial.LINE)
        self.assertIn("Server agent (free, limited)", pm_trial.LINE)
        self.assertEqual(pm_trial.USE_RE.match("use glm-5.3 for the server agent").group("name"), "glm-5.3")
        self.assertTrue(pm_trial.USE_RE.match("use glm-5.3 for the server agent").group("trial"))
        self.assertTrue(trial_turn.NAME_RE.search("Server agent, hi") and trial_turn.NAME_RE.search("trial agent: hi"))
        js = (Path(__file__).resolve().parents[1] / "static" / "panel_agents.js").read_text()
        self.assertIn("<h2>Server agent (free, limited)</h2>", js)
        self.assertIn("${esc(t.used)}/${esc(t.per_day)} messages", js)
