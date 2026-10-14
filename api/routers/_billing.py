"""AA-798 — one reading of a ``shared.v_tenant_monthly_usage`` row, shared by the admin billing
view (``GET /admin/billing``) and the tenant's own ``GET /v1/billing``.

Two rules live here so they cannot drift between the two endpoints:

* **No invented plan.** Both endpoints used to answer a missing usage row with a made-up Starter
  plan (fixed price, 50 tours, 5000 calls, fixed overage rate). The view LEFT JOINs
  ``shared.membership_plans`` on ``tenants.plan_id``, and every plan column there is NOT NULL, so
  a NULL ``tours_quota_monthly`` means exactly "no plan attached". Then every plan-derived field
  (quotas, price, overage rate, quota %, overage) is ``None`` and ``plan_attached`` is ``False``;
  the UI shows "No plan attached" instead of a number nobody agreed to.
* **LLM cost has one definition.** ``llm_cost_usd`` is read from the view as-is. Since migration
  223 it is ``SUM(shared.llm_call_log.cost_usd)`` for the tenant, current calendar month, Jev
  included, shadow rows excluded. No endpoint computes a second formula (the old
  ``pipeline_runs`` + flat per-version estimate is gone with 223).
"""
from typing import Any, Optional

# Columns that only mean something when a plan is attached.
PLAN_DERIVED_FIELDS = (
    "tours_quota_monthly", "api_calls_quota_monthly", "price_usd_monthly",
    "overage_rate_usd_per_tour", "quota_tours_pct", "quota_calls_pct",
    "tours_overage", "overage_usd",
)


def _plain(v: Any) -> Any:
    """Decimal -> float; ints, strings and None pass through."""
    if v is None or isinstance(v, (int, bool, str)):
        return v
    return float(v) if hasattr(v, "__float__") else v


def usage_row_to_dict(row: Any) -> dict:
    """A ``v_tenant_monthly_usage`` row (asyncpg Record or dict) as a JSON-ready dict.

    Adds ``plan_attached``; nulls every plan-derived field when no plan is attached;
    ``billing_month`` becomes ``YYYY-MM``."""
    d = dict(row)
    attached = d.get("tours_quota_monthly") is not None
    out = {k: _plain(v) for k, v in d.items() if k != "billing_month"}
    if not attached:
        for k in PLAN_DERIVED_FIELDS:
            if k in out:
                out[k] = None
    out["plan_attached"] = attached
    bm: Optional[Any] = d.get("billing_month")
    out["billing_month"] = str(bm)[:7] if bm else None
    return out


def no_plan_usage(tenant_name: Optional[str] = None, plan_tier: Optional[str] = None) -> dict:
    """The usage block for a tenant that has no usage row: nothing counted, no plan invented."""
    out = {k: None for k in PLAN_DERIVED_FIELDS}
    out.update({
        "tenant_name": tenant_name, "plan_tier": plan_tier, "billing_month": None,
        "tours_rewritten": 0, "api_calls_used": 0, "plan_attached": False,
    })
    return out
