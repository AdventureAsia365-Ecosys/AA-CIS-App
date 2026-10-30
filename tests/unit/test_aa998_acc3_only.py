"""AA-998 — acc3 is the only satellite account: every entry point that used to accept acc1 refuses it before any
AWS call, and every default is acc3."""
import inspect
from unittest.mock import MagicMock, patch

import re

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from shared.llm_client import bedrock_batch, bedrock_satellite as bs
from shared.llm_client.client import LLMClient


def test_the_satellite_table_holds_acc3_only():
    # AA-1002 (S3): the satellite accounts are the assume-role Bedrock connections; the built-in mirror of the 304
    # seed (used when the registry cannot be read) has acc3 only.
    from shared.llm_client import connections
    builtin = {k for k, c in connections.BUILTIN_CONNECTIONS.items() if c.auth_mode == "assume_role"}
    assert builtin == {"acc3"}
    assert set(bedrock_batch._BATCH_SERVICE_ROLE_ARN) == {"acc3"}
    assert bs.DEFAULT_SATELLITE_ACCOUNT == "acc3"
    assert bs.INFERENCE_PROFILE_SONNET == bs.legacy_inference_profile("acc3", "sonnet")
    assert not hasattr(bs, "_SATELLITE_ACCOUNTS") and not hasattr(bs, "_INFERENCE_PROFILES")


@pytest.mark.parametrize("fn", [bs._get_satellite_session, bs.get_satellite_client, bs.invoke_claude,
                                LLMClient._call_bedrock_satellite])
def test_defaults_are_acc3(fn):
    assert inspect.signature(fn).parameters["account"].default == "acc3"


def test_satellite_refuses_the_retired_account_without_sts():
    with patch.object(bs.boto3, "client") as m_client:
        with pytest.raises(ValueError, match="Unknown satellite account"):
            bs.invoke_claude("p", account="acc1")
        with pytest.raises(ValueError, match="Unknown satellite account"):
            bs.get_satellite_client("bedrock", account="acc1")
    m_client.assert_not_called()


def test_batch_submit_refuses_the_retired_account():
    with pytest.raises(bedrock_batch.BatchUnavailable, match="unknown account 'acc1'"):
        bedrock_batch.submit_batch_job("s3://b/k", account="acc1")


def test_s1_batch_request_accepts_acc3_only():
    from api.routers.admin_pipeline import S1BatchSubmitRequest

    assert S1BatchSubmitRequest(tour_ids=["t"], batch_id="b").account == "acc3"
    with pytest.raises(ValidationError):
        S1BatchSubmitRequest(tour_ids=["t"], batch_id="b", account="acc1")


def test_settings_offer_acc3_only():
    from api.routers import admin_llm_ops as ops

    assert list(ops._ACCOUNT_ROUTE_VALUES) == ["acc3"]
    assert [o["value"] for o in ops._account_route_options()] == ["acc3"]


@pytest.mark.asyncio
@pytest.mark.parametrize("route,ok", [("acc1", False), ("acc2", False), ("acc3", True), (None, True)])
async def test_patch_llm_config_refuses_any_account_route_but_acc3(route, ok):
    from api.routers import admin_llm_ops as ops

    row = {"stage": "t9_write", "role": "writer", "provider": "claude", "model_id": "haiku", "account_route": None,
           "fallback_model_ids": [], "shadow_model_id": None, "shadow_sample_pct": 0, "updated_at": None}

    async def rows():
        return [row]

    async def models():
        return None   # catalog unreachable -> the fixed writer options (haiku available)

    async def setter(stage, model_id, account_route, **kw):
        return {**row, "model_id": model_id, "account_route": account_route}

    with patch.object(ops, "verify_admin_secret"), patch.object(ops, "list_stage_configs", rows), \
            patch.object(ops, "_load_catalog", models), \
            patch.object(ops, "set_stage_config", side_effect=setter) as m_set, \
            patch.object(ops, "_wm_reverify", MagicMock(side_effect=lambda *a, **k: _noop())):
        if ok:
            out = await ops.patch_llm_config("t9_write", ops.LlmConfigPatch(model_id="haiku", account_route=route),
                                             x_admin_secret="x", x_admin_user_id="a")
            assert out["account_route"] == route
        else:
            with pytest.raises(HTTPException) as exc:
                await ops.patch_llm_config("t9_write", ops.LlmConfigPatch(model_id="haiku", account_route=route),
                                           x_admin_secret="x", x_admin_user_id="a")
            assert exc.value.status_code == 422 and "LLM account only" in exc.value.detail
            assert not re.search(r"acc[0-9]", exc.value.detail)   # AA-998: the admin never sees an account key
            m_set.assert_not_called()


async def _noop():
    return None
