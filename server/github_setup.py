"""One-time GitHub setup for the loop: check every agent identity, and apply the merge-gate ruleset.

  python bin/github_setup.py check [--repo owner/name] [agent ...]   # default agents: anna-agent betty-agent
  python bin/github_setup.py ruleset [--repo owner/name]             # as the repo owner (your normal `gh` login)
  python bin/github_setup.py pages [--repo owner/name]               # publish the default branch (GitHub Pages)

The repo defaults to LOOP_REPO. A room's "Check" in the Hagency console runs the same check
(read-only); changing the repo's settings stays with its owner, here.

Never reads or prints tokens: it only asks GitHub "who am I" and "what may I do" per identity.
See docs/GITHUB_SETUP.md for the full walkthrough.
"""
import asyncio
import json
import sys
import urllib.parse

from common import github
from common import github_app
from common import registry
from common import solo_review
from common.github import LABEL, REPO, GitHubError, run, run_json
from server import readiness_identity

DEFAULT_AGENTS = ("anna-agent", "betty-agent")
RULESET_FILE = github.ROOT / "docs" / "github-ruleset.json"
RULESET_NAME = json.loads(RULESET_FILE.read_text())["name"]


LABEL_STEPS = ("no '{label}' label on {repo} yet. On GitHub open {repo} → Issues → Labels → New label, name it "
               "{label}, then click Create label")
SOLO_FIX = "switch on Solo review below"


def _names(items) -> str:
    items = list(items)
    return ", ".join(items[:-1]) + f" and {items[-1]}" if len(items) > 1 else "".join(items)


def _by_login(identities: dict[str, dict]) -> dict[str, list[str]]:
    """GitHub account (as GitHub compares them: any case) -> the agents signed in as it."""
    found: dict[str, list[str]] = {}
    for agent, ident in identities.items():
        if "login" in ident:
            found.setdefault(ident["login"].lower(), []).append(agent)
    return found


def _identity_problem(agent: str, ident: dict, repo: str, detail: bool) -> str | None:
    if "problem" in ident:                    # from its own report (readiness_identity): already says what to do
        return ident["problem"]
    if "error" in ident:                      # gh here, as an agent the server runs: its folder said no
        return f"{agent}: not signed in to GitHub" + (f" ({ident['error']})" if detail else "")
    if ident.get("push") is None:
        return f"{agent} (@{ident['login']}): couldn't check its Write access to {repo} just now; try again"
    if not ident["push"]:
        return (f"{agent} (@{ident['login']}): no Write access to {repo}. The repo's owner adds @{ident['login']} on "
                f"GitHub: {repo} → Settings → Collaborators → Add people, with the Write role")
    return None


def approvals(main_rules: list[dict]) -> int:
    """How many approving reviews the branch's rules need (0: no review rule; ADR 0016: optional, but meetable)."""
    return max([r.get("parameters", {}).get("required_approving_review_count", 0) or 0
                for r in main_rules if r.get("type") == "pull_request"] or [0])


def _owner(agent: str) -> str:
    """Whose agent it is (registry); an unregistered one (the CLI's test agents) counts as its own person."""
    return registry.owner_of(agent) or agent


def _reviewers_short(identities: dict[str, dict], need: int) -> bool:
    """No checker here whose approval GitHub counts (an account never approves its own PR, and the loop's checker
    is another PERSON's agent: routing.checkers_for; GPT review: one owner's two accounts are solo review), or more
    approvals than the loop asks for: it has one checker approve (GPT review)."""
    if need != 1:
        return need > 1
    pairs = [(ident["login"].lower(), _owner(agent)) for agent, ident in identities.items() if "login" in ident]
    return not any(login != other and owner != theirs for login, owner in pairs for other, theirs in pairs)


def _review_problem(identities: dict[str, dict], need: int, repo: str, branch: str, solo: bool, bypass: bool) -> str | None:
    """Why the review rule can't be met here, or None. With Solo review on (ADR 0023) a repo admin merges past it,
    once the rule lets admins past (bypass: solo_review's "ok")."""
    if not _reviewers_short(identities, need):
        return None
    shared = [(login, agents) for login, agents in _by_login(identities).items() if len(agents) > 1]
    rule = f"{branch}'s review rule ({need} approving review{'s' * (need > 1)})"
    if not solo and need > 1:
        return f"{rule} can't be met (the loop has one checker approve each PR): {SOLO_FIX}"
    if not solo:
        why = (f"{_names(shared[0][1])} share the GitHub account @{shared[0][0]}" if shared
               else "only 1 GitHub account is here" if len(_by_login(identities)) < 2
               else "every agent here is one person's, and the loop has another person's agent check the work")
        return f"{why}, so {rule} can't be met (GitHub never lets an account approve its own PR): {SOLO_FIX}"
    not_admin = [login for login, agents in _by_login(identities).items() if identities[agents[0]].get("admin") is not True]
    if not_admin:
        return (f"Solo review is on, but only a repo admin may merge past {branch}'s review rule, and "
                f"{_names('@' + x for x in not_admin)} isn't an admin of {repo} (or that couldn't be checked)")
    if not bypass:
        return ("Solo review is on, but GitHub's review rule doesn't let repo admins merge past it yet (as far as "
                "Hagency knows). Say \"@pm allow solo review\" and click 👍 on my question to have it added, or: "
                + solo_review.BYPASS_STEPS.format(repo=repo).replace("**", ""))
    return None


def evaluate(identities: dict[str, dict], main_rules: list[dict], repo: str = REPO, branch: str = "main",
             detail: bool = True, solo: bool = False, bypass: bool = False) -> list[str]:
    """identities: agent -> {"login", "push", "admin"?} (None: unknown), {"error"} (gh here) or {"problem"} (its own
    report); main_rules: GET rules/branches/<branch>; solo: Solo review is on for this repo (ADR 0023); bypass:
    its rule lets repo admins merge past. detail=False leaves out raw GitHub CLI errors (the console shows this to
    room moderators)."""
    problems = [p for a, i in identities.items() if (p := _identity_problem(a, i, repo, detail))]
    if not problems and (p := _review_problem(identities, approvals(main_rules), repo, branch, solo, bypass)):
        problems.append(p)
    return problems


def verified(identities: dict[str, dict], main_rules: list[dict], repo: str, branch: str) -> list[str]:
    """What a passing check saw working, one line each."""
    lines = [f"{_names(agents)}: signed in to GitHub as @{login} (as {'each' if len(agents) > 1 else 'it'} reports), "
             f"and GitHub says @{login} can write to {repo}" for login, agents in _by_login(identities).items()]
    lines.append(f"the '{LABEL}' label is on {repo}")
    need = approvals(main_rules)
    if not need:
        lines.append(f"{branch} has no review rule, so the loop's own reviewer decides")
    elif _reviewers_short(identities, need):
        lines.append(f"{branch}'s review rule: Solo review is on, and GitHub lets the repo admin account merge past "
                     "it after the loop's own review")
    else:
        lines.append(f"{branch}'s review rule ({need} approving review{'s' * (need > 1)}) can be met: "
                     f"{len(_by_login(identities))} different GitHub accounts are here")
    return lines


async def identity(agent: str, repo: str = REPO) -> dict:
    """An agent this server runs itself (older local test agents): `gh` here, in its own folder."""
    try:
        login = (await run_json(["api", "user"], agent))["login"]
    except GitHubError as e:
        return {"error": str(e)[:200]}   # reported as a problem by evaluate()
    try:
        perms = (await run_json(["api", f"repos/{repo}"], agent)).get("permissions", {})
    except GitHubError:                  # signed in, the repo unreadable: unknown, never "not signed in" (GPT review)
        return {"login": login, "push": None, "admin": None}
    return {"login": login, "push": bool(perms.get("push")), "admin": bool(perms.get("admin"))}


async def agent_identity(agent: str, repo: str) -> dict:
    """An agent on a person's computer: its own report (readiness_identity, ADR 0021); else gh here."""
    found = readiness_identity.entry_of(agent)
    if found is not None and found["agent"] == registry.REMOTE:
        return await readiness_identity.reported(agent, found, repo)
    return await identity(agent, repo)


async def readiness(repo: str, agents: list[str], detail: bool = True,
                    solo: bool = False, bypass: bool = False) -> tuple[dict, list[str], list[str]]:
    """(identities, problems, what was verified: only when there are no problems) for the loop on `repo` (its
    default branch): read-only, nothing changes. solo/bypass: as evaluate()."""
    identities = {a: await agent_identity(a, repo) for a in agents}
    try:
        branch = (await run_json(["api", f"repos/{repo}"])).get("default_branch") or "main"
        rules = await run_json(["api", f"repos/{repo}/rules/branches/{urllib.parse.quote(branch, safe='')}"])
        labels = {lb["name"] for lb in await run_json(["label", "list", "-R", repo, "--json", "name", "--limit", "200"])}
    except github_app.NotInstalled:
        return identities, [f"the Hagency Rooms GitHub App isn't installed on {repo}: its owner installs it "
                            f"(read-only, one click) at {github_app.install_url()}"], []
    except GitHubError as e:
        return identities, [f"can't read {repo}" + (f": {str(e)[:160]}" if detail else "")], []
    rules = rules if isinstance(rules, list) else []
    problems = evaluate(identities, rules, repo, branch, detail, solo, bypass)
    if LABEL not in labels:                   # the App is read-only: its owner adds it on GitHub's own pages
        problems.append(LABEL_STEPS.format(label=LABEL, repo=repo))
    return identities, problems, [] if problems else verified(identities, rules, repo, branch)


async def check(agents: list[str], repo: str = REPO) -> int:
    identities, problems, seen = await readiness(repo, agents)
    for a, i in identities.items():
        said = "write" if i.get("push") else "NO WRITE" if "login" in i else i.get("error") or i.get("problem")
        print(f"{a:12} {i.get('login', '-'):20} {said}")
    print("\n".join(f"✗ {p}" for p in problems) or "\n".join([f"✓ {repo} ready for the loop", *seen]))
    return 1 if problems else 0


async def ensure_label(repo: str) -> None:
    """The label the loop works from; planning opens issues with it, so a new repo needs it."""
    await run(["label", "create", LABEL, "-R", repo, "--color", "0e8a16", "--force",
               "--description", "Agents may build this (Hagency loop)"])
    print(f"✓ label '{LABEL}' on {repo}")


async def apply_ruleset(repo: str = REPO) -> int:
    await ensure_label(repo)
    existing = await run_json(["api", f"repos/{repo}/rulesets"])
    if any(r.get("name") == RULESET_NAME for r in existing):
        print(f"✓ ruleset '{RULESET_NAME}' already on {repo}")
        return 0
    await run(["api", "-X", "POST", f"repos/{repo}/rulesets", "--input", str(RULESET_FILE)])
    print(f"✓ ruleset '{RULESET_NAME}' created on {repo}")
    return 0


async def enable_pages(repo: str = REPO) -> int:
    """A repo setting, so the owner turns it on (agents only change files, through reviewed PRs)."""
    try:
        site = await run_json(["api", f"repos/{repo}/pages"])
    except GitHubError:
        site = await run_json(["api", "-X", "POST", f"repos/{repo}/pages",
                               "-f", "source[branch]=main", "-f", "source[path]=/"])
    print(f"✓ GitHub Pages on for {repo}: {pages_url(site, repo)}")
    return 0


def pages_url(site: dict, repo: str) -> str:
    return site.get("html_url") or f"https://{repo.split('/')[0].lower()}.github.io/{repo.split('/')[1]}/"


def main(argv: list[str]) -> int:
    repo = REPO
    if "--repo" in argv:
        i = argv.index("--repo")
        repo, argv = (argv[i + 1] if i + 1 < len(argv) else ""), argv[:i] + argv[i + 2:]
        if not github.REPO_RE.match(repo):
            print("--repo takes owner/name")
            return 2
    if argv[:1] == ["check"]:
        return asyncio.run(check(argv[1:] or list(DEFAULT_AGENTS), repo))
    if argv == ["ruleset"]:
        return asyncio.run(apply_ruleset(repo))
    if argv == ["pages"]:
        return asyncio.run(enable_pages(repo))
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
