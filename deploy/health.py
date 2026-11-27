"""Is Hagency Rooms up? Stdlib only: runs in @pm's container and on the operator's computer.

  python deploy/health.py --internal MATRIX_DOMAIN SERVER_NAME [open|token]   # bootstrap.sh, inside Docker
  python3 deploy/health.py ROOMS_DOMAIN MATRIX_DOMAIN SERVER_NAME # deploy.sh, over HTTPS from outside

Checks Element (page and config), Synapse's client API, that sign-up needs an invite token or a
confirmed email (either mode of signup.py, never neither; bootstrap names the mode it configured,
and then exactly that mode must be live), @pm's
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


VERIFYING_STAGES = ("m.login.registration_token",   # invite-only sign-up
                    "m.login.email.identity")       # open sign-up with a confirmed email (signup.py)


SIGNUP_STAGE = {"token": VERIFYING_STAGES[0], "open": VERIFYING_STAGES[1]}


def verified_only(text: str, mode: str | None = None) -> bool:
    """Every registration flow needs an invite token or a confirmed email: no flow signs up unchecked.
    With a mode (signup.py's), every flow needs exactly that one: closing sign-up must have taken effect."""
    flows = json.loads(text).get("flows") or [{}]
    needed = (SIGNUP_STAGE[mode],) if mode else VERIFYING_STAGES
    if mode == "open" and not any(SIGNUP_STAGE["token"] not in f.get("stages", []) for f in flows):
        return False                          # open, yet every flow still wants a token (GPT review)
    return all(any(s in f.get("stages", []) for s in needed) for f in flows)


def checks(rooms: str, matrix: str, console: str, domain: str, server: str, public: bool,
           mode: str | None = None) -> list[tuple[str, object]]:
    """(name, check) pairs; each check returns True or raises."""
    pm = urllib.parse.quote(f"@pm:{server}")
    items = [
        ("Element loads", lambda: "<title>" in fetch(f"{rooms}/")[1]),
        ("Element points at this homeserver", lambda: json.loads(fetch(f"{rooms}/config.json")[1])
         ["default_server_config"]["m.homeserver"]["base_url"] == f"https://{domain}"),
        ("client API (/_matrix/client/versions)", lambda: "versions" in json.loads(fetch(f"{matrix}/_matrix/client/versions")[1])),
        (f"sign-up needs {'a confirmed email' if mode == 'open' else 'an invite token' if mode else 'an invite token or a confirmed email'}",
         lambda: verified_only(fetch(f"{matrix}/_matrix/client/v3/register", {})[1], mode)),
        ("@pm's account", lambda: json.loads(fetch(f"{matrix}/_matrix/client/v3/profile/{pm}")[1]).get("displayname")
         == "Product Manager"),
        ("@pm online: /connect.sh served", lambda: "bin/connect.py" in (t := fetch(f"{console}/connect.sh")[1])
         and "__HAGENCY_CONSOLE__" not in t),
        ("the Connect page and /connect.ps1 served", lambda: "connect/start.js" in fetch(f"{console}/connect/start")[1]
         and "bin\\connect_win.py" in (t := fetch(f"{console}/connect.ps1")[1]) and "__HAGENCY_CONSOLE__" not in t),
        ("Get your agent and Make an invite link served (ADR 0015)",
         lambda: "/connect/new/page.js" in fetch(f"{console}/connect/new")[1] and "/invite/new/page.js" in
         fetch(f"{console}/invite/new")[1] and "const SignIn" in fetch(f"{console}/auth/signin.js")[1]),
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
            ("Element's home page has the step ① and ③ buttons",
             lambda: all(f'<a href="{p}"' in fetch(f"{rooms}/hagency/home.html")[1] for p in ("/connect/new", "/invite/new"))),
            ("Element links Hagency's style fixes",
             lambda: 'href="/hagency/element-fixes.css"' in fetch(f"{rooms}/")[1]
             and ".mx_SSOButton_mini" in fetch(f"{rooms}/hagency/element-fixes.css")[1]),
            ("Element loads the Hagency panel button (ADR 0026)",
             lambda: 'src="/hagency/panel-dock.js"' in fetch(f"{rooms}/")[1]
             and (r := fetch(f"{rooms}/hagency/panel-dock.js"))[0] == 200 and "moveToContainer" in r[1]
             and "__PM_ID__" not in r[1]),                     # filled in (element_home.dock_script)
            ("the privacy policy served at /privacy", lambda: (r := fetch(f"{rooms}/privacy"))[0] == 200 and "<h1>Privacy</h1>" in r[1]),
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
    if len(argv) in (3, 4) and argv[0] == "--internal" and argv[3:4] in ([], ["open"], ["token"]):
        items = checks("http://element", "http://synapse:8008", "http://localhost:8650", *argv[1:3], public=False,
                       mode=(argv[3:4] or [None])[0])
    elif len(argv) == 3:
        items = checks(f"https://{argv[0]}", f"https://{argv[1]}", f"https://{argv[0]}", *argv[1:], public=True)
    else:
        print(__doc__)
        return 2
    return 0 if run(items) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
