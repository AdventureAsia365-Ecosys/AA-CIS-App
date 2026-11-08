"""AA-1052 W6 — the runtime edit gate of the writing path's admin surfaces (services/quality/edit_gate.py).

A gated edit (a stage of change_control.GATED_STAGE_PREFIXES, or a writer / judge stage) of a stage model / chain, a
catalog row that serves such a stage, the hold switch of a route policy, or a route-control save:
  * is refused with 409 while a rerun wave is open (shared.rerun_wave.status = 'open');
  * otherwise needs an eval run id or a break-glass reason (422 without), stored in the audit row.
No database, no AWS: routes, catalog rows and waves are fakes. Invented stages and reasons only.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from api.routers import admin_llm_ops as ops
from api.routers import admin_llm_route as route_api
from api.services import llm_route_control as rc
from api.services import llm_route_trace as trace_svc
from services.quality import change_control, edit_gate
from shared.llm_client import route_evidence
from tests.unit.test_aa1024_route_governance import BREAK_GLASS, JUDGE_ROW, RUN_ID, _call, _patch_ops

REAL_READ_OPEN_WAVES = edit_gate.read_open_waves     # before the unit conftest's autouse stub replaces it
WAVE = [{"id": "11111111-2222-4333-8444-555555555555", "name": "Invented wave"}]
GATED_VALIDATE = {**JUDGE_ROW, "stage": "t9_invented_check", "role": "validate"}
PLAIN_VALIDATE = {**JUDGE_ROW, "stage": "invented_check", "role": "validate"}


def _waves(monkeypatch, waves):
    async def reader(pool=None):
        return waves
    monkeypatch.setattr(edit_gate, "read_open_waves", reader)


# ── the rule ──────────────────────────────────────────────────────────────────────────────────────────────────────

def test_gated_stages_are_the_change_control_prefixes_writer_judge_and_the_policy_defaults():
    for p in change_control.GATED_STAGE_PREFIXES:
        assert edit_gate.is_gated_stage(p + "x" if p.endswith("_") else p, "validate")
    assert edit_gate.is_gated_stage("invented_judge", "judge")
    assert edit_gate.is_gated_stage("invented_writer", "writer")
    assert edit_gate.is_gated_stage("*")
    assert not edit_gate.is_gated_stage("invented_check", "validate")
    assert not edit_gate.is_gated_stage("place_card_draft", None)
    assert edit_gate.gated([("t2_x", "validate"), ("a3_x", "validate"), ("j", "judge")]) == ["j", "t2_x"]


def test_needs_evidence_takes_the_stage_prefix_and_keeps_the_role_rule():
    assert route_evidence.needs_evidence("judge") and route_evidence.needs_evidence("writer")
    assert not route_evidence.needs_evidence("validate")
    assert route_evidence.needs_evidence("validate", "t10_invented")
    assert not route_evidence.needs_evidence("validate", "a3_invented")


def test_the_frozen_message_names_the_wave_and_the_stages():
    err = edit_gate.frozen_error("the route of stage t2_x", ["t2_x", "t2_x"], WAVE)
    assert isinstance(err, edit_gate.EditFrozen)
    assert "Invented wave" in str(err) and "t2_x" in str(err) and err.stages == ["t2_x"] and err.waves == WAVE


def test_refuse_if_frozen_reads_the_waves_only_for_a_gated_edit(monkeypatch):
    calls = []

    async def reader(pool=None):
        calls.append(pool)
        return WAVE
    monkeypatch.setattr(edit_gate, "read_open_waves", reader)
    asyncio.run(edit_gate.refuse_if_frozen("x", []))
    assert calls == []
    with pytest.raises(edit_gate.EditFrozen):
        asyncio.run(edit_gate.refuse_if_frozen("x", ["t2_x"], pool="p"))
    assert calls == ["p"]


def test_read_open_waves_uses_the_open_status_check():
    assert "FROM shared.rerun_wave WHERE status = 'open'" in edit_gate.OPEN_WAVES_SQL
    pool = MagicMock(fetch=AsyncMock(return_value=[{"id": "w", "name": "n"}]))
    assert asyncio.run(REAL_READ_OPEN_WAVES(pool)) == [{"id": "w", "name": "n"}]
    assert pool.fetch.await_args.args[0] == edit_gate.OPEN_WAVES_SQL
    # a pool-like object that only lends connections (the route control's test pool)
    conn = MagicMock(fetch=AsyncMock(return_value=[]))
    acquire = MagicMock()
    acquire.__aenter__ = AsyncMock(return_value=conn)
    acquire.__aexit__ = AsyncMock(return_value=False)
    lender = MagicMock(spec=["acquire"], acquire=MagicMock(return_value=acquire))
    assert asyncio.run(REAL_READ_OPEN_WAVES(lender)) == [] and conn.fetch.await_count == 1


def test_require_evidence_for_the_policy_defaults_row():
    with pytest.raises(route_evidence.EvidenceRefused, match="route policy"):
        asyncio.run(edit_gate.require_evidence("*", None, eval_run_id=None, break_glass_reason=None,
                                               what="hold switch change of the route policy *"))
    got = asyncio.run(edit_gate.require_evidence("*", None, eval_run_id=None, break_glass_reason=BREAK_GLASS))
    assert got == {"break_glass_reason": BREAK_GLASS}
    assert asyncio.run(edit_gate.require_evidence("a3_x", "validate", eval_run_id=None,
                                                  break_glass_reason=None)) is None


# ── stage model / chain (admin_llm_ops) ─────────────────────────────────────────────────────────────────────────────

def test_a_gated_validate_stage_model_change_needs_evidence():
    ctx, setter = _patch_ops(GATED_VALIDATE, "set_stage_config")
    with pytest.raises(HTTPException) as exc:
        _call(lambda: ops.patch_llm_config("t9_invented_check", ops.LlmConfigPatch(model_id="claude-x"),
                                           x_admin_secret="x", x_admin_user_id="a"), ctx)
    assert exc.value.status_code == 422 and "gated stage t9_invented_check" in exc.value.detail
    setter.assert_not_awaited()
    ctx, setter = _patch_ops(GATED_VALIDATE, "set_stage_config")
    _call(lambda: ops.patch_llm_config("t9_invented_check", ops.LlmConfigPatch(
        model_id="claude-x", break_glass_reason=BREAK_GLASS), x_admin_secret="x", x_admin_user_id="a"), ctx)
    assert setter.await_args.kwargs["evidence"] == {"break_glass_reason": BREAK_GLASS}


def test_a_plain_validate_stage_stays_free():
    ctx, setter = _patch_ops(PLAIN_VALIDATE, "set_stage_config")
    _call(lambda: ops.patch_llm_config("invented_check", ops.LlmConfigPatch(model_id="claude-x"),
                                       x_admin_secret="x", x_admin_user_id="a"), ctx)
    assert setter.await_args.kwargs["evidence"] is None


def test_a_gated_stage_edit_is_refused_while_a_wave_is_open(monkeypatch):
    _waves(monkeypatch, WAVE)
    ctx, setter = _patch_ops(JUDGE_ROW, "set_stage_config")
    with pytest.raises(HTTPException) as exc:
        _call(lambda: ops.patch_llm_config("invented_judge", ops.LlmConfigPatch(model_id="nova-x",
                                                                                 eval_run_id=RUN_ID),
                                           x_admin_secret="x", x_admin_user_id="a"), ctx)
    assert exc.value.status_code == 409 and "Invented wave" in exc.value.detail
    setter.assert_not_awaited()
    # the shadow-only change needs no evidence but is still frozen
    ctx, setter = _patch_ops(JUDGE_ROW, "set_stage_route")
    with pytest.raises(HTTPException) as exc:
        _call(lambda: ops.patch_llm_route("invented_judge", ops.LlmRoutePatch(shadow_model_id="nova-x",
                                                                              shadow_sample_pct=5),
                                          x_admin_secret="x", x_admin_user_id="a"), ctx)
    assert exc.value.status_code == 409
    setter.assert_not_awaited()
    # a plain stage is not frozen
    ctx, setter = _patch_ops(PLAIN_VALIDATE, "set_stage_config")
    _call(lambda: ops.patch_llm_config("invented_check", ops.LlmConfigPatch(model_id="claude-x"),
                                       x_admin_secret="x", x_admin_user_id="a"), ctx)
    setter.assert_awaited()


def test_an_unreadable_wave_table_refuses_with_503(monkeypatch):
    async def broken(pool=None):
        raise OSError("db down")
    monkeypatch.setattr(edit_gate, "read_open_waves", broken)
    ctx, setter = _patch_ops(JUDGE_ROW, "set_stage_config")
    with pytest.raises(HTTPException) as exc:
        _call(lambda: ops.patch_llm_config("invented_judge", ops.LlmConfigPatch(model_id="nova-x",
                                                                                 eval_run_id=RUN_ID),
                                           x_admin_secret="x", x_admin_user_id="a"), ctx)
    assert exc.value.status_code == 503
    setter.assert_not_awaited()


def test_the_stage_kill_switch_is_never_gated(monkeypatch):
    _waves(monkeypatch, WAVE)
    setter = AsyncMock(return_value={"stage": "t2_x", "is_active": False, "updated_at": None})
    with patch.object(ops, "verify_admin_secret"), patch.object(ops, "set_stage_enabled", setter):
        asyncio.run(ops.patch_llm_stage_switch("t2_x", ops.LlmStageSwitch(enabled=False, reason="incident now"),
                                               x_admin_secret="x", x_admin_user_id="a"))
    setter.assert_awaited()


# ── catalog ─────────────────────────────────────────────────────────────────────────────────────────────────────────

def _catalog_ctx(rows, setter):
    return [patch.object(ops, "verify_admin_secret"),
            patch.object(ops, "list_stage_configs", AsyncMock(return_value=rows)),
            patch.object(ops, "set_catalog_price", setter)]


def test_gated_stages_of_a_model_are_its_primary_and_fallback_routes_only():
    rows = [{"stage": "t2_a", "role": "writer", "model_id": "m1", "fallback_model_ids": ["m2"]},
            {"stage": "invented_check", "role": "validate", "model_id": "m1", "fallback_model_ids": []},
            {"stage": "s1_b", "role": "validate", "model_id": "m3", "fallback_model_ids": [], "shadow_model_id": "m1"}]
    assert ops.gated_stages_of("m1", rows) == ["t2_a"]
    assert ops.gated_stages_of("m2", rows) == ["t2_a"]
    assert ops.gated_stages_of("m9", rows) == []


def test_a_catalog_switch_of_a_model_on_a_gated_route_needs_evidence_and_is_audited():
    rows = [{"stage": "t2_a", "role": "writer", "model_id": "m1", "fallback_model_ids": []}]
    setter = AsyncMock(return_value={"model_key": "m1", "enabled": False})
    with pytest.raises(HTTPException) as exc:
        _call(lambda: ops.patch_llm_catalog("m1", ops.LlmCatalogPatch(enabled=False), x_admin_secret="x",
                                            x_admin_user_id="a"), _catalog_ctx(rows, setter))
    assert exc.value.status_code == 422 and "catalog switch of m1" in exc.value.detail
    setter.assert_not_awaited()
    _call(lambda: ops.patch_llm_catalog("m1", ops.LlmCatalogPatch(enabled=False, break_glass_reason=BREAK_GLASS),
                                        x_admin_secret="x", x_admin_user_id="a"), _catalog_ctx(rows, setter))
    assert setter.await_args.kwargs["evidence"] == {"break_glass_reason": BREAK_GLASS}
    assert setter.await_args.kwargs["fields"] == {"enabled": False}       # the evidence is not a catalog field


def test_a_catalog_switch_off_any_gated_route_passes_and_a_wave_freezes_a_gated_one(monkeypatch):
    rows = [{"stage": "t2_a", "role": "writer", "model_id": "m1", "fallback_model_ids": []}]
    setter = AsyncMock(return_value={"model_key": "m9", "enabled": True})
    _call(lambda: ops.patch_llm_catalog("m9", ops.LlmCatalogPatch(enabled=True), x_admin_secret="x",
                                        x_admin_user_id="a"), _catalog_ctx(rows, setter))
    assert setter.await_args.kwargs["evidence"] is None
    _waves(monkeypatch, WAVE)
    setter = AsyncMock()
    with pytest.raises(HTTPException) as exc:
        _call(lambda: ops.patch_llm_catalog("m1", ops.LlmCatalogPatch(enabled=True, eval_run_id=RUN_ID),
                                            x_admin_secret="x", x_admin_user_id="a"), _catalog_ctx(rows, setter))
    assert exc.value.status_code == 409
    setter.assert_not_awaited()


def test_the_catalog_read_names_the_gated_stages_of_each_model():
    rows = [{"stage": "t9_w", "role": "writer", "model_id": "m1", "fallback_model_ids": []}]
    with patch.object(ops, "list_catalog_rows", AsyncMock(return_value=[{"model_key": "m1"}, {"model_key": "m2"}])), \
            patch.object(ops, "list_stage_configs", AsyncMock(return_value=rows)):
        out = asyncio.run(ops.get_llm_catalog())
    assert [m["gated_stages"] for m in out["models"]] == [["t9_w"], []]
    with patch.object(ops, "list_catalog_rows", AsyncMock(return_value=[{"model_key": "m1"}])), \
            patch.object(ops, "list_stage_configs", AsyncMock(side_effect=OSError("down"))):
        out = asyncio.run(ops.get_llm_catalog())
    assert out["models"][0]["gated_stages"] is None


def test_the_catalog_store_writes_the_evidence_into_the_audit_row_and_the_bell():
    from shared.llm_client import catalog as cat

    class _Txn:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *a):
            return False
    before = {"price_in_per_mtok": 1, "price_out_per_mtok": 2, "price_source": "x", "enabled": True}
    row = {k: None for k in ("model_key", "label", "vendor", "provider", "api_style", "bedrock_profile_ids",
                             "wire_model", "callable_via", "supports_temperature", "max_output_tokens",
                             "price_in_per_mtok", "price_out_per_mtok", "price_cache_read_per_mtok",
                             "price_cache_write_per_mtok", "price_source", "enabled", "blocked_reason", "notes",
                             "updated_at", "updated_by")}
    conn = MagicMock(transaction=MagicMock(return_value=_Txn()), execute=AsyncMock(), close=AsyncMock(),
                     fetchrow=AsyncMock(side_effect=[before, {**row, "model_key": "m1", "enabled": False}]))
    with patch.object(cat.asyncpg, "connect", AsyncMock(return_value=conn)), \
            patch.object(cat, "get_database_url", return_value="postgresql://x"), \
            patch.object(cat, "_row_to_dict", lambda r: dict(r)):
        asyncio.run(cat.set_catalog_price("m1", fields={"enabled": False}, updated_by="admin:a", audit_actor="admin:a",
                                          evidence={"break_glass_reason": BREAK_GLASS}))
    sqls = [c.args[0] for c in conn.execute.await_args_list]
    assert any("acp_shared.audit_log" in s for s in sqls) and any("shared.notifications" in s for s in sqls)
    audit = next(c.args for c in conn.execute.await_args_list if "acp_shared.audit_log" in c.args[0])
    assert '"evidence": {"break_glass_reason"' in audit[5]
    bell = next(c.args for c in conn.execute.await_args_list if "shared.notifications" in c.args[0])
    assert "Catalog row m1 changed without an eval run" in bell[4]


# ── route policy (admin_llm_route) ──────────────────────────────────────────────────────────────────────────────────

def _request():
    return MagicMock(app=MagicMock(state=MagicMock(pool=object())))


def _policy_ctx(hold_before, exists=True):
    writer = AsyncMock(return_value={"stage": "x"})
    clearer = AsyncMock(return_value={"stage": "x", "cleared": True})
    return writer, clearer, [patch.object(trace_svc, "read_hold", AsyncMock(return_value=(exists, hold_before))),
                             patch.object(trace_svc, "write_policy", writer),
                             patch.object(trace_svc, "clear_policy", clearer),
                             patch.object(route_evidence, "read_eval_run", AsyncMock(return_value=None))]


def test_a_threshold_only_policy_edit_of_a_gated_stage_passes_without_evidence():
    writer, _, ctx = _policy_ctx(hold_before=None)
    _call(lambda: route_api.put_route_policy(route_api.RoutePolicyBody(fallback_alert_pct=30), _request(),
                                             stage="t2_x", x_admin_user_id="a"), ctx)
    assert writer.await_args.kwargs["evidence"] is None
    assert "eval_run_id" not in writer.await_args.args[2]        # the evidence is not a policy field


def test_a_hold_switch_change_of_a_gated_stage_or_the_defaults_needs_evidence():
    for stage in ("t2_x", "*"):
        writer, _, ctx = _policy_ctx(hold_before=None)
        with pytest.raises(HTTPException) as exc:
            _call(lambda s=stage: route_api.put_route_policy(route_api.RoutePolicyBody(hold_fallback_writes=True),
                                                             _request(), stage=s, x_admin_user_id="a"), ctx)
        assert exc.value.status_code == 422 and "hold switch" in exc.value.detail
        writer.assert_not_awaited()
    writer, _, ctx = _policy_ctx(hold_before=None)
    _call(lambda: route_api.put_route_policy(route_api.RoutePolicyBody(hold_fallback_writes=True,
                                                                       break_glass_reason=BREAK_GLASS),
                                             _request(), stage="t2_x", x_admin_user_id="a"), ctx)
    assert writer.await_args.kwargs["evidence"] == {"break_glass_reason": BREAK_GLASS}


def test_a_hold_switch_change_of_a_plain_stage_passes():
    writer, _, ctx = _policy_ctx(hold_before=None)
    _call(lambda: route_api.put_route_policy(route_api.RoutePolicyBody(hold_fallback_writes=True), _request(),
                                             stage="a3_invented", x_admin_user_id="a"), ctx)
    assert writer.await_args.kwargs["evidence"] is None


def test_a_policy_reset_that_clears_a_hold_needs_evidence_and_a_wave_freezes_it(monkeypatch):
    _, clearer, ctx = _policy_ctx(hold_before=True)
    with pytest.raises(HTTPException) as exc:
        _call(lambda: route_api.delete_route_policy(_request(), stage="t9_x", x_admin_user_id="a", eval_run_id=None,
                                                    break_glass_reason=None), ctx)
    assert exc.value.status_code == 422
    clearer.assert_not_awaited()
    _, clearer, ctx = _policy_ctx(hold_before=True)
    _call(lambda: route_api.delete_route_policy(_request(), stage="t9_x", x_admin_user_id="a",
                                                eval_run_id=None, break_glass_reason=BREAK_GLASS), ctx)
    assert clearer.await_args.kwargs["evidence"] == {"break_glass_reason": BREAK_GLASS}
    _, clearer, ctx = _policy_ctx(hold_before=None)        # a row without a hold: thresholds only, free
    _call(lambda: route_api.delete_route_policy(_request(), stage="t9_x", x_admin_user_id="a", eval_run_id=None,
                                                    break_glass_reason=None), ctx)
    assert clearer.await_args.kwargs["evidence"] is None
    _waves(monkeypatch, WAVE)
    _, clearer, ctx = _policy_ctx(hold_before=True)
    with pytest.raises(HTTPException) as exc:
        _call(lambda: route_api.delete_route_policy(_request(), stage="t9_x", x_admin_user_id="a",
                                                    eval_run_id=None, break_glass_reason=BREAK_GLASS), ctx)
    assert exc.value.status_code == 409
    clearer.assert_not_awaited()


def test_the_policy_store_audits_the_evidence_and_rings_the_bell():
    conn = MagicMock(execute=AsyncMock())
    audit = AsyncMock()
    with patch("services.acp_shared.audit_log.write_audit_log", audit):
        asyncio.run(trace_svc._policy_bell(conn, "t2_x", "admin:a", {"break_glass_reason": BREAK_GLASS}, {}))
        asyncio.run(trace_svc._policy_bell(conn, "t2_x", "admin:a", None, {}))
    bells = [c.args for c in conn.execute.await_args_list if "shared.notifications" in c.args[0]]
    assert len(bells) == 1 and "Route policy of stage t2_x changed without an eval run" in bells[0][4]


# ── route control (save / preset / revert) ──────────────────────────────────────────────────────────────────────────

def test_route_control_refuses_a_gated_change_while_a_wave_is_open(monkeypatch):
    preview = {"unchanged": False, "valid": True, "errors": [], "warnings": [], "diff": [], "stages": [],
               "needs_evidence": True, "evidence_stages": ["t2_x"]}
    monkeypatch.setattr(rc, "_proposal", AsyncMock(return_value=(MagicMock(), {}, [])))
    monkeypatch.setattr(rc, "preview_world", AsyncMock(return_value=dict(preview)))
    _waves(monkeypatch, WAVE)
    with pytest.raises(rc.RouteControlFrozen) as exc:
        asyncio.run(rc.apply(object(), actor="admin:a", action="save", expected_version=3,
                             break_glass_reason=BREAK_GLASS, order_mode="aws_first"))
    assert "Invented wave" in str(exc.value) and isinstance(exc.value, rc.RouteControlConflict)   # the router: 409
    out = asyncio.run(rc.preview(object(), order_mode="aws_first"))
    assert out["frozen_by_waves"] == WAVE
    monkeypatch.setattr(rc, "preview_world", AsyncMock(return_value={**preview, "evidence_stages": [],
                                                                      "needs_evidence": False}))
    assert asyncio.run(rc.preview(object(), order_mode="aws_first"))["frozen_by_waves"] == []
