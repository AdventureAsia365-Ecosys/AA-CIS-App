import logging, time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Column widths of shared.tenant_api_usage (endpoint varchar(100), method varchar(10)) -- schema limits, not policy.
# A longer value made the INSERT fail and the usage row was lost (S282, Sentry AA-CIS-API-M).
_ENDPOINT_WIDTH = 100
_METHOD_WIDTH = 10

async def track_api_call(
    pool,
    tenant_id: str = None,
    endpoint: str = None,
    method: str = None,
    status_code: int = None,
    response_ms: int = None,
    actor_type: str = "tenant",
    admin_user_id: str = None,
):
    """Log API call to tenant_api_usage for billing/usage metrics.

    AA-441 (migration 110): actor_type distinguishes tenant (/v1/*) vs admin (/admin/*)
    traffic in this shared table — tenant_id is nullable now, admin_user_id is the admin-side
    counterpart (FK to shared.admin_users, resolved from the x-admin-user-id header). Default
    stays actor_type='tenant' so every existing caller (rate_limit_middleware's /v1/* path)
    keeps writing rows exactly as before, unchanged.
    """
    endpoint = (endpoint or "")[:_ENDPOINT_WIDTH]
    method = (method or "")[:_METHOD_WIDTH]
    try:
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO shared.tenant_api_usage
                    (tenant_id, endpoint, method, status_code, response_ms, called_at,
                     actor_type, admin_user_id)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            """, tenant_id, endpoint, method, status_code, response_ms,
                datetime.now(timezone.utc), actor_type, admin_user_id)
    except Exception:
        # Non-blocking — never fail request due to billing error. A lost usage row is a metering gap, not a
        # request failure: warning with the traceback, not an error event (S282, Sentry AA-CIS-API-M).
        logger.warning("Billing track error", exc_info=True)
