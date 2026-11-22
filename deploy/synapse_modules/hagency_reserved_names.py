"""A Synapse module: names the server itself needs are never taken through public sign-up (ADR 0013).

@pm, @owner, every agent (identity.py: <name>-agent) and the server's bots (admin-bot) are made by
trusted provisioning (the admin API with the registration secret). Once sign-up is open, someone could
register one of those names first: @pm on a fresh database before @pm's container starts, or a
person's future agent name. This refuses them when the sign-up is public:
  - a password sign-up that carries an email (open sign-up always does: registrations_require_3pid);
  - a sign-in through Google or GitHub (auth_provider_id is set).
The admin API (no email, no provider) and a token holder's sign-up without email are left alone.
Synapse answers a refusal with 429 "Rate limited" (synapse/handlers/register.py), on purpose vague.
configure_synapse.py loads it only when open sign-up or an SSO provider is on.
"""
try:
    from synapse.spam_checker_api import RegistrationBehaviour
except ImportError:          # unit tests run without Synapse installed; inside Synapse it always imports
    RegistrationBehaviour = None


def is_reserved(username, names, suffixes) -> bool:
    if not isinstance(username, str):
        return False                         # Synapse picks the name itself: never one of ours
    name = username.strip().lower()
    return name in names or name.endswith(tuple(suffixes))


def is_public(email_threepid, auth_provider_id) -> bool:
    return email_threepid is not None or auth_provider_id is not None


class ReservedNames:
    def __init__(self, config: dict, api) -> None:
        self.names = {n.lower() for n in config["names"]}
        self.suffixes = tuple(s.lower() for s in config["suffixes"])
        api.register_spam_checker_callbacks(check_registration_for_spam=self.check_registration_for_spam)

    @staticmethod
    def parse_config(config: dict) -> dict:
        ok = (isinstance(config, dict) and isinstance(config.get("names"), list)
              and isinstance(config.get("suffixes"), list) and config["names"] and config["suffixes"])
        if not ok:
            raise ValueError("hagency_reserved_names needs config: {names: [...], suffixes: [...]}")
        return config

    async def check_registration_for_spam(self, email_threepid, username, request_info, auth_provider_id=None):
        if is_public(email_threepid, auth_provider_id) and is_reserved(username, self.names, self.suffixes):
            return RegistrationBehaviour.DENY
        return RegistrationBehaviour.ALLOW
