"""Adjust Synapse's generated homeserver.yaml for the public server (bootstrap.sh runs it every time;
running it again changes nothing):
  - Postgres instead of SQLite (the database block is replaced);
  - sign-up with a registration token (an invite), or open to anyone with a confirmed email once SMTP
    is set in .env (signup.py, ADR 0013); never without either; no guests, no URL previews;
  - sign-in with Google and GitHub, each once its keys are in .env (sso_login.py);
  - every new account joins the private Welcome room, created by @pm, where @pm greets them
    (pm_welcome.py; @pm must exist before the first sign-up, or Synapse can't create the room);
  - the public URL; Synapse's own rate limits, except sign-ins per address (@pm's container signs in often);
  - Oracle's free limits: uploads up to 20 MB, local media kept 90 days and remote media 30 days;
  - @pm is a moderator in every new room (synapse_modules/hagency_pm_moderator.py, room version 12);
  - @pm's private chat with a person here opens joined (synapse_modules/hagency_auto_accept.py).
It refuses a config whose server_name differs from .env (a server's name can never change). The
new file is written next to the old one and swapped in, so an interruption never leaves half a
config; synapse/.hagency-configured marks success, and bootstrap won't start Synapse without it.
Usage: python3 configure_synapse.py ../synapse/homeserver.yaml
"""
import json
import re
import sys
from pathlib import Path

import envfile
import hostfiles
import signup
import sso_login

MARKER = ".hagency-configured"
WELCOME_LOCALPART = "welcome"                 # #welcome:<server>; pm_welcome.py looks for this room
PM_LOCALPART = "pm"


def welcome_settings(server_name: str) -> dict:
    """New accounts are auto-joined to a private Welcome room that @pm creates on first use."""
    return {
        "auto_join_rooms": json.dumps([f"#{WELCOME_LOCALPART}:{server_name}"]),
        "autocreate_auto_join_rooms": "true",
        "auto_join_mxid_localpart": PM_LOCALPART,    # @pm creates it and owns it
        "autocreate_auto_join_room_preset": "private_chat",   # invite-only: Synapse invites each new account
        "auto_join_rooms_for_guests": "false",
    }


def module_settings(server_name: str) -> dict:
    """The modules Synapse loads from /modules: @pm at power 50 in new rooms (it needs room version 12),
    and @pm's private chat with a person here opens already joined."""
    pm = f"@{PM_LOCALPART}:{server_name}"
    modules = [{"module": "hagency_pm_moderator.PmModerator", "config": {"pm": pm}},
               {"module": "hagency_auto_accept.AutoAccept", "config": {"pm": pm, "server": server_name}}]
    return {"modules": json.dumps(modules), "default_room_version": '"12"'}


LEGACY_DIRECTORY_KEY = "restrict_public_rooms_to_local_users"   # replaced by the two allow_public_rooms_* keys


def directory_settings(server_name: str) -> dict:
    """Only @pm lists rooms in the room directory (the Lobby, and rooms their owners set to Listed or
    Open through @pm). Synapse's default lets only server admins publish, so "Listed" failed with
    403 "Not allowed to publish room" (seen live in the screen test); everyone else is still refused."""
    pm = f"@{PM_LOCALPART}:{server_name}"
    # People on other servers (a matrix.org account in Element) browse this directory over federation;
    # Synapse's default refuses (federation publicRooms answered 401, seen live). Only @pm lists rooms,
    # so it holds just the Lobby and Listed/Open project rooms. Without a sign-in: still refused.
    return {"room_list_publication_rules": json.dumps([{"user_id": pm, "action": "allow"}, {"action": "deny"}]),
            "allow_public_rooms_over_federation": "true",
            "allow_public_rooms_without_auth": "false"}


def set_keys(text: str, values: dict) -> str:
    """Set top-level `key: value` lines (replace, or append once): running again changes nothing."""
    for key, value in values.items():
        if re.search(rf"(?m)^{key}:", text):
            text = re.sub(rf"(?m)^{key}:.*$", lambda _m: f"{key}: {value}", text)
        else:
            text += ("" if text.endswith("\n") else "\n") + f"{key}: {value}\n"
    return text


def configure(text: str, e: dict) -> str:
    found = re.search(r'(?m)^server_name:\s*"?([^"\n]+)"?\s*$', text)
    if not found or found.group(1).strip() != e["SERVER_NAME"]:
        raise SystemExit(f"✗ homeserver.yaml is for {found and found.group(1)!r}, .env says {e['SERVER_NAME']!r}; "
                         "a server's name can't change (start over with an empty synapse/ to use a new one)")
    database = ("database:\n  name: psycopg2\n  args:\n    user: synapse\n"
                f"    password: {json.dumps(e['POSTGRES_PASSWORD'])}\n    database: synapse\n    host: postgres\n"
                "    cp_min: 5\n    cp_max: 10\n")
    text, replaced = re.subn(r"(?m)^database:\n(?:[ \t]+.*\n)+", database, text)
    if replaced != 1:
        raise SystemExit("✗ couldn't find the database block; check homeserver.yaml by hand")
    extra = {
        "public_baseurl": json.dumps(f"https://{e['MATRIX_DOMAIN']}/"),
        "enable_registration": "true",
        # An admin hands out tokens (invite-only), unless open sign-up requires a confirmed email instead.
        "registration_requires_token": "false" if signup.is_open(e) else "true",
        "allow_guest_access": "false",
        "max_upload_size": "20M",                    # with media_retention: the disk and traffic stay free-tier
        "media_retention": "{local_media_lifetime: 90d, remote_media_lifetime: 30d}",
        # @pm signs in from one container address (itself, admin-bot for invites, new agents): Synapse's
        # default (5, then one per ~5 min per address) refused it on a deploy; 20, then one per 50 s. Per account and
        # failed-attempt limits stay Synapse's own, so guessing a password is still slow.
        "rc_login": "{address: {per_second: 0.02, burst_count: 20}}",
        "url_preview_enabled": "false",              # previews would fetch arbitrary URLs from the server
        **welcome_settings(e["SERVER_NAME"]),
        **module_settings(e["SERVER_NAME"]),
        **directory_settings(e["SERVER_NAME"]),
    }
    # Synapse refuses to start with this older key next to the two directory keys above (GPT re-check).
    text = re.sub(rf"(?m)^{LEGACY_DIRECTORY_KEY}:.*\n?", "", text)
    # Closing sign-up (or removing a provider's keys) again removes what opened it.
    text = signup.drop_keys(text, signup.stale_keys(e) + sso_login.stale_keys(e))
    return set_keys(set_keys(text, extra), {**signup.settings(e), **sso_login.settings(e)})


def install(path: Path, e: dict) -> bool:
    """Configure in place (atomically, never through a link Synapse's container could plant);
    True if the file changed, so bootstrap restarts Synapse to load it."""
    old = hostfiles.read_regular(path)
    new = configure(old, e)
    if new != old:
        st = path.lstat()
        hostfiles.replace(path, new, 0o600, (st.st_uid, st.st_gid))   # Synapse's container reads it as its own user (991)
    hostfiles.replace(path.parent / MARKER, "ok\n", 0o644)
    return new != old


if __name__ == "__main__":
    values = envfile.read()
    if envfile.validate(values):
        raise SystemExit("✗ run `python3 envfile.py check` first")
    changed = install(Path(sys.argv[1]), values)
    print(f"✓ homeserver.yaml {'configured' if changed else 'unchanged'}: Postgres, Welcome room, @pm moderator, chats open joined")
    print(f"  {signup.describe(values)}; {sso_login.describe(values)}")
