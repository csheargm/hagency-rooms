"""Text that goes into a room must not carry local details: error messages from git, gh or the agent
CLIs often include absolute paths (user names, folder layout). Everything posted from an exception
or a tool's output goes through room_safe() first; the full text stays in the local log.
An agent's own reply goes through secret_safe(): it runs with its owner's full CLI (it can read
their files), so anything shaped like a credential, and the agent adapter's own token, is cut out."""
import html
import re
from pathlib import Path

_PATH_RE = re.compile(r"(?:/(?:Users|home|private|var|tmp|opt|Volumes)/|~/)[^\s'\"`)\]]*")
MAX_ROOM_ERROR = 300
CREDENTIAL_RE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)"
    r"|gh[pousr]_[A-Za-z0-9_]{6,}|github_pat_[A-Za-z0-9_]{20,}|eyJ[\w-]+\.[\w-]+\.[\w-]+|-----BEGIN[^-]*-----"
    r"|\bsk-[A-Za-z0-9_-]{20,}|\bsyt_[A-Za-z0-9_-]{10,}|\bxox[abposr]-[A-Za-z0-9-]{10,}|\bAKIA[0-9A-Z]{16}\b")
REDACTED = "<redacted>"
# Markdown's own characters, backslash-escaped (GPT review: entities like "&#91;" became issue links #91)
MARKDOWN_RE = re.compile(r"[\\`*_\[\]]")


def room_safe(text: object, limit: int = MAX_ROOM_ERROR) -> str:
    s = str(text).replace(str(Path.home()), "~")
    return _PATH_RE.sub("<local path>", s)[:limit]


_LOCAL = r"(?:file://\S*|(?:/(?:Users|home|private|var|tmp|opt|Volumes)/|~/)|[A-Za-z]:\\)"
LOCAL_LINK_RE = re.compile(r"\[([^\]]*)\]\(\s*<?" + _LOCAL + r"[^\s]*?\)(?=\s|$|[.,;:!?])")
LOCAL_PATH_RE = re.compile(r"(?<![\w:/.\-])" + _LOCAL + r"[^\s'\"`)\]]*")
PREAMBLE_RE = re.compile(r"\A(?:I(?:'|’)ll|I will|Let me)\b(?:[^\n.!?]|[.!?](?=\S))*[.!?]?[ \t]*\n\s*\n(?=\S)")   # one sentence only


def public_safe(text: object, *secrets: str) -> str:
    """An agent's summary as it goes to GitHub (a public pull request): credentials cut (secret_safe), links to
    files on its computer keep only their text, other local paths (macOS, Linux, Windows, file://) go, and a
    leading "I'll …" paragraph (its plan, not its result) is dropped when more follows. Live test 2026-11-23:
    a PR showed /Users/<name>/… and "I'll add … without running Git". Web links are left alone (GPT review)."""
    s = secret_safe(str(text).strip(), *secrets)
    s = LOCAL_LINK_RE.sub(r"\1", s)
    s = PREAMBLE_RE.sub("", s)
    s = s.replace(str(Path.home()), "~")
    return LOCAL_PATH_RE.sub("(a local file)", s)


def secret_safe(text: object, *secrets: str) -> str:
    """text with the given secrets, and anything shaped like a credential, cut out."""
    out = str(text)
    for secret in secrets:
        out = out.replace(secret, REDACTED) if secret else out
    return CREDENTIAL_RE.sub(REDACTED, out)


def markup_safe(text: object) -> str:
    """Issue titles and reasons come from issue authors and agents: never let them become markup
    (the status board and @pm's messages are rendered as HTML in Element and in the side panel)."""
    return MARKDOWN_RE.sub(r"\\\g<0>", html.escape(" ".join(str(text).split()), quote=False))
