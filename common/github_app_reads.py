"""@pm's `gh` reads, answered through the GitHub App (github_app.py) with the same JSON `gh` gives.

github.run_json sends @pm's own reads here (agent=None) when the App is configured. Only the read
shapes @pm uses are known; anything else (any write) is refused: the App is read-only, and agents'
writes stay on their own identities. Every read names its repo, and gets a token for that repo only.
"""
import re
import urllib.parse

from common import github
from common import github_app

ISSUE_FIELDS = {"number", "title", "state", "body", "labels"}
PR_FIELDS = {"number", "title", "state", "mergeable", "mergeStateStatus", "reviewDecision",
             "headRefOid", "headRefName", "isCrossRepository"}     # GraphQL's names are gh's names
API_PATH_RE = re.compile(r"^repos/([A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100})(/[A-Za-z0-9._%/-]*)?$")
PAGE = 100                        # GitHub's largest page
PR_QUERY = ("query($owner: String!, $name: String!, $number: Int!) "
            "{ repository(owner: $owner, name: $name) { pullRequest(number: $number) { %s } } }")


def _opt(args: list[str], name: str, default: str | None = None) -> str | None:
    return args[args.index(name) + 1] if name in args and args.index(name) + 1 < len(args) else default


def _repo(args: list[str]) -> str:
    repo = _opt(args, "-R")
    if not repo or not github.REPO_RE.match(repo):
        raise github.GitHubError("a read needs its repo (-R owner/name)")
    return repo


def _fields(args: list[str], known: set[str]) -> list[str]:
    fields = (_opt(args, "--json") or "").split(",")
    unknown = [f for f in fields if f not in known]
    if unknown:
        raise github.GitHubError(f"@pm can't read {','.join(unknown)[:60]} through the GitHub App")
    return fields


def _number(text: str | None) -> int:
    if not text or not text.isdigit():
        raise github.GitHubError("an issue or PR number is a plain number")
    return int(text)


def _limit(args: list[str]) -> int:
    return max(1, min(_number(_opt(args, "--limit", "30")), 1000))


def _issue(raw: dict, fields: list[str]) -> dict:
    shaped = {"number": raw.get("number"), "title": raw.get("title"), "body": raw.get("body") or "",
              "state": str(raw.get("state", "")).upper(),
              "labels": [{"name": lb.get("name")} for lb in raw.get("labels") or [] if isinstance(lb, dict)]}
    return {f: shaped[f] for f in fields}


def _is_dict(item) -> bool:
    return isinstance(item, dict)


async def _pages(repo: str, path: str, query: dict, limit: int, keep=_is_dict) -> list:
    """Up to `limit` items that `keep` accepts, page by page (the limit counts kept items only)."""
    out: list = []
    page = 1
    while len(out) < limit:
        q = urllib.parse.urlencode({**query, "per_page": PAGE, "page": page})
        batch = await github_app.get(repo, f"/repos/{repo}{path}?{q}")
        if not isinstance(batch, list):
            raise github.GitHubError(f"GitHub's answer for {path} wasn't a list")
        out += [item for item in batch if keep(item)]
        if len(batch) < PAGE:
            break
        page += 1
    return out[:limit]


def _is_issue(raw) -> bool:
    return isinstance(raw, dict) and "pull_request" not in raw     # REST lists PRs as issues too; gh doesn't


async def issue_list(args: list[str]) -> list:
    repo, fields, limit = _repo(args), _fields(args, ISSUE_FIELDS), _limit(args)
    query = {"state": (_opt(args, "--state") or "open").lower()}
    if _opt(args, "--label"):
        query["labels"] = _opt(args, "--label")
    return [_issue(raw, fields) for raw in await _pages(repo, "/issues", query, limit, _is_issue)]


async def issue_view(args: list[str]) -> dict:
    repo, fields, n = _repo(args), _fields(args, ISSUE_FIELDS), _number(args[2] if len(args) > 2 else None)
    raw = await github_app.get(repo, f"/repos/{repo}/issues/{n}")
    if not isinstance(raw, dict) or "pull_request" in raw:
        raise github.GitHubError(f"#{n} on {repo} isn't an issue")
    return _issue(raw, fields)


async def pr_view(args: list[str]) -> dict:
    repo, fields, n = _repo(args), _fields(args, PR_FIELDS), _number(args[2] if len(args) > 2 else None)
    owner, name = repo.split("/", 1)
    data = await github_app.graphql(repo, PR_QUERY % " ".join(fields), {"owner": owner, "name": name, "number": n})
    pr = (data.get("repository") or {}).get("pullRequest")
    if not isinstance(pr, dict):
        raise github.GitHubError(f"no PR #{n} on {repo}")
    return {f: "" if pr.get(f) is None else pr.get(f) for f in fields}   # gh prints null as ""


PR_LIST_FIELDS = {"number", "title", "state", "headRefName", "isCrossRepository"}


def _pr_listed(raw: dict, repo: str, fields: list[str]) -> dict:
    head_repo = ((raw.get("head") or {}).get("repo") or {}).get("full_name")   # None: the fork was deleted
    shaped = {"number": raw.get("number"), "title": raw.get("title"), "state": str(raw.get("state", "")).upper(),
              "headRefName": (raw.get("head") or {}).get("ref") or "",
              "isCrossRepository": str(head_repo or "").lower() != repo.lower()}
    return {f: shaped[f] for f in fields}


async def pr_list(args: list[str]) -> list:
    """Open PRs only: the one list @pm makes (pm_paused.py, a paused issue's PR that was never recorded)."""
    repo, fields, limit = _repo(args), _fields(args, PR_LIST_FIELDS), _limit(args)
    if (_opt(args, "--state") or "open").lower() != "open":
        raise github.GitHubError("@pm lists only open PRs through the GitHub App")
    query = {"state": "open"}
    if _opt(args, "--head"):          # REST's head filter names the owner: a fork's same-named branch isn't it
        query["head"] = f"{repo.split('/', 1)[0]}:{_opt(args, '--head')}"
    return [_pr_listed(raw, repo, fields) for raw in await _pages(repo, "/pulls", query, limit)]


async def label_list(args: list[str]) -> list:
    repo, fields = _repo(args), _fields(args, {"name"})
    return [{f: lb.get(f) for f in fields} for lb in await _pages(repo, "/labels", {}, _limit(args))]


async def api_get(args: list[str]) -> object:
    m = API_PATH_RE.match(args[1]) if len(args) == 2 else None    # a plain GET: no -X, -f, --input
    segments = urllib.parse.unquote(args[1]).split("/") if m else []   # decoded first: %2F and %2e too
    if not m or any(seg in ("", ".", "..") for seg in segments):
        raise github.GitHubError("@pm reads only repo paths through the GitHub App")
    return await github_app.get(m.group(1), f"/{args[1]}")


READS = {("issue", "list"): issue_list, ("issue", "view"): issue_view, ("pr", "view"): pr_view,
         ("pr", "list"): pr_list, ("label", "list"): label_list}


async def run_json(args: list[str]) -> object:
    """What `gh <args>` would print as JSON, for the read shapes @pm uses."""
    if args[:1] == ["api"]:
        return await api_get(args)
    read = READS.get(tuple(args[:2]))
    if read is None:
        raise github.GitHubError(f"@pm can't run gh {' '.join(args[:2])[:40]} through the read-only GitHub App")
    return await read(args)
