"""Sign-in with Google and with GitHub (Synapse's oidc_providers; configure_synapse.py applies it; ADR 0013).

Each provider is on only when both its keys are set (and valid) in .env, independently of the other
and of SMTP: Google alone gives "Continue with Google" while password sign-up stays token-only.
The provider has verified the person (Google its account, GitHub its account), so SSO sign-up needs
no token or email; password registration is never opened by this file.

A new SSO account never takes over an existing one: allow_existing_users stays false, so a mapped
localpart that is taken (pm, an agent's) is never linked; Synapse appends 1, 2, ... until it finds a
free one (synapse/handlers/sso.py, _call_attribute_mapper). Names ending -agent or -bot (identity.py's
agent accounts, admin-bot) get a suffix too, so an SSO account never squats an agent's future name.
Redirect URL for both apps: https://<MATRIX_DOMAIN>/_synapse/client/oidc/callback (Caddyfile routes
/_synapse/client* to Synapse). No value is ever printed.
"""
import json
import re

import envfile

PROVIDERS = {"google": ("HAGENCY_GOOGLE_CLIENT_ID", "HAGENCY_GOOGLE_CLIENT_SECRET"),
             "github": ("HAGENCY_GITHUB_CLIENT_ID", "HAGENCY_GITHUB_CLIENT_SECRET")}
CALLBACK_PATH = "/_synapse/client/oidc/callback"
RESERVED_SUFFIXES = ("-agent", "-bot")          # identity.AGENT_SUFFIX and the server's bots (admin-bot)


def localpart_template(source: str, tag: str) -> str:
    """Jinja for Synapse's mapper: `source` lower-cased, plus -<tag> when it ends like an agent or bot name."""
    ends = ", ".join(f"'{s}'" for s in RESERVED_SUFFIXES)
    return (f"{{% set n = ({source})|trim|lower %}}"
            f"{{{{ n ~ '-{tag}' if n.endswith(({ends})) else n }}}}")


def problems(e: dict, idp: str) -> list[str]:
    """Names (never values) of this provider's keys that are missing or don't validate."""
    keys = PROVIDERS[idp]
    return ([f"{k} is empty" for k in keys if not e.get(k)]
            + [f"{k} doesn't look right" for k in keys if e.get(k) and not re.fullmatch(envfile.RULES[k], e[k])])


def enabled(e: dict) -> list[str]:
    return [idp for idp in PROVIDERS if not problems(e, idp)]


def google(e: dict) -> dict:
    cid, secret = PROVIDERS["google"]
    return {"idp_id": "google", "idp_name": "Google", "idp_brand": "google",
            "issuer": "https://accounts.google.com/", "client_id": e[cid], "client_secret": e[secret],
            "scopes": ["openid", "profile", "email"],
            "allow_existing_users": False, "enable_registration": True,
            "user_mapping_provider": {"config": {
                "localpart_template": localpart_template("user.email|localpart_from_email", "g"),
                "display_name_template": "{{ user.name }}", "email_template": "{{ user.email }}"}}}


def github(e: dict) -> dict:
    cid, secret = PROVIDERS["github"]
    return {"idp_id": "github", "idp_name": "GitHub", "idp_brand": "github", "discover": False,
            "issuer": "https://github.com/", "client_id": e[cid], "client_secret": e[secret],
            "authorization_endpoint": "https://github.com/login/oauth/authorize",
            "token_endpoint": "https://github.com/login/oauth/access_token",
            "userinfo_endpoint": "https://api.github.com/user",
            "user_profile_method": "userinfo_endpoint", "scopes": ["read:user"],
            "allow_existing_users": False, "enable_registration": True,
            "user_mapping_provider": {"config": {
                "subject_claim": "id", "localpart_template": localpart_template("user.login", "gh"),
                "display_name_template": "{{ user.name }}"}}}


def settings(e: dict) -> dict:
    """oidc_providers (one flow-style JSON line, values quoted by json.dumps) and sso's return address."""
    on = enabled(e)
    if not on:
        return {}
    build = {"google": google, "github": github}
    return {"oidc_providers": json.dumps([build[idp](e) for idp in on]),
            # After signing in, back to Element without Synapse's "continue to this site?" page.
            "sso": json.dumps({"client_whitelist": [f"https://{e['ROOMS_DOMAIN']}/"]}) if e.get("ROOMS_DOMAIN") else "{}"}


def stale_keys(e: dict) -> list[str]:
    return [] if enabled(e) else ["oidc_providers", "sso"]


def describe(e: dict) -> str:
    """One line for bootstrap's output: which providers are on, and why the others are off (names only)."""
    parts = []
    for idp in PROVIDERS:
        found = problems(e, idp)
        on_or_off = "on" if not found else "off" if len(found) == 2 and all("empty" in p for p in found) \
            else f"off ({', '.join(found)})"
        parts.append(f"{idp} {on_or_off}")
    return "sign-in with " + ", ".join(parts)
