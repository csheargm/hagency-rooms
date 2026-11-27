"""Shared pieces for trial builds' tests (ADR 0027): a demo template, a scripted builder model, the
sandboxes' fake commands and the worker's dependencies, all in memory. No tests of its own."""
import os
import tempfile
from pathlib import Path
from unittest import mock

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from common import model_slot
from common import sandbox
from common import sandbox_fake
from server import trial_budget
from server import trial_build
from server import trial_build_config
from server import trial_github
from server import trial_jobs
from server import trial_pool
from tests.fake_trial_github import FakeTrialGitHub, tarball

REPO = "hagency-trials/demo-1"
tarball_of = tarball                  # the template as GitHub serves it (tests use kit.tarball_of)
BROKEN = "def add(a, b):\n    return a - b\n"
FIXED = "def add(a, b):\n    return a + b\n"
TEMPLATE = {"src/calc.py": BROKEN, "tests/test_calc.py": "from src.calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n",
            "README.md": "A demo repo.\n"}
FREE = (3, 10)                        # the free tier's builds: a day, ever (trial_tiers.TIERS)
CAPS = {"TRIAL_BUILDS_PER_DAY": 20, "TRIAL_BUILD_QUEUE": 5,
        "TRIAL_MIN_ACCOUNT_DAYS": 30, "demo_users": []}
_KEY: Path | None = None


def key_file() -> Path:
    """An RSA key (mode 600) for the Trial App's JWT; made once per test run."""
    global _KEY
    if _KEY is None:
        pem = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        _KEY = Path(tempfile.mkdtemp()) / "trial-app.pem"
        _KEY.write_bytes(pem)
        os.chmod(_KEY, 0o600)
    return _KEY


def configured_env(folder: Path, **extra) -> mock._patch:
    """trial.env as the owner fills it, pointing at `folder` for the shared state."""
    return mock.patch.dict(os.environ, {"E2B_API_KEY": "e2b_" + "k" * 20, "TRIAL_APP_ID": "12345", "TRIAL_ORG": "hagency-trials",
                                        "TRIAL_REPOS": "demo-1", "OLLAMA_API_KEY": "o" * 20, "TRIAL_SHARED_DIR": str(folder),
                                        "TRIAL_APP_PRIVATE_KEY_PATH": str(key_file()), **extra})


def tool(name: str, **args) -> dict:
    return {"content": "", "tool_calls": [{"function": {"name": name, "arguments": args}}]}


def scripted(*messages):
    """A builder model that answers these messages in turn (then finishes); it records every chat."""
    asked = []

    async def ask(chat):
        asked.append([dict(m) for m in chat])
        i = len(asked) - 1
        return (messages[i] if i < len(messages) else tool("finish", summary="Done.")), 1000
    ask.asked = asked
    return ask


FIX = (tool("read_file", path="src/calc.py"), tool("write_file", path="src/calc.py", content=FIXED),
       tool("run", command="python -m pytest -q"), tool("finish", summary="add() now adds."))


def junit(passed: int = 1, failed: int = 0, skipped: int = 0) -> str:
    """pytest's JUnit report, as --junitxml writes it."""
    return (f'<?xml version="1.0" encoding="utf-8"?><testsuites name="pytest tests"><testsuite name="pytest" errors="0" '
            f'failures="{failed}" skipped="{skipped}" tests="{passed + failed + skipped}" time="0.01"></testsuite></testsuites>')


def on_run(box, cmd):
    """The template's commands: the fixed test command passes only once add() adds."""
    if box.kind == sandbox.TEST and cmd == f"{trial_build_config.test_command()} --junitxml={trial_build.REPORT}":
        ok = box.files.get("src/calc.py") == FIXED.encode()
        box.outside[trial_build.REPORT] = junit(passed=int(ok), failed=int(not ok)).encode()
        return sandbox.Result(0 if ok else 1, "1 passed" if ok else "1 failed")
    return sandbox.Result(0, "1 passed (the build sandbox says so; it is never believed)")


async def no_balance():
    return None


async def review_ask(chat):
    return "- The change makes add() add. Check negative numbers too."


def deps(folder: Path, gh: FakeTrialGitHub | None = None, sandboxes=None, ask_build=None, slot=None) -> trial_build.Deps:
    gh = gh or FakeTrialGitHub({REPO: TEMPLATE})
    client = trial_github.TrialGitHub("12345", str(key_file()), [REPO], call=gh.call, fetch=gh.fetch)
    d = trial_build.Deps(gh=client, sandboxes=sandboxes or sandbox_fake.FakeSandboxes(on_run), pool=trial_pool.Pool(folder / "pool.json", [REPO], 7),
                         jobs=trial_jobs.Jobs(folder), slot=slot or model_slot.Slot(model_slot.lock_path(folder), model_slot.BUILD),
                         budget=trial_budget.Budget(folder / "build-budget.json", fetch=no_balance),
                         ask_build=ask_build or scripted(*FIX),
                         ask_review=review_ask)
    d.fake = gh
    d.jobs.beat(CAPS)                     # the worker is up (enqueue checks it)
    return d
