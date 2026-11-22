"""Who may create an account on the public server (configure_synapse.py applies it; ADR 0013).

  - SMTP settings in .env complete and valid, and HAGENCY_OPEN_SIGNUP=1: anyone may sign up, but only
    with an email address they confirm (registrations_require_3pid), and no invite token is needed;
  - anything else (no SMTP, part of it, a value that doesn't validate, or HAGENCY_OPEN_SIGNUP=0):
    sign-up stays invite-token only, exactly as before.
Complete SMTP also turns on Synapse's email: password reset by email works in both modes.
Registration never opens without verification: enable_registration_without_verification is always
removed, and the open mode is only ever paired with a required email. No value is ever printed.
"""
import json
import re

import envfile

BRAND = "Hagency Rooms"                       # Element's brand (element_home.py) and the emails' app_name
SWITCH = "HAGENCY_OPEN_SIGNUP"                # 1: open sign-up once SMTP is set; 0: invite tokens only
SMTP_KEYS = ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS", "SMTP_FROM")
IMPLICIT_TLS_PORT = 465                       # TLS from the first byte; any other port must offer STARTTLS
NO_VERIFICATION = "enable_registration_without_verification"
# Per client address: 3 sign-ups, then one every ~5.5 min (Synapse's default is ~10 a minute).
RC_REGISTRATION = "{per_second: 0.003, burst_count: 3}"
# Confirmation emails per address or account: 3, then one every ~8 min. A free SMTP plan sends only
# a few hundred a day, so this keeps one sender from spending it all (Synapse's default is 5 then 0.003).
RC_3PID_VALIDATION = "{per_second: 0.002, burst_count: 3}"
EMAIL_KEYS = ("email", "request_token_inhibit_3pid_errors", "rc_3pid_validation")
OPEN_KEYS = ("registrations_require_3pid", "disable_msisdn_registration", "rc_registration")


def smtp_problems(e: dict) -> list[str]:
    """Names (never values) of the SMTP settings that are missing or don't validate."""
    missing = [k for k in SMTP_KEYS if not e.get(k)]
    bad = [k for k in SMTP_KEYS if e.get(k) and not re.fullmatch(envfile.RULES[k], e[k])]
    return [f"{k} is empty" for k in missing] + [f"{k} doesn't look right" for k in bad]


def is_open(e: dict) -> bool:
    return e.get(SWITCH) == "1" and not smtp_problems(e)


def describe(e: dict) -> str:
    """One line for bootstrap's output: which mode, and why (setting names only)."""
    if is_open(e):
        return "sign-up open to anyone with a confirmed email (set HAGENCY_OPEN_SIGNUP=0 in .env to close it)"
    problems = smtp_problems(e)
    if problems:
        return f"sign-up by invite token only: SMTP not ready ({', '.join(problems)})"
    return "sign-up by invite token only: HAGENCY_OPEN_SIGNUP isn't 1 (password reset by email works)"


def email_block(e: dict) -> str:
    """Synapse's email section as one flow-style line (JSON is YAML), every value quoted by json.dumps."""
    port = int(e["SMTP_PORT"])
    tls = {"force_tls": True} if port == IMPLICIT_TLS_PORT else {"require_transport_security": True}
    block = {"smtp_host": e["SMTP_HOST"], "smtp_port": port, "smtp_user": e["SMTP_USER"],
             "smtp_pass": e["SMTP_PASS"], **tls, "enable_tls": True,
             "notif_from": f"{BRAND} <{e['SMTP_FROM']}>", "app_name": BRAND,
             "client_base_url": f"https://{e['ROOMS_DOMAIN']}" if e.get("ROOMS_DOMAIN") else None,
             "enable_notifs": False, "validation_token_lifetime": "1h"}
    return json.dumps({k: v for k, v in block.items() if v is not None})


def settings(e: dict) -> dict:
    """The keys this mode sets. registration_requires_token is in configure_synapse's own list."""
    if smtp_problems(e):
        return {}
    values = {"email": email_block(e), "request_token_inhibit_3pid_errors": "true",
              "rc_3pid_validation": RC_3PID_VALIDATION}
    if is_open(e):
        values.update({"registrations_require_3pid": "[email]", "disable_msisdn_registration": "true",
                       "rc_registration": RC_REGISTRATION})
    return values


def drop_keys(text: str, keys) -> str:
    """Remove top-level keys, with any indented lines under them (a hand-written block)."""
    for key in keys:
        text = re.sub(rf"(?m)^{key}:.*\n?(?:[ \t]+.*\n?)*", "", text)
    return text


def stale_keys(e: dict) -> list[str]:
    """Keys a previous mode may have left that this one must not keep (closing sign-up removes them)."""
    wanted = settings(e)
    return [NO_VERIFICATION] + [k for k in EMAIL_KEYS + OPEN_KEYS if k not in wanted]
