"""AA-642 (D4, S263 plan T-7) — the Bedrock Batch kill switch.

POST /admin/s1-batch/submit answers 503 with the reason while the switch (env BATCH_SUBMIT_SWITCH) is closed, which is
the default, and nothing is materialized, uploaded or submitted. ``submit_batch_job`` refuses too (a caller other than
the API), after the account check. With the switch on, the endpoint reaches the submit path as before.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from api.routers import admin_pipeline
from shared.llm_client import bedrock_batch


@pytest.fixture(autouse=True)
def _no_secret_check(monkeypatch):
    monkeypatch.setattr(admin_pipeline, "verify_admin_secret", lambda _secret: None)


@pytest.mark.parametrize("value", [None, "", "off", "false", "0", "no", "maybe"])
def test_the_switch_is_closed_unless_set_on(monkeypatch, value):
    if value is None:
        monkeypatch.delenv(bedrock_batch.BATCH_SWITCH_ENV, raising=False)
    else:
        monkeypatch.setenv(bedrock_batch.BATCH_SWITCH_ENV, value)
    assert bedrock_batch.batch_submit_closed_reason() == bedrock_batch.BATCH_CLOSED_REASON


@pytest.mark.parametrize("value", ["on", "ON", "true", "1", "yes", " on "])
def test_the_switch_opens_with_on(monkeypatch, value):
    monkeypatch.setenv(bedrock_batch.BATCH_SWITCH_ENV, value)
    assert bedrock_batch.batch_submit_closed_reason() is None


def test_the_reason_names_the_trigger_and_the_switch():
    reason = bedrock_batch.BATCH_CLOSED_REASON
    assert "T-BATCH" in reason and bedrock_batch.BATCH_SWITCH_ENV in reason and "100 USD" in reason


@pytest.mark.asyncio
async def test_submit_endpoint_answers_503_and_submits_nothing_when_closed(monkeypatch):
    monkeypatch.delenv(bedrock_batch.BATCH_SWITCH_ENV, raising=False)
    submit = AsyncMock()
    req = admin_pipeline.S1BatchSubmitRequest(tour_ids=["t1"], batch_id="b1")
    with patch("services.content_generation.s1_batch.submit_s1_batch", new=submit):
        with pytest.raises(HTTPException) as exc:
            await admin_pipeline.submit_s1_batch_endpoint(req, request=None, x_admin_secret="x")
    assert exc.value.status_code == 503
    assert exc.value.detail == bedrock_batch.BATCH_CLOSED_REASON
    submit.assert_not_awaited()


@pytest.mark.asyncio
async def test_submit_endpoint_reaches_the_submit_path_when_open(monkeypatch):
    monkeypatch.setenv(bedrock_batch.BATCH_SWITCH_ENV, "on")
    submit = AsyncMock(side_effect=bedrock_batch.BatchUnavailable("below the minimum"))
    req = admin_pipeline.S1BatchSubmitRequest(tour_ids=["t1"], batch_id="b1")
    with patch("services.content_generation.s1_batch.submit_s1_batch", new=submit):
        with pytest.raises(HTTPException) as exc:
            await admin_pipeline.submit_s1_batch_endpoint(req, request=None, x_admin_secret="x")
    assert exc.value.status_code == 400            # the submit path ran (and refused on its own)
    submit.assert_awaited_once()


def test_submit_batch_job_refuses_when_closed_without_calling_bedrock(monkeypatch):
    monkeypatch.delenv(bedrock_batch.BATCH_SWITCH_ENV, raising=False)
    with patch.object(bedrock_batch, "get_satellite_client") as client:
        with pytest.raises(bedrock_batch.BatchUnavailable, match="switched off"):
            bedrock_batch.submit_batch_job("s3://bucket/key", account="acc3")
    client.assert_not_called()


def test_submit_batch_job_still_names_an_unknown_account_first(monkeypatch):
    monkeypatch.delenv(bedrock_batch.BATCH_SWITCH_ENV, raising=False)
    with pytest.raises(bedrock_batch.BatchUnavailable, match="unknown account"):
        bedrock_batch.submit_batch_job("s3://bucket/key", account="acc9")
