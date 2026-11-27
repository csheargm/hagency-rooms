"""Element Web's config and Hagency's home page for it (hostfiles.py public writes both on the VM).

Element's own first screen ("Send a Direct Message / Explore Public Rooms / Create a Group Chat")
gives a newcomer no next step. Instead, embedded_pages.home_url shows element_page/home.html:
three numbered steps whose links open things inside Element (the chat with @pm, the new-room
dialog). Caddy serves the folder at /hagency/ (Caddyfile, docker-compose.yml), on Element's own
name, because Element fetches the page and inlines it (no iframe). The login and sign-up pages
get Hagency's logo and background. Everything is served from this server: no outside assets.
"""
import html
import json
from pathlib import Path

from configure_synapse import PM_LOCALPART
from signup import BRAND

SOURCE = Path(__file__).resolve().parent / "element_page"   # the page's template and images, in git
OUT = "element-home"                          # generated next to bootstrap.sh; Caddy mounts it read-only
PREFIX = "/hagency"                           # where Caddy serves OUT's files (Caddyfile: @hagency)
PM_PLACEHOLDER = "__PM_ID__"
FIXES_CSS = "element-fixes.css"             # Element's own style bugs; element-nginx.conf links it
TRANSLATIONS = "translations.json"           # Element's wording, overridden: "Remove this device" → "Sign out"
DOCK_JS = "panel-dock.js"                    # a "Hagency panel" button when the panel is minimised (ADR 0026)
STATIC = ("logo.svg", "background.svg", "privacy.html", FIXES_CSS, TRANSLATIONS, DOCK_JS)   # privacy.html: Google sign-in links it
# Our own panel's widget URL as @pm adds it (panel.widget_url; test_panel checks they agree). Element
# matches it exactly against the widget's template URL: no other widget is pre-approved.
PANEL_TEMPLATE = "/panel?room=$matrix_room_id"
# Element's own widget-lifecycle module (in the element-web/modules image, docker-compose.yml) skips
# the "may share data" and "verify your identity" prompts for that one URL. Screen test: a newcomer
# saw both before the panel appeared in every new room.
WIDGET_LIFECYCLE = "io.element.element-web-modules.widget-lifecycle"
# Element hides "Ask to join" on a knock room behind this labs feature (off by default). The config's
# value wins over each person's own Labs switch (v1.12.35 Settings.tsx: LEVELS_DEVICE_ONLY_SETTINGS_WITH_
# CONFIG_PRIORITISED; ConfigSettingsHandler reads "features", booleans). Screen test: a listed room only
# offered Join, which Synapse refuses with 403 "You are not invited to this room".
FEATURES = {"feature_ask_to_join": True}
# ...and may open the person's chat with @pm in place (the widget API's navigate, MSC2931): its
# "your chat with Product Manager" links. No other capability (nothing in a room is read or sent).
PANEL_APPROVED = {"preload_approved": True, "identity_approved": True,
                  "capabilities_approved": ["org.matrix.msc2931.navigate"]}
# Screen test: right after sign-up a newcomer got Element's "Introducing Sections" tip on top of the
# rest. v1.12.35's ReleaseAnnouncementStore shows each feature not marked seen in the account setting
# "releaseAnnouncementData", which has a config level (Settings.tsx: LEVELS_ACCOUNT_SETTINGS), so
# setting_defaults marks it seen for everyone (ConfigSettingsHandler reads "setting_defaults").
# Not configurable in v1.12.35, so left as they are: "Back up your chats" (DeviceListener: account
# data only; turning recovery off would weaken encryption) and "Don't miss a reply" (RoomView and
# Notifier: the browser's own permission and the push rules, no setting).
SETTING_DEFAULTS = {"releaseAnnouncementData": {"room_list_section": True}}


def pm_id(server_name: str) -> str:
    return f"@{PM_LOCALPART}:{server_name}"


def element_config(values: dict) -> dict:
    """Element points at this homeserver, shows Hagency's home page and branding, trusts our panel, and lets
    people skip the identity check after sign-in (ADR 0018)."""
    rooms = f"https://{values['ROOMS_DOMAIN']}{PREFIX}"
    return {"default_server_config": {"m.homeserver": {"base_url": f"https://{values['MATRIX_DOMAIN']}",
                                                       "server_name": values["SERVER_NAME"]}},
            "disable_custom_urls": False, "disable_guests": True, "brand": BRAND, "features": FEATURES,
            # An X on "Confirm your digital identity" after sign-in (default true: no way past it but a reset).
            # Hagency's rooms aren't encrypted (bots can't read them): ADR 0018.
            "force_verification": False,
            "setting_defaults": SETTING_DEFAULTS,
            "embedded_pages": {"home_url": f"{rooms}/home.html"},
            "custom_translations_url": f"{rooms}/{TRANSLATIONS}",   # owner: no obvious way to sign out
            "branding": {"auth_header_logo_url": f"{rooms}/logo.svg", "welcome_background_url": f"{rooms}/background.svg"},
            WIDGET_LIFECYCLE: {"widget_permissions": {panel_url(values): PANEL_APPROVED}}}


def panel_url(values: dict) -> str:
    """The panel's widget URL as Element sees it before filling in the room."""
    return f"https://{values['ROOMS_DOMAIN']}{PANEL_TEMPLATE}"


def home_page(server_name: str) -> str:
    """home.html with @pm's ID filled in (escaped, though envfile.py only lets a domain through)."""
    template = (SOURCE / "home.html").read_text()
    if PM_PLACEHOLDER not in template:
        raise SystemExit(f"✗ {SOURCE / 'home.html'} has no {PM_PLACEHOLDER}: the 'Get your agent' link would be broken")
    return template.replace(PM_PLACEHOLDER, html.escape(pm_id(server_name), quote=True))


def files(values: dict) -> dict[str, str]:
    """{file name in OUT: its text}: the filled-in page and the images."""
    out = {"home.html": home_page(values["SERVER_NAME"])}
    out.update({name: (SOURCE / name).read_text() for name in STATIC})
    return out


def config_text(values: dict) -> str:
    return json.dumps(element_config(values), indent=2)
