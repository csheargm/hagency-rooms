"""Which Ollama Cloud models a visitor may pick for the trial agent (ADR 0025): the operator's allowlist
TRIAL_MODELS, comma-separated, the first one the default. An entry may carry a weight, "name*2": its
tokens count that many times against the monthly token cap (trial_budget.py), for bigger models.
The balance guard needs no weights: Ollama's balance is in money, so bigger models already count more.
A visitor's choice is kept with them (trial_agent.visitors) and checked against the list at every use:
a name the operator removed falls back to the default.
"""
import os
import re

# Checked on https://ollama.com/api/tags, 2026-11-27: Z.ai's coding flagship first, then Moonshot's largest
# (3T-class: counts double), its coding model, a fast DeepSeek and OpenAI's open model.
DEFAULT_LIST = "glm-5.3,kimi-k3.1-pro*2,kimi-k2.7-code,deepseek-v4.1-flash,gpt-oss:120b"
NAME_RE = re.compile(r"[a-z0-9][a-z0-9._:-]{0,80}")
ENTRY = r"[a-z0-9][a-z0-9._:-]{0,80}(?:\*[0-9]{1,2}(?:\.[0-9]{1,2})?)?"   # also deploy/envfile.py's rule
WEIGHT_RE = re.compile(rf"({NAME_RE.pattern})(?:\*([0-9]{{1,2}}(?:\.[0-9]{{1,2}})?))?")
MAX_MODELS = 12


def weighted() -> dict[str, float]:
    """name → weight, in the operator's order. A malformed entry is skipped; if none is usable, only the
    default list's FIRST model (never the whole bundled list: GPT review). deploy/envfile.py refuses such a list."""
    out: dict[str, float] = {}
    for raw in (os.environ.get("TRIAL_MODELS", "").strip() or DEFAULT_LIST).split(","):
        m = WEIGHT_RE.fullmatch(raw.strip().lower())
        if m and len(out) < MAX_MODELS:
            out.setdefault(m.group(1), float(m.group(2) or 1) or 1.0)
    return out or dict([next(iter(weighted_default().items()))])


def weighted_default() -> dict[str, float]:
    return {m.group(1): float(m.group(2) or 1) for m in map(WEIGHT_RE.fullmatch, DEFAULT_LIST.split(","))}


def names() -> list[str]:
    return list(weighted())


def default() -> str:
    return names()[0]


def allowed(name: object) -> bool:
    return isinstance(name, str) and name.strip().lower() in weighted()


def chosen(name: object) -> str:
    """The model to use for a visitor's stored choice: it, if still allowed, else the default."""
    return name.strip().lower() if allowed(name) else default()


def weight(name: str) -> float:
    return weighted().get(name, 1.0)
