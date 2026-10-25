"""AA-761 — /v1/account (api/routers/v1_account.py, api/services/tenant_account.py), no DB.

The tenant boundary is the point of these tests: the tenant id comes only from the verified JWT, every query binds
it, the action allowlist keeps staff-only rows out, and ``details`` never leaves raw. Real Postgres (cross-tenant
refusal on actual rows): tests/integration/test_aa761_tenant_account_sql.py.
"""
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import jwt
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from api.routers import v1_account
from api.routers.auth import JWT_ALG, JWT_SECRET, _create_jwt
from api.services import tenant_account as ta
from services.acp_shared.audit_log import AdminAuditAction, AdminTenantAuditAction, TenantAuditAction

TENANT_A = "11111111-2222-3333-4444-555555555555"
TENANT_B = "99999999-8888-7777-6666-555555555555"


def _pool(fetchval=0, fetch=None, fetchrow=None):
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=fetchval)
    conn.fetch = AsyncMock(return_value=fetch or [])
    conn.fetchrow = AsyncMock(return_value=fetchrow)
    pool = MagicMock()
    pool.acquire.return_value.__aenter__.return_value = conn
    pool.acquire.return_value.__aexit__.return_value = False
    return pool, conn


def _row(action, actor=f"tenant:{TENANT_A}", details=None, resource_type="content_piece", resource_id="r1"):
    return {"id": "a1b2", "created_at": datetime(2026, 10, 20, 8, 30, tzinfo=timezone.utc), "actor": actor,
            "action": action, "resource_type": resource_type, "resource_id": resource_id,
            "details": details if details is not None else {}}


# ── allowlist ───────────────────────────────────────────────────────────────────────────────────

def test_every_tenant_action_is_visible_and_staff_only_rows_are_not():
    tenant_actions = {v for k, v in vars(TenantAuditAction).items() if k.isupper()}
    assert tenant_actions <= set(ta.TENANT_VISIBLE_ACTIONS)
    hidden = {v for k, v in vars(AdminAuditAction).items() if k.isupper()}
    hidden |= {AdminTenantAuditAction.BRAND_UPDATED, AdminTenantAuditAction.CONFIG_UPDATED,
               AdminTenantAuditAction.PLAN_ID_BACKFILL, AdminTenantAuditAction.ERASED,
               AdminTenantAuditAction.OFFBOARD, AdminTenantAuditAction.MEMBER_ROLE_CHANGED,
               AdminTenantAuditAction.MEMBER_REMOVED, "staff.request", "publish_mode_transition",
               "trip_reallocation_suggestion"}
    assert not hidden & set(ta.TENANT_VISIBLE_ACTIONS)
    assert not any(a.startswith(("content_piece.admin_", "tenant_tour_version.admin_"))
                   for a in ta.TENANT_VISIBLE_ACTIONS)
    assert {cat for cat, _ in ta.TENANT_VISIBLE_ACTIONS.values()} == set(ta.CATEGORIES)


def test_visible_actions_narrow_never_widen():
    assert set(ta.visible_actions(ta.ActivityFilters())) == set(ta.TENANT_VISIBLE_ACTIONS)
    content = ta.visible_actions(ta.ActivityFilters(category="content"))
    assert content and all(ta.TENANT_VISIBLE_ACTIONS[a][0] == "content" for a in content)
    assert ta.visible_actions(ta.ActivityFilters(action="tenant.login")) == ["tenant.login"]
    # an action outside the category gives nothing, not the action
    assert ta.visible_actions(ta.ActivityFilters(category="content", action="tenant.login")) == []


@pytest.mark.parametrize("f, msg", [
    (ta.ActivityFilters(category="staff"), "unknown category"),
    (ta.ActivityFilters(action="staff.request"), "unknown action"),
    (ta.ActivityFilters(action="content_piece.admin_sentence_cut"), "unknown action"),
    (ta.ActivityFilters(since=date(2026, 10, 2), until=date(2026, 10, 1)), "after"),
    (ta.ActivityFilters(until=date.max), "'until' must be between"),
    (ta.ActivityFilters(until=date(2101, 1, 1)), "'until' must be between"),
    (ta.ActivityFilters(since=date.min), "'since' must be between"),
    (ta.ActivityFilters(since=date(1999, 12, 31)), "'since' must be between"),
])
def test_invalid_filters_are_refused(f, msg):
    with pytest.raises(ValueError, match=msg):
        ta.validate_filters(f)


# ── SQL shape ───────────────────────────────────────────────────────────────────────────────────

def test_where_binds_tenant_allowlist_and_dates():
    where, args = ta.build_where(TENANT_A, ta.ActivityFilters(since=date(2026, 10, 1), until=date(2026, 10, 31)))
    assert where == ("a.tenant_id = $1 AND a.action = ANY($2::text[]) AND a.created_at >= $3 "
                     "AND a.created_at < $4")
    assert args[0] == TENANT_A
    assert set(args[1]) == set(ta.TENANT_VISIBLE_ACTIONS)
    assert args[2] == datetime(2026, 10, 1, tzinfo=timezone.utc)
    assert args[3] == datetime(2026, 11, 1, tzinfo=timezone.utc)  # until is inclusive


def test_page_queries_order_and_offset():
    count_sql, rows_sql, args = ta.build_page_queries(TENANT_A, ta.ActivityFilters(), page=3, page_size=20)
    assert "ORDER BY a.created_at DESC, a.id DESC LIMIT $3 OFFSET $4" in rows_sql
    assert args[-2:] == [20, 40]
    assert "a.tenant_id = $1" in count_sql and "a.tenant_id = $1" in rows_sql


# ── items ───────────────────────────────────────────────────────────────────────────────────────

def test_row_to_item_never_returns_raw_details():
    item = ta.row_to_item(_row("tour.rewrite_triggered", details={"retry": True, "job_id": "job-secret-id"}))
    assert item["detail"] == "Retry"
    assert "details" not in item and "job-secret-id" not in str(item)
    assert item["category"] == "tours" and item["actor"] == "you" and item["at"].startswith("2026-10-20T08:30")


@pytest.mark.parametrize("action, details, expected", [
    ("content_piece.published", {"channel": "blog", "success": True, "publish_id": "x"}, "WordPress"),
    ("content_piece.published", {"channel": "facebook", "success": False}, "Facebook · failed"),
    ("content_piece.exported", {"channel": "blog", "format": "html"}, "WordPress · HTML"),
    ("tenant_tour_version.exported", {"format": "docx", "version_number": 3}, "DOCX · v3"),
    ("integration.disconnected", {"integration_type": "wordpress"}, "WordPress"),
    ("tenant.login", {"login_method": "api_key", "client": "portal"}, "Content studio"),
    ("tenant.login", {"login_method": "api_key"}, "API"),
    ("tenant.plan_changed", {"before": {"plan_tier": "starter", "plan_id": "p1"},
                             "after": {"plan_tier": "growth", "plan_id": "p2"}}, "Starter → Growth"),
    ("tenant.rate_limit_changed", {"before": {"rate_limit_rpm": 60}, "after": {"rate_limit_rpm": 300}},
     "300 requests / minute"),
    ("tenant.daily_publish_limit_changed", {"after": {"daily_publish_limit": None}}, "Default"),
    ("tenant.status_changed", {"before": "active", "after": "suspended"}, "Suspended"),
    ("tenant.key_rotated", {"name": "Agency name"}, ""),
    ("agency.onboard", {"name": "x", "plan_id": "secret"}, ""),
    ("content_piece.edited", '{"content_length": 12}', ""),
    ("content_piece.created", "not json", ""),
])
def test_detail_text_whitelist(action, details, expected):
    assert ta.detail_text(action, details) == expected


@pytest.mark.parametrize("actor, kind", [
    (f"tenant:{TENANT_A}", "you"), ("admin:42", "adventure_asia"), ("admin", "adventure_asia"),
    ("system", "system"), (None, "system"),
])
def test_actor_kind(actor, kind):
    assert ta.actor_kind(actor) == kind


def test_csv_escapes_formulas_and_marks_a_cut_file():
    item = ta.row_to_item(_row("tenant.login", resource_type="tenant", resource_id="=HYPERLINK(1)"))
    out = ta.items_to_csv([item], truncated=True)
    lines = out.strip().split("\n")
    assert lines[0] == ",".join(ta.CSV_HEADER)
    assert "'=HYPERLINK(1)" in lines[1]
    assert lines[1].split(",")[3] == "You"
    assert lines[-1].startswith("Only the newest")


# ── service with a fake pool ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_activity_binds_only_the_given_tenant():
    pool, conn = _pool(fetchval=1, fetch=[_row("tenant.login", resource_type="tenant")])
    out = await ta.list_activity(pool, TENANT_A, ta.ActivityFilters(category="account"), page=1, page_size=500)
    assert out["pagination"] == {"page": 1, "page_size": ta.MAX_PAGE_SIZE, "total": 1, "pages": 1}
    assert out["data"][0]["label"] == "Signed in with your API key"
    for call in (conn.fetchval.await_args, conn.fetch.await_args):
        assert call.args[1] == TENANT_A
        assert TENANT_B not in call.args


@pytest.mark.asyncio
async def test_list_activity_skips_the_page_query_when_empty():
    pool, conn = _pool(fetchval=0)
    out = await ta.list_activity(pool, TENANT_A, ta.ActivityFilters())
    assert out["data"] == [] and out["pagination"]["pages"] == 0
    conn.fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_export_cuts_at_the_bound(monkeypatch):
    monkeypatch.setattr(ta, "EXPORT_MAX_ROWS", 2)
    pool, conn = _pool(fetch=[_row("tenant.login", resource_type="tenant")] * 3)
    out = await ta.export_activity_csv(pool, TENANT_A, ta.ActivityFilters())
    assert conn.fetch.await_args.args[-1] == 3  # bound + 1, to know whether the file was cut
    assert out.count("Signed in with your API key") == 2
    assert out.strip().endswith("Narrow the dates to export the rest.")


@pytest.mark.asyncio
async def test_api_key_facts_masks_and_takes_the_latest_use():
    t1 = datetime(2026, 10, 1, tzinfo=timezone.utc)
    t2 = datetime(2026, 10, 5, tzinfo=timezone.utc)
    pool, conn = _pool(fetchrow={"has_key": True, "issued_at": t1, "last_sign_in_at": t1, "last_api_call_at": t2})
    out = await ta.api_key_facts(pool, TENANT_A)
    assert out["masked"] == "cis_" + "•" * 16
    assert out["last_used_at"] == t2.isoformat() and out["issued_at"] == t1.isoformat()
    args = conn.fetchrow.await_args.args
    assert str(args[1]) == TENANT_A and args[2] == TENANT_A
    assert set(args[3]) == set(ta.KEY_ISSUE_ACTIONS)
    assert "api_key_hash" not in out and not any("hash" in k for k in out)


@pytest.mark.asyncio
async def test_api_key_facts_revoked_or_unknown():
    pool, _ = _pool(fetchrow={"has_key": False, "issued_at": None, "last_sign_in_at": None,
                              "last_api_call_at": None})
    out = await ta.api_key_facts(pool, TENANT_A)
    assert out["has_key"] is False and out["masked"] is None and out["last_used_at"] is None
    pool, _ = _pool(fetchrow=None)
    assert (await ta.api_key_facts(pool, TENANT_A))["has_key"] is False


# ── routes ──────────────────────────────────────────────────────────────────────────────────────

def test_tenant_id_must_be_a_uuid():
    assert v1_account._tenant_id({"sub": TENANT_A.upper()}) == TENANT_A
    with pytest.raises(HTTPException) as e:
        v1_account._tenant_id({"sub": "not-a-uuid"})
    assert e.value.status_code == 403


@pytest.fixture
def client():
    # Only this router: the full app's middlewares (rate limit, Redis) are not what these tests are about.
    app = FastAPI()
    app.include_router(v1_account.router)
    pool, conn = _pool(fetchval=0, fetchrow=None)
    app.state.pool = pool
    return TestClient(app), conn


def _token(role="tenant", sub=TENANT_A):
    if role == "tenant":
        return _create_jwt(sub, "Agency", "starter", client="portal")
    payload = {"sub": sub, "role": role, "exp": datetime.now(timezone.utc) + timedelta(minutes=5)}
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALG)


def test_routes_need_a_tenant_token(client):
    c, _ = client
    for path in ("/v1/account/activity", "/v1/account/activity/export", "/v1/account/api-key"):
        assert c.get(path).status_code in (401, 403)
        r = c.get(path, headers={"Authorization": f"Bearer {_token(role='admin')}"})
        assert r.status_code == 403, (path, r.status_code)


def test_query_string_tenant_id_is_ignored(client):
    c, conn = client
    r = c.get(f"/v1/account/activity?tenant_id={TENANT_B}",
              headers={"Authorization": f"Bearer {_token()}"})
    assert r.status_code == 200, r.text
    assert conn.fetchval.await_args.args[1] == TENANT_A
    assert TENANT_B not in conn.fetchval.await_args.args


def test_bad_filters_are_400(client):
    c, _ = client
    h = {"Authorization": f"Bearer {_token()}"}
    assert c.get("/v1/account/activity?action=staff.request", headers=h).status_code == 400
    assert c.get("/v1/account/activity?since=2026-10-02&until=2026-10-01", headers=h).status_code == 400
    assert c.get("/v1/account/activity?page_size=101", headers=h).status_code == 422
    # far dates are a 400, not a 500 from the until + 1 day overflow (S251 review)
    assert c.get("/v1/account/activity?until=9999-12-31", headers=h).status_code == 400
    assert c.get("/v1/account/activity/export?until=9999-12-31", headers=h).status_code == 400
    assert c.get("/v1/account/activity?since=0001-01-01", headers=h).status_code == 400


def test_bounds_of_the_date_window_are_accepted():
    ta.validate_filters(ta.ActivityFilters(since=ta.DATE_FILTER_MIN, until=ta.DATE_FILTER_MAX))
    _, args = ta.build_where(TENANT_A, ta.ActivityFilters(until=ta.DATE_FILTER_MAX))
    assert args[-1] == datetime(2101, 1, 1, tzinfo=timezone.utc)


def test_export_is_csv(client):
    c, _ = client
    r = c.get("/v1/account/activity/export?category=content", headers={"Authorization": f"Bearer {_token()}"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert r.text.split("\n")[0] == ",".join(ta.CSV_HEADER)
