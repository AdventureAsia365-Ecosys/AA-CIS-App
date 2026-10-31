"""The versioned read contract of the CIS control plane for other apps (root ADR 0009 decision 13, CIS migration
323).

- ``GATEWAY_CONFIG_VIEW`` (``catalog.v_llm_gateway_config_v1``): one row per TripPlanner stage (``tp_*``) and model
  of its chain (``position`` 0 = the primary, then the fallbacks in order), with the route, the catalog row of the
  model (NULL columns when the key is not in the catalog) and its deployments as a JSON array ordered by priority.
- ``APP_SPEND_VIEW`` (``catalog.v_llm_app_spend_today_v1``): today's (UTC) ledger spend of ``app = 'tripplanner'``
  per stage.

Both are granted to the ``tripplanner`` role only. A column is only ever appended in a ``_v1`` view; a change of
meaning is a new ``_v2`` view.
"""

from __future__ import annotations

GATEWAY_CONFIG_VIEW = "catalog.v_llm_gateway_config_v1"
APP_SPEND_VIEW = "catalog.v_llm_app_spend_today_v1"

GATEWAY_CONFIG_COLUMNS = (
    "stage",
    "role",
    "is_active",
    "account_route",
    "allowed_scopes",
    "shadow_model_id",
    "shadow_sample_pct",
    "position",
    "model_key",
    "in_catalog",
    "vendor",
    "provider",
    "api_style",
    "bedrock_profile_ids",
    "wire_model",
    "max_output_tokens",
    "model_enabled",
    "price_in_per_mtok",
    "price_out_per_mtok",
    "price_cache_read_per_mtok",
    "price_cache_write_per_mtok",
    "deployments",
    "route_updated_at",
)
APP_SPEND_COLUMNS = ("day", "stage", "calls", "unpriced_calls", "failed_calls", "cost_usd", "last_call_at")

# The SELECTs a consumer runs (column names, never *, so an appended column changes nothing).
GATEWAY_CONFIG_SQL = (
    f"SELECT {', '.join(GATEWAY_CONFIG_COLUMNS)} FROM {GATEWAY_CONFIG_VIEW} "  # noqa: S608 — fixed identifiers
    "WHERE stage = $1 AND is_active ORDER BY position"
)
APP_SPEND_SQL = (
    f"SELECT COALESCE(SUM(cost_usd), 0) AS spent FROM {APP_SPEND_VIEW} "  # noqa: S608 — fixed identifiers
    "WHERE stage = ANY($1::text[])"
)
