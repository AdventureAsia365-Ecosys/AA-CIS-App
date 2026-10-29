"""AA-900 — account-level LLM refusals: the classifier, the gateway's typed error, and the job runner's retry rule.

Since 29/10/2026 every Bedrock model on the LLM account answers "Error 002: Access to Bedrock models is not allowed for
this account". A retry minutes later is refused the same way, so a job must fail at once (reason llm_access_denied)
instead of burning its attempts; a throttle or an outage keeps the old retry rule.
"""
from unittest.mock import AsyncMock, patch

import pytest

from shared.jobs import queue
from shared.jobs.registry import JobContext, NonRetryable
from shared.jobs.worker import failure_outcome
from shared.llm_client import errors as E
from shared.llm_client.catalog import CatalogModel
from shared.llm_client.client import LLMClient
from shared.llm_client.models import LLMRequest, LLMResponse
from shared.llm_client.role_config import StageConfig

ERROR_002 = ("An error occurred (AccessDeniedException) when calling the Converse operation: "
             "Error 002: Access to Bedrock models is not allowed for this account")
NOT_AVAILABLE = "AccessDeniedException: openai.gpt-5.6-luna is not available for this account."
NO_CREDIT = ("RateLimitError: Error code: 429 - {'error': {'message': 'You exceeded your current quota, please check "
             "your plan and billing details.', 'type': 'insufficient_quota', 'code': 'insufficient_quota'}}")
THROTTLE = ("An error occurred (ThrottlingException) when calling the Converse operation (reached max retries: 2): "
            "Too many requests, please wait before trying again.")
RATE_LIMIT = "RateLimitError: Error code: 429 - {'error': {'code': 'rate_limit_exceeded'}}"


# ── classifier ────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text, expected", [
    (ERROR_002, E.ACCESS_DENIED),
    ("Error 002: Access to Bedrock models is not allowed for this account", E.ACCESS_DENIED),
    (NOT_AVAILABLE, E.ACCESS_DENIED),
    ("User: arn:aws:sts::000000000000:assumed-role/x is not authorized to perform: bedrock:InvokeModel",
     E.ACCESS_DENIED),
    ("An error occurred (AccessDenied) when calling the AssumeRole operation", E.ACCESS_DENIED),
    ("PermissionDeniedError: Error code: 403 - unsupported_country_region_territory", E.ACCESS_DENIED),
    (NO_CREDIT, E.QUOTA_EXHAUSTED),
    (THROTTLE, E.THROTTLED),
    (RATE_LIMIT, E.THROTTLED),
    ("ReadTimeoutError: Read timeout on endpoint URL", E.OTHER),
    ("gpt-5.6-luna output does not match schema 'verdict'", E.OTHER),
    ("llm_access_denied: All models in the route failed", E.ACCESS_DENIED),
    ("", E.OTHER),
    (None, E.OTHER),
])
def test_classify_text(text, expected):
    assert E.classify_text(text) == expected
    assert E.classify(text) == expected


def test_classify_walks_the_cause_chain():
    try:
        try:
            raise RuntimeError(ERROR_002)
        except RuntimeError as inner:
            raise RuntimeError("Satellite Bedrock failed") from inner
    except RuntimeError as outer:
        assert E.classify(outer) == E.ACCESS_DENIED


def test_an_aggregate_with_a_decided_class_is_not_reread_from_its_cause():
    """A route that saw one denied model and one throttled one is not account-level: its last cause must not decide."""
    agg = RuntimeError("All models in the route failed")
    agg.llm_error_class = E.THROTTLED
    agg.__cause__ = RuntimeError(ERROR_002)
    assert E.classify(agg) == E.THROTTLED
    assert not E.is_llm_access_denied(agg)


def test_account_level_class():
    assert E.account_level_class([]) is None
    assert E.account_level_class([E.ACCESS_DENIED, E.ACCESS_DENIED]) == E.ACCESS_DENIED
    assert E.account_level_class([E.ACCESS_DENIED, E.QUOTA_EXHAUSTED]) == E.ACCESS_DENIED
    assert E.account_level_class([E.QUOTA_EXHAUSTED]) == E.QUOTA_EXHAUSTED
    assert E.account_level_class([E.ACCESS_DENIED, E.THROTTLED]) is None
    assert E.account_level_class([E.ACCESS_DENIED, E.OTHER]) is None


def test_typed_error_message_and_class():
    import copy
    import pickle

    exc = E.LLMAccessDenied("every model refused", E.QUOTA_EXHAUSTED)
    assert isinstance(exc, RuntimeError)
    assert str(exc) == "llm_access_denied: every model refused"
    for clone in (copy.copy(exc), pickle.loads(pickle.dumps(exc))):
        assert str(clone) == str(exc) and clone.llm_error_class == E.QUOTA_EXHAUSTED
    assert E.classify(exc) == E.QUOTA_EXHAUSTED
    wrapped = RuntimeError("run failed")
    wrapped.__cause__ = exc
    assert E.is_llm_access_denied(wrapped)
    assert not E.is_llm_access_denied(RuntimeError(ERROR_002))  # text alone never decides a retry


# ── gateway ───────────────────────────────────────────────────────────────────────────────────────────────────────

def _model(key, **kw):
    base = dict(model_key=key, label=key, vendor="openai", provider="bedrock",
                api_style="converse", bedrock_profile_ids={"acc3": f"global.openai.{key}"},
                callable_via=("llm_client",), supports_temperature=False,
                price_in_per_mtok=0.2, price_out_per_mtok=1.2, enabled=True)
    base.update(kw)
    return CatalogModel(**base)


CATALOG = {m.model_key: m for m in (
    _model("gpt-5.6-luna"), _model("gpt-6-luna"),
    _model("gpt-6-luna-openai", provider="openai", api_style="openai_chat", bedrock_profile_ids={},
           wire_model="gpt-6-luna"),
    _model("off-model", enabled=False, blocked_reason="disabled"),
)}
ROUTE = StageConfig("t10_judge", "judge", "openai", "gpt-5.6-luna", None,
                    fallback_model_ids=("off-model", "gpt-6-luna", "gpt-6-luna-openai"),
                    shadow_model_id=None, shadow_sample_pct=0)
SINGLE = StageConfig("t10_judge", "judge", "openai", "gpt-5.6-luna", None,
                     fallback_model_ids=(), shadow_model_id=None, shadow_sample_pct=0)


def _client():
    with patch("shared.llm_client.client.boto3"), patch("shared.llm_client.client.openai"):
        return LLMClient()


def _req():
    return LLMRequest(system_prompt="s", user_prompt="u", stage="t10_judge")


def _gen(client, cfg, errors_by_key):
    def fake(request, key, *_):
        err = errors_by_key.get(key)
        if err is None:
            return LLMResponse(content="{}", model_used=key, provider="p")
        raise RuntimeError(err)
    with patch.object(client, "_call_key", side_effect=fake), \
            patch("shared.llm_client.client.get_stage_config_sync", return_value=cfg), \
            patch("shared.llm_client.client.get_model_sync", side_effect=CATALOG.get):
        return client.generate(_req())


def test_route_every_model_denied_raises_typed_error():
    with pytest.raises(E.LLMAccessDenied) as ei:
        _gen(_client(), ROUTE, {"gpt-5.6-luna": ERROR_002, "gpt-6-luna": NOT_AVAILABLE,
                                "gpt-6-luna-openai": NO_CREDIT})
    assert str(ei.value).startswith("llm_access_denied: All models in the route failed")
    assert ei.value.llm_error_class == E.ACCESS_DENIED
    assert E.classify(ei.value.__cause__) == E.QUOTA_EXHAUSTED  # the provider's own words stay the cause


def test_route_every_model_out_of_credit_is_quota_class():
    with pytest.raises(E.LLMAccessDenied) as ei:
        _gen(_client(), ROUTE, {k: NO_CREDIT for k in ("gpt-5.6-luna", "gpt-6-luna", "gpt-6-luna-openai")})
    assert ei.value.llm_error_class == E.QUOTA_EXHAUSTED


def test_route_with_one_throttled_model_keeps_the_plain_error():
    with pytest.raises(RuntimeError) as ei:
        _gen(_client(), ROUTE, {"gpt-5.6-luna": ERROR_002, "gpt-6-luna": ERROR_002, "gpt-6-luna-openai": RATE_LIMIT})
    assert not isinstance(ei.value, E.LLMAccessDenied)
    assert not E.is_llm_access_denied(ei.value)
    assert E.classify(ei.value) == E.THROTTLED


def test_route_with_an_outage_keeps_the_plain_error():
    with pytest.raises(RuntimeError) as ei:
        _gen(_client(), ROUTE, {"gpt-5.6-luna": ERROR_002, "gpt-6-luna": "ReadTimeoutError",
                                "gpt-6-luna-openai": ERROR_002})
    assert not E.is_llm_access_denied(ei.value)
    assert E.classify(ei.value) == E.OTHER


def test_route_falls_back_when_only_some_models_are_denied():
    resp = _gen(_client(), ROUTE, {"gpt-5.6-luna": ERROR_002, "gpt-6-luna": ERROR_002})
    assert resp.model_used == "gpt-6-luna-openai" and resp.fallback_used is True


def test_single_model_denied_raises_typed_error():
    with pytest.raises(E.LLMAccessDenied):
        _gen(_client(), SINGLE, {"gpt-5.6-luna": ERROR_002})


def test_single_model_throttled_raises_the_provider_error_unchanged():
    with pytest.raises(RuntimeError) as ei:
        _gen(_client(), SINGLE, {"gpt-5.6-luna": THROTTLE})
    assert not E.is_llm_access_denied(ei.value)
    assert str(ei.value) == THROTTLE


def test_legacy_chain_every_tier_denied_raises_typed_error():
    c = _client()
    refused = RuntimeError(f"Satellite Bedrock failed: {ERROR_002}")
    with patch.object(c, "_call_bedrock_satellite", side_effect=refused), \
            patch("shared.llm_client.client.openai_platform_allowed", return_value=False), \
            patch("shared.llm_client.client.legacy_acc2_native_enabled", return_value=False):
        with pytest.raises(E.LLMAccessDenied, match="GPT-4.1 retired"):
            c._legacy_chain(_req(), "sonnet", "acc3")


def test_legacy_chain_with_a_throttled_tier_keeps_the_plain_error():
    c = _client()
    side = [RuntimeError(f"Satellite Bedrock failed: {ERROR_002}"),
            RuntimeError(f"Satellite Bedrock failed: {THROTTLE}")]
    with patch.object(c, "_call_bedrock_satellite", side_effect=side), \
            patch("shared.llm_client.client.openai_platform_allowed", return_value=False), \
            patch("shared.llm_client.client.legacy_acc2_native_enabled", return_value=False):
        with pytest.raises(RuntimeError) as ei:
            c._legacy_chain(_req(), "sonnet", "acc3")
    assert not E.is_llm_access_denied(ei.value)


# ── job runner retry decision ─────────────────────────────────────────────────────────────────────────────────────

def test_failure_outcome_typed_denial_does_not_retry():
    error, retryable, reason = failure_outcome(E.LLMAccessDenied("All models in the route failed: " + ERROR_002))
    assert retryable is False and reason == "llm_access_denied"
    assert error.startswith("llm_access_denied: All models")
    assert not error.startswith("llm_access_denied: llm_access_denied")


def test_failure_outcome_wrapped_denial_is_prefixed():
    outer = RuntimeError("t2 rewrite failed")
    outer.__cause__ = E.LLMAccessDenied("x")
    error, retryable, reason = failure_outcome(outer)
    assert (retryable, reason) == (False, "llm_access_denied")
    assert error.startswith("llm_access_denied: RuntimeError: t2 rewrite failed")


def test_failure_outcome_keeps_the_old_rule_for_everything_else():
    assert failure_outcome(RuntimeError(THROTTLE)) == (f"RuntimeError: {THROTTLE}", True, None)
    # the text of a denial without the gateway's decision does not stop retries
    assert failure_outcome(RuntimeError(ERROR_002))[1:] == (True, None)
    assert failure_outcome(NonRetryable("bad payload")) == ("NonRetryable: bad payload", False, None)


# ── S1 writer path: no internal retries, the job sees the typed error ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_run_tour_safe_stops_at_the_first_denial_and_raises_it():
    from api.routers import admin_pipeline as ap

    denied = E.LLMAccessDenied("s1_generate refused")
    req = ap.TourRunRequest(tour_id="t1", batch_id="not-a-uuid", tenant_id="x")
    with patch.object(ap, "_execute_run_tour", AsyncMock(side_effect=denied)) as run, \
            patch.object(ap.asyncio, "sleep", AsyncMock()) as sleep:
        with pytest.raises(E.LLMAccessDenied):
            await ap._run_tour_safe(req)
    assert run.await_count == 1
    sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_tour_safe_still_swallows_other_failures():
    from api.routers import admin_pipeline as ap

    req = ap.TourRunRequest(tour_id="t1", batch_id="not-a-uuid", tenant_id="x")
    with patch.object(ap, "_execute_run_tour", AsyncMock(side_effect=RuntimeError(THROTTLE))) as run, \
            patch.object(ap.asyncio, "sleep", AsyncMock()):
        assert await ap._run_tour_safe(req) is None
    assert run.await_count == 3


@pytest.mark.asyncio
async def test_s1_rewrite_job_clears_its_checkpoint_and_raises_the_denial():
    from services.jobs import s1_rewrite_job as sj

    job = queue.Job(id="job-1", kind="s1_rewrite", payload={"tour_id": "t1", "batch_id": "b", "tenant_id": "x"},
                    status="running", attempt=1, max_attempts=1, progress={}, cost_usd=0.0)
    ctx = JobContext(None, job, {"redis": None})
    ctx.progress = AsyncMock()
    with patch("api.routers.admin_pipeline._build_s1_progress_sink", return_value=None), \
            patch("api.routers.admin_pipeline._run_tour_safe", AsyncMock(side_effect=E.LLMAccessDenied("x"))):
        with pytest.raises(E.LLMAccessDenied) as ei:
            await sj.run(ctx)
    assert failure_outcome(ei.value)[1] is False
    cleared = [c for c in ctx.progress.await_args_list if "s1_writer_state" in c.kwargs]
    assert cleared, "the failed job must drop its resume state, as on any other final failure"


def test_rewrite_tour_reraises_a_denial_from_the_writer_node():
    """graph.generate_node re-raises; _rewrite_tour must not turn it into a 'failed' dict."""
    import asyncio

    from api.routers import v1_pipeline as vp

    class _Graph:
        async def astream(self, *_a, **_k):
            raise E.LLMAccessDenied("writer refused")
            yield  # pragma: no cover

    tour = {"name": "T", "country": "VN", "duration": "2 days"}
    with patch.object(vp, "build_graph", return_value=_Graph()):
        with pytest.raises(E.LLMAccessDenied):
            asyncio.run(vp._rewrite_tour(tour, 0, 1, brand_rules={}, seo={}))
