"""
/v1/integrations — AA-457 [T11 PR1], tenant-facing third-party integration credentials.

WordPress only for now (T11's blog channel). Tenant-JWT-gated (`get_tenant()`, same shared
dependency v1_publish.py/v1_content_writing.py/v1_angle_gate.py already use — no new auth
mechanism). Every query is scoped by the caller's own `tenant_id`, never trusts a body/query
param for it — same anti-IDOR shape v1_publish.py/v1_competitors.py already established.

Secrets Manager: reuses `acp/cms/{tenant_id}` — the exact naming convention `v1_s4_blog.py`
already invented (confirmed live before this task: 0 secrets ever existed under it). Reuses the
same "arbitrary secret_key string, fetch directly, no caching" pattern
`services/acp_s4_blog/cms/publisher.py::_get_cms_creds()` established — NOT `shared/secrets.py`'s
fixed-ARN-per-env-var pattern, which is the wrong shape for a per-tenant secret space (AA-456
STEP0 §4 covers why explicitly).

The application password itself NEVER touches Postgres — only Secrets Manager. `config` JSONB on
`shared.tenant_integrations` holds only the non-secret `site_url`.

Scope for THIS PR (AA-457): save/update credentials + test-connection + read current status.
NOT in scope: the actual publish endpoint, the content_piece list endpoint, the WordPressAdapter
draft->publish change — all AA-458.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
from contextlib import nullcontext
from typing import Optional
from urllib.parse import urlparse

import aiohttp
import boto3
import structlog
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from api.core.aa544_tenant_pool import INTEGRATION_DISCONNECT_FLAG, acquire_scoped_conn
from api.routers.v1_tours import get_tenant
from services.acp_shared.audit_log import TenantAuditAction, write_audit_log

logger = structlog.get_logger()
router = APIRouter(prefix="/v1/integrations", tags=["integrations"])

_SM_REGION = "us-west-1"
_TEST_TIMEOUT = aiohttp.ClientTimeout(total=8)

_BLOCKED_HOSTNAMES = {"localhost"}


class SaveWordPressRequest(BaseModel):
    wp_url: str
    username: str
    app_password: str


def _validate_wp_url(url: str) -> str:
    """Basic SSRF-aware validation for a tenant-supplied WordPress site URL — blocks the
    obvious local/private-IP-literal cases (localhost, 127.0.0.1, 169.254.x.x cloud-metadata
    range, RFC1918 ranges, etc). Deliberately does NOT perform DNS resolution at save time — a
    domain that doesn't resolve yet (typo, not-yet-propagated DNS) is a real-URL-just-unreachable
    case, which is exactly what the test-connection endpoint (not this validator) classifies.
    Returns the trimmed URL or raises ValueError with a message safe to show the tenant."""
    url = url.strip().rstrip("/")
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ValueError("wp_url must start with https://")
    hostname = (parsed.hostname or "").lower()
    if not hostname:
        raise ValueError("wp_url must include a valid hostname")
    if hostname in _BLOCKED_HOSTNAMES or hostname.endswith(".local"):
        raise ValueError("wp_url cannot point to a local address")
    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        ip = None
    if ip is not None and (
        ip.is_private or ip.is_loopback or ip.is_link_local
        or ip.is_reserved or ip.is_multicast or ip.is_unspecified
    ):
        raise ValueError("wp_url cannot point to a private or reserved IP address")
    return url


def _sm_client(config: Optional[BotoConfig] = None):
    return boto3.client("secretsmanager", region_name=_SM_REGION, config=config)


def _put_secret(secret_key: str, value: dict) -> None:
    """Create-or-update — same net effect as v1_s4_blog.py's cms_secret_key convention, just
    given a real writer for the first time (that router only ever built the key string, never
    wrote to it for a real tenant)."""
    client = _sm_client()
    payload = json.dumps(value)
    try:
        client.create_secret(Name=secret_key, SecretString=payload)
        logger.info("integration_secret_created", secret_key=secret_key)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ResourceExistsException":
            client.put_secret_value(SecretId=secret_key, SecretString=payload)
            logger.info("integration_secret_updated", secret_key=secret_key)
        else:
            raise


def _get_secret(secret_key: str) -> dict:
    """Same shape as services/acp_s4_blog/cms/publisher.py::_get_cms_creds() — arbitrary
    secret_key at call time, no caching (a connect/test/publish action is a rare, deliberate
    tenant click, not a hot path). Sync boto3: callers run it through asyncio.to_thread (AA-859)."""
    client = _sm_client()
    return json.loads(client.get_secret_value(SecretId=secret_key)["SecretString"])


# AA-815 / AA-859 — what disconnect writes over the credentials (see _tombstone_secret below). A row can
# still point at a tombstoned secret: a disconnect whose DELETE failed after the tombstone, or a request
# that read the row just before a concurrent disconnect deleted it. Every credential reader checks for it
# and answers 422 "reconnect", never a KeyError (500) on creds["wp_url"].
_TOMBSTONE = {"disconnected": True}


def is_tombstoned_secret(creds) -> bool:
    return creds == _TOMBSTONE


def reconnect_detail(name: str) -> str:
    return f"{name} was disconnected — reconnect it and try again"


def _row_to_status(row) -> dict:
    config = row["config"]
    if isinstance(config, str):
        config = json.loads(config)
    return {
        "connected": True,
        "site_url": config.get("site_url"),
        "connected_at": row["connected_at"].isoformat() if row["connected_at"] else None,
        "last_verified_at": row["last_verified_at"].isoformat() if row["last_verified_at"] else None,
        "last_verify_error": row["last_verify_error"],
    }


@router.get("/wordpress")
async def get_wordpress_status(request: Request, tenant=Depends(get_tenant)):
    """Current connection status — never returns the secret itself, only non-secret config +
    verification state. Used by the FE to decide: show the connect form, or the connected state."""
    pool = request.app.state.pool
    tenant_id = tenant["sub"]

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT config, connected_at, last_verified_at, last_verify_error "
            "FROM shared.tenant_integrations WHERE tenant_id = $1::uuid AND integration_type = 'wordpress'",
            tenant_id,
        )
    if not row:
        return {"connected": False, "site_url": None, "connected_at": None,
                "last_verified_at": None, "last_verify_error": None}
    return _row_to_status(row)


@router.post("/wordpress")
async def save_wordpress(body: SaveWordPressRequest, request: Request, tenant=Depends(get_tenant)):
    """Save/update WordPress credentials. Writes the real credential JSON to Secrets Manager at
    acp/cms/{tenant_id}; the DB row only ever gets site_url + the secret's KEY NAME, never the
    application password. connected_at is set only on first connect (ON CONFLICT leaves it
    alone) — a credential UPDATE isn't a new "connection"."""
    try:
        wp_url = _validate_wp_url(body.wp_url)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    if not body.username.strip() or not body.app_password.strip():
        raise HTTPException(status_code=422, detail="username and app_password are required")

    pool = request.app.state.pool
    tenant_id = tenant["sub"]
    secret_key = f"acp/cms/{tenant_id}"

    try:
        await asyncio.to_thread(_put_secret, secret_key, {
            "wp_url": wp_url, "username": body.username, "app_password": body.app_password,
        })
    except ClientError as exc:
        logger.error("integration_secret_write_failed", tenant_id=tenant_id, error=str(exc))
        raise HTTPException(status_code=502, detail="Could not save credentials — try again")

    config = json.dumps({"site_url": wp_url})
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO shared.tenant_integrations
                (tenant_id, integration_type, config, secret_key, connected_at)
            VALUES ($1::uuid, 'wordpress', $2::jsonb, $3, now())
            ON CONFLICT (tenant_id, integration_type) DO UPDATE SET
                config = EXCLUDED.config,
                secret_key = EXCLUDED.secret_key,
                updated_at = now()
            RETURNING config, connected_at, last_verified_at, last_verify_error
            """,
            tenant_id, config, secret_key,
        )

    logger.info("wordpress_integration_saved", tenant_id=tenant_id, site_url=wp_url)
    return _row_to_status(row)


def _classify_test_failure(status: Optional[int], exc: Optional[Exception], invalid_200_body: bool = False) -> str:
    if status == 401:
        return "Wrong username or application password"
    if status == 404:
        return "WordPress REST API is not enabled on this site"
    if status == 200 and invalid_200_body:
        # AA-460: a 200 with the wrong shape/content-type is real and common — WAF/anti-bot
        # challenge pages, maintenance pages, and misconfigured catch-all routes all return
        # 200 at arbitrary paths. Live-verify (AA-457-02) confirmed this exact case: InfinityFree's
        # anti-bot layer returns 200/text-html for every request, previously misread as success
        # for correct AND wrong AND garbage credentials alike.
        return "Unexpected response from this URL — verify it's a WordPress site with REST API enabled"
    if status is not None:
        return f"WordPress returned an unexpected response (HTTP {status})"
    return "Could not connect to this WordPress site — check the URL"


@router.post("/wordpress/test")
async def test_wordpress(request: Request, tenant=Depends(get_tenant)):
    """Real connection test — GET {wp_url}/wp-json/wp/v2/users/me with Basic Auth, the standard
    WordPress Application Password smoke-test. Success -> last_verified_at bumped, error cleared.
    Failure -> last_verify_error set with a classified message, last_verified_at LEFT UNCHANGED
    (a failed retest must not silently mark a previously-good connection as freshly verified)."""
    pool = request.app.state.pool
    tenant_id = tenant["sub"]

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT config, secret_key FROM shared.tenant_integrations "
            "WHERE tenant_id = $1::uuid AND integration_type = 'wordpress'",
            tenant_id,
        )
    if not row or not row["secret_key"]:
        raise HTTPException(status_code=404, detail="WordPress is not connected yet")

    try:
        creds = await asyncio.to_thread(_get_secret, row["secret_key"])
    except ClientError as exc:
        # AA-457 live-verify (24/08/2026) hit this path via a real, unrelated IAM gap
        # (ECS task role missing secretsmanager:CreateSecret) and this branch logged nothing,
        # making the 502 hard to diagnose from CloudWatch alone — logging added after that.
        logger.error("integration_secret_read_failed", tenant_id=tenant_id,
                     secret_key=row["secret_key"], error=str(exc))
        raise HTTPException(status_code=502, detail="Could not read saved credentials — try reconnecting")
    if is_tombstoned_secret(creds):
        raise HTTPException(status_code=422, detail=reconnect_detail("WordPress"))

    try:
        wp_url = _validate_wp_url(creds["wp_url"])
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    status_code: Optional[int] = None
    error: Optional[Exception] = None
    invalid_200_body = False
    try:
        auth = aiohttp.BasicAuth(creds["username"], creds["app_password"])
        async with aiohttp.ClientSession(timeout=_TEST_TIMEOUT) as session:
            async with session.get(f"{wp_url}/wp-json/wp/v2/users/me", auth=auth) as resp:
                status_code = resp.status
                if status_code == 200:
                    # AA-460: status 200 alone doesn't mean this is real WordPress — a WAF/
                    # anti-bot challenge page, a maintenance page, or a misconfigured catch-all
                    # route can all return 200 at this exact path (confirmed live, AA-457-02:
                    # InfinityFree's anti-bot layer did exactly this for every credential tried,
                    # correct and wrong alike). Require content-type + real WordPress user-object
                    # shape (an "id" field) before trusting it.
                    content_type = resp.headers.get("content-type", "")
                    body_text = await resp.text()
                    parsed_body = None
                    if content_type.startswith("application/json"):
                        try:
                            parsed_body = json.loads(body_text)
                        except (json.JSONDecodeError, ValueError):
                            parsed_body = None
                    if not (isinstance(parsed_body, dict) and "id" in parsed_body):
                        invalid_200_body = True
    except Exception as exc:  # noqa: BLE001 — classified below, any network failure lands here
        error = exc

    success = status_code == 200 and not invalid_200_body
    message = None if success else _classify_test_failure(status_code, error, invalid_200_body)

    async with pool.acquire() as conn:
        if success:
            updated = await conn.fetchrow(
                """
                UPDATE shared.tenant_integrations
                SET last_verified_at = now(), last_verify_error = NULL, updated_at = now()
                WHERE tenant_id = $1::uuid AND integration_type = 'wordpress'
                RETURNING config, connected_at, last_verified_at, last_verify_error
                """,
                tenant_id,
            )
        else:
            updated = await conn.fetchrow(
                """
                UPDATE shared.tenant_integrations
                SET last_verify_error = $2, updated_at = now()
                WHERE tenant_id = $1::uuid AND integration_type = 'wordpress'
                RETURNING config, connected_at, last_verified_at, last_verify_error
                """,
                tenant_id, message,
            )

    logger.info("wordpress_test_connection", tenant_id=tenant_id, success=success,
                status_code=status_code, error=str(error) if error else None)

    result = _row_to_status(updated)
    result["success"] = success
    return result


# ── AA-462 — Facebook (T11's first non-blog channel) ────────────────────────────────────────
# Mirrors the WordPress save/status/test shape above exactly (same table, same secret-never-
# touches-Postgres principle, same AA-460 don't-trust-bare-200 lesson) — the generalization
# migration 117 already anticipated ("this table is meant to hold more than just WordPress
# later"), so no new migration needed. secret_key uses a distinct acp/social/{tenant_id} prefix
# (not acp/cms/{tenant_id}) — a Page Access Token isn't a CMS credential, and a tenant
# connecting both WordPress and Facebook needs two independent secrets, not one shared blob.


class SaveFacebookRequest(BaseModel):
    page_id: str
    page_access_token: str


def _fb_row_to_status(row) -> dict:
    config = row["config"]
    if isinstance(config, str):
        config = json.loads(config)
    return {
        "connected": True,
        "page_id": config.get("page_id"),
        "connected_at": row["connected_at"].isoformat() if row["connected_at"] else None,
        "last_verified_at": row["last_verified_at"].isoformat() if row["last_verified_at"] else None,
        "last_verify_error": row["last_verify_error"],
    }


@router.get("/facebook")
async def get_facebook_status(request: Request, tenant=Depends(get_tenant)):
    pool = request.app.state.pool
    tenant_id = tenant["sub"]

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT config, connected_at, last_verified_at, last_verify_error "
            "FROM shared.tenant_integrations WHERE tenant_id = $1::uuid AND integration_type = 'facebook'",
            tenant_id,
        )
    if not row:
        return {"connected": False, "page_id": None, "connected_at": None,
                "last_verified_at": None, "last_verify_error": None}
    return _fb_row_to_status(row)


@router.post("/facebook")
async def save_facebook(body: SaveFacebookRequest, request: Request, tenant=Depends(get_tenant)):
    """Save/update Facebook Page credentials. The Page Access Token never touches Postgres —
    written to Secrets Manager at acp/social/{tenant_id}; the DB row only ever holds the
    non-secret page_id + the secret's KEY NAME. connected_at set only on first connect."""
    if not body.page_id.strip() or not body.page_access_token.strip():
        raise HTTPException(status_code=422, detail="page_id and page_access_token are required")

    pool = request.app.state.pool
    tenant_id = tenant["sub"]
    secret_key = f"acp/social/{tenant_id}"

    try:
        await asyncio.to_thread(_put_secret, secret_key, {
            "page_id": body.page_id.strip(), "page_access_token": body.page_access_token.strip(),
        })
    except ClientError as exc:
        logger.error("integration_secret_write_failed", tenant_id=tenant_id, error=str(exc))
        raise HTTPException(status_code=502, detail="Could not save credentials — try again")

    config = json.dumps({"page_id": body.page_id.strip()})
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO shared.tenant_integrations
                (tenant_id, integration_type, config, secret_key, connected_at)
            VALUES ($1::uuid, 'facebook', $2::jsonb, $3, now())
            ON CONFLICT (tenant_id, integration_type) DO UPDATE SET
                config = EXCLUDED.config,
                secret_key = EXCLUDED.secret_key,
                updated_at = now()
            RETURNING config, connected_at, last_verified_at, last_verify_error
            """,
            tenant_id, config, secret_key,
        )

    logger.info("facebook_integration_saved", tenant_id=tenant_id, page_id=body.page_id)
    return _fb_row_to_status(row)


def _classify_facebook_test_failure(status, body: Optional[dict]) -> str:
    if isinstance(body, dict) and "error" in body:
        err = body["error"]
        code = err.get("code")
        if code in (190,):  # OAuthException — expired/invalid token
            return "Invalid or expired Page Access Token"
        if code in (100,):  # Unsupported get request — usually a bad page_id
            return "Facebook Page not found — check the Page ID"
        return f"Facebook returned an error: {err.get('message', 'unknown')}"
    if status is not None:
        return f"Facebook returned an unexpected response (HTTP {status})"
    return "Could not connect to the Facebook Graph API"


@router.post("/facebook/test")
async def test_facebook(request: Request, tenant=Depends(get_tenant)):
    """Real connection test — GET /{page_id}?fields=id,name&access_token=... (the standard Graph
    API token/page smoke-test, same idea as WordPress's own /users/me check). Success ->
    last_verified_at bumped; failure -> last_verify_error set, last_verified_at unchanged."""
    pool = request.app.state.pool
    tenant_id = tenant["sub"]

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT config, secret_key FROM shared.tenant_integrations "
            "WHERE tenant_id = $1::uuid AND integration_type = 'facebook'",
            tenant_id,
        )
    if not row or not row["secret_key"]:
        raise HTTPException(status_code=404, detail="Facebook is not connected yet")

    try:
        creds = await asyncio.to_thread(_get_secret, row["secret_key"])
    except ClientError as exc:
        logger.error("integration_secret_read_failed", tenant_id=tenant_id,
                     secret_key=row["secret_key"], error=str(exc))
        raise HTTPException(status_code=502, detail="Could not read saved credentials — try reconnecting")
    if is_tombstoned_secret(creds):
        raise HTTPException(status_code=422, detail=reconnect_detail("Facebook"))

    status_code: Optional[int] = None
    parsed_body: Optional[dict] = None
    try:
        params = {"fields": "id,name", "access_token": creds["page_access_token"]}
        async with aiohttp.ClientSession(timeout=_TEST_TIMEOUT) as session:
            async with session.get(
                f"https://graph.facebook.com/{creds['page_id']}", params=params
            ) as resp:
                status_code = resp.status
                content_type = resp.headers.get("content-type", "")
                body_text = await resp.text()
                if content_type.startswith("application/json"):
                    try:
                        parsed_body = json.loads(body_text)
                    except (json.JSONDecodeError, ValueError):
                        parsed_body = None
    except Exception:  # noqa: BLE001 — classified below (status_code stays None -> generic message)
        pass

    success = (
        status_code == 200 and isinstance(parsed_body, dict)
        and "id" in parsed_body and "error" not in parsed_body
    )
    message = None if success else _classify_facebook_test_failure(status_code, parsed_body)

    async with pool.acquire() as conn:
        if success:
            updated = await conn.fetchrow(
                """
                UPDATE shared.tenant_integrations
                SET last_verified_at = now(), last_verify_error = NULL, updated_at = now()
                WHERE tenant_id = $1::uuid AND integration_type = 'facebook'
                RETURNING config, connected_at, last_verified_at, last_verify_error
                """,
                tenant_id,
            )
        else:
            updated = await conn.fetchrow(
                """
                UPDATE shared.tenant_integrations
                SET last_verify_error = $2, updated_at = now()
                WHERE tenant_id = $1::uuid AND integration_type = 'facebook'
                RETURNING config, connected_at, last_verified_at, last_verify_error
                """,
                tenant_id, message,
            )

    logger.info("facebook_test_connection", tenant_id=tenant_id, success=success, status_code=status_code)

    result = _fb_row_to_status(updated)
    result["success"] = success
    return result


# ── AA-815 — disconnect (WordPress + Facebook) ──────────────────────────────────────────────
# Deletes the shared.tenant_integrations row and overwrites the secret with a tombstone via
# PutSecretValue. Not DeleteSecret: the ECS task role is granted only CreateSecret/PutSecretValue
# on acp/cms/* and acp/social/* (AA-CIS-Infra modules/ecs/main.tf), and a reconnect reuses the
# same secret name through _put_secret() anyway.
#
# AA-859 (item 6) — two short transactions around the tombstone, so no tenant connection is held across
# Secrets Manager (the tenant pool has 1-2 connections per task; AA-817's single transaction held one
# for up to ~16 s on a slow PutSecretValue):
#   1. read secret_key + updated_at; release. No row -> 200 disconnected=false.
#   2. tombstone, no connection held. Failure -> 502, the row and the credentials stay intact.
#   3. DELETE exactly the row read in step 1 (same updated_at). 0 rows -> re-SELECT in the same
#      transaction: gone -> 200 disconnected=false + aa859_integration_disconnect_raced (a double click:
#      the other request deleted it); still visible -> 503 aa817_integration_disconnect_row_mismatch (RLS
#      refused the DELETE, or a reconnect/test changed updated_at), credentials already tombstoned.
#   4. audit on MAIN after commit (best-effort).
# Step 3 failing after step 2 (no tenant connection, connection drop, COMMIT error) is logged as
# aa859_integration_disconnect_tombstoned_not_deleted: the row stays with dead credentials (every
# credential reader answers 422 "reconnect" on a tombstone) and a retry of the disconnect heals it.

_DISCONNECTED_STATUS = {
    "wordpress": {"connected": False, "site_url": None, "connected_at": None,
                  "last_verified_at": None, "last_verify_error": None},
    "facebook": {"connected": False, "page_id": None, "connected_at": None,
                 "last_verified_at": None, "last_verify_error": None},
}


# Bounded, so one disconnect cannot hang on botocore's default 60 s x retries.
_TOMBSTONE_SM_CONFIG = BotoConfig(connect_timeout=3, read_timeout=5,
                                  retries={"mode": "standard", "total_max_attempts": 2})  # <= ~16 s


def _tombstone_secret(secret_key: str) -> None:
    try:
        _sm_client(_TOMBSTONE_SM_CONFIG).put_secret_value(
            SecretId=secret_key, SecretString=json.dumps(_TOMBSTONE))
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ResourceNotFoundException":
            raise


# Step 1, and the re-SELECT of step 3.
_SELECT_INTEGRATION_SQL = (
    "SELECT secret_key, updated_at FROM shared.tenant_integrations "
    "WHERE tenant_id = $1::uuid AND integration_type = $2"
)
# Step 3: only the row step 1 read (a reconnect in between changes updated_at and is not deleted).
_DELETE_INTEGRATION_SQL = (
    "DELETE FROM shared.tenant_integrations "
    "WHERE tenant_id = $1::uuid AND integration_type = $2 AND updated_at = $3 RETURNING secret_key"
)


async def _disconnect(request: Request, tenant_id: str, integration_type: str) -> dict:
    pool = request.app.state.pool
    ids = {"tenant_id": str(tenant_id), "integration_type": integration_type}

    # 1. read the row (short transaction; the tenant pool path is acquire_scoped_conn's own, GUC LOCAL)
    async with acquire_scoped_conn(
        request, tenant_id, INTEGRATION_DISCONNECT_FLAG, fail_closed=True,
    ) as (conn, used_tenant_pool):
        logger.info("aa817_integration_disconnect", **ids, used_tenant_pool=used_tenant_pool)
        async with (nullcontext() if used_tenant_pool else conn.transaction()):
            row = await conn.fetchrow(_SELECT_INTEGRATION_SQL, tenant_id, integration_type)
    if not row:  # idempotent: already disconnected
        return {**_DISCONNECTED_STATUS[integration_type], "disconnected": False}

    # 2. tombstone the credentials with no connection held
    tombstoned = False
    if row["secret_key"]:
        try:
            await asyncio.to_thread(_tombstone_secret, row["secret_key"])
        except (ClientError, BotoCoreError) as exc:  # BotoCoreError: the bounded timeouts
            logger.error("integration_secret_tombstone_failed", **ids, error=str(exc))
            raise HTTPException(status_code=502, detail="Could not remove saved credentials — try again")
        tombstoned = True

    # 3. delete exactly that row (second short transaction)
    try:
        async with acquire_scoped_conn(
            request, tenant_id, INTEGRATION_DISCONNECT_FLAG, fail_closed=True,
        ) as (conn, used_tenant_pool):
            async with (nullcontext() if used_tenant_pool else conn.transaction()):
                deleted = await conn.fetchrow(_DELETE_INTEGRATION_SQL, tenant_id, integration_type,
                                              row["updated_at"])
                if deleted is not None:
                    outcome = "deleted"
                elif await conn.fetchrow(_SELECT_INTEGRATION_SQL, tenant_id, integration_type) is None:
                    outcome = "raced"
                else:
                    outcome = "row_mismatch"
    except Exception as exc:
        if tombstoned:
            logger.error("aa859_integration_disconnect_tombstoned_not_deleted", **ids, error=repr(exc))
        raise

    if outcome == "row_mismatch":
        logger.error("aa817_integration_disconnect_row_mismatch", **ids, rows_deleted=0,
                     tombstoned=tombstoned, used_tenant_pool=used_tenant_pool)
        raise HTTPException(status_code=503, detail="Could not disconnect right now — try again")
    if outcome == "raced":  # another request (double click) deleted it between steps 1 and 3
        logger.info("aa859_integration_disconnect_raced", **ids, used_tenant_pool=used_tenant_pool)
        return {**_DISCONNECTED_STATUS[integration_type], "disconnected": False}

    logger.info("integration_disconnected", **ids)
    # 4. AA-859 MAIN exception: audit_log has no RLS; written after the tenant transaction committed.
    try:  # best-effort: the disconnect already happened, a lost audit row must not undo the answer
        await write_audit_log(
            pool, tenant_id=str(tenant_id), actor=f"tenant:{tenant_id}",
            action=TenantAuditAction.INTEGRATION_DISCONNECTED, resource_type="tenant_integration",
            resource_id=integration_type, details={"integration_type": integration_type},
        )
    except Exception as exc:
        logger.warning("integration_disconnect_audit_failed", tenant_id=tenant_id, error=str(exc))
    return {**_DISCONNECTED_STATUS[integration_type], "disconnected": True}


@router.delete("/wordpress")
async def disconnect_wordpress(request: Request, tenant=Depends(get_tenant)):
    return await _disconnect(request, tenant["sub"], "wordpress")


@router.delete("/facebook")
async def disconnect_facebook(request: Request, tenant=Depends(get_tenant)):
    return await _disconnect(request, tenant["sub"], "facebook")


__all__ = ["router"]
