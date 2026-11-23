"""Unit tests: Element's config and Hagency's home page (element_home.py, hostfiles.py public),
and how Caddy serves them. Run from deploy/: python3 -m unittest test_element_home"""
import json
import os
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path

import element_home
import health
import hostfiles

HERE = Path(__file__).parent
VALUES = {"ROOMS_DOMAIN": "rooms.example.org", "MATRIX_DOMAIN": "matrix.example.org",
          "SERVER_NAME": "matrix.example.org", "ADMIN_DOMAIN": "admin.example.org"}
# What Element Web v1.12.35's sanitiser keeps in an embedded page (shared-utils sanitizeHtmlParams.ts:
# MATRIX_FORMATTING_TAGS and MATRIX_ALLOWED_ATTRIBUTES); anything else is dropped before it renders.
ELEMENT_TAGS = {"font", "del", "s", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "p", "a", "ul", "ol", "sup",
                "sub", "nl", "li", "b", "i", "u", "strong", "em", "strike", "code", "hr", "br", "div", "table",
                "thead", "caption", "tbody", "tr", "th", "td", "pre", "span", "img", "details", "summary"}
ELEMENT_ATTRS = {"font": {"color", "style"}, "span": {"style"}, "a": {"href", "name", "target", "rel"},
                 "img": {"src", "alt", "title", "style"}, "ol": {"start"}}


class Page(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags, self.links, self.dropped, self.imgs = [], [], [], []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        for name, value in attrs:
            if name not in ELEMENT_ATTRS.get(tag, set()):
                self.dropped.append((tag, name))
            if (tag, name) == ("a", "href"):
                self.links.append(value)
            if (tag, name) == ("img", "src"):
                self.imgs.append(value)


def parse(text: str) -> Page:
    page = Page()
    page.feed(text)
    return page


class ElementConfig(unittest.TestCase):
    def test_points_at_this_server_and_shows_our_home_page_and_branding(self):
        config = element_home.element_config(VALUES)
        self.assertEqual(config["default_server_config"]["m.homeserver"],
                         {"base_url": "https://matrix.example.org", "server_name": "matrix.example.org"})
        self.assertEqual(config["brand"], "Hagency Rooms")
        self.assertTrue(config["disable_guests"])
        self.assertEqual(config["embedded_pages"], {"home_url": "https://rooms.example.org/hagency/home.html"})
        self.assertEqual(config["branding"], {"auth_header_logo_url": "https://rooms.example.org/hagency/logo.svg",
                                              "welcome_background_url": "https://rooms.example.org/hagency/background.svg"})
        self.assertEqual(json.loads(element_home.config_text(VALUES)), config)

    def test_a_listed_room_offers_ask_to_join(self):
        """Screen test: Element gates knocking behind a labs feature, so a listed room only offered
        Join (403 "You are not invited"). The written config turns it on for everyone, as a boolean."""
        config = json.loads(element_home.config_text(VALUES))
        self.assertIs(config["features"]["feature_ask_to_join"], True)

    def test_a_newcomer_is_spared_the_introducing_sections_tip(self):
        """Screen test: "Introducing Sections" came on top of Element's other pop-ups right after
        sign-up. It is marked seen by default; nothing touches encryption (no "e2ee" key at all)."""
        config = json.loads(element_home.config_text(VALUES))
        self.assertEqual(config["setting_defaults"], {"releaseAnnouncementData": {"room_list_section": True}})
        self.assertNotIn("e2ee", config)

    def test_every_url_element_loads_is_on_this_server(self):
        """No outside assets: the page, the logo and the background are all ours, under /hagency/."""
        config = element_home.element_config(VALUES)
        urls = [config["embedded_pages"]["home_url"], *config["branding"].values()]
        for url in urls:
            self.assertTrue(url.startswith(f"https://{VALUES['ROOMS_DOMAIN']}{element_home.PREFIX}/"), url)
            self.assertIn(url.rsplit("/", 1)[1], element_home.files(VALUES))      # each one is a file we write


class PanelTrusted(unittest.TestCase):
    """Screen test: in a new room Element asked twice ("Widget added by Product Manager... may share
    data", then "Allow this widget to verify your identity") before the Hagency panel appeared."""
    def test_only_our_exact_panel_url_skips_both_prompts(self):
        rules = element_home.element_config(VALUES)[element_home.WIDGET_LIFECYCLE]["widget_permissions"]
        self.assertEqual(rules, {"https://rooms.example.org/panel?room=$matrix_room_id":
                                 {"preload_approved": True, "identity_approved": True,
                                  "capabilities_approved": ["org.matrix.msc2931.navigate"]}})
        self.assertFalse(any(pattern.endswith("*") for pattern in rules))   # Element: "*" would match by prefix

    def test_element_runs_the_image_that_carries_its_widget_lifecycle_module(self):
        """vectorim/element-web has no /modules: that config would do nothing there."""
        self.assertIn("image: ghcr.io/element-hq/element-web/modules:v1.12.35", (HERE / "docker-compose.yml").read_text())


class HomePage(unittest.TestCase):
    def setUp(self):
        self.text = element_home.home_page(VALUES["SERVER_NAME"])
        self.page = parse(self.text)

    def test_three_steps_in_order_each_one_click(self):
        steps = [self.text.index(s) for s in ("① Get your agent", "② Start or join a project", "Create a project room",
                                              "Find a project to join", "③ Invite people")]
        self.assertEqual(steps, sorted(steps))
        self.assertIn("What is this?", self.text)
        self.assertEqual(self.page.links, ["#/user/@pm:matrix.example.org?action=chat", "/connect/new", "#/new", "#/directory",
                                           "/invite/new", "/privacy"])

    def test_steps_one_and_three_are_buttons_to_pms_sign_in_pages(self):
        """Owner request (ADR 0015): step ① gets the install line and step ③ an invite link right from
        here, no detour through the chat with @pm. They open in a new tab, so Element stays open."""
        page = self.text.split("-->", 1)[1]
        for path, label in (("/connect/new", "① Get your agent"), ("/invite/new", "Make an invite link")):
            anchor = page[page.index(f'<a href="{path}"'):]
            self.assertIn('target="_blank" rel="noopener"', anchor.split(">", 1)[0])
            self.assertIn(label, anchor.split("</a>", 1)[0])
        rooms = (HERE / "Caddyfile").read_text().split("@pm path ")[1].split("\n")[0].split()
        self.assertTrue({"/connect/*", "/invite/*"} <= set(rooms))          # both reach @pm, as /connect/start does

    def test_my_agents_and_settings_comes_first_and_opens_the_chat_with_pm(self):
        """Owner request: a prominent button to My Hagency (the panel in the Product Manager chat)."""
        self.assertLess(self.text.index("⚙ My agents &amp; settings"), self.text.index("① Get your agent"))
        self.assertEqual(self.page.links[0], "#/user/@pm:matrix.example.org?action=chat")

    def test_links_open_inside_element(self):
        """Element keeps #/ links as they are (same tab, its router): #/user/<id>?action=chat opens or
        starts the chat with that user (MatrixChat.viewUser → chatCreateOrReuse), #/new the new-room dialog."""
        for link in self.page.links:
            self.assertTrue(link.startswith("#/") or link in ("/connect/new", "/invite/new", "/privacy"), link)   # or @pm's pages, the policy
        self.assertNotIn(element_home.PM_PLACEHOLDER, self.text)

    def test_survives_elements_sanitiser(self):
        """Element inlines the page through its sanitiser (no iframe): no tag or attribute it would drop."""
        self.assertEqual(set(self.page.tags) - ELEMENT_TAGS, set())
        self.assertEqual(self.page.dropped, [])
        body = self.text.split("-->", 1)[1]                                # the comment is for people (stripped)
        self.assertNotIn("<style", body)
        self.assertNotIn("<script", body)
        self.assertNotIn("_t(", self.text)                                  # Element would treat it as a translation

    def test_follows_elements_theme_and_loads_nothing_from_outside(self):
        self.assertIn("var(--cpd-color-bg-action-primary-rest", self.text)
        self.assertIn("var(--cpd-color-text-on-solid-primary", self.text)
        self.assertEqual(self.page.imgs, [])                                # the owner saw an <img> broken: none
        self.assertNotIn("http", self.text.split("-->", 1)[1])

    def test_the_server_name_is_escaped(self):
        self.assertIn("@pm:a&amp;b", element_home.home_page("a&b"))

    def test_a_template_without_the_placeholder_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "home.html").write_text("<p>no link</p>")
            old, element_home.SOURCE = element_home.SOURCE, Path(d)
            try:
                with self.assertRaises(SystemExit):
                    element_home.home_page("x.org")
            finally:
                element_home.SOURCE = old


class Written(unittest.TestCase):
    def test_public_configs_write_the_page_readable_whatever_the_umask(self):
        with tempfile.TemporaryDirectory() as d:
            old = os.umask(0o077)                                            # bootstrap.sh runs with umask 077
            try:
                hostfiles.public_configs(VALUES, Path(d))
                hostfiles.public_configs(VALUES, Path(d))                    # again: same result
            finally:
                os.umask(old)
            site = Path(d) / element_home.OUT
            self.assertEqual(site.stat().st_mode & 0o777, 0o755)
            self.assertEqual(sorted(p.name for p in site.iterdir()), ["background.svg", "element-fixes.css", "home.html", "logo.svg", "privacy.html"])
            for f in site.iterdir():
                self.assertEqual(f.stat().st_mode & 0o777, 0o644, f.name)
            self.assertIn("#/user/@pm:matrix.example.org", (site / "home.html").read_text())
            config = json.loads((Path(d) / "element-config.json").read_text())
            self.assertIn("embedded_pages", config)

    def test_a_planted_link_instead_of_the_folder_is_refused(self):
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as elsewhere:
            (Path(d) / element_home.OUT).symlink_to(elsewhere)
            with self.assertRaises(SystemExit):
                hostfiles.public_configs(VALUES, Path(d))
            self.assertEqual(list(Path(elsewhere).iterdir()), [])


class Served(unittest.TestCase):
    def test_caddy_serves_the_folder_at_hagency_before_element(self):
        rooms = (HERE / "Caddyfile").read_text().split("{$ROOMS_DOMAIN} {")[1].split("\n}")[0]
        route = rooms.split("@hagency path ")[1].split("\n")[0].split()
        self.assertEqual(sorted(route), sorted(f"{element_home.PREFIX}/{n}" for n in ["home.html", *element_home.STATIC]))
        self.assertIn("handle @hagency {\n\t\turi strip_prefix /hagency\n\t\troot * /srv/element-home", rooms)
        self.assertNotIn("/hagency/*", rooms)                               # never the whole folder
        self.assertLess(rooms.index("handle @hagency"), rooms.index("reverse_proxy element:80"))
        self.assertIn("./element-home:/srv/element-home:ro", (HERE / "docker-compose.yml").read_text())
        self.assertIn("element-home/", (HERE / ".gitignore").read_text())   # generated, never committed

    def test_deploy_checks_the_page_from_outside(self):
        names = [n for n, _ in health.checks("https://r", "https://m", "https://r", "m", "m", public=True)]
        self.assertIn("Element's home page links to @pm", names)
        self.assertIn("Element's home page has the step ① and ③ buttons", names)
        self.assertIn("Get your agent and Make an invite link served (ADR 0015)", names)
        self.assertIn("the privacy policy served at /privacy", names)
        self.assertIn("Element links Hagency's style fixes", names)

    def test_element_page_links_the_style_fixes_caddy_serves(self):
        """Screen test 2026-11-23: Google and GitHub sign-in buttons stacked, the second shifted right."""
        conf = (HERE / "element-nginx.conf").read_text()
        self.assertIn(f'href="{element_home.PREFIX}/{element_home.FIXES_CSS}"', conf)
        self.assertIn("./element-nginx.conf:/etc/nginx/conf.d/hagency.conf:ro", (HERE / "docker-compose.yml").read_text())
        css = element_home.files(VALUES)[element_home.FIXES_CSS]
        self.assertIn(".mx_SSOButtons .mx_SSOButton.mx_SSOButton_mini {\n    width: auto;", css)

    def test_privacy_has_a_short_address_and_is_linked_from_home(self):
        rooms = (HERE / "Caddyfile").read_text().split("{$ROOMS_DOMAIN} {")[1].split("\n}")[0]
        self.assertIn("redir /privacy /hagency/privacy.html 308", rooms)
        self.assertLess(rooms.index("redir /privacy"), rooms.index("reverse_proxy element:80"))
        self.assertIn('<a href="/privacy"', (element_home.SOURCE / "home.html").read_text())

    def test_privacy_page_is_written_out_with_the_rest(self):
        page = element_home.files(VALUES)["privacy.html"]
        self.assertIn("<h1>Privacy</h1>", page)
        self.assertIn("no one has opened it for 90 days", page)   # Synapse purges media unused that long:
        self.assertIn("local_media_lifetime: 90d", (HERE / "configure_synapse.py").read_text())


if __name__ == "__main__":
    unittest.main()
