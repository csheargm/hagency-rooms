"""A leading mention pill in a message's plain body: who it names and where the rest begins.

Live test 2026-11-26: "@Product Manager retry #13", typed with Element's mention pill on a phone, got
"I didn't catch that": a pill's plain body is a markdown link or the full ID, not the words. Forms read
here, at the start of the text only: [label](https://matrix.to/#/<id>), https://matrix.to/#/<id>, <id>.
Each pattern is one bounded run of a single character class, so no input backtracks (GPT review).
"""
import re
import urllib.parse

PERMALINK = "https://matrix.to/#/"
MXID_RE = re.compile(r"@[a-z0-9._=/+-]{1,255}:(?:[A-Za-z0-9.-]{1,255}|\[[0-9A-Fa-f:.]{2,45}\])(?::\d{1,5})?", re.I)
MD_LINK_RE = re.compile(r"\s*\[[^\]\n]{0,100}\]\(([^)\s]{1,400})\)")
BARE_LINK_RE = re.compile(r"\s*(https://matrix\.to/#/[^\s]{1,400})")
BARE_ID_RE = re.compile(r"\s*(@[^\s:]{1,255}:[^\s]{1,300})")   # "@pm:retry" names a user too (GPT review)
TAIL_RE = re.compile(r"[:,]?(?:\s+|$)")


def user_of(link: str) -> str | None:
    """The one user a matrix.to link names (percent-decoded), or None for anything else."""
    if not link.startswith(PERMALINK):
        return None
    target = urllib.parse.unquote(link[len(PERMALINK):].split("?", 1)[0])
    return target if MXID_RE.fullmatch(target) else None


def leading(text: str) -> tuple[str, int] | None:
    """(user id, where the rest starts) for a pill at the very start; (its raw text, 0) for something
    shaped like one that names no one exactly; None when the text doesn't start with one."""
    for pattern, from_link in ((MD_LINK_RE, True), (BARE_LINK_RE, True), (BARE_ID_RE, False)):
        m = pattern.match(text)
        if not m:
            continue
        raw = m.group(1)
        if not from_link or pattern is BARE_LINK_RE:
            raw = raw.rstrip(":,")        # "@pm:server: stop", "https://matrix.to/#/@pm:server, stop"
        user = user_of(raw) if from_link else (raw if MXID_RE.fullmatch(raw) else None)
        tail = TAIL_RE.match(text, m.end() if pattern is MD_LINK_RE else m.start(1) + len(raw))
        if user is None or tail is None:
            return raw, 0             # looks like a pill but names no one exactly ("@pm:x; stop"): not @pm
        return user, tail.end()
    return None
