"""The trial builder's tool loop (ADR 0027): the model asks, the BUILD sandbox does, nothing runs here.

Tools: run, read_file, write_file, list_dir (all inside the sandbox, paths relative to the repo) and
finish. Caps: TRIAL_BUILD_STEPS tool calls, TRIAL_BUILD_S seconds, TRIAL_BUILD_TOKENS model tokens,
OUTPUT_CHARS of each tool's output kept in the chat. Every model call holds the one trial Ollama
slot (talk first) and is counted against the month (trial_budget.Budget). Whatever the loop ends
with, the worker builds and checks the patch itself (trial_patch.py): `finish` is not trusted either.
"""
import asyncio
import json
import posixpath
import shlex
import time
from dataclasses import dataclass

from common import sandbox
from server import trial_budget
from server import trial_build_model

OUTPUT_CHARS = 8 * 1024
MAX_WRITE = 200 * 1024
RUN_TIMEOUT_S = 120
FINISHED, STEPS, TIME, TOKENS, STOPPED = "finished", "steps", "time", "tokens", "stopped"


class BuildStopped(RuntimeError):
    """The month's budget, a pause or the plan's share stops builds (trial_budget's reason)."""
    def __init__(self, why: str) -> None:
        super().__init__(why)
        self.why = why


@dataclass
class Ended:
    reason: str
    summary: str = ""
    steps: int = 0
    tokens: int = 0


def repo_path(path: object) -> str | None:
    """A path inside the repo, normalised; None when it leaves it (absolute, "..")."""
    p = posixpath.normpath(str(path or ".").strip())
    return None if p.startswith(("/", "..")) or "\0" in p else ("" if p == "." else p)


def arguments(raw: object) -> dict:
    """A tool call's arguments: Ollama gives an object; some models a JSON string. Anything else: none."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:        # not JSON: the tool answers that its arguments are missing
            return {}
    return raw if isinstance(raw, dict) else {}


def cut(text: str) -> str:
    return text if len(text) <= OUTPUT_CHARS else text[:OUTPUT_CHARS] + "\n[… cut]"


class ToolLoop:
    def __init__(self, box, slot, budget: trial_budget.Budget, ask=None, clock=time.monotonic,
                 steps: int = 40, seconds: float = 600, tokens: int = 150_000) -> None:
        self.box, self.slot, self.budget, self.ask = box, slot, budget, ask or trial_build_model.ask
        self.clock, self.max_steps, self.max_tokens = clock, steps, tokens
        self.deadline, self.used = clock() + seconds, 0

    async def think(self, chat: list[dict]) -> dict | None:
        """One model call (None: this build's tokens are used up); BuildStopped: the month's are."""
        if stop := await self.budget.stopped():
            raise BuildStopped(stop)
        reserved = trial_build_model.high_estimate(chat)
        if self.used + reserved > self.max_tokens:
            return None
        if not self.budget.reserve(reserved):
            raise BuildStopped(trial_budget.TOKENS)
        await self.slot.acquire()
        spent = reserved                     # a failed call keeps its estimate: it may have cost
        try:
            message, spent = await self.ask(chat)
        finally:
            self.slot.release()
            self.budget.settle(reserved, spent)
            self.used += spent
        return message

    async def tool(self, name: str, args: dict) -> str:
        """One tool's result, as text for the model (refusals too: it may try something else)."""
        if name == "run":                 # output capped inside the sandbox (common/sandbox.capped)
            left = max(1.0, min(RUN_TIMEOUT_S, self.deadline - self.clock()))
            try:
                r = await self.box.run_capped(str(args.get("command", "")), OUTPUT_CHARS, timeout=left)
            except asyncio.TimeoutError:
                return "the command timed out"
            return cut(f"exit code {r.exit_code}\n{r.stdout}".strip())
        path = repo_path(args.get("path"))
        if path is None:
            return "refused: paths are relative to the repo and stay inside it"
        if name == "read_file":
            data = await self.box.read_capped(path, OUTPUT_CHARS + 1)
            return "no such file" if data is None else cut(data.decode("utf-8", "replace"))
        if name == "write_file":
            content = str(args.get("content", ""))
            if not path or len(content) > MAX_WRITE:
                return "refused: a file of the repo, at most 200 KB"
            await self.box.write_files({path: content.encode("utf-8")})
            return "written"
        if name == "list_dir":
            r = await self.box.run_capped(f"ls -la -- {shlex.quote(posixpath.join(sandbox.ROOT, path))}", OUTPUT_CHARS, timeout=30)
            return cut(r.stdout)
        return f"no such tool: {name[:40]}"

    async def run(self, chat: list[dict]) -> Ended:
        steps = 0
        while True:
            if self.clock() >= self.deadline:
                return Ended(TIME, steps=steps, tokens=self.used)
            message = await self.think(chat)
            if message is None:
                return Ended(TOKENS, steps=steps, tokens=self.used)
            if self.clock() >= self.deadline:     # the slot or the model took the time left (GPT review)
                return Ended(TIME, steps=steps, tokens=self.used)
            calls = [c.get("function") or {} for c in message.get("tool_calls") or [] if isinstance(c, dict)]
            chat.append({"role": "assistant", "content": trial_build_model.clean(message.get("content") or "", OUTPUT_CHARS),
                         "tool_calls": message.get("tool_calls") or []})
            if not calls:
                chat.append({"role": "user", "content": "Use the tools to make the change, then call finish."})
            for call in calls:
                name = str(call.get("name", ""))
                args = arguments(call.get("arguments"))
                if name == "finish":
                    return Ended(FINISHED, trial_build_model.clean(args.get("summary", ""), 2000), steps, self.used)
                steps += 1
                if steps > self.max_steps:
                    return Ended(STEPS, steps=steps - 1, tokens=self.used)
                result = await self.tool(name, args)
                chat.append({"role": "tool", "tool_name": name, "content": trial_build_model.clean(result, OUTPUT_CHARS)})
