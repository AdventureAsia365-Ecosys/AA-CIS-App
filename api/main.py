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
from services.acp_shared.atom_replaced import AtomReplaced
from api.routers.auth import (
    verify_jwt, verify_tenant_jwt, tenant_login_gone, TENANT_LOGIN_GONE,
    _verify_password, _create_admin_jwt,
    AdminLoginRequest, AdminLoginResponse, VerifyAdminResponse,
    STAFF_ROLES,
)
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
from api.routers.v1_facts import router as v1_facts_router  # AA-814
from api.routers.v1_account import router as v1_account_router
from api.routers.v1_account_members import router as v1_account_members_router  # AA-978
from api.routers.v1_account_addons import router as v1_account_addons_router  # AA-1036 R3 add-on requests
from api.routers.v1_trip_page import router as v1_trip_page_router  # AA-482
from api.routers.v1_trip_page import admin_router as admin_trip_page_router  # AA-482
from api.routers.admin import router as admin_router
from api.routers.admin_tenant_provision import router as admin_tenant_provision_router  # AA-1028 R1 slice 2
from api.routers.admin_tenant_subscription import router as admin_tenant_subscription_router  # AA-1028 R1 slice 3
from api.routers.admin_tenant_addon_requests import router as admin_tenant_addon_requests_router  # AA-1036 R3
from api.routers.admin_plans import router as admin_plans_router  # AA-1028 R1 slice 3
from api.routers.admin_api_docs import router as admin_api_docs_router  # AA-577 — /admin/openapi.json
from api.routers.admin_data_model import router as admin_data_model_router  # AA-894 — /admin/data-model
from api.routers.admin_docs import router as admin_docs_router  # AA-898 — /admin/docs/*
from api.routers.admin import verify_admin_secret  # AA-577 — reused to gate Swagger/ReDoc/openapi.json
from api.routers.admin_pipeline import router as admin_pipeline_router
from api.routers.admin_settings import router as admin_settings_router
from api.routers.admin_atoms import router as admin_atoms_router
from api.routers.admin_a4 import router as admin_a4_router
from api.routers.admin_dashboard import router as admin_dashboard_router
from api.routers.admin_overview import router as admin_overview_router  # AA-664
from api.routers.admin_data_coverage import router as admin_data_coverage_router  # AA-992
from api.routers.admin_seo import router as admin_seo_router  # AA-705
from api.routers.admin_llm_ops import router as admin_llm_ops_router  # AA-518/AA-505
from api.routers.admin_segment_research import router as admin_segment_research_router  # AA-646
from api.routers.admin_job_runner import router as admin_job_runner_router  # AA-650
from api.routers.admin_budgets import router as admin_budgets_router  # AA-649
from api.routers.admin_ai_gateway import router as admin_ai_gateway_router  # AA-804
from api.routers.admin_llm_route_control import router as admin_llm_route_control_router  # AA-1042
from api.routers.admin_llm_route import router as admin_llm_route_router  # AA-1042 (Trace tab)
from api.routers.admin_llm_route import quality_router as admin_llm_route_quality_router  # AA-1042 (Tenant QA list)
from api.routers.admin_usage_explorer import router as admin_usage_explorer_router  # AA-1045
from api.routers.admin_decisions import router as admin_decisions_router  # AA-660
from api.routers.admin_platform import router as admin_platform_router  # AA-883
from api.routers.admin_photos import router as admin_photos_router, public_router as photos_public_router  # AA-708
from api.routers.admin_places import router as admin_places_router  # AA-712
from api.routers.admin_supply_content import router as admin_supply_content_router  # AA-835
from api.routers.admin_search import router as admin_search_router  # AA-752 — global ⌘K search
from api.routers.admin_waves import router as admin_waves_router  # AA-727 — rerun waves as objects
from api.routers.admin_content_quality import router as admin_content_quality_router  # AA-769
from api.routers.admin_tenant_compliance import router as admin_tenant_compliance_router  # AA-1039 R6
from api.routers.admin_tenant360 import router as admin_tenant360_router  # AA-766
from api.routers.admin_view_as import router as admin_view_as_router  # AA-1035 R2 slice 2 — view as tenant
from api.routers.admin_tenant_assignments import router as admin_tenant_assignments_router  # AA-1035 R2 slice 2
from api.routers.admin_platform_ops import router as admin_platform_ops_router  # AA-884
from api.routers.admin_crm_console import router as admin_crm_console_router  # AA-941
from api.routers.admin_alert_recipients import router as admin_alert_recipients_router  # AA-913
from api.routers.admin_aa_content import router as admin_aa_content_router  # AA-977
from api.routers.admin_writing_memory import router as admin_writing_memory_router  # AA-917
from api.routers.admin_claim_labels import router as admin_claim_labels_router  # AA-1048 W2
from api.routers.admin_facts import router as admin_facts_router  # AA-814
from api.routers.admin_reports import router as admin_reports_router  # AA-1003
from api.routers.admin_quality_funnel import router as admin_quality_funnel_router  # AA-900 W0
from api.routers.v1_progress import router as v1_progress_router  # AA-637
from api.routers.catalog_http import (  # AA-976 — catalog.http.v1 for the partner CRM (M2M)
    CatalogProblem, catalog_problem_handler, router as catalog_http_router,
)
from api.routers.tenant_membership import router as tenant_membership_router  # AA-978 — tenant.membership.v1
from api.routers.tenant_account import router as tenant_account_router  # AA-1028 — tenant.entitlements.v1
from api.routers.tenant_addons import router as tenant_addons_router  # AA-1036 — tenant.addons.v1
from api.routers.tenant_session import router as tenant_session_router  # AA-979 P2b — partner-pool studio sign-in
from api.middleware.tenant_role import tenant_role_middleware  # AA-979 P2b — Content studio roles (csr)
from api.middleware.view_as import ViewAsGateMiddleware  # AA-1035 R2 slice 2 — read-only view-as tokens
from api.middleware.rate_limit import rate_limit_middleware
from api.middleware.sentry_context import sentry_context_middleware
from api.middleware.staff_bearer import StaffBearerMiddleware  # AA-816 C3
from api.middleware.staff_audit import StaffAuditMiddleware  # AA-884
from api.middleware.request_id import RequestIdMiddleware, unhandled_error_handler  # AA-871
from api.core import readiness  # AA-884 — /health git_sha, /health/ready
from api.version import __version__  # AA-891 — release version (release-please bumps it)
from api.core import staff_identity  # AA-816 C3
from api.core import login_throttle  # AA-876
from api.core import view_as  # AA-1035 — a view-as token is not a tenant session (verify-tenant)
from api.core.sentry import init_sentry
from api.schemas.public_api import Health, Readiness, TenantSession  # AA-899
# AA-1036: the tenant.login audit row was written here by the retired API-key sign-in; the partner sign-in writes it
# (api/routers/tenant_session.py).
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
    # AA-874: URL + TLS / AUTH kwargs from REDIS_HOST / REDIS_TLS / REDIS_AUTH_TOKEN (shared/cache/redis_conn.py).
    from api.core.redis_client import build_api_redis
    from shared.cache.redis_conn import redis_conn_args
    redis_url, redis_kwargs = redis_conn_args()
    redis = build_api_redis(redis_url, **redis_kwargs)
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

    # AA-751 / AA-832 / S263 — every 300 s read per-role connections, XID age and the shared.job backlog (line
    # `ops_metrics_sampled`) and run the in-app ops checks on them (shared/ops_checks.py: admin bell + alert
    # e-mail on the SNS topic; no CloudWatch metric since the S263 alert diet). AA-913: the same tick reconciles
    # the platform alert recipients with SNS. api only, off when
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
    # AA-979 P2b — same for the partner pool (POST /auth/tenant-session); off under ENVIRONMENT == "test".
    from api.core.partner_identity import start_partner_jwks_prefetch, stop_partner_jwks_prefetch
    partner_prefetch_task = start_partner_jwks_prefetch()

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
            from services.writing_memory import config_fp   # AA-900 W0: the writers run in this process too
            config_fp.set_service(config_fp.WRITER_SERVICE)
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

    # S263 review: the stop also flushes the ops log events counted since the last tick (bounded, 5 s).
    await stop_ops_metrics_sampler(ops_metrics_task, pool=pool)
    await stop_cis_staff_jwks_prefetch(identity_prefetch_task)
    await stop_m2m_jwks_prefetch(m2m_prefetch_task)
    await stop_partner_jwks_prefetch(partner_prefetch_task)
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
# AA-898 — GET /admin/docs/{manifest,page} (admin page /admin/docs), early for the same reason.
app.include_router(admin_docs_router)


app.include_router(v1_tours_router)
app.include_router(v1_quota_router)  # AA-489 — GET /v1/quota, real replacement for AA-428's dead endpoint
app.include_router(v1_marketplace_router)  # AA-444 — tenant Marketplace view
app.include_router(v1_planning_router)  # AA-448 — T7 Content Planning (preview only so far)
app.include_router(v1_slate_router)  # AA-511 — the Slate (GET /v1/slate, POST /v1/subjects/{id}/pick)
# AA-859 wave 4 (S247): v1_route_hub_router (GET /v1/routes, /v1/hubs, GET/POST /v1/route-picks) removed: 0 callers
# in CIS frontend / portal, TripPlanner, Booking or Infra, 0 requests in 30 days of /ecs/aa-cis-dev logs, and
# /v1/routes was broken anyway (a tuple bound to text[]). acp_contract.route_pick was
# quarantined in `retired` (CIS 294) and dropped (CIS 296, AA-850).
app.include_router(v1_angle_gate_router)  # AA-449 — T8 Angle Gate
app.include_router(v1_content_writing_router)  # AA-450 — T9 Content Writing + T10-inline
# AA-579: v1_exports_router (POST /v1/exports) removed — S8/S9 tàn dư (21/04/2026), 0 traffic
# tenant thật trong 14 ngày CloudWatch, 0 UI caller. Xem AA-579 (Linear) trước khi khôi phục.
app.include_router(v1_pipeline_router)
app.include_router(v1_publish_router)  # AA-455 bước 1 — tenant self-unpublish (publish_log)
app.include_router(v1_notifications_router)  # AA-986 L3 — tenant bell: GET /v1/notifications, mark read
app.include_router(v1_integrations_router)  # AA-457 [T11 PR1] — tenant WordPress credentials
app.include_router(v1_facts_router)  # AA-814 — tenant Facts Entries: /v1/facts (tenant pool)
app.include_router(v1_account_router)  # AA-761 — tenant Account pages: activity trail + CSV, API key facts
app.include_router(v1_account_members_router)  # AA-978 — tenant Account › Members (CIS 276 directory)
app.include_router(v1_account_addons_router)  # AA-1036 R3 — the agency app's add-ons and "Request add-on"
app.include_router(v1_trip_page_router)  # AA-482 — GET /v1/trip/{tour_id}, public page data
app.include_router(admin_trip_page_router)  # AA-482 — /admin/trip-pages/{publish,recheck}
app.include_router(admin_tenant_provision_router)  # before admin_router: /tenants/provision is no tenant id
app.include_router(admin_view_as_router)  # AA-1035 R2 slice 2 — /admin/tenants/{id}/view-as, /admin/view-as/sessions
app.include_router(admin_tenant_assignments_router)  # AA-1035 R2 slice 2 — /admin/tenant-assignments (Assignments page)
app.include_router(admin_tenant_subscription_router)  # AA-1028 R1 slice 3 (Tenant 360 › Subscription)
app.include_router(admin_tenant_addon_requests_router)  # AA-1036 R3 (Tenant 360 › Subscription › requests)
app.include_router(admin_plans_router)  # AA-1028 R1 slice 3 (Tenants › Plans & add-ons)
app.include_router(admin_router)
app.include_router(admin_pipeline_router)
app.include_router(admin_settings_router)
app.include_router(admin_atoms_router)
app.include_router(admin_a4_router)
app.include_router(admin_dashboard_router)  # AA-527 (bổ sung) — Segment/Score/Route-Hub/Slate audit panels
app.include_router(admin_overview_router)  # AA-664 — /admin/overview (whole-system snapshot, cached 60s)
app.include_router(admin_data_coverage_router)  # AA-992 — /admin/data-coverage (counts, raw upload to CRM)
app.include_router(admin_seo_router)  # AA-705 — /admin/seo-intelligence (demand, keywords, PAA, gaps, spend)
app.include_router(admin_llm_ops_router)  # AA-518/AA-505 — /admin/llm-config, /admin/llm-usage/*
app.include_router(admin_segment_research_router)  # AA-646 — /admin/segment-research/{preview,run,status}
app.include_router(admin_job_runner_router)  # AA-650 — /admin/job-runner/*
app.include_router(admin_budgets_router)  # AA-649 — /admin/budgets (cost guard limits)
app.include_router(admin_ai_gateway_router)  # AA-804 — /admin/ai-gateway/{providers,trace} (AI providers page)
app.include_router(admin_llm_route_control_router)  # AA-1042 — /admin/ai-gateway/route-control/* (Route control tab)
app.include_router(admin_llm_route_router)  # AA-1042 — /admin/ai-gateway/route-* (Trace tab: trace, alerts)
app.include_router(admin_llm_route_quality_router)  # AA-1042 — /admin/content-quality/fallback-writes (tenant_content)
app.include_router(admin_usage_explorer_router)  # AA-1045 — /admin/usage-explorer/* (AI & spend › Usage explorer)
app.include_router(admin_decisions_router)  # AA-660 — /admin/decisions (Jev per stage, cost)
app.include_router(admin_platform_router)  # AA-883 — /admin/platform/staff (read-only staff directory)
app.include_router(admin_platform_ops_router)  # AA-884 — /admin/platform/{apps,audit,cost}
app.include_router(admin_crm_console_router)  # AA-941 — /admin/platform/crm/* (CRM L1 console relay, no include prefix)
app.include_router(admin_alert_recipients_router)  # AA-913 — /admin/platform/alert-recipients (no include prefix)
app.include_router(admin_aa_content_router)  # AA-977 — /admin/aa-content (AA content editor, no include prefix)
app.include_router(admin_writing_memory_router)  # AA-917 — /admin/writing-memory (no include prefix)
app.include_router(admin_claim_labels_router)  # AA-1048 W2 — /admin/writing-memory/claim-labels (no include prefix)
app.include_router(admin_facts_router)  # AA-814 — /admin/facts (platform Facts Entries, no include prefix)
app.include_router(admin_reports_router)  # AA-1003 — /admin/reports (report register, runs; no include prefix)
app.include_router(admin_quality_funnel_router)  # AA-900 W0 — GET /admin/quality/funnel (CIS-08 snapshot, $0)
app.include_router(admin_photos_router)  # AA-708 — /admin/photos (Drive photo sync, matching)
app.include_router(admin_places_router)  # AA-712 — /admin/places/backfill (place pipeline)
app.include_router(admin_supply_content_router)  # AA-835 — /admin/supply-content (Booking supply content review)
app.include_router(admin_search_router)  # AA-752 — GET /admin/search (global ⌘K palette)
app.include_router(admin_waves_router)  # AA-727 — /admin/waves (expected set, report, check)
app.include_router(admin_content_quality_router)  # AA-769 — /admin/content-quality (T2/T9 quality, place cards)
app.include_router(admin_tenant_compliance_router)  # AA-1039 R6 — /admin/tenant-qa/compliance (d20 / d21 flags)
app.include_router(admin_tenant360_router)  # AA-766 — /admin/tenants/{id}/{cost,quality,markets}
app.include_router(photos_public_router)  # AA-708 — /content/photos/{id} (public image redirect)
app.include_router(v1_progress_router)  # AA-637 — GET /v1/progress/{kind}/{job_id}
app.include_router(catalog_http_router)  # AA-976 — /catalog/v1 (catalog.http.v1, M2M cis.catalog/read)
app.include_router(tenant_membership_router)  # AA-978 — /tenant/membership/v1 (M2M cis.tenant/read)
app.include_router(tenant_account_router)  # AA-1028 — /tenant/v1 (M2M cis.tenant/read)
app.include_router(tenant_addons_router)  # AA-1036 R3 — /tenant/v1 add-ons (M2M cis.tenant/addons)
app.include_router(tenant_session_router)  # AA-979 P2b — /auth/tenant-session, /auth/tenant-memberships (partner pool)

app.middleware("http")(rate_limit_middleware)
# AA-979 P2b — registered after the rate limiter so it wraps it: a viewer / editor token refused for its Content studio
# role (csr claim) is answered 403 before it reaches the rate limiter, the meter or the route.
app.middleware("http")(tenant_role_middleware)
# AA-1035 R2 slice 2 — registered after the tenant role middleware so it wraps it and the rate limiter: a request with a
# staff view-as token (api/core/view_as.py) is refused unless it is an allowed read of an open session, and every
# request it serves is logged before the route runs.
app.add_middleware(ViewAsGateMiddleware)
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
# AA-990 — T9 / T8 work on a request whose atom a later atomize run replaced: 409 atom_replaced, plain sentence
# (same status_code / detail / headers shape as TourNotServedError, so the same handler answers it).
app.add_exception_handler(AtomReplaced, tour_not_served_handler)
# AA-976 / AA-978 — contract Problem body; CatalogProblem is api.core.m2m_auth.M2MProblem, also raised by
# /tenant/membership/v1 and (AA-1028) /tenant/v1.
app.add_exception_handler(CatalogProblem, catalog_problem_handler)
app.add_exception_handler(Exception, unhandled_error_handler)
app.add_middleware(RequestIdMiddleware)

def get_pool() -> asyncpg.Pool:
    if not pool:
        raise HTTPException(status_code=503, detail="DB not ready")
    return pool

# AA-1036 R3 slice 3 (root ADR 0019 d6): tenant API keys are gone. Any body answers 410 Gone with a short JSON body
# naming the partner sign-in (POST /auth/tenant-session through the agency web). The body is never parsed, the login
# throttle and the DB are never touched, and no tenant.login audit row is written. The API Gateway resource of this
# path (AA-876) still forwards here, so a caller sees the 410, not a 403 / 404 from the edge.
@app.post(
    "/auth/tenant-login", tags=["auth"], deprecated=True, status_code=410,
    summary="Removed: tenant API keys are gone (410 Gone)",
    responses={410: {"description": "Always: tenant API keys were removed; sign in with the partner pool.",
                     "content": {"application/json": {"example": TENANT_LOGIN_GONE}}}},
)
async def tenant_login():
    return tenant_login_gone()

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
    # AA-1035 follow-up (S268): a staff view-as token (amr staff_view_as, root ADR 0019 d5) is a read-only staff view,
    # not a tenant session: whatever asks "is this a tenant session?" must not be told yes. The gate never sees it here
    # (the token is in the body, not the Authorization header).
    if view_as.is_view_as(payload) or payload.get("amr") == view_as.VIEW_AS_AMR:
        raise HTTPException(status_code=403, detail=view_as.NOT_A_TENANT_SESSION)
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


async def _load_active_staff(db: asyncpg.Pool, admin_id: str):
    """The staff row behind a legacy session, or None when it is gone or deactivated (the legacy verify-admin path)."""
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

# AA-911 (root ADR 0019 d23, S271): the CIS -> AA-Booking sign-in bridge is retired. POST /auth/booking-token and
# GET /auth/.well-known/jwks.json are gone (404): Booking staff sign in with the Cognito WORKFORCE booking-staff client
# only, on the Booking admin's own host, and Booking accepts only the WORKFORCE issuer (AA-Booking identity_config).


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
