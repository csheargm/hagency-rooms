"""Who a person is: their full Matrix ID (@anna:this.server, @alice:matrix.org), wherever ownership
is decided. A bare name ("anna") means a person on this server: agents.json's old keys, onboard.py.

Every agent account lives on THIS server. Its localpart comes from its person's full ID:
  - a person here whose name is plain lowercase letters and digits: <name>-agent (anna-agent);
  - anyone else (another server, or an unusual name here): a readable prefix plus a hash of the full
    ID, e.g. @alice:matrix.org → alice-at-matrix-org-3f2a9c01d4-agent. The plain names have no
    "-", so the two kinds never meet; the hash keeps @alice:matrix.org and @alice:matrix-org apart.
A person's further agents add the short label they chose before "-agent": cx-codex-agent,
alice-at-matrix-org-3f2a9c01d4-laptop-agent. Labels have no "-" and at most 9 characters, so a
label is never mistaken for a plain name or for the 10-character hash (ADR 0005).
Pure functions (no project imports): bridge_config.py uses them on people's own computers too.
"""
import hashlib
import re

NAME_RE = re.compile(r"^[a-z][a-z0-9]{1,23}$")   # a person here whose agent is simply <name>-agent
AGENT_SUFFIX = "-agent"
USER_RE = re.compile(r"^@[^:\s]{1,255}:[A-Za-z0-9.-]{1,255}(:\d{1,5})?$")   # a full Matrix user ID
PREFIX_LOCAL, PREFIX_SERVER, HASH_CHARS = 20, 30, 10
LABEL_MAX = 9
LABEL_RE = re.compile(r"^[a-z][a-z0-9]{0,8}$")   # a further agent's label (LABEL_MAX): never as long as the hash
# A message addressed to the shared server agent by name ("Server agent, …"; "Trial agent", its old name; or its
# exact id): it answers it (server/trial_turn.py), a person's own agent and @pm leave it alone (bridge_followup.py,
# pm_implicit.py). Words only, with a space: "@server-agent" / "trial-agent" are a real person's agent (independent
# review), and sign-up keeps "server" and "trial" as usernames anyway (deploy/sso_login.RESERVED_PEOPLE).
# Its localpart alone ("hagency.trial-agent, …"; never "…:another.server"): the full id is matched where it is known.
LOOKALIKE_AGENTS = ("server-agent", "trial-agent")   # real agents of @server / @trial (made before they were reserved)
ID_END = r"(?![\w.-])(?!:\S)"   # an id ends here: "…-agent: hi" and "…-agent:server: hi" yes, "…-agent:other.server" no
SERVER_AGENT_NAME_RE = re.compile(rf"^[^\w@]*@?(?:(?P<id>hagency\.trial-agent){ID_END}|(?:server|trial) agent\b)", re.I)


def person_id(who: str, home: str) -> str:
    """`who` as a full Matrix ID; a bare name is a person on `home` (this server)."""
    return who if who.startswith("@") else f"@{who}:{home}"


def split(user_id: str) -> tuple[str, str]:
    local, _, server = user_id.lstrip("@").partition(":")
    return local, server


def is_local(user_id: str, home: str) -> bool:
    return split(user_id)[1] == home


def shown_name(room, user_id: str) -> str:
    """How a person is called in what we write: their display name exactly as they set it in the room,
    else their localpart unchanged ("cx" stays "cx", never "Cx"). One line, at most 80 characters."""
    user = (getattr(room, "users", None) or {}).get(user_id)
    name = getattr(user, "display_name", None)
    flat = " ".join(name.split())[:80] if isinstance(name, str) else ""
    return flat or split(user_id)[0]


def plain(text: str) -> str:
    """A name someone chose (a room's, a person's), shown as text in markdown messages: no links,
    emphasis or line breaks that could pass for our own instructions (GPT review)."""
    flat = " ".join(str(text).split())[:80]
    return re.sub(r"([\\`*_{}\[\]()<>+!|~-])", r"\\\1", flat)   # "#": github.linkify links issues


def _clean(text: str, limit: int) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:limit].strip("-") or "x"


def agent_localpart(person: str, home: str, label: str | None = None) -> str:
    """The localpart of `person`'s agent account on `home` (deterministic, see the module docstring);
    with a label, the localpart of that further agent of theirs."""
    if label:
        if not LABEL_RE.fullmatch(label):
            raise ValueError(f"not an agent label: {label!r}")
        return f"{handle(person, home)}-{label}{AGENT_SUFFIX}"
    full = person_id(person, home)
    local, server = split(full)
    if server == home and NAME_RE.match(local):
        return f"{local}{AGENT_SUFFIX}"
    tag = hashlib.sha256(full.encode()).hexdigest()[:HASH_CHARS]
    where = f"-at-{_clean(server, PREFIX_SERVER)}" if server != home else ""
    return f"{_clean(local, PREFIX_LOCAL)}{where}-{tag}{AGENT_SUFFIX}"


def handle(person: str, home: str) -> str:
    """A safe file and folder name for `person` (their agent's localpart without -agent)."""
    return agent_localpart(person, home)[:-len(AGENT_SUFFIX)]

