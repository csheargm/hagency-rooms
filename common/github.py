"""GitHub access for agents: every call runs the `gh` CLI (no shell) under that agent's own
identity, i.e. its own GH_CONFIG_DIR in .gh/<agent>/ (git-ignored; tokens stay in the keyring).

GitHub — not this code — is the merge gate: a PR is merge-ready only when GitHub itself
reports it MERGEABLE + CLEAN + APPROVED, which needs the repo's branch protection / ruleset.
Without required reviews GitHub never reports APPROVED, so the loop fails closed.
"""
import asyncio
import json
import os
import re
from pathlib import Path

from common import lifecycle
from common import paths

ROOT = paths.ROOT
# The default repo, only for seeding a room that has none yet; each room keeps its own (room_state).
REPO = os.environ.get("LOOP_REPO", "your-name/hagency-demo-sandbox")
LABEL = "agent-ok"
REPO_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$")   # owner/name
LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")                        # a GitHub username
# Which repos rooms may choose (owner decision, batch 12–14). With the GitHub App (the public server):
# any repo, since a room still needs the App on it and the asker's own write access there
# (pm_settings.choose_repo). The operator may restrict the choice with HAGENCY_REPOS="a/b c/d".
# Without the App, @pm reads with the host's own `gh` login, so a free choice would make a room a
# window into any repo that login can see: then only the listed repos (and LOOP_REPO).
OPERATOR_REPOS = frozenset(os.environ.get("HAGENCY_REPOS", "").replace(",", " ").split())
ALLOWED_REPOS = frozenset({REPO, *OPERATOR_REPOS})


def any_repo() -> bool:
    """No operator list, and the App reads GitHub: any repo may be chosen."""
    from common import github_app                         # here: it imports this module
    return not OPERATOR_REPOS and github_app.configured()


def allowed_repo(name) -> str | None:
    """`name` if this server may work on it: the operator's spelling when listed (GitHub names ignore
    case), else as given when any repo may be chosen (any_repo)."""
    if not isinstance(name, str) or not REPO_RE.match(name):
        return None
    return _listed(name) or (name if any_repo() else None)


def bound_repo(name) -> str | None:
    """`name` as a room's binding names it (room_state.repo_in: only @pm's own state is read): the
    operator's spelling when listed; unlisted only while there is no operator list, so a list set later
    unbinds other repos, and an agent's process (no App settings of its own) reads what @pm chose."""
    if not isinstance(name, str) or not REPO_RE.match(name):
        return None
    return _listed(name) or (None if OPERATOR_REPOS else name)


def _listed(name: str) -> str | None:
    return next((r for r in sorted(ALLOWED_REPOS) if REPO_RE.match(r) and r.lower() == name.lower()), None)
GH_BIN = os.environ.get("GH_BIN", "gh")          # tests point this at tests/fake_gh.py
IDENTITY_ROOT = Path(os.environ.get("GH_IDENTITY_ROOT", ROOT / ".gh"))
CMD_TIMEOUT_S = 120

# Event content keys shared by loop.py and the agent adapters.
TASK_KEY = "xyz.hagencylabs.task"
RESULT_KEY = "xyz.hagencylabs.result"
LEVEL_EVENT = "xyz.hagencylabs.loop"
STATUS_EVENT = "xyz.hagencylabs.status"   # @pm's live project status (the board text), for agents to read
LEVELS = ("off", "assist", "autonomous")
PM_BOT = "pm"                    # Matrix localpart of the project coordinator, @pm (loop.py)


# "PR #12" / "#12" in loop lines → links to the loop repo (markdown; not inside words or URLs).
REF_RE = re.compile(r"(?<![\w/\[])(PR )?#(\d+)\b")


def linkify(text: str, repo: str | None = None) -> str:
    """Markdown with each issue/PR reference linked to github.com/<repo> (no links without a repo)."""
    if not repo or not REPO_RE.match(repo):
        return text

    def link(m: re.Match) -> str:
        kind = "pull" if m.group(1) else "issues"
        return f"[{m.group(0)}](https://github.com/{repo}/{kind}/{m.group(2)})"
    return REF_RE.sub(link, text)


class GitHubError(RuntimeError):
    pass


# Live test 2026-11-23: a newcomer's agent failed with "(.gh/<agent>/ not signed in)", which said nothing
# they could act on; "retry" can't help until it is signed in (docs/GITHUB_SETUP.md, step 1).
NO_IDENTITY = ("{agent} isn't signed in to GitHub yet, so it can't open issues or pull requests. On its computer, "
               "in the .hagency/app folder in its owner's home folder, its owner runs: GH_CONFIG_DIR=.gh/{agent} "
               "gh auth login --web --insecure-storage (needs the GitHub CLI, gh), then says \"@pm retry\" with the issue number")
# (rooms never show local paths: redact.room_safe cuts them, so the folder is said in words)


def identity_dir(agent: str) -> Path | None:
    """The agent's gh config dir, or None if the person hasn't signed that identity in."""
    d = IDENTITY_ROOT / agent
    return d if (d / "hosts.yml").exists() else None


# Never let ambient overrides pick a different account/host than the agent's own config dir.
SCRUBBED_ENV = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN", "GH_HOST")
# Host-side git must not run user/system config (filters, fsmonitor, hooks) on agent-edited trees.
ISOLATED_GIT_ENV = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"}


async def run(args: list[str], agent: str | None = None, cwd: Path | None = None,
              program: str | None = None, extra_env: dict | None = None) -> str:
    """Run gh (or git) as `agent`; agent=None uses the ambient login (read-only use only)."""
    env = dict(os.environ)
    if agent is not None:
        d = identity_dir(agent)
        if d is None:
            raise GitHubError(NO_IDENTITY.format(agent=agent))
        env["GH_CONFIG_DIR"] = str(d)
        for k in SCRUBBED_ENV:
            env.pop(k, None)
    env.update(extra_env or {})
    argv = [program or GH_BIN, *args]
    proc = await asyncio.create_subprocess_exec(*argv, cwd=cwd, env=env, stdin=asyncio.subprocess.DEVNULL,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await lifecycle.communicate(proc, None, CMD_TIMEOUT_S)
    except asyncio.TimeoutError as e:
        raise GitHubError(f"{argv[0]} {args[0]} timed out") from e
    if proc.returncode != 0:
        msg = (err.decode(errors="replace").strip() or out.decode(errors="replace").strip())[-300:]
        raise GitHubError(f"{Path(argv[0]).name} {' '.join(args[:2])} failed: {msg}")
    return out.decode(errors="replace")


async def run_json(args: list[str], agent: str | None = None) -> object:
    """gh's JSON. @pm's own reads (agent=None) go through the GitHub App when it is configured (the
    public server has no gh login); else, and for every agent, the gh CLI as that identity."""
    if agent is None:
        from common import github_app                       # imported here: both modules import this one
        from common import github_app_reads
        if github_app.configured():
            return await github_app_reads.run_json(args)
    return json.loads(await run(args, agent))


async def git(args: list[str], agent: str, cwd: Path) -> str:
    """git with gh as the credential helper, so pushes use the agent's identity."""
    helper = f"!{GH_BIN} auth git-credential"
    safe = ["-c", "credential.helper=", "-c", f"credential.helper={helper}",
            "-c", f"core.hooksPath={os.devnull}", "-c", "core.fsmonitor=false"]
    return await run([*safe, *args], agent, cwd, program="git", extra_env=ISOLATED_GIT_ENV)


async def merge_ready(pr: int, agent: str | None = None, repo: str = REPO) -> tuple[bool, dict]:
    """GitHub's own verdict on whether PR can merge now (needs required-review protection)."""
    info = await run_json(["pr", "view", str(pr), "-R", repo, "--json",
                           "state,mergeable,mergeStateStatus,reviewDecision,headRefOid"], agent)
    ok = (info.get("state") == "OPEN" and info.get("mergeable") == "MERGEABLE"
          and info.get("mergeStateStatus") == "CLEAN" and info.get("reviewDecision") == "APPROVED")
    return ok, info
