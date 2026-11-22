"""The public site carries the Cloudflare Web Analytics beacon exactly once
per page, placed just before </body>, and loads no other off-site script."""
import json
import pathlib
import re
import unittest
from html.parser import HTMLParser
from urllib.parse import urlsplit

SITE = pathlib.Path(__file__).resolve().parent.parent / "site"
BEACON_SRC = "https://static.cloudflareinsights.com/beacon.min.js"
BEACON_TOKEN = "1845a7ab79014d08b7f31222d14b0735"
# Elements whose content never runs as script in a browser.
INERT = {"template", "textarea", "title", "noscript", "xmp", "iframe",
         "noembed", "noframes", "style", "plaintext"}
BEACON = ("<!-- Cloudflare Web Analytics --><script type='module' "
          f"src='{BEACON_SRC}' "
          f"data-cf-beacon='{{\"token\": \"{BEACON_TOKEN}\"}}'>"
          "</script><!-- End Cloudflare Web Analytics -->")


class ScriptCollector(HTMLParser):
    """Collects attributes of every live <script> tag. Comments are skipped
    by the parser and scripts inside INERT elements never run, so neither counts.
    For a duplicate attribute the first wins, as in browsers."""

    def __init__(self):
        super().__init__()
        self.scripts = []
        self.inert_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in INERT:
            self.inert_depth += 1
        elif tag == "script" and not self.inert_depth:
            first = {}
            for name, value in attrs:
                first.setdefault(name, value)
            self.scripts.append(first)

    def handle_endtag(self, tag):
        if tag in INERT and self.inert_depth:
            self.inert_depth -= 1


def scripts_in(html):
    parser = ScriptCollector()
    parser.feed(html)
    return parser.scripts


class SiteAnalyticsTest(unittest.TestCase):
    def pages(self):
        found = sorted(SITE.rglob("*.html"))
        self.assertTrue(found, "no published HTML pages found")
        return found

    def test_beacon_once_before_body_end(self):
        for page in self.pages():
            html = page.read_text(encoding="utf-8")
            self.assertEqual(html.count(BEACON), 1, page.name)
            self.assertRegex(html, re.escape(BEACON) + r"\s*</body>", page.name)
            beacons = [s for s in scripts_in(html) if s.get("src") == BEACON_SRC]
            self.assertEqual(len(beacons), 1, page.name)
            self.assertEqual(beacons[0].get("type"), "module", page.name)
            token = json.loads(beacons[0]["data-cf-beacon"])["token"]
            self.assertEqual(token, BEACON_TOKEN, page.name)

    def test_no_other_external_script(self):
        for page in self.pages():
            for attrs in scripts_in(page.read_text(encoding="utf-8")):
                src = (attrs.get("src") or "").strip()
                parts = urlsplit(src)
                if parts.scheme or parts.netloc:
                    self.assertEqual(src, BEACON_SRC, page.name)

    def test_parser_rejects_bad_markup(self):
        off_site = '<SCRIPT SRC = https://evil.example/x.js></SCRIPT>'
        srcs = [s.get("src") for s in scripts_in(off_site)]
        self.assertEqual(srcs, ["https://evil.example/x.js"])
        commented = f"<!-- <script type='module' src='{BEACON_SRC}'></script> -->"
        self.assertEqual(scripts_in(commented), [])
        dup = '<script src="https://evil.example/x.js" src="app.js"></script>'
        self.assertEqual(scripts_in(dup)[0]["src"], "https://evil.example/x.js")
        inert = f"<template>{BEACON}"
        self.assertEqual(scripts_in(inert), [])
        as_text = f"<textarea>{BEACON}</body></textarea>"
        self.assertEqual(scripts_in(as_text), [])


if __name__ == "__main__":
    unittest.main()
