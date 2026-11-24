"""AA-1154 (A0) — the risk level of an action card, computed by rule (never by an LLM; S285 advisory section 4).

Level = the highest of four axes (R0 read, R1 simulate / a few cents, R2 tighten and narrow and easy to undo, R3
loosen / paid / wide / hard to undo, R4 outside the admin API):

- **direction**: ``read`` R0, ``simulate`` R1, ``tighten`` R2, ``reroute`` / ``rerun`` / ``loosen`` R3. A card that
  changes a limit passes ``change={"before": {...}, "after": {...}}``: ``settings_registry.loosened()`` with the
  action's ``limit_kinds`` decides tighten (nothing loosened) or loosen;
- **scope**: a state change (tighten and above) on ``app`` scope is at least R2; on ``all``, or on a connection or
  route that serves at least ``wide_scope_stages`` stages, R3. A read or a simulation is never raised by its scope;
- **money**: ``cents`` with an estimate at most ``small_money_usd`` is R1; ``paid`` with an estimate above it, or with
  no estimate yet, is R3 (a DRY RUN that measures the cost brings it down);
- **reversible**: a state change that cannot be undone is R3.

``copy_only`` actions are R4. Pure; every limit comes from ``shared/ops_assist_settings.snapshot()``.
"""
from __future__ import annotations

from typing import Optional

from api.services.ops_assist.catalog import Action
from api.services.settings_registry import loosened

LEVELS = ("R0", "R1", "R2", "R3", "R4")
_DIRECTION = {"read": 0, "simulate": 1, "tighten": 2, "reroute": 3, "rerun": 3, "loosen": 3}
_CHANGES = ("tighten", "reroute", "rerun", "loosen")


def direction_of(action: Action, change: Optional[dict]) -> str:
    """The action's direction; for a limit change the loosened() verdict of its before / after. Pure."""
    if change and action.limit_kinds:
        worse = loosened(change.get("before"), change.get("after") or {}, **action.limit_kinds)
        return "loosen" if worse else "tighten"
    return action.direction


def assess(action: Action, limits: dict, *, stages: Optional[int] = None, cost_usd: Optional[float] = None,
           change: Optional[dict] = None) -> dict:
    """{level, axes: {direction, scope, money, reversible}, why: [str]} for one card. ``stages``: how many active
    stages the action touches (a connection or route action); ``cost_usd``: the card's cost estimate, None when no
    estimate exists yet. Pure."""
    direction = direction_of(action, change)
    why: list[str] = []
    if action.copy_only:
        return {"level": "R4", "axes": {"direction": direction, "scope": action.scope, "money": action.money,
                                        "reversible": action.reversible},
                "why": ["Outside the admin API: the tech lead runs it from the runbook (MFA)."]}
    level = _DIRECTION[direction]
    why.append(f"Direction {direction}: {LEVELS[level]}.")
    scope, scope_level, scope_why = _scope(action, limits, stages, direction)
    level = max(level, scope_level)
    why += scope_why
    money, money_level, money_why = _money(action, limits, cost_usd)
    level = max(level, money_level)
    why += money_why
    return {"level": LEVELS[level], "axes": {"direction": direction, "scope": scope, "money": money,
                                             "reversible": action.reversible}, "why": why}


def _scope(action: Action, limits: dict, stages: Optional[int], direction: str) -> tuple[str, int, list[str]]:
    """(scope, level, why) of the scope and reversible axes; a read or a simulation is never raised. Pure."""
    wide = stages is not None and stages >= int(limits["wide_scope_stages"])
    scope = "wide" if wide else action.scope
    if direction not in _CHANGES:
        return scope, 0, []
    level, why = 0, []
    if action.scope == "all" or wide:
        level = 3
        detail = f" ({stages} stages, at or above {limits['wide_scope_stages']})" if wide else ""
        why.append(f"Scope {scope}{detail}: R3.")
    elif action.scope == "app":
        level = 2
        why.append("Scope one app: at least R2.")
    if not action.reversible:
        level = 3
        why.append("Hard to undo: R3.")
    return scope, level, why


def _money(action: Action, limits: dict, cost_usd: Optional[float]) -> tuple[str, int, list[str]]:
    """(money, level, why) of the money axis. Pure."""
    small = float(limits["small_money_usd"])
    if action.money == "none":
        return "none", 0, []
    if cost_usd is None:
        if action.money == "cents":
            return "cents", 1, ["A few cents at most: R1."]
        return "paid", 3, ["Paid work with no cost estimate yet: R3 until a DRY RUN measures it."]
    if cost_usd > small:
        return "paid", 3, [f"Estimated {cost_usd:g} USD, above {small:g} USD: R3."]
    return "cents", 1, [f"Estimated {cost_usd:g} USD, at most {small:g} USD: R1 on money."]


def rank(level: str) -> int:
    return LEVELS.index(level)
