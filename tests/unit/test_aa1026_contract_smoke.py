"""AA-1026 I6 — the acc2 fallback contract smoke (services/jobs/llm_contract_smoke_job.py): each judge / validate stage
sends its own prompt through a forced acc2 model and its own parser judges the reply. No provider, AWS or database is
reached: the gateway's ``probe`` is a fake that answers canned replies (invented text only)."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.jobs import llm_contract_smoke_job as smoke
from shared.jobs.registry import NonRetryable, get_kind
from shared.llm_client.catalog import CatalogModel
from shared.llm_client.connections import BUILTIN_CONNECTIONS, ProviderConnection
from shared.llm_client.deployments import ModelDeployment
from shared.llm_client.models import LLMResponse
from shared.llm_client.role_config import StageConfig

NOVA = CatalogModel(model_key="nova-lite", label="Amazon Nova Lite", vendor="amazon", provider="bedrock",
                    api_style="converse", bedrock_profile_ids={"acc2": "us.amazon.nova-lite-v1:0"},
                    callable_via=("llm_client",), price_in_per_mtok=0.077, price_out_per_mtok=0.308, enabled=True,
                    family="amazon")
SCOUT = CatalogModel(model_key="llama4-scout", label="Meta Llama 4 Scout", vendor="meta", provider="bedrock",
                     api_style="converse", bedrock_profile_ids={"acc2": "us.meta.llama4-scout-17b-instruct-v1:0"},
                     callable_via=("llm_client",), price_in_per_mtok=0.17, price_out_per_mtok=0.66, enabled=True,
                     family="meta")
HAIKU = CatalogModel(model_key="haiku-5-5", label="Claude Haiku 5.5", vendor="anthropic", provider="bedrock",
                     api_style="converse", bedrock_profile_ids={"acc3": "global.anthropic.claude-haiku-5-5"},
                     callable_via=("llm_client",), price_in_per_mtok=0.1, price_out_per_mtok=0.5, enabled=True)
MODELS = {m.model_key: m for m in (NOVA, SCOUT, HAIKU)}
ACC2 = ProviderConnection(connection_key="acc2", kind="bedrock", auth_mode="task_role", label="acc2",
                          data_scope="aws_only", enabled=True, region="us-west-1")
DEPS = {"nova-lite": [ModelDeployment("nova-lite", "acc2", "us.amazon.nova-lite-v1:0", 20, True, "bedrock_profile")],
        "llama4-scout": [ModelDeployment("llama4-scout", "acc2", "us.meta.llama4-scout-17b-instruct-v1:0", 20, True,
                                         "bedrock_profile")],
        "haiku-5-5": [ModelDeployment("haiku-5-5", "acc3", "global.anthropic.claude-haiku-5-5", 10, True,
                                      "bedrock_profile")]}

# One reply each stage's parser accepts (invented content).
GOOD = {
    "brand_audit": {"brand_audit": {"publish_ready": True, "fields_to_fix": [], "failure_codes": [], "issues": [],
                                    "scores": {"brand_fit": 1, "human_read": 1, "seo_fit": 1, "trip_type_accuracy": 1,
                                               "publish_readiness": 1}, "notes": "", "lessons_extracted": []}},
    "s1_source_facts": {"days": [{"day": 1, "distances": ["40 km"]}, {"day": 2, "distances": ["12 km"]}]},
    "a3_atomize": {"atoms": [{"place": "Orvalen ridge", "action": "walk"}]},
    "a3_place_card": {"display_name": "Varro hut", "locality": "", "region": "", "country": "Orvalia", "facts": []},
    "a3_place_card_check": {"identity_ok": True, "verdicts": ["supported", "unsure"], "locality_ok": True,
                            "region_ok": True},
    "a3_place_days": {"days": [{"day": 1, "start": "Kestrel Bay", "end": "Tamsin Lodge", "overnight": "Tamsin Lodge",
                                "places": []},
                               {"day": 2, "start": "Tamsin Lodge", "end": "Varro hut", "overnight": "Varro hut",
                                "places": []}]},
    "a3_place_locate": {"Tamsin Lodge": None, "Varro hut": None},
    "a3_place_dedupe_check": {"verdict": "unsure", "confidence": 0.4, "reason": "names differ"},
    "t10_claims": {"rows": [{"i": "1", "t": "trip_fact", "ref": "S1", "q": "walk 12 km", "p": ["Varro hut"]},
                            {"i": "2", "t": "number", "ref": "F1", "q": "sits at 2,100 m", "p": ["Varro hut"]}]},
    "t9_faq_answer_fit": {"items": [{"n": 1, "verdict": "ANSWERS", "reason": ""},
                                    {"n": 2, "verdict": "ANSWERS", "reason": ""}]},
    "t9_faq_filter": {"items": [{"n": 1, "label": "KEEP", "reason_code": "on_subject", "confidence": "high"},
                                {"n": 2, "label": "KEEP", "reason_code": "on_subject", "confidence": "high"},
                                {"n": 3, "label": "DROP", "reason_code": "word_meaning", "confidence": "high"}]},
}
GOOD["s1_brand_audit"] = GOOD["t2_brand_audit"] = GOOD.pop("brand_audit")


def _reply(content: str) -> LLMResponse:
    return LLMResponse(content=content, model_used="nova-lite", provider="bedrock-native", input_tokens=100,
                       output_tokens=20, cost_usd=0.00002)


class FakeClient:
    def __init__(self, replies: dict):
        self.replies = replies
        self.requests: list = []

    def probe(self, request, model, target):
        self.requests.append((request, model, target))
        return _reply(self.replies[self.stage])


def _target(model):
    from shared.llm_client.vendor_route import VendorDeployment
    return VendorDeployment(DEPS[model.model_key][0], ACC2)


def _run(stage: str, content) -> tuple[dict, FakeClient]:
    client = FakeClient({stage: content if isinstance(content, str) else json.dumps(content)})
    client.stage = stage
    model = MODELS[smoke.PLANNED[stage]]
    return smoke.run_case(smoke.CASES[stage], model, _target(model), client=client), client


def test_every_planned_stage_has_a_case_and_no_writer_or_anthropic_pick():
    assert set(smoke.CASES) == set(smoke.PLANNED)
    assert {c.role for c in smoke.CASES.values()} == {"judge", "validate"}
    assert set(smoke.PLANNED.values()) == {"nova-lite", "llama4-scout"}
    # the place card's draft and check are smoked on two different makers (S234 rule, migration 331)
    assert smoke.PLANNED["a3_place_card"] != smoke.PLANNED["a3_place_card_check"]


@pytest.mark.parametrize("stage", sorted(smoke.PLANNED))
def test_a_reply_the_stage_parser_accepts_passes(stage):
    result, client = _run(stage, GOOD[stage])
    assert result["contract_ok"] is True, result
    assert result["called"] and result["cost_usd"] == pytest.approx(0.00002)
    request, model, target = client.requests[0]
    assert request.stage == smoke.LEDGER_STAGE and request.role == smoke.CASES[stage].role
    assert target.connection.connection_key == "acc2" and model.model_key == smoke.PLANNED[stage]


@pytest.mark.parametrize("stage", sorted(smoke.PLANNED))
def test_prose_instead_of_json_fails_the_contract(stage):
    result, _ = _run(stage, "I am sorry, I cannot help with that.")
    assert result["contract_ok"] is False and result["error_class"] == "contract", result
    assert result["cost_usd"] == pytest.approx(0.00002)              # billed all the same


def test_the_requests_are_the_stages_own_prompts():
    from services.acp_content_writing.claim_rules import EXTRACTION_SYSTEM
    from services.acp_shared.atom_extraction import SYSTEM_PROMPT as ATOMIZE_SYSTEM
    from services.content_generation.brand_audit_node import BRAND_AUDIT_SCHEMA
    from services.content_generation.source_facts import _SCHEMA
    from services.places import days, dedupe, locate

    seen = {}
    for stage in smoke.PLANNED:
        _result, client = _run(stage, GOOD[stage])
        seen[stage] = client.requests[0][0]
    assert seen["s1_brand_audit"].json_schema["schema"] == BRAND_AUDIT_SCHEMA
    assert seen["s1_brand_audit"].temperature == 0.1 and "temperature" in seen["s1_brand_audit"].model_fields_set
    assert seen["s1_source_facts"].json_schema == _SCHEMA and seen["s1_source_facts"].max_tokens == 4096
    assert seen["a3_atomize"].system_prompt == ATOMIZE_SYSTEM and seen["a3_atomize"].json_schema is None
    assert seen["a3_place_days"].system_prompt == days.SYSTEM_PROMPT
    assert seen["a3_place_locate"].system_prompt == locate.SYSTEM_PROMPT
    assert seen["a3_place_dedupe_check"].json_schema == dedupe.VERDICT_SCHEMA
    assert seen["t10_claims"].system_prompt == EXTRACTION_SYSTEM and seen["t10_claims"].json_schema
    assert "Varro hut" in seen["a3_place_card"].user_prompt                 # draft_card's own user prompt
    assert "FACTS:\n1. The Varro hut stands" in seen["a3_place_card_check"].user_prompt


def test_a_check_reply_with_the_wrong_number_of_verdicts_fails():
    result, _ = _run("a3_place_card_check", {"identity_ok": True, "verdicts": ["supported"]})
    assert result["contract_ok"] is False and "one verdict per fact" in result["error"]


def test_a_locate_answer_missing_a_name_fails():
    result, _ = _run("a3_place_locate", {"Varro hut": None})
    assert result["contract_ok"] is False and "Tamsin Lodge" in result["error"]


def test_a_refused_call_is_the_stage_answer_not_a_crash():
    class Refused(FakeClient):
        def probe(self, request, model, target):
            raise RuntimeError("AccessDeniedException: not allowed")
    client = Refused({})
    model = NOVA
    result = smoke.run_case(smoke.CASES["a3_atomize"], model, _target(model), client=client)
    assert result["contract_ok"] is False and result["called"] is False and result["cost_usd"] == 0
    assert result["error_class"] == "access_denied"


def test_an_unusable_reply_counts_its_billed_cost():
    from shared.llm_client.client import _billed_unusable

    class Unusable(FakeClient):
        def probe(self, request, model, target):
            raise _billed_unusable("schema mismatch", [{"model": "nova-lite", "cost_usd": 0.0003}])
    result = smoke.run_case(smoke.CASES["s1_source_facts"], NOVA, _target(NOVA), client=Unusable({}))
    assert result["error_class"] == "unusable_reply" and result["cost_usd"] == pytest.approx(0.0003)


# ── the plan ─────────────────────────────────────────────────────────────────────────────────────────────────────

def _registry(route: dict, deps=None, conn=ACC2):
    deps = deps if deps is not None else DEPS
    stage_cfg = lambda s: StageConfig(s, "validate", "openai", route.get(s, ("gpt-6-luna",))[0], "acc3",  # noqa: E731
                                      fallback_model_ids=tuple(route.get(s, ("gpt-6-luna",))[1:]))
    return [patch("shared.llm_client.role_config.get_stage_config_sync", side_effect=stage_cfg),
            patch("shared.llm_client.deployments.deployments_for_sync",
                  side_effect=lambda k, enabled_only=True: list(deps.get(k, []))),
            patch("shared.llm_client.connections.registry_rows_sync", return_value={"acc2": conn}),
            patch("shared.llm_client.catalog.get_model_sync", side_effect=MODELS.get)]


def _plan(route: dict, **kw) -> dict:
    ps = _registry(route, **kw)
    for p in ps:
        p.start()
    try:
        return smoke.build_plan(None)
    finally:
        for p in ps:
            p.stop()


def test_the_plan_reads_the_route_then_falls_back_to_the_planned_model():
    plan = _plan({"a3_place_card_check": ("haiku-5-5", "sonnet-5-5", "nova-lite")})
    by = {e["stage"]: e for e in plan["stages"]}
    assert (by["a3_place_card_check"]["model_key"], by["a3_place_card_check"]["model_source"]) == ("nova-lite",
                                                                                                    "route")
    assert (by["a3_atomize"]["model_key"], by["a3_atomize"]["model_source"]) == ("nova-lite", "planned")
    assert all(e["callable"] for e in plan["stages"])
    # every stage, max_tokens fully spent: still far under the 0.10 USD cap of ADR 0009 decision 12
    assert 0 < plan["estimated_usd"] < smoke.COST_CAP_USD / 2 and plan["unpriced"] == 0


def test_the_plan_refuses_an_anthropic_pick_and_a_switched_off_deployment():
    off = {**DEPS, "llama4-scout": [ModelDeployment("llama4-scout", "acc2", "w", 20, False, "bedrock_profile")]}
    plan = _plan({"a3_atomize": ("gpt-6-luna", "haiku-5-5")}, deps=off)
    by = {e["stage"]: e for e in plan["stages"]}
    assert by["a3_atomize"]["model_key"] == "nova-lite"                    # haiku-5-5 has no acc2 deployment
    assert not by["a3_place_card_check"]["callable"] and "switched off" in by["a3_place_card_check"]["why_not"]
    plan = _plan({}, conn=ProviderConnection(connection_key="acc2", kind="bedrock", auth_mode="task_role",
                                             label="acc2", data_scope="aws_only", enabled=False))
    assert not any(e["callable"] for e in plan["stages"])


def test_an_override_replaces_the_route_and_the_planned_model_and_is_checked_like_them():
    ps = _registry({"a3_place_card_check": ("haiku-5-5", "sonnet-5-5", "nova-lite")})
    for p in ps:
        p.start()
    try:
        plan = smoke.build_plan(["a3_place_card_check", "a3_atomize", "a3_place_days"],
                                {"a3_place_card_check": "llama4-scout", "a3_atomize": "haiku-5-5"})
    finally:
        for p in ps:
            p.stop()
    by = {e["stage"]: e for e in plan["stages"]}
    assert (by["a3_place_card_check"]["model_key"], by["a3_place_card_check"]["model_source"]) == ("llama4-scout",
                                                                                                    "override")
    assert by["a3_place_card_check"]["callable"]
    assert by["a3_atomize"]["model_source"] == "override" and not by["a3_atomize"]["callable"]   # Anthropic: never
    assert (by["a3_place_days"]["model_key"], by["a3_place_days"]["model_source"]) == ("nova-lite", "planned")


def test_the_planned_anthropic_model_is_never_called():
    with patch.dict(smoke.PLANNED, {"a3_atomize": "haiku-5-5"}):
        plan = _plan({})
    entry = next(e for e in plan["stages"] if e["stage"] == "a3_atomize")
    assert not entry["callable"] and "Anthropic" in entry["why_not"]


# ── the job ──────────────────────────────────────────────────────────────────────────────────────────────────────

def _ctx(payload: dict):
    pool = MagicMock()
    db = MagicMock(execute=AsyncMock())
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=db)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    ctx = SimpleNamespace(payload=payload, pool=pool, job_id="job-1", result=None)
    ctx.set_result = lambda r: setattr(ctx, "result", r)
    return ctx, db


def test_the_kind_is_single_attempt_and_background():
    from shared.jobs.worker import load_kinds
    load_kinds()
    k = get_kind(smoke.KIND)
    assert k.max_attempts == 1 and k.background and not k.deploy_gate_exempt


@pytest.mark.asyncio
async def test_a_dry_run_makes_no_call():
    ctx, db = _ctx({})
    with patch.object(smoke, "build_plan", return_value={"stages": [], "unknown": [], "estimated_usd": 0.01,
                                                         "unpriced": 0}), \
            patch.object(smoke, "run_smoke", AsyncMock()) as run:
        result = await smoke.run(ctx)
    assert result["dry_run"] is True and result["stages_run"] == 0
    run.assert_not_called()
    db.execute.assert_not_called()


@pytest.mark.asyncio
async def test_a_live_run_over_the_cap_is_refused_before_any_call():
    ctx, _db = _ctx({"dry_run": False})
    with patch.object(smoke, "build_plan", return_value={"stages": [], "unknown": [], "estimated_usd": 0.5,
                                                         "unpriced": 0}), \
            patch.object(smoke, "run_smoke", AsyncMock()) as run:
        with pytest.raises(NonRetryable, match="over the 0.1 USD cap"):
            await smoke.run(ctx)
    run.assert_not_called()


@pytest.mark.asyncio
async def test_a_live_run_logs_each_reply_with_its_billing_fields_and_audits_once():
    stage = "t9_faq_filter"
    plan = {"stages": [{"stage": stage, "role": "validate", "model_key": "nova-lite", "model_source": "planned",
                        "callable": True, "why_not": None, "estimated_usd": 0.001},
                       {"stage": "a3_place_card_check", "role": "validate", "model_key": "llama4-scout",
                        "model_source": "planned", "callable": False, "why_not": "switched off",
                        "estimated_usd": None}],
            "unknown": [], "estimated_usd": 0.001, "unpriced": 0}
    client = FakeClient({stage: json.dumps(GOOD[stage])})
    client.stage = stage
    pool = MagicMock()
    with patch("shared.llm_client.catalog.get_model_sync", side_effect=MODELS.get), \
            patch.object(smoke, "_target", return_value=(NOVA, _target(NOVA), None)), \
            patch("shared.llm_client.call_log.record_call_with_pool", AsyncMock()) as rec:
        results = await smoke.run_smoke(pool, plan, client=client)
    assert [r["stage"] for r in results] == [stage, "a3_place_card_check"]
    assert results[0]["contract_ok"] is True and "replies" not in results[0]
    assert results[1]["called"] is False and results[1]["skipped"] == "switched off"
    kw = rec.await_args.kwargs
    assert kw["stage"] == "adhoc_aa1026" and kw["role"] == "validate" and kw["account"] == "acc2"
    assert kw["quality_signal"]["contract_smoke"] == stage and kw["quality_signal"]["parser_ok"] is True
    assert {"cache_read_tokens", "pricing_mode", "connection_key"} <= set(kw)      # **billed_fields(resp)
    assert smoke.summarize(results) == {"stages_run": 1, "passed": 1, "failed": [],
                                        "skipped": ["a3_place_card_check"], "cost_usd": pytest.approx(0.00002)}


@pytest.mark.asyncio
async def test_the_spent_cap_stops_the_remaining_stages():
    plan = {"stages": [{"stage": s, "role": "validate", "model_key": "nova-lite", "model_source": "planned",
                        "callable": True, "why_not": None, "estimated_usd": 0.001}
                       for s in ("a3_atomize", "a3_place_days")], "unknown": [], "estimated_usd": 0.002,
            "unpriced": 0}
    client = FakeClient({"a3_atomize": json.dumps(GOOD["a3_atomize"])})
    client.stage = "a3_atomize"
    with patch("shared.llm_client.catalog.get_model_sync", side_effect=MODELS.get), \
            patch.object(smoke, "_target", return_value=(NOVA, _target(NOVA), None)), \
            patch("shared.llm_client.call_log.record_call_with_pool", AsyncMock()):
        results = await smoke.run_smoke(MagicMock(), plan, client=client, cap_usd=0.00001)
    assert results[0]["called"] and results[1]["skipped"].startswith("cost cap")


@pytest.mark.asyncio
async def test_enqueue_refuses_an_unknown_stage_and_dedupes_the_list():
    with pytest.raises(ValueError, match="no contract smoke for stage"):
        await smoke.enqueue_manual(MagicMock(), dry_run=True, stages=["s1_generate"], actor="admin:x")
    with patch.object(smoke, "enqueue", AsyncMock(return_value=("job-1", True))) as enq:
        await smoke.enqueue_manual(MagicMock(), dry_run=False, stages=["a3_atomize", "a3_atomize"], actor="admin:x")
    payload = enq.await_args.args[2]
    assert payload == {"dry_run": False, "requested_by": "admin:x", "stages": ["a3_atomize"]}


@pytest.mark.asyncio
async def test_enqueue_checks_the_model_overrides():
    with pytest.raises(ValueError, match="no contract smoke for stage"):
        await smoke.enqueue_manual(MagicMock(), dry_run=True, stages=None, actor="admin:x",
                                   models={"s1_generate": "nova-lite"})
    with pytest.raises(ValueError, match="leaves out"):
        await smoke.enqueue_manual(MagicMock(), dry_run=True, stages=["a3_atomize"], actor="admin:x",
                                   models={"a3_place_days": "llama4-scout"})
    with pytest.raises(ValueError, match="needs a model key"):
        await smoke.enqueue_manual(MagicMock(), dry_run=True, stages=None, actor="admin:x", models={"a3_atomize": " "})
    with patch.object(smoke, "enqueue", AsyncMock(return_value=("job-2", True))) as enq:
        await smoke.enqueue_manual(MagicMock(), dry_run=True, stages=["a3_atomize"], actor="admin:x",
                                   models={"a3_atomize": " llama4-scout "})
    assert enq.await_args.args[2]["models"] == {"a3_atomize": "llama4-scout"}


@pytest.mark.asyncio
async def test_a_run_passes_the_overrides_to_the_plan():
    ctx, _db = _ctx({"stages": ["a3_atomize"], "models": {"a3_atomize": "llama4-scout"}})
    with patch.object(smoke, "build_plan", return_value={"stages": [], "unknown": [], "estimated_usd": 0.0,
                                                         "unpriced": 0}) as plan:
        await smoke.run(ctx)
    assert plan.call_args.args == (["a3_atomize"], {"a3_atomize": "llama4-scout"})


@pytest.mark.asyncio
async def test_the_admin_route_enqueues_and_maps_an_unknown_stage_to_422(monkeypatch):
    from fastapi import HTTPException

    from api.routers import admin_job_runner
    monkeypatch.setattr(admin_job_runner, "verify_admin_secret", lambda s: None)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=MagicMock())))
    with patch.object(smoke, "enqueue", AsyncMock(return_value=("job-9", True))):
        out = await admin_job_runner.llm_contract_smoke_run(
            admin_job_runner.LlmContractSmokeRunRequest(), request, x_admin_secret="s", x_admin_user_id="u1")
    assert out == {"job_id": "job-9", "kind": "llm_contract_smoke", "dry_run": True, "stages": None, "models": None}
    with pytest.raises(HTTPException) as e:
        await admin_job_runner.llm_contract_smoke_run(
            admin_job_runner.LlmContractSmokeRunRequest(stages=["t2_generate"]), request, x_admin_secret="s",
            x_admin_user_id="u1")
    assert e.value.status_code == 422


def test_the_route_needs_ai_admin():
    from api.core import staff_authz
    assert staff_authz.POLICY[("POST", "/admin/job-runner/llm-contract-smoke/run")] == staff_authz.AI_ADMIN
