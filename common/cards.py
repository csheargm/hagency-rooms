"""Panel cards (ADR 0019): small views of a project room that a person's own agent makes for them.

One place for the contract both sides use: what the agent is asked for, how its answer is read, and
how a card is wrapped before the panel shows it. The agent adapter makes the card (card_task.py); @pm
checks it again and keeps it (pm_cards.py), because what an agent sends back is never trusted.

A card runs in a sandboxed frame (allow-scripts only: no access to the panel, its session, cookies or
storage, no forms, popups or navigating the page) under its own Content-Security-Policy that blocks every
network request, so all a card can do is draw the room data the panel hands it.
"""
import re
import unicodedata

MAX_HTML = 40_000                 # characters; Matrix events top out at 64 KB
MAX_TITLE = 40
MAX_REQUEST = 300
TITLE_RE = re.compile(r"^\s*TITLE:\s*(.+?)\s*$", re.M)
HTML_BLOCK_RE = re.compile(r"```html\s*\n(.*?)```", re.S | re.I)
DOCTYPE_RE = re.compile(r"^\s*<!doctype[^>]*>", re.I)

# What hagency.onData hands a card (pm_cards.card_data builds exactly this).
DATA_DOC = """hagency.onData(function (d) { ... }) calls your function now and again every few seconds with:
  d.room    the room's name                      d.repo  "owner/name", or null
  d.mode    "off", "assist" or "autonomous"       d.me    the username of the person looking
  d.board   the room's Project status, as markdown text
  d.work    [{issue, title, stage, builder, reviewer, pr, ended}]: issues the agents took on
            (stage: building, reviewing, fixing, merging, done, stopped, ...; builder, reviewer: agent
            names; pr: a number or null; ended: Unix seconds when it finished, or null)
  d.queued  [{issue, title}]: issues waiting their turn
  d.plan    [title, ...]: the plan waiting for approval
  d.agents  [{name, owner, online}]: the agents in the room (online: true, false or null = unknown)"""


class CardError(ValueError):
    pass


def clean_request(text: str) -> str | None:
    """What the person asked for, on one line, or None (empty, too long, or holding control characters)."""
    text = " ".join(str(text).split())
    if not 1 <= len(text) <= MAX_REQUEST or any(unicodedata.category(c)[0] == "C" for c in text):
        return None
    return text


def card_prompt(request: str, now_html: str = "") -> str:
    change = (f"\nThe card is now:\n```html\n{now_html}\n```\nChange it as asked; keep what wasn't asked about.\n"
              if now_html else "")
    return ("You make one small card for the Hagency panel, a page shown above a project's chat room. Only the "
            "person who asked sees it.\n"
            f"They ask: {request}\n{change}\n"
            "How the card gets its data:\n" + DATA_DOC + "\n\n"
            "Rules:\n"
            "- One self-contained HTML document: inline <style> and <script> only. No external files, fonts, images, "
            "links or network requests (all are blocked). Draw charts with inline SVG or CSS.\n"
            "- Read data only through hagency.onData. No forms, alerts, popups or storage.\n"
            "- About 700 px wide or narrower, as tall as it needs. Transparent background; readable in light and "
            "dark mode (prefers-color-scheme). Plain, friendly words; say so when there is no data yet.\n"
            f"- Under {MAX_HTML:,} characters.\n\n"
            f"Answer with exactly: a first line `TITLE: <a short name, at most {MAX_TITLE} characters>`, then the "
            "HTML in one ```html block. Nothing else.")


def clean_title(title: str) -> str:
    title = " ".join(str(title).split()).strip("\"'`*")
    title = "".join(c for c in title if unicodedata.category(c)[0] != "C")
    return title[:MAX_TITLE].strip() or "My card"


def clean_html(html: object) -> str:
    """The card's HTML, re-checked: a non-empty string under the limit, without its own doctype."""
    if not isinstance(html, str):
        raise CardError("no HTML came back")
    html = DOCTYPE_RE.sub("", html, count=1).strip()
    if not html:
        raise CardError("the card came back empty")
    if len(html) > MAX_HTML:
        raise CardError(f"the card is too big ({len(html):,} characters; at most {MAX_HTML:,})")
    return html


def parse_reply(text: str) -> tuple[str, str]:
    """(title, html) from the agent's answer; CardError when it holds no card."""
    block = HTML_BLOCK_RE.search(text)
    html = block.group(1) if block else (text.strip() if text.strip().startswith("<") else "")
    if not html:
        raise CardError("the agent's answer held no card")
    title = TITLE_RE.search(text[:block.start()] if block else "")
    return clean_title(title.group(1) if title else ""), clean_html(html)


# Put before every card: its own policy (no network at all), then hagency.onData and the frame's height
# for the panel. The panel's own policy applies too (a srcdoc frame inherits it).
FRAME_HEAD = """<!doctype html><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'">
<script>
window.hagency = (function () {
  var fns = [], last = null;
  addEventListener("message", function (e) {
    if (e.source !== parent || !e.data || !("hagency" in e.data)) return;
    last = e.data.hagency;
    fns.forEach(function (f) { try { f(last); } catch (x) { /* the card's own bug: the rest still draw */ } });
  });
  function size() { parent.postMessage({cardHeight: document.documentElement.scrollHeight}, "*"); }
  addEventListener("load", function () { size(); if (window.ResizeObserver) new ResizeObserver(size).observe(document.documentElement); });
  return {onData: function (f) { fns.push(f); if (last) f(last); }};
})();
</script>
"""


def frame_doc(html: str) -> str:
    """The whole document the panel's sandboxed frame shows (its srcdoc)."""
    return FRAME_HEAD + clean_html(html)
