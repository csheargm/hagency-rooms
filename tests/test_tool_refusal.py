"""The ⚠️ line under an agent's reply when a tool was refused: everyday words, the how-to on a docs page."""
import unittest

from adapters.agents import tool_refusal
from tests import ROOT


class ToolRefusalWords(unittest.TestCase):
    def test_the_owners_gmail_case_reads_plainly(self):
        """Owner feedback: the Gmail refusal was full of tool names and a settings path."""
        text = tool_refusal.note(("mcp__claude_ai_Gmail__search_threads", "mcp__claude_ai_Gmail__get_thread"))
        self.assertEqual(text.strip(), "⚠️ I wasn't allowed to use Gmail from this chat: it needs your OK "
                                       "first, and I can't ask for it here. How to allow it: " + tool_refusal.HOW_TO_URL)
        for jargon in ("mcp__", "settings.json", "permissions", "chat turns", "runtime"):
            self.assertNotIn(jargon, text)

    def test_friendly_names(self):
        self.assertEqual(tool_refusal.friendly("WebSearch"), "web search")
        self.assertEqual(tool_refusal.friendly("Bash"), "the command line")
        self.assertEqual(tool_refusal.friendly("mcp__claude_ai_Google_Drive__search"), "Google Drive")
        self.assertEqual(tool_refusal.friendly("mcp__notes__read"), "notes")
        self.assertEqual(tool_refusal.friendly("SomethingNew"), "SomethingNew")

    def test_many_tools_are_cut_short(self):
        text = tool_refusal.note(tuple(f"mcp__s{i}__t" for i in range(9)))
        self.assertIn("s0, s1, s2, s3 or more", text)

    def test_both_kinds_get_one_line_each_and_nothing_gives_nothing(self):
        text = tool_refusal.note(("Bash", "mcp__claude_ai_Gmail__x"))
        self.assertEqual(text.count("⚠️"), 2)
        self.assertEqual(text.count(tool_refusal.HOW_TO_URL), 2)        # both kinds link the guide
        self.assertEqual(tool_refusal.note(()), "")

    def test_the_names_are_logged_for_the_docs_steps(self):
        with self.assertLogs("bridge", "WARNING") as logs:
            tool_refusal.note(("mcp__claude_ai_Gmail__get_thread",))
        self.assertIn("mcp__claude_ai_Gmail__get_thread", logs.output[0])

    def test_the_linked_page_exists_and_names_the_settings(self):
        page = (ROOT / "docs" / "AGENT_TOOLS.md").read_text(encoding="utf-8")
        self.assertTrue(tool_refusal.HOW_TO_URL.endswith("docs/AGENT_TOOLS.md"))
        self.assertIn('"allow"', page)


if __name__ == "__main__":
    unittest.main()
