"""AA-1113 (S284 tech lead redesign; CIS 453, AA-Partner crm-077) — the split of each LLM connection's provider budget
between the apps that spend on it.

A model connection has one budget at its provider (``cis.spend.connection_provider_budget_usd``, CIS 453: a json
object {connection key: USD per month}). No app reads another app's ledger or view (root ADR 0008a): each app keeps its
OWN monthly cap per connection and enforces it on its own ledger, and the shares together must stay under the provider
budget so the provider total is never exceeded:

- the CIS share is the connection's ``shared.llm_provider_connection.monthly_budget_usd`` (the CIS ledger; the CIS
  gateway's connection budget). It is a limit only while the connection's policy is a hard stop
  (``shared.llm_connection_cost_policy.hard_stop``, true when the connection has no policy row): with hard_stop false
  the CIS gateway only alerts at 100 % and keeps calling, so the share is reported as alert only and the CIS spend on
  that connection counts as uncapped. TripPlanner writes into the same CIS ledger but does not enforce this cap yet
  (follow-up: TripPlanner reads ``v_llm_gateway_config_v1.over_cap_connections``), so its calls are not bounded by
  the CIS share;
- the CRM share is the CRM setting ``crm.ai.connection_monthly_cap_usd`` (crm-077, the same json shape), which CIS reads
  only through the CRM settings relay Platform > Settings already uses (crm.ops.v1 ``/settings``, the staff member's own
  token; api/services/crm_settings_relay.py), never from the CRM's tables.

This module is pure: it parses the two maps and computes one row per connection with the over-allocation flag. The AI
gateway Budgets tab reads it (api/services/ai_gateway.py, the AA-1113 code PR). Nothing here sits on a call path.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping, Optional

# CIS 453: the provider budget per connection (Platform > Settings, area spend).
PROVIDER_BUDGET_KEY = "cis.spend.connection_provider_budget_usd"
# AA-Partner crm-077: the CRM's own monthly cap per connection (Platform > Settings, CRM tab), read through the relay.
CRM_CAP_KEY = "crm.ai.connection_monthly_cap_usd"
CRM_CAP_AREA = "ai"

OK = "ok"
OVER_ALLOCATED = "over_allocated"   # the shares add up to more than the provider budget
UNBOUNDED = "unbounded"             # a provider budget, but an app has no cap (or an alert-only one) on it
CRM_UNKNOWN = "crm_unknown"         # the CRM's share could not be read (relay refused or down)
NO_PROVIDER_BUDGET = "no_provider_budget"
STATES = (OK, OVER_ALLOCATED, UNBOUNDED, CRM_UNKNOWN, NO_PROVIDER_BUDGET)


def budget_map(value: Any) -> dict[str, Decimal]:
    """{connection key: USD} from a setting value (a json object of numbers >= 0). ``ValueError`` on any other shape:
    the caller reports the setting as unreadable instead of guessing."""
    if not isinstance(value, Mapping):
        raise ValueError("not a JSON object")
    out: dict[str, Decimal] = {}
    for key, raw in value.items():
        if not isinstance(key, str) or not key or isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
            raise ValueError(f"entry {key!r} is not a number")
        try:
            usd = Decimal(str(raw))
        except InvalidOperation:
            raise ValueError(f"entry {key!r} is not a number") from None
        if not usd.is_finite() or usd < 0:
            raise ValueError(f"entry {key!r} is below 0")
        out[key] = usd
    return out


def _dec(value: Any) -> Optional[Decimal]:
    return None if value is None else Decimal(str(value))


def split_row(connection_key: str, *, provider_budget: Any, cis_share: Any, crm_share: Any,
              crm_known: bool = True, cis_hard_stop: bool = True) -> dict:
    """One connection's split. ``None`` = not set (no provider budget declared, no CIS cap, no CRM cap).

    ``over_allocated`` is true when a provider budget is set and the shares that are set add up to more than it (an
    unknown CRM share counts 0, so CIS alone above the budget is still flagged). ``cis_hard_stop`` is the connection's
    cost policy: false = the CIS share is alert only (the gateway keeps calling past it), so CIS counts as uncapped
    and is listed in ``alert_only_apps`` too. The state, worst first: no_provider_budget (nothing to check),
    over_allocated, crm_unknown, unbounded (an app with no cap, or an alert-only one, could spend the whole provider
    budget), ok."""
    provider, cis = _dec(provider_budget), _dec(cis_share)
    crm = _dec(crm_share) if crm_known else None
    allocated = (cis or Decimal(0)) + (crm or Decimal(0))
    over = provider is not None and allocated > provider
    alert_only = ["cis"] if cis is not None and not cis_hard_stop else []
    uncapped = [app for app, share in (("cis", cis), ("crm", crm))
                if (share is None and (app == "cis" or crm_known)) or app in alert_only]
    if provider is None:
        state = NO_PROVIDER_BUDGET
    elif over:
        state = OVER_ALLOCATED
    elif not crm_known:
        state = CRM_UNKNOWN
    elif uncapped:
        state = UNBOUNDED
    else:
        state = OK
    return {
        "connection_key": connection_key,
        "provider_budget_usd": None if provider is None else float(provider),
        "cis_share_usd": None if cis is None else float(cis),
        "cis_hard_stop": bool(cis_hard_stop),
        "crm_share_usd": None if crm is None else float(crm),
        "crm_known": crm_known,
        "allocated_usd": float(allocated),
        "headroom_usd": None if provider is None else float(provider - allocated),
        "over_allocated": over,
        "uncapped_apps": uncapped,
        "alert_only_apps": alert_only,
        "state": state,
    }


def split_rows(connections: Iterable[tuple[str, Any]], provider_budgets: Mapping[str, Decimal],
               crm_caps: Optional[Mapping[str, Decimal]],
               cis_hard_stops: Optional[Mapping[str, bool]] = None) -> dict:
    """Every registry connection ``(key, CIS share)`` with its split, and the keys of the two maps that name no
    connection (a typo in a setting value would otherwise go unseen). ``crm_caps`` None = the CRM share is unknown.
    ``cis_hard_stops`` {key: the policy's hard_stop}; a key it lacks is a hard stop (no policy row = the default)."""
    rows, known = [], set()
    for key, cis_share in connections:
        known.add(key)
        rows.append(split_row(key, provider_budget=provider_budgets.get(key), cis_share=cis_share,
                              crm_share=None if crm_caps is None else crm_caps.get(key),
                              crm_known=crm_caps is not None,
                              cis_hard_stop=(cis_hard_stops or {}).get(key, True)))
    rows.sort(key=lambda r: r["connection_key"])
    stray = sorted((set(provider_budgets) | set(crm_caps or {})) - known)
    return {"rows": rows, "unknown_keys": stray,
            "over_allocated": [r["connection_key"] for r in rows if r["over_allocated"]]}
