import json, time, os, logging, uuid
import structlog
from fastapi import Request
from fastapi.responses import JSONResponse
from api.routers.auth import is_portal_session, verify_jwt
from api.core.secret_compare import secrets_equal
from api.core.staff_credential import principal_verified
from shared.services.billing_service import track_api_call

logger = logging.getLogger(__name__)

# Fallback only — used when the shared.tenants lookup below (Redis cache miss +
# DB error, or Redis itself down) can't be completed. Real per-tenant limits
# live in shared.tenants.rate_limit_rpm (AA-432) — see _get_tenant_meta().
PLAN_RPM = {
    "starter": 60,
    "growth": 300,
    "business": 1000,
    "enterprise": 9999,
}

# AA-432: 30s TTL cache for {rate_limit_rpm, is_active} per tenant, keyed in Redis
# (not in-process — multiple ECS tasks would each hold a stale copy otherwise).
# This is the "revoke" mechanism: flipping shared.tenants.is_active=false blocks
# the tenant within this TTL, without waiting for their JWT (24h) to expire.
# 30s was picked as "fast enough to feel like revoke, slow enough that a normal
# request pattern doesn't add a DB round-trip per call" — not load-tested; adjust
# if it turns out to be too slow/fast in practice.
_TENANT_META_TTL_S = 30


async def _get_tenant_meta(request: Request, tenant_id: str) -> dict | None:
    """Returns {"rate_limit_rpm": int, "is_active": bool}, or None if both the
    Redis cache and the DB lookup failed (caller falls back to PLAN_RPM/fail-open
    — see call site)."""
    redis = request.app.state.redis
    cache_key = f"tenant_meta:{tenant_id}"
    try:
        cached = await redis.get(cache_key)
        if cached is not None:
            return json.loads(cached)
    except Exception as e:
        logger.warning(f"tenant_meta Redis read error: {e}")

    try:
        async with request.app.state.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT rate_limit_rpm, is_active FROM shared.tenants WHERE tenant_id = $1::uuid",
                tenant_id,
            )
    except Exception as e:
        logger.error(f"tenant_meta DB error: {e}")
        return None
    if row is None:
        return None

    meta = {"rate_limit_rpm": row["rate_limit_rpm"], "is_active": row["is_active"]}
    try:
        await redis.set(cache_key, json.dumps(meta), ex=_TENANT_META_TTL_S)
    except Exception as e:
        logger.warning(f"tenant_meta Redis write error: {e}")
    return meta


async def rate_limit_middleware(request: Request, call_next):
    path = request.url.path

    # AA-441 (bug #1, AA-438-04/AA-439-00 #13): this middleware used to unconditionally skip
    # every non-/v1/* path before checking auth at all, so shared.tenant_api_usage — the sole
    # data source for the admin Dashboard's "Pipeline Health" panel — never recorded a single
    # row of admin (/admin/*) activity. /admin/* traffic is tracked (not rate-limited — staff
    # aren't subject to a tenant RPM bucket) via a separate branch below.
    if path.startswith("/admin/"):
        return await _track_admin_call(request, call_next)

    if not path.startswith("/v1/"):
        return await call_next(request)

    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return await call_next(request)

    try:
        payload = verify_jwt(auth.split(" ", 1)[1])
    except Exception as e:
        logger.warning(f"Rate limit JWT error: {e}")
        return await call_next(request)

    tenant_id = payload["sub"]
    plan_tier = payload.get("plan_tier", "starter")
    # AA-871 — the verified tenant on every log line the route writes (call_next starts the route in a new task that
    # copies this context). Never outlives the request: this dispatch runs in a task of this request only (in api.main,
    # the child task started by the outer `sentry_context_middleware`), which ends with it. request.state carries the
    # id out to the request-id middleware's `request_completed` line, which runs in the outer task and does not see
    # this binding.
    structlog.contextvars.bind_contextvars(tenant_id=str(tenant_id))
    request.state.tenant_id = str(tenant_id)
    # AA-760 — a portal session (the portal's own page loads and its API playground) is rate-limited like any
    # tenant call but NOT metered: shared.tenant_api_usage is the "API calls" count on the tenant's Billing page,
    # which counts calls the tenant's own systems make. Every portal call stays in the log: the request-id
    # middleware's `request_completed` line carries tenant_id + client (method, path, route, status, duration).
    portal = is_portal_session(payload)
    request.state.tenant_client = "portal" if portal else "api"

    # AA-432: revoke + real per-tenant rate limit. tenant_meta is None only when
    # BOTH the Redis cache and the DB are unreachable — fail OPEN in that case
    # (fall back to the JWT's plan_tier + PLAN_RPM, old behavior) rather than
    # taking down all tenant traffic on a transient infra blip. is_active=false
    # is a hard block regardless — that check only runs when the lookup actually
    # succeeded, so it's never bypassed by a fail-open path.
    tenant_meta = await _get_tenant_meta(request, tenant_id)
    if tenant_meta is not None and not tenant_meta["is_active"]:
        return JSONResponse(
            status_code=403,
            content={"detail": "Tenant is deactivated"},
        )
    rpm_limit = (
        tenant_meta["rate_limit_rpm"] if tenant_meta is not None
        else PLAN_RPM.get(plan_tier, 60)
    )

    # Rate limit check
    try:
        redis = request.app.state.redis
        window = int(time.time() // 60)
        key = f"ratelimit:{tenant_id}:{window}"
        count = await redis.incr(key)
        if count == 1:
            await redis.expire(key, 120)
    except Exception as e:
        logger.error(f"Rate limit Redis error: {e}")
        count = 0

    if count > rpm_limit:
        retry_after = 60 - int(time.time() % 60)
        # Track 429 (machine calls only — AA-760)
        if not portal:
            await track_api_call(
                request.app.state.pool,
                tenant_id, request.url.path,
                request.method, 429
            )
        return JSONResponse(
            status_code=429,
            content={"detail": f"Rate limit exceeded. Plan: {plan_tier}, limit: {rpm_limit} RPM"},
            headers={"Retry-After": str(retry_after)}
        )

    # Process request + measure time
    start = time.time()
    response = await call_next(request)
    response_ms = int((time.time() - start) * 1000)

    # Track the call (machine calls only — AA-760: a portal session is not an API call)
    if not portal:
        await track_api_call(
            request.app.state.pool,
            tenant_id, request.url.path,
            request.method, response.status_code,
            response_ms
        )

    response.headers["X-RateLimit-Limit"] = str(rpm_limit)
    response.headers["X-RateLimit-Remaining"] = str(max(0, rpm_limit - count))
    response.headers["X-RateLimit-Plan"] = plan_tier
    return response


async def _track_admin_call(request: Request, call_next):
    """AA-441: log /admin/* activity into shared.tenant_api_usage (actor_type='admin',
    migration 110) so the Dashboard's Pipeline Health panel reflects real admin usage instead of
    reading permanently idle.

    Only tracks requests that actually authenticate as staff (X-Admin-Secret match, or since AA-816 C9 a
    principal the staff bearer middleware verified: Cognito Bearer or the Preview credential) — an
    unauthenticated/invalid request is passed through untracked, same permissive pattern the
    /v1/* branch above uses for a missing/invalid Bearer token, and still gets its real 401/403
    from the route's own auth dependency. Not rate-limited: staff aren't subject to a tenant RPM
    bucket, this branch only measures + records.
    """
    admin_secret = os.environ.get("ADMIN_SECRET", "")
    x_admin_secret = request.headers.get("X-Admin-Secret", "")
    is_secret = bool(admin_secret) and secrets_equal(x_admin_secret, admin_secret)
    if not is_secret and not principal_verified():
        return await call_next(request)

    # AA-232: verified admin identity, forwarded by the admin BFF proxy
    # (frontend/app/api/admin/[...path]/route.ts) as x-admin-user-id — sent on every admin
    # request already, just never read by any backend code until this fix. Legacy
    # ADMIN_SECRET-only callers (no BFF in front of them) won't send it — tracked with
    # admin_user_id=NULL in that case (see migration 110's actor CHECK constraint).
    raw_admin_user_id = request.headers.get("x-admin-user-id", "")
    admin_user_id = None
    if raw_admin_user_id:
        try:
            admin_user_id = str(uuid.UUID(raw_admin_user_id))
        except ValueError:
            logger.warning(f"Ignoring malformed x-admin-user-id: {raw_admin_user_id!r}")

    start = time.time()
    response = await call_next(request)
    response_ms = int((time.time() - start) * 1000)

    await track_api_call(
        request.app.state.pool,
        tenant_id=None,
        endpoint=request.url.path,
        method=request.method,
        status_code=response.status_code,
        response_ms=response_ms,
        actor_type="admin",
        admin_user_id=admin_user_id,
    )
    return response
