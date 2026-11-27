"""Independent review of the server agent for everyone (ADR 0025): "Server agent, …" is the shared agent's
alone, and @pm and a person's own agent leave it alone; a real person's agent called "server-agent" or
"trial-agent" is never taken for it. Run: .venv/bin/python -m unittest tests.test_server_agent_names"""
import time
import unittest
from types import SimpleNamespace

from adapters.agents import bridge
from common import identity
from server import trial_turn
from server.pm_implicit import Implicit

S = "bridge.localhost"
SHARED = f"@hagency.trial-agent:{S}"


def event(sender, body, mentions=None):
    content = {"body": body, **({"m.mentions": {"user_ids": mentions}} if mentions is not None else {})}
    return SimpleNamespace(sender=sender, body=body, source={"content": content}, event_id="$e",
                           server_timestamp=int(time.time() * 1000))


class TheName(unittest.TestCase):
    def test_its_words_and_exact_id_match(self):
        for body in ("Server agent, what is a rebase?", "server agent: hi", "Trial agent, hi", "  Server agent (free, "
                     "limited): hi", "hagency.trial-agent, hi", "@Server agent (free, limited), hi", "@Trial agent, hi",
                     "hagency.trial-agent: hi", "@hagency.trial-agent:"):
            self.assertTrue(identity.SERVER_AGENT_NAME_RE.search(body), body)

    def test_a_real_persons_server_agent_or_trial_agent_never_matches(self):
        for body in (f"@server-agent:{S} build #4", "server-agent, build #4", "trial-agent: hi", f"@trial-agent:{S} hi",
                     "the server agent is slow", "@hagency.trial-agent-x hi", "observer agent, hi",
                     "@hagency.trial-agent:elsewhere.test hi"):
            self.assertFalse(identity.SERVER_AGENT_NAME_RE.search(body), body)


class WhoItHears(unittest.TestCase):
    def test_pills_decide_and_only_its_exact_id_counts(self):
        mine = trial_turn.mentions
        self.assertFalse(mine(event("@a:" + S, "Server agent, hi", [f"@server-agent:{S}"]), SHARED))   # pilled elsewhere
        self.assertTrue(mine(event("@a:" + S, "Server agent (free, limited): hi", [SHARED]), SHARED))
        self.assertTrue(mine(event("@a:" + S, "Server agent, and you?", ["@betty:" + S]), SHARED))   # a reply to Betty
        self.assertTrue(mine(event("@a:" + S, "Server agent, hi"), SHARED))
        self.assertTrue(mine(event("@a:" + S, f"{SHARED} hi"), SHARED))
        self.assertFalse(mine(event("@a:" + S, f"{SHARED}.evil hi"), SHARED))
        self.assertFalse(mine(event("@a:" + S, "@hagency.trial-agent:elsewhere.test hi"), SHARED))
        # GPT review of ff1e0ff: an explicit address wins over a lookalike pill elsewhere; only local lookalikes veto
        lookalike = [f"@server-agent:{S}"]
        self.assertTrue(mine(event("@a:" + S, f"{SHARED}, can you review this for Server agent?", lookalike), SHARED))
        self.assertTrue(mine(event("@a:" + S, "hagency.trial-agent, hi", lookalike), SHARED))
        self.assertFalse(mine(event("@a:" + S, "Server agent, hi", ["@server-agent:elsewhere.test"]), SHARED))   # any server
        self.assertFalse(mine(event("@a:" + S, f"prefix{SHARED} hi"), SHARED))       # not its id: no boundary before "@"
        for body in ("hagency.trial-agent: hi", f"{SHARED}: hi"):   # a colon as the delimiter
            self.assertTrue(mine(event("@a:" + S, body), SHARED), body)


class PmLeavesItAlone(unittest.TestCase):
    def for_someone_else(self, body):
        pm = SimpleNamespace(user_id=f"@pm:{S}")
        room = SimpleNamespace(users={f"@anna:{S}": None, pm.user_id: None})
        return Implicit.for_someone_else(pm, room, event(f"@anna:{S}", body), body)

    def test_server_agent_comma_is_for_someone_else(self):
        self.assertTrue(self.for_someone_else("Server agent, what does PR 3 change?"))
        self.assertFalse(self.for_someone_else("what does PR 3 change?"))          # still @pm's to consider


class OwnAgentCalledServerAgent(unittest.TestCase):
    def test_the_owner_of_server_agent_still_gets_follow_ups(self):
        """A person "server" (made before the name was reserved) owns @server-agent: its follow-ups still work."""
        b = bridge.Bridge("server", "claude")
        self.assertEqual(b.user_id, f"@server-agent:{S}")
        room = SimpleNamespace(room_id="!r")
        b.open_questions["!r"] = {"event": "$q", "at": time.time(), "left": 5}
        self.assertEqual(b.may_answer(room, event(b.owner_id, "server-agent: 95130")), "$q")
        self.assertIsNone(b.may_answer(room, event(b.owner_id, "Server agent, what is CI?")))   # the shared one's
