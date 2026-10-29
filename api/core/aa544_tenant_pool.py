"""
AA-544 -- aa_app_user tenant pool + per-request RLS GUC helper.

Rollout scope: wired into routes ONE AT A TIME, each behind its OWN Redis flag (see the *_FLAG
constants below), per AA-544's original plan's Stage 5 principle ("mo rong dan tung route,
moi route revert rieng qua flag"). Do NOT import this into another router without going
through AA-544's own stage-by-stage report-then-confirm ritual first -- picking which route is
next is a decision, not something to copy-paste in ad hoc. Routes on this module (all fail_closed
since AA-817 PR-A, S239; each logs `used_tenant_pool` per request):
  - Stage 1 (10/09/2026): GET /v1/publish-log/pending (v1_publish.py::list_pending)
  - Stage 5 (10/09/2026): GET /v1/content-writing/pieces/{piece_id} and .../export
    (service.py::fetch_piece)
  - AA-815: GET /v1/publish-log (v1_publish.py::list_publish_log)
  - AA-815 + AA-817 (237): DELETE /v1/integrations/{wordpress,facebook} (v1_integrations.py::_disconnect)
  - AA-817 (S237): GET /v1/content-writing/requests/{id}/latest-piece
    (service.py::fetch_latest_piece_for_request)
  - AA-859 wave 2 (S240, all six flags ON on Dev 16/10/2026 after the automated flip-verify run, root
    docs/audits/2026-10-16-S240-aa859/flip_report.json):
    W2 PATCH /v1/content-writing/pieces/{id} (PIECE_EDIT_FLAG); W3 POST /v1/angle-gate/requests/{id}/choose
    and /reopen (ANGLE_GATE_DECIDE_FLAG, needs 241); W4 DELETE /v1/publish-log/{id} (UNPUBLISH_FLAG);
    W5a GET /v1/tours/pool, /my-versions, /versions/{id} (T2_VERSION_READS_FLAG); W6 PATCH
    /v1/tours/versions/{id} (T2_VERSION_WRITE_FLAG); W9 GET/POST /v1/integrations/{wordpress,facebook}
    and POST .../test (INTEGRATION_CONNECT_FLAG, needs 241).
  - AA-859 D5b (S247): GET /v1/tours/{id}/full (tenant-JWT branch), GET /v1/tours/{id} and
    GET /v1/tours/versions/{id}/export-docx (T2_VERSION_DETAIL_READS_FLAG; its own flag, so reverting it
    never reverts W5a).
  - AA-859 wave 3 (S247): GET / POST /admin/brand-identity with a tenant Bearer (BRAND_IDENTITY_FLAG, needs
    262; the X-Admin-Secret staff branch stays on the admin pool) and POST /v1/publish-log/{piece_id}/publish
    (PUBLISH_FLAG; no new migration).
  - AA-859 wave 4 (S247, migrations 265 + 266): GET /v1/angle-gate/requests/{id} and the choose / reopen re-read
    (ANGLE_GATE_READ_FLAG), GET /v1/slate (SLATE_READS_FLAG), POST /v1/subjects/{id}/pick and /cut
    (SUBJECT_DECIDE_FLAG), GET /v1/planning/trip-reallocation/suggest and POST .../confirm
    (TRIP_REALLOCATION_FLAG), GET /v1/marketplace (MARKETPLACE_FLAG), GET /v1/content-writing/reviews and
    .../requests/{id}/review (REVIEWS_FLAG). Each group has its own flag and needs 265 and / or 266 (see the
    constants): `SET <key> false` on Dev before the deploy, flip after the apply.
  - AA-814 (S257): GET / POST / PATCH / DELETE /v1/facts (v1_facts.py, FACTS_FLAG, needs 302).
  - AA-976 (S249), outside the flag ritual above: GET /catalog/v1/tours, /catalog/v1/tours/{source_system}/{source_id}
    and /catalog/v1/destinations/{id} (api/routers/catalog_http.py, M2M caller: the partner CRM). It does NOT use
    acquire_scoped_conn: services/catalog_http/reader.catalog_connection takes app.state.tenant_pool directly, in a
    READ ONLY transaction with app.tenant_id = '' (the catalog views run as catalog_owner, aa_app_user only needs
    the SELECT it already holds), and a per-process semaphore (reader.MAX_CONCURRENT_READS = 1) caps it at ONE of
    this pool's connections per api task. No flag, no `used_tenant_pool` log line (no tenant). Anyone resizing
    TENANT_POOL_MAX_SIZE or the role's CONNECTION LIMIT must count this consumer.

Design (matches AA-544's approved 2-pool split): Admin (/admin/*) keeps app.state.pool unchanged,
always. That pool logs in as the CIS runtime role: aa_cis_admin today (owner, so exempt on the ENABLE-only
RLS tables, and a member of rds_superuser, which FORCE RLS does not filter on RDS: AA-817 probe), cis_app
from X1 S18 (AA-860; CIS 244 gives it a permissive USING (true) policy on every RLS table, ENABLE-only and
FORCE alike, for platform parity). Neither has BYPASSRLS. This module adds a SECOND pool, aa_app_user
(non-BYPASSRLS, real RLS applies), used ONLY by routes that explicitly opt in via
acquire_scoped_conn() below, and only when that route's OWN flag is on.

Flag encoding (S241 decision D-12, fail closed): the tenant pool is the default. Only an explicit
OFF value (FLAG_OFF_VALUES: "false", "0", "off", "no") switches a route back to the admin pool. "true",
a missing key, any other value, a Redis error / timeout or no Redis client all mean ON (tenant pool, RLS).
The decision is read once per request and reused by every acquire_scoped_conn block of that request.

Rollback (per route, independent of every other route on this pool): `redis-cli SET <that route's
flag key> false` reverts it to the admin pool immediately -- no deploy needed, and does not affect
any other route already on the tenant pool. `DEL` is NOT a rollback any more: a deleted key means ON.
Redis keeps no data across a node reboot / replacement (single node, no persistence), so a Redis-only
rollback silently ends there. The durable rollback is the env var FORCE_ADMIN_ENV
(TENANT_POOL_FORCE_ADMIN_FLAGS, comma-separated flag keys, in the api task definition, read once at
process start): a key listed there is OFF whatever Redis says.

GUC safety: app.tenant_id is set via set_config(..., is_local=true) INSIDE an explicit
transaction (SET LOCAL semantics) -- scoped to that one transaction so it cannot leak onto the
next request that reuses this same physical connection from the pool. Every query that needs
tenant scoping MUST run inside the same `async with conn.transaction():` block
acquire_scoped_conn() opens -- see its docstring.
"""
from __future__ import annotations

import asyncio
import os
import time
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Optional
from uuid import UUID

import asyncpg
import structlog
from fastapi import HTTPException, Request

logger = structlog.get_logger()

# One flag per canary route -- each independently revertible. Add a new named constant here
# (don't invent ad hoc string literals at call sites) whenever a new route joins the rollout.
# S241 D-12: a missing key is ON, so a newly wired route runs on the tenant pool from its first deploy.
# To ship one dark, `SET <its key> false` on Dev BEFORE that deploy.
STAGE1_PUBLISH_PENDING_FLAG = "aa544:stage1:publish_pending_pool"
STAGE5_GET_PIECE_FLAG = "aa544:stage5:get_piece_pool"
# AA-815 — GET /v1/publish-log (tenant's published/unpublished rows).
PUBLISH_LOG_LIST_FLAG = "aa544:publish_log_list_pool"
# AA-815 — DELETE /v1/integrations/{wordpress|facebook} (tenant disconnect). Needs migration 237
# (RLS + GRANT SELECT, DELETE on shared.tenant_integrations for aa_app_user), applied 16/10/2026.
INTEGRATION_DISCONNECT_FLAG = "aa544:integration_disconnect_pool"
# AA-817 — tenant content-piece reads: GET /v1/content-writing/requests/{id}/latest-piece
# (service.fetch_latest_piece_for_request). Works with and without migration 229 (content_piece and
# angle_gate_request have had RLS and grants for aa_app_user since 113/115/147; 229 only rewrites the
# policies to the NULLIF/WITH CHECK template).
CONTENT_PIECE_READS_FLAG = "aa817:content_piece_reads_pool"
# AA-859 wave 2 (plan D7). Declared together, ahead of the route PRs, so those PRs do not conflict on
# this file; each stays unused until its route PR wires it in. "Needs 241" = the route needs migration
# 241's grants / RLS, so a 241 rollback must turn that flag off first.
PIECE_EDIT_FLAG = "aa859:piece_edit_pool"                    # W2 content piece PATCH; no 241
T2_VERSION_WRITE_FLAG = "aa859:t2_version_write_pool"        # W6 tenant tour version PATCH; no 241
ANGLE_GATE_DECIDE_FLAG = "aa859:angle_gate_decide_pool"      # W3 angle-gate choose + reopen; needs 241
UNPUBLISH_FLAG = "aa859:unpublish_pool"                      # W4 unpublish; no 241
T2_VERSION_READS_FLAG = "aa859:t2_version_reads_pool"        # W5 T2 version reads (job status on MAIN); no 241
INTEGRATION_CONNECT_FLAG = "aa859:integration_connect_pool"  # W9 integration GET / POST / test; needs 241
# AA-859 D5b (S247): the three T2 detail reads W5a left on MAIN. Own flag, not T2_VERSION_READS_FLAG: that one
# is verified and ON, so a route added under it would move with no flip of its own (review of PR-D5a).
T2_VERSION_DETAIL_READS_FLAG = "aa859:t2_version_detail_reads_pool"  # D5b /{id}/full, /{id}, export-docx; no 241
# AA-859 wave 3 (S247, plan D7). "Needs 262" = migration 262's RLS + grants on tenant_brand_rules /
# tenant_seo_config: a 262 rollback must turn BRAND_IDENTITY_FLAG off first.
BRAND_IDENTITY_FLAG = "aa859:brand_identity_pool"            # GET / POST /admin/brand-identity (tenant); needs 262
PUBLISH_FLAG = "aa859:publish_pool"                          # POST /v1/publish-log/{piece_id}/publish; no new grant
# AA-859 wave 4 (S247). "Needs 265" = RLS + grants on subject / quarter_plan / quarter_plan_version / year_plan /
# tenant_tour_pages and the platform read allowlist (route, atom_segment*, place_card*, atom_place_card); "needs 266"
# = RLS + SELECT on acp_contract.tour_atoms. A 265 / 266 rollback turns the flags that need it off first.
ANGLE_GATE_READ_FLAG = "aa859:angle_gate_read_pool"          # GET angle-gate request + choose/reopen re-read; 265
SLATE_READS_FLAG = "aa859:slate_reads_pool"                  # GET /v1/slate (subject list); needs 265
SUBJECT_DECIDE_FLAG = "aa859:subject_decide_pool"            # POST /v1/subjects/{id}/pick, /cut; needs 265 + 266
TRIP_REALLOCATION_FLAG = "aa859:trip_reallocation_pool"      # trip-reallocation suggest / confirm; needs 265 + 266
MARKETPLACE_FLAG = "aa859:marketplace_pool"                  # GET /v1/marketplace; needs 266
REVIEWS_FLAG = "aa859:reviews_pool"                          # GET content-writing reviews + review; needs 265 + 266
# AA-986 L3 (S248): the tenant's own tour notifications (GET /v1/notifications, mark read). Needs CIS 274 (RLS on
# shared.notifications + aa_app_user SELECT / UPDATE (is_read)); a 274 rollback turns it off first.
TENANT_NOTIFICATIONS_FLAG = "aa986:tenant_notifications_pool"  # GET /v1/notifications, POST .../read; needs 274
# AA-814: the tenant's own Facts Entries (GET / POST / PATCH / DELETE /v1/facts). Reads and writes need CIS 229's
# facts policies (applied), DELETE needs CIS 302 (GRANT DELETE to aa_app_user); a 302 rollback turns it off first.
FACTS_FLAG = "aa814:facts_pool"                                # /v1/facts (list, create, edit, delete); needs 302

# Every flag above. A new *_FLAG constant must be added here too (a unit test checks it), and to the
# ALLOWED set of the flag tooling (root docs/audits/2026-10-16-S240-aa859/aa859_flag.py, and its wave-3 / wave-4
# copies aa859_wave3_flag.py / aa859_wave4_flag.py), so it can be SET false on Dev.
ALL_TENANT_POOL_FLAGS = frozenset({
    STAGE1_PUBLISH_PENDING_FLAG, STAGE5_GET_PIECE_FLAG, PUBLISH_LOG_LIST_FLAG, INTEGRATION_DISCONNECT_FLAG,
    CONTENT_PIECE_READS_FLAG, PIECE_EDIT_FLAG, T2_VERSION_WRITE_FLAG, ANGLE_GATE_DECIDE_FLAG, UNPUBLISH_FLAG,
    T2_VERSION_READS_FLAG, INTEGRATION_CONNECT_FLAG, T2_VERSION_DETAIL_READS_FLAG, BRAND_IDENTITY_FLAG, PUBLISH_FLAG,
    ANGLE_GATE_READ_FLAG, SLATE_READS_FLAG, SUBJECT_DECIDE_FLAG, TRIP_REALLOCATION_FLAG, MARKETPLACE_FLAG, REVIEWS_FLAG,
    TENANT_NOTIFICATIONS_FLAG, FACTS_FLAG,
})

# S241 D-12: the explicit values that turn a route's flag OFF (admin pool), compared after strip +
# lower-case. "false" is what the flag tooling writes; "0" / "off" / "no" are accepted so a hand-typed
# rollback during an incident works. Anything else -- including a missing key -- is ON. FLAG_ON_VALUE is
# what the flag tooling writes for ON.
FLAG_OFF_VALUE = "false"
FLAG_OFF_VALUES = frozenset({FLAG_OFF_VALUE, "0", "off", "no"})
FLAG_ON_VALUE = "true"
# Bound on the flag read. On timeout the route stays on the tenant pool (fail closed), like any other
# read failure. The Redis client itself is also bounded (api/core/redis_client.py socket timeouts), which
# is what keeps the rate-limit middleware from hanging on a blackholed Redis before the route runs.
FLAG_READ_TIMEOUT_S = 0.5

# Durable per-route rollback (survives a Redis restart, which drops every key = every route back ON).
# Comma-separated flag keys; read once at import (process start). A listed key is OFF without a Redis read.
FORCE_ADMIN_ENV = "TENANT_POOL_FORCE_ADMIN_FLAGS"


def _parse_force_admin(raw: Optional[str]) -> frozenset:
    return frozenset(k.strip() for k in (raw or "").split(",") if k.strip())


_FORCE_ADMIN_FLAGS = _parse_force_admin(os.environ.get(FORCE_ADMIN_ENV))
if _FORCE_ADMIN_FLAGS - ALL_TENANT_POOL_FLAGS:
    # A typo here would otherwise be a rollback that silently does nothing.
    logger.error("aa817_tenant_flag_force_admin_unknown_keys", env=FORCE_ADMIN_ENV,
                 keys=sorted(_FORCE_ADMIN_FLAGS - ALL_TENANT_POOL_FLAGS))
_force_admin_logged: set = set()   # one INFO line per key per process
_REQUEST_CACHE_ATTR = "_tenant_flag_cache"

_SECRET_ID = "aa-cis/dev/rds-app-user"

# AA-817 (critique C4) / AA-859 PR-S — sizing against `ALTER ROLE aa_app_user CONNECTION LIMIT 15`
# (migration 234) and api autoscaling max_capacity 7 (AA-CIS-Infra modules/ecs/main.tf, lowered from 10 in
# Infra #156). Data standards C2.2 row `cis-tenant`: max_size 2 per api task x 7 tasks = 14 <= 15. 2, not 1:
# with 1, one slow statement (up to the 15 s statement_timeout) makes every queued tenant request on that
# task 503 after the 5 s acquire timeout (head-of-line blocking). Change this and max_capacity together.
# min_size 0 + a short idle lifetime: an idle task holds no connection at all, and a task that cannot get
# one (limit reached, RDS down) answers 503 per request (aa817_tenant_pool_unavailable) instead of losing
# the pool for its whole life, which is what min_size=1 at boot did.
TENANT_POOL_MIN_SIZE = 0
TENANT_POOL_MAX_SIZE = 2
TENANT_POOL_ACQUIRE_TIMEOUT_S = 5.0      # C2.3g: fail fast on exhaustion, <= 5 s
TENANT_POOL_IDLE_LIFETIME_S = 60.0       # asyncpg max_inactive_connection_lifetime
TENANT_POOL_SERVER_SETTINGS = {
    "application_name": "cis-tenant",
    "statement_timeout": "15000",        # C2.3f: pool-level, below the 30 s API GW timeout
    "lock_timeout": "5000",
}

# Errors that mean "no tenant connection to be had" (as opposed to a failing query): role CONNECTION
# LIMIT (53300 TooManyConnectionsError) and other connect-time Postgres errors, a closed/invalid pool,
# network errors, and the acquire timeout.
_POOL_UNAVAILABLE_ERRORS = (asyncpg.PostgresError, asyncpg.InterfaceError, OSError, asyncio.TimeoutError)


async def create_tenant_pool() -> Optional[asyncpg.Pool]:
    """Best-effort, called once at app startup (api/main.py's lifespan()). A missing/
    unreachable secret must never crash app boot -- same convention as the AA-223 startup
    sweep already in lifespan(). Returns None when the secret or the pool object cannot be made;
    a fail_closed route then answers 503 while its flag is on (see acquire_scoped_conn).

    Lazy (min_size 0, see the sizing note above): creating the pool opens no connection, so a task
    that boots while aa_app_user is at its CONNECTION LIMIT still gets a pool and recovers on its
    own. One probe query at boot only logs whether the role can log in right now."""
    try:
        import boto3
        client = boto3.client(
            "secretsmanager", region_name=os.environ.get("AWS_REGION", "us-west-1")
        )
        dsn = client.get_secret_value(SecretId=_SECRET_ID)["SecretString"]
        pool = await asyncpg.create_pool(
            dsn, min_size=TENANT_POOL_MIN_SIZE, max_size=TENANT_POOL_MAX_SIZE,
            max_inactive_connection_lifetime=TENANT_POOL_IDLE_LIFETIME_S,
            server_settings=dict(TENANT_POOL_SERVER_SETTINGS),
        )
    except Exception as e:
        logger.error("aa544_tenant_pool_create_failed", error=repr(e))
        return None
    try:
        # bounded: the first connect must not hold app boot for asyncpg's 60 s connect timeout
        await asyncio.wait_for(pool.fetchval("SELECT 1"), timeout=TENANT_POOL_ACQUIRE_TIMEOUT_S)
        logger.info("aa817_tenant_pool_ready", max_size=TENANT_POOL_MAX_SIZE)
    except Exception as e:
        logger.error("aa817_tenant_pool_unavailable", flag_key=None, stage="boot_probe",
                     reason=_unavailable_reason(e), error=repr(e))
    return pool


def _unavailable_reason(exc: BaseException) -> str:
    if isinstance(exc, asyncpg.TooManyConnectionsError):
        return "too_many_connections"
    if isinstance(exc, asyncio.TimeoutError):
        return "acquire_timeout"
    if isinstance(exc, asyncpg.InterfaceError):
        return "pool_closed"
    return "connect_error"


def _request_flag_cache(request: Request) -> Optional[dict]:
    """The per-request {flag_key: decision} dict on request.state, created on first use; None when the
    request has no usable state (the decision is then simply not cached)."""
    state = getattr(request, "state", None)
    if state is None:
        return None
    try:
        cache = getattr(state, _REQUEST_CACHE_ATTR, None)
        if not isinstance(cache, dict):
            cache = {}
            setattr(state, _REQUEST_CACHE_ATTR, cache)
        return cache
    except Exception:
        return None


async def _flag_enabled(request: Request, flag_key: str) -> bool:
    """S241 decision D-12: tenant-pool flags FAIL CLOSED. Returns False (admin pool) only when the key is
    in TENANT_POOL_FORCE_ADMIN_FLAGS (durable rollback) or Redis holds an explicit OFF value
    (FLAG_OFF_VALUES) -- the per-route rollback. Everything else is True (tenant pool, RLS): "true", a
    missing key, an unrecognised value, a Redis error, a read slower than FLAG_READ_TIMEOUT_S, or no Redis
    client on app.state. A Redis outage therefore never moves tenant traffic onto the admin pool (which
    RLS does not filter); with the tenant pool down too, the fail_closed routes answer 503.

    One decision per request: the first read is memoised on request.state, so a route with several
    acquire_scoped_conn blocks (unpublish: 3, disconnect: 2) never switches pools mid-request -- e.g. a
    rollback ("false") read in block 1 followed by a Redis blip in block 3, after the irreversible remote
    delete -- and pays at most one Redis round-trip and one ERROR line.

    A failed read logs `aa817_tenant_flag_read_failed` at ERROR (`fallback="tenant_pool"`, `reason=`), and
    an unrecognised value logs `aa817_tenant_flag_unrecognized` at ERROR. The evidence that a route runs on
    the tenant pool is still the per-request `used_tenant_pool=True` line, never the flag value."""
    if flag_key in _FORCE_ADMIN_FLAGS:
        if flag_key not in _force_admin_logged:
            _force_admin_logged.add(flag_key)
            logger.info("aa817_tenant_flag_forced_admin", flag_key=flag_key, env=FORCE_ADMIN_ENV)
        return False
    cache = _request_flag_cache(request)
    if cache is not None and flag_key in cache:
        return cache[flag_key]
    decision = await _read_flag(request, flag_key)
    if cache is not None:
        cache[flag_key] = decision
    return decision


async def tenant_flag_enabled(request: Request, flag_key: str) -> bool:
    """The same fail-closed, per-request-cached decision acquire_scoped_conn takes for `flag_key` (see
    _flag_enabled). For a route already inside a tenant block that must decide whether a SECOND flag's
    statements may run on that same connection (AA-859 wave 4: the angle-gate choose / reopen re-read)."""
    return await _flag_enabled(request, flag_key)


async def _read_flag(request: Request, flag_key: str) -> bool:
    """One Redis read of `flag_key`, decoded per D-12 (see _flag_enabled)."""
    redis = getattr(request.app.state, "redis", None)
    if redis is None:
        logger.error("aa817_tenant_flag_read_failed", flag_key=flag_key, fallback="tenant_pool",
                     reason="client_missing")
        return True
    try:
        val = await asyncio.wait_for(redis.get(flag_key), timeout=FLAG_READ_TIMEOUT_S)
    except asyncio.TimeoutError as e:
        logger.error("aa817_tenant_flag_read_failed", flag_key=flag_key, fallback="tenant_pool",
                     reason="timeout", error=repr(e))
        return True
    except Exception as e:
        logger.error("aa817_tenant_flag_read_failed", flag_key=flag_key, fallback="tenant_pool",
                     reason="redis_error", error=repr(e))
        return True
    if val is None:
        return True  # missing key = ON (D-12); rollback is an explicit SET <key> false
    if isinstance(val, bytes):
        val = val.decode("utf-8", "replace")
    normalized = str(val).strip().lower()
    if normalized in FLAG_OFF_VALUES:
        return False
    if normalized != FLAG_ON_VALUE:
        # ERROR, not WARNING: this is most likely a rollback typed by hand that did not take.
        logger.error("aa817_tenant_flag_unrecognized", flag_key=flag_key, value=str(val)[:32],
                     treated_as="on", off_values=sorted(FLAG_OFF_VALUES))
    return True


def _is_uuid(value) -> bool:
    """AA-859: the policies cast app.tenant_id to uuid, so a malformed JWT `sub` would surface as a
    22P02 inside a policy (a 500). acquire_scoped_conn refuses it up front instead (503).

    Canonical lower-case form only (what Postgres prints for a uuid column): Python's UUID() also takes
    'urn:uuid:...', braces, no hyphens, upper case and stray whitespace, some of which `::uuid` rejects
    (22P02 again) and all of which would silently match nothing under a text-compare policy."""
    text = str(value)
    try:
        return str(UUID(text)) == text
    except (TypeError, ValueError, AttributeError):
        return False


def _ms(seconds: float) -> float:
    return round(seconds * 1000, 1)


@asynccontextmanager
async def acquire_scoped_conn(request: Request, tenant_id: str, flag_key: str, *, fail_closed: bool = True):
    """Yields (conn, used_tenant_pool: bool).

    `flag_key` is one of this module's own *_FLAG constants -- each route on this rollout
    gets its own flag, so reverting one route never affects another already on the tenant pool.

    When that route's flag is on AND the tenant pool booted successfully: acquires from
    request.app.state.tenant_pool (aa_app_user, non-BYPASSRLS) and opens a transaction with
    app.tenant_id set LOCAL to it. Everything the caller does with `conn` inside this
    `async with` block MUST happen inside that same transaction for the GUC to apply -- do not
    acquire a second connection or start a nested transaction on this one mid-block.

    Otherwise (flag explicitly off -- an OFF value or the env override, see _flag_enabled -- or, with
    an explicit fail_closed=False, the tenant pool never came up): acquires from request.app.state.pool
    (aa_cis_admin, the pre-rollout behavior) with no transaction wrapper. A flag that cannot be read
    counts as ON (S241 D-12).

    AA-817 `fail_closed=True` (the default since S241, and what every caller passes): once the route's
    flag is on, the route is meant to be isolated by RLS, so it never silently falls back to the admin
    pool, which RLS does not filter (owner exempt). A missing tenant pool, an empty or malformed (not a uuid, AA-859)
    tenant id, no tenant connection within TENANT_POOL_ACQUIRE_TIMEOUT_S (CONNECTION LIMIT reached, RDS
    unreachable), or a GUC that did not take the tenant id raises 503 instead. An explicit OFF value is
    unchanged (admin pool); an unreadable flag is ON, so a Redis outage gives the tenant pool, not the
    admin pool.

    AA-859 pool timing: when the tenant pool was used, one INFO line `aa859_tenant_pool_timing` with
    `wait_ms` (acquire) and `hold_ms` (acquire -> release), so Logs Insights can take percentiles. Never
    hold this block across a Secrets Manager or HTTP call: with max_size 1-2 per task, a held connection
    is every other tenant request's wait."""
    tenant_pool = getattr(request.app.state, "tenant_pool", None)
    if fail_closed:
        use_tenant_pool = await _flag_enabled(request, flag_key)
        if use_tenant_pool and (tenant_pool is None or not tenant_id or not _is_uuid(tenant_id)):
            logger.error("aa817_tenant_context_unavailable", flag_key=flag_key,
                         tenant_pool=tenant_pool is not None, tenant_id_present=bool(tenant_id),
                         tenant_id_valid=bool(tenant_id) and _is_uuid(tenant_id))
            raise HTTPException(status_code=503, detail="Tenant data path unavailable, try again shortly")
    else:
        use_tenant_pool = tenant_pool is not None and await _flag_enabled(request, flag_key)

    if use_tenant_pool:
        started = time.monotonic()
        acquired = None
        try:
            async with AsyncExitStack() as stack:
                try:
                    conn = await stack.enter_async_context(
                        tenant_pool.acquire(timeout=TENANT_POOL_ACQUIRE_TIMEOUT_S))
                except _POOL_UNAVAILABLE_ERRORS as e:
                    # One event for every "no tenant connection" case; `reason=` says which case. The api
                    # logs are JSON lines (shared/log_config.py), and the AA-CIS-Infra metric filter on the
                    # term "aa817_tenant_pool_unavailable" alarms on any count > 0 (AA-874).
                    logger.error("aa817_tenant_pool_unavailable", flag_key=flag_key, stage="acquire",
                                 reason=_unavailable_reason(e), error=repr(e), fail_closed=fail_closed,
                                 wait_ms=_ms(time.monotonic() - started))
                    if fail_closed:
                        raise HTTPException(status_code=503,
                                            detail="Tenant data path unavailable, try again shortly") from e
                    conn = None
                if conn is not None:
                    acquired = time.monotonic()
                    async with conn.transaction():
                        if fail_closed:
                            applied = await conn.fetchval("SELECT set_config('app.tenant_id', $1, true)",
                                                          tenant_id)
                            if applied != tenant_id:
                                logger.error("aa817_tenant_guc_not_applied", flag_key=flag_key)
                                raise HTTPException(status_code=503,
                                                    detail="Tenant data path unavailable, try again shortly")
                        else:
                            await conn.execute("SELECT set_config('app.tenant_id', $1, true)", tenant_id)
                        yield conn, True
        finally:
            if acquired is not None:  # logged after the release, whether the block succeeded or raised
                logger.info("aa859_tenant_pool_timing", flag_key=flag_key, wait_ms=_ms(acquired - started),
                            hold_ms=_ms(time.monotonic() - acquired))
        if acquired is not None:
            return
    async with request.app.state.pool.acquire() as conn:
        yield conn, False
