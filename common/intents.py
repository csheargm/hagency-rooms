"""What a person means when they talk to @pm, in plain words. No AI here on purpose: "stop" must
work even when every model is down, and a fixed list of intents means a cleverly worded message
can't produce anything else. (An AI fallback for phrasings this misses can come later; it would
still only pick from this same list.)
"""
import re

OFF, ASSIST, AUTONOMOUS, STATUS, SHOW_PLAN, PLAN, ABOUT, HELP, UNKNOWN = (
    "off", "assist", "autonomous", "status", "show_plan", "plan", "about", "help", "unknown")
FOLLOW_NEVER, FOLLOW_ASK, FOLLOW_ALWAYS = "follow_never", "follow_ask", "follow_always"
WEB = "web"            # "let my agent search the web": no switch since ADR 0006; @pm explains
CONSOLE = "console"
SET_REPO = "set_repo"
RETRY = "retry"
START = "start"        # "what should we do first?": the next steps, for this room
# "this room works on owner/name", "set the repo to owner/name", "use https://github.com/owner/name":
# the repo is the command's own argument (never any other owner/name-looking text in the message).
SET_REPO_RE = re.compile(
    r"\b(?:(?:this |the )?room (?:now )?works? on\s+|(?:set |change |switch )?(?:the |this room'?s? )?repo(?:sitory)?"
    r"(?: is| to|:)\s+|use\s+(?:the repo\s+)?(?=https?://))(?:https?://github\.com/)?"
    r"(?P<repo>[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100})/?(?=$|[\s,;!?)])", re.I)
NEGATED = re.compile(r"\b(don'?t|do not|never|not|no longer|stop using)\b", re.I)

_RULES = [   # checked in order; the first match wins, so the safest meanings come first
    # The follow-me settings mention "my agent" explicitly, so they can go before "stop" without
    # ever swallowing a real stop ("stop bringing my agent" is a setting, not a halt).
    # The old per-room web switch (ADR 0006 retired it), only ever answered. Either way round, and
    # either word order: "stop letting my agent use the web", "enable web search for my agent".
    (WEB, r"^(?=.*\b(stop letting|don'?t let|do not let|no longer let|disallow|revoke|take away|disable|"
          r"turn off|switch off)\b)(?=.*\bmy agent\b)(?=.*\b(web|internet|search\w*|brows\w*|online)\b)"),
    # A request, never a question about it: it starts with the verb ("can you …" is a polite form).
    (WEB, r"^\s*((can|could|would|will) you\s+)?(please\s+)?(let|allow|permit|enable|turn on|switch on)\b"
          r"(?=[^?]*\bmy agent\b)(?=[^?]*\b(web|internet|search\w*|brows\w*|online)\b)[^?]*\??$"),
    (FOLLOW_NEVER, r"\b(don'?t|do not|never|stop|no longer) (bring|invit|add)\w* my agent\b"),
    (FOLLOW_ASK, r"\bask (me )?(first |before )?(\w+ )?(bring|invit|add)\w* my agent\b|\bask me (first|before)\b.*\bmy agent\b"),
    (FOLLOW_ALWAYS, r"\b(always |please )?(bring|invite|add) my agent\b"),
    (OFF, r"\b(stop|pause|halt|kill|freeze|turn (\w+ ){0,3}off|switch (\w+ ){0,3}off|shut (\w+ )?down|"
          r"hold (on|off)|cancel)\b"),
    (ASSIST, r"\b(approv\w*|check with (us|me)|ask (us|me)|assist\w*|let (us|me) (review|check|decide)|"
             r"review (each|every|it|them)|human review|before (you )?merg\w*|we merge|i('ll| will) merge)\b"),
    (AUTONOMOUS, r"\b(all the way|to the end|end to end|on your own|autonomous\w*|without asking|"
                 r"(just )?(get|do) (it |them |this )?(all |everything )?done|get (it |them )?all done|"
                 r"finish (it|the project|everything|all|them)|full auto|go all in)\b"),
    (STATUS, r"\b(status|where are we|progress|what'?s left|how'?s it going|what are (you|the agents) doing|"
             r"what'?s happening|update me)\b"),
    (SET_REPO, SET_REPO_RE.pattern),
    (RETRY, r"^\s*(please\s+)?(retry|try (#?\d+ |it |that |this )?again|restart|redo)\b"),
    (CONSOLE, r"\b(console|settings?)\b.*\blink\b|\bsend me (my |the )?(console|settings)\b|\bopen (the )?console\b"),
    (SHOW_PLAN, r"\b((show|reiterate|repeat|see|resend|what'?s|where'?s) (me )?(the |teh |our |that |this )?plan)\b"),
    (PLAN, r"\b(let'?s (build|do|start|go|make)|build (it|this|that)|start (the project|building|it)|go ahead|"
           r"go for it|kick (it )?off|ship it|spec is ready|ready to build|make it|plan (it|this))\b"),
    (SHOW_PLAN, r"\bplans?\b"),                 # any other mention of the plan: show it
    (ABOUT, r"\b(who are you|what are you|your model|which model|are you (an? )?(ai|bot|human|llm))\b"),
    # Live test 2026-11-23: "@pm what should we do first in this project?" only got "Noted. …".
    # The whole message only (GPT review): "for the next steps, add a migration" revises a plan, and
    # "help with the next steps" is a help request; a short tail like "in this project?" is fine.
    (START, r"^\W*(@?pm\b[\s:,]*)?(please\s+)?(so\s+)?(what (should|do|shall|can) (we|i) do( first| next| now)?|"
            r"what'?s next|what next|what now|where (do|should|can) (we|i) (start|begin)|"
            r"how (do|should|can) (we|i) (start|begin|get started)|getting started|(the )?(next|first) steps?)"
            r"\b[^.!?;\n]{0,40}[?.!]*\s*$"),
    (HELP, r"\b(help|what can you do|how do (i|we) use you)\b"),
]
_COMPILED = [(intent, re.compile(rx, re.I)) for intent, rx in _RULES]
YES_RE = re.compile(r"^\s*(yes|yep|yeah|confirm(ed)?|go|do it|ok(ay)?|sure|✅|👍|👌)[\s.!]*$", re.I)
CONFIRM_REACTIONS = ("✅", "✔", "☑", "👍", "👌", "🆗", "💯")


def is_confirm_reaction(key: str) -> bool:
    """Any common "OK" emoji, ignoring skin tones and the emoji/text variation selectors."""
    base = "".join(ch for ch in key if ch not in "\ufe0f\ufe0e" and not 0x1F3FB <= ord(ch) <= 0x1F3FF)
    return base in CONFIRM_REACTIONS


MAX_TEXT = 1000                       # longer messages are read only this far (keeps every rule linear-ish)
SETTINGS = (FOLLOW_NEVER, FOLLOW_ASK, FOLLOW_ALWAYS)


def plainly_says(text: str, intent: str) -> bool:
    """The person's own words are an unmistakable request for `intent` (else: ask with a 👍)."""
    return classify(text.strip()[:MAX_TEXT]) == intent


def classify(text: str) -> str:
    text = text.strip()[:MAX_TEXT]
    for intent, rx in _COMPILED:
        m = rx.search(text)
        if m and intent in (ASSIST, AUTONOMOUS) and NEGATED.search(text[:m.start()]):
            return UNKNOWN            # "do not go autonomous" is not a request for it
        if m and (intent != SET_REPO or repo_named(text)):
            return intent
    return UNKNOWN


def repo_named(text: str) -> str | None:
    """The owner/name in a "this room works on owner/name" message (a GitHub URL works too)."""
    m = SET_REPO_RE.search(text)
    if not m or NEGATED.search(text[:m.start()]):     # "please do not use …" is not a command
        return None
    repo = m.group("repo").rstrip(".")               # a sentence's full stop, never part of the name
    return repo.removesuffix(".git") or None


QUESTION_RE = re.compile(r"\?\s*$|^\W*(@?pm\b[\s:,]*)?(what|how|why|when|which|who|does|do|did|is|are|was|can|"
                         r"could|would|will|should|shall|may|might)\b", re.I)


def is_question(text: str) -> bool:
    """Asked, not told ("what does autonomous mode do?", "can you go all the way"): never changes the mode
    by itself (GPT review); @pm says what the mode does and asks a 👍."""
    return QUESTION_RE.search(text.strip()) is not None


def is_yes(text: str) -> bool:
    """A bare OK ("yes", "ok", 👍): safe to read as consent even when it isn't addressed to @pm."""
    return YES_RE.match(text) is not None


# An approval in a person's own words: "ok go ahead", "sounds good, let's build it", "yes please".
# Every word must be one of these, so "go ahead but skip #2", "not yet" or "ok, wait" never count.
_APPROVE_CORE = {"yes", "yep", "yeah", "yup", "ok", "okay", "okey", "oki", "k", "kk", "sure", "go", "approve",
                 "approved", "confirm", "confirmed", "lgtm", "proceed", "agree", "agreed", "good", "great",
                 "perfect", "fine", "absolutely", "definitely", "ship", "build", "👍", "✅", "👌"}
_APPROVE_FILLER = {"ahead", "do", "it", "it's", "its", "please", "pls", "plz", "let's", "lets", "let", "us", "with",
                   "the", "this", "that", "that's", "thats", "plan", "them", "all", "right", "alright", "cool",
                   "nice", "now", "then", "and", "thanks", "thank", "you", "pm", "sounds", "looks", "seems", "me",
                   "for", "to", "on", "start", "work", "works", "make", "makes", "sense", "i", "we", "so", "just",
                   "really", "very", "totally", "move", "forward", "of", "course", "a", "open", "issue", "issues",
                   "go", "is", "set", "both", "one", "these", "those", "yes", "ok"} - {"us", "you", "for"}
_APPROVE_TOKEN = re.compile(r"[a-z']+|👍|✅|👌")
_APPROVE_MAX_WORDS = 15


def is_approval(text: str) -> bool:
    """A yes said in a sentence. Only for messages addressed to @pm (or replies to its question)."""
    t = text.lower().replace("’", "'")
    if "?" in t:
        return False              # a question is never a yes
    words = _APPROVE_TOKEN.findall(t)
    leftover = _APPROVE_TOKEN.sub("", t)
    if not words or len(words) > _APPROVE_MAX_WORDS or re.search(r"[^\s.,!:;()\-–—️@]", leftover):
        return False              # anything else (a number, a name, another emoji) means more than "yes"
    return (all(w in _APPROVE_CORE or w in _APPROVE_FILLER for w in words)
            and any(w in _APPROVE_CORE for w in words))


def issue_named(text: str) -> int | None:
    """The issue number in "retry #9" / "retry issue 9" (None: not said)."""
    m = re.search(r"(?:#|\bissue\s+|\bretry\s+)(\d{1,7})\b", text, re.I)
    return int(m.group(1)) if m else None
