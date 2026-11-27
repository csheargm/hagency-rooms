"""@pm's planning step: "@pm let's build it" → the requester's own agent drafts issues from the
room's discussion → @pm shows the plan → the owner or a maintainer confirms with ✅ → the same agent
opens the issues under its own GitHub identity → the loop picks them up.

@pm has no AI and no GitHub write access of its own: the requester's agent does the thinking and
the writing, so planning runs on the requester's model and account. Mixed into loop.Loop.
"""
import asyncio
import re
import time
import urllib.parse
import uuid

from nio import MatrixRoom

from common import github
from server import ownership
from common import room_state
from server.pm_settings import NO_REPO
from adapters.agents import planning
from common.setup_accounts import api

CONFIRM_S = 600
HISTORY = 200                     # room events read for the discussion
# What happens once the plan is approved, by the room's mode (so "autonomous" is never a surprise).
AFTER_APPROVAL = {"autonomous": "Then the room runs on its own: build → review → merge, no more 👍 needed.",
                  "assist": "Then agents build and review; a person merges each change.",
                  "off": "Nothing starts building until the owner says \"@pm let us approve each change\" "
                         "or \"@pm take it all the way\"."}   # live test: "assist or autonomous" was jargon


# Live test 2026-11-23 (a 3B local model): "⚠️ plan didn't work: the plan wasn't valid JSON: Expecting ','
# delimiter…" told a newcomer nothing they could act on.
OPENED = "Opened "                   # @pm's line after filing a plan: a new plan's discussion starts after it
FILED_RE = re.compile(r"^Opened #\d+(, #\d+)*\. ")   # exactly that line (GPT review: not any reply starting "Opened")
PLAN_FAILED = ("⚠️ {agent} couldn't turn the discussion into a plan ({why}). Small local models often can't. "
               "Say \"@pm let's build it\" to try again; I plan with the first agent of whoever asks, so someone "
               "whose first agent runs a stronger AI can ask instead.")
# Review 2026-11-26: after a failed filing (no GitHub sign-in, say), "@pm retry" said "Nothing is paused
# here"; the plan is kept (last_plans), so showing it again gives its 👍 back. Only when filing failed before
# any issue was made (no sign-in): after a partial filing, approving again would open duplicates (GPT re-check).
PLAN_ABOVE = "To open these issues, click 👍 on my plan question above."
PLAN_TO_APPROVE = 'To open these issues, say "@pm show the plan" and click 👍 on it.'
FILE_AGAIN = ' The plan is kept: once that\'s fixed, say "@pm show the plan" and approve it again.'

class Planner:
    plans: dict                   # room → {"task_id", "agent", "by"} while an agent drafts or files
    last_plans: dict              # room → the plan last shown (items, agent, by), kept on disk

    def own_agent(self, room: MatrixRoom, person: str) -> str | None:
        """The requester's own registered agent in this room: planning runs on their model."""
        want = ownership.own_agent_id(person)       # theirs by full ID, never a namesake's
        return want if want and want in self.agents_in(room) else None

    async def start_plan(self, room: MatrixRoom, sender: str) -> None:
        rid = room.room_id
        repo = await room_state.repo_of(self.client, rid)
        if repo is None:
            await self.say(rid, NO_REPO)
            return
        agent = self.own_agent(room, sender)
        if agent is None:
            await self.say(rid, f"I plan with your own agent, and {ownership.agent_id(sender).split(':')[0][1:]} "
                                "isn't in this room. Invite it, then ask me again.")
            return
        discussion = await asyncio.to_thread(self.discussion, rid)
        issues = await github.run_json(["issue", "list", "-R", repo, "--state", "open", "--label",
                                        github.LABEL, "--json", "title", "--limit", "100"])
        task_id = uuid.uuid4().hex
        self.plans[rid] = {"task_id": task_id, "agent": agent, "by": sender, "repo": repo}
        await self.say(rid, f"{agent}, please turn this room's discussion into a plan", agent,
                       {"kind": "plan", "task_id": task_id, "repo": repo, "discussion": discussion,
                        "existing": [i["title"] for i in issues]})

    def discussion(self, room_id: str, include_self: bool = False) -> str:
        """What people (and their agents' chat replies) said, oldest first; no task traffic, board edits
        or commands. include_self adds @pm's own messages (for understanding a reply to them)."""
        path = f"/_matrix/client/v3/rooms/{urllib.parse.quote(room_id)}/messages?dir=b&limit={HISTORY}"
        events = api("GET", path, token=self.client.access_token).get("chunk", [])
        lines = []
        for ev in reversed(events):
            content = ev.get("content", {})
            body = content.get("body", "")
            if (not include_self and ev.get("sender") == self.user_id and FILED_RE.match(body)
                    and "format" in content):     # a model's reply is sent plain (say(plain=True)): never this line
                lines = []                    # filed already: only what was said since is a new ask (live test)
                continue
            if (ev.get("type") != "m.room.message" or (ev.get("sender") == self.user_id and not include_self)
                    or not body or body.lstrip("* ").startswith("📋")
                    or github.TASK_KEY in content or github.RESULT_KEY in content or "m.new_content" in content
                    or body.startswith("!")):
                continue
            lines.append(f"{ev['sender'].split(':')[0].lstrip('@')}: {body}")
        return "\n".join(lines)[-planning.MAX_DISCUSSION:]

    async def on_plan_result(self, room: MatrixRoom, sender: str, result: dict) -> None:
        rid, plan = room.room_id, self.plans.get(room.room_id)
        if not plan or result.get("task_id") != plan["task_id"] or sender != plan["agent"]:
            return                                    # stale, or not the agent we asked
        self.plans.pop(rid)
        if result.get("error"):
            await self.say(rid, PLAN_FAILED.format(agent=plan["agent"].split(":")[0].lstrip("@"),
                                                   why=str(result["error"])[:200])
                           if result.get("kind") == "plan" else
                           f"⚠️ {result['kind'].replace('_', ' ')} didn't work: {str(result['error'])[:200]}"
                           + (FILE_AGAIN if result.get("kind") == "file_issues" and rid in self.last_plans
                              and github.NOT_SIGNED_IN in str(result["error"]) else ""))   # only when none was opened (GPT)
        elif result["kind"] == "plan":
            await self.propose(rid, plan, result.get("items"))
        else:
            self.last_plans.pop(rid, None)                    # opened: it's issues now, not a plan
            self.kick()                                       # list them on the board / start them now
            numbers = ", ".join(f"#{n}" for n in result.get("numbers", []))
            nxt = ("The agents start on them now." if await self.level(rid) != "off" else
                   'Say "take it all the way" or "let us approve each change" to start building.')
            self.note_outcome(plan.get("proposal"), f"approved, and I opened {numbers}")
            await self.say(rid, f"{OPENED}{numbers}. {nxt}")

    async def propose(self, rid: str, plan: dict, items: object) -> None:
        try:
            if not isinstance(items, list) or len(items) > planning.MAX_ITEMS:
                raise planning.PlanError("not a list of items")
            items = [planning.clean_item(x) for x in items]       # re-checked: the agent isn't trusted
        except planning.PlanError as e:
            await self.say(rid, f"⚠️ the plan came back malformed ({e}); ask me again")
            return
        if not items and self.last_plans.get(rid, {}).get("items"):
            await self.say(rid, "I found nothing to change, so the plan stays as it was:")
            await self.show_plan(rid)                         # never lose a plan to an empty revision
            return
        if not items:
            await self.say(rid, "I don't see anything new agreed in the discussion yet.")
            return
        self.last_plans[rid] = {"items": items, "agent": plan["agent"], "by": plan["by"], "repo": plan.get("repo")}
        await self.show_plan(rid)

    async def show_plan(self, rid: str, approve: bool = True) -> None:
        """Post the room's current plan with its ✅ (also for "@pm show the plan"). approve=False: only shown,
        nothing to approve installed (a request @pm only inferred: pm_implicit.act_implicit, GPT review)."""
        last = self.last_plans.get(rid)
        if not last:
            await self.say(rid, "There's no plan yet. Say \"let's build it\" and I'll draft one from the discussion.")
            return
        items, gen, older = last["items"], self.generation.get(rid, 0), self.pending.get(rid)
        if not approve:
            await self.say(rid, f"Here's the plan:\n\n{planning.show(items)}\n\n" + (
                PLAN_ABOVE if (older or {}).get("kind") == "plan" else PLAN_TO_APPROVE))
            return
        if (older or {}).get("kind") == "plan":
            self.note_outcome(older.get("event_id"), "replaced by a newer plan, shown below it")
        after = AFTER_APPROVAL.get(await self.level(rid), AFTER_APPROVAL["off"])
        event_id = await self.say(rid, f"Here's the plan from your discussion:\n\n{planning.show(items)}\n\n"
                                       f"**Click 👍 below** (or reply \"ok\") to open "
                                       f"{'this issue' if len(items) == 1 else f'these {len(items)} issues'}. {after} To change it, tell me what to change.")
        if not self.still(rid, gen, older):   # pm_understand.py: a stop, or another question, while I posted it
            self.note_outcome(event_id, "dropped: things changed before it could be approved")
            return
        self.pending[rid] = {"kind": "plan", "event_id": event_id, "until": time.time() + CONFIRM_S,
                             "items": items, "agent": last["agent"], "by": last["by"], "repo": last.get("repo")}
        await self.offer_thumb(rid, event_id)

    async def file_plan(self, room: MatrixRoom, pending: dict, approver: str) -> None:
        repo = await room_state.repo_of(self.client, room.room_id)
        if repo is None or pending.get("repo") != repo:   # planned from one repo's discussion, never filed in another
            await self.say(room.room_id, "This room's repo changed since that plan; ask me to plan again.")
            return
        task_id = uuid.uuid4().hex
        self.plans[room.room_id] = {"task_id": task_id, "agent": pending["agent"], "by": approver,
                                    "proposal": pending.get("event_id")}
        await self.say(room.room_id, f"{pending['agent']}: open the approved issues", pending["agent"],
                       {"kind": "file_issues", "task_id": task_id, "items": pending["items"],
                        "repo": repo,
                        "approved_by": approver.split(":")[0].lstrip("@")})
