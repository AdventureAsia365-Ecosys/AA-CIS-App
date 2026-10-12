"""AA-741 pre-audit hardening — the T-series must be measurable before the 4-tenant audit run.

Each test pins one silent failure found while reading the code (S229):
1. Jev DPA guard failed OPEN when a T-series call site forgot `tenant_id`.
2. `brand_audit_node.meal_time_claims_supported` passed a str where `judge_units` wants
   {unit index: per-unit source} (since #631) → every call failed open → the A1-3 code fired
   on every meal mention; and tenant text would have gone to Jev with no tenant id.
3. T2 `_prepare` swallowed a brand-load DB error into an unbranded rewrite (AA-425 mode).
4. `trigger_rewrite` billed the quota before checking the tour, did not apply the active-Master
   gate, and let a double click create (and bill) two pending versions.
5. Tenant endpoints accepted a staff JWT (same secret, role never checked).
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest
from fastapi import HTTPException

from shared.llm_client import decide as dmod


# ── 1. Jev tenant guard ──────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("tenant,stage,expected", [
    (None, "a3_research", True),          # platform content, platform stage
    (None, "s1_grounding", True),
    (None, "t3_grounding", False),        # tenant stage without a tenant → fail closed
    (None, "t10_gates", False),
    ("None", "t7_debate", False),         # str(None) at a call site counts as missing
    ("", "t8_angle_rank", False),
    (dmod.PLATFORM_TENANT_ID, "t2_judge_tiebreak", True),
    ("not-listed", "a3_research", False),
])
def test_tenant_allowed(tenant, stage, expected, monkeypatch):
    monkeypatch.setattr(dmod, "_allowlist", {"listed"})
    assert dmod.tenant_allowed(tenant, stage) is expected


def test_allow_listed_tenant_is_allowed_on_tenant_stage(monkeypatch):
    monkeypatch.setattr(dmod, "_allowlist", {"listed"})
    assert dmod.tenant_allowed("listed", "t9_facts") is True


def test_tenant_stage_without_tenant_is_skipped_and_never_calls_jev(monkeypatch):
    q = dmod.Question(key="a1_claim_supported", stage="t3_grounding", kind="noul", instructions="",
                      criteria=None, mode="enforce", accept_floor=0.9, reject_ceiling=None,
                      threshold_version=1, question_hash="h")
    monkeypatch.setattr(dmod, "_load_config", AsyncMock())
    monkeypatch.setattr(dmod, "_questions", {"a1_claim_supported": q})
    call_jev = AsyncMock()
    monkeypatch.setattr(dmod, "_call_jev", call_jev)

    class _Db:
        def acquire(self):
            ctx = AsyncMock()
            ctx.__aenter__ = AsyncMock(return_value=AsyncMock())
            ctx.__aexit__ = AsyncMock(return_value=False)
            return ctx

    import asyncio
    d = asyncio.run(dmod._decide(_Db(), "t3_grounding", "k", {"s": 1}, ["a1_claim_supported"], None))
    v = d.verdicts["a1_claim_supported"]
    assert v.zone == "skipped" and "without tenant_id" in (v.error or "")
    call_jev.assert_not_called()


# ── 2. judge_units contract + meal/time check ────────────────────────────────────────────────

def test_judge_units_rejects_a_plain_source_string():
    from services.content_generation import grounding as g
    with pytest.raises(TypeError):
        g.judge_units([{"sentence": "x"}], "the whole source")


TOUR = {"itineraries": "Day 5: Depart Peradeniya station at 12:30 PM. Dinner included at the lodge."}
ITIN = "Day 5 — Lodge\nDinner is included at the lodge tonight."


def test_meal_check_sends_per_unit_sources_and_no_tenant_for_a1():
    from services.content_generation import brand_audit_node as ba
    with patch("services.content_generation.grounding.judge_units", return_value={}) as m:
        ba.meal_time_claims_supported(ITIN, TOUR)
    sources = m.call_args.args[1]
    assert isinstance(sources, dict) and sources and all(isinstance(v, str) for v in sources.values())
    assert m.call_args.kwargs["tenant_id"] is None


def test_meal_check_carries_the_tenant_on_a_tenant_rewrite():
    from services.content_generation import brand_audit_node as ba
    with patch("services.content_generation.grounding.judge_units", return_value={}) as m:
        ba.meal_time_claims_supported(ITIN, TOUR, tenant_id="t-1", is_tenant_rewrite=True)
    assert m.call_args.kwargs["tenant_id"] == "t-1"


def test_meal_check_never_sends_ownerless_tenant_text():
    from services.content_generation import brand_audit_node as ba
    with patch("services.content_generation.grounding.judge_units") as m:
        assert ba.meal_time_claims_supported(ITIN, TOUR, tenant_id=None, is_tenant_rewrite=True) is False
    m.assert_not_called()


# ── 3. T2 brand load ─────────────────────────────────────────────────────────────────────────

def _pool(conn):
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    return pool


PT = {"aa_name": "Sapa Trek", "country": "Vietnam", "duration": "3D2N"}


@pytest.mark.asyncio
async def test_brand_load_error_raises_instead_of_writing_unbranded():
    from services.jobs import t2_rewrite_job as t2
    conn = AsyncMock()
    conn.fetchrow.side_effect = RuntimeError("pool exhausted")
    with pytest.raises(RuntimeError):
        await t2._prepare(_pool(conn), PT, "t-1", "EN-GB")


@pytest.mark.asyncio
async def test_tenant_without_brand_row_keeps_language_and_is_flagged():
    from services.jobs import t2_rewrite_job as t2
    conn = AsyncMock()
    conn.fetchrow.return_value = None
    _, rules = await t2._prepare(_pool(conn), PT, "t-1", "EN-GB")
    assert rules["rewrite_language"] == "EN-GB"
    assert rules["_brand_loaded"] is False


# ── 4. trigger_rewrite: validate first, charge last ──────────────────────────────────────────

class _Txn:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc):
        return False


def _rewrite_call(conn):
    from api.routers import v1_tours
    conn.transaction = MagicMock(return_value=_Txn())
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=_pool(conn))))
    body = v1_tours.RewriteRequest(rewrite_language="en-US", seo_mode="standard")
    return v1_tours.trigger_rewrite("pt-1", body, request, {"sub": "t-1"})


def _charged(conn) -> bool:
    return any("tenant_rewrite_usage" in str(c.args[0]) for c in conn.fetchval.call_args_list)


@pytest.mark.asyncio
async def test_inactive_or_missing_tour_is_404_and_not_billed():
    conn = AsyncMock()
    conn.fetchrow.return_value = None           # the active-Master gate filters it out
    with pytest.raises(HTTPException) as e:
        await _rewrite_call(conn)
    assert e.value.status_code == 404
    assert "master_status = 'active'" in conn.fetchrow.call_args.args[0]
    assert not _charged(conn)


@pytest.mark.asyncio
async def test_second_click_while_pending_is_409_and_not_billed():
    conn = AsyncMock()
    conn.fetchrow.return_value = {"id": "pt-1", "aa_name": "Sapa Trek"}
    conn.fetchval.side_effect = ["v-existing"]  # in-flight version found
    with pytest.raises(HTTPException) as e:
        await _rewrite_call(conn)
    assert e.value.status_code == 409
    assert not _charged(conn)
    assert any("pg_advisory_xact_lock" in c.args[0] for c in conn.execute.call_args_list)


# ── 5. staff JWT on tenant endpoints ─────────────────────────────────────────────────────────

def _token(role):
    from api.routers import auth
    return jwt.encode({"sub": "u-1", "role": role}, auth.JWT_SECRET, algorithm=auth.JWT_ALG)


@pytest.mark.parametrize("role", ["admin", "reviewer", "content"])
def test_staff_token_is_refused_by_tenant_auth(role):
    from api.routers import auth, v1_tours
    with pytest.raises(HTTPException) as e:
        auth.verify_tenant_jwt(_token(role))
    assert e.value.status_code == 403
    with pytest.raises(HTTPException) as e2:
        v1_tours.get_tenant(SimpleNamespace(credentials=_token(role)))
    assert e2.value.status_code == 403


def test_tenant_token_still_works():
    from api.routers import auth, v1_tours
    assert auth.verify_tenant_jwt(_token("tenant"))["sub"] == "u-1"
    assert v1_tours.get_tenant(SimpleNamespace(credentials=_token("tenant")))["role"] == "tenant"


def test_brand_identity_resolver_refuses_staff_token():
    from api.routers import admin_pipeline
    with pytest.raises(HTTPException) as e:
        admin_pipeline._resolve_brand_tenant_id(SimpleNamespace(credentials=_token("admin")), None)
    assert e.value.status_code == 403
