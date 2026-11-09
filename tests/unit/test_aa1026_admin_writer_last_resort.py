"""AA-1026 (S263, tech lead T-3) — the admin route PATCH keeps working on the A-series writers after migration 327.

Migration 327 ends the routes of five writer stages on GPT-6 Luna through the OpenAI API (vendor_direct). The writer /
judge vendor rule marks a GPT model "not selectable" for a writer, and the route PATCH re-validates every fallback the
row keeps, so without an exception an admin could no longer change a shadow or a fallback of those stages. The
exception is narrow: the LAST fallback of a writer stage whose data scopes include vendor_direct may be a model of
another maker reached only outside AWS. Everything else keeps the old answer.

Review S263: T-3 holds for the whole route, not per model. The gateway tries every deployment of a model (acc3, then
the Anthropic API) before the route's next model, so a vendor_direct stage may not name a model reached inside AWS
after a model that also has a vendor API deployment (``_check_aws_before_vendor``, on both PATCH endpoints).

AA-1026 (S267, tech lead: "prefer the same model first, then go outside", root ADR 0009 decision 5 amendment): that
interleaving is now the decided order (model first: Sonnet 5.5 on acc3, Sonnet 5.5 on the Anthropic API, then Sonnet
4.6 on acc3), so the rule is relaxed: it refuses only a model reached ONLY outside AWS ahead of a model served inside
AWS (the vendor-only rung stays the end of the route), whatever the gateway's order switch says.
"""
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from shared.llm_client.catalog import CatalogModel


def _m(key, **kw):
    base = dict(model_key=key, label=key, vendor="anthropic", provider="bedrock", api_style="converse",
                bedrock_profile_ids={"acc3": f"global.anthropic.claude-{key}"}, callable_via=("llm_client",),
                price_in_per_mtok=0.1, price_out_per_mtok=0.5, enabled=True)
    base.update(kw)
    return CatalogModel(**base)


MODELS = [
    _m("haiku-5-5"), _m("sonnet-5-5"),
    _m("gpt-6-luna", vendor="openai", bedrock_profile_ids={"acc3": "global.openai.gpt-6-luna"}),
    _m("gpt-6-luna-openai", vendor="openai", provider="openai", api_style="openai_chat", bedrock_profile_ids={},
       wire_model="gpt-6-luna"),
]
REACH = {"haiku-5-5": frozenset({"aws_only", "vendor_direct"}), "sonnet-5-5": frozenset({"aws_only", "vendor_direct"}),
         "gpt-6-luna": frozenset({"aws_only"}), "gpt-6-luna-openai": frozenset({"vendor_direct"})}
BOTH = ["aws_only", "vendor_direct"]


def _writer_row(scopes, fallbacks=("gpt-6-luna-openai",), stage="supply_content_writer"):
    return {"stage": stage, "role": "writer", "model_id": "sonnet-5-5", "fallback_model_ids": list(fallbacks),
            "shadow_model_id": None, "shadow_sample_pct": 0, "allowed_scopes": scopes}


async def _patch_route(body, row, models=MODELS):
    from api.routers import admin_llm_ops as ops

    async def setter(stage, **kw):
        return {"stage": stage, "model_id": row["model_id"], "updated_at": None, **kw}
    with patch.object(ops, "verify_admin_secret"), \
            patch.object(ops, "list_stage_configs", AsyncMock(return_value=[row])), \
            patch.object(ops, "_load_catalog", AsyncMock(return_value=models)), \
            patch.object(ops, "_load_reach", AsyncMock(return_value=REACH)), \
            patch.object(ops, "set_stage_route", setter):
        return await ops.patch_llm_route(row["stage"], ops.LlmRoutePatch(**body), x_admin_secret="x",
                                         x_admin_user_id="a")


@pytest.mark.asyncio
async def test_a_shadow_change_keeps_the_openai_api_last_resort_of_a_vendor_direct_writer():
    out = await _patch_route({"shadow_model_id": "haiku-5-5", "shadow_sample_pct": 5}, _writer_row(BOTH))
    assert out["fallback_model_ids"] == ["gpt-6-luna-openai"]
    assert (out["shadow_model_id"], out["shadow_sample_pct"]) == ("haiku-5-5", 5)


@pytest.mark.asyncio
async def test_the_writer_last_resort_may_be_saved_explicitly():
    out = await _patch_route({"fallback_model_ids": ["gpt-6-luna-openai"]}, _writer_row(BOTH))
    assert out["fallback_model_ids"] == ["gpt-6-luna-openai"]


@pytest.mark.asyncio
@pytest.mark.parametrize("body,detail", [
    ({"fallback_model_ids": ["gpt-6-luna-openai", "haiku-5-5"]}, "last fallback"),          # not last
    ({"shadow_model_id": "gpt-6-luna-openai", "shadow_sample_pct": 5}, "last fallback"),    # never a shadow
    ({"fallback_model_ids": ["gpt-6-luna", "gpt-6-luna-openai"]}, "'gpt-6-luna' is not selectable"),  # GPT on AWS
], ids=["not_last", "shadow", "other_maker_on_aws"])
async def test_the_vendor_rule_still_holds_everywhere_else(body, detail):
    with pytest.raises(HTTPException) as exc:
        await _patch_route(body, _writer_row(BOTH))
    assert exc.value.status_code == 422 and detail in exc.value.detail


@pytest.mark.asyncio
async def test_an_aws_only_writer_may_not_end_on_the_openai_api():
    with pytest.raises(HTTPException) as exc:
        await _patch_route({"fallback_model_ids": ["haiku-5-5", "gpt-6-luna-openai"]},
                           _writer_row(["aws_only"], fallbacks=("haiku-5-5",), stage="t9_write"))
    assert exc.value.status_code == 422 and "may not send content outside AWS" in exc.value.detail


@pytest.mark.asyncio
async def test_a_disabled_last_resort_is_refused():
    models = [m for m in MODELS if m.model_key != "gpt-6-luna-openai"] + [
        _m("gpt-6-luna-openai", vendor="openai", provider="openai", api_style="openai_chat", bedrock_profile_ids={},
           enabled=False)]
    with pytest.raises(HTTPException) as exc:
        await _patch_route({"shadow_model_id": None}, _writer_row(BOTH), models=models)
    assert exc.value.status_code == 422 and "'gpt-6-luna-openai' is not selectable" in exc.value.detail


def test_the_helper_answers_only_for_a_vendor_direct_writer():
    from api.routers.admin_llm_ops import _writer_last_resort
    outside = frozenset({"gpt-6-luna-openai"})
    assert _writer_last_resort("writer", ("aws_only", "vendor_direct"), MODELS, outside, "gpt-6-luna-openai")
    assert not _writer_last_resort("judge", ("aws_only", "vendor_direct"), MODELS, outside, "gpt-6-luna-openai")
    assert not _writer_last_resort("writer", ("aws_only",), MODELS, outside, "gpt-6-luna-openai")
    assert not _writer_last_resort("writer", None, MODELS, outside, "gpt-6-luna-openai")
    assert not _writer_last_resort("writer", ("aws_only", "vendor_direct"), MODELS, outside, "gpt-6-luna")


@pytest.mark.asyncio
@pytest.mark.parametrize("aws_first", ["off", "on"])
async def test_an_aws_fallback_behind_a_model_with_a_vendor_api_now_saves(monkeypatch, aws_first):
    """S267: sonnet-5-5 -> haiku-5-5 -> GPT-6 Luna (OpenAI API). Model first, the gateway calls Sonnet 5.5 on acc3,
    then on the Anthropic API, then Haiku 5.5 on acc3: the order the tech lead chose (S263 refused it)."""
    from api.routers import admin_llm_ops as ops
    monkeypatch.setenv("LLM_ROUTE_AWS_FIRST", aws_first)
    with patch.object(ops, "_change_evidence", AsyncMock(return_value={"eval_run_id": "run-1"})):
        out = await _patch_route({"fallback_model_ids": ["haiku-5-5", "gpt-6-luna-openai"], "eval_run_id": "run-1"},
                                 _writer_row(BOTH))
    assert out["fallback_model_ids"] == ["haiku-5-5", "gpt-6-luna-openai"]


@pytest.mark.asyncio
async def test_a_primary_change_ahead_of_an_aws_fallback_now_saves():
    from api.routers import admin_llm_ops as ops
    # a writer whose primary is served only on acc3 (an invented key with no registry row) and whose route goes on to
    # haiku-5-5 and GPT-6 Luna: sonnet-5-5 as the primary puts its Anthropic API ahead of haiku-5-5 on acc3, which is
    # the model-first order of S267
    row = {**_writer_row(BOTH, fallbacks=("haiku-5-5", "gpt-6-luna-openai")), "model_id": "claude-acc3-only"}
    with patch.object(ops, "verify_admin_secret"), \
            patch.object(ops, "list_stage_configs", AsyncMock(return_value=[row])), \
            patch.object(ops, "_load_catalog", AsyncMock(return_value=MODELS)), \
            patch.object(ops, "_load_reach", AsyncMock(return_value=REACH)), \
            patch.object(ops, "_change_evidence", AsyncMock(return_value={"eval_run_id": "run-1"})), \
            patch.object(ops, "set_stage_config", AsyncMock(side_effect=RuntimeError("saved"))) as setter:
        with pytest.raises(RuntimeError, match="saved"):                  # every check passed: the save was reached
            await ops.patch_llm_config("supply_content_writer", ops.LlmConfigPatch(model_id="sonnet-5-5"),
                                       x_admin_secret="x", x_admin_user_id="a")
    setter.assert_awaited_once()


@pytest.mark.parametrize("aws_first", ["off", "on"])
@pytest.mark.parametrize("scopes,chain,refused", [
    (("aws_only", "vendor_direct"), ["sonnet-5-5", "gpt-6-luna-openai"], False),               # the five writers
    (("aws_only", "vendor_direct"), ["gpt-6-luna", "haiku-5-5", "gpt-6-luna-openai"], False),  # AWS-only model ahead
    (("aws_only", "vendor_direct"), ["sonnet-5-5", "haiku-5-5", "gpt-6-luna-openai"], False),  # model first (S267)
    (("aws_only", "vendor_direct"), ["sonnet-5-5", "unknown-model"], False),      # no registry row: called on acc3
    (("aws_only", "vendor_direct"), ["sonnet-5-5", "sonnet-5-5"], False),         # the same model again
    (("aws_only", "vendor_direct"), ["gpt-6-luna-openai", "gpt-6-luna"], True),   # vendor-only ahead of an AWS model
    (("aws_only", "vendor_direct"), ["sonnet-5-5", "gpt-6-luna-openai", "unknown-model"], True),
    (("aws_only",), ["sonnet-5-5", "haiku-5-5"], False),                          # never reaches a vendor API
    (None, ["gpt-6-luna-openai", "haiku-5-5"], False),                            # no stage policy: the S3 rule
], ids=["writer", "aws_model_ahead", "model_first", "unknown_after", "duplicate", "vendor_only_ahead",
        "vendor_only_mid", "aws_only_stage", "no_policy"])
def test_the_aws_before_vendor_rule(monkeypatch, aws_first, scopes, chain, refused):
    from api.routers.admin_llm_ops import _check_aws_before_vendor
    monkeypatch.setenv("LLM_ROUTE_AWS_FIRST", aws_first)
    if refused:
        with pytest.raises(HTTPException) as exc:
            _check_aws_before_vendor("s", scopes, REACH, chain)
        assert "may leave AWS only as the last resort" in exc.value.detail
        assert "gpt-6-luna-openai is reached only through a vendor API" in exc.value.detail
    else:
        _check_aws_before_vendor("s", scopes, REACH, chain)


def test_with_deployment_routing_off_the_relaxed_rule_is_the_same(monkeypatch):
    from api.routers.admin_llm_ops import _check_aws_before_vendor
    monkeypatch.setenv("LLM_DEPLOYMENT_ROUTING", "off")
    _check_aws_before_vendor("s", ("aws_only", "vendor_direct"), REACH,
                             ["sonnet-5-5", "haiku-5-5", "gpt-6-luna-openai"])
    with pytest.raises(HTTPException):
        _check_aws_before_vendor("s", ("aws_only", "vendor_direct"), REACH, ["gpt-6-luna-openai", "haiku-5-5"])


# ── S267 Q2: "openai-direct for a GPT twin if one exists" — the twin right behind its AWS model ─────────────────────

TWINS = {"gpt-6-luna-openai": frozenset({"gpt-6-luna"})}


def test_the_catalog_names_gpt_6_luna_openai_the_twin_of_gpt_6_luna():
    from shared.llm_client import data_scope
    assert data_scope.vendor_twins(MODELS, REACH) == TWINS


@pytest.mark.parametrize("chain,refused", [
    (["gpt-6-luna", "gpt-6-luna-openai", "haiku-5-5"], False),            # the twin in GPT-6 Luna's slot
    (["gpt-5.6-luna", "gpt-6-luna", "gpt-6-luna-openai", "haiku-5-5"], False),
    (["haiku-5-5", "gpt-6-luna-openai", "gpt-6-luna"], True),             # not behind its AWS model
    (["gpt-6-luna", "haiku-5-5", "gpt-6-luna-openai", "sonnet-5-5"], True),
], ids=["twin_slot", "twin_slot_mid", "twin_ahead_of_its_model", "twin_after_another_model"])
def test_the_aws_before_vendor_rule_keeps_a_twin_in_its_model_slot(chain, refused):
    from api.routers.admin_llm_ops import _check_aws_before_vendor
    if refused:
        with pytest.raises(HTTPException):
            _check_aws_before_vendor("s", ("aws_only", "vendor_direct"), REACH, chain, TWINS)
    else:
        _check_aws_before_vendor("s", ("aws_only", "vendor_direct"), REACH, chain, TWINS)


def _validate_row(fallbacks):
    return {"stage": "a3_atomize", "role": "validate", "model_id": "gpt-6-luna", "fallback_model_ids": list(fallbacks),
            "shadow_model_id": None, "shadow_sample_pct": 0, "allowed_scopes": BOTH}


@pytest.mark.asyncio
async def test_a_route_with_the_twin_right_behind_gpt_6_luna_saves():
    out = await _patch_route({"fallback_model_ids": ["gpt-6-luna-openai", "haiku-5-5"]}, _validate_row(["haiku-5-5"]))
    assert out["fallback_model_ids"] == ["gpt-6-luna-openai", "haiku-5-5"]


@pytest.mark.asyncio
async def test_the_twin_anywhere_else_mid_route_is_still_refused():
    with pytest.raises(HTTPException) as exc:
        await _patch_route({"fallback_model_ids": ["haiku-5-5", "gpt-6-luna-openai", "sonnet-5-5"]},
                           _validate_row(["haiku-5-5"]))
    assert exc.value.status_code == 422 and "last fallback" in exc.value.detail
