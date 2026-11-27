"""AA-796 — tenant-JWT /v1/* reads carry no score, QA state, score-derived status or LLM cost.
AA-1188 (DB-01) — nor AA's commercial / supplier data: supplier name and id, source price text, net / cost / margin.

Projection only: the stored tenant_tour_versions.status (needs_review = T2 score < 7.0) is the
admin signal and is not changed; tenant reads map it to ai_generated. Every route is called with
"poisoned" DB rows that carry every forbidden column, then the response is key-walked.
"""
import datetime as dt
import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.routers import _tenant_projection as tp
from api.routers import v1_marketplace, v1_tours

# AA-1028 R1 slice 3: the plan readers that replaced PLAN_LIMITS answer with the CIS 355 seed (tests/unit/conftest.py)
pytestmark = pytest.mark.usefixtures("seeded_plans")

TENANT = {"sub": str(uuid.uuid4())}
NOW = dt.datetime(2026, 10, 14, 9, 0, tzinfo=dt.timezone.utc)
PT_ID = uuid.uuid4()
TOUR_ID = uuid.uuid4()
VERSION_ID = uuid.uuid4()

# AA-920 WM-25: the writing-memory stamp a T2 version stores (CIS 297), with ids a tenant must never receive.
WM_RELEASE_ID = str(uuid.uuid4())
WM_RULE_ID = str(uuid.uuid4())
WM_GENERATION_META = {"writing_release": {"writer": "t2", "platform": {"release_id": WM_RELEASE_ID, "release_no": 1,
                                                                       "items_sha256": "a" * 64},
                                          "tenant": None, "ignored": False},
                      "rule_ids": [WM_RULE_ID]}

# Every forbidden column a real row can carry (published_tours, tenant_tour_versions, raw_tours,
# v_tenant_monthly_usage), plus the stored score-derived status. AA-920 WM-25: `generation_meta` (CIS 297) and the
# writing-memory keys a later join could add (`rule_ids`, `writing_release`, `memory_enabled`): dropped by name or by
# TENANT_FORBIDDEN_KEY_RE. AA-1047: `fs_id` (CIS 386), the admin FactSheet id a T2 version is stamped with.
# AA-1188 (DB-01): AA's commercial / supplier data a joined row could carry (raw_tours.provider / supplier_id /
# price_raw, and the net / cost / margin / markup columns of the booking schema). Values are checked too.
SUPPLIER_NAME = "Horizon Voyages DMC"
SOURCE_PRICE = "US$450 net per person"
_COMMERCIAL = {
    "provider": SUPPLIER_NAME, "supplier_id": uuid.uuid4(), "price_raw": SOURCE_PRICE, "net_amount": Decimal("310"),
    "target_margin": Decimal("0.18"), "markup_pct": Decimal("20"), "supplier_cost": Decimal("300"),
}
_POISON = {
    **_COMMERCIAL,
    "status": "needs_review", "quality_score": Decimal("5.5"), "quality_score_id": uuid.uuid4(),
    "qa_status": "escalated", "qa_repair_count": 2, "qa_checked_at": NOW, "qa_auto_passed": True,
    "llm_cost_usd": Decimal("0.12"), "jev_cost_usd": Decimal("0.01"),
    "generation_meta": WM_GENERATION_META, "rule_ids": [WM_RULE_ID], "writing_release": WM_GENERATION_META[
        "writing_release"], "memory_enabled": True, "fs_id": uuid.uuid4(),
}


_STAFF_TOUR_COLUMNS = {"approved_by": "staff@example.test", "deleted_by": None, "master_status_reason": "internal",
                       "human_lock_at": NOW, "human_lock_by": "staff@example.test",
                       "human_lock_reason": "held for a staff check", "s3_gold_path": "s3://bucket/gold/x.json",
                       "content_embedding": [0.1, 0.2]}


def _tour_row(**over):
    row = {"id": PT_ID, "tour_id": TOUR_ID, "aa_name": "Ha Long Bay", "aa_subtitle": "s", "aa_summary": "x",
           "aa_highlights": "[]", "aa_itineraries": "", "aa_description": "", "seo_title": "t", "seo_meta": "m",
           "seo_keywords_used": "[]", "published_at": NOW, "country": "Vietnam", "duration": "3 days",
           "already_rewritten": False, "generated_content_id": uuid.uuid4(),
           "master_status": "active", "deleted_at": None,
           "quality_score": Decimal("8.4"), "quality_score_id": uuid.uuid4(),
           # AA-1188: the staff / pipeline columns of a `SELECT pt.*` row
           **_STAFF_TOUR_COLUMNS, **_COMMERCIAL}
    row.update(over)
    return row


def _version_row(**over):
    row = {"id": VERSION_ID, "version_number": 1, "edit_source": "ai_generated", "rewrite_language": "en-US",
           "created_at": NOW, "rewritten_content": '{"name": "n", "status": "done"}', "published_tour_id": PT_ID,
           "tour_id": TOUR_ID, "aa_name": "Ha Long Bay", "country": "Vietnam", "duration": "3 days",
           "job_status": "succeeded", **_POISON}
    row.update(over)
    return row


def _raw_row():
    """Every silver_aa_internal.raw_tours column (docs/architecture/db-schema-reference.md), internal ones poisoned."""
    return {"tour_id": TOUR_ID, "tenant_id": uuid.uuid4(), "batch_id": uuid.uuid4(), "source_id": uuid.uuid4(),
            "tour_id_external": "EXT-1", "sku": "SKU-1", "provider": SUPPLIER_NAME,
            "src_name": "Ha Long", "src_subtitle": "s", "src_summary": "Call Horizon on +84 000",
            "src_description": "d", "src_highlights": [], "src_itineraries": "Day 1",
            "country": "Vietnam", "duration": "3 days", "group_size": "2-12", "period": "2026",
            "price_raw": SOURCE_PRICE, "inclusions": "Boat", "exclusions": "Drinks",
            "links": {"brochure": "https://supplier.example.test/x.pdf"}, "activities": ["kayaking"],
            "feature": "f", "best_time_to_go": "Oct-Apr", "pipeline_status": "published", "ingest_at": NOW,
            "review_status": "approved", "reviewed_by": uuid.uuid4(), "reviewed_at": NOW,
            "review_notes": "internal note", "source_group_id": uuid.uuid4(), "source_version": 2,
            "source_status": "active", "deleted_at": None, "deleted_by": "staff@example.test",
            "lifecycle_stage": "live", "country_raw_unresolved": None, "source_key": "k", "country_iso2": "VN",
            "supplier_id": uuid.uuid4(), "entry_origin": "sheet", "manual_facts": {"net": 310}}


class _Conn:
    """Answers each query by matching the SQL text, records every query it saw."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def _answer(self, sql, args, default):
        self.calls.append((sql, args))
        for needle, value in self.routes:
            if needle in sql:
                return value
        return default

    async def fetchrow(self, sql, *args):
        return self._answer(sql, args, None)

    async def fetch(self, sql, *args):
        return self._answer(sql, args, [])

    async def fetchval(self, sql, *args):
        return self._answer(sql, args, 0)


def _request(conn):
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    request = MagicMock()
    request.app.state.pool = pool
    request.app.state.redis.get = AsyncMock(return_value="false")  # S241 D-12: explicit OFF
    return request


def _string_values(obj):
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _string_values(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _string_values(v)
    elif isinstance(obj, str):
        yield obj


def assert_tenant_clean(resp):
    assert tp.forbidden_keys(resp) == []
    values = set(_string_values(resp))
    assert "needs_review" not in values
    # AA-920 WM-25: no writing-memory key at any depth, and no release / rule id as a value either
    flat = str(resp)
    for key in ("generation_meta", "rule_ids", "release", "memory"):
        assert key not in flat, key
    assert WM_RELEASE_ID not in flat and WM_RULE_ID not in flat
    # AA-1188 (DB-01): no supplier name and no source price text as a value either
    assert SUPPLIER_NAME not in flat and SOURCE_PRICE not in flat


# ── one call per tenant-JWT route in scope ────────────────────────────────────

async def _pool(sort="quality"):
    conn = _Conn([("SELECT COUNT(*)", 1), ("pt.aa_highlights", [_tour_row()]),
                  ("SELECT DISTINCT rt.country", [{"country": "Vietnam"}])])
    return await v1_tours.browse_pool(_request(conn), TENANT, page=1, page_size=20, country=None, search=None,
                                      duration_min=None, duration_max=None, sort=sort), conn


async def _my_versions(status=None):
    conn = _Conn([("SELECT COUNT(*)", 1), ("ttv.rewritten_content", [_version_row()])])
    return await v1_tours.list_my_versions(_request(conn), TENANT, page=1, page_size=20, status=status,
                                           country=None), conn


async def _version():
    history = [{"id": VERSION_ID, "version_number": 1, "status": "needs_review", "edit_source": "ai_generated",
                "created_at": NOW, "quality_score": Decimal("5")}]
    conn = _Conn([("SELECT ttv.*", _version_row()), ("ORDER BY version_number ASC", history)])
    return await v1_tours.get_version(str(VERSION_ID), _request(conn), TENANT), conn


async def _tour():
    conn = _Conn([("SELECT pt.*", _tour_row())])
    return await v1_tours.get_tour(str(PT_ID), _request(conn), TENANT), conn


def _full_conn():
    gen = {"id": uuid.uuid4(), "metadata": {"llm_cost": 0.1, "judge": {}}, "quality_score": 7.1}
    qs = {"id": uuid.uuid4(), "score_overall": 7.1, "evaluated_at": NOW}
    return _Conn([("published_tours", _tour_row()), ("raw_tours", _raw_row()),
                  ("quality_scores", qs), ("generated_content", gen),
                  ("seo_context", {"top_keywords": "[]", "fetched_at": NOW})])


async def _full_tenant():
    conn = _full_conn()
    creds = MagicMock(credentials="tenant-token")
    with patch.object(v1_tours, "verify_tenant_jwt", return_value=TENANT):
        return await v1_tours.get_tour_full(str(PT_ID), _request(conn), x_admin_secret=None,
                                            credentials=creds), conn


async def _billing():
    usage = {"tenant_name": "T", "plan_tier": "starter", "billing_month": dt.date(2026, 10, 1),
             "tours_quota_monthly": 50, "api_calls_quota_monthly": 5000, "price_usd_monthly": Decimal("299"),
             "tours_rewritten": 1, "api_calls_used": 3, "quota_tours_pct": 2.0, "quota_calls_pct": 0.1,
             "tours_overage": 0, "overage_usd": Decimal("0"), "overage_rate_usd_per_tour": Decimal("4"),
             "llm_cost_usd": Decimal("0.12"), "jev_cost_usd": Decimal("0.01")}
    activity = [{"id": VERSION_ID, "created_at": NOW, "status": "needs_review", "edit_source": "ai_generated",
                 "aa_name": "Ha Long Bay", "country": "Vietnam"}]
    conn = _Conn([("v_tenant_monthly_usage", usage), ("ttv.edit_source", activity), ("rate_limit_rpm", 60)])
    return await v1_tours.get_my_billing(_request(conn), TENANT), conn


async def _marketplace():
    row = {"version_id": VERSION_ID, "version_number": 1, "version_created_at": NOW, "published_tour_id": PT_ID,
           "tour_id": TOUR_ID, "name": "Ha Long Bay", "country": "Vietnam", "duration": "3 days",
           "catalog_source_id": "key-1", "atom_count": 3, **_POISON}
    conn = _Conn([("posts_per_week", {"posts_per_week": 3}), ("latest_versions", [row])])
    return await v1_marketplace.get_marketplace(_request(conn), TENANT), conn


TENANT_ROUTES = {
    "GET /v1/tours/pool?sort=quality": _pool,
    "GET /v1/tours/my-versions": _my_versions,
    "GET /v1/tours/versions/{id}": _version,
    "GET /v1/tours/{id}": _tour,
    "GET /v1/tours/{id}/full (tenant JWT)": _full_tenant,
    "GET /v1/billing": _billing,
    "GET /v1/marketplace": _marketplace,
}


@pytest.mark.asyncio
@pytest.mark.parametrize("route", sorted(TENANT_ROUTES))
async def test_tenant_response_has_no_quality_keys(route):
    resp, _ = await TENANT_ROUTES[route]()
    assert_tenant_clean(resp)


# ── per-route behaviour ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pool_sql_has_no_quality_and_quality_sort_falls_back_to_newest():
    _, conn = await _pool(sort="quality")
    for sql, _ in conn.calls:
        assert "quality" not in sql
    listing = next(sql for sql, _ in conn.calls if "pt.aa_highlights" in sql)
    assert "ORDER BY pt.published_at DESC" in listing


def test_pool_no_longer_takes_min_quality():
    import inspect
    assert "min_quality" not in inspect.signature(v1_tours.browse_pool).parameters


@pytest.mark.asyncio
async def test_my_versions_maps_needs_review_and_filter_matches_both():
    resp, conn = await _my_versions(status="ai_generated")
    assert resp["data"][0]["status"] == "ai_generated"
    sql, args = conn.calls[0]
    assert "ttv.status = ANY($2::text[])" in sql
    assert args[1] == ["ai_generated", "needs_review"]


@pytest.mark.asyncio
async def test_my_versions_other_status_filter_is_exact():
    _, conn = await _my_versions(status="approved")
    assert conn.calls[0][1][1] == ["approved"]


@pytest.mark.asyncio
async def test_version_detail_and_history_map_status():
    resp, _ = await _version()
    assert resp["status"] == "ai_generated"
    assert [h["status"] for h in resp["version_history"]] == ["ai_generated"]


@pytest.mark.asyncio
async def test_full_tenant_branch_never_reads_generated_or_quality():
    resp, conn = await _full_tenant()
    assert set(resp) == {"published", "raw", "seo"}
    assert not any("generated_content" in sql or "quality_scores" in sql for sql, _ in conn.calls)
    assert "review_notes" not in resp["raw"] and resp["raw"]["country"] == "Vietnam"


@pytest.mark.asyncio
async def test_full_tenant_raw_is_the_allowlist_only():
    """AA-1188 (DB-01): /full (tenant JWT) projects raw_tours through TENANT_RAW_FIELDS: trip facts, never the supplier,
    its id, the source price text, links, manual facts, the source copy or any pipeline / review / lifecycle column."""
    resp, _ = await _full_tenant()
    assert set(resp["raw"]) == set(tp.TENANT_RAW_FIELDS)
    for key in ("provider", "supplier_id", "price_raw", "links", "manual_facts", "deleted_by", "src_name",
                "src_summary", "batch_id", "sku", "pipeline_status", "source_status", "lifecycle_stage",
                "entry_origin", "tenant_id"):
        assert key not in resp["raw"], key
    assert resp["raw"]["inclusions"] == "Boat" and resp["raw"]["duration"] == "3 days"
    # and the published row loses the staff / pipeline columns of `SELECT pt.*`
    for key in _STAFF_TOUR_COLUMNS:
        assert key not in resp["published"], key
    assert resp["published"]["aa_name"] == "Ha Long Bay"


def test_raw_allowlist_holds_no_forbidden_column():
    assert not any(tp.tenant_forbidden_key(k) for k in tp.TENANT_RAW_FIELDS)
    # a forbidden key is refused even if someone adds it to the allowlist
    assert tp._only({"price_raw": "x", "country": "VN"}, {"price_raw", "country"}) == {"country": "VN"}


@pytest.mark.parametrize("key", ["provider", "supplier_id", "supplier", "price_raw", "price_raw_seen", "net_amount",
                                 "net", "supplier_net", "target_margin", "effective_margin", "margins", "markup_pct",
                                 "cost", "net_cost", "llm_cost_usd", "supplier_cost", "providers"])
def test_commercial_keys_are_forbidden(key):
    assert tp.TENANT_COMMERCIAL_KEY_RE.search(key) and tp.tenant_forbidden_key(key)
    assert tp.forbidden_keys({"x": [{key: 1}]}) == [f"x[0].{key}"]


@pytest.mark.parametrize("key", ["from_price", "price_usd", "price_available", "price_usd_monthly", "overage_usd",
                                 "overage_rate_usd_per_tour", "internet_fee", "planet", "costume", "network",
                                 "aa_name", "country", "duration", "inclusions", "exclusions", "activities",
                                 "generator", "cover_image_url", "already_rewritten", "atom_count"])
def test_tenant_content_keys_are_not_commercial(key):
    assert not tp.TENANT_COMMERCIAL_KEY_RE.search(key)


def test_tenant_reads_never_select_the_source_price_text():
    """AA-1188 (DB-01): the pool and version SQL read trip facts of raw_tours, never price_raw."""
    for sql in (v1_tours._POOL_ROWS_SQL, v1_tours._VERSION_SQL, v1_marketplace._MARKETPLACE_QUERY):
        assert "price_raw" not in sql


@pytest.mark.asyncio
async def test_full_admin_branch_unchanged():
    conn = _full_conn()
    with patch.dict("os.environ", {"ADMIN_SECRET": "s3cret"}):
        resp = await v1_tours.get_tour_full(str(PT_ID), _request(conn), x_admin_secret="s3cret", credentials=None)
    assert set(resp) == {"published", "raw", "generated", "quality", "seo"}
    assert resp["published"]["quality_score"] == 8.4 and resp["quality"]["score_overall"] == 7.1


@pytest.mark.asyncio
async def test_billing_drops_costs_and_maps_activity_status():
    resp, conn = await _billing()
    assert "llm_cost_usd" not in resp and "jev_cost_usd" not in resp
    assert resp["activity"][0]["status"] == "ai_generated"
    usage_sql = next(sql for sql, _ in conn.calls if "v_tenant_monthly_usage" in sql)
    assert "llm_cost" not in usage_sql and "jev_cost" not in usage_sql


@pytest.mark.asyncio
async def test_marketplace_low_score_version_reads_ready():
    resp, _ = await _marketplace()
    assert resp["tours"][0]["status"] == "ready"


# ── shared helpers ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("stored,seen", [("needs_review", "ai_generated"), ("ai_generated", "ai_generated"),
                                         ("pending", "pending"), ("failed", "failed"),
                                         ("approved", "approved"), ("rejected", "rejected"), (None, None)])
def test_tenant_version_status(stored, seen):
    assert tp.tenant_version_status(stored) == seen


def test_marketplace_reuses_the_shared_state_function():
    assert v1_marketplace.tenant_version_state is tp.tenant_version_state


def test_forbidden_keys_walks_nested_values():
    obj = {"a": [{"ok": 1, "qa_status": "x"}], "b": {"judge": {"brand_fit": 1}}, "jev_cost_usd": 0}
    assert sorted(tp.forbidden_keys(obj)) == ["a[0].qa_status", "b.judge", "jev_cost_usd"]


def test_writing_memory_keys_are_forbidden_at_any_depth():
    """AA-920 WM-25 Done-when: no tenant response carries `generation_meta`, `rule_ids`, `release` or `memory` keys."""
    obj = {"generation_meta": {}, "x": {"rule_ids": []}, "y": [{"writing_release": {"release_id": "r"}}],
           "memory_enabled": True}
    assert sorted(tp.forbidden_keys(obj)) == ["generation_meta", "memory_enabled", "x.rule_ids", "y[0].writing_release",
                                              "y[0].writing_release.release_id"]
    assert "generation_meta" in tp.TENANT_HIDDEN_VERSION_FIELDS
    # a version row keeps its content columns and loses every memory column
    row = {"id": "v", "rewritten_content": "{}", "created_at": NOW, **_POISON}
    out = tp.project_version(row)
    assert set(out) == {"id", "rewritten_content", "created_at", "status"}


# ── AA-802: a version corrected by an admin (silent correction) stays clean ────────────────────

_HELD_RC = {"name": "Ha Long Bay", "summary": "Cruise the bay. The boat carries 400 guests. Kayak at dusk.",
            "highlights": ["Sunrise on deck", "Cave walk"], "status": "done"}


def _corrected_rc():
    """What POST /admin/content-quality/tour-versions/{id}/sentences/apply writes: the same section
    splice (cut of the ungrounded sentence) into the tenant's rewritten_content, in place."""
    import json
    from api.routers import admin_content_quality as cq
    from services.acp_content_writing import sentence_repair
    text = _HELD_RC["summary"]
    bad = "The boat carries 400 guests."
    start = text.index(bad)
    new_text = sentence_repair.apply_edit(text, start, start + len(bad), "")
    return json.dumps(cq._t2_with_field(_HELD_RC, "summary", None, new_text))


async def _version_corrected():
    history = [{"id": VERSION_ID, "version_number": 1, "status": "needs_review", "edit_source": "ai_generated",
                "created_at": NOW}]
    conn = _Conn([("SELECT ttv.*", _version_row(rewritten_content=_corrected_rc())),
                  ("ORDER BY version_number ASC", history)])
    return await v1_tours.get_version(str(VERSION_ID), _request(conn), TENANT), conn


async def _my_versions_corrected():
    conn = _Conn([("SELECT COUNT(*)", 1), ("ttv.rewritten_content", [_version_row(rewritten_content=_corrected_rc())])])
    return await v1_tours.list_my_versions(_request(conn), TENANT, page=1, page_size=20, status=None,
                                           country=None), conn


@pytest.mark.asyncio
@pytest.mark.parametrize("call", [_version_corrected, _my_versions_corrected])
async def test_admin_corrected_version_reaches_the_tenant_clean(call):
    resp, conn = await call()
    assert_tenant_clean(resp)
    flat = str(resp)
    assert "Kayak at dusk." in flat and "400 guests" not in flat  # the repaired text, nothing else
    # the correction history (audit_log) and its gate names are admin-only: never read here
    assert not any("audit_log" in sql for sql, _ in conn.calls)
    assert "admin" not in flat and "T3_grounding" not in flat


def test_fact_sheet_id_never_reaches_the_tenant():
    """AA-1047: tenant_tour_versions.fs_id (CIS 386) is admin data; the version projection drops it and the key-walk
    flags it at any depth, while a key that only contains the letters is not caught."""
    assert "fs_id" in tp.TENANT_HIDDEN_VERSION_FIELDS
    row = {"id": "v", "rewritten_content": "{}", "created_at": NOW, "fs_id": uuid.uuid4()}
    assert "fs_id" not in tp.project_version(row)
    assert tp.forbidden_keys({"v": [{"fs_id": "x"}], "offs_idx": 1}) == ["v[0].fs_id"]


@pytest.mark.asyncio
async def test_version_detail_drops_the_fact_sheet_id():
    resp, _ = await _version()
    assert "fs_id" not in resp
    assert all("fs_id" not in h for h in resp["version_history"])
