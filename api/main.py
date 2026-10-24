from fastapi import FastAPI, HTTPException, Depends, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from fastapi.openapi.docs import (get_redoc_html, get_swagger_ui_html,
                                   get_swagger_ui_oauth2_redirect_html)
from contextlib import asynccontextmanager
import asyncio
import asyncpg
import redis.asyncio as aioredis  # noqa: F401 -- tests patch main.aioredis.from_url (shared module)
import os
import time
import uuid
from typing import Optional
import structlog

from api.routers.v1_notifications import router as v1_notifications_router
from api.routers._served import tour_not_served_handler
from shared.tour_liveness import TourNotServedError
from api.routers.auth import (
    _hash_api_key, _create_jwt, verify_jwt, verify_tenant_jwt,
    TenantLoginRequest, TenantLoginResponse,
    _verify_password, _create_admin_jwt,
    AdminLoginRequest, AdminLoginResponse, VerifyAdminResponse,
    STAFF_ROLES, BookingTokenResponse,
)
from api.core import booking_jwt  # AA-680
from api.routers.v1_tours import router as v1_tours_router
from api.routers.v1_tours import quota_router as v1_quota_router
from api.routers.v1_marketplace import router as v1_marketplace_router
from api.routers.v1_planning import router as v1_planning_router
from api.routers.v1_planning import slate_router as v1_slate_router
from api.routers.v1_angle_gate import router as v1_angle_gate_router
from api.routers.v1_content_writing import router as v1_content_writing_router
from api.routers.v1_pipeline import router as v1_pipeline_router
from api.routers.v1_publish import router as v1_publish_router
from api.routers.v1_integrations import router as v1_integrations_router
from api.routers.v1_trip_page import router as v1_trip_page_router  # AA-482
from api.routers.v1_trip_page import admin_router as admin_trip_page_router  # AA-482
from api.routers.v1_rules import router as v1_rules_router
from api.routers.v1_social import router as v1_social_router
from api.routers.admin import router as admin_router
from api.routers.admin_api_docs import router as admin_api_docs_router  # AA-577 — /admin/openapi.json
from api.routers.admin_data_model import router as admin_data_model_router  # AA-894 — /admin/data-model
from api.routers.admin import verify_admin_secret  # AA-577 — reused to gate Swagger/ReDoc/openapi.json
from api.routers.admin_pipeline import router as admin_pipeline_router
from api.routers.admin_settings import router as admin_settings_router
from api.routers.admin_atoms import router as admin_atoms_router
from api.routers.admin_a4 import router as admin_a4_router
from api.routers.admin_dashboard import router as admin_dashboard_router
from api.routers.admin_overview import router as admin_overview_router  # AA-664
from api.routers.admin_seo import router as admin_seo_router  # AA-705
from api.routers.admin_llm_ops import router as admin_llm_ops_router  # AA-518/AA-505
from api.routers.admin_segment_research import router as admin_segment_research_router  # AA-646
from api.routers.admin_job_runner import router as admin_job_runner_router  # AA-650
from api.routers.admin_budgets import router as admin_budgets_router  # AA-649
from api.routers.admin_decisions import router as admin_decisions_router  # AA-660
from api.routers.admin_platform import router as admin_platform_router  # AA-883
from api.routers.admin_photos import router as admin_photos_router, public_router as photos_public_router  # AA-708
from api.routers.admin_places import router as admin_places_router  # AA-712
from api.routers.admin_search import router as admin_search_router  # AA-752 — global ⌘K search
from api.routers.admin_waves import router as admin_waves_router  # AA-727 — rerun waves as objects
from api.routers.admin_content_quality import router as admin_content_quality_router  # AA-769
from api.routers.admin_tenant360 import router as admin_tenant360_router  # AA-766
from api.routers.admin_platform_ops import router as admin_platform_ops_router  # AA-884
from api.routers.admin_crm_console import router as admin_crm_console_router  # AA-941
from api.routers.admin_alert_recipients import router as admin_alert_recipients_router  # AA-913
from api.routers.v1_progress import router as v1_progress_router  # AA-637
from api.routers.catalog_http import (  # AA-976 — catalog.http.v1 for the partner CRM (M2M)
    CatalogProblem, catalog_problem_handler, router as catalog_http_router,
)
from api.middleware.rate_limit import rate_limit_middleware
from api.middleware.sentry_context import sentry_context_middleware
from api.middleware.staff_bearer import StaffBearerMiddleware  # AA-816 C3
from api.middleware.staff_audit import StaffAuditMiddleware  # AA-884
from api.middleware.request_id import RequestIdMiddleware, unhandled_error_handler  # AA-871
from api.core import readiness  # AA-884 — /health git_sha, /health/ready
from api.version import __version__  # AA-891 — release version (release-please bumps it)
from api.core import staff_identity  # AA-816 C3
from api.core import login_throttle  # AA-876
from api.core.staff_authz import NO_BOOKING_ROLES  # AA-878
from api.core.sentry import init_sentry
from api.schemas.public_api import Health, Jwks, Readiness, TenantSession  # AA-899
from services.acp_shared.audit_log import TenantAuditAction, write_audit_log
from shared.log_config import configure_for_process  # AA-871

# AA-871 — JSON lines (structlog + stdlib + uvicorn) with request_id / tenant_id / job ids; skipped under unit tests.
configure_for_process("cis-api")

logger = structlog.get_logger()
pool: asyncpg.Pool = None

init_sentry()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global pool
    # S241: socket timeouts + one retry (api/core/redis_client.py), so a hung Redis fails fast and every
    # fail-open Redis caller (rate limit, tenant-pool flags) answers instead of waiting for the 30 s API GW.
    from api.core.redis_client import build_api_redis
    redis = build_api_redis(
        f"redis://{os.environ.get('REDIS_HOST', 'aa-cis-dev-redis.wvp8vb.0001.usw1.cache.amazonaws.com')}:6379",
    )
    app.state.redis = redis
    pool = await asyncpg.create_pool(
        os.environ["DATABASE_URL"], min_size=2, max_size=10,
        # AA-824: one application_name per pool, so pg_stat_activity / pg_stat_statements show who
        # holds the connections on the shared RDS (data standards C2.2).
        server_settings={"application_name": "cis-api"},
    )
    # AA-859 wave 4 (S247) — timed acquires on the main pool: `aa859_main_pool_wait` (only waits >= 50 ms),
    # per-interval peak in-use / max wait on the gauge line, and a default acquire timeout
    # (`aa859_main_pool_acquire_timeout`). Same object, so every caller of app.state.pool is covered.
    from api.core.aa859_main_pool_gauge import instrument_main_pool, start_main_pool_gauge, stop_main_pool_gauge
    pool = instrument_main_pool(pool)
    app.state.pool = pool

    # AA-859 PR-S — 60 s INFO line `aa859_main_pool_gauge` (size / idle / max of the main pool), the
    # baseline for the wave-4 main-pool shrink decision. api only; cancelled before the pool closes.
    main_pool_gauge_task = start_main_pool_gauge(app)

    # AA-751 / AA-832 — every 300 s publish AA/RDS per-role connections and AA/Jobs/CIS backlog metrics
    # (one PutMetricData per namespace, line `ops_metrics_sampled`). Replaces the Lambda -> endpoint ->
    # job chain. AA-913: the same tick reconciles the platform alert recipients with SNS. api only, off when
    # ENVIRONMENT == "test", never raises; cancelled before the pool closes.
    from api.core.ops_metrics_sampler import start_ops_metrics_sampler, stop_ops_metrics_sampler
    ops_metrics_task = start_ops_metrics_sampler(app)

    # AA-854 C2 — load the WORKFORCE JWKS once in the background (log `identity_jwks_prefetched`), so the staff
    # token verifier has keys before the first request. No route uses the verifier yet (C3). Off when
    # ENVIRONMENT == "test", never raises, never delays start-up.
    from api.core.identity_config import start_cis_staff_jwks_prefetch, stop_cis_staff_jwks_prefetch
    identity_prefetch_task = start_cis_staff_jwks_prefetch()
    # AA-976 — same for the M2M pool (catalog.http.v1 callers); off under ENVIRONMENT == "test", never raises.
    from api.core.m2m_auth import start_m2m_jwks_prefetch, stop_m2m_jwks_prefetch
    m2m_prefetch_task = start_m2m_jwks_prefetch()

    # AA-544 Stage 1 — second pool, aa_app_user (non-BYPASSRLS), lazy (min_size 0, AA-817). None
    # only when the secret or the pool object cannot be made. Used only by routes that opt in via
    # api/core/aa544_tenant_pool.py::acquire_scoped_conn(), each behind its own Redis flag; all of
    # them fail closed (503, never the admin pool) while their flag is on — list in that module.
    from api.core.aa544_tenant_pool import create_tenant_pool
    app.state.tenant_pool = await create_tenant_pool()

    # AA-650 — durable job runner. The worker runs as its own ECS service (`python -m worker`,
    # AA-651); the API starts an in-process worker only when JOB_WORKER_IN_API is explicitly truthy
    # (local/dev), off by default (AA-735, ADR 0003 nac 5).
    job_worker, job_worker_task = None, None
    from shared.jobs.worker import Worker, in_api_enabled, load_kinds
    if in_api_enabled():
        try:
            load_kinds()
            job_worker = Worker(pool, resources={"redis": redis})
            job_worker_task = asyncio.create_task(job_worker.run(), name="job-worker")
        except Exception as e:
            logger.warning("job_worker_start_failed", error=repr(e))

    yield

    # AA-650 — stop claiming, let running jobs finish within the grace period, release the rest
    # back to the queue (not counted as an attempt) before the pool closes.
    if job_worker is not None:
        await job_worker.shutdown()
        if job_worker_task is not None:
            await asyncio.gather(job_worker_task, return_exceptions=True)

    await stop_ops_metrics_sampler(ops_metrics_task)
    await stop_cis_staff_jwks_prefetch(identity_prefetch_task)
    await stop_m2m_jwks_prefetch(m2m_prefetch_task)
    await stop_main_pool_gauge(main_pool_gauge_task)
    await pool.close()
    if app.state.tenant_pool is not None:
        await app.state.tenant_pool.close()
    await redis.aclose()

# AA-577 — docs_url/redoc_url/openapi_url all disabled here (were the FastAPI defaults: /docs,
# /redoc, /openapi.json) so none of the 4 routes auto-register unprotected; re-registered by hand
# below, each behind require_admin_secret_for_docs().
app = FastAPI(
    title="AA-CIS API",
    version=__version__,
    description="Adventure Asia Content Intelligence System",
    lifespan=lifespan,
    docs_url=None, redoc_url=None, openapi_url=None,
)


def _is_publicly_deployed() -> bool:
    """AA-577 STEP0 — true only for the real, internet-reachable ECS deployment (the one bots
    scan), never for a developer's laptop or the GitHub Actions CI runner. Deliberately NOT
    `ENVIRONMENT` (`os.getenv("ENVIRONMENT", "dev")`, api/core/sentry.py's own convention):
    Terraform sets `environment = "dev"` for the ONE real AWS deployment this app has today (no
    separate staging/prod cluster exists — confirmed via `aws ecs list-clusters`) — gating on
    `ENVIRONMENT == "production"` would never be true and would leave Swagger fully exposed,
    exactly the opposite of this fix. `AWS_EXECUTION_ENV` is auto-injected by AWS on every real
    ECS/Fargate task (confirmed live: `AWS_ECS_FARGATE` on the real running task) and absent on
    both a local machine and CI — a reliable "is this the real live thing" signal that needs no
    Terraform change and can't be spoofed by an unauthenticated caller (it's never read from a
    request, only from this process's own environment)."""
    return bool(os.environ.get("AWS_EXECUTION_ENV"))


# AA-876 — no CORS on a deployed task. No browser calls this API: every caller is a server (the admin, portal
# and Booking BFFs, TripPlanner's server components and Lambdas), so a browser preflight from any origin gets no
# Access-Control-Allow-Origin. Local dev keeps the two localhost front-ends.
_LOCAL_CORS_ORIGINS = ["http://localhost:3000", "http://localhost:3001"]


def cors_allow_origins(environ=None) -> list[str]:
    """AA-876 — origins the CORS middleware allows: none on the real ECS deployment (same
    AWS_EXECUTION_ENV signal as _is_publicly_deployed), the localhost front-ends elsewhere."""
    env = os.environ if environ is None else environ
    if env.get("AWS_EXECUTION_ENV"):
        return []
    return list(_LOCAL_CORS_ORIGINS)


_cors_origins = cors_allow_origins()
if _cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


def require_admin_secret_for_docs(x_admin_secret: str = Header(None)):
    """AA-577 — Swagger UI/ReDoc/the OpenAPI schema JSON were public on the real deployment,
    handing a bot the full API surface (route names, params) for free — a real CloudWatch-
    confirmed finding, not a theoretical one. Gated behind the same X-Admin-Secret mechanism
    every /admin/* route already uses (verify_admin_secret(), api/routers/admin.py) — but ONLY
    when `_is_publicly_deployed()`, per Nghiệp's explicit decision (10/09/2026): local/CI stays
    fully public, no secret needed, for coding/testing convenience."""
    if _is_publicly_deployed():
        verify_admin_secret(x_admin_secret)


@app.get("/openapi.json", include_in_schema=False)
async def get_openapi_json(_: None = Depends(require_admin_secret_for_docs)):
    return app.openapi()


@app.get("/docs", include_in_schema=False)
async def get_swagger_docs(_: None = Depends(require_admin_secret_for_docs)):
    return get_swagger_ui_html(
        openapi_url="/openapi.json", title=f"{app.title} - Swagger UI",
        oauth2_redirect_url="/docs/oauth2-redirect",
    )


@app.get("/docs/oauth2-redirect", include_in_schema=False)
async def get_swagger_oauth2_redirect(_: None = Depends(require_admin_secret_for_docs)):
    return get_swagger_ui_oauth2_redirect_html()


@app.get("/redoc", include_in_schema=False)
async def get_redoc_docs(_: None = Depends(require_admin_secret_for_docs)):
    return get_redoc_html(openapi_url="/openapi.json", title=f"{app.title} - ReDoc")


# AA-577 follow-up — GET /admin/openapi.json for the admin page /admin/api-docs (through the BFF,
# admin role). Included before every other router so no later GET /admin/{x} route can shadow it.
app.include_router(admin_api_docs_router)
# AA-894 scope 2 — GET /admin/data-model (admin page /admin/data-model), early for the same reason.
app.include_router(admin_data_model_router)


app.include_router(v1_tours_router)
app.include_router(v1_quota_router)  # AA-489 — GET /v1/quota, real replacement for AA-428's dead endpoint
app.include_router(v1_marketplace_router)  # AA-444 — tenant Marketplace view
app.include_router(v1_planning_router)  # AA-448 — T7 Content Planning (preview only so far)
app.include_router(v1_slate_router)  # AA-511 — the Slate (GET /v1/slate, POST /v1/subjects/{id}/pick)
# AA-859 wave 4 (S247): v1_route_hub_router (GET /v1/routes, /v1/hubs, GET/POST /v1/route-picks) removed: 0 callers
# in CIS frontend / portal, TripPlanner, Booking or Infra, 0 requests in 30 days of /ecs/aa-cis-dev logs, and
# /v1/routes was broken anyway (a tuple bound to text[]). acp_contract.route_pick stays (Dead Table Registry candidate).
app.include_router(v1_angle_gate_router)  # AA-449 — T8 Angle Gate
app.include_router(v1_content_writing_router)  # AA-450 — T9 Content Writing + T10-inline
# AA-579: v1_exports_router (POST /v1/exports) removed — S8/S9 tàn dư (21/04/2026), 0 traffic
# tenant thật trong 14 ngày CloudWatch, 0 UI caller. Xem AA-579 (Linear) trước khi khôi phục.
app.include_router(v1_pipeline_router)
app.include_router(v1_publish_router)  # AA-455 bước 1 — tenant self-unpublish (publish_log)
app.include_router(v1_notifications_router)  # AA-986 L3 — tenant bell: GET /v1/notifications, mark read
app.include_router(v1_integrations_router)  # AA-457 [T11 PR1] — tenant WordPress credentials
app.include_router(v1_trip_page_router)  # AA-482 — GET /v1/trip/{tour_id}, public page data
app.include_router(admin_trip_page_router)  # AA-482 — /admin/trip-pages/{publish,recheck}
app.include_router(v1_rules_router)
app.include_router(v1_social_router)
app.include_router(admin_router)
app.include_router(admin_pipeline_router)
app.include_router(admin_settings_router)
app.include_router(admin_atoms_router)
app.include_router(admin_a4_router)
app.include_router(admin_dashboard_router)  # AA-527 (bổ sung) — Segment/Score/Route-Hub/Slate audit panels
app.include_router(admin_overview_router)  # AA-664 — /admin/overview (whole-system snapshot, cached 60s)
app.include_router(admin_seo_router)  # AA-705 — /admin/seo-intelligence (demand, keywords, PAA, gaps, spend)
app.include_router(admin_llm_ops_router)  # AA-518/AA-505 — /admin/llm-config, /admin/llm-usage/*
app.include_router(admin_segment_research_router)  # AA-646 — /admin/segment-research/{preview,run,status}
app.include_router(admin_job_runner_router)  # AA-650 — /admin/job-runner/*
app.include_router(admin_budgets_router)  # AA-649 — /admin/budgets (cost guard limits)
app.include_router(admin_decisions_router)  # AA-660 — /admin/decisions (Jev per stage, cost)
app.include_router(admin_platform_router)  # AA-883 — /admin/platform/staff (read-only staff directory)
app.include_router(admin_platform_ops_router)  # AA-884 — /admin/platform/{apps,audit,cost}
app.include_router(admin_crm_console_router)  # AA-941 — /admin/platform/crm/* (CRM L1 console relay, no include prefix)
app.include_router(admin_alert_recipients_router)  # AA-913 — /admin/platform/alert-recipients (no include prefix)
app.include_router(admin_photos_router)  # AA-708 — /admin/photos (Drive photo sync, matching)
app.include_router(admin_places_router)  # AA-712 — /admin/places/backfill (place pipeline)
app.include_router(admin_search_router)  # AA-752 — GET /admin/search (global ⌘K palette)
app.include_router(admin_waves_router)  # AA-727 — /admin/waves (expected set, report, check)
app.include_router(admin_content_quality_router)  # AA-769 — /admin/content-quality (T2/T9 quality, place cards)
app.include_router(admin_tenant360_router)  # AA-766 — /admin/tenants/{id}/{cost,quality,markets}
app.include_router(photos_public_router)  # AA-708 — /content/photos/{id} (public image redirect)
app.include_router(v1_progress_router)  # AA-637 — GET /v1/progress/{kind}/{job_id}
app.include_router(catalog_http_router)  # AA-976 — /catalog/v1 (catalog.http.v1, M2M cis.catalog/read)

app.middleware("http")(rate_limit_middleware)
# AA-816 C3 — registered after the rate limiter so it wraps it (Starlette: last registered = outermost): a
# Cognito Bearer on a staff path rewrites x-admin-role / x-admin-user-id before the rate limiter's admin
# tracking and the routes read them. Legacy HS256 Bearer and secret-only requests pass through untouched, except the
# AA-816 C9 Preview credential, whose x-admin-role it caps at viewer (api/core/staff_credential.py).
# AA-884 — one acp_shared.audit_log row per non-GET staff call (secret-bearing request on a staff path). Registered
# before StaffBearerMiddleware so it sits INSIDE it: it sees the verified Cognito identity StaffBearer puts on the
# (copied) scope it passes down, and the route template the router writes into that scope. A request StaffBearer
# refuses (401/403/503 on a bad Cognito token) never reaches it; those are logged as staff_bearer_refused.
app.add_middleware(StaffAuditMiddleware)
app.add_middleware(StaffBearerMiddleware)
app.middleware("http")(sentry_context_middleware)
# AA-871 — registered LAST = outermost user middleware: every response (401/403/429 from the middlewares above
# included) carries X-Request-ID and every log line of the request carries request_id. The unhandled 500 is
# answered outside all user middleware, so its handler adds the header itself.
# AA-986 L3 — new tenant work on a tour AA no longer serves: 409 tour_discontinued / tour_paused, plain sentence.
app.add_exception_handler(TourNotServedError, tour_not_served_handler)
app.add_exception_handler(CatalogProblem, catalog_problem_handler)  # AA-976 — contract Problem body
app.add_exception_handler(Exception, unhandled_error_handler)
app.add_middleware(RequestIdMiddleware)

def get_pool() -> asyncpg.Pool:
    if not pool:
        raise HTTPException(status_code=503, detail="DB not ready")
    return pool

@app.post("/auth/tenant-login", response_model=TenantLoginResponse, tags=["auth"])
async def tenant_login(
    request: Request,
    body: TenantLoginRequest,
    db: asyncpg.Pool = Depends(get_pool),
):
    # AA-876 — per-IP failure + attempt limits before any DB read (api/core/login_throttle.py). No per-key
    # backoff: an API key has far too much entropy to guess, and a per-key wait would let anyone lock a tenant out.
    await login_throttle.check(request, "tenant")
    if not body.api_key or len(body.api_key) < 10:
        raise HTTPException(status_code=400, detail="Invalid API key format")
    key_hash = _hash_api_key(body.api_key)
    async with db.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT tenant_id::text, name, plan_tier FROM shared.tenants "
            "WHERE api_key_hash = $1 AND is_active = true",
            key_hash,
        )
    if not row:
        await login_throttle.record_failure(request, "tenant")
        raise HTTPException(status_code=401, detail="Invalid API key")
    token = _create_jwt(row["tenant_id"], row["name"], row["plan_tier"], client=body.client)

    # AA-559 — semantic tenant-activity log. Best-effort/swallowed (unlike every other new call
    # site this issue adds): login has no pre-existing DB write of its own to be atomic with, and
    # a real tenant must never be locked out of the portal because an audit INSERT hiccuped.
    try:
        await write_audit_log(
            db, tenant_id=row["tenant_id"], actor=f"tenant:{row['tenant_id']}",
            action=TenantAuditAction.TENANT_LOGIN, resource_type="tenant",
            resource_id=row["tenant_id"],
            # AA-760 — a portal sign-in says so (its token is a portal session, not metered as API calls).
            details={"login_method": "api_key", **({"client": body.client} if body.client else {})},
        )
    except Exception as exc:
        logger.error("tenant_login_audit_log_failed", tenant_id=row["tenant_id"], error=str(exc))

    return TenantLoginResponse(
        token=token,
        tenant_id=row["tenant_id"],
        tenant_name=row["name"],
        plan_tier=row["plan_tier"],
    )

async def _session_token_from_body(request: Request) -> str:
    """The ``token`` of a ``{"token": "..."}`` body (verify-tenant / verify-admin), or "" when it is missing or not
    a string. A body that is not JSON or not an object is a client error: 401 "Invalid token", never a 500 from
    ``request.json()`` / ``.get`` (AA-816 S2 live probe: an empty POST answered 500)."""
    try:
        body = await request.json()
    except (ValueError, RecursionError):
        # ValueError: JSONDecodeError (empty or not JSON) and UnicodeDecodeError. RecursionError: a deeply nested
        # body such as b"[" * 100000 makes json.loads recurse past the limit (a RuntimeError, not a ValueError).
        raise HTTPException(status_code=401, detail="Invalid token") from None
    if not isinstance(body, dict):
        raise HTTPException(status_code=401, detail="Invalid token")
    token = body.get("token", "")
    return token if isinstance(token, str) else ""


@app.post("/auth/verify-tenant", tags=["auth"], response_model=TenantSession)  # AA-899: model
async def verify_tenant(request: Request):
    # verify_tenant_jwt, not verify_jwt: staff HS256 tokens share JWT_SECRET but carry no name / plan_tier, so a
    # staff token used to end in a KeyError (500); it is now refused with 403 "Tenant token required".
    payload = verify_tenant_jwt(await _session_token_from_body(request))
    return {
        "valid": True,
        "tenant_id": payload["sub"],
        "name": payload["name"],
        "plan_tier": payload["plan_tier"],
    }

@app.post("/auth/admin-login", response_model=AdminLoginResponse, tags=["auth"])
async def admin_login(
    request: Request,
    body: AdminLoginRequest,
    db: asyncpg.Pool = Depends(get_pool),
):
    """
    Per-user admin/reviewer login (AA-232). Verifies username+password against
    shared.admin_users (bcrypt). Constant-shape 401 on any failure — unknown
    user, wrong password, and inactive account all look identical (no
    enumeration signal). Helpers/models live in api.routers.auth.
    """
    if not body.username or not body.password:
        raise HTTPException(status_code=400, detail="Username and password required")
    # AA-876 — per-IP limits + per-(username, IP) backoff, before the DB read and bcrypt (api/core/login_throttle.py).
    await login_throttle.check(request, "admin", body.username)
    async with db.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id::text, username, password_hash, role::text, is_active "
            "FROM shared.admin_users WHERE username = $1",
            body.username,
        )
    # Always run a bcrypt compare to keep response timing constant: on no row, and on a Cognito-only row
    # whose password_hash is NULL (migration 247, AA-816), _verify_password compares against a dummy
    # hash and returns False, so both end in the same 401 as a wrong password (never a 500).
    stored_hash = row["password_hash"] if row else None
    # AA-876 — bcrypt (cost 12, ~250 ms) in the threadpool, so a login burst no longer stalls the event loop
    # for every other request on the task.
    password_ok = await run_in_threadpool(_verify_password, body.password, stored_hash)
    if not row or not row["is_active"] or stored_hash is None or not password_ok:
        await login_throttle.record_failure(request, "admin", body.username)
        raise HTTPException(status_code=401, detail="Invalid username or password")
    await login_throttle.record_success(request, "admin", body.username)
    token = _create_admin_jwt(row["id"], row["username"], row["role"])
    return AdminLoginResponse(
        token=token,
        admin_id=row["id"],
        username=row["username"],
        role=row["role"],
    )

# AA-882 (GĐ3 review, S241 §5.2) — /auth/verify-admin re-reads the staff row, so a deactivated or
# demoted user loses the old role within this TTL (plus the BFF's own verify cache, lib/auth-server.ts)
# instead of at the 24 h JWT expiry. Keyed by admin id; a miss (unknown or inactive) is cached too.
_STAFF_STATE_TTL_S = 60.0
_STAFF_STATE_MAX = 2048
# Legacy (HS256) path only; the Cognito path has its own (iss, sub) cache with the same TTL in
# api/core/staff_identity.py (shared with the staff bearer middleware, which can not import this module).
_staff_state_cache: dict[str, tuple[float, Optional[tuple[str, str, Optional[str], Optional[str]]]]] = {}


async def _current_staff(
    db: asyncpg.Pool, admin_id: str,
) -> Optional[tuple[str, str, Optional[str], Optional[str]]]:
    """(username, role, email, display_name) of an active staff account, or None; cached for
    _STAFF_STATE_TTL_S."""
    now = time.monotonic()
    hit = _staff_state_cache.get(admin_id)
    if hit and hit[0] > now:
        return hit[1]
    row = await _load_active_staff(db, admin_id)
    state = (row["username"], row["role"], row.get("email"), row.get("display_name")) if row else None
    if len(_staff_state_cache) >= _STAFF_STATE_MAX:
        _staff_state_cache.clear()
    _staff_state_cache[admin_id] = (now + _STAFF_STATE_TTL_S, state)
    return state


@app.post("/auth/verify-admin", response_model=VerifyAdminResponse, tags=["auth"])
async def verify_admin(request: Request, db: asyncpg.Pool = Depends(get_pool)):
    """
    Verify a staff session token (server-side, called by the Next.js middleware and BFF) — mirrors
    /auth/verify-tenant. The path is chosen by the unverified `iss` (AA-816 C3):

    - Cognito access token (`iss` = the WORKFORCE issuer): WORKFORCE x {cis-admin, breakglass} verifier (C2), then
      api/core/staff_identity.py: a `cis-*` group is required (403 otherwise), the row is found by
      (cognito_iss, cognito_sub) or bound once by email on the first sign-in, 401 when unknown or inactive,
      role = highest `cis-*` group (a difference with the DB role is logged as drift), last_login_at set
      (best effort).
    - Any other `iss` (EXTERNAL portal, Booking RS256): 401, never a staff session.
    - Legacy HS256 token (no `iss`): gated by LEGACY_ADMIN_LOGIN_ENABLED (default OFF since AA-816 S2). Re-reads
      shared.admin_users (cached 60 s): 401 when the account is gone or inactive, and the role returned is the
      DB role, not the JWT claim, so the BFF forwards the current role (x-admin-role) and staff_policy
      enforces it within the cache TTLs, not at the 24 h token expiry.
    """
    token = await _session_token_from_body(request)
    kind = staff_identity.token_kind(token)
    if kind == staff_identity.AUTH_OTHER:
        # A JWT from another issuer (EXTERNAL portal, CIS-minted Booking RS256): never a CIS staff session.
        logger.warning("verify_admin_refused", auth=staff_identity.AUTH_OTHER, status=401, reason="foreign_issuer")
        raise HTTPException(status_code=401, detail="Invalid staff token")
    if kind == staff_identity.AUTH_COGNITO:
        try:
            identity = await staff_identity.authenticate(db, token, touch_login=True)
        except staff_identity.StaffAuthError as exc:
            logger.warning("verify_admin_refused", auth=staff_identity.AUTH_COGNITO, status=exc.status,
                           reason=exc.reason)
            raise HTTPException(status_code=exc.status, detail=exc.detail) from None
        return VerifyAdminResponse(
            admin_id=identity.admin_id, username=identity.username, role=identity.role, valid=True,
            email=identity.email, display_name=identity.display_name, auth=staff_identity.AUTH_COGNITO,
        )

    if not staff_identity.legacy_login_enabled():
        logger.warning("verify_admin_refused", auth=staff_identity.AUTH_LEGACY, reason="legacy_disabled")
        raise HTTPException(status_code=401, detail="Password sessions are switched off; sign in again")
    payload = verify_jwt(token)
    if payload.get("role") not in STAFF_ROLES:
        # Explicit whitelist — same secret/alg means a tenant JWT would decode fine here.
        raise HTTPException(status_code=401, detail="Not an admin token")
    state = await _current_staff(db, str(payload.get("sub", "")))
    if state is None or state[1] not in STAFF_ROLES:
        logger.warning("verify_admin_refused", admin_id=str(payload.get("sub")),
                       session_role=payload.get("role"),
                       reason="inactive_or_unknown" if state is None else "role_not_staff")
        raise HTTPException(status_code=401, detail="Staff user is inactive or unknown")
    username, role, email, display_name = state
    return VerifyAdminResponse(
        admin_id=payload["sub"],
        username=username,
        role=role,
        valid=True,
        email=email,
        display_name=display_name,
        auth=staff_identity.AUTH_LEGACY,
    )

# ── AA-680 — CIS as staff identity provider for AA-Booking (root ADR 0004, O1) ──────────────
# POST /auth/booking-token swaps a valid CIS staff session (cis_admin_token cookie or a Bearer
# header holding the same HS256 staff JWT) for a 15-minute RS256 JWT with aud=booking. The JWKS that
# Booking verifies it with is public. Key material: api/core/booking_jwt.py (Secrets Manager).

_BOOKING_JWKS_CACHE_CONTROL = "public, max-age=600"


def _staff_session_token(request: Request) -> str:
    auth = request.headers.get("Authorization", "")
    if auth[:7].lower() == "bearer " and auth[7:].strip():
        return auth[7:].strip()
    return request.cookies.get("cis_admin_token", "")


async def _load_active_staff(db: asyncpg.Pool, admin_id: str):
    """The staff row behind a session, or None when it is gone or deactivated. Re-read on every
    Booking token so a deactivated user or a changed role stops at the next mint (<= 15 min),
    not at the 24 h CIS session expiry."""
    try:
        uid = uuid.UUID(admin_id)
    except (ValueError, TypeError, AttributeError):
        return None
    async with db.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id::text AS id, username, role::text AS role, email, display_name "
            "FROM shared.admin_users WHERE id = $1 AND is_active",
            uid,
        )
    return row


@app.post("/auth/booking-token", response_model=BookingTokenResponse, tags=["auth"])
async def booking_token(request: Request, db: asyncpg.Pool = Depends(get_pool)):
    token = _staff_session_token(request)
    if not token:
        raise HTTPException(status_code=401, detail="Staff session required")
    payload = verify_jwt(token)
    if payload.get("role") not in STAFF_ROLES:
        raise HTTPException(status_code=401, detail="Not a staff session")
    row = await _load_active_staff(db, payload.get("sub"))
    if not row or row["role"] not in STAFF_ROLES:
        # Visible trail for repeated mint attempts by a deactivated, demoted or unknown user.
        logger.warning("booking_token_refused", admin_id=str(payload.get("sub")),
                       session_role=payload.get("role"),
                       reason="inactive_or_unknown" if not row else "role_not_staff")
        raise HTTPException(status_code=401, detail="Staff user is inactive or unknown")
    if row["role"] in NO_BOOKING_ROLES:
        # AA-878: a read-only CIS role has no Booking access; Booking has no mapping for it, so never mint one.
        logger.warning("booking_token_refused", admin_id=row["id"], cis_role=row["role"], reason="role_no_booking")
        raise HTTPException(status_code=403, detail="This staff role has no access to Booking")
    try:
        # Off the event loop: a cache refresh is a blocking Secrets Manager call.
        keys = await booking_jwt.get_signing_keys_async()
    except booking_jwt.SigningKeyUnavailable:
        raise HTTPException(status_code=503, detail="Booking token signing key is not configured")
    jti = str(uuid.uuid4())
    access_token, expires_in = booking_jwt.mint_booking_token(
        admin_id=row["id"], username=row["username"], role=row["role"], jti=jti, keys=keys,
    )
    # jti + kid let a token Booking sees be traced back to this mint.
    logger.info("booking_token_issued", admin_id=row["id"], cis_role=row["role"], jti=jti,
                kid=keys.kid)
    return BookingTokenResponse(access_token=access_token, token_type="Bearer", expires_in=expires_in)


@app.get("/auth/.well-known/jwks.json", tags=["auth"], responses={200: {"model": Jwks}})  # AA-899: doc
async def booking_jwks():
    try:
        body = booking_jwt.jwks(await booking_jwt.get_signing_keys_async())
    except booking_jwt.SigningKeyUnavailable:
        raise HTTPException(status_code=503, detail="Booking token signing key is not configured")
    return JSONResponse(body, headers={"Cache-Control": _BOOKING_JWKS_CACHE_CONTROL})


@app.get("/health", response_model=Health)  # AA-899: model
async def health():
    # AA-884: git_sha = the commit the image was built from (deploy smoke asserts it equals the deployed SHA).
    # AA-891: version = the release version in the image (api/version.py, bumped by release-please; the deploy smoke
    # asserts it equals .release-please-manifest.json of the deployed commit).
    # `status` stays the first field and "ok" (ALB health check and CI smoke read it).
    return {"status": "ok", "service": "aa-cis-api", "version": __version__, "git_sha": readiness.git_sha()}


@app.get("/health/ready", responses={  # AA-899: documents the JSONResponse body (200 and 503)
    200: {"model": Readiness}, 503: {"model": Readiness, "description": "Not ready"}})
async def health_ready():
    """AA-884 — DB answers and the newest numeric CIS migration is at least readiness.REQUIRED_CIS_SCHEMA. 200 or 503,
    cached 5 s in-process (public route)."""
    status, body = await readiness.readiness(pool)
    return JSONResponse(body, status_code=status)
