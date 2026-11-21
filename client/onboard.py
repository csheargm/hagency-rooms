"""Bring a person and their own agent into Hagency — one command per person:

  python onboard.py anna --agent hermes      # anna brings her Hermes (two profiles, see below)
  python onboard.py betty --agent claude     # betty brings Claude Code (or: codex)

It creates the Matrix accounts <person> and <person>-agent (generated passwords go to the
git-ignored .env and are never printed), opens a private DM between the person and their agent,
records the choice in agents.json, and for Hermes sets up the room profile "<person>room"
(hermes_profile.py: "<person>room" obeys only the person, with their own Hermes tools; "<person>loop"
obeys only @pm's loop tasks, with no file/terminal/web tools; both are the same agent account).
It also checks that the agent has its own GitHub identity in .gh/<person>-agent/.
Run it again any time: it only fills in what's missing. Then ./start.sh runs everyone's agent.
"""
import argparse
import sys
import urllib.parse

from adapters.agents import agents
from common import github
from adapters.agents import hermes_profile
from common import registry
from common import setup_accounts
from common.setup_accounts import SERVER_NAME, api, load_env, login, mark_direct

NAME_RE = registry.NAME_RE


def ensure_user(env: dict, user: str, shared_secret: str) -> str:
    try:
        password, created = setup_accounts.ensure_user(env, user, shared_secret)
    except setup_accounts.AccountError as e:
        raise SystemExit(str(e)) from e
    if created:
        print(f"  created @{user}:{SERVER_NAME}")
    return password


def ensure_dm(person: str, person_pw: str, agent_pw: str, known: str | None) -> str:
    """A private room for the person and their agent, marked as a DM for both (Element, Hermes)."""
    me, bot = login(person, person_pw), login(f"{person}-agent", agent_pw)
    me_id, bot_id = f"@{person}:{SERVER_NAME}", f"@{person}-agent:{SERVER_NAME}"
    room = known
    if not room:
        room = api("POST", "/_matrix/client/v3/createRoom", {"is_direct": True, "preset": "trusted_private_chat",
                                                            "invite": [bot_id]}, token=me)["room_id"]
    api("POST", f"/_matrix/client/v3/join/{urllib.parse.quote(room)}", {}, token=bot)
    for token, user_id, other in ((me, me_id, bot_id), (bot, bot_id, me_id)):
        mark_direct(token, user_id, other, room)
    return room


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("person", help="the person's Matrix name, e.g. anna")
    ap.add_argument("--agent", required=True, choices=["hermes", *sorted(agents.BACKENDS)])
    ap.add_argument("--loop", action="store_true", help=argparse.SUPPRESS)   # old flag: both profiles are always set up
    args = ap.parse_args()
    if not NAME_RE.match(args.person):
        raise SystemExit("use a short lowercase name, letters and digits (e.g. anna)")
    shared_secret = setup_accounts.shared_secret()
    env = load_env()
    print(f"{args.person}:")
    person_pw = ensure_user(env, args.person, shared_secret)
    agent_pw = ensure_user(env, f"{args.person}-agent", shared_secret)
    people, key = registry.load(), registry.person_id(args.person)
    info = {**people.get(key, {}), "agent": args.agent}
    info["dm"] = ensure_dm(args.person, person_pw, agent_pw, info.get("dm"))
    people[key] = info
    registry.save(people)
    if args.agent == "hermes":
        hermes_profile.configure_both(args.person, owner=args.person, agent=f"{args.person}-agent",
                                      password=agent_pw, home_room=info["dm"])
        personal, loop = hermes_profile.profile_names(args.person)
        print(f"  Hermes profiles ready: '{personal}' (only {args.person}, their own Hermes tools) and "
              f"'{loop}' (only @pm's loop tasks, no file/terminal/web tools)")
    print(f"  agent: {args.agent}; private DM with {args.person}-agent ready")
    if github.identity_dir(f"{args.person}-agent") is None:
        print(f"  ⚠ {args.person}-agent has no GitHub identity yet: see docs/GITHUB_SETUP.md, step 1, "
              f"with the folder .gh/{args.person}-agent", file=sys.stderr)
    print(f"  sign in to Element as {args.person} (password: PW_{args.person.upper()} in .env)")


if __name__ == "__main__":
    main()
