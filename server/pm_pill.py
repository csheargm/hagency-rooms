"""A leading mention pill in a message's plain body: who it names and where the rest begins.

Live test 2026-11-26: "@Product Manager retry #13", typed with Element's mention pill on a phone, got
"I didn't catch that": a pill's plain body is a markdown link or the full ID, not the words. Forms read
here, at the start of the text only: [label](https://matrix.to/#/<id>), https://matrix.to/#/<id>, <id>.
Each pattern is one bounded run of a single character class, so no input backtracks (GPT review).
"""
import re
import urllib.parse

PERMALINK = "https://matrix.to/#/"
MXID_RE = re.compile(r"@[a-z0-9._=/+-]{1,255}:[A-Za-z0-9.-]{1,255}(?::\d{1,5})?", re.I)
MD_LINK_RE = re.compile(r"\s*\[[^\]\n]{0,100}\]\(([^)\s]{1,400})\)")
BARE_LINK_RE = re.compile(r"\s*(https://matrix\.to/#/[^\s]{1,400})")
BARE_ID_RE = re.compile(r"\s*(@[^\s:]{1,255}:[^\s.]{0,255}\.[^\s]{1,300})")   # a dot: "@pm:retry #9" is shorthand
TAIL_RE = re.compile(r"[:,]?(?:\s+|$)")


def user_of(link: str) -> str | None:
    """The one user a matrix.to link names (percent-decoded), or None for anything else."""
    if not link.startswith(PERMALINK):
        return None
    target = urllib.parse.unquote(link[len(PERMALINK):].split("?", 1)[0])
    return target if MXID_RE.fullmatch(target) else None


def leading(text: str) -> tuple[str, int] | None:
    """(user id, where the rest of the text starts) for a pill at the very start, else None."""
    for pattern, from_link in ((MD_LINK_RE, True), (BARE_LINK_RE, True), (BARE_ID_RE, False)):
        m = pattern.match(text)
        if not m:
            continue
        raw = m.group(1)
        if not from_link or pattern is BARE_LINK_RE:
            raw = raw.rstrip(":,")        # "@pm:server: stop", "https://matrix.to/#/@pm:server, stop"
        user = user_of(raw) if from_link else (raw if MXID_RE.fullmatch(raw) else None)
        if user is None:
            return None
        rest = m.start(1) + len(raw)
        tail = TAIL_RE.match(text, rest if pattern is not MD_LINK_RE else m.end())
        return (user, tail.end()) if tail else None
    return None
