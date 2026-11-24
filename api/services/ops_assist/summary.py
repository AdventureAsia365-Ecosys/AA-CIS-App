"""AA-1154 (A0) — the plain-words Summary of an analysis, for the business owner (tech lead AI10: a Summary tab, no
action buttons). Built by rule from the same cards the tech lead sees; no LLM in A0, no jargon (no risk codes, no
routes, no capability names). Pure."""
from __future__ import annotations

from typing import Optional

_WHAT = {
    "llm_access_denied": "One of the AI accounts the platform uses has stopped accepting requests.",
    "llm_throttled": "One of the AI accounts is slowing requests down because of a usage limit.",
    "llm_cost_spike": "AI spending is higher than usual.",
    "queue_stuck": "Background work is waiting longer than it should.",
    "server_errors": "One of the apps is returning more errors or answering more slowly than it should.",
    "generic": "Something on the status page needs a look.",
}
_CALM = {
    "R0": "watch only",
    "R1": "a check that changes nothing",
    "R2": "a small change that is easy to undo",
    "R3": "a bigger change that needs a written reason",
    "R4": "a step the tech lead does by hand",
}


def _first(cards: list[dict], action: str) -> Optional[dict]:
    return next((c for c in cards if c["action"] == action), None)


def build(playbook: str, cards: list[dict], *, title: Optional[str] = None) -> dict:
    """{headline, lines: [str]}: what is wrong, whether customers are affected, what the team can do (in order, in
    plain words) and what it costs to wait. Pure."""
    lines = [_WHAT.get(playbook, _WHAT["generic"])]
    if title:
        lines.append(f"What the system reports: {title}.")
    observe = _first(cards, "observe")
    if observe and observe.get("predictions"):
        lines.append(f"For now: {observe['predictions'][0]['text']}")
    if observe and observe.get("if_nothing"):
        lines.append(f"If nothing is done: {observe['if_nothing']}")
    options = [c for c in cards if c["action"] != "observe"]
    if options:
        steps = "; ".join(f"{n}. {c['title']} ({_CALM.get(c['risk']['level'], 'a step')})"
                          for n, c in enumerate(options[:3], start=1))
        lines.append(f"What the team can do, in this order: {steps}.")
    lines.append("Nothing is done automatically; a person on the team decides each step.")
    return {"headline": _WHAT.get(playbook, _WHAT["generic"]), "lines": lines}
