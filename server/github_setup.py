"""One-time GitHub setup for the loop: check every agent identity, and apply the merge-gate ruleset.

  python github_setup.py check [--repo owner/name] [agent ...]   # default agents: anna-agent betty-agent
  python github_setup.py ruleset [--repo owner/name]             # as the repo owner (your normal `gh` login)
  python github_setup.py pages [--repo owner/name]               # publish the default branch (GitHub Pages)

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
from common.github import LABEL, REPO, GitHubError, run, run_json

DEFAULT_AGENTS = ("anna-agent", "betty-agent")
RULESET_FILE = github.ROOT / "github-ruleset.json"
RULESET_NAME = json.loads(RULESET_FILE.read_text())["name"]


def evaluate(identities: dict[str, dict], main_rules: list[dict], repo: str = REPO, branch: str = "main",
             detail: bool = True) -> list[str]:
    """identities: agent -> {"login", "push"} (or {"error"}); main_rules: GET rules/branches/<branch>.
    detail=False leaves out raw GitHub CLI errors (the console shows this to room moderators)."""
    problems = []
    for agent, ident in identities.items():
        if "error" in ident:
            problems.append(f"{agent}: not signed in to GitHub" + (f" ({ident['error']})" if detail else ""))
        elif not ident.get("push"):
            problems.append(f"{agent} ({ident['login']}): no Write access to {repo} — add it as a collaborator")
    logins = [i["login"] for i in identities.values() if "login" in i]
    if len(set(logins)) != len(logins):
        problems.append(f"agents share a GitHub account ({logins}); each needs its own, or nobody can approve")
    pr_rules = [r for r in main_rules if r.get("type") == "pull_request"]
    if not any(r.get("parameters", {}).get("required_approving_review_count", 0) >= 1 for r in pr_rules):
        problems.append(f"{branch} on {repo} doesn't require an approving review — run `github_setup.py ruleset --repo {repo}`")
    return problems


async def identity(agent: str, repo: str = REPO) -> dict:
    try:
        login = (await run_json(["api", "user"], agent))["login"]
        perms = (await run_json(["api", f"repos/{repo}"], agent)).get("permissions", {})
        return {"login": login, "push": bool(perms.get("push"))}
    except GitHubError as e:
        return {"error": str(e)[:200]}   # reported as a problem by evaluate()


async def readiness(repo: str, agents: list[str], detail: bool = True) -> tuple[dict, list[str]]:
    """(identities, problems) for the loop on `repo` (its default branch): read-only, nothing changes."""
    identities = {a: await identity(a, repo) for a in agents}
    try:
        branch = (await run_json(["api", f"repos/{repo}"])).get("default_branch") or "main"
        rules = await run_json(["api", f"repos/{repo}/rules/branches/{urllib.parse.quote(branch, safe='')}"])
        labels = {lb["name"] for lb in await run_json(["label", "list", "-R", repo, "--json", "name", "--limit", "200"])}
    except github_app.NotInstalled:
        return identities, [f"the Hagency Rooms GitHub App isn't installed on {repo}: its owner installs it "
                            f"(read-only, one click) at {github_app.install_url()}"]
    except GitHubError as e:
        return identities, [f"can't read {repo}" + (f": {str(e)[:160]}" if detail else "")]
    problems = evaluate(identities, rules if isinstance(rules, list) else [], repo, branch, detail)
    if LABEL not in labels:
        problems.append(f"no '{LABEL}' label on {repo} — run `github_setup.py ruleset --repo {repo}`")
    return identities, problems


async def check(agents: list[str], repo: str = REPO) -> int:
    identities, problems = await readiness(repo, agents)
    for a, i in identities.items():
        print(f"{a:12} {i.get('login', '-'):20} {'write' if i.get('push') else 'NO WRITE' if 'login' in i else i['error']}")
    print("\n".join(f"✗ {p}" for p in problems) or f"✓ {repo} ready for the loop")
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
