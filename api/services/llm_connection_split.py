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


# ── AA-1143 (decision G3, CIS 455 / 456): one budget per connection as rows ───────────────────────────────────────
#
# shared.llm_connection_budget holds, per connection, the total (app NULL: the provider budget) and one fixed-USD share
# per app other than cis; CIS takes the remainder (total minus the other apps' shares), which the table's trigger
# writes to llm_provider_connection.monthly_budget_usd (the number the CIS gateway enforces). The rules below are the
# API's checks before a save; the database refuses the same over-allocation on its own.

REMAINDER_APP = "cis"
CRM_APP = "crm"
# The apps whose own gateway enforces their share on their own ledger, from the copy CIS sends them (the CRM, root
# ADR 0008a): on a connection with a total, no share means no spend. Every other share app spends in the CIS ledger.
OWN_LEDGER_APPS = (CRM_APP,)
REASON_MIN, REASON_MAX = 20, 500        # the Settings rule (CIS 455 CHECK)
USD_MAX = Decimal("1000000")            # the bound of monthly_budget_usd and of the provider budget setting
CENT = Decimal("0.01")


class BudgetEditRefused(ValueError):
    """A connection budget change the rules refuse (422), with a stable ``code`` for the page."""

    def __init__(self, code: str, message: str):
        super().__init__(code, message)
        self.code = code
        self.message = message

    def __str__(self) -> str:
        return self.message


def usd_amount(value: Any, what: str) -> Decimal:
    """A USD amount 0..1000000, rounded to the cent (the column is numeric(10,2)). BudgetEditRefused otherwise."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise BudgetEditRefused("amount_invalid", f"{what}: not an amount")
    try:
        usd = Decimal(str(value))
    except InvalidOperation:
        raise BudgetEditRefused("amount_invalid", f"{what}: not an amount") from None
    if not usd.is_finite() or usd < 0 or usd > USD_MAX:
        raise BudgetEditRefused("amount_invalid", f"{what}: must be from 0 to 1000000 USD")
    return usd.quantize(CENT)


def remainder(total: Optional[Decimal], shares: Mapping[str, Optional[Decimal]]) -> Optional[Decimal]:
    """The CIS remainder: total minus the other apps' shares (None shares count 0); None without a total."""
    if total is None:
        return None
    return total - sum((v for v in shares.values() if v is not None), Decimal(0))


def check_reason(reason: Any) -> str:
    text = reason.strip() if isinstance(reason, str) else ""
    if not REASON_MIN <= len(text) <= REASON_MAX:
        raise BudgetEditRefused("reason_length", f"a reason of {REASON_MIN} to {REASON_MAX} characters is required")
    return text


def plan_edit(apps: Iterable[str], before_total: Optional[Decimal], before_shares: Mapping[str, Optional[Decimal]],
              total: Any = None, shares: Optional[Mapping[str, Any]] = None) -> dict:
    """The state after one save, checked. ``apps``: the share apps (every app of shared.llm_app_profile but cis);
    ``total`` None = unchanged; ``shares`` {app: amount | None (remove)} = only the apps that change. Returns
    {"total", "shares" (every share app), "changes": [(app or None, new amount or None)], "remainder"}: the rows to
    write, decreases first (the order a reader of the CRM copy needs, see ``crm_tightens``). Refuses: an unknown app
    or cis (CIS takes the remainder), no total on a connection that has none, shares above the total."""
    apps = list(apps)
    new_total = before_total if total is None else usd_amount(total, "total")
    if new_total is None:
        raise BudgetEditRefused("total_required", "set the connection's total first")
    new_shares = {a: before_shares.get(a) for a in apps}
    changes: list[tuple[Optional[str], Optional[Decimal]]] = []
    for app, raw in (shares or {}).items():
        if app == REMAINDER_APP:
            raise BudgetEditRefused("cis_is_remainder", "CIS has no share of its own: it takes the remainder")
        if app not in new_shares:
            raise BudgetEditRefused("unknown_app", f"unknown app {app!r}")
        new = None if raw is None else usd_amount(raw, app)
        if new != new_shares[app]:
            new_shares[app] = new
            changes.append((app, new))
    left = remainder(new_total, new_shares)
    if left is not None and left < 0:
        raise BudgetEditRefused(
            "over_allocated", f"the apps' shares add up to {new_total - left} USD, more than the total {new_total} USD")
    if new_total != before_total:
        changes.append((None, new_total))
    # Shares that go down first, then the total, then the shares that go up (one INSERT keeps this order in its ids).

    def rank(change):
        app, new = change
        if app is None:
            return 1
        old = before_shares.get(app)
        return 0 if (new or Decimal(0)) < (old or Decimal(0)) else 2
    changes.sort(key=rank)
    return {"total": new_total, "shares": new_shares, "changes": changes, "remainder": left}


def effective_caps(total: Optional[Decimal], shares: Mapping[str, Optional[Decimal]]) -> dict:
    """{app: its real cap} for the tighten-only check: cis = the remainder, an app = its share. What "no share" means
    depends on where the app's spend is counted, so it is one rule for every app, not a CRM special case:

    - an app that spends inside the CIS ledger (TripPlanner today): no share = it spends inside the remainder, which
      the cis entry bounds, so its own entry is None;
    - an app with its own ledger (``OWN_LEDGER_APPS``: its gateway enforces the copy CIS sends it): on a connection
      with a total, no share = a cap of 0 (the copy says "0.00" and its gateway blocks the connection), so its entry
      is 0. Giving it a share is then a raise and removing one a cut. Without a total CIS sends it nothing for the
      connection (its own fallback applies), so the entry stays None. Pure."""
    caps = {a: (None if v is None else float(v)) for a, v in shares.items()}
    if total is not None:
        for app in OWN_LEDGER_APPS:
            if app in caps and caps[app] is None:
                caps[app] = 0.0
    left = remainder(total, shares)
    caps[REMAINDER_APP] = None if left is None else float(left)
    caps["total"] = None if total is None else float(total)
    return caps


def crm_tightens(before: Optional[Decimal], after: Optional[Decimal]) -> bool:
    """True when the CRM's share goes down (or is removed): its copy is sent BEFORE the CIS rows are written, so for a
    moment the CRM is held to the lower amount while CIS still has the old remainder (never above the total). A rise
    is sent after the CIS rows (CIS first takes the lower remainder)."""
    return (after or Decimal(0)) < (before or Decimal(0)) or (before is not None and after is None)
