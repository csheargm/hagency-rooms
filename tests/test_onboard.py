"""Unit tests: onboarding pieces. Run: .venv/bin/python -m unittest tests.test_onboard"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from adapters.agents import hermes_profile
from common import registry
from client import supervise
from adapters.agents import workdirs


class Supervisor(unittest.TestCase):
    def kids(self, people, default="codex", profiles=("annaroom", "annaloop")):
        home = Path(tempfile.mkdtemp())
        for name in profiles:
            (home / "profiles" / name).mkdir(parents=True)
            (home / "profiles" / name / "config.yaml").write_text("model: {}\n")
        with mock.patch.object(registry, "load", return_value=people), \
                mock.patch.object(hermes_profile, "HERMES_HOME", home), \
                mock.patch.object(hermes_profile, "set_repos") as set_repo:     # never touch real profiles
            kids = {k.name: k for k in supervise.children(default, False)}
        self.set_repo_calls = [c.args for c in set_repo.call_args_list]
        return kids

    def test_each_person_gets_the_agent_they_brought(self):
        kids = self.kids({"anna": {"agent": "hermes"}, "betty": {"agent": "claude"}, "carl": {}})
        self.assertEqual(kids["anna-agent"].argv, ["hermes", "-p", "annaroom", "gateway", "run"])
        self.assertEqual(kids["betty-agent"].argv[-3:], ["betty", "--agent", "claude"])
        self.assertEqual(kids["carl-agent"].argv[-2:], ["--agent", "codex"])      # not onboarded: start.sh's choice
        self.assertIn("pm", kids)
        self.assertEqual(self.set_repo_calls, [("annaroom",), ("annaloop",)])   # this run's repos, both

    def test_a_hermes_agent_runs_as_its_personal_and_its_loop_profile(self):
        kids = self.kids({"anna": {"agent": "hermes"}})
        self.assertEqual(kids["anna-loop"].argv, ["hermes", "-p", "annaloop", "gateway", "run"])
        self.assertEqual((kids["anna-agent"].profile, kids["anna-loop"].profile), ("annaroom", "annaloop"))
        self.assertEqual(kids["anna-loop"].owner, "anna")          # its hagency tools follow anna's code choice
        self.assertIn("anna-buttons", kids)

    def test_a_profile_not_set_up_yet_is_not_started(self):
        kids = self.kids({"anna": {"agent": "hermes"}}, profiles=("annaroom",))   # onboarded before the split
        self.assertIn("anna-agent", kids)
        self.assertNotIn("anna-loop", kids)

    def test_registry_defaults_to_the_two_demo_people(self):
        with mock.patch.object(registry, "PATH", Path(tempfile.mkdtemp()) / "missing.json"):
            self.assertEqual(sorted(registry.load()), ["@anna:bridge.localhost", "@betty:bridge.localhost"])


class HermesProfile(unittest.TestCase):
    def setUp(self):
        home = Path(tempfile.mkdtemp())
        (home / "profiles" / "annaroom").mkdir(parents=True)
        for name in ("annaroom", "annaloop"):
            (home / "profiles" / name).mkdir(parents=True, exist_ok=True)
            (home / "profiles" / name / "SOUL.md").write_text("You are helpful.")
        (home / "config.yaml").write_text("model:\n  default: gpt-oss:120b\n  provider: ollama-cloud\n"
                                          "  base_url: https://ollama.com/v1\nproviders: {}\n")
        (home / ".env").write_text("OLLAMA_API_KEY=model-key\nGITHUB_TOKEN=personal\nTELEGRAM_BOT_TOKEN=personal\n")
        for p in (mock.patch.object(hermes_profile, "HERMES_HOME", home),
                  mock.patch.object(workdirs, "WORK_HOME", Path(tempfile.mkdtemp()))):
            p.start()
            self.addCleanup(p.stop)
        self.profile = hermes_profile.configure("annaroom", owner="anna", agent="anna-agent", password="pw",
                                                home_room="!dm:x")

    def test_only_the_model_key_comes_along(self):
        env = (self.profile / ".env").read_text()
        self.assertIn("OLLAMA_API_KEY=model-key", env)
        self.assertNotIn("personal", env)
        self.assertEqual((self.profile / ".env").stat().st_mode & 0o777, 0o600)

    def test_obeys_only_its_owner(self):
        env = (self.profile / ".env").read_text()
        self.assertIn("MATRIX_ALLOWED_USERS=@anna:bridge.localhost\n", env)   # @pm's tasks can't steer its tools
        self.assertNotIn("MATRIX_ALLOWED_ROOMS", env)                        # the owner can DM it anywhere

    def test_no_leftover_setting_lets_anyone_else_in(self):
        """GPT review 2: an old GATEWAY_ALLOWED_USERS / *_ALLOW_ALL_USERS line would widen the owner profile."""
        env = self.profile / ".env"
        env.write_text(env.read_text() + "GATEWAY_ALLOWED_USERS=@mallory:x\nMATRIX_ALLOW_ALL_USERS=true\n")
        hermes_profile.configure("annaroom", owner="anna", agent="anna-agent", password="pw", home_room=None)
        self.assertNotIn("mallory", env.read_text())
        self.assertNotIn("ALLOW_ALL", env.read_text())

    def both(self):
        personal, loop = hermes_profile.configure_both("anna", owner="anna", agent="anna-agent", password="pw",
                                                       home_room="!dm:x")
        read = lambda d: dict(ln.split("=", 1) for ln in (d / ".env").read_text().splitlines() if "=" in ln)  # noqa: E731
        return read(personal), read(loop), (personal / "config.yaml").read_text(), (loop / "config.yaml").read_text()

    def test_two_profiles_one_account_one_sender_each(self):
        """Owner decision: Hermes people are in the GitHub loop AND have a full-power personal agent."""
        penv, lenv, _, _ = self.both()
        self.assertEqual(penv["MATRIX_USER_ID"], lenv["MATRIX_USER_ID"])            # the room sees one agent
        self.assertNotEqual(penv["MATRIX_DEVICE_ID"], lenv["MATRIX_DEVICE_ID"])
        self.assertEqual(penv["MATRIX_ALLOWED_USERS"], "@anna:bridge.localhost")
        self.assertEqual(lenv["MATRIX_ALLOWED_USERS"], "@pm:bridge.localhost")
        self.assertEqual(penv.get("MATRIX_HOME_ROOM"), "!dm:x")
        self.assertNotIn("MATRIX_HOME_ROOM", lenv)                                  # its DM is the owner's

    def test_no_message_is_handled_twice_or_by_the_wrong_profile(self):
        """Hermes acts only on senders in MATRIX_ALLOWED_USERS (others: silence, also for invites:
        docs/hermes-matrix-invite-allowlist.patch), so exactly one profile handles each sender."""
        penv, lenv, pcfg, lcfg = self.both()
        handlers = {who: [n for n, env in (("personal", penv), ("loop", lenv))
                          if who in env["MATRIX_ALLOWED_USERS"].split(",")]
                    for who in ("@anna:bridge.localhost", "@pm:bridge.localhost", "@betty:bridge.localhost")}
        self.assertEqual(handlers, {"@anna:bridge.localhost": ["personal"], "@pm:bridge.localhost": ["loop"],
                                    "@betty:bridge.localhost": []})
        for cfg in (pcfg, lcfg):
            self.assertIn("unauthorized_dm_behavior: ignore", cfg)                # no pairing replies either

    def test_earlier_pairings_never_let_anyone_in(self):
        """GPT review 3: Hermes also admits users paired before; the allowlist alone must decide."""
        approved = hermes_profile.profile_dir("annaroom") / "platforms" / "pairing" / "matrix-approved.json"
        approved.parent.mkdir(parents=True)
        approved.write_text('{"@pm:bridge.localhost": {}}')
        self.both()
        self.assertFalse(approved.exists())

    def test_the_loop_profiles_hagency_tools_have_no_web(self):
        """GPT review 3: the loop profile's MCP server must not run web_search/fetch_page either."""
        import asyncio
        import os
        os.environ.setdefault("HAGENCY_AGENT", "anna-agent")     # mcp_core needs it at import
        from mcp_tools import mcp_web
        _, _, pcfg, lcfg = self.both()
        self.assertIn(f"{hermes_profile.LOOP_ONLY_ENV}: '1'", lcfg)
        self.assertNotIn(hermes_profile.LOOP_ONLY_ENV, pcfg)
        with mock.patch.dict("os.environ", {hermes_profile.LOOP_ONLY_ENV: "1"}):
            self.assertIn("no web access", asyncio.run(mcp_web.web_search("x"))["error"])

    def test_the_loop_profile_keeps_contained_tools_and_the_personal_one_the_owners(self):
        """GPT review: Hermes gives one toolset to every turn, so @pm's tasks (other people's issue
        text) must never get the owner's terminal/file/web tools."""
        _, _, pcfg, lcfg = self.both()
        self.assertIn("matrix: [todo, memory, skills]", lcfg)
        self.assertNotIn("matrix: [hermes-matrix", lcfg)
        self.assertNotIn("terminal:", lcfg)
        self.assertIn("matrix: [hermes-matrix]", pcfg)
        self.assertIn("hagency_mcp.py", lcfg)                                      # its loop tools

    def test_hermes_own_tools_and_a_quiet_room(self):
        """ADR 0006: the owner's Hermes gets its normal Matrix toolset (no todo/memory/skills-only list)."""
        cfg = (self.profile / "config.yaml").read_text()
        self.assertIn("matrix: [hermes-matrix]", cfg)
        self.assertNotIn("matrix: [todo, memory, skills]", cfg)
        self.assertIn('tool_progress: "off"', cfg)
        self.assertIn('memory_notifications: "off"', cfg)
        self.assertIn("gateway_restart_notification: false", cfg)   # seen live: "Gateway shutting down" ×5 per update
        self.assertIn('reply_to_mode: "off"', cfg)   # the owner: an answer shouldn't quote the question
        self.assertIn("provider: ollama-cloud", cfg)

    def test_a_model_chosen_for_the_profile_is_kept(self):
        cfg = self.profile / "config.yaml"
        cfg.write_text(cfg.read_text().replace("provider: ollama-cloud", "provider: openai-codex"))
        hermes_profile.configure("annaroom", owner="anna", agent="anna-agent", password="pw", home_room=None)
        self.assertIn("provider: openai-codex", cfg.read_text())

    def test_set_repos_points_the_mcp_server_at_this_runs_repos(self):
        """Seen live: Hermes's MCP server didn't get LOOP_REPO and opened issues in the default repo.
        Now also the allowed repos (HAGENCY_REPOS), and only checked owner/name values reach the YAML."""
        from common import github
        cfg = self.profile / "config.yaml"
        for allowed in ({"me/project-a"}, {"me/project-b", "me/c", "x'\n  evil: 1"}):
            with mock.patch.object(github, "REPO", sorted(allowed)[0]), \
                    mock.patch.object(github, "OPERATOR_REPOS", frozenset(allowed)):
                hermes_profile.set_repos("annaroom")
        text = cfg.read_text()
        self.assertEqual((text.count("LOOP_REPO:"), text.count("HAGENCY_REPOS:")), (1, 1))
        self.assertIn("HAGENCY_REPOS: 'me/c me/project-b'", text)
        self.assertNotIn("evil", text)

    def test_room_rules_are_written_once(self):
        hermes_profile.configure("annaroom", owner="anna", agent="anna-agent", password="pw", home_room=None)
        self.assertEqual((self.profile / "SOUL.md").read_text().count("In Hagency rooms:"), 1)


if __name__ == "__main__":
    unittest.main()
