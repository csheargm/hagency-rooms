"""The deploy settings in .env, read as data (never sourced by a shell) and validated, so the shell,
Docker Compose and configure_synapse.py all see the same plain values.

  python3 envfile.py check            # validate .env (creating it, mode 600, from .env.example)
  python3 envfile.py get NAME         # print one validated, non-secret value
Secrets are filled here, never on a command line, and never printed.
"""
import base64
import os
import re
import secrets
import stat
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENV, EXAMPLE = HERE / ".env", HERE / ".env.example"
APP_KEY = HERE / "secrets" / "github-app.pem"    # mounted read-only into @pm (docker-compose.yml)
PEM_RE = re.compile(r"-----BEGIN (RSA )?PRIVATE KEY-----\s+([A-Za-z0-9+/=\s]+?)\s*-----END \1?PRIVATE KEY-----")
MAX_KEY_BYTES = 16384
MIN_KEY_DER = 600                                # a 2048-bit RSA key is ~1200 bytes of DER
DER_SEQUENCE = 0x30
REPO = r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}"   # owner/name, as github.REPO_RE
DOMAIN = r"(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}"
RULES = {
    "ROOMS_DOMAIN": DOMAIN, "MATRIX_DOMAIN": DOMAIN, "SERVER_NAME": DOMAIN, "ADMIN_DOMAIN": DOMAIN,
    "POSTGRES_PASSWORD": r"[0-9a-f]{64}",
    "OPERATOR": r"[a-z0-9._=-]{1,64}",       # the account guard.py warns (a localpart)
    "PM_UID": r"[1-9][0-9]{2,5}",            # @pm's container runs as the deploy user (owns ~/hagency/pm)
    "ALLOW_REMOTE": r"[01]",                 # people from other servers may get an agent (default 1)
    "REMOTE_PER_HOUR": r"[0-9]{1,4}",        # how many of their agents per hour (default 10)
    "GITHUB_APP_ID": r"[1-9][0-9]{0,11}",         # @pm's GitHub App (docs/GITHUB_APP.md); empty: no GitHub reads
    "GITHUB_APP_SLUG": r"[a-z0-9][a-z0-9-]{0,62}",   # its github.com/apps/<slug> name, for install links
    "GITHUB_APP_CLIENT_ID": r"Iv[A-Za-z0-9.]{6,62}",  # its Client ID (public, not the secret): Connect GitHub
    "HAGENCY_REPOS": rf"{REPO}(,{REPO}){{0,49}}",   # optional: only these repos (empty: any; github.allowed_repo)
    # Outgoing email for sign-up and password reset (signup.py, ADR 0013); the owner fills these.
    "SMTP_HOST": DOMAIN, "SMTP_PORT": r"25|465|587|2525",
    "SMTP_USER": r"[A-Za-z0-9._%+@=-]{1,254}",
    "SMTP_PASS": r"[A-Za-z0-9!%&()*+,./:;<=>?@\[\]^_{|}~#-]{8,256}",   # no quotes, $, backslashes or spaces (.env rules)
    "SMTP_FROM": rf"[A-Za-z0-9._%+-]{{1,64}}@{DOMAIN}",               # the address only; signup.py adds the name
    "HAGENCY_OPEN_SIGNUP": r"[01]",            # 1: open sign-up once SMTP is set; 0: invite tokens only
    # Sign-in with Google / GitHub (sso_login.py): each on once both of its keys are set.
    "HAGENCY_GOOGLE_CLIENT_ID": r"[0-9]{6,30}-[a-z0-9]{8,64}\.apps\.googleusercontent\.com",
    "HAGENCY_GOOGLE_CLIENT_SECRET": r"[A-Za-z0-9_-]{16,128}",
    "HAGENCY_GITHUB_CLIENT_ID": r"[A-Za-z0-9.]{16,40}",
    "HAGENCY_GITHUB_CLIENT_SECRET": r"[A-Za-z0-9_]{20,128}",
}
# Sign-up and sign-in settings: a bad value turns that feature off (signup.py and sso_login.py check them
# against RULES and bootstrap prints which), instead of stopping the deploy, so a typo made while
# closing sign-up (HAGENCY_OPEN_SIGNUP=0) can't leave the old, open config running (GPT review).
FAIL_CLOSED = {"SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS", "SMTP_FROM", "HAGENCY_OPEN_SIGNUP",
               "HAGENCY_GOOGLE_CLIENT_ID", "HAGENCY_GOOGLE_CLIENT_SECRET",
               "HAGENCY_GITHUB_CLIENT_ID", "HAGENCY_GITHUB_CLIENT_SECRET"}
SECRETS = {"POSTGRES_PASSWORD"}               # filled with a random value when empty
PRIVATE = SECRETS | {"SMTP_USER", "SMTP_PASS",   # never printed (`get` refuses them)
                     "HAGENCY_GOOGLE_CLIENT_SECRET", "HAGENCY_GITHUB_CLIENT_SECRET"}
OPTIONAL = {"OPERATOR", "ALLOW_REMOTE", "REMOTE_PER_HOUR",     # OPERATOR: empty until the operator has an account
            "GITHUB_APP_ID", "GITHUB_APP_SLUG", "GITHUB_APP_CLIENT_ID",   # the GitHub App: empty until
            "HAGENCY_REPOS",                                               # it is registered
            "SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS", "SMTP_FROM",   # empty: sign-up by invite only
            "HAGENCY_OPEN_SIGNUP", "HAGENCY_GOOGLE_CLIENT_ID", "HAGENCY_GOOGLE_CLIENT_SECRET",
            "HAGENCY_GITHUB_CLIENT_ID", "HAGENCY_GITHUB_CLIENT_SECRET"}


def read() -> dict:
    values = {}
    for line in ENV.read_text().splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            key, sep, value = line.partition("=")
            if not sep:
                raise SystemExit(f"✗ .env: not NAME=value: {key.strip()[:40]}")
            values[key.strip()] = value.strip()
    return values


def ensure() -> dict:
    """Create .env (mode 600 from the start) and fill empty secrets; refuse a readable .env."""
    if not ENV.exists():
        fd = os.open(ENV, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(EXAMPLE.read_text())
    if ENV.stat().st_mode & 0o077:
        raise SystemExit("✗ .env is readable by others: run `chmod 600 .env`")
    values = read()
    for line in EXAMPLE.read_text().splitlines():  # settings added since this .env was made: their defaults
        key, sep, default = line.partition("=")
        if sep and not line.startswith("#") and key.strip() not in values:
            values[key.strip()] = default.strip()
    values.setdefault("PM_UID", str(os.getuid()))   # the user running bootstrap.sh (ubuntu: 1001 on Oracle)
    for key in SECRETS:
        if not values.get(key):
            values[key] = secrets.token_hex(32)
    lines = [ln for ln in EXAMPLE.read_text().splitlines() if ln.startswith("#") or "=" not in ln]
    body = "\n".join(lines) + "\n" + "".join(f"{k}={v}\n" for k, v in values.items())
    tmp = ENV.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(body)
    os.replace(tmp, ENV)
    return values


def validate(values: dict) -> list[str]:
    problems = [f"{k} is missing" for k in RULES if not values.get(k) and k not in OPTIONAL]
    problems += [f"{k} doesn't look right" for k, rx in RULES.items()
                 if values.get(k) and k not in FAIL_CLOSED and not re.fullmatch(rx, values[k])]
    return problems


def app_key_problems(values: dict) -> list[str]:
    """The GitHub App's key, checked by `check` (deploy.sh runs it before @pm stops; docs/GITHUB_APP.md):
    a plain file of the deploy user's (PM_UID; @pm runs as it), mode 600, holding a PEM private key
    (its body base64 DER). Opened once, without following links, and checked on that open file.
    Never prints its text."""
    if not values.get("GITHUB_APP_ID"):
        return []
    want = "GITHUB_APP_ID is set: put the App's private key in deploy/secrets/github-app.pem (yours, mode 600)"
    try:
        fd = os.open(APP_KEY, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)   # a FIFO never blocks the check
    except OSError:
        return [want]
    with os.fdopen(fd, "rb") as f:
        st = os.fstat(f.fileno())
        mine = str(st.st_uid) == (values.get("PM_UID") or str(os.getuid()))
        if not stat.S_ISREG(st.st_mode) or not mine or stat.S_IMODE(st.st_mode) != 0o600:
            return [want]
        raw = f.read(MAX_KEY_BYTES + 1)
    if len(raw) > MAX_KEY_BYTES:
        return ["deploy/secrets/github-app.pem isn't a PEM private key"]
    text = raw.decode(errors="replace")
    m = PEM_RE.fullmatch(text.strip())
    try:
        der = base64.b64decode("".join(m.group(2).split()), validate=True) if m else b""
    except ValueError:
        der = b""
    if not is_der_sequence(der):
        return ["deploy/secrets/github-app.pem isn't a PEM private key"]
    return []


def is_der_sequence(der: bytes) -> bool:
    """One whole DER SEQUENCE of key size (what a PKCS#1 or PKCS#8 key is). The host's python3 has no
    `cryptography`, so @pm's first JWT is the full check; this catches a truncated or wrong file."""
    if len(der) < MIN_KEY_DER or der[0] != DER_SEQUENCE or not der[1] & 0x80:
        return False                              # a key is longer than 127 bytes: the long length form
    width = der[1] & 0x7F
    if not 0 < width <= 4 or der[2] == 0:        # DER: the shortest length encoding, no leading zero
        return False
    return 2 + width + int.from_bytes(der[2:2 + width], "big") == len(der)


if __name__ == "__main__":
    cmd = sys.argv[1:2]
    if cmd == ["check"]:
        values = ensure()
        problems = validate(values) + app_key_problems(values)
        print("\n".join(f"✗ {p} (edit ~/hagency/deploy/.env)" for p in problems) or "✓ .env ok")
        sys.exit(1 if problems else 0)
    if cmd == ["get"] and len(sys.argv) == 3 and sys.argv[2] in RULES and sys.argv[2] not in PRIVATE | FAIL_CLOSED:
        values = read()
        if validate(values):
            raise SystemExit("✗ run `python3 envfile.py check` first")
        print(values.get(sys.argv[2], ""))
        sys.exit(0)
    print(__doc__)
    sys.exit(2)
