"""AA-862 — Cohere Embed v4 falls through acc2 -> acc3 through the stage route that migration 240
writes: one catalog row per account, chained with fallback_model_ids. Routing code is unchanged (only
the acc2 client now makes a single attempt); these tests pin the behaviour the migration relies on.
AA-998: acc3 is the only satellite account; a profile under the retired fallback account is never used."""
import io
import json
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from services.acp_shared import content_embedding as ce_mod
from shared.llm_client import embed as embed_mod
from shared.llm_client.catalog import CatalogModel
from shared.llm_client.role_config import StageConfig

PROFILE = "global.cohere.embed-v4:0"
BASE, ACC3 = "cohere-embed-v4", "cohere-embed-v4-acc3"
STALE = "cohere-embed-v4-stale"  # a row whose only profile is on the retired fallback account


def _row(key, account, **kw):
    base = dict(model_key=key, label=key, vendor="cohere", provider="bedrock", api_style="embed",
                bedrock_profile_ids={account: PROFILE}, callable_via=("embed",),
                price_in_per_mtok=0.12, price_out_per_mtok=0.0, enabled=True)
    base.update(kw)
    return CatalogModel(**base)


CATALOG = {
    BASE: _row(BASE, "acc2"),
    ACC3: _row(ACC3, "acc3"),
}


def _route(stage="f10_embed", fallbacks=(ACC3,)):
    return StageConfig(stage, "embed", "cohere", BASE, None, fallback_model_ids=tuple(fallbacks))


def _throttled():
    client = MagicMock()
    client.invoke_model.side_effect = ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "Too many requests"}}, "InvokeModel")
    return client


def _ok(vectors, tokens="1000"):
    client = MagicMock()
    client.invoke_model.return_value = {
        "body": io.BytesIO(json.dumps({"embeddings": {"float": vectors}}).encode()),
        "ResponseMetadata": {"HTTPHeaders": {"x-amzn-bedrock-input-token-count": tokens}},
    }
    return client


def _runtimes(by_account):
    calls = []

    def runtime_for(account):
        calls.append(account)
        return by_account[account]
    return runtime_for, calls


class TestFallThrough:
    def test_acc2_throttle_falls_through_to_acc3(self):
        acc3 = _ok([[0.5] * 4])
        runtime_for, calls = _runtimes({"acc2": _throttled(), "acc3": acc3})
        with patch.object(embed_mod, "get_stage_config_sync", return_value=_route()), \
             patch.object(embed_mod, "get_model_sync", side_effect=CATALOG.get), \
             patch.object(embed_mod, "_runtime_for", side_effect=runtime_for):
            resp = embed_mod.embed("f10_embed", ["q"], input_type="search_query", dimensions=4)
        assert calls == ["acc2", "acc3"]
        assert resp.account == "acc3" and resp.model_used == ACC3 and resp.fallback_used is True
        assert resp.vectors == [[0.5] * 4]
        assert resp.cost_usd == pytest.approx(0.00012)  # priced from the acc3 row (same price)
        assert acc3.invoke_model.call_args.kwargs["modelId"] == PROFILE

    def test_acc2_answers_first_when_it_is_not_throttled(self):
        runtime_for, calls = _runtimes({"acc2": _ok([[0.1] * 4]), "acc3": _ok([[0.9] * 4])})
        with patch.object(embed_mod, "get_stage_config_sync", return_value=_route()), \
             patch.object(embed_mod, "get_model_sync", side_effect=CATALOG.get), \
             patch.object(embed_mod, "_runtime_for", side_effect=runtime_for):
            resp = embed_mod.embed("a3_place_embed", ["t"], dimensions=4)
        assert calls == ["acc2"]
        assert resp.account == "acc2" and resp.model_used == BASE and resp.fallback_used is False

    def test_enabled_row_on_the_retired_account_is_never_called(self):
        # AA-998: an enabled row whose only profile is on acc1 (stale data) gets no account.
        catalog = {**CATALOG, STALE: _row(STALE, "acc1")}
        runtime_for, calls = _runtimes({"acc2": _throttled(), "acc3": _throttled()})
        with patch.object(embed_mod, "get_stage_config_sync", return_value=_route(fallbacks=(ACC3, STALE))), \
             patch.object(embed_mod, "get_model_sync", side_effect=catalog.get), \
             patch.object(embed_mod, "_runtime_for", side_effect=runtime_for), \
             pytest.raises(RuntimeError, match="cohere-embed-v4-stale: no Bedrock profile"):
            embed_mod.embed("f10_embed", ["t"])
        assert calls == ["acc2", "acc3"]

    def test_satellite_runtime_comes_from_the_satellite_session(self):
        from shared.llm_client import bedrock_satellite
        sentinel = object()
        with patch.object(bedrock_satellite, "get_satellite_client", return_value=sentinel) as m_sat:
            assert embed_mod._runtime_for("acc3") is sentinel
        m_sat.assert_called_once_with("bedrock-runtime", account="acc3")


class TestCallLog:
    def test_f10_embed_logs_the_acc3_fallback_as_a_satellite_call(self):
        runtime_for, _ = _runtimes({"acc2": _throttled(), "acc3": _ok([[0.3] * ce_mod.EMBEDDING_DIMENSIONS])})
        with patch.object(embed_mod, "get_stage_config_sync", return_value=_route()), \
             patch.object(embed_mod, "get_model_sync", side_effect=CATALOG.get), \
             patch.object(embed_mod, "_runtime_for", side_effect=runtime_for), \
             patch.object(ce_mod, "_pace_calls"), \
             patch.object(ce_mod, "record_call_sync") as m_log:
            assert ce_mod.compute_embedding("Some text.") == [0.3] * ce_mod.EMBEDDING_DIMENSIONS
        kwargs = m_log.call_args.kwargs
        assert kwargs["model"] == ACC3 and kwargs["account"] == "acc3"
        assert kwargs["fallback_used"] is True and kwargs["provider"] == "bedrock-satellite"


def test_acc2_client_makes_one_attempt_only(monkeypatch):
    """botocore reads the legacy `max_attempts` as retries (1 -> 2 attempts); a throttle must cost one
    acc2 call before the fall-through, so the client sets `total_max_attempts=1`."""
    monkeypatch.setattr(embed_mod, "_native_client", None)
    client = embed_mod._acc2_client()
    try:
        assert client.meta.config.retries == {"total_max_attempts": 1, "mode": "standard"}
    finally:
        monkeypatch.setattr(embed_mod, "_native_client", None)
