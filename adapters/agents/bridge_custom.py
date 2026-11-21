"""Change your agent's own code from your private room with it (customize.py does the work).

  Betty (in her private room with betty-agent): "change your code so the transcript caption is shorter"
  betty-agent: works on Betty's private copy, then posts what changed with one-click buttons:
    👍 Use it myself         tests it, then Betty's agent runs her version (↩️ goes back)
    📤 Send as pull request  opens a PR to the official repo, tested by GitHub
    🗑️ Discard

Only the owner, only in a private two-person room with their agent, and each button works once,
in the room it was offered in. Mixed into bridge.Bridge (client, owner, owner_id, user_id, backend,
send, tasks, started_ms, sync_point).
"""
import asyncio
import logging
import re

from nio import MatrixRoom, ReactionEvent

from adapters.agents import agents
from common import customize
from common import github
from common import lifecycle
from common.redact import room_safe
from common.saved_dict import SavedDict

log = logging.getLogger("bridge")
CHANGE = re.compile(r"^\W*(please\s+|can you\s+|could you\s+)?(change|make|add|fix|update|remove|delete|"
                    r"customi[sz]e|improve|tweak|rename|let|teach)\b", re.I)
ABOUT_CODE = re.compile(r"\b(your (own )?(code|bridge|program)|the bridge|hagency( rooms)?|yourself)\b", re.I)
GO_BACK = customize.GO_BACK
USE, SEND, DROP, BACK = "👍", "📤", "🗑️", "↩️"


def edit_prompt(request: str) -> str:
    return ("You are changing the code of Hagency Rooms, the program that connects you to this chat. This folder "
            "is your owner's private copy: the running version is not affected until they choose to use it.\n"
            f"Your owner asks: {request}\n\nRules: edit only files in this folder. Keep the change small and in the "
            "existing style. Add or update a unittest (tests/test_*.py) for the new behaviour. Never weaken the owner-only "
            "check, the agent sandbox settings or secret handling unless the request explicitly asks for that, and "
            "never touch .env or credentials. Don't create links, .gitattributes or .pth files. Reply with 2-4 short "
            "plain lines saying what you changed.")


def is_private(room: MatrixRoom) -> bool:
    """Just the owner and me, and nobody can walk in."""
    return (room.joined_count == 2 and room.invited_count == 0
            and getattr(room, "join_rule", "invite") not in ("public", "knock", "knock_restricted", "restricted"))


class BridgeCustom:
    def wants_change(self, room: MatrixRoom, event) -> bool:
        """The owner, in our private room, asking to change this program (not a question about it)."""
        text = event.body.strip()
        return (is_private(room) and event.sender == self.owner_id and "?" not in text[-1:]
                and (bool(GO_BACK.search(text)) or (bool(CHANGE.match(text)) and bool(ABOUT_CODE.search(text)))))

    def offers(self) -> SavedDict:
        """Offer message → what it offers; on disk, so a click after a restart still works."""
        return SavedDict(customize.ROOT / "data" / f"custom-offers-{self.owner}.json")

    def custom_busy(self) -> asyncio.Lock:
        if not hasattr(self, "custom_lock"):
            self.custom_lock = asyncio.Lock()   # one change, test or PR at a time
        return self.custom_lock

    async def change_code(self, room: MatrixRoom, event) -> None:
        rid, text = room.room_id, event.body.strip()
        if await self.stopped_now(room, event):   # agent_stop.py: my owner stopped me
            return
        if GO_BACK.search(text):
            await self.go_back(rid)
            return
        if self.custom_busy().locked():
            await self.send(rid, "I'm still busy with your last change; ask again when it's done.", reply_to=event)
            return
        async with self.custom_busy():
            await self.send(rid, "🛠 Working on your private copy of my code. The version you use now isn't touched.")
            try:
                work = await customize.prepare(self.owner)
                summary = await agents.run_turn(self.backend, work, edit_prompt(text), write=True, fresh=True)
                sha = await customize.commit(self.owner, text)
                files = await customize.changed_files(self.owner) if sha else []
            except (agents.AgentError, github.GitHubError, customize.CustomError, OSError) as e:
                log.error("customizing failed: %s", e)
                await self.send(rid, f"⚠️ I couldn't make that change ({room_safe(e)}).", reply_to=event)
                return
            if not sha:
                await self.send(rid, f"I didn't change anything:\n\n{summary}", reply_to=event)
                return
            await self.offer(rid, sha, text, summary, files)

    async def offer(self, rid: str, sha: str, request: str, summary: str, files: list[str]) -> None:
        body, keys, record = offer_message(self.owner, rid, sha, request, summary, files)
        event_id = await self.send(rid, body)
        self.offers()[event_id] = record
        for key in keys:
            await self.react(rid, event_id, key)

    async def react(self, rid: str, event_id: str, key: str) -> None:
        try:
            await self.client.room_send(rid, "m.reaction", {"m.relates_to": {
                "rel_type": "m.annotation", "event_id": event_id, "key": key}})
        except Exception as e:                 # noqa: BLE001 — a missing button: typing still works
            log.warning("couldn't add %s in %s: %s", key, rid, e)

    async def on_custom_reaction(self, room: MatrixRoom, event: ReactionEvent) -> None:
        if event.sender != self.owner_id or lifecycle.is_stale(event, self.started_ms) or not self.sync_point.first_time(event.event_id):
            return                            # my own buttons, anyone else's clicks, and replays do nothing
        offer = self.offers().get(event.reacts_to)
        if not offer or offer.get("room") != room.room_id or offer.get("done") or not is_private(room):
            return                            # only once, only where it was offered, only while private
        if await self.stopped_now(room, None):   # agent_stop.py: a button click is work too
            return
        if self.custom_busy().locked():
            await self.send(room.room_id, "I'm still busy with your last change; click again when it's done.")
            return
        job = asyncio.create_task(self.act_on(room.room_id, event.reacts_to, event.key))
        self.tasks.add(job)
        job.add_done_callback(self.tasks.discard)

    async def act_on(self, rid: str, offer_id: str, key: str) -> None:
        async with self.custom_busy():
            offer = self.offers().get(offer_id) or {}
            if offer.get("done") or await self.is_stopped():   # agent_stop.py: stopped while it waited
                return
            try:
                done = await self.do(rid, offer, key)
            except (github.GitHubError, customize.CustomError, OSError) as e:
                log.error("custom action %s failed: %s", key, e)
                await self.send(rid, f"⚠️ That didn't work ({room_safe(e)}).")
                return
            if done:
                self.offers()[offer_id] = {**offer, "done": key}

    async def do(self, rid: str, offer: dict, key: str) -> bool:
        """One button; True when that offer is used up."""
        if key == BACK and offer.get("back"):
            changed = customize.go_back(self.owner, only_if=offer["back"])
            await self.send(rid, "↩️ Going back to the official version: I'll be back in a moment." if changed
                            else "That version isn't the one running any more, so nothing changed.")
            return True
        if key == USE and "sha" in offer:
            return await self.use(rid, offer)
        if key == SEND and "sha" in offer:
            await self.send(rid, "📤 Opening a pull request…")
            url = await customize.send_pr(self.owner, offer["sha"], offer["request"].splitlines()[0], pr_body(self.owner, offer))
            await self.send(rid, f"📤 Pull request opened: {url}\nThe project's tests run on it automatically.")
            return True
        if key == DROP and "sha" in offer:
            gone = await customize.discard(self.owner, offer["sha"])
            await self.send(rid, "🗑️ Discarded." if gone else "That's not my newest change any more, so I left it.")
            return True
        return False

    async def use(self, rid: str, offer: dict) -> bool:
        await self.send(rid, "🧪 Testing your version before switching (a minute or two)…")
        ok, result = await customize.use(self.owner, offer["sha"])
        if not ok:
            await self.send(rid, f"❌ Not switching: {result}. I'm staying on the version you use now. Tell me what to fix.")
            return False
        back = await self.send(rid, f"✅ {result.capitalize()}. Switching to your version {offer['sha'][:7]}: I'll be back "
                                    f"in a moment.\n\n{BACK} **Go back** to the official version any time (or tell @pm "
                                    "\"go back to the official bridge\", or use the console).")
        self.offers()[back] = {"room": rid, "back": offer["sha"]}
        await self.react(rid, back, BACK)
        return True

    async def go_back(self, rid: str) -> None:
        changed = customize.go_back(self.owner)
        await self.send(rid, "↩️ Going back to the official version: I'll be back in a moment." if changed
                        else "I'm already running the official version.")


def offer_message(owner: str, rid: str, sha: str, request: str, summary: str, files: list[str]):
    """The offer's text, its buttons, and what to remember about it (also used for Hermes: mcp_custom.py)."""
    shown = ", ".join(f"`{f}`" for f in files[:customize.MAX_DIFF_FILES]) + (" …" if len(files) > customize.MAX_DIFF_FILES else "")
    risky, no_use = customize.sensitive(files), customize.why_not_use(owner)
    body = (f"✏️ **Your version is ready** ({sha[:7]})\n\n{summary}\n\nFiles changed since the official version: {shown}\n\n"
            + (f"⚠️ **This touches the safety checks** ({', '.join(risky)}): the code that decides who I obey "
               "and what I may do.\n\n" if risky else "")
            + ("" if no_use else f"{USE} **Use it myself**: I test it, then run it, with my access to this computer. ")
            + f"{SEND} **Send as pull request** “{request.splitlines()[0][:100]}” to the official project. {DROP} **Discard**."
            + (f"\n\n(Using it here isn't possible: {no_use}.)" if no_use else ""))
    record = {"room": rid, "sha": sha, "request": request, "summary": summary, "risky": risky}
    return body, ((SEND, DROP) if no_use else (USE, SEND, DROP)), record


def pr_body(owner: str, offer: dict) -> str:
    risky = offer.get("risky") or []
    return (f"{offer['summary']}\n\nAsked for by {owner} in their private room and made by their agent with "
            "Hagency Rooms' customize flow.\n"
            + (f"\n⚠️ Touches safety-related files: {', '.join(risky)}. Please review those carefully.\n" if risky else ""))
