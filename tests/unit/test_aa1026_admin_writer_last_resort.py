"""AA-1026 (S263, tech lead T-3) — the admin route PATCH keeps working on the A-series writers after migration 327.

Migration 327 ends the routes of five writer stages on GPT-6 Luna through the OpenAI API (vendor_direct). The writer /
judge vendor rule marks a GPT model "not selectable" for a writer, and the route PATCH re-validates every fallback the
row keeps, so without an exception an admin could no longer change a shadow or a fallback of those stages. The
exception is narrow: the LAST fallback of a writer stage whose data scopes include vendor_direct may be a model of
another maker reached only outside AWS. Everything else keeps the old answer.

Review S263: T-3 holds for the whole route, not per model. The gateway tries every deployment of a model (acc3, then
the Anthropic API) before the route's next model, so a vendor_direct stage may not name a model reached inside AWS
after a model that also has a vendor API deployment (``_check_aws_before_vendor``, on both PATCH endpoints).
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
async def test_an_aws_fallback_behind_a_model_with_a_vendor_api_is_refused():
    """sonnet-5-5 -> haiku-5-5 -> GPT-6 Luna (OpenAI API): the gateway would call sonnet-5-5 on the Anthropic API
    before haiku-5-5 on acc3, i.e. leave AWS while an AWS path is still untried."""
    with pytest.raises(HTTPException) as exc:
        await _patch_route({"fallback_model_ids": ["haiku-5-5", "gpt-6-luna-openai"]}, _writer_row(BOTH))
    assert exc.value.status_code == 422
    assert "may leave AWS only when every AWS path of its route is blocked" in exc.value.detail
    assert "haiku-5-5" in exc.value.detail


@pytest.mark.asyncio
async def test_a_primary_change_that_would_put_a_vendor_api_ahead_of_an_aws_fallback_is_refused():
    from api.routers import admin_llm_ops as ops
    # a writer whose primary is served only on acc3 (an invented key with no registry row) and whose route goes on to
    # haiku-5-5 and GPT-6 Luna: making sonnet-5-5 the primary would put its Anthropic API ahead of haiku-5-5 on acc3
    row = {**_writer_row(BOTH, fallbacks=("haiku-5-5", "gpt-6-luna-openai")), "model_id": "claude-acc3-only"}
    with patch.object(ops, "verify_admin_secret"), \
            patch.object(ops, "list_stage_configs", AsyncMock(return_value=[row])), \
            patch.object(ops, "_load_catalog", AsyncMock(return_value=MODELS)), \
            patch.object(ops, "_load_reach", AsyncMock(return_value=REACH)), \
            patch.object(ops, "set_stage_config", AsyncMock()) as setter:
        with pytest.raises(HTTPException) as exc:
            await ops.patch_llm_config("supply_content_writer", ops.LlmConfigPatch(model_id="sonnet-5-5"),
                                       x_admin_secret="x", x_admin_user_id="a")
    assert exc.value.status_code == 422 and "AWS deployment of haiku-5-5" in exc.value.detail
    setter.assert_not_awaited()


@pytest.mark.parametrize("scopes,chain,refused", [
    (("aws_only", "vendor_direct"), ["sonnet-5-5", "gpt-6-luna-openai"], False),               # the five writers
    (("aws_only", "vendor_direct"), ["gpt-6-luna", "haiku-5-5", "gpt-6-luna-openai"], False),  # AWS-only model ahead
    (("aws_only", "vendor_direct"), ["sonnet-5-5", "haiku-5-5", "gpt-6-luna-openai"], True),
    (("aws_only", "vendor_direct"), ["sonnet-5-5", "unknown-model"], True),       # no registry row: called on acc3
    (("aws_only", "vendor_direct"), ["sonnet-5-5", "sonnet-5-5"], False),         # the same model again
    (("aws_only",), ["sonnet-5-5", "haiku-5-5"], False),                          # never reaches a vendor API
    (None, ["sonnet-5-5", "haiku-5-5"], False),                                   # no stage policy: the S3 rule
], ids=["writer", "aws_model_ahead", "interleaved", "unknown_after", "duplicate", "aws_only_stage", "no_policy"])
def test_the_aws_before_vendor_rule(scopes, chain, refused):
    from api.routers.admin_llm_ops import _check_aws_before_vendor
    if refused:
        with pytest.raises(HTTPException):
            _check_aws_before_vendor("s", scopes, REACH, chain)
    else:
        _check_aws_before_vendor("s", scopes, REACH, chain)
