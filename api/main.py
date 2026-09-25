from fastapi import FastAPI, HTTPException, Depends, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import (get_redoc_html, get_swagger_ui_html,
                                   get_swagger_ui_oauth2_redirect_html)
from contextlib import asynccontextmanager
import asyncio
import asyncpg
import redis.asyncio as aioredis
import os
import structlog

from api.routers.auth import (
    _hash_api_key, _create_jwt, verify_jwt,
    TenantLoginRequest, TenantLoginResponse,
    _verify_password, _create_admin_jwt,
    AdminLoginRequest, AdminLoginResponse, VerifyAdminResponse,
)
from api.routers.v1_tours import router as v1_tours_router
from api.routers.v1_tours import quota_router as v1_quota_router
from api.routers.v1_marketplace import router as v1_marketplace_router
from api.routers.v1_planning import router as v1_planning_router
from api.routers.v1_planning import slate_router as v1_slate_router
from api.routers.v1_route_hub import router as v1_route_hub_router
from api.routers.v1_angle_gate import router as v1_angle_gate_router
from api.routers.v1_content_writing import router as v1_content_writing_router
from api.routers.v1_pipeline import router as v1_pipeline_router
from api.routers.v1_competitors import router as v1_competitors_router
from api.routers.v1_publish import router as v1_publish_router
from api.routers.v1_integrations import router as v1_integrations_router
from api.routers.v1_trip_page import router as v1_trip_page_router  # AA-482
from api.routers.v1_trip_page import admin_router as admin_trip_page_router  # AA-482
from api.routers.v1_rules import router as v1_rules_router
from api.routers.v1_social import router as v1_social_router
from api.routers.admin import router as admin_router
from api.routers.admin import verify_admin_secret  # AA-577 — reused to gate Swagger/ReDoc/openapi.json
from api.routers.admin_pipeline import router as admin_pipeline_router
from api.routers.admin_settings import router as admin_settings_router
from api.routers.admin_atoms import router as admin_atoms_router
from api.routers.admin_a4 import router as admin_a4_router
from api.routers.admin_dashboard import router as admin_dashboard_router
from api.routers.admin_llm_ops import router as admin_llm_ops_router  # AA-518/AA-505
from api.routers.v1_progress import router as v1_progress_router  # AA-637
from api.middleware.rate_limit import rate_limit_middleware
from api.middleware.sentry_context import sentry_context_middleware
from api.core.sentry import init_sentry
from services.acp_shared.audit_log import TenantAuditAction, write_audit_log

logger = structlog.get_logger()
pool: asyncpg.Pool = None

init_sentry()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global pool
    redis = aioredis.from_url(
        f"redis://{os.environ.get('REDIS_HOST', 'aa-cis-dev-redis.wvp8vb.0001.usw1.cache.amazonaws.com')}:6379",
        encoding="utf-8", decode_responses=True
    )
    app.state.redis = redis
    pool = await asyncpg.create_pool(
        os.environ["DATABASE_URL"], min_size=2, max_size=10,
    )
    app.state.pool = pool

    # AA-544 Stage 1 — second pool, aa_app_user (non-BYPASSRLS). Best-effort: None on failure,
    # every caller falls back to app.state.pool (aa_cis_admin, unchanged). Used only by routes
    # that explicitly opt in via api/core/aa544_tenant_pool.py::acquire_scoped_conn() — today
    # that's exactly 1 route (GET /v1/publish-log/pending), behind a Redis flag default-off.
    from api.core.aa544_tenant_pool import create_tenant_pool
    app.state.tenant_pool = await create_tenant_pool()

    # AA-223: recover run-tour jobs left 'running' by a prior container exit.
    # Best-effort — a transient DB error here must NOT crash boot (crash-loop risk).
    try:
        from api.routers.jobs_repo import sweep_interrupted
        n = await sweep_interrupted()
        logger.info("aa223_startup_sweep", interrupted_jobs=n)
    except Exception as e:
        logger.warning("aa223_startup_sweep_failed", error=repr(e))

    yield

    # AA-295: drain in-flight background jobs (run-tour-async / revalidate) before closing
    # the pool. _background_tasks is module-private to admin_pipeline (leading underscore) —
    # reached into here rather than made public because it's only ever needed at this one
    # shutdown call site. Without this, a rolling-deploy SIGTERM tears the pool/redis down
    # immediately while a job is still mid-flight, and the job never gets a chance to reach
    # its own except-CancelledError handler (see admin_pipeline._run_tour_job).
    from api.routers.admin_pipeline import _background_tasks
    if _background_tasks:
        logger.warning("shutdown_draining_background_tasks", count=len(_background_tasks))
        _done, _pending = await asyncio.wait(_background_tasks, timeout=25)
        if _pending:
            logger.error("shutdown_forced_task_abandon", count=len(_pending))

    await pool.close()
    if app.state.tenant_pool is not None:
        await app.state.tenant_pool.close()
    await redis.aclose()

# AA-577 — docs_url/redoc_url/openapi_url all disabled here (were the FastAPI defaults: /docs,
# /redoc, /openapi.json) so none of the 4 routes auto-register unprotected; re-registered by hand
# below, each behind require_admin_secret_for_docs().
app = FastAPI(
    title="AA-CIS API",
    version="0.3.0",
    description="Adventure Asia Content Intelligence System",
    lifespan=lifespan,
    docs_url=None, redoc_url=None, openapi_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://localhost:3001",
        "https://api-cis.lumiguides.it.com",
        "https://aa-cis.lumiguides.it.com",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
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


app.include_router(v1_tours_router)
app.include_router(v1_quota_router)  # AA-489 — GET /v1/quota, real replacement for AA-428's dead endpoint
app.include_router(v1_marketplace_router)  # AA-444 — tenant Marketplace view
app.include_router(v1_planning_router)  # AA-448 — T7 Content Planning (preview only so far)
app.include_router(v1_slate_router)  # AA-511 — the Slate (GET /v1/slate, POST /v1/subjects/{id}/pick)
app.include_router(v1_route_hub_router)  # AA-510 — Route/Hub derivation + Subject pick
app.include_router(v1_angle_gate_router)  # AA-449 — T8 Angle Gate
app.include_router(v1_content_writing_router)  # AA-450 — T9 Content Writing + T10-inline
# AA-579: v1_exports_router (POST /v1/exports) removed — S8/S9 tàn dư (21/04/2026), 0 traffic
# tenant thật trong 14 ngày CloudWatch, 0 UI caller. Xem AA-579 (Linear) trước khi khôi phục.
app.include_router(v1_pipeline_router)
app.include_router(v1_competitors_router)
app.include_router(v1_publish_router)  # AA-455 bước 1 — tenant self-unpublish (publish_log)
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
app.include_router(admin_llm_ops_router)  # AA-518/AA-505 — /admin/llm-config, /admin/llm-usage/*
app.include_router(v1_progress_router)  # AA-637 — GET /v1/progress/{kind}/{job_id}

app.middleware("http")(rate_limit_middleware)
app.middleware("http")(sentry_context_middleware)

def get_pool() -> asyncpg.Pool:
    if not pool:
        raise HTTPException(status_code=503, detail="DB not ready")
    return pool

@app.post("/auth/tenant-login", response_model=TenantLoginResponse, tags=["auth"])
async def tenant_login(
    body: TenantLoginRequest,
    db: asyncpg.Pool = Depends(get_pool),
):
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
        raise HTTPException(status_code=401, detail="Invalid API key")
    token = _create_jwt(row["tenant_id"], row["name"], row["plan_tier"])

    # AA-559 — semantic tenant-activity log. Best-effort/swallowed (unlike every other new call
    # site this issue adds): login has no pre-existing DB write of its own to be atomic with, and
    # a real tenant must never be locked out of the portal because an audit INSERT hiccuped.
    try:
        await write_audit_log(
            db, tenant_id=row["tenant_id"], actor=f"tenant:{row['tenant_id']}",
            action=TenantAuditAction.TENANT_LOGIN, resource_type="tenant",
            resource_id=row["tenant_id"], details={"login_method": "api_key"},
        )
    except Exception as exc:
        logger.error("tenant_login_audit_log_failed", tenant_id=row["tenant_id"], error=str(exc))

    return TenantLoginResponse(
        token=token,
        tenant_id=row["tenant_id"],
        tenant_name=row["name"],
        plan_tier=row["plan_tier"],
    )

@app.post("/auth/verify-tenant", tags=["auth"])
async def verify_tenant(request: Request):
    body = await request.json()
    payload = verify_jwt(body.get("token", ""))
    return {
        "valid": True,
        "tenant_id": payload["sub"],
        "name": payload["name"],
        "plan_tier": payload["plan_tier"],
    }

@app.post("/auth/admin-login", response_model=AdminLoginResponse, tags=["auth"])
async def admin_login(
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
    async with db.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id::text, username, password_hash, role::text, is_active "
            "FROM shared.admin_users WHERE username = $1",
            body.username,
        )
    # Always run bcrypt compare (even on no-row) to keep response timing constant.
    dummy_hash = "$2b$12$" + "0" * 53  # valid bcrypt shape, never matches
    password_ok = _verify_password(body.password, row["password_hash"] if row else dummy_hash)
    if not row or not row["is_active"] or not password_ok:
        raise HTTPException(status_code=401, detail="Invalid username or password")
    token = _create_admin_jwt(row["id"], row["username"], row["role"])
    return AdminLoginResponse(
        token=token,
        admin_id=row["id"],
        username=row["username"],
        role=row["role"],
    )

@app.post("/auth/verify-admin", response_model=VerifyAdminResponse, tags=["auth"])
async def verify_admin(request: Request):
    """
    Verify an admin JWT (server-side, called by Next.js middleware) — mirrors
    /auth/verify-tenant. Stateless: does not re-check admin_users.is_active on
    each call, so a deactivated admin stays valid until token expiry (24h).
    """
    body = await request.json()
    payload = verify_jwt(body.get("token", ""))
    if payload.get("role") not in ("admin", "reviewer"):
        # Explicit whitelist — same secret/alg means a tenant JWT would decode fine here.
        raise HTTPException(status_code=401, detail="Not an admin token")
    return VerifyAdminResponse(
        admin_id=payload["sub"],
        username=payload["username"],
        role=payload["role"],
        valid=True,
    )

@app.get("/health")
async def health():
    return {"status": "ok", "service": "aa-cis-api", "version": "0.3.0"}
