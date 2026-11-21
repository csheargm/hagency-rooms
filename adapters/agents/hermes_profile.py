"""Hermes profiles for working in Hagency rooms: separate profiles with no personal skills or
memories, that post only final replies. Hermes gives one toolset to every turn, whoever sent it, so
each person's Hermes agent runs as TWO profiles side by side, logged in as the same Matrix account
(the room sees one agent; different device IDs), each obeying only its own sender
(docs/adr/0006-owner-cli-powers.md):
  - "<person>room" (personal): obeys only the owner, with the owner's own Hermes set-up (toolsets,
    approvals, terminal of their default profile; Hermes's Matrix toolset if they set none), its
    terminal in ~/.hagency/work/<owner>/hermes, outside the app;
  - "<person>loop" (loop): obeys only @pm's GitHub-loop tasks (other people's issue text), with no
    file/terminal/web tools: repo work only through the hagency MCP server (contained edits, own
    GitHub identity).
Each accepts invites only from its own sender (docs/hermes-matrix-invite-allowlist.patch) and
ignores everyone else, also in DMs (no pairing offers). Used by onboard.py, supervise.py and
test_hermes_e2e.py.

Only the model's API key is copied from the default profile, read from file to file and never
printed; nothing else personal (other keys, skills, memories) comes along.
"""
import os
import re
import subprocess
from pathlib import Path

from common import github
from adapters.agents import workdirs
from common import bridge_config
from common.bridge_config import ROOT, SERVER_NAME

HERMES_HOME = Path.home() / ".hermes"
PY = str(ROOT / ".venv" / "bin" / "python")
PROVIDER_KEYS = {"ollama-cloud": "OLLAMA_API_KEY", "openrouter": "OPENROUTER_API_KEY", "anthropic": "ANTHROPIC_API_KEY",
                 "openai": "OPENAI_API_KEY", "nous": "NOUS_API_KEY", "gemini": "GEMINI_API_KEY"}
HERMES_MATRIX_TOOLSET = "hermes-matrix"           # Hermes's own default for its Matrix platform
LOOP_TOOLS = """toolsets: [hermes-cli]
platform_toolsets:
  matrix: [todo, memory, skills]      # no file/terminal/web: repo edits go through hagency's contained tools
approvals:
  mode: manual"""
TOOLSET_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
WIDENING_KEYS = ("GATEWAY_ALLOWED_USERS", "GATEWAY_ALLOW_ALL_USERS", "MATRIX_ALLOW_ALL_USERS")   # never kept
MATRIX_KEYS = ("MATRIX_HOMESERVER", "MATRIX_USER_ID", "MATRIX_PASSWORD", "MATRIX_DEVICE_ID", "MATRIX_ALLOWED_USERS",
               "MATRIX_ALLOWED_ROOMS", "MATRIX_HOME_ROOM", "MATRIX_E2EE_MODE", "MATRIX_AUTO_THREAD",
               "MATRIX_SESSION_SCOPE", "MATRIX_REQUIRE_MENTION")
ROOM_RULES = ("\n\nIn Hagency rooms: before answering any question about the project (status, what's done or "
              "planned, what happened, what the app does), call the hagency tool project_status and, when the "
              "question is about the code or content, read_project_file. They show the live state and the whole "
              "room's conversation; your own memory of earlier turns may be out of date. "
              "When @pm gives you a task, ALWAYS call the hagency tool next_task first, even "
              "if the issue looks familiar from earlier in the conversation: each task is new work and the tools, "
              "not your memory, say what to do. Then do exactly what each answer's `next` field says. Edit repo "
              "files only with the hagency read_file/write_files tools. Other people read the room: never post "
              "file paths, folder names, tool calls or raw JSON there; reply with one short line about the outcome. "
              "Before doing anything outside the project that isn't a read (posting elsewhere, writing a shared "
              "document, contacting anyone), call the hagency tool ask_owner and go ahead only if it returns "
              "approved. "
              "When your owner asks you to change your own code (how you or Hagency behave), call the hagency "
              "tool start_code_change and follow its `next` fields; the change goes to their private copy and they "
              "choose with buttons in your private room. If they ask to go back to the official version, call "
              "go_back_to_official.\n")


PERSONAL, LOOP = "room", "loop"                 # profile name = person + suffix
LOOP_ONLY_ENV = "HAGENCY_LOOP_ONLY"              # the loop profile's MCP server: no hagency web tools (mcp_web.py)


def profile_names(person: str) -> tuple[str, str]:
    """(personal, loop) profile names for a person's Hermes agent."""
    return f"{person}{PERSONAL}", f"{person}{LOOP}"


def profile_dir(name: str) -> Path:
    return HERMES_HOME / "profiles" / name


def ensure_profile(name: str, owner: str) -> Path:
    if not profile_dir(name).exists():
        subprocess.run(["hermes", "profile", "create", name, "--no-skills", "--no-alias", "--description",
                        f"{owner}'s agent for Hagency rooms (no personal data)"], check=True, capture_output=True)
    return profile_dir(name)


def default_model() -> tuple[dict, str | None]:
    """The default profile's model settings, and the env var holding its API key."""
    text = (HERMES_HOME / "config.yaml").read_text()
    block = re.search(r"^model:\n((?:  .*\n)+)", text, re.M)
    model = dict(re.findall(r"^  (\w+): (.+)$", block.group(1), re.M)) if block else {}
    return model, PROVIDER_KEYS.get(model.get("provider", ""))


def _block(text: str, key: str, indent: str = "") -> list[str] | None:
    """The lines of `key:` (its own line first, comments cut), down to the next key at the same
    indent; None when the key isn't there at all."""
    out = []
    for ln in text.splitlines():
        if out and ln.strip() and not ln.startswith(indent + " ") and not ln.startswith(indent + "-"):
            break
        if out or re.match(rf"^{indent}{key}:", ln):
            out.append(re.sub(r"\s+#.*$", "", ln))
    return [ln for ln in out if ln.strip() and not ln.lstrip().startswith("#")] or None


def _names(block: list[str] | None) -> list[str] | None:
    """A YAML list of toolset names (`key: [a, b]` or `- a` lines); only plain names come along.
    None: not set. An empty list stays empty (the owner chose no tools)."""
    if block is None:
        return None
    inline = block[0].partition(":")[2].strip()
    raw = inline[1:-1].split(",") if inline.startswith("[") and inline.endswith("]") else []
    raw += [ln.strip().removeprefix("-") for ln in block[1:]]
    return [n for n in (r.strip().strip("'\"") for r in raw) if TOOLSET_RE.match(n)]


def owner_tools(owner: str) -> str:
    """The owner's own toolsets, approvals and terminal settings (their default profile), as YAML for
    the room profile; its terminal works in the owner's work folder, never in the app's install.
    (An inline `terminal: {...}` mapping of theirs is kept whole, with its own cwd.)"""
    cfg = HERMES_HOME / "config.yaml"
    text = cfg.read_text() if cfg.exists() else ""
    toolsets = _names(_block(text, "toolsets"))
    platforms = _block(text, "platform_toolsets")
    inline = re.search(r"\bmatrix:\s*(\[[^\]]*\])", platforms[0]) if platforms else None   # {matrix: [..]}
    matrix = (_names([f"matrix: {inline.group(1)}"]) if inline else
              _names(_block("\n".join(platforms), "matrix", "  ")) if platforms else None)
    term = _block(text, "terminal") or ["terminal:"]
    if term[0].partition(":")[2].strip():         # an inline mapping of theirs: kept whole (its own cwd)
        terminal_yaml = term[:1]
    else:
        terminal_yaml = ["terminal:", *(ln for ln in term[1:] if not re.match(r"^  cwd:", ln)), None]
    cwd = str(workdirs.hermes_dir(owner)).replace("'", "''")      # YAML single-quoted
    return "\n".join([f"toolsets: [{', '.join(['hermes-cli'] if toolsets is None else toolsets)}]",
                      "platform_toolsets:",
                      f"  matrix: [{', '.join([HERMES_MATRIX_TOOLSET] if matrix is None else matrix)}]",
                      *(_block(text, "approvals") or ["approvals:", "  mode: manual"]),
                      *(ln if ln is not None else f"  cwd: '{cwd}'" for ln in terminal_yaml)])


def repo_env() -> dict:
    """The repos this run works on, for the agent's MCP server (only checked owner/name values reach
    the YAML)."""
    shared_ok = {"HAGENCY_CUSTOM_SHARED_OK": "1"} if os.environ.get("HAGENCY_CUSTOM_SHARED_OK") == "1" else {}
    return {"LOOP_REPO": github.REPO,           # (and the operator's "Use it myself" opt-in, customize.py)
            "HAGENCY_REPOS": " ".join(r for r in sorted(github.OPERATOR_REPOS) if github.REPO_RE.match(r)),   # none: any
            **shared_ok}


def set_repos(name: str) -> None:
    """Point the profile's hagency MCP server at this run's repos (Hermes gives MCP servers only the
    env in its config, so LOOP_REPO / HAGENCY_REPOS set for start.sh never reach them otherwise)."""
    cfg = profile_dir(name) / "config.yaml"
    text = cfg.read_text()
    env = repo_env()
    if "HAGENCY_CUSTOM_SHARED_OK" not in env:         # the operator took the opt-in back: drop it here too
        text = re.sub(r"^      HAGENCY_CUSTOM_SHARED_OK: .*\n", "", text, flags=re.M)
    for key, value in env.items():
        if re.search(rf"^      {key}: .*$", text, re.M):
            text = re.sub(rf"^      {key}: .*$", f"      {key}: '{value}'", text, flags=re.M)
        else:
            text = re.sub(r"^(      HAGENCY_AGENT: .*)$", rf"\1\n      {key}: '{value}'", text, count=1, flags=re.M)
    cfg.write_text(text)
    soul = profile_dir(name) / "SOUL.md"             # the room rules too: new tools need their rule
    if soul.exists():
        soul.write_text(soul.read_text().split("\n\nIn Hagency rooms:")[0] + ROOM_RULES)


# The hagency server's args, as onboarding writes them ([...]) or as Hermes re-saves them (a "- " list).
MCP_ARGS = re.compile(r"^    args:(?: \[[^\]\n]*hagency_mcp\.py\]|\n *- [^\n]*hagency_mcp\.py)$", re.M)


def set_code_dir(name: str, code: Path) -> bool:
    """Run the profile's hagency MCP server from `code`: the official folder, or its owner's own
    version (customize.py). HAGENCY_HOME keeps it finding the official folder's state either way.
    False: no hagency args line to point (a hand-edited profile), nothing changed."""
    cfg = profile_dir(name) / "config.yaml"
    text, found = MCP_ARGS.subn(lambda m: f"    args: [{code / 'hagency_mcp.py'}]", cfg.read_text())
    if not found:
        return False
    if not re.search(r"^      HAGENCY_HOME: .*$", text, re.M):
        text = re.sub(r"^(      HAGENCY_AGENT: .*)$", rf"\1\n      HAGENCY_HOME: '{ROOT}'", text, count=1, flags=re.M)
    cfg.write_text(text)
    return True


def profile_model(profile: Path) -> tuple[dict, str | None]:
    """The model this profile already uses (e.g. chosen with `hermes -p <profile> model`), kept on
    re-runs; a new profile starts with the default profile's model."""
    existing = profile / "config.yaml"
    if existing.exists():
        block = re.search(r"^model:\n((?:  .*\n)+)", existing.read_text(), re.M)
        if block:
            model = dict(re.findall(r"^  (\w+): (.+)$", block.group(1), re.M))
            if model.get("provider"):
                return model, PROVIDER_KEYS.get(model["provider"])   # OAuth providers need no key copy
    return default_model()


def write_env(profile: Path, values: dict, key_name: str | None) -> None:
    path = profile / ".env"
    lines = path.read_text().splitlines() if path.exists() else []
    lines = [ln for ln in lines if ln.split("=", 1)[0].strip() not in (*MATRIX_KEYS, *WIDENING_KEYS, key_name)]
    if key_name:
        lines += [ln for ln in (HERMES_HOME / ".env").read_text().splitlines() if ln.startswith(key_name + "=")]
    if not all(one_line(v) for v in values.values()):
        raise ValueError("a Matrix setting has a line break in it")   # would add its own lines to .env
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
    os.fchmod(fd, 0o600)                     # an older .env keeps its mode otherwise (GPT review)
    with os.fdopen(fd, "w") as f:
        f.write("\n".join([*lines, *(f"{k}={v}" for k, v in values.items())]) + "\n")


def one_line(value) -> bool:
    """No line break of any kind (splitlines also splits at \\u2028, \\x1c...): it would add its own
    lines to .env or the YAML (GPT re-check)."""
    text = str(value)
    return text.splitlines() in ([text], [])


def _yaml_env(env: dict) -> str:
    """The MCP server's env block, each value single-quoted for YAML (a quote doubled; no line breaks)."""
    if not all(one_line(v) for v in env.values()):
        raise ValueError("a setting for the hagency MCP server has a line break in it")
    return "\n".join(f"      {k}: '{str(v).replace(chr(39), chr(39) * 2)}'" for k, v in env.items())


def configure(name: str, *, owner: str, agent: str, password: str, home_room: str | None,
              mcp_env: dict | None = None, loop: bool = False, owner_id: str | None = None) -> Path:
    """Write one profile's config.yaml, .env (Matrix + model key) and room rules in SOUL.md: the
    personal one (owner only, the owner's own tools) or, with loop=True, the loop one (@pm only,
    the contained LOOP_TOOLS). configure_both writes the pair a person's agent runs as. On a
    person's own computer (connect.py, ADR 0008) `owner_id` is their full Matrix ID and the
    homeserver comes from the credentials file."""
    profile = ensure_profile(name, owner)
    workdirs.hermes_dir(owner).mkdir(parents=True, exist_ok=True)
    model, key_name = profile_model(profile)
    env = {"HAGENCY_AGENT": agent, **repo_env(), "PATH": os.environ["PATH"], "HOME": os.environ["HOME"],
           **(mcp_env or {}), **({LOOP_ONLY_ENV: "1"} if loop else {})}     # loop: no hagency web tools either
    forget_pairings(profile)
    env_lines = _yaml_env(env)
    model_lines = "\n".join(f"  {k}: {v}" for k, v in model.items())
    (profile / "config.yaml").write_text(f"""model:
{model_lines}
{LOOP_TOOLS if loop else owner_tools(owner)}
display:                              # a shared room sees final replies only: no tool calls, paths or drafts
  tool_progress: "off"
  interim_assistant_messages: false
  memory_notifications: "off"         # Hermes still learns; it just doesn't announce it in the room
matrix:
  require_mention: true
  gateway_restart_notification: false # restarts (e.g. after a code update) aren't announced in every room
  reply_to_mode: "off"                # answers don't quote the question; threads are kept (docs/hermes-matrix-reply-mode.patch)
unauthorized_dm_behavior: ignore      # anyone not in MATRIX_ALLOWED_USERS: silence (no pairing codes)
onboarding:                           # no "shall I build your profile?" offers in a shared room
  profile_build: "off"
mcp_servers:
  hagency:
    command: {PY}
    args: [{ROOT / 'hagency_mcp.py'}]
    timeout: 300
    env:
{env_lines}
""")
    homeserver = bridge_config.CREDENTIALS.get("homeserver") or "http://127.0.0.1:8648"
    values = {"MATRIX_HOMESERVER": homeserver, "MATRIX_USER_ID": f"@{agent}:{SERVER_NAME}",
              "MATRIX_PASSWORD": password, "MATRIX_DEVICE_ID": f"HERMES_{name.upper()}",
              "MATRIX_ALLOWED_USERS": f"@pm:{SERVER_NAME}" if loop else owner_id or f"@{owner}:{SERVER_NAME}",   # one each
              "MATRIX_E2EE_MODE": "off", "MATRIX_AUTO_THREAD": "false", "MATRIX_SESSION_SCOPE": "room",
              "MATRIX_REQUIRE_MENTION": "true", **({"MATRIX_HOME_ROOM": home_room} if home_room and not loop else {})}
    write_env(profile, values, key_name)
    soul = profile / "SOUL.md"
    soul.write_text(soul.read_text().split("\n\nIn Hagency rooms:")[0] + ROOM_RULES)
    return profile


def forget_pairings(profile: Path) -> None:
    """Hermes also lets in users paired earlier: none are kept, so the allowlist alone decides."""
    for folder in (profile / "platforms" / "pairing", profile / "pairing"):
        for name in ("matrix-approved.json", "matrix-pending.json"):
            (folder / name).unlink(missing_ok=True)


def configure_both(person: str, *, owner: str, agent: str, password: str, home_room: str | None,
                   mcp_env: dict | None = None, owner_id: str | None = None) -> tuple[Path, Path]:
    """The two profiles a person's Hermes agent runs as: (personal, loop)."""
    personal, loop = profile_names(person)
    same = {"owner": owner, "agent": agent, "password": password, "mcp_env": mcp_env, "owner_id": owner_id}
    return configure(personal, home_room=home_room, **same), configure(loop, home_room=None, loop=True, **same)
