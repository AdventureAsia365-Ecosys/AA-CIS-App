"""AA-757 — atomize (t5_atomize) runs through the gateway, not a raw invoke_claude.

Before AA-757 both atomize call sites in services/acp_produce/tenant_pipeline.py called
shared.llm_client.bedrock_satellite.invoke_claude directly, so the stage had no route, no
fallback, no shadow and could only run Claude models. AA-757 moves both onto
LLMClient.generate(stage="t5_atomize"), which is what lets the admin switch the model (e.g. to
GPT-6 Luna) and gives fallback/shadow for free. These tests assert the gateway contract:

  * the LLMRequest carries stage "t5_atomize", the atom SYSTEM_PROMPT as the system prompt,
    max_tokens=4096 and NO model_tier (so the admin stage route decides the model);
  * the JSON parse is unchanged (strip_json_fence + ["atoms"]);
  * exactly one llm_call_log row per call, with cost/model/account/fallback/provider read from
    the gateway response (role from the stage config);
  * tenant_pipeline no longer imports invoke_claude.

They drive the real coroutine with a fake pool, same shape as test_aa508_atom_content_hash.py.
"""
import ast
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.acp_produce import tenant_pipeline
from services.acp_shared.atom_extraction import SYSTEM_PROMPT as _SYSTEM_PROMPT

TENANT_ID = "33333333-3333-3333-3333-333333333333"
TOUR_ID = "44444444-4444-4444-4444-444444444444"
VERSION_ID = "77777777-7777-7777-7777-777777777777"

ONE_DAY_ITINERARY = (
    "Day 1 — Arrival in Hanoi\n"
    "Walk through the Old Quarter and try street food."
)


def _pool_ctx(conn):
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    return pool


def _fake_conn():
    conn = AsyncMock()
    conn.fetch.return_value = []  # no fingerprints cached -> the one day is read
    return conn


def _fake_response(content='{"atoms": [{"place": "Old Quarter", "action": "walk"}]}'):
    """LLMResponse-shaped: the fields the atomize code reads off LLMClient.generate()."""
    return MagicMock(
        content=content, model_used="satellite-haiku-4-5", input_tokens=120, output_tokens=48,
        cost_usd=0.00042, stop_reason="end_turn", satellite_account="acc3",
        fallback_used=False, provider="bedrock-satellite",
    )


def _run(gen_mock, record_mock, *, stage_cfg=None):
    """Patch the gateway + stage config + cost log and run run_t5_atomize for ONE day."""
    stage_cfg = stage_cfg or MagicMock(model_id="haiku", account_route="acc3", role="writer")
    client = MagicMock()
    client.generate = gen_mock
    conn = _fake_conn()
    pool = _pool_ctx(conn)
    cm_client = patch("services.acp_produce.tenant_pipeline.LLMClient", return_value=client)
    cm_cfg = patch("services.acp_produce.tenant_pipeline.get_stage_config",
                   AsyncMock(return_value=stage_cfg))
    cm_log = patch("services.acp_produce.tenant_pipeline.record_call_with_pool", record_mock)
    cm_jev = patch("services.acp_produce.tenant_pipeline.decide",
                   AsyncMock(return_value=MagicMock(rejected=MagicMock(return_value=False))))
    return cm_client, cm_cfg, cm_log, cm_jev, pool


@pytest.mark.asyncio
async def test_atomize_calls_gateway_with_t5_stage_and_no_model_tier():
    """The one LLM call is LLMClient.generate(LLMRequest(stage='t5_atomize', ...)): the atom
    SYSTEM_PROMPT as system, max_tokens=4096, and model_tier unset so the admin route applies."""
    gen = MagicMock(return_value=_fake_response())
    record = AsyncMock()
    cm_client, cm_cfg, cm_log, cm_jev, pool = _run(gen, record)
    with cm_client, cm_cfg, cm_log, cm_jev:
        result = await tenant_pipeline.run_t5_atomize(
            TENANT_ID, TOUR_ID,
            {"name": "Hanoi", "summary": "s", "highlights": [], "itineraries": ONE_DAY_ITINERARY},
            pool, country="Vietnam", version_id=VERSION_ID,
        )

    assert result["status"] == "success"
    gen.assert_called_once()
    request = gen.call_args.args[0]
    assert request.stage == "t5_atomize"
    assert request.system_prompt == _SYSTEM_PROMPT
    assert request.max_tokens == 4096
    # No per-request model override: the admin stage route decides the model (fallback + shadow).
    assert request.model_tier is None
    # The day's body is in the user prompt (built by build_day_user_prompt), not the system one.
    assert "Old Quarter" in request.user_prompt


@pytest.mark.asyncio
async def test_cost_log_reads_from_gateway_response_not_recomputed():
    """Exactly one llm_call_log row for the call, with model/tokens/cost/account/fallback/
    provider taken straight from the gateway response and role from the stage config."""
    gen = MagicMock(return_value=_fake_response())
    record = AsyncMock()
    cm_client, cm_cfg, cm_log, cm_jev, pool = _run(gen, record)
    with cm_client, cm_cfg, cm_log, cm_jev:
        await tenant_pipeline.run_t5_atomize(
            TENANT_ID, TOUR_ID,
            {"name": "Hanoi", "summary": "s", "highlights": [], "itineraries": ONE_DAY_ITINERARY},
            pool, country="Vietnam", version_id=VERSION_ID,
        )

    record.assert_awaited_once()
    kw = record.await_args.kwargs
    assert kw["stage"] == "t5_atomize"
    assert kw["role"] == "writer"                 # from the stage config, not hardcoded
    assert kw["model"] == "satellite-haiku-4-5"
    assert kw["tokens_in"] == 120 and kw["tokens_out"] == 48
    assert kw["cost_usd"] == 0.00042              # from response.cost_usd, not recomputed
    assert kw["account"] == "acc3"
    assert kw["fallback_used"] is False
    assert kw["provider"] == "bedrock-satellite"


@pytest.mark.asyncio
async def test_json_parse_unchanged_strip_fence_and_atoms_key():
    """A fenced JSON body still parses via strip_json_fence + ['atoms']; a body missing the key
    fails the day exactly as before (no atoms written, status 'failed')."""
    gen = MagicMock(return_value=_fake_response(
        content='```json\n{"atoms": [{"place": "Old Quarter", "action": "walk"}]}\n```'))
    record = AsyncMock()
    cm_client, cm_cfg, cm_log, cm_jev, pool = _run(gen, record)
    with cm_client, cm_cfg, cm_log, cm_jev:
        ok = await tenant_pipeline.run_t5_atomize(
            TENANT_ID, TOUR_ID,
            {"name": "Hanoi", "summary": "s", "highlights": [], "itineraries": ONE_DAY_ITINERARY},
            pool, country="Vietnam", version_id=VERSION_ID,
        )
    assert ok["status"] == "success" and ok["atom_count"] == 1

    gen2 = MagicMock(return_value=_fake_response(content='{"not_atoms": []}'))
    record2 = AsyncMock()
    cm_client2, cm_cfg2, cm_log2, cm_jev2, pool2 = _run(gen2, record2)
    with cm_client2, cm_cfg2, cm_log2, cm_jev2:
        bad = await tenant_pipeline.run_t5_atomize(
            TENANT_ID, TOUR_ID,
            {"name": "Hanoi", "summary": "s", "highlights": [], "itineraries": ONE_DAY_ITINERARY},
            pool2, country="Vietnam", version_id=VERSION_ID,
        )
    assert bad["status"] == "failed"


def test_tenant_pipeline_no_longer_imports_invoke_claude():
    """The grep-level guard from the task: tenant_pipeline makes no invoke_claude call (the name
    survives only in a historical comment). Parsed with ast so comments do not count."""
    path = Path(tenant_pipeline.__file__)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    calls = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and (
            (isinstance(n.func, ast.Name) and n.func.id == "invoke_claude")
            or (isinstance(n.func, ast.Attribute) and n.func.attr == "invoke_claude")
        )
    ]
    assert calls == []
    # LLMClient is the import it uses instead.
    assert hasattr(tenant_pipeline, "LLMClient")
