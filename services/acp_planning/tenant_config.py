"""
services.acp_planning.tenant_config — AA-323 Gap 3.

Single read path for the markets/channels/capacity_posts_per_week trio that
N4 (runway_map)/N5 (plan_quarter)/N6 (allocate_month) need per tenant.
Deliberately joins two tables rather than inventing one:
  - acp_shared.tenant_config (markets, channels) — new, migration 101.
  - shared.tenants.posts_per_week (capacity) — already exists, migration 099
    (AA-384) — NOT duplicated here, see migration 101's header comment.

A tenant with no acp_shared.tenant_config row (never configured via the new
Tenants > Planning tab) falls back to markets ["US"] — the exact value the
pre-AA-323 hardcoded demo constants used.

AA-840: the channels fallback is every Slate Channel (was ["blog"]). The channel
list is read by the T7 Slate (services/acp_shared/slate.py::_tenant_enabled_channels),
which proposes for every Channel when a tenant has no row; the admin Planning
config form shows this value, so a ["blog"] fallback there showed a config the
Slate did not follow, and saving the form unchanged would have narrowed the
tenant's Slate to blog only.
"""
from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from services.acp_shared.slate import CHANNEL_BARS

_DEFAULT_MARKETS = ["US"]
_DEFAULT_CHANNELS = list(CHANNEL_BARS)


@dataclass
class TenantPlanningConfig:
    markets: list[str]
    channels: list[str]
    capacity_posts_per_week: int


class TenantNotFoundError(Exception):
    """Raised when tenant_id has no shared.tenants row at all."""


async def fetch_tenant_planning_config(tenant_id: UUID, pool) -> TenantPlanningConfig:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT t.posts_per_week, tc.markets, tc.channels
            FROM shared.tenants t
            LEFT JOIN acp_shared.tenant_config tc ON tc.tenant_id = t.tenant_id
            WHERE t.tenant_id = $1
            """,
            tenant_id,
        )
    if row is None:
        raise TenantNotFoundError(f"Unknown tenant_id: {tenant_id}")
    return TenantPlanningConfig(
        markets=list(row["markets"]) if row["markets"] else list(_DEFAULT_MARKETS),
        channels=list(row["channels"]) if row["channels"] else list(_DEFAULT_CHANNELS),
        capacity_posts_per_week=row["posts_per_week"],
    )


async def save_tenant_planning_config(
    tenant_id: UUID, markets: list[str], channels: list[str], posts_per_week: int, pool,
) -> None:
    async with pool.acquire() as conn:
        async with conn.transaction():
            updated = await conn.execute(
                "UPDATE shared.tenants SET posts_per_week = $2 WHERE tenant_id = $1",
                tenant_id, posts_per_week,
            )
            if updated == "UPDATE 0":
                raise TenantNotFoundError(f"Unknown tenant_id: {tenant_id}")
            await conn.execute(
                """
                INSERT INTO acp_shared.tenant_config (tenant_id, markets, channels, updated_at)
                VALUES ($1, $2, $3, now())
                ON CONFLICT (tenant_id) DO UPDATE
                SET markets = $2, channels = $3, updated_at = now()
                """,
                tenant_id, markets, channels,
            )


__all__ = [
    "TenantPlanningConfig", "TenantNotFoundError",
    "fetch_tenant_planning_config", "save_tenant_planning_config",
]
