"""AA-1113 (S284 tech lead redesign; CIS 453, AA-Partner crm-077) — ``GET /admin/ai-gateway/connection-split``: the
split of each LLM connection's provider budget between the CIS share and the CRM share (AI gateway › Budgets).

Three sources, each read on its own (a failing one is named in ``errors`` / ``crm_error``, the others still show):

- the registry connections and their CIS share (``shared.llm_provider_connection.monthly_budget_usd``, the CIS
  gateway's connection budget), with the connection's cost policy (``shared.llm_connection_cost_policy.hard_stop``
  through ``cost_guard.read_cost_policies``): the CIS gateway refuses at the share only on a hard stop, so a share
  whose policy is alert only is reported as uncapped, never as a limit. TripPlanner writes into the same ledger but
  does not enforce this cap yet (a follow-up), so the CIS share does not bound TripPlanner's calls;
- the provider budget per connection, the CIS setting ``cis.spend.connection_provider_budget_usd`` (CIS 453; the
  settings snapshot);
- the CRM share, the CRM setting ``crm.ai.connection_monthly_cap_usd`` (crm-077) as the CRM reports it through the
  relay Platform › Settings already uses for its CRM tab (crm.ops.v1 ``GET /settings``, area ``ai``, with the staff
  member's own WORKFORCE token; api/services/crm_settings_relay.py). CIS never reads the CRM's tables and the CRM
  never reads CIS's (root ADR 0008a): this page is where the two shares meet.

The rule (over-allocated, unbounded, ...) is pure and lives in :mod:`api.services.llm_connection_split`, which ships
with the CIS 453 migration PR (MIG-4: the key's reader rides with its seed); it is imported inside the functions so the
API imports even on a tree that does not have it yet.
"""
from __future__ import annotations

from typing import Any, Optional

import structlog

from api.services import crm_console
from shared import cost_guard, settings

logger = structlog.get_logger()

CRM_NOT_PUBLISHED = "crm_setting_not_published"   # the CRM does not list the key yet (crm-077 not deployed there)
CRM_UNREADABLE = "crm_setting_unreadable"         # the CRM's value is not an object of amounts


async def _crm_caps(authorization: Optional[str]) -> tuple[Optional[dict], Optional[str]]:
    """(the CRM's caps by connection key, None) or (None, why not)."""
    from api.services import llm_connection_split as split

    status, body = await crm_console.crm_ops_call("GET", "/settings", authorization,
                                                  params={"area": split.CRM_CAP_AREA})
    if status != 200:
        code = body.get("detail") if isinstance(body.get("detail"), str) else None
        return None, code or f"crm_status_{status}"
    item = next((s for s in body.get("settings") or []
                 if isinstance(s, dict) and s.get("key") == split.CRM_CAP_KEY), None)
    if item is None:
        return None, CRM_NOT_PUBLISHED
    try:
        return split.budget_map(item.get("value")), None
    except ValueError:
        return None, CRM_UNREADABLE


async def read_connection_split(pool: Any, authorization: Optional[str]) -> dict:
    from api.services import llm_connection_split as split
    from shared.llm_client import connections as conn_registry

    errors: list[dict] = []
    async with pool.acquire() as conn:
        registry = await conn_registry.fetch_connections(conn)
        try:
            policies = await cost_guard.read_cost_policies(conn)
        except Exception as e:  # a policy read that fails: every share is called alert only, never a limit
            logger.warning("connection_split_cost_policy_unreadable", error=type(e).__name__)
            policies = None
            errors.append({"source": "cost_policy", "error": type(e).__name__})
    try:
        provider = split.budget_map(await settings.aget(split.PROVIDER_BUDGET_KEY))
    except Exception as e:  # a value that breaks the shape: say so, check nothing against it
        logger.warning("connection_split_provider_budget_unreadable", error=type(e).__name__)
        provider = {}
        errors.append({"source": "provider_budget", "error": type(e).__name__})
    crm_caps, crm_error = await _crm_caps(authorization)
    hard_stops = {k: (policies is not None and cost_guard.policy_of(policies, k).hard_stop) for k in registry}
    out = split.split_rows(((k, c.monthly_budget_usd) for k, c in registry.items()), provider, crm_caps, hard_stops)
    for row in out["rows"]:
        c = registry[row["connection_key"]]
        row.update(label=c.label, kind=c.kind, enabled=c.enabled)
    return {**out, "crm_error": crm_error, "errors": errors,
            "provider_budget_key": split.PROVIDER_BUDGET_KEY, "crm_cap_key": split.CRM_CAP_KEY}


async def read_for(request: Any) -> dict:
    """The route's call: the app pool and the staff member's own token (the CRM relay)."""
    from api.services.platform_apps import relay_authorization

    return await read_connection_split(request.app.state.pool, relay_authorization(request))
