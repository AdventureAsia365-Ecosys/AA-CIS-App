"""AA-900 — an R2 change eval logged under `adhoc_aa900` must be routed and billed like `writing_memory_eval`.

The eval label (call_log.bind_eval_stage) only changes the llm_call_log row. The request's own stage picks the route
(shared.llm_role_config), and the satellite account is acc3 whatever the label is. These tests pin that: with the label
bound, the gateway asks for the `writing_memory_eval` route (never `adhoc_aa900`, which has no route row) and every
Bedrock attempt goes to the acc3 satellite client with the acc3 profile id.

S257 (29/10/2026) context: the first `--stage adhoc_aa900` run failed every judge call with AccessDenied
"openai.gpt-5.6-luna is not available for this account". That was acc3 itself (same satellite session served Claude
Sonnet at the same minute), not the label: see docs/implementation-notes/AA-900.md.

No DB, no model, no AWS.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from services.writing_memory import config as cfg
from services.writing_memory import eval_judge
from shared.llm_client import call_log
from shared.llm_client.catalog import CatalogModel
from shared.llm_client.client import LLMClient
from shared.llm_client.models import LLMRequest
from shared.llm_client.role_config import StageConfig


def _model(key, **kw):
    base = dict(model_key=key, label=key, vendor="openai", provider="bedrock",
                api_style="converse", bedrock_profile_ids={"acc3": f"global.openai.{key}"},
                callable_via=("llm_client",), supports_temperature=False,
                price_in_per_mtok=0.2, price_out_per_mtok=1.2, enabled=True)
    base.update(kw)
    return CatalogModel(**base)


CATALOG = {m.model_key: m for m in (_model("gpt-5.6-luna"), _model("gpt-6-luna"))}

# The live route of the R2 judge stage (CIS 291): account_route acc3, Bedrock luna first.
EVAL_ROUTE = StageConfig(cfg.EVAL_STAGE, "judge", "openai", "gpt-5.6-luna", "acc3",
                         fallback_model_ids=("gpt-6-luna",), shadow_model_id=None, shadow_sample_pct=0)


def _stage_config(stage):
    """Only the real stage has a route row; `adhoc_aa900` (a label) has none, like on Dev."""
    return EVAL_ROUTE if stage == cfg.EVAL_STAGE else None


def _converse_ok(**_kwargs):
    return {"output": {"message": {"content": [{"text": "ok"}]}},
            "usage": {"inputTokens": 10, "outputTokens": 5}, "stopReason": "end_turn"}


def _client():
    with patch("shared.llm_client.client.boto3"), patch("shared.llm_client.client.openai"):
        return LLMClient()


def _generate(label, rt, request):
    """One gateway call with `label` bound (None = no eval run); returns (response, stages asked, accounts used)."""
    stages, accounts = [], []

    def stage_cfg(stage):
        stages.append(stage)
        return _stage_config(stage)

    def satellite(service, account="acc3"):
        accounts.append(account)
        return rt

    c = _client()
    with patch("shared.llm_client.client.get_stage_config_sync", side_effect=stage_cfg), \
            patch("shared.llm_client.client.get_model_sync", side_effect=CATALOG.get), \
            patch("shared.llm_client.bedrock_satellite.get_satellite_client", side_effect=satellite):
        if label is None:
            return c.generate(request), stages, accounts
        with call_log.bind_eval_stage(label, "run-1"):
            return c.generate(request), stages, accounts


@pytest.mark.parametrize("label", [None, cfg.EVAL_STAGE, cfg.CHANGE_EVAL_STAGE])
def test_the_label_never_changes_the_route_or_the_account(label):
    rt = MagicMock()
    rt.converse.side_effect = _converse_ok
    resp, stages, accounts = _generate(label, rt, LLMRequest(system_prompt="s", user_prompt="u",
                                                             stage=cfg.EVAL_STAGE))
    assert stages == [cfg.EVAL_STAGE]
    assert accounts == ["acc3"]
    assert rt.converse.call_args.kwargs["modelId"] == "global.openai.gpt-5.6-luna"
    assert resp.satellite_account == "acc3"
    assert resp.model_used == "satellite-gpt-5.6-luna"


def test_a_failing_primary_falls_back_on_the_same_route_and_account_under_the_change_label():
    rt = MagicMock()
    rt.converse.side_effect = [RuntimeError("primary down"), _converse_ok()]
    resp, stages, accounts = _generate(cfg.CHANGE_EVAL_STAGE, rt,
                                       LLMRequest(system_prompt="s", user_prompt="u", stage=cfg.EVAL_STAGE))
    assert stages == [cfg.EVAL_STAGE]
    assert accounts == ["acc3", "acc3"]
    assert [c.kwargs["modelId"] for c in rt.converse.call_args_list] == [
        "global.openai.gpt-5.6-luna", "global.openai.gpt-6-luna"]
    assert resp.fallback_used is True and resp.satellite_account == "acc3"


def test_the_r2_judge_asks_for_the_eval_stage_while_the_change_label_is_bound():
    fake = MagicMock()
    with patch.object(eval_judge, "LLMClient", return_value=fake), \
            call_log.bind_eval_stage(cfg.CHANGE_EVAL_STAGE, "run-1"):
        eval_judge._generate_sync("prompt")
    assert fake.generate.call_args.args[0].stage == cfg.EVAL_STAGE


def test_only_the_ledger_row_carries_the_change_label():
    with call_log.bind_eval_stage(cfg.CHANGE_EVAL_STAGE, "run-1"):
        stage, tenant, qs, piece, req = call_log._relabel(cfg.EVAL_STAGE, None, {"arm": "candidate"}, None, None)
    assert stage == cfg.CHANGE_EVAL_STAGE
    assert qs == {"arm": "candidate", "real_stage": cfg.EVAL_STAGE, "eval_run": "run-1"}
    assert (tenant, piece, req) == (None, None, None)
