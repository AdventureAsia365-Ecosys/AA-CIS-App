"""
AA-544 -- aa_app_user tenant pool + per-request RLS GUC helper.

Rollout scope: wired into routes ONE AT A TIME, each behind its OWN Redis flag (see
STAGE_FLAGS below), per AA-544's original plan's Stage 5 principle ("mo rong dan tung route,
moi route revert rieng qua flag"). Do NOT import this into another router without going
through AA-544's own stage-by-stage report-then-confirm ritual first -- picking which route is
next is a decision, not something to copy-paste in ad hoc. Live routes so far:
  - Stage 1 (10/09/2026): GET /v1/publish-log/pending (v1_publish.py::list_pending)
  - Stage 5 (10/09/2026): GET /v1/content-writing/pieces/{piece_id} (service.py::fetch_piece)

Design (matches AA-544's approved 2-pool split): Admin (/admin/*) keeps app.state.pool
(aa_cis_admin, BYPASSRLS) unchanged, always. This module adds a SECOND pool, aa_app_user
(non-BYPASSRLS, real RLS applies), used ONLY by routes that explicitly opt in via
acquire_scoped_conn() below, and only when that route's OWN flag is on.

Rollback (per route, independent of every other route on this pool): `redis-cli DEL
<that route's flag key>` (or SET ... false) reverts it to the admin pool immediately -- no
deploy needed, and does not affect any other route already on the tenant pool.

GUC safety: app.tenant_id is set via set_config(..., is_local=true) INSIDE an explicit
transaction (SET LOCAL semantics) -- scoped to that one transaction so it cannot leak onto the
next request that reuses this same physical connection from the pool. Every query that needs
tenant scoping MUST run inside the same `async with conn.transaction():` block
acquire_scoped_conn() opens -- see its docstring.
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Optional

import asyncpg
import structlog
from fastapi import Request

logger = structlog.get_logger()

# One flag per canary route -- each independently revertible. Add a new named constant here
# (don't invent ad hoc string literals at call sites) whenever a new route joins the rollout.
STAGE1_PUBLISH_PENDING_FLAG = "aa544:stage1:publish_pending_pool"
STAGE5_GET_PIECE_FLAG = "aa544:stage5:get_piece_pool"
# AA-815 — GET /v1/publish-log (tenant's published/unpublished rows). Default off (admin pool).
PUBLISH_LOG_LIST_FLAG = "aa544:publish_log_list_pool"
# AA-815 — DELETE /v1/integrations/{wordpress|facebook} (tenant disconnect). Default off (admin pool).
INTEGRATION_DISCONNECT_FLAG = "aa544:integration_disconnect_pool"

_SECRET_ID = "aa-cis/dev/rds-app-user"


async def create_tenant_pool() -> Optional[asyncpg.Pool]:
    """Best-effort, called once at app startup (api/main.py's lifespan()). A missing/
    unreachable secret must never crash app boot -- same convention as the AA-223 startup
    sweep already in lifespan(). Returns None on any failure; every caller below treats None
    as "stay on the admin pool" (fail closed to pre-Stage-1 behavior, never fail open into an
    unproven code path).

    Small pool (max_size=5) on purpose -- this is a 1-route canary, not yet real traffic
    volume; revisit sizing when Stage 2+ adds more routes."""
    try:
        import boto3
        client = boto3.client(
            "secretsmanager", region_name=os.environ.get("AWS_REGION", "us-west-1")
        )
        dsn = client.get_secret_value(SecretId=_SECRET_ID)["SecretString"]
        return await asyncpg.create_pool(dsn, min_size=1, max_size=5)
    except Exception as e:
        logger.error("aa544_tenant_pool_create_failed", error=repr(e))
        return None


async def _flag_enabled(request: Request, flag_key: str) -> bool:
    """Fail CLOSED -- any Redis error or unset key means "off" (falls back to the admin pool,
    the pre-rollout behavior for that route). Never fail open into a code path that hasn't been
    proven yet."""
    try:
        val = await request.app.state.redis.get(flag_key)
    except Exception as e:
        logger.warning("aa544_flag_read_failed", flag_key=flag_key, error=repr(e))
        return False
    return val == "true"


@asynccontextmanager
async def acquire_scoped_conn(request: Request, tenant_id: str, flag_key: str):
    """Yields (conn, used_tenant_pool: bool).

    `flag_key` is one of this module's own STAGE*_FLAG constants -- each route on this rollout
    gets its own flag, so reverting one route never affects another already on the tenant pool.

    When that route's flag is on AND the tenant pool booted successfully: acquires from
    request.app.state.tenant_pool (aa_app_user, non-BYPASSRLS) and opens a transaction with
    app.tenant_id set LOCAL to it. Everything the caller does with `conn` inside this
    `async with` block MUST happen inside that same transaction for the GUC to apply -- do not
    acquire a second connection or start a nested transaction on this one mid-block.

    Otherwise (flag off, or tenant pool never came up): acquires from
    request.app.state.pool (aa_cis_admin, current behavior, completely unchanged) with no
    transaction wrapper -- this is the fail-closed default path, not an error case."""
    tenant_pool = getattr(request.app.state, "tenant_pool", None)
    use_tenant_pool = tenant_pool is not None and await _flag_enabled(request, flag_key)

    if use_tenant_pool:
        async with tenant_pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SELECT set_config('app.tenant_id', $1, true)", tenant_id)
                yield conn, True
    else:
        async with request.app.state.pool.acquire() as conn:
            yield conn, False
