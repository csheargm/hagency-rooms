"""@pm's side of builds by the server agent (ADR 0027): "Try a build" on the Server agent card in My agents, or "try a
build" in our private chat; the queue and outcome messages, in that chat.

Offered only while the talk-only trial runs (pm_trial.py) AND the trial-worker's heartbeat is fresh
(trial_jobs.Jobs.alive: it beats only when E2B, the Trial App and the org are configured and not
switched off). Otherwise nothing here shows or answers, and the words fall through as before.
@pm checks the gate and caps (trial_gate.py) and queues the job; the worker builds; @pm says what
happened, from fixed templates: model words only as an escaped, length-capped excerpt with no links,
no mentions and no markup. Mixed into pm_trial.TrialOffer (say, trial_on, trial_eligible, private_room, busy).
"""
import asyncio
import logging
import re
import time

from common import intents
from common.redact import inert, room_safe
from server import github_link
from server import trial_build
from server import trial_build_config
from server import trial_gate
from server import trial_jobs
from server import trial_tiers
from server.pm_address import unaddressed

log = logging.getLogger("loop")
# "try a build", "start a server agent build", "can I do a build": a request for one, never a refusal (GPT review)
BUILD_RE = re.compile(r"\b(try|start|run|do|want|give me)\s+(?:a|the|one|another|my)?\s*(?:server[- ]agent\s+|trial\s+)?build\b", re.I)


def wants_build(text: str) -> bool:
    """Asks for a build; never "don't start a build" or "bring my own … build" (as pm_trial.wants_trial)."""
    m = BUILD_RE.search(text)
    return bool(m) and not (intents.NEGATED.search(text[:m.end()]) or intents.BRING_OWN_RE.search(text))
RELAY_S = 5
EXCERPT_CHARS = 400
PRIVACY = ("It works on a private Hagency demo repo (not yours): the issue and the code go to E2B (a sandbox "
           "service) and Ollama Cloud. Nothing of yours is used.")
QUEUED = "🛠️ Your server agent build is queued: you're **#{n}**. I'll tell you here when it starts and when it's done. " + PRIVACY
ALREADY = "🛠️ Your server agent build is {where}. One at a time: I'll tell you here when it's done."
BUSY = "⏳ Server agent builds are busy: {n} are waiting already. Please try again in a while."
OFF = "Builds by the server agent aren't available just now."
NOT_FOR_YOU = "Builds by the server agent aren't available to your account."
STARTED = "🛠️ Your server agent build has started: issue #{issue} in {repo}, \"{title}\". It usually takes a few minutes."
DONE = ("✅ Your server agent build is done: pull request #{pr} in {repo}, for issue #{issue}. Changed: {files}. The tests "
        "passed (run again in a separate sandbox with no network), and an **AI self-check** was posted on it as a "
        "comment, not an approval. **Ready: a person merges** (the trial never merges).\n\nThe self-check said: {review}")
POSTED = "an **AI self-check** was posted on it as a comment, not an approval"
NOT_POSTED = "the AI self-check couldn't be posted on it (read the change yourself)"
OPENED_UNFINISHED = ("⚠️ Your server agent build opened pull request #{pr} in {repo}, but didn't finish: {why}. Its AI "
                     "self-check may be missing. A person reviews and merges it (the trial never merges).")
PAUSED = "⏳ Builds by the server agent are paused for now (today's sandbox time or the AI budget is used up). Please try again later."
NO_REPO = "⏳ Every demo repo is in use by someone else's build just now. Please try again in a while."
TOO_SOON = "One moment: I'm still answering your last build request."
ASK_EVERY_S = 20
FAILED = "⚠️ Your server agent build didn't finish: {why}. Nothing was opened. You have {left} builds left today."


def excerpt(text: object, limit: int = EXCERPT_CHARS) -> str:
    """Anything a model or a repo chose (its words, a title, a file name), inert in a room (common/redact.inert)."""
    return inert(room_safe(text, 4000), limit)


def news_words(job: dict, event: str, left: int) -> str:
    out = job.get("outcome") or {}
    if event == "started":
        return STARTED.format(issue=int(job.get("issue") or 0), repo=excerpt(job.get("repo") or "a demo repo", 120),
                              title=excerpt(job.get("title") or "", 120))
    if event == trial_jobs.DONE:
        files = ", ".join(excerpt(p, 200) for p in (out.get("files") or [])[:10]) or "nothing"
        said = DONE.format(pr=int(out.get("pr") or 0), repo=excerpt(out.get("repo") or "", 120), issue=int(out.get("issue") or 0),
                           files=files, review=excerpt(out.get("review") or "(nothing)"))
        return said.replace(POSTED, NOT_POSTED) if out.get("review") == trial_build.NO_REVIEW else said
    why = trial_build.WHY.get(str(out.get("why")), trial_build.WHY["error"])
    if out.get("pr"):                 # it got as far as opening one (then a restart or a switch-off)
        return OPENED_UNFINISHED.format(why=why, pr=int(out["pr"]), repo=excerpt(out.get("repo") or "", 120))
    if out.get("why") == "patch" and out.get("rule"):
        why = f"{why}, because {excerpt(out['rule'], 200)}"
    return FAILED.format(why=why, left=left)


class BuildOffer:
    def build_jobs(self) -> trial_jobs.Jobs:
        if "_build_jobs" not in self.__dict__:
            self._build_jobs = trial_jobs.Jobs(trial_build_config.shared_dir())
        return self._build_jobs

    def build_usage(self) -> trial_gate.Usage:
        if "_build_usage" not in self.__dict__:
            self._build_usage = trial_gate.Usage()
        return self._build_usage

    def build_caps(self) -> dict | None:
        """The worker's caps while builds are on (and the talk trial runs); None: off, nothing shows."""
        return self.build_jobs().alive() if self.trial_on() else None

    def may_build(self, person: str, caps: dict) -> bool:
        """Anyone who may use the server agent, with agents of their own or not (the gate decides the rest)."""
        return self.trial_eligible(person) or person in (caps.get("demo_users") or [])

    def build_allowance(self, person: str) -> tuple[int, int]:
        """(builds a day, builds ever) from their tier (trial_tiers.py: free 3 and 10; a paid tier may raise them)."""
        tiers = self.trial.tiers if self.trial is not None else trial_tiers.Tiers()   # down: their tier is unknown, so FREE
        return tuple(max(0, int(tiers.limit(person, k))) for k in (trial_tiers.BUILDS_DAY, trial_tiers.BUILDS_TOTAL))

    async def start_build(self, room_id: str, person: str) -> str:
        """The gate, then the queue; what was said (also posted in room_id)."""
        caps = self.build_caps()
        link = github_link.link_of(person)
        if caps is None:
            said = OFF
        elif not self.may_build(person, caps):
            said = NOT_FOR_YOU
        elif refused := self.too_soon(person) or self.unavailable(caps, link) or await trial_gate.refusal(
                person, link, caps, self.build_allowance(person), self.build_usage(), time.time()):
            said = refused
        else:
            said = self.queue_build(room_id, person, link, caps)
        await self.say(room_id, said)
        return said

    def too_soon(self, person: str) -> str | None:
        """One ask per person per ASK_EVERY_S: clicks never become a flood of GitHub lookups (independent review)."""
        asked = self.__dict__.setdefault("_build_asks", {})
        if time.monotonic() - asked.get(person, -ASK_EVERY_S) < ASK_EVERY_S:
            return TOO_SOON
        asked[person] = time.monotonic()
        return None

    @staticmethod
    def unavailable(caps: dict, link: dict | None) -> str | None:
        """Refused before anything is counted: builds paused, or every demo repo leased to someone else."""
        if caps.get("paused"):
            return PAUSED
        holders = caps.get("lease_holders") or []
        if caps.get("free_repos") == 0 and not (link and link["id"] in holders):
            return NO_REPO
        return None

    def queue_build(self, room_id: str, person: str, link: dict, caps: dict) -> str:
        usage = self.build_usage()
        state, said, n = self.build_jobs().enqueue(person, link["id"], room_id, int(caps["TRIAL_BUILD_QUEUE"]),
                                                   admit=lambda now: self.unavailable(now, link), charge=lambda: usage.take(link["id"]))
        if state == trial_jobs.REFUSED:              # paused, or no repo, by the latest heartbeat (GPT review)
            return said
        if state == trial_jobs.OFF:                  # the worker stopped while the gate was checked (GPT review)
            return OFF
        if state == trial_jobs.BUSY:
            return BUSY.format(n=caps["TRIAL_BUILD_QUEUE"])
        if state == trial_jobs.ALREADY:
            return ALREADY.format(where="running now" if n == 0 else f"queued (#{n})")
        return QUEUED.format(n=n)                     # counted as queued (charge); given back if the server fails it

    async def on_build_text(self, room, event) -> bool:
        """"try a build" in our private chat (the caller checked it is): True when answered."""
        if not wants_build(unaddressed(event.body, self.user_id)) or self.build_caps() is None:
            return False
        await self.start_build(room.room_id, event.sender)
        return True

    async def build_action(self, person: str) -> str | dict:
        """The panel's "Try a build": the same as typing it, answered in our private chat."""
        dm = await self.private_room(person)
        if dm is None:
            return "I couldn't open our private chat; please try again."
        said = await self.start_build(dm, person)
        plain = said.replace("**", "")              # the panel shows it as text (the chat has it with markup)
        return {"result": "ok", "says": plain} if said.startswith("🛠️") else plain

    def build_view(self, person: str) -> dict | None:
        """The Server agent card's build part, or None (builds off)."""
        caps = self.build_caps()
        if caps is None or not self.may_build(person, caps):
            return None
        mine = self.build_jobs().of(person)
        link = github_link.link_of(person)
        allowance = self.build_allowance(person)
        if caps.get("paused") and mine is None:
            return {"left": 0, "per_day": allowance[0], "github": bool(link), "days": caps.get("TRIAL_MIN_ACCOUNT_DAYS", 30),
                    "state": "paused for now"}
        return {"left": trial_gate.left_today(self.build_usage(), link["id"] if link else None, allowance),
                "per_day": allowance[0], "github": bool(link),
                "days": caps.get("TRIAL_MIN_ACCOUNT_DAYS", 30),
                "state": None if mine is None else "running" if mine[1]["state"] == trial_jobs.RUNNING else f"queued #{mine[1]['position']}"}

    async def relay_builds_once(self) -> int:
        """Say every piece of news not said yet; how many were said."""
        jobs, said = self.build_jobs(), 0
        for job_id, job, event in jobs.news():
            link = github_link.link_of(job.get("visitor") or "")
            allowance = self.build_allowance(job.get("visitor") or "")
            left = trial_gate.left_today(self.build_usage(), link["id"] if link else job.get("github_id"), allowance)
            if event == trial_jobs.FAILED and (job.get("outcome") or {}).get("why") in trial_build.REFUNDED:
                self.refund(jobs, job_id, job)       # the server's failure, not theirs: given back, once, first
                left = trial_gate.left_today(self.build_usage(), link["id"] if link else job.get("github_id"), allowance)
            try:
                await self.say(job["dm"], news_words(job, event, left))
            except RuntimeError as e:    # Matrix refused: said again next round (the job keeps it unsaid)
                log.error("trial build %s news to %s: %s", job_id, job.get("dm"), room_safe(e))
                continue
            jobs.told(job_id, event)
            said += 1
        return said

    def refund(self, jobs: trial_jobs.Jobs, job_id: str, job: dict) -> None:
        """Give the build back on the day it was charged (marked on the job first: never twice)."""
        if (day := jobs.mark_refunded(job_id)) and job.get("github_id") is not None:
            self.build_usage().give_back(int(job["github_id"]), day)

    async def relay_builds(self) -> None:
        while True:
            try:
                await self.relay_builds_once()
            except (OSError, ValueError, KeyError, TypeError) as e:   # a damaged jobs file: logged, tried again
                log.error("trial build news: %s", room_safe(e))
            await asyncio.sleep(RELAY_S)
