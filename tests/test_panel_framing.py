"""Seen live: the Hagency panel was blank in Element Desktop. Its frame came from vector://vector, which
the panel's frame-ancestors (and Caddy's X-Frame-Options SAMEORIGIN) refused. The panel now names the
Element apps that may frame it (bridge_config.panel_embedders) and talks to whichever of them does.
Run: .venv/bin/python -m unittest tests.test_panel_framing"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from common import bridge_config
from server import connect_page
from server import panel
from tests.test_panel_signin import HARNESS

ROOT = Path(__file__).resolve().parent.parent
DESKTOP = "vector://vector"


def frame_ancestors(csp: str) -> list[str]:
    return re.search(r"frame-ancestors ([^;]+)", csp).group(1).split()


class Headers(unittest.TestCase):
    def test_the_panel_may_be_framed_by_our_element_desktop_and_elements_own_apps_only(self):
        allowed = frame_ancestors(panel.HEADERS["Content-Security-Policy"])
        for origin in ("'self'", bridge_config.ELEMENT_ORIGIN, DESKTOP, "https://app.element.io"):
            self.assertIn(origin, allowed)
        self.assertFalse(any("*" in o for o in allowed))
        self.assertNotIn("X-Frame-Options", panel.HEADERS)          # it can't name several origins

    def test_the_list_is_configurable_but_never_a_wildcard(self):
        with mock.patch.dict(os.environ, {"HAGENCY_PANEL_EMBEDDERS": "https://chat.example.org * https://x.example/p vector://vector"}):
            self.assertEqual(bridge_config.panel_embedders(),
                             [bridge_config.ELEMENT_ORIGIN, "https://chat.example.org", DESKTOP])

    def test_a_malformed_element_origin_never_reaches_the_policy(self):
        with mock.patch.object(bridge_config, "ELEMENT_ORIGIN", "https://x.example; script-src *"), \
                mock.patch.dict(os.environ, {"HAGENCY_PANEL_EMBEDDERS": "vector://vector"}):
            self.assertEqual(bridge_config.panel_embedders(), [DESKTOP])

    def test_the_connect_page_is_never_framed_and_element_itself_only_by_itself(self):
        self.assertEqual(frame_ancestors(connect_page.PAGE_CSP), ["'none'"])
        rooms = (ROOT / "deploy" / "Caddyfile").read_text().split("{$ROOMS_DOMAIN} {")[1].split("\n}")[0]
        element = rooms.split("\thandle {")[1].split("\t}")[0]
        self.assertIn("X-Frame-Options SAMEORIGIN", element)        # on Element's own pages
        self.assertEqual(len(re.findall(r"X-Frame-Options (SAMEORIGIN|DENY)", rooms)), 1)   # and nowhere else there


@unittest.skipIf(shutil.which("node") is None, "node not installed")
class SignsInFromElementDesktop(unittest.TestCase):
    def sign_in(self, origin: str) -> str:
        page = panel.PAGE.replace("__EMBEDDERS__", json.dumps(panel.EMBEDDERS))
        script = re.search(r"<script>(.*)</script>", page, re.S).group(1)
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as h:
            h.write(HARNESS)
        routes = {"/auth/openid": {"body": {"session": "s"}},
                  "/console/state": {"body": {"room": "P", "repo": None, "user": "a", "moderator": False, "mode": "off",
                                              "waiting": None, "html": ""}}}
        return subprocess.run([shutil.which("node"), h.name, script, "happy", origin, json.dumps(routes)],
                              capture_output=True, text=True, timeout=30)

    def test_element_desktop_signs_in(self):
        done = self.sign_in(DESKTOP)
        self.assertEqual(done.returncode, 0, done.stderr[-800:])
        self.assertEqual(done.stdout.split("\n")[-1], "")         # signed in: the line is cleared

    def test_a_page_not_on_the_list_is_never_answered(self):
        done = self.sign_in("https://evil.example")                 # the stand-in finds no sign-in request
        self.assertIn("reading 'requestId'", done.stderr)          # to answer: the panel never asked it


class RoomMessage(unittest.TestCase):
    def test_a_new_rooms_first_message_says_how_to_see_the_panel_anywhere(self):
        note = panel.browser_note("!r:x")
        self.assertIn("click Continue or Allow on the panel once", note)
        self.assertIn("/console?room=%21r%3Ax)", note)


if __name__ == "__main__":
    unittest.main()
