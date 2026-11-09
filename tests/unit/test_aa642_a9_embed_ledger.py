"""AA-642 A9 (gap OPS-02) — embedding calls on the ledger v2 like every other gateway call.

- A success carries connection_key, latency_ms, attempt_no, deployment_wire_id and pricing_mode, and the callers' rows
  take them through call_log.billed_fields (content_embedding and the place pipeline).
- A failed attempt gets the gateway's own 0 USD row: role embed, status refused (an account refusal, an open circuit)
  or error (an outage), the connection, latency and attempt number.
- A route model that is not tried (disabled in the catalog, no usable deployment) gets a refused row too.
No AWS, no DB: the background writes are captured.
"""
import io
import json
from unittest.mock import MagicMock, patch

import pytest

from shared.llm_client import call_log
from shared.llm_client import embed as embed_mod
from shared.llm_client.catalog import CatalogModel
from shared.llm_client.role_config import SAFE_DEFAULTS, StageConfig


def _model(key="cohere-embed-v4", **kw):
    base = dict(model_key=key, label=key, vendor="cohere", provider="bedrock", api_style="embed",
                bedrock_profile_ids={"acc2": f"us.{key}:0"}, callable_via=("embed",),
                price_in_per_mtok=0.12, price_out_per_mtok=0.0, enabled=True)
    base.update(kw)
    return CatalogModel(**base)


def _runtime(vectors):
    client = MagicMock()
    client.invoke_model.return_value = {
        "body": io.BytesIO(json.dumps({"embeddings": {"float": vectors}}).encode()),
        "ResponseMetadata": {"HTTPHeaders": {"x-amzn-bedrock-input-token-count": "10"}}}
    return client


def _refusing(message):
    client = MagicMock()
    client.invoke_model.side_effect = RuntimeError(message)
    return client


@pytest.fixture
def rows(monkeypatch):
    seen: list = []

    async def record_calls(rows):
        seen.extend(rows)
    monkeypatch.setattr(call_log, "record_calls", record_calls)
    yield seen


def _wait():
    call_log.wait_for_pending_writes()


def test_a_success_carries_the_attempt_facts_for_the_callers_row(rows):
    with patch.object(embed_mod, "get_stage_config_sync", return_value=SAFE_DEFAULTS["f10_embed"]), \
         patch.object(embed_mod, "get_model_sync", return_value=_model()), \
         patch.object(embed_mod, "_runtime_for", return_value=_runtime([[0.1, 0.2]])):
        resp = embed_mod.embed("f10_embed", ["a quiet lake"])
    _wait()
    assert rows == []                                       # a success is the caller's row
    assert resp.connection_key == "acc2" and resp.attempt_no == 1 and resp.latency_ms >= 0
    assert resp.deployment_wire_id == "us.cohere-embed-v4:0" and resp.pricing_mode == "catalog"
    fields = call_log.billed_fields(resp)
    assert fields["connection_key"] == "acc2" and fields["attempt_no"] == 1
    assert fields["latency_ms"] == resp.latency_ms and fields["deployment_wire_id"] == "us.cohere-embed-v4:0"
    assert fields["pricing_mode"] == "catalog"


def test_the_code_table_price_is_named_when_the_catalog_is_unreadable(rows):
    with patch.object(embed_mod, "get_stage_config_sync", return_value=SAFE_DEFAULTS["f10_embed"]), \
         patch.object(embed_mod, "get_model_sync", return_value=None), \
         patch.object(embed_mod, "_runtime_for", return_value=_runtime([[0.1]])):
        resp = embed_mod.embed("f10_embed", ["t"])
    assert resp.pricing_mode == "code_table"


def test_a_refused_then_answered_route_writes_one_refused_row_and_numbers_the_attempts(rows):
    cfg = StageConfig("f10_embed", "embed", "cohere", "embed-a", None, fallback_model_ids=("embed-b",))
    refused = _refusing("AccessDeniedException: Error 002 the account is not authorized")
    with patch.object(embed_mod, "get_stage_config_sync", return_value=cfg), \
         patch.object(embed_mod, "get_model_sync", side_effect=lambda k: _model(k)), \
         patch.object(embed_mod, "_runtime_for", side_effect=[refused, _runtime([[0.3]])]):
        resp = embed_mod.embed("f10_embed", ["t"])
    _wait()
    assert resp.model_used == "embed-b" and resp.attempt_no == 2 and resp.fallback_used is True
    assert len(rows) == 1
    row = rows[0]
    assert row["stage"] == "f10_embed" and row["role"] == "embed" and row["model"] == "embed-a"
    assert row["status"] == "refused" and row["error_class"] == "access_denied" and row["cost_usd"] == 0.0
    assert row["connection_key"] == "acc2" and row["attempt_no"] == 1 and row["latency_ms"] >= 0
    assert row["deployment_wire_id"] == "us.embed-a:0" and row["provider"] == "bedrock-native"
    assert row["quality_signal"]["gateway_attempt"] is True


def test_an_outage_is_an_error_row_and_the_route_still_raises(rows):
    with patch.object(embed_mod, "get_stage_config_sync", return_value=SAFE_DEFAULTS["f10_embed"]), \
         patch.object(embed_mod, "get_model_sync", return_value=_model()), \
         patch.object(embed_mod, "_runtime_for", return_value=_refusing("connection reset by peer")):
        with pytest.raises(RuntimeError, match="All models in the route failed"):
            embed_mod.embed("f10_embed", ["t"])
    _wait()
    assert [(r["status"], r["error_class"], r["attempt_no"], r["connection_key"]) for r in rows] == [
        ("error", "other", 1, "acc2")]


def test_a_route_model_that_is_not_tried_gets_a_refused_row(rows):
    cfg = StageConfig("f10_embed", "embed", "cohere", "embed-off", None, fallback_model_ids=("embed-b",))
    models = {"embed-off": _model("embed-off", enabled=False), "embed-b": _model("embed-b")}
    with patch.object(embed_mod, "get_stage_config_sync", return_value=cfg), \
         patch.object(embed_mod, "get_model_sync", side_effect=models.get), \
         patch.object(embed_mod, "_runtime_for", return_value=_runtime([[0.3]])):
        resp = embed_mod.embed("f10_embed", ["t"])
    _wait()
    assert resp.model_used == "embed-b" and resp.attempt_no == 1
    assert [(r["model"], r["status"], r["cost_usd"], r["latency_ms"], r["attempt_no"]) for r in rows] == [
        ("embed-off", "refused", 0.0, 0, None)]


def test_no_usable_deployment_is_a_refused_row(rows):
    with patch.object(embed_mod, "get_stage_config_sync", return_value=SAFE_DEFAULTS["f10_embed"]), \
         patch.object(embed_mod, "get_model_sync", return_value=_model(bedrock_profile_ids={})):
        with pytest.raises(RuntimeError, match="no Bedrock profile or usable deployment"):
            embed_mod.embed("f10_embed", ["t"])
    _wait()
    assert [(r["status"], r["role"], r["connection_key"]) for r in rows] == [("refused", "embed", None)]


def test_a_ledger_write_failure_never_breaks_the_route(monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("db down")
    monkeypatch.setattr(call_log, "gateway_failure_row", boom)
    cfg = StageConfig("f10_embed", "embed", "cohere", "embed-a", None, fallback_model_ids=("embed-b",))
    with patch.object(embed_mod, "get_stage_config_sync", return_value=cfg), \
         patch.object(embed_mod, "get_model_sync", side_effect=lambda k: _model(k)), \
         patch.object(embed_mod, "_runtime_for", side_effect=[_refusing("throttled"), _runtime([[0.3]])]):
        assert embed_mod.embed("f10_embed", ["t"]).model_used == "embed-b"


def test_the_place_pipeline_row_carries_the_attempt_facts(monkeypatch):
    """services.places.llm.embed_texts passes **billed_fields(resp): the new facts reach its row."""
    import asyncio

    from services.places import llm as places_llm

    resp = embed_mod.EmbedResponse(vectors=[[0.1]], model_used="cohere-embed-v4", account="acc2", input_tokens=3,
                                   tokens_estimated=False, cost_usd=0.0, connection_key="acc2", latency_ms=42,
                                   attempt_no=1, deployment_wire_id="us.cohere-embed-v4:0", pricing_mode="catalog")
    seen = {}

    async def fake_record(pool, **kw):
        seen.update(kw)
    monkeypatch.setattr(places_llm, "record_call_with_pool", fake_record)
    monkeypatch.setattr(embed_mod, "embed", lambda *a, **kw: resp)
    asyncio.run(places_llm.embed_texts(object(), ["a harbour"]))
    assert seen["connection_key"] == "acc2" and seen["latency_ms"] == 42 and seen["attempt_no"] == 1
    assert seen["deployment_wire_id"] == "us.cohere-embed-v4:0" and seen["role"] == "embed"
