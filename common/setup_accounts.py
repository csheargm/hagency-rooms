"""One-time setup: create anna, betty, their agent users and the bots, plus a shared room.

Passwords are generated and written to the git-ignored .env; they are never printed.
Idempotent: existing users/room are reused.
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import time
import sys
import threading
import urllib.error
import urllib.request
import urllib.parse
from pathlib import Path

from common import registry
from common.bridge_config import HOMESERVER as HS, ROOT, SERVER_NAME, password_key, read_env
from common.github import LEVEL_EVENT, PM_BOT, REPO
from common.room_state import content
# each gets a <person>-agent (onboard.py adds people). People who signed up themselves and brought
# their own agent (pm_welcome.py: "remote") have accounts we didn't create and stay out of the Lounge.
PEOPLE = sorted(registry.local_name(p) for p, v in registry.load().items()
                if v.get("agent") != registry.REMOTE and registry.local_name(p))
# Bot → exact power level. @pm is the room's one bot: it writes the room's mode and pins its
# status board, so it is a moderator (state events need 50); people decide, it applies.
BOTS = {"pm": 50}
DISPLAY_NAMES = {"pm": "Product Manager"}        # what people see in Element
USERS = [*PEOPLE, *(f"{p}-agent" for p in PEOPLE), *BOTS]
ROOM_ALIAS = "lounge"
HTTP_TIMEOUT_S = 30                      # never hang on the homeserver
TEST_ROOM_ALIAS = "bridge-test"          # automated tests talk here, never in the Lounge


def api(method, path, body=None, token=None):
    req = urllib.request.Request(HS + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return {"_status": e.code, **json.loads(e.read() or b"{}")}


def load_env():
    return read_env() if (ROOT / ".env").exists() else {}


def save_env(env):
    lines = [f"{k}={v}" for k, v in env.items()]
    path = ROOT / ".env"
    tmp = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")   # @pm writes it too: readers never see half
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)   # never world-readable
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, path)


class AccountError(RuntimeError):
    """The homeserver refused to create or sign in an account."""


def shared_secret() -> str:
    """Synapse's registration secret (never printed): from its config here, or on the public server
    from the file bootstrap.sh copies it to (HAGENCY_SHARED_SECRET_FILE, mode 600; @pm can't read Synapse's config)."""
    secret_file = os.environ.get("HAGENCY_SHARED_SECRET_FILE")
    if secret_file:
        return Path(secret_file).read_text().strip()
    yaml = (ROOT / "data" / "homeserver.yaml").read_text()
    return re.search(r'^registration_shared_secret: "?([^"\n]+)"?', yaml, re.M).group(1)


def ensure_user(env: dict, user: str, secret: str, admin: bool = False, verify: bool = False) -> tuple[str, bool]:
    """(password, created now) for `user`: a generated password saved to .env BEFORE registering, so
    a failed register can be retried (verify: a saved password that can't sign in is registered
    again). Raises AccountError if the homeserver refuses."""
    key = password_key(user)
    if key in env and not verify:
        return env[key], False
    if key in env:
        token = try_login(user, env[key])
        if token:
            api("POST", "/_matrix/client/v3/logout", {}, token=token)
            return env[key], False
    else:
        env[key] = secrets.token_urlsafe(18)
        save_env(env)
    r = register(user, env[key], secret, admin)
    if "_status" in r:
        raise AccountError(f"couldn't create @{user}: {r.get('errcode')} {r.get('error')}")
    return env[key], True


def register(user, password, shared_secret, admin=False):
    nonce = api("GET", "/_synapse/admin/v1/register")["nonce"]
    mac = hmac.new(shared_secret.encode(), digestmod=hashlib.sha1)
    mac.update(b"\x00".join([nonce.encode(), user.encode(), password.encode(), b"admin" if admin else b"notadmin"]))
    return api("POST", "/_synapse/admin/v1/register",
               {"nonce": nonce, "username": user, "password": password, "mac": mac.hexdigest(), "admin": admin})


LOGIN_WAIT_S = 15 * 60                    # Synapse's per-account sign-in limit refills one every few minutes


def try_login(user, password, sleep=None) -> str | None:
    """A session token, or None if the password is wrong. Synapse's "too many sign-ins" (429) is
    waited out, never read as a wrong password: that made @pm try to re-create itself and crash-loop."""
    waited = 0.0
    while True:
        r = api("POST", "/_matrix/client/v3/login",
                {"type": "m.login.password", "identifier": {"type": "m.id.user", "user": user}, "password": password})
        limited = r.get("errcode") == "M_LIMIT_EXCEEDED" or r.get("_status") == 429
        if limited and waited >= LOGIN_WAIT_S:    # never "wrong password": that would re-create the account
            raise AccountError(f"Synapse is still limiting @{user}'s sign-ins; try again later")
        if not limited:
            return r.get("access_token")
        pause = min(max(r.get("retry_after_ms", 5000) / 1000, 1.0), LOGIN_WAIT_S - waited)
        print(f"Synapse limits {user}'s sign-ins: waiting {pause:.0f}s", flush=True)
        (sleep or time.sleep)(pause)
        waited += pause


def login(user, password):
    token = try_login(user, password)
    if not token:
        raise SystemExit(f"login failed for {user}")
    return token


def mark_direct(token: str, user_id: str, other: str, room: str) -> None:
    """Record `room` as user_id's DM with `other` (m.direct), so clients show it as a direct chat."""
    path = f"/_matrix/client/v3/user/{urllib.parse.quote(user_id)}/account_data/m.direct"
    direct = {k: v for k, v in api("GET", path, token=token).items() if not k.startswith("_") and k not in ("errcode", "error")}
    if room not in direct.get(other, []):
        direct[other] = [*direct.get(other, []), room]
        api("PUT", path, direct, token=token)


PW_KEY_PM = password_key(PM_BOT)


def ensure_pm() -> None:
    """The public server: only @pm (people sign up themselves; their agents run on their computers).
    A fresh database gets @pm again; must run before the first sign-up. Usually no sign-in at all:
    each start used three (this check, the name, loop.py) and a few deploys an hour used up
    Synapse's per-account limit, so @pm couldn't sign in (2026-11-21)."""
    env = load_env()
    me = urllib.parse.quote(f"@{PM_BOT}:{SERVER_NAME}")
    profile = api("GET", f"/_matrix/client/v3/profile/{me}")
    if PW_KEY_PM in env and profile.get("displayname") == DISPLAY_NAMES[PM_BOT]:
        print(f"checked @{PM_BOT}:{SERVER_NAME}")     # exists with its name; loop.py's sign-in checks the password
        return
    try:
        password, created = ensure_user(env, PM_BOT, shared_secret(), verify="_status" in profile)   # missing: a fresh database
    except AccountError as e:
        raise SystemExit(str(e)) from e
    token = login(PM_BOT, password)
    try:
        api("PUT", f"/_matrix/client/v3/profile/{urllib.parse.quote(f'@{PM_BOT}:{SERVER_NAME}')}/displayname",
            {"displayname": DISPLAY_NAMES[PM_BOT]}, token=token)
    finally:
        api("POST", "/_matrix/client/v3/logout", {}, token=token)
    print(f"{'created' if created else 'checked'} @{PM_BOT}:{SERVER_NAME}")


def main():
    if sys.argv[1:] == ["--pm-only"]:
        return ensure_pm()
    secret, env = shared_secret(), load_env()
    for user in USERS:
        try:
            if ensure_user(env, user, secret)[1]:
                print(f"created @{user}:{SERVER_NAME}")
        except AccountError as e:
            raise SystemExit(str(e)) from e

    room_id = ensure_room(env, ROOM_ALIAS, "Lounge")
    print(f"room ready: #{ROOM_ALIAS}:{SERVER_NAME} ({room_id}); passwords in .env")


def ensure_room(env: dict, alias_name: str, name: str) -> str:
    """A room with everyone in it (anna creates it; betty, the agents and the bots join)."""
    anna = login("anna", env["PW_ANNA"])
    betty = login("betty", env["PW_BETTY"])
    alias = f"#{alias_name}:{SERVER_NAME}"
    existing = api("GET", f"/_matrix/client/v3/directory/room/{urllib.request.quote(alias)}", token=anna)
    room_id = existing.get("room_id")
    if not room_id:
        room_id = api("POST", "/_matrix/client/v3/createRoom", {
            "name": name, "room_alias_name": alias_name, "preset": "private_chat",
            "invite": [f"@{u}:{SERVER_NAME}" for u in ("betty", *(f"{p}-agent" for p in PEOPLE))],
        }, token=anna)["room_id"]
        print(f"created room {alias}")
    api("POST", f"/_matrix/client/v3/join/{urllib.request.quote(room_id)}", {}, token=betty)
    room = urllib.request.quote(room_id)
    bots = {bot: login(bot, env["PW_" + bot.upper().replace("-", "_")]) for bot in BOTS}
    for bot, power in BOTS.items():
        add_bot(anna, bots[bot], room, bot, power)
    level = api("GET", f"/_matrix/client/v3/rooms/{room}/state/{LEVEL_EVENT}/?format=event", token=anna)
    if level.get("sender") != f"@{PM_BOT}:{SERVER_NAME}":   # written as @pm: only @pm's binding counts
        api("PUT", f"/_matrix/client/v3/rooms/{room}/state/{LEVEL_EVENT}/", content("off", REPO), token=bots[PM_BOT])
    return room_id


def add_bot(admin, bot, room, name, power):
    """The bot joins the room at exactly `power`."""
    bot_id = f"@{name}:{SERVER_NAME}"
    if name in DISPLAY_NAMES:
        api("PUT", f"/_matrix/client/v3/profile/{urllib.request.quote(bot_id)}/displayname",
            {"displayname": DISPLAY_NAMES[name]}, token=bot)
    api("POST", f"/_matrix/client/v3/rooms/{room}/invite", {"user_id": bot_id}, token=admin)
    api("POST", f"/_matrix/client/v3/join/{room}", {}, token=bot)
    pl = api("GET", f"/_matrix/client/v3/rooms/{room}/state/m.room.power_levels/", token=admin)
    users = pl.setdefault("users", {})
    if users.get(bot_id, 0) == power:
        return
    users[bot_id] = power
    r = api("PUT", f"/_matrix/client/v3/rooms/{room}/state/m.room.power_levels/", pl, token=admin)
    if "event_id" not in r:
        raise SystemExit(f"couldn't set {name}'s power level in {room}: {r.get('errcode')}")


if __name__ == "__main__":
    main()
