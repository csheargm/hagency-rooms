"""Run a person's own agent CLI (Codex or Claude Code) for one room turn. The ollama and remote
kinds are Codex with another model provider (codex_provider.py, docs/adr/0008-agent-kinds.md).

Each room gets its own working folder (workdirs.py) and a saved session id so the CLI resumes
the same conversation next time. The prompt goes in on stdin (never as argv, so chat text can't be
parsed as a CLI flag) and nothing runs through a shell.

Two kinds of turn (docs/adr/0006-owner-cli-powers.md):
  - owner=True: the owner's own message. The CLI runs as the owner configured it on this machine
    (their settings, MCP servers, deny rules); the agent adapter adds no lockdown, only what a chat room
    needs to work without interactive approvals: web, edits and commands in the work folder
    (owner_cli.py). Whatever is still refused, the reply says plainly which setting blocked it.
  - owner=False (default): turns not driven by the owner's own words, on untrusted text (loop
    builds/reviews/plans, @pm's brain, a member's question under --allow-members): read-only, no
    approvals, no connectors/MCP/web; only builds (write=True) may edit, inside the task's own clone
    (Codex workspace-write sandbox, no network; Claude Edit/Write tools only).
"""
import asyncio
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from common import bridge_config
from adapters.agents import cli_path
from adapters.agents import codex_provider
from common.redact import secret_safe
from common import lifecycle
from adapters.agents import owner_cli
from adapters.agents import tool_refusal
from adapters.agents.workdirs import SESSION_FILE, SESSION_ID_RE

TURN_TIMEOUT_S = 600
TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


@dataclass
class AgentResult:
    text: str
    session_id: str | None
    denied: tuple[str, ...] = ()       # tools the owner's own permission rules didn't allow this turn


class AgentError(RuntimeError):
    pass


# Locked turns only (owner=False). The sandbox only limits files and shell. Codex can also reach
# the owner's ChatGPT connectors (Gmail, Drive, calendars...), plugins, MCP servers, computer/browser
# use and web search, while reading untrusted text (other people's chat lines, issue bodies). All of
# it is switched off.
CODEX_FEATURES_OFF = ("apps", "plugins", "computer_use", "browser_use", "in_app_browser",
                      "image_generation", "multi_agent")
CODEX_TOOLS_OFF = ("shell_tool", "unified_exec")   # bare turns: no commands at all (checked live, Codex 0.169)
BARE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _codex_lockdown() -> list[str]:
    args = [x for f in CODEX_FEATURES_OFF for x in ("-c", f"features.{f}=false")]
    args += ["-c", 'web_search="disabled"']
    try:
        cfg, config = owner_cli.codex_config()
    except owner_cli.ConfigError as e:
        raise AgentError(f"{e} (needed to switch its MCP servers off)") from e   # fail closed
    servers = config.get("mcp_servers", {})
    for name in servers:
        if not BARE_KEY_RE.match(name):
            raise AgentError(f"can't safely switch off Codex MCP server {name!r}; rename it in {cfg}")
        args += ["-c", f"mcp_servers.{name}.enabled=false"]
    return args


def _codex_argv(workdir: Path, session_id: str | None, write: bool = False, owner: bool = False,
                provider: tuple[str, ...] = ()) -> list[str]:
    """owner: the owner's ~/.codex/config.toml, plus what a room needs to work (owner_cli.py).
    provider: another model provider's options (codex_provider.py); its hosted web search is OpenAI's."""
    sandbox = "workspace-write" if write else "read-only"
    common = ["--json", "--skip-git-repo-check", *provider]
    if owner:
        try:
            common += owner_cli.codex_owner_args(hosted_web=not provider)
        except owner_cli.ConfigError as e:
            raise AgentError(str(e)) from e
        if provider:   # live test 2026-11-23: the owner's plugins, computer/browser use etc. made a 147k-token
            # request that no local model takes (Ollama: "exceeds the available context size"); they are
            # OpenAI-hosted features, of no use with another provider. Off: about 2.6k tokens.
            common += [x for f in CODEX_FEATURES_OFF for x in ("-c", f"features.{f}=false")]
    else:      # and none of the owner's own widening of workspace-write (network, extra roots)
        common += ["-c", f'sandbox_mode="{sandbox}"', "-c", 'approval_policy="never"',
                   "-c", "sandbox_workspace_write.network_access=false",
                   "-c", "sandbox_workspace_write.writable_roots=[]", *_codex_lockdown()]
    if session_id:
        return ["codex", "exec", "resume", *common, session_id, "-"]
    return ["codex", "exec", *common, "-C", str(workdir), "-"]


LOG_LINE_RE = re.compile(r"^\d{4}-\d\d-\d\dT\S+\s+(TRACE|DEBUG|INFO|WARN|ERROR)\s")   # a CLI's own log line
ERROR_LINE = "ERROR: "                                                   # Codex's line for the person


def failure_reason(output: str) -> str:
    """What to tell the person when the CLI failed. Live test 2026-11-23: the chat showed Codex's
    internal log lines ("…Z ERROR codex_models_manager::manager: failed to refresh…") instead of its
    own last error ("request (147017 tokens) exceeds the available context size")."""
    lines = [s.strip() for s in output.splitlines() if s.strip()]
    said = [s[len(ERROR_LINE):] for s in lines if s.startswith(ERROR_LINE)]
    if said:
        return said[-1]
    return " | ".join([s for s in lines if not LOG_LINE_RE.match(s)][-3:] or lines[-3:])


def _parse_codex(stdout: str) -> AgentResult:
    session_id, messages = None, []
    for line in stdout.splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue  # codex may print non-JSON banner lines; they carry no reply text
        if ev.get("type") == "thread.started":
            session_id = ev.get("thread_id")
        item = ev.get("item") or {}
        if ev.get("type") == "item.completed" and item.get("type") == "agent_message":
            messages.append(item.get("text", ""))
    return AgentResult("\n\n".join(m for m in messages if m.strip()), session_id)


def _claude_argv(workdir: Path, session_id: str | None, write: bool = False, owner: bool = False) -> list[str]:
    resume = ["--resume", session_id] if session_id else []
    if owner:     # the owner's settings (all sources, deny rules win) plus what a room needs (owner_cli.py)
        return ["claude", "-p", "--output-format", "json", *owner_cli.claude_owner_args(), *resume]
    # "default" permission mode in -p: tools that need approval are denied, not prompted.
    # --tools limits built-ins to read-only ones even if user settings pre-approve Bash/Edit;
    # --strict-mcp-config (with no --mcp-config) loads no MCP servers.
    tools, mode = ("Read,Grep,Glob,Edit,Write", "acceptEdits") if write else ("Read,Grep,Glob", "default")
    # --setting-sources user: never load .claude/ settings or hooks from the (untrusted) project.
    argv = ["claude", "-p", "--output-format", "json", "--permission-mode", mode,
            "--tools", tools, "--strict-mcp-config", "--setting-sources", "user"]
    return argv + resume


def _parse_claude(stdout: str) -> AgentResult:
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as e:
        raise AgentError(f"claude returned non-JSON output: {stdout[:200]!r}") from e
    if data.get("is_error"):
        raise AgentError(f"claude error: {str(data.get('result'))[:300]}")
    return AgentResult(data.get("result", ""), data.get("session_id"), _denied_tools(data))


def _denied_tools(data: dict) -> tuple[str, ...]:
    """The names (never the inputs: they can hold paths or secrets) of tools Claude Code refused."""
    names = [d.get("tool_name") for d in data.get("permission_denials") or [] if isinstance(d, dict)]
    return tuple(dict.fromkeys(n for n in names if isinstance(n, str) and TOOL_NAME_RE.fullmatch(n)))   # GPT review: "Bash\n" passed match()


def _provider_argv(kind: str):
    """Codex's argv for the ollama / remote kind, with the model saved by connect.py."""
    def argv(workdir: Path, session_id: str | None, write: bool = False, owner: bool = False) -> list[str]:
        try:
            provider = codex_provider.args(kind, bridge_config.CREDENTIALS)
        except codex_provider.ProviderError as e:
            raise AgentError(str(e)) from e
        return _codex_argv(workdir, session_id, write, owner, tuple(provider))
    return argv


BACKENDS = {"codex": (_codex_argv, _parse_codex), "claude": (_claude_argv, _parse_claude),
            "ollama": (_provider_argv("ollama"), _parse_codex), "remote": (_provider_argv("remote"), _parse_codex)}


def _kind_env(backend: str) -> dict:
    try:
        return codex_provider.env(backend, os.environ.get("HAGENCY_CREDENTIALS"), bridge_config.CREDENTIALS)
    except codex_provider.ProviderError as e:
        raise AgentError(str(e)) from e


def agent_env(workdir: Path) -> dict:
    """The CLI's git must not climb out of its folder into an enclosing repo (loop checkouts still
    live in the app's rooms/): a checkout without .git would otherwise show our files as its "changes"."""
    return {**os.environ, "GIT_CEILING_DIRECTORIES": str(workdir.resolve().parent)}


def without_tools(argv: list[str]) -> list[str]:
    """A locked, fresh turn's argv with no tools at all (@pm's understanding step, ADR 0022): Claude Code
    gets an empty --tools; Codex (ollama, remote) also loses its shell (read-only, it could still read files)."""
    if argv[0] == "claude":
        at = argv.index("--tools") + 1
        return [*argv[:at], "", *argv[at + 1:]]
    return [*argv[:2], *(x for f in CODEX_TOOLS_OFF for x in ("-c", f"features.{f}=false")), *argv[2:]]


async def run_turn(backend: str, workdir: Path, prompt: str, write: bool = False,
                   fresh: bool = False, owner: bool = False, bare: bool = False) -> str:
    """Run one turn in workdir, resuming that room's saved session unless fresh=True. owner=True
    only for a turn its owner's own message asked for (see the module docstring). bare=True: a fresh,
    locked turn with no tools at all (without_tools)."""
    if bare and (write or owner or not fresh):
        raise AgentError("a turn without tools is always fresh and locked")
    argv_fn, parse_fn = BACKENDS[backend]
    workdir.mkdir(parents=True, exist_ok=True)
    state_file = workdir / SESSION_FILE
    state = {} if fresh else (json.loads(state_file.read_text()) if state_file.exists() else {})
    state = state if isinstance(state, dict) else {}
    key = backend if owner else f"{backend}-locked"   # a locked turn never shares the owner's conversation
    key += codex_provider.session_tag(backend, bridge_config.CREDENTIALS)   # remote: per service address
    session_id = state.get(key)
    if session_id is not None and not (isinstance(session_id, str) and SESSION_ID_RE.match(session_id)):
        session_id = None   # never pass a malformed id as a CLI positional; start a fresh session

    program, *args = (without_tools if bare else list)(argv_fn(workdir, session_id, write, owner))
    extra = _kind_env(backend)                         # remote: the API key, in the environment only
    proc = await asyncio.create_subprocess_exec(       # Windows: npm's .cmd wrappers too, with no shell
        *cli_path.command(program), *args, cwd=workdir, env={**agent_env(workdir), **extra},
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await lifecycle.communicate(proc, prompt.encode(), TURN_TIMEOUT_S)
    except asyncio.TimeoutError as e:
        raise AgentError(f"{backend} timed out after {TURN_TIMEOUT_S}s") from e
    stdout = out.decode(errors="replace")
    if proc.returncode != 0:
        parse_fn(stdout)  # CLIs that report errors as JSON on stdout (claude) raise a clear AgentError here
        reason = failure_reason(err.decode(errors="replace").strip() or stdout.strip())
        reason = secret_safe(codex_provider.scrub(reason, extra))   # GPT review: an ERROR line may hold a credential
        raise AgentError(f"{backend} exited {proc.returncode}: {reason[:400]}")

    result = parse_fn(stdout)
    note = tool_refusal.note(result.denied) if owner else ""   # task output (VERDICT, plan JSON) stays as is
    if not result.text.strip() and not note:
        raise AgentError(f"{backend} returned an empty reply")
    if not fresh and result.session_id and result.session_id != session_id:
        state[key] = result.session_id
        tmp = state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(state))
        os.replace(tmp, state_file)   # atomic: a crash can't leave a half-written state file
    return codex_provider.scrub((result.text.strip() + note).strip(), extra)   # a service echoing its key
