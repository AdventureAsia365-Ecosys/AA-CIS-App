"""AA-686 — admin LLM-ops backend: stage route PATCH + shadow, catalog read/edit, shadow A/B
report. No live DB / no live LLM — asyncpg connections and the request pool are mocked.

Covers the contract's "Done when":
  * route validation (vendor rule, duplicate / primary-in-fallback, shadow == primary, unknown
    stage/model -> 404/422),
  * catalog edit (enable-without-price -> 422, unknown model -> 404),
  * the audit insert (before/after, same transaction),
  * the shadow-report math on fixture rows (agreement, score delta, repeat variance, unparsed).
"""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest
from fastapi import HTTPException

from api.routers import admin_llm_ops as ops
from shared.llm_client import catalog as catalog_mod
from shared.llm_client import role_config as rc_mod
from shared.llm_client.catalog import CatalogModel, CatalogPriceRequired


# ── catalog fixtures (mirrors test_aa658_model_catalog.py) ──────────────────────────────────────

def _model(key, **kw):
    base = dict(model_key=key, label=key, vendor="anthropic", provider="bedrock",
                api_style="converse", bedrock_profile_ids={"acc3": f"global.anthropic.{key}"},
                callable_via=("llm_client",), price_in_per_mtok=2.0, price_out_per_mtok=10.0,
                enabled=True)
    base.update(kw)
    return CatalogModel(**base)


SONNET5 = _model("sonnet-5")
HAIKU = _model("haiku", api_style="anthropic_native", price_in_per_mtok=1.0, price_out_per_mtok=5.0)
LUNA = _model("gpt-5.6-luna", vendor="openai", supports_temperature=False,
              bedrock_profile_ids={"acc3": "global.openai.gpt-5.6-luna"},
              price_in_per_mtok=0.1, price_out_per_mtok=0.5)
GPT41 = _model("gpt-4.1", vendor="openai", provider="openai", api_style="openai_chat",
               bedrock_profile_ids={}, callable_via=("llm_client", "openai_direct", "pinned"),
               price_in_per_mtok=2.0, price_out_per_mtok=8.0)
ALL = [SONNET5, HAIKU, LUNA, GPT41]


# ── test doubles for asyncpg ────────────────────────────────────────────────────────────────────

class _TxnCM:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _conn(fetchrow_side=None, fetchrow_return=None):
    conn = MagicMock()
    conn.transaction = MagicMock(return_value=_TxnCM())
    conn.execute = AsyncMock()
    conn.close = AsyncMock()
    if fetchrow_side is not None:
        conn.fetchrow = AsyncMock(side_effect=fetchrow_side)
    else:
        conn.fetchrow = AsyncMock(return_value=fetchrow_return)
    conn.fetch = AsyncMock(return_value=[])
    return conn


def _pool_with_conn(conn):
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    return pool


def _request_with_pool(conn):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=_pool_with_conn(conn))))


# ╔══════════════════════════════════════════════════════════════════════════════════════════════╗
# ║ 1. PATCH /admin/llm-config/{stage}/route — validation                                          ║
# ╚══════════════════════════════════════════════════════════════════════════════════════════════╝

JUDGE_STAGE = {"stage": "t10_judge", "role": "judge", "model_id": "gpt-5.6-luna",
               "fallback_model_ids": [], "shadow_model_id": None, "shadow_sample_pct": 0}


def _patch_route_ctx(stage_rows, setter=None):
    async def fake_list():
        return stage_rows

    async def fake_catalog():
        return ALL

    cm = [patch.object(ops, "verify_admin_secret"),
          patch.object(ops, "list_stage_configs", side_effect=fake_list),
          patch.object(ops, "_load_catalog", side_effect=fake_catalog)]
    if setter is not None:
        cm.append(patch.object(ops, "set_stage_route", setter))
    return cm


async def _call_route(body, stage_rows, setter=None):
    ctxs = _patch_route_ctx(stage_rows, setter)
    for c in ctxs:
        c.start()
    try:
        return await ops.patch_llm_route(
            "t10_judge", ops.LlmRoutePatch(**body), x_admin_secret="x", x_admin_user_id="admin1")
    finally:
        for c in ctxs:
            c.stop()


@pytest.mark.asyncio
async def test_route_unknown_stage_is_404():
    with pytest.raises(HTTPException) as exc:
        await _call_route({"fallback_model_ids": ["gpt-4.1"]}, stage_rows=[])
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_route_rejects_wrong_vendor_fallback():
    # sonnet-5 is Anthropic -> not available for a judge stage (vendor rule).
    with pytest.raises(HTTPException) as exc:
        await _call_route({"fallback_model_ids": ["sonnet-5"]}, stage_rows=[JUDGE_STAGE])
    assert exc.value.status_code == 422
    assert "not selectable" in exc.value.detail


@pytest.mark.asyncio
async def test_route_rejects_duplicate_fallback():
    with pytest.raises(HTTPException) as exc:
        await _call_route({"fallback_model_ids": ["gpt-4.1", "gpt-4.1"]}, stage_rows=[JUDGE_STAGE])
    assert exc.value.status_code == 422
    assert "duplicate" in exc.value.detail


@pytest.mark.asyncio
async def test_route_rejects_primary_in_fallback():
    with pytest.raises(HTTPException) as exc:
        await _call_route({"fallback_model_ids": ["gpt-5.6-luna"]}, stage_rows=[JUDGE_STAGE])
    assert exc.value.status_code == 422
    assert "primary" in exc.value.detail


@pytest.mark.asyncio
async def test_route_rejects_shadow_equal_primary():
    with pytest.raises(HTTPException) as exc:
        await _call_route({"shadow_model_id": "gpt-5.6-luna"}, stage_rows=[JUDGE_STAGE])
    assert exc.value.status_code == 422
    assert "differ from the primary" in exc.value.detail


@pytest.mark.asyncio
async def test_route_rejects_shadow_sample_pct_out_of_range():
    with pytest.raises(HTTPException) as exc:
        await _call_route({"shadow_sample_pct": 150}, stage_rows=[JUDGE_STAGE])
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_route_valid_update_calls_setter():
    captured = {}

    async def setter(stage, **kw):
        captured.update(stage=stage, **kw)
        return {"stage": stage, "model_id": "gpt-5.6-luna", "updated_at": None,
                "fallback_model_ids": kw["fallback_model_ids"],
                "shadow_model_id": kw["shadow_model_id"],
                "shadow_sample_pct": kw["shadow_sample_pct"]}

    res = await _call_route({"fallback_model_ids": ["gpt-4.1"], "shadow_model_id": "gpt-4.1",
                             "shadow_sample_pct": 50}, stage_rows=[JUDGE_STAGE], setter=setter)
    assert captured["fallback_model_ids"] == ["gpt-4.1"]
    assert captured["shadow_model_id"] == "gpt-4.1"
    assert captured["shadow_sample_pct"] == 50
    assert captured["audit_actor"] == "admin:admin1"
    assert res["route"]["shadow"]["model_id"] == "gpt-4.1"


# ╔══════════════════════════════════════════════════════════════════════════════════════════════╗
# ║ 2. set_stage_route store helper — audit insert (before/after) in the same transaction          ║
# ╚══════════════════════════════════════════════════════════════════════════════════════════════╝

@pytest.mark.asyncio
async def test_set_stage_route_audits_before_after_in_transaction():
    before = {"model_id": "gpt-5.6-luna", "fallback_model_ids": ["old"],
              "shadow_model_id": None, "shadow_sample_pct": 0}
    after_row = {"stage": "t10_judge", "role": "judge", "provider": "openai",
                 "model_id": "gpt-5.6-luna", "account_route": None, "is_active": True,
                 "updated_at": None, "updated_by": "admin:admin1",
                 "fallback_model_ids": ["gpt-4.1"], "shadow_model_id": "gpt-4.1",
                 "shadow_sample_pct": 25}
    conn = _conn(fetchrow_side=[before, after_row])

    with patch.object(rc_mod.asyncpg, "connect", AsyncMock(return_value=conn)), \
            patch.object(rc_mod, "get_database_url", return_value="postgresql://x"), \
            patch.object(rc_mod, "invalidate"):
        out = await rc_mod.set_stage_route(
            "t10_judge", fallback_model_ids=["gpt-4.1"], shadow_model_id="gpt-4.1",
            shadow_sample_pct=25, updated_by="admin:admin1", audit_actor="admin:admin1")

    assert out["fallback_model_ids"] == ["gpt-4.1"]
    # transaction was opened and the audit insert ran inside it
    conn.transaction.assert_called_once()
    audit_call = conn.execute.call_args
    assert "acp_shared.audit_log" in audit_call.args[0]
    assert audit_call.args[1] == "admin:admin1"          # actor
    assert audit_call.args[2] == "llm_route_changed"     # action
    assert audit_call.args[3] == "llm_role_config"       # resource_type
    assert audit_call.args[4] == "t10_judge"             # resource_id
    details = json.loads(audit_call.args[5])
    assert details["before"]["fallback_model_ids"] == ["old"]
    assert details["after"]["fallback_model_ids"] == ["gpt-4.1"]
    assert details["after"]["shadow_model_id"] == "gpt-4.1"


@pytest.mark.asyncio
async def test_set_stage_route_unknown_stage_raises_value_error():
    conn = _conn(fetchrow_side=[None, None])  # before=None, update returns None
    with patch.object(rc_mod.asyncpg, "connect", AsyncMock(return_value=conn)), \
            patch.object(rc_mod, "get_database_url", return_value="postgresql://x"), \
            patch.object(rc_mod, "invalidate"):
        with pytest.raises(ValueError):
            await rc_mod.set_stage_route("nope", fallback_model_ids=[], shadow_model_id=None,
                                         shadow_sample_pct=0, updated_by="u", audit_actor="a")


@pytest.mark.asyncio
async def test_set_stage_config_audits_model_change():
    before = {"model_id": "haiku", "account_route": "acc3"}
    after_row = {"stage": "t9_write", "role": "writer", "provider": "claude", "model_id": "sonnet",
                 "account_route": "acc3", "is_active": True, "updated_at": None,
                 "updated_by": "admin:a", "fallback_model_ids": [], "shadow_model_id": None,
                 "shadow_sample_pct": 0}
    conn = _conn(fetchrow_side=[before, after_row])
    with patch.object(rc_mod.asyncpg, "connect", AsyncMock(return_value=conn)), \
            patch.object(rc_mod, "get_database_url", return_value="postgresql://x"), \
            patch.object(rc_mod, "invalidate"):
        await rc_mod.set_stage_config("t9_write", "sonnet", "acc3", updated_by="admin:a",
                                      audit_actor="admin:a")
    audit_call = conn.execute.call_args
    assert audit_call.args[2] == "llm_model_changed"
    details = json.loads(audit_call.args[5])
    assert details["before"] == {"model_id": "haiku", "account_route": "acc3"}
    assert details["after"] == {"model_id": "sonnet", "account_route": "acc3"}


# ╔══════════════════════════════════════════════════════════════════════════════════════════════╗
# ║ 3. catalog edit — enable-without-price -> 422, unknown -> 404, audit                            ║
# ╚══════════════════════════════════════════════════════════════════════════════════════════════╝

@pytest.mark.asyncio
async def test_catalog_patch_enable_without_price_maps_check_to_422():
    async def raiser(model_key, **kw):
        raise CatalogPriceRequired("cannot enable a model without both an input and an output price")

    with patch.object(ops, "verify_admin_secret"), \
            patch.object(ops, "set_catalog_price", raiser):
        with pytest.raises(HTTPException) as exc:
            await ops.patch_llm_catalog("opus-5-5", ops.LlmCatalogPatch(enabled=True),
                                        x_admin_secret="x", x_admin_user_id="a")
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_catalog_patch_unknown_model_is_404():
    async def raiser(model_key, **kw):
        raise ValueError("unknown model_key: 'nope'")

    with patch.object(ops, "verify_admin_secret"), \
            patch.object(ops, "set_catalog_price", raiser):
        with pytest.raises(HTTPException) as exc:
            await ops.patch_llm_catalog("nope", ops.LlmCatalogPatch(price_in_per_mtok=1.0),
                                        x_admin_secret="x", x_admin_user_id="a")
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_catalog_patch_empty_body_is_422():
    with patch.object(ops, "verify_admin_secret"):
        with pytest.raises(HTTPException) as exc:
            await ops.patch_llm_catalog("haiku", ops.LlmCatalogPatch(),
                                        x_admin_secret="x", x_admin_user_id="a")
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_set_catalog_price_maps_db_check_to_price_required():
    conn = _conn()
    conn.fetchrow = AsyncMock(side_effect=[
        {"price_in_per_mtok": None, "price_out_per_mtok": None, "price_source": None,
         "enabled": False},
        asyncpg.exceptions.CheckViolationError(
            'new row for relation "llm_model_catalog" violates check constraint '
            '"llm_model_catalog_enabled_needs_price"'),
    ])

    with patch.object(catalog_mod.asyncpg, "connect", AsyncMock(return_value=conn)), \
            patch.object(catalog_mod, "get_database_url", return_value="postgresql://x"), \
            patch.object(catalog_mod, "invalidate"):
        with pytest.raises(CatalogPriceRequired):
            await catalog_mod.set_catalog_price("opus-5-5", fields={"enabled": True},
                                                updated_by="u", audit_actor="a")


@pytest.mark.asyncio
async def test_set_catalog_price_audits_before_after():
    before = {"price_in_per_mtok": 2.0, "price_out_per_mtok": 10.0, "price_source": "old",
              "enabled": True}
    after_row = {"model_key": "sonnet-5", "label": "Sonnet 5", "vendor": "anthropic",
                 "provider": "bedrock", "api_style": "converse", "bedrock_profile_ids": {},
                 "wire_model": None, "callable_via": ["llm_client"], "supports_temperature": True,
                 "max_output_tokens": None, "price_in_per_mtok": 3.0, "price_out_per_mtok": 10.0,
                 "price_cache_read_per_mtok": None, "price_cache_write_per_mtok": None,
                 "price_source": "new", "enabled": True, "blocked_reason": None, "notes": None,
                 "updated_at": None, "updated_by": "a"}
    conn = _conn()
    conn.fetchrow = AsyncMock(side_effect=[before, after_row])

    with patch.object(catalog_mod.asyncpg, "connect", AsyncMock(return_value=conn)), \
            patch.object(catalog_mod, "get_database_url", return_value="postgresql://x"), \
            patch.object(catalog_mod, "invalidate"):
        out = await catalog_mod.set_catalog_price(
            "sonnet-5", fields={"price_in_per_mtok": 3.0, "price_source": "new"},
            updated_by="u", audit_actor="admin:a")

    assert out["price_in_per_mtok"] == 3.0
    audit_call = conn.execute.call_args
    assert "acp_shared.audit_log" in audit_call.args[0]
    assert audit_call.args[2] == "llm_catalog_changed"
    assert audit_call.args[3] == "llm_model_catalog"
    assert audit_call.args[4] == "sonnet-5"
    details = json.loads(audit_call.args[5])
    assert details["before"]["price_in_per_mtok"] == 2.0
    assert details["after"]["price_in_per_mtok"] == 3.0


@pytest.mark.asyncio
async def test_set_catalog_price_rejects_non_editable_fields():
    with patch.object(catalog_mod.asyncpg, "connect", AsyncMock()), \
            patch.object(catalog_mod, "get_database_url", return_value="postgresql://x"):
        with pytest.raises(ValueError):
            await catalog_mod.set_catalog_price("haiku", fields={"vendor": "openai"},
                                                updated_by="u", audit_actor="a")


# ╔══════════════════════════════════════════════════════════════════════════════════════════════╗
# ║ 4. shadow report math                                                                          ║
# ╚══════════════════════════════════════════════════════════════════════════════════════════════╝

def test_parse_judge_output_rubric_pass_fraction():
    raw = json.dumps({"items": [{"score": "1"}, {"score": "1"}, {"score": "0"}]})
    out = ops._parse_judge_output("t10_judge", raw)
    assert out["score"] == pytest.approx(2 / 3)
    assert out["passed"] is False


def test_parse_judge_output_status_pass():
    raw = json.dumps({"status": "pass", "failure_codes": []})
    out = ops._parse_judge_output("s1_brand_audit", raw)
    assert out["passed"] is True
    assert out["score"] is None


def test_parse_judge_output_brand_audit_nested_status():
    raw = json.dumps({"brand_audit": {"status": "manual_check", "failure_codes": ["X"]}})
    out = ops._parse_judge_output("s1_brand_audit", raw)
    assert out["passed"] is False


def test_parse_judge_output_numeric_brand_fit_score():
    raw = json.dumps({"brand_fit_score": 8, "status": "pass"})
    out = ops._parse_judge_output("s1_judge", raw)
    assert out["score"] == 8.0 and out["passed"] is True


def test_parse_judge_output_salvages_fenced_json():
    raw = "```json\n{\"status\": \"pass\"}\n```"
    out = ops._parse_judge_output("t10_judge", raw)
    assert out["passed"] is True


def test_parse_judge_output_unparsable_is_none():
    assert ops._parse_judge_output("t10_judge", "this is not json at all {{{") is None
    assert ops._parse_judge_output("t10_judge", "") is None
    assert ops._parse_judge_output("t10_judge", None) is None


def test_parse_judge_output_json_without_signals_is_none():
    assert ops._parse_judge_output("t10_judge", json.dumps({"notes": "hello"})) is None


def test_percentile_and_stddev_helpers():
    assert ops._percentile([], 50) is None
    assert ops._percentile([5.0], 95) == 5.0
    assert ops._percentile([1.0, 2.0, 3.0, 4.0], 50) == 2.0
    assert ops._percentile([1.0, 2.0, 3.0, 4.0], 100) == 4.0
    assert ops._stddev([5.0]) is None
    assert ops._stddev([2.0, 4.0]) == pytest.approx(1.0)


def _row(stage, primary_out, shadow_out, *, sha="s", err=None, p_cost=0.01, s_cost=0.002, lat=100):
    return {"stage": stage, "primary_model": "gpt-5.6-luna", "shadow_model": "gpt-4.1",
            "primary_output": primary_out, "shadow_output": shadow_out, "shadow_error": err,
            "primary_cost_usd": p_cost, "shadow_cost_usd": s_cost, "shadow_latency_ms": lat,
            "request_sha256": sha}


def test_summarize_shadow_group_agreement_delta_cost():
    rows = [
        _row("s1_judge", json.dumps({"brand_fit_score": 8, "status": "pass"}),
             json.dumps({"brand_fit_score": 6, "status": "pass"}), sha="a"),
        _row("s1_judge", json.dumps({"brand_fit_score": 5, "status": "fail"}),
             json.dumps({"brand_fit_score": 9, "status": "pass"}), sha="b"),
    ]
    g = ops._summarize_shadow_group("s1_judge", rows)
    assert g["n"] == 2
    assert g["agreement_sample"] == 2
    assert g["agreement_rate"] == pytest.approx(0.5)   # row a agrees (pass/pass), row b disagrees
    # |8-6| = 2, |5-9| = 4 -> mean 3
    assert g["mean_abs_score_delta"] == pytest.approx(3.0)
    assert g["primary_cost_usd"] == pytest.approx(0.02)
    assert g["shadow_cost_usd"] == pytest.approx(0.004)
    assert g["primary_cost_per_call"] == pytest.approx(0.01)
    assert g["shadow_latency_p50_ms"] == 100
    assert g["unparsed"] == 0


def test_summarize_shadow_group_counts_unparsed_not_dropped():
    rows = [
        _row("t10_judge", json.dumps({"status": "pass"}), json.dumps({"status": "pass"}), sha="a"),
        _row("t10_judge", "garbage not json", json.dumps({"status": "pass"}), sha="b"),
    ]
    g = ops._summarize_shadow_group("t10_judge", rows)
    assert g["n"] == 2
    assert g["unparsed"] == 1
    assert g["agreement_sample"] == 1       # only the parsable row counts toward agreement


def test_summarize_shadow_group_shadow_error_counted():
    rows = [
        _row("t10_judge", json.dumps({"status": "pass"}), None, err="402 billing", s_cost=None),
        _row("t10_judge", json.dumps({"status": "pass"}), json.dumps({"status": "pass"}), sha="b"),
    ]
    g = ops._summarize_shadow_group("t10_judge", rows)
    assert g["shadow_error"] == 1
    # the errored row still parses its primary; it is not unparsed and not an agreement sample
    assert g["unparsed"] == 0
    assert g["agreement_sample"] == 1


def test_summarize_shadow_group_repeat_scoring_variance():
    # Same request_sha256 scored twice by each model: primary 8 then 8 (stddev 0), shadow 6 then 8
    # (stddev 1.0 population).
    rows = [
        _row("s1_judge", json.dumps({"brand_fit_score": 8}), json.dumps({"brand_fit_score": 6}),
             sha="same"),
        _row("s1_judge", json.dumps({"brand_fit_score": 8}), json.dumps({"brand_fit_score": 8}),
             sha="same"),
    ]
    g = ops._summarize_shadow_group("s1_judge", rows)
    assert g["primary_repeat_score_stddev"] == pytest.approx(0.0)
    assert g["shadow_repeat_score_stddev"] == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_shadow_report_endpoint_groups_by_stage_and_models():
    rows = [
        _row("s1_judge", json.dumps({"brand_fit_score": 8}), json.dumps({"brand_fit_score": 8})),
        _row("t10_judge", json.dumps({"status": "pass"}), json.dumps({"status": "fail"})),
    ]
    conn = _conn()
    conn.fetch = AsyncMock(return_value=rows)
    request = _request_with_pool(conn)
    res = await ops.get_llm_shadow_report(request, days=30)
    assert res["days"] == 30
    assert [g["stage"] for g in res["groups"]] == ["s1_judge", "t10_judge"]
