"""Is Hagency Rooms up? Stdlib only: runs in @pm's container and on the operator's computer.

  python deploy/health.py --internal MATRIX_DOMAIN SERVER_NAME   # bootstrap.sh, inside the Docker network
  python3 deploy/health.py ROOMS_DOMAIN MATRIX_DOMAIN SERVER_NAME # deploy.sh, over HTTPS from outside

Checks Element (page and config), Synapse's client API, that sign-up needs an invite token, @pm's
account, and @pm's console serving /connect.sh, the Connect page and /connect.ps1 (it starts only
after @pm has signed in and synced).
From outside also .well-known, federation on 8448 and Element's home page (Caddy serves it). Prints ✓/✗ per check; exits 1 on any ✗.
"""
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT_S = 15


def fetch(url: str, body: dict | None = None) -> tuple[int, str]:
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", "User-Agent": "hagency-health"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            return r.status, r.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")


def token_only(text: str) -> bool:
    """Every registration flow needs m.login.registration_token (invite-only)."""
    flows = json.loads(text).get("flows") or [{}]
    return all("m.login.registration_token" in f.get("stages", []) for f in flows)


def checks(rooms: str, matrix: str, console: str, domain: str, server: str, public: bool) -> list[tuple[str, object]]:
    """(name, check) pairs; each check returns True or raises."""
    pm = urllib.parse.quote(f"@pm:{server}")
    items = [
        ("Element loads", lambda: "<title>" in fetch(f"{rooms}/")[1]),
        ("Element points at this homeserver", lambda: json.loads(fetch(f"{rooms}/config.json")[1])
         ["default_server_config"]["m.homeserver"]["base_url"] == f"https://{domain}"),
        ("client API (/_matrix/client/versions)", lambda: "versions" in json.loads(fetch(f"{matrix}/_matrix/client/versions")[1])),
        ("sign-up needs an invite token", lambda: token_only(fetch(f"{matrix}/_matrix/client/v3/register", {})[1])),
        ("@pm's account", lambda: json.loads(fetch(f"{matrix}/_matrix/client/v3/profile/{pm}")[1]).get("displayname")
         == "Product Manager"),
        ("@pm online: /connect.sh served", lambda: "bin/connect.py" in (t := fetch(f"{console}/connect.sh")[1])
         and "__HAGENCY_CONSOLE__" not in t),
        ("the Connect page and /connect.ps1 served", lambda: "connect/start.js" in fetch(f"{console}/connect/start")[1]
         and "bin\\connect_win.py" in (t := fetch(f"{console}/connect.ps1")[1]) and "__HAGENCY_CONSOLE__" not in t),
    ]
    if public:
        items += [
            ("/.well-known/matrix/server", lambda: json.loads(fetch(f"{matrix}/.well-known/matrix/server")[1])["m.server"]),
            ("/.well-known/matrix/client", lambda: json.loads(fetch(f"{matrix}/.well-known/matrix/client")[1])["m.homeserver"]),
            ("federation on 8448 (/_matrix/key/v2/server)",
             lambda: json.loads(fetch(f"{matrix}:8448/_matrix/key/v2/server")[1])["server_name"] == server),
            ("admin API not public", lambda: fetch(f"{matrix}/_synapse/admin/v1/server_version")[0] == 404),
            ("Element's home page links to @pm",
             lambda: f"#/user/@pm:{server}?action=chat" in fetch(f"{rooms}/hagency/home.html")[1]),
        ]
    return items


def run(items) -> bool:
    ok = True
    for name, check in items:
        try:
            passed = bool(check())
        except (OSError, ValueError, KeyError, TypeError) as e:       # unreachable, not JSON, wrong shape
            passed, name = False, f"{name} ({type(e).__name__}: {str(e)[:120]})"
        print(f"{'✓' if passed else '✗'} {name}", flush=True)
        ok = ok and passed
    return ok


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[0] == "--internal":
        items = checks("http://element", "http://synapse:8008", "http://localhost:8650", *argv[1:], public=False)
    elif len(argv) == 3:
        items = checks(f"https://{argv[0]}", f"https://{argv[1]}", f"https://{argv[0]}", *argv[1:], public=True)
    else:
        print(__doc__)
        return 2
    return 0 if run(items) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
