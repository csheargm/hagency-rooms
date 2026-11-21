"""What an owner's own turn adds to their CLI so it can work from a chat room (ADR 0005).

A chat room can't answer an interactive approval, and both CLIs deny what would need one when run
non-interactively (`claude -p`, `codex exec`). So the owner's agent gets, without a prompt, what the
owner decided it should have: web search and fetch, file edits, and commands in its work folder.
The owner's own explicit choices still win:
  - Claude Code: `--permission-mode acceptEdits` (unless their settings' defaultMode is already
    bypassPermissions) and `--allowedTools` for the web, edit and shell tools. Their deny rules
    always win over an allow, so anything they denied stays denied; the reply names it.
  - Codex: `approval_policy="never"` (exec can't ask), and, only where their config.toml doesn't set
    it: `sandbox_mode="workspace-write"`, network on in that sandbox, `web_search="live"`. An
    explicit `sandbox_mode = "read-only"` or `web_search = "disabled"` of theirs is kept.
    With another model provider (the ollama / remote kinds, ADR 0008) there is no OpenAI-hosted web
    search to turn on: `web_search="disabled"` where they set none (the shell still has network).
"""
import json
import os
import tomllib
from pathlib import Path

CLAUDE_OWNER_TOOLS = ("WebSearch", "WebFetch", "Edit", "Write", "Bash")   # Bash runs in the work folder
CLAUDE_OWNER_MODE = "acceptEdits"
CLAUDE_KEEP_MODES = ("bypassPermissions",)                               # already wider: theirs stays


OWN_PERMISSIONS_KEYS = ("profile", "default_permissions", "permissions")   # Codex profiles / permission sets


class ConfigError(RuntimeError):
    pass


def codex_config() -> tuple[Path, dict]:
    """The owner's ~/.codex/config.toml (CODEX_HOME), parsed; {} when there is none."""
    cfg = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "config.toml"
    try:
        return cfg, tomllib.loads(cfg.read_text()) if cfg.exists() else {}
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"can't read {cfg}: {e}") from e


def claude_default_mode() -> str | None:
    home = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
    try:
        perms = json.loads((home / "settings.json").read_text()).get("permissions", {})
    except (OSError, ValueError, AttributeError):
        return None       # no readable user settings: Claude Code itself uses its defaults then
    mode = perms.get("defaultMode") if isinstance(perms, dict) else None
    return mode if isinstance(mode, str) else None


def claude_owner_args() -> list[str]:
    mode = [] if claude_default_mode() in CLAUDE_KEEP_MODES else ["--permission-mode", CLAUDE_OWNER_MODE]
    return [*mode, "--allowedTools", ",".join(CLAUDE_OWNER_TOOLS)]


def codex_owner_args(hosted_web: bool = True) -> list[str]:
    _, cfg = codex_config()
    args = ["-c", 'approval_policy="never"']
    own_profile = any(k in cfg for k in OWN_PERMISSIONS_KEYS)   # theirs decides sandbox/network: never mixed
    if not own_profile and "sandbox_mode" not in cfg:
        args += ["-c", 'sandbox_mode="workspace-write"']
    ws = cfg.get("sandbox_workspace_write")
    if not own_profile and not (isinstance(ws, dict) and "network_access" in ws):
        args += ["-c", "sandbox_workspace_write.network_access=true"]
    if "web_search" not in cfg:
        args += ["-c", 'web_search="live"' if hosted_web else 'web_search="disabled"']
    return args
