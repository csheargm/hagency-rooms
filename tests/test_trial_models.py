"""Unit and flow tests: a visitor picks which allowlisted Ollama Cloud model their trial agent uses
(ADR 0025). Run: .venv/bin/python -m unittest tests.test_trial_models"""
import os
import unittest
from types import SimpleNamespace
from unittest import mock

from server import pm_trial
from server import trial_models
from tests.test_trial_agent import hear
from tests.test_trial_flow import ME, NOW_MS, TrialBase, said_to
from tests.test_welcome import DM, NEW, reaction, text


class Allowlist(unittest.TestCase):
    def test_the_default_list_first_is_the_default(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(trial_models.names(), ["glm-5.3", "kimi-k3.1-pro", "kimi-k2.7-code", "deepseek-v4.1-flash", "gpt-oss:120b"])
            self.assertEqual(trial_models.default(), "glm-5.3")
            self.assertEqual(trial_models.weight("kimi-k3.1-pro"), 2.0)       # the 3T-class model counts double

    def test_the_operators_list_and_bad_entries(self):
        with mock.patch.dict(os.environ, {"TRIAL_MODELS": " Kimi-K2.7-code*1.5, ../etc, gpt-oss:120b ,glm-5.3*x"}):
            self.assertEqual(trial_models.names(), ["kimi-k2.7-code", "gpt-oss:120b"])
            self.assertEqual(trial_models.weight("kimi-k2.7-code"), 1.5)
        with mock.patch.dict(os.environ, {"TRIAL_MODELS": "../x,;"}):
            self.assertEqual(trial_models.default(), "glm-5.3")                # nothing usable: the default list

    def test_only_allowlisted_names_and_a_removed_choice_falls_back(self):
        with mock.patch.dict(os.environ, {"TRIAL_MODELS": "glm-5.3,gpt-oss:120b"}):
            self.assertTrue(trial_models.allowed("GPT-OSS:120b"))
            for bad in ("llama3", "", None, 5, "glm-5.3; rm -rf"):
                self.assertFalse(trial_models.allowed(bad))
            self.assertEqual(trial_models.chosen("kimi-k3.1-pro"), "glm-5.3")


class Picking(TrialBase):
    def start(self):
        self.join_welcome()
        self.run_(self.lp.on_welcome_reaction(DM, reaction(NEW, "🧪")))
        self.dm = SimpleNamespace(room_id="!trialdm", users={ME: None, NEW: None}, invited_users={})
        self.trial_client.rooms["!trialdm"] = self.dm
        self.models = []

        async def model(chat, count=None, model=None):
            self.models.append(model)
            count(100)
            return "ok"
        self.lp.trial.ask_model = model

    def ask_trial(self, body="hi"):
        self.run_(hear(self.lp.trial, self.dm, SimpleNamespace(sender=NEW, body=body, source={"content": {}},
                                                                     server_timestamp=NOW_MS, event_id="$q")))

    def test_use_a_model_in_our_chat_then_its_replies_name_it(self):
        self.start()
        self.assertTrue(self.run_(self.lp.on_welcome_text(DM, text(NEW, "use kimi-k3.1-pro"))))
        self.assertEqual(said_to(self, "!dm")[-1], pm_trial.MODEL_SET.format(name="kimi-k3.1-pro"))
        self.ask_trial()
        self.assertEqual(self.models, ["kimi-k3.1-pro"])
        self.assertTrue(self.trial_posts[-1][1].endswith("— Trial agent · kimi-k3.1-pro"))
        self.assertEqual(self.lp.trial.budget.data["tokens"], 200)                # weighted: it counts double
        self.assertEqual(self.lp.trial_view(NEW)["model"], "kimi-k3.1-pro")      # the panel row shows it

    def test_a_name_off_the_list_is_refused(self):
        self.start()
        self.assertTrue(self.run_(self.lp.on_welcome_text(DM, text(NEW, "@pm use the model llama3-70b"))))
        self.assertIn("isn't one of the trial agent's models", said_to(self, "!dm")[-1])
        self.ask_trial()
        self.assertEqual(self.models, ["glm-5.3"])

    def test_which_models(self):
        self.start()
        self.assertTrue(self.run_(self.lp.on_welcome_text(DM, text(NEW, "which models can I use?"))))
        self.assertIn("gpt-oss:120b", said_to(self, "!dm")[-1])

    def test_the_panel_picker(self):
        self.start()
        self.assertEqual(self.run_(self.lp.trial_action(NEW, "trial_model", "kimi-k2.7-code")), "ok")
        self.assertEqual(self.lp.trial.model_of(NEW), "kimi-k2.7-code")
        self.assertIn("isn't one", self.run_(self.lp.trial_action(NEW, "trial_model", "made-up")))
        self.assertEqual(self.lp.trial_view(NEW)["models"], trial_models.names())

    def test_someone_not_using_the_trial_is_never_answered_about_models(self):
        self.join_welcome()                       # offered, but never started it
        self.assertFalse(self.run_(self.lp.on_trial_text(DM, text(NEW, "use kimi-k3.1-pro"))))
        self.assertEqual(self.run_(self.lp.trial_action(NEW, "trial_model", "kimi-k3.1-pro")), pm_trial.NOT_ENROLLED)

    def test_caps_are_shared_across_models(self):
        self.start()
        self.ask_trial()
        self.lp.trial.set_model(NEW, "gpt-oss:120b")
        self.ask_trial()
        self.ask_trial()                          # TRIAL_PER_VISITOR is 2 here, whichever model
        self.assertEqual(self.models, ["glm-5.3", "gpt-oss:120b"])
        self.assertIn("00:00 UTC", self.trial_posts[-1][1])


class Panel(unittest.TestCase):
    def test_the_trial_card_has_the_model_picker(self):
        from pathlib import Path
        static = Path(__file__).resolve().parent.parent / "static"     # the card: panel_agents.js (My agents tab)
        page = (static / "panel.html").read_text() + (static / "panel_agents.js").read_text()
        self.assertIn('data-a="trial_model"', page)
        self.assertIn('"trialmodel"]', page)          # kept while it's open, not redrawn under the person
        self.assertIn('trialCard(s.trial)', page)


class AskBody(unittest.IsolatedAsyncioTestCase):
    async def test_the_chosen_model_goes_in_the_request(self):
        from server import trial_model
        seen = {}

        class Resp:
            status, headers = 200, {}

            async def json(self, content_type=None):
                return {"message": {"content": "hi"}}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        class Session:
            def post(self, url, json, headers, timeout):
                seen.update(json)
                return Resp()
        await trial_model.ask([], session=Session(), model="gpt-oss:120b")
        self.assertEqual(seen["model"], "gpt-oss:120b")
        await trial_model.ask([], session=Session(), model="not-allowed")
        self.assertEqual(seen["model"], trial_models.default())                  # never an unlisted name


if __name__ == "__main__":
    unittest.main()
