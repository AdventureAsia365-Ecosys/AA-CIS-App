"""AA-1024 A4 / A6 / A2 and AA-1026 D7 — route governance (root ADR 0009 decisions 5 and 12, tech lead T-19, S263).

- A4: the writer / judge rule reads the model FAMILY (catalog ``family``, migration 320), not the vendor; the admin
  gives English reasons; the eval runners (R2, the S1-old regression) refuse a same-family judge before the run and
  at call time (``family_rule.trace_models`` records the models that really wrote an item).
- D7: a change of a writer / judge stage's models (primary or fallback chain) needs an eval run id or a break-glass
  reason (422 otherwise); the evidence lands in the audit row, a break glass raises an admin bell.
- A6: a saved route that breaks the failure-domain rule answers 200 with ``warnings[]`` (report only).
- A2: a request body never picks its model (``model_tier`` with a value = 422) and the dedup keys drop it.
No database, no AWS, no vendor: catalog rows, route rows and connections are fakes. Invented stages / reasons only.
"""
import asyncio
import inspect
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from api.core.model_tier import MODEL_TIER_REFUSED, refuse_model_tier
from api.routers import admin_llm_ops as ops
from shared.llm_client import family_rule, route_evidence
from shared.llm_client import role_config as rc_mod
from shared.llm_client.catalog import CatalogModel
from shared.llm_client.role_config import StageConfig
from tests.unit.test_aa1002_s3_deployment_routing import World


def _m(key, vendor="anthropic", family=None, **kw):
    base = dict(model_key=key, label=key, vendor=vendor, family=family, provider="bedrock", api_style="converse",
                bedrock_profile_ids={"acc3": f"global.{vendor}.{key}"}, callable_via=("llm_client",),
                price_in_per_mtok=1.0, price_out_per_mtok=2.0, enabled=True)
    base.update(kw)
    return CatalogModel(**base)


CLAUDE = _m("claude-x")
GPT = _m("gpt-x", vendor="openai")
NOVA = _m("nova-x", vendor="amazon", family="amazon")
# a Claude model resold under another vendor name: the family decides, not the vendor string
CLAUDE_RESOLD = _m("claude-resold", vendor="market", family="anthropic")
MODELS = [CLAUDE, GPT, NOVA, CLAUDE_RESOLD]
VIETNAMESE = re.compile(r"[àáảãạăằắẳẵặâầấẩẫậèéẻẽẹêềếểễệìíỉĩịòóỏõọôồốổỗộơờớởỡợùúủũụưừứửữựỳýỷỹỵđ]", re.I)
BREAK_GLASS = "every AWS path is refused; keep the stage running"
RUN_ID = "7f1c2a3b-0000-4000-8000-000000000001"


# ╔══ A4 — the family rule ═════════════════════════════════════════════════════════════════════════════════════════╗

def test_the_admin_rule_reads_the_family_not_the_vendor():
    judge = {o["model_id"]: o for o in ops._catalog_options("judge", "invented_judge", MODELS)}
    writer = {o["model_id"]: o for o in ops._catalog_options("writer", "invented_writer", MODELS)}
    assert not judge["claude-resold"]["available"] and "family" in judge["claude-resold"]["reason"]
    assert judge["gpt-x"]["available"] and judge["nova-x"]["available"]
    assert writer["claude-resold"]["available"] and writer["claude-x"]["available"]
    assert not writer["nova-x"]["available"] and "family" in writer["nova-x"]["reason"]


def test_every_admin_refusal_reason_is_english():
    off = _m("off-x", enabled=False)
    no_gateway = _m("pinned-x", callable_via=("pinned",))
    reasons = [o.get("reason") for role in ("judge", "writer", "validate")
               for o in ops._catalog_options(role, "invented", [*MODELS, off, no_gateway]) if o.get("reason")]
    assert reasons and not any(VIETNAMESE.search(r) for r in reasons)
    assert ops._REASON_NOT_ENABLED in reasons and ops._REASON_NOT_CALLABLE in reasons


def test_a_judge_primary_of_the_writer_family_is_refused_with_an_english_422():
    row = {"stage": "invented_judge", "role": "judge", "model_id": "gpt-x", "fallback_model_ids": [],
           "shadow_model_id": None, "shadow_sample_pct": 0, "allowed_scopes": None}
    with patch.object(ops, "verify_admin_secret"), \
            patch.object(ops, "list_stage_configs", AsyncMock(return_value=[row])), \
            patch.object(ops, "_load_catalog", AsyncMock(return_value=MODELS)), \
            patch.object(ops, "_load_reach", AsyncMock(return_value={})), \
            patch.object(ops, "set_stage_config", AsyncMock()) as setter:
        with pytest.raises(HTTPException) as exc:
            asyncio.run(ops.patch_llm_config("invented_judge", ops.LlmConfigPatch(
                model_id="claude-resold", break_glass_reason=BREAK_GLASS), x_admin_secret="x", x_admin_user_id="a"))
    assert exc.value.status_code == 422 and "not selectable" in exc.value.detail
    setter.assert_not_awaited()


def test_labels_resolve_to_catalog_keys_and_families():
    assert family_rule.model_key_of("satellite-claude-x") == "claude-x"
    assert family_rule.model_key_of("anthropic-api-claude-x") == "claude-x"
    assert family_rule.model_key_of("azure-openai-gpt-x") == "gpt-x"
    assert family_rule.family_of("anthropic-api-claude-resold", MODELS) == "anthropic"
    assert family_rule.family_of("nova-x", MODELS) == "amazon"
    assert family_rule.family_of("unknown-model", MODELS) is None


def test_shared_families_and_check_pair():
    assert family_rule.shared_families(["satellite-claude-x"], ["gpt-x"], MODELS) == []
    assert family_rule.shared_families(["claude-x", "gpt-x"], ["satellite-gpt-x"], MODELS) == ["openai"]
    assert family_rule.shared_families(["unknown-model"], ["unknown-model"], MODELS) == []    # unproven: allowed
    with pytest.raises(family_rule.SameFamilyJudge) as exc:
        family_rule.check_pair(["gpt-x"], ["satellite-gpt-x"], models=MODELS, what="the test judge")
    assert "openai" in str(exc.value) and "decision 12" in str(exc.value)


def test_route_conflicts_compare_the_writer_primary_with_every_judge_model():
    writer = {"stage": "invented_writer", "model_id": "claude-x", "fallback_model_ids": ["gpt-x"]}
    clean = {"stage": "invented_judge", "model_id": "gpt-x", "fallback_model_ids": ["nova-x"]}
    dirty = {"stage": "other_judge", "model_id": "gpt-x", "fallback_model_ids": ["claude-resold"]}
    assert family_rule.route_conflicts(writer, [clean], MODELS) == []     # the writer's GPT last resort: call time
    assert family_rule.route_conflicts(writer, [clean, dirty], MODELS) == ["other_judge: claude-resold (anthropic)"]
    assert family_rule.route_conflicts(None, [dirty], MODELS) == []


def test_stage_pair_conflicts_reads_routes_and_takes_an_explicit_writer_model():
    rows = [{"stage": "invented_judge", "model_id": "gpt-x", "fallback_model_ids": ["claude-resold"]}]
    with patch("shared.llm_client.role_config.list_stage_configs", AsyncMock(return_value=rows)):
        got = asyncio.run(family_rule.stage_pair_conflicts(None, ["invented_judge", "missing"], writer_model="claude-x",
                                                           models=MODELS))
    assert got == ["invented_judge: claude-resold (anthropic)"]


def test_the_trace_records_only_inside_the_block_and_across_threads():
    family_rule.note_model("writer", "claude-x")            # no trace bound: nothing, no error
    with family_rule.trace_models() as seen:
        family_rule.note_model("writer", "satellite-claude-x")
        asyncio.run(asyncio.to_thread(family_rule.note_model, "judge", "gpt-x"))
        family_rule.note_model("writer", None)
    assert seen == [("writer", "satellite-claude-x"), ("judge", "gpt-x")]
    assert family_rule.labels(seen + [("writer", "satellite-claude-x")], "writer") == ["satellite-claude-x"]


def test_the_gateway_records_the_model_that_served_a_reply(monkeypatch):
    from shared.llm_client import catalog, vendor_route
    hook = vendor_route.circuit_open
    world = World(monkeypatch)
    cfg = StageConfig(stage="adhoc_aa1024", role="writer", provider="claude", model_id="gpt-6-luna",
                      account_route="acc3")
    monkeypatch.setattr("shared.llm_client.client.get_stage_config_sync", lambda stage: cfg)
    try:
        with family_rule.trace_models() as seen:
            resp = world.generate(stage="adhoc_aa1024")
        assert seen == [("writer", resp.model_used)]
        assert family_rule.model_key_of(resp.model_used) == "gpt-6-luna"
    finally:
        world.reset()
        catalog.invalidate()
        vendor_route.circuit_open = hook


# ── the R2 runner ───────────────────────────────────────────────────────────────────────────────────────────────

def _r2_candidate(*, writer_model, judge_model):
    from services.writing_memory import r2

    async def writer_fn(writer, inp, *, view, shared_inputs):
        family_rule.note_model("writer", writer_model)         # what LLMClient.generate records
        family_rule.note_model("judge", "nova-x")              # the writer's own inner judge: not the R2 judge
        return {"content_text": "An invented day on the coast of Lumeria."}

    async def judge(it, output, arm):
        return SimpleNamespace(verdict="pass", score=9.0, failure_codes=[], model=judge_model, cost_usd=0.0,
                               judge_version="v", claims=())

    def checks(it, output):
        return {"qa_artefacts": [], "isolation": {"leak": False}, "injection": []}

    def row(it, **kw):
        return kw

    conf = r2.R2Config(writer="a1")
    return asyncio.run(r2._candidate({"item_id": "i1", "input": {}}, conf=conf, writer="a1", view=None,
                                     writer_fn=writer_fn, shared_inputs={}, judge=judge, checks=checks,
                                     control=None, counts=True, row=row, family_models=MODELS))


def test_r2_turns_a_same_family_verdict_into_an_error_row():
    got, cmp = _r2_candidate(writer_model="satellite-gpt-x", judge_model="gpt-x")
    assert got["verdict"] == "error" and "openai" in got["det"]["same_family"]
    assert cmp["hard_stop"] is None


def test_r2_keeps_a_verdict_of_another_family():
    got, _cmp = _r2_candidate(writer_model="anthropic-api-claude-x", judge_model="gpt-x")
    assert got["verdict"] == "pass" and "same_family" not in got["det"]


def test_r2_refuses_a_run_whose_judge_route_has_the_writer_family():
    from services.writing_memory import r2
    with patch("shared.llm_client.catalog.list_models", AsyncMock(return_value=MODELS)), \
            patch.object(family_rule, "stage_pair_conflicts",
                         AsyncMock(return_value=["writing_memory_eval: claude-resold (anthropic)"])) as check:
        with pytest.raises(r2.R2Refused) as exc:
            asyncio.run(r2.check_judge_family("a1"))
    assert "claude-resold" in str(exc.value)
    assert check.await_args.args == ("s1_generate", ["writing_memory_eval"])


def test_r2_refuses_a_run_it_cannot_check_and_hands_over_the_catalog_when_clean():
    from services.writing_memory import r2
    with patch("shared.llm_client.catalog.list_models", AsyncMock(side_effect=OSError("db down"))):
        with pytest.raises(r2.R2Refused) as exc:
            asyncio.run(r2.check_judge_family("t2"))
    assert "could not be checked" in str(exc.value)
    with patch("shared.llm_client.catalog.list_models", AsyncMock(return_value=MODELS)), \
            patch.object(family_rule, "stage_pair_conflicts", AsyncMock(return_value=[])):
        assert asyncio.run(r2.check_judge_family("t2")) == MODELS


def test_the_s1_regression_eval_refuses_a_judge_route_of_the_writer_family():
    from services.eval import regression
    rows = [{"stage": "s1_judge", "model_id": "gpt-x", "fallback_model_ids": []},
            {"stage": "s1_brand_audit", "model_id": "gpt-x", "fallback_model_ids": ["claude-resold"]}]
    models = [*MODELS, _m("haiku", api_style="anthropic_native")]
    with patch("shared.llm_client.role_config.list_stage_configs", AsyncMock(return_value=rows)):
        with pytest.raises(family_rule.SameFamilyJudge) as exc:
            asyncio.run(regression.check_judge_family(models))
    assert "s1_brand_audit: claude-resold" in str(exc.value)
    rows[1]["fallback_model_ids"] = ["nova-x"]
    with patch("shared.llm_client.role_config.list_stage_configs", AsyncMock(return_value=rows)):
        asyncio.run(regression.check_judge_family(models))


def test_a_same_family_tour_makes_the_s1_regression_run_invalid():
    from services.eval import regression
    src = inspect.getsource(regression.run_s1_old_eval)
    assert "trace_models" in src and "check_judge_family(models)" in src
    assert '"invalid": deg["invalid"] or bool(same_family_tours)' in src
    assert f'model_tier="{regression.S1_OLD_WRITER_MODEL}"' in src        # the checked model is the one written with
    current = {"avg_quality_score": 5.0, "details": {"invalid": True, "same_family_tours": ["Invented tour"]}}
    assert regression._detect_regression("s1_old", current, {"avg_quality_score": 9.0}) is False


# ╔══ D7 — a route change needs evidence ═══════════════════════════════════════════════════════════════════════════╗

def _run(**kw):
    row = {"pipeline": "writing_memory", "run_kind": "r2", "writer": "a1", "verdict": "pass",
           "regression_detected": False, "finished_at": "2026-11-01T00:00:00+00:00"}
    row.update(kw)
    return row


@pytest.mark.parametrize("row,problem", [
    (None, "does not exist"),
    (_run(finished_at=None), "has not finished"),
    (_run(verdict="fail"), "verdict fail"),
    (_run(verdict="invalid"), "verdict invalid"),
    (_run(regression_detected=True), "detected a regression"),
])
def test_an_eval_run_that_cannot_back_a_change(row, problem):
    assert problem in route_evidence.eval_run_problem(RUN_ID, row)


@pytest.mark.parametrize("row", [_run(), _run(verdict="informative"),
                                 _run(pipeline="s1_old", run_kind="regression", writer=None, verdict=None,
                                      finished_at=None)])
def test_an_eval_run_that_backs_a_change(row):
    assert route_evidence.eval_run_problem(RUN_ID, row) is None


def _require(role="judge", run_id=None, reason=None, row=None):
    return asyncio.run(route_evidence.require(role, eval_run_id=run_id, break_glass_reason=reason,
                                              read_run=AsyncMock(return_value=row)))


def test_require_refuses_a_change_without_evidence_and_a_short_reason():
    with pytest.raises(route_evidence.EvidenceRefused, match="needs evidence"):
        _require()
    with pytest.raises(route_evidence.EvidenceRefused, match="at least 20 characters"):
        _require(reason="outage")
    with pytest.raises(route_evidence.EvidenceRefused, match="is not a run id"):
        _require(run_id="run-7")
    with pytest.raises(route_evidence.EvidenceRefused, match="does not exist"):
        _require(run_id=RUN_ID, reason=BREAK_GLASS, row=None)        # a bad id is never waved through


def test_require_returns_the_audited_evidence():
    assert _require(run_id=RUN_ID, row=_run()) == {
        "eval_run_id": RUN_ID, "eval_run": {"pipeline": "writing_memory", "run_kind": "r2", "writer": "a1",
                                            "verdict": "pass"}}
    assert _require(reason=f"  {BREAK_GLASS}  ") == {"break_glass_reason": BREAK_GLASS}
    assert _require(role="validate") is None and _require(role="embed") is None


def test_a_break_glass_change_writes_the_admin_bell_and_a_plain_one_does_not():
    conn = MagicMock(execute=AsyncMock())
    assert asyncio.run(route_evidence.record_break_glass(conn, stage="invented_judge", actor="admin:a",
                                                         evidence={"eval_run_id": RUN_ID}, change={})) is False
    conn.execute.assert_not_awaited()
    assert asyncio.run(route_evidence.record_break_glass(conn, stage="invented_judge", actor="admin:a",
                                                         evidence={"break_glass_reason": BREAK_GLASS},
                                                         change={"fallback_model_ids": ["gpt-x"]})) is True
    sql, tenant, event, stage, payload = conn.execute.await_args.args
    assert "INSERT INTO shared.notifications" in sql and "ARRAY['admin']" in sql
    assert (event, stage) == (route_evidence.BREAK_GLASS_EVENT, "invented_judge") and BREAK_GLASS in payload


JUDGE_ROW = {"stage": "invented_judge", "role": "judge", "model_id": "gpt-x", "fallback_model_ids": ["nova-x"],
             "shadow_model_id": None, "shadow_sample_pct": 0, "allowed_scopes": None, "account_route": None}


def _patch_ops(row, setter_name, *, read_run=None):
    async def setter(stage, *a, **kw):
        return {"stage": stage, "model_id": a[0] if a else row["model_id"], "updated_at": None,
                "fallback_model_ids": kw.get("fallback_model_ids", row["fallback_model_ids"]),
                "shadow_model_id": kw.get("shadow_model_id"), "shadow_sample_pct": kw.get("shadow_sample_pct", 0)}
    mock = AsyncMock(side_effect=setter)
    ctx = [patch.object(ops, "verify_admin_secret"),
           patch.object(ops, "list_stage_configs", AsyncMock(return_value=[row])),
           patch.object(ops, "_load_catalog", AsyncMock(return_value=MODELS)),
           patch.object(ops, "_load_reach", AsyncMock(return_value={})),
           patch.object(ops, setter_name, mock),
           patch.object(route_evidence, "read_eval_run", read_run or AsyncMock(return_value=_run()))]
    return ctx, mock


def _call(fn, ctx):
    for c in ctx:
        c.start()
    try:
        return asyncio.run(fn())
    finally:
        for c in ctx:
            c.stop()


def test_a_judge_model_change_without_evidence_is_422():
    ctx, setter = _patch_ops(JUDGE_ROW, "set_stage_config")
    with pytest.raises(HTTPException) as exc:
        _call(lambda: ops.patch_llm_config("invented_judge", ops.LlmConfigPatch(model_id="nova-x"),
                                           x_admin_secret="x", x_admin_user_id="a"), ctx)
    assert exc.value.status_code == 422 and "eval_run_id" in exc.value.detail
    setter.assert_not_awaited()


def test_a_judge_model_change_with_an_eval_run_is_saved_with_it():
    ctx, setter = _patch_ops(JUDGE_ROW, "set_stage_config")
    out = _call(lambda: ops.patch_llm_config("invented_judge", ops.LlmConfigPatch(model_id="nova-x",
                                                                                   eval_run_id=RUN_ID),
                                             x_admin_secret="x", x_admin_user_id="a"), ctx)
    assert setter.await_args.kwargs["evidence"]["eval_run_id"] == RUN_ID
    assert out["warnings"] == []                              # no request pool: the A6 check is skipped


def test_an_account_route_only_save_and_a_validate_stage_need_no_evidence():
    ctx, setter = _patch_ops(JUDGE_ROW, "set_stage_config")
    _call(lambda: ops.patch_llm_config("invented_judge", ops.LlmConfigPatch(model_id="gpt-x"),
                                       x_admin_secret="x", x_admin_user_id="a"), ctx)
    assert setter.await_args.kwargs["evidence"] is None
    ctx, setter = _patch_ops({**JUDGE_ROW, "stage": "invented_check", "role": "validate"}, "set_stage_config")
    _call(lambda: ops.patch_llm_config("invented_check", ops.LlmConfigPatch(model_id="claude-x"),
                                       x_admin_secret="x", x_admin_user_id="a"), ctx)
    assert setter.await_args.kwargs["evidence"] is None


def test_a_fallback_change_needs_evidence_a_shadow_change_does_not():
    ctx, setter = _patch_ops(JUDGE_ROW, "set_stage_route")
    with pytest.raises(HTTPException) as exc:
        _call(lambda: ops.patch_llm_route("invented_judge", ops.LlmRoutePatch(fallback_model_ids=[]),
                                          x_admin_secret="x", x_admin_user_id="a"), ctx)
    assert exc.value.status_code == 422
    setter.assert_not_awaited()
    ctx, setter = _patch_ops(JUDGE_ROW, "set_stage_route")
    _call(lambda: ops.patch_llm_route("invented_judge", ops.LlmRoutePatch(shadow_model_id="nova-x",
                                                                          shadow_sample_pct=5),
                                      x_admin_secret="x", x_admin_user_id="a"), ctx)
    assert setter.await_args.kwargs["evidence"] is None
    ctx, setter = _patch_ops(JUDGE_ROW, "set_stage_route")
    _call(lambda: ops.patch_llm_route("invented_judge", ops.LlmRoutePatch(fallback_model_ids=[],
                                                                          break_glass_reason=BREAK_GLASS),
                                      x_admin_secret="x", x_admin_user_id="a"), ctx)
    assert setter.await_args.kwargs["evidence"] == {"break_glass_reason": BREAK_GLASS}


def test_an_unreadable_eval_run_table_refuses_with_503():
    ctx, setter = _patch_ops(JUDGE_ROW, "set_stage_config", read_run=AsyncMock(side_effect=OSError("db down")))
    with pytest.raises(HTTPException) as exc:
        _call(lambda: ops.patch_llm_config("invented_judge", ops.LlmConfigPatch(model_id="nova-x",
                                                                                 eval_run_id=RUN_ID),
                                           x_admin_secret="x", x_admin_user_id="a"), ctx)
    assert exc.value.status_code == 503
    setter.assert_not_awaited()


def test_the_evidence_body_is_bounded():
    with pytest.raises(ValidationError):
        ops.LlmRoutePatch(break_glass_reason="x" * 501)
    with pytest.raises(ValidationError):
        ops.LlmConfigPatch(model_id="gpt-x", eval_run_id="r" * 65)


class _Txn:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def test_the_store_writes_the_evidence_into_the_audit_row_and_the_bell_in_the_same_transaction():
    before = {"model_id": "gpt-x", "fallback_model_ids": ["nova-x"], "shadow_model_id": None, "shadow_sample_pct": 0}
    after = {"stage": "invented_judge", "role": "judge", "provider": "openai", "model_id": "gpt-x",
             "account_route": None, "is_active": True, "updated_at": None, "updated_by": "admin:a",
             "fallback_model_ids": [], "shadow_model_id": None, "shadow_sample_pct": 0}
    conn = MagicMock(transaction=MagicMock(return_value=_Txn()), execute=AsyncMock(), close=AsyncMock(),
                     fetchrow=AsyncMock(side_effect=[before, after]))
    with patch.object(rc_mod.asyncpg, "connect", AsyncMock(return_value=conn)), \
            patch.object(rc_mod, "get_database_url", return_value="postgresql://x"):
        asyncio.run(rc_mod.set_stage_route("invented_judge", fallback_model_ids=[], shadow_model_id=None,
                                           shadow_sample_pct=0, updated_by="admin:a", audit_actor="admin:a",
                                           evidence={"break_glass_reason": BREAK_GLASS}))
    sqls = [c.args[0] for c in conn.execute.await_args_list]
    assert any("acp_shared.audit_log" in s for s in sqls) and any("shared.notifications" in s for s in sqls)
    audit = next(c.args for c in conn.execute.await_args_list if "acp_shared.audit_log" in c.args[0])
    assert '"evidence": {"break_glass_reason"' in audit[5]


# ╔══ A6 — the failure-domain rule on a saved route ═══════════════════════════════════════════════════════════════╗

def _report(status, reason=None):
    return {"ready": True, "stages": [{"stage": "invented_judge", "status": status, "reason": reason},
                                      {"stage": "other", "status": "breach"}]}


def test_route_warnings_name_a_breach_only():
    warn = ops.route_warnings(_report("breach", "every usable deployment is in bedrock:acc3:aws_only"),
                              "invented_judge")
    assert len(warn) == 1 and "report only" in warn[0] and "bedrock:acc3:aws_only" in warn[0]
    assert ops.route_warnings(_report("ok"), "invented_judge") == []
    assert ops.route_warnings(_report("degrade"), "invented_judge") == []
    assert ops.route_warnings({"ready": False}, "invented_judge") == []
    assert ops.route_warnings(_report("breach"), "not_listed") == []


def test_the_saved_route_is_checked_with_the_digest_rule():
    from api.services import llm_availability
    from shared.llm_client import invariant
    conn = object()
    acquire = MagicMock()
    acquire.__aenter__ = AsyncMock(return_value=conn)
    acquire.__aexit__ = AsyncMock(return_value=False)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        pool=SimpleNamespace(acquire=MagicMock(return_value=acquire)))))
    conns = [{"connection_key": "acc3", "circuit": {"refusing": True, "error_class": "credential"}}]
    with patch.object(llm_availability, "read_connections", AsyncMock(return_value=(True, conns))), \
            patch.object(invariant, "read", AsyncMock(return_value=_report("breach"))) as read:
        warn = asyncio.run(ops._route_warnings(request, "invented_judge"))
    assert read.await_args.kwargs["blocked"] == {"acc3": "credential"} and len(warn) == 1
    with patch.object(llm_availability, "read_connections", AsyncMock(side_effect=OSError("db down"))):
        assert asyncio.run(ops._route_warnings(request, "invented_judge")) == []       # never fails the change
    assert asyncio.run(ops._route_warnings(None, "invented_judge")) == []


def test_the_route_patch_answers_200_with_the_warnings():
    ctx, _setter = _patch_ops(JUDGE_ROW, "set_stage_route")
    ctx.append(patch.object(ops, "_route_warnings", AsyncMock(return_value=["Failure-domain rule (report only)"])))
    out = _call(lambda: ops.patch_llm_route("invented_judge", ops.LlmRoutePatch(shadow_model_id=None),
                                            x_admin_secret="x", x_admin_user_id="a"), ctx)
    assert out["warnings"] == ["Failure-domain rule (report only)"]


# ╔══ A2 — a request never picks its model ════════════════════════════════════════════════════════════════════════╗

def _bodies():
    from api.routers import admin_pipeline as ap
    from api.routers import v1_pipeline as v1
    run = {"tour_id": "t1", "batch_id": "b1", "tenant_id": "x"}
    return [(ap.TourRunRequest, run), (v1.TourRunRequest, run),
            (ap.IngestS3Request, {"s3_key": "k", "bucket": "b"}),
            (ap.S1BatchSubmitRequest, {"tour_ids": ["t1"], "batch_id": "b1"})]


@pytest.mark.parametrize("i", range(4))
def test_a_body_with_a_model_tier_is_refused_and_an_explicit_null_is_ignored(i):
    model, body = _bodies()[i]
    assert "model_tier" not in model.model_fields
    with pytest.raises(ValidationError, match="model_tier is not accepted"):
        model(**body, model_tier="haiku")
    assert "model_tier" not in model(**body, model_tier=None).model_dump()


def test_the_api_answers_422_for_a_model_tier():
    from api.routers.admin_pipeline import TourRunRequest
    app = FastAPI()

    @app.post("/probe")
    async def probe(req: TourRunRequest):
        return {"ok": True}
    client = TestClient(app)
    bad = client.post("/probe", json={"tour_id": "t1", "batch_id": "b1", "tenant_id": "x", "model_tier": "sonnet"})
    assert bad.status_code == 422 and MODEL_TIER_REFUSED[:40] in bad.text
    assert client.post("/probe", json={"tour_id": "t1", "batch_id": "b1", "tenant_id": "x"}).status_code == 200


def test_refuse_model_tier_passes_other_input_through():
    assert refuse_model_tier({"a": 1, "model_tier": ""}) == {"a": 1}
    assert refuse_model_tier("not a dict") == "not a dict"


def test_the_batch_ingest_and_the_job_no_longer_carry_a_model_tier():
    from services.content_generation import s1_batch
    from services.jobs import s1_batch_ingest_job
    assert "model_tier" not in inspect.signature(s1_batch.ingest_s1_batch).parameters
    assert "model_tier=" not in inspect.getsource(s1_batch_ingest_job)
