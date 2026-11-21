"""Text that goes into a room must not carry local details: error messages from git, gh or the agent
CLIs often include absolute paths (user names, folder layout). Everything posted from an exception
or a tool's output goes through room_safe() first; the full text stays in the local log.
An agent's own reply goes through secret_safe(): it runs with its owner's full CLI (it can read
their files), so anything shaped like a credential, and the bridge's own token, is cut out."""
import re
from pathlib import Path

_PATH_RE = re.compile(r"(?:/(?:Users|home|private|var|tmp|opt|Volumes)/|~/)[^\s'\"`)\]]*")
MAX_ROOM_ERROR = 300
CREDENTIAL_RE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)"
    r"|gh[pousr]_[A-Za-z0-9_]{6,}|github_pat_[A-Za-z0-9_]{20,}|eyJ[\w-]+\.[\w-]+\.[\w-]+|-----BEGIN[^-]*-----"
    r"|\bsk-[A-Za-z0-9_-]{20,}|\bsyt_[A-Za-z0-9_-]{10,}|\bxox[abposr]-[A-Za-z0-9-]{10,}|\bAKIA[0-9A-Z]{16}\b")
REDACTED = "<redacted>"


def room_safe(text: object, limit: int = MAX_ROOM_ERROR) -> str:
    s = str(text).replace(str(Path.home()), "~")
    return _PATH_RE.sub("<local path>", s)[:limit]


def secret_safe(text: object, *secrets: str) -> str:
    """text with the given secrets, and anything shaped like a credential, cut out."""
    out = str(text)
    for secret in secrets:
        out = out.replace(secret, REDACTED) if secret else out
    return CREDENTIAL_RE.sub(REDACTED, out)
