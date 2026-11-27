"""Where trial builds run untrusted code (ADR 0027): an E2B Firecracker microVM per build, never our VM.

Two kinds of sandbox, both fresh for one use and killed in `finally`:
  - BUILD: the agent's tool loop. Egress denied, except the package indexes (EGRESS_ALLOW). Before
    anything is uploaded, a blocked host is probed: unless it is plainly unreachable, the build is
    refused (fail closed: an inconclusive probe is a refusal too).
  - TEST: no network at all; the pristine repo plus the validated patch, and the template's fixed
    test command.
Nothing secret ever goes in: no environment variables are passed, and check_clean() refuses a sandbox
whose environment holds anything shaped like a credential (or can't be read). Every sandbox carries
METADATA, so the reaper can kill anything of ours older than REAP_AFTER_S (a crash, a lost connection).
A sandbox's output never floods the worker: commands run through capped(), listings are bounded.

E2BSandboxes wraps the E2B SDK (deploy/requirements-trial.txt pins it; imported only when used, so @pm and
the tests never need it). common/sandbox_fake.py is the same interface in memory, for tests.
"""
import asyncio
import base64
import hashlib
import json
import re
import shlex
import time
from dataclasses import dataclass

BUILD, TEST = "build", "test"
ROOT = "/home/user/repo"
EGRESS_ALLOW = ["pypi.org", "files.pythonhosted.org", "registry.npmjs.org"]
PROBE_HOST = "example.com"                    # not allowed: it must not answer
PROBE_CMD = f"curl -sS -o /dev/null -m 5 https://{PROBE_HOST}/"
# curl's exits that mean "no route out" (resolve, connect, timeout, TLS cut, nothing back): only these pass.
# Anything else (curl missing, a usage error, an answer) is inconclusive or open: refused (GPT review).
BLOCKED_EXITS = {6, 7, 28, 35, 52, 56}
ENV_CMD = "env"
SAFE_ENV_RE = re.compile(r"^(PATH|HOME|USER|LOGNAME|SHELL|PWD|OLDPWD|SHLVL|TERM|LANG|LC_[A-Z]+|HOSTNAME|_|"
                         r"PYTHON[A-Z_]*|PIP_[A-Z_]*|NODE_[A-Z_]*|NPM_CONFIG_[A-Z_]*|E2B_SANDBOX|E2B_SANDBOX_ID|"
                         r"E2B_TEMPLATE_ID|E2B_EVENTS_ADDRESS|DEBIAN_FRONTEND|VIRTUAL_ENV|TZ|MAIL)$")
CREDENTIAL_NAME_RE = re.compile(r"TOKEN|SECRET|PASS|KEY|CRED|AUTH|SESSION|COOKIE", re.I)
METADATA = {"hagency": "trial-build"}
REAP_AFTER_S = 20 * 60
VCPU, MEMORY_MB = 2, 1024
# Lists the repo's files as JSON, inside the sandbox (its own python3): {"files": {path: [sha256, size]},
# "links": [...], "other": [...], "truncated": bool}: at most MAX_ENTRIES entries, files over MAX_HASHED bytes
# not hashed (refused by size anyway). Whatever it says is checked again on our side (trial_patch.py).
MAX_ENTRIES, MAX_HASHED = 20_000, 5 * 1024 * 1024
LIST_PY = r"""
import hashlib, json, os, stat, sys
root, cap, big = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
out, n = {"files": {}, "links": [], "other": [], "truncated": False}, 0
for d, dirs, names in os.walk(root):
    dirs[:] = [x for x in dirs if x != ".git"]
    for x in names + [x for x in dirs if os.path.islink(os.path.join(d, x))]:
        n += 1
        if n > cap:
            out["truncated"] = True
            break
        p = os.path.join(d, x); rel = os.path.relpath(p, root); st = os.lstat(p)
        if stat.S_ISLNK(st.st_mode): out["links"].append(rel)
        elif stat.S_ISREG(st.st_mode):
            out["files"][rel] = [hashlib.sha256(open(p, "rb").read()).hexdigest() if st.st_size <= big else "", st.st_size]
        else: out["other"].append(rel)
print(json.dumps(out))
"""
LIST_CMD = f"python3 -c {shlex.quote(LIST_PY)} {ROOT} {MAX_ENTRIES} {MAX_HASHED}"
MAX_LIST_CHARS = 4_000_000
MAX_OUTPUT = 256 * 1024               # any one command's output we hold
MAX_READ = 1024 * 1024                # any one file we read back
EXIT_MARK = "__hagency_exit__"


class SandboxError(RuntimeError):
    """The sandbox couldn't be made, used or trusted; str() is safe to show (no keys, no paths of ours)."""


@dataclass
class Result:
    exit_code: int
    stdout: str = ""
    stderr: str = ""


def is_stale(started: float, now: float, older_than_s: int) -> bool:
    """The reaper's rule: started `older_than_s` ago or more (0: every one, the kill switch)."""
    return now - started >= older_than_s


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _is_entry(v: object) -> bool:
    return isinstance(v, list) and len(v) == 2 and isinstance(v[0], str) and isinstance(v[1], int) and v[1] >= 0


def parse_listing(text: str) -> dict:
    """LIST_CMD's output, shape-checked (a sandbox that answers nonsense, or too much, is refused);
    {"files": {path: (sha256, size)}, "links": [...], "other": [...]}."""
    if len(text) > MAX_LIST_CHARS:
        raise SandboxError("the sandbox listed too many files")
    try:
        data = json.loads(text)
    except ValueError:
        raise SandboxError("the sandbox's file listing wasn't readable") from None
    files, links, other = (data.get(k) if isinstance(data, dict) else None for k in ("files", "links", "other"))
    if not (isinstance(files, dict) and isinstance(links, list) and isinstance(other, list)
            and all(isinstance(k, str) and _is_entry(v) for k, v in files.items())
            and all(isinstance(p, str) for p in links + other)):
        raise SandboxError("the sandbox's file listing wasn't readable")
    if data.get("truncated"):
        raise SandboxError("the sandbox made too many files")
    return {"files": {k: (v[0], v[1]) for k, v in files.items()}, "links": links, "other": other}


def capped(cmd: str, limit: int) -> str:
    """`cmd` in the repo, passed as data (base64: it can't break out of the wrapper), its output in a file of
    which only `limit` bytes come back, then our own exit line (GPT review: a flood never reaches the worker)."""
    data = base64.b64encode(cmd.encode()).decode()
    return (f"cd {ROOT} && echo {data} | base64 -d > /tmp/.hagency-cmd && bash /tmp/.hagency-cmd > /tmp/.hagency-out "
            f"2>&1 < /dev/null; code=$?; head -c {int(limit)} /tmp/.hagency-out; printf '\\n{EXIT_MARK} %s' \"$code\"")


class Bounded:
    """Collects a command's output as it streams, keeping at most `limit` characters (the rest is dropped:
    the SDK keeps nothing itself, retain_output=False)."""
    def __init__(self, limit: int) -> None:
        self.parts, self.size, self.limit, self.over = [], 0, limit, False

    def add(self, text: str) -> None:
        if self.size + len(text) > self.limit:
            self.over = True
            text = text[:max(0, self.limit - self.size)]
        if not text:
            return                        # full: nothing more is kept, not even an empty part (GPT review)
        self.parts.append(text)
        self.size += len(text)

    def text(self) -> str:
        return "".join(self.parts)


def parse_capped(out: str) -> Result:
    body, _, last = out.rpartition("\n")
    mark, _, code = last.partition(" ")
    if mark != EXIT_MARK or not code.strip().lstrip("-").isdigit():
        raise SandboxError("the sandbox's command didn't end properly")
    return Result(int(code), body)


async def check_egress(box) -> None:
    """Refuse (SandboxError) unless a blocked host is plainly unreachable: a firewall that doesn't hold, or a
    probe that can't tell, fails closed."""
    try:
        probe = await box.run(PROBE_CMD, timeout=15)
    except asyncio.TimeoutError:
        return                    # it hung: unreachable, as it should be
    if probe.exit_code not in BLOCKED_EXITS:
        raise SandboxError("the sandbox's network check failed, so it was stopped for safety")


async def check_clean(box) -> None:
    """Refuse a sandbox whose environment holds anything but plain settings, or can't be read (ADR 0027)."""
    env = await box.run(ENV_CMD, timeout=15)
    names = [line.split("=", 1)[0] for line in env.stdout.splitlines() if "=" in line]
    odd = [n for n in names if CREDENTIAL_NAME_RE.search(n) or not SAFE_ENV_RE.match(n)]
    if env.exit_code != 0 or "PATH" not in names or odd:
        raise SandboxError("the sandbox's environment check failed, so it was stopped for safety")


class E2BSandboxes:
    """Makes E2B sandboxes from our pinned template (common to every build)."""
    def __init__(self, api_key: str, template: str, timeout_s: int) -> None:
        self.api_key, self.template, self.timeout_s = api_key, template, timeout_s

    async def create(self, kind: str, job_id: str) -> "E2BBox":
        from e2b import AsyncSandbox           # deploy/requirements-trial.txt (the worker only)
        network = ({"allow_out": EGRESS_ALLOW, "deny_out": lambda ctx: [ctx.all_traffic]} if kind == BUILD else None)
        try:
            sbx = await AsyncSandbox.create(template=self.template, timeout=self.timeout_s, envs={},
                                            metadata={**METADATA, "kind": kind, "job": job_id},
                                            allow_internet_access=kind == BUILD, network=network,
                                            cpu_count=VCPU, memory_mb=MEMORY_MB, api_key=self.api_key)
        except Exception as e:   # noqa: BLE001 — the SDK raises many kinds; the build is refused, the type logged
            raise SandboxError(f"the sandbox service refused a sandbox ({type(e).__name__})") from None
        return E2BBox(sbx)

    async def reap(self, now: float | None = None, older_than_s: int = REAP_AFTER_S) -> int:
        """Kill every sandbox of ours older than `older_than_s` (0: all of them: the kill switch)."""
        from e2b import AsyncSandbox, SandboxQuery
        now, killed = time.time() if now is None else now, 0
        pages = AsyncSandbox.list(query=SandboxQuery(metadata=METADATA), api_key=self.api_key)
        while pages.has_next:
            for info in await pages.next_items():
                if is_stale(info.started_at.timestamp(), now, older_than_s):
                    killed += bool(await AsyncSandbox.kill(info.sandbox_id, api_key=self.api_key))
        return killed


class E2BBox:
    def __init__(self, sbx) -> None:
        self.sbx, self.id = sbx, sbx.sandbox_id

    async def run(self, cmd: str, timeout: float = 120, cwd: str | None = None, limit: int = MAX_OUTPUT) -> Result:
        """At most `limit` characters of output are ever held (SandboxError past it); the model's commands go
        through run_capped, which keeps them far below it."""
        from e2b import CommandExitException, TimeoutException
        out, err = Bounded(limit), Bounded(limit)
        try:
            r = await self.sbx.commands.run(cmd, timeout=timeout, cwd=cwd, envs={}, on_stdout=out.add,
                                            on_stderr=err.add, retain_output=False)
            code = r.exit_code
        except CommandExitException as e:      # a non-zero exit is an answer, not a failure of ours
            code = e.exit_code
        except TimeoutException:
            raise asyncio.TimeoutError() from None
        if out.over:
            raise SandboxError("the sandbox answered with too much output")
        return Result(code, out.text(), err.text())

    async def run_capped(self, cmd: str, limit: int, timeout: float = 120) -> Result:
        return parse_capped((await self.run(capped(cmd, limit), timeout=timeout)).stdout)

    async def read_capped(self, path: str, limit: int) -> bytes | None:
        """At most `limit` bytes of a file (the repo's, relative, or an absolute path); None: no such file."""
        r = await self.run_capped(f"head -c {int(limit)} -- {shlex.quote(path)}", limit, timeout=30)
        return r.stdout.encode("utf-8", "surrogateescape") if r.exit_code == 0 else None

    async def write_files(self, files: dict[str, bytes]) -> None:
        from e2b.sandbox.filesystem.filesystem import WriteEntry
        entries = [WriteEntry(path=f"{ROOT}/{p}", data=d) for p, d in files.items()]
        for start in range(0, len(entries), 100):
            await self.sbx.files.write_files(entries[start:start + 100])

    async def read(self, path: str, limit: int = MAX_READ) -> bytes:
        """A file of the repo (relative), or an absolute path, streamed: SandboxError once it passes `limit`
        bytes (GPT review: a file can grow after it was listed)."""
        stream = await self.sbx.files.read(path if path.startswith("/") else f"{ROOT}/{path}", format="stream")
        data = bytearray()
        try:
            async for chunk in stream:
                data += chunk
                if len(data) > limit:
                    raise SandboxError("a file in the sandbox is too big")
        finally:
            await stream.aclose()
        return bytes(data)

    async def listing(self) -> dict:
        return parse_listing((await self.run(LIST_CMD, timeout=60, limit=MAX_LIST_CHARS)).stdout)

    async def kill(self) -> None:
        try:
            await self.sbx.kill()
        except Exception:   # noqa: BLE001 — already gone; E2B's own timeout and our reaper still end it
            pass
