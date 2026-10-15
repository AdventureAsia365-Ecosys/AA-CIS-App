"""AA-843 — the legacy haiku/sonnet chain starts at the satellite; acc2-native only behind a flag.

acc2 is a channel-program account, so every acc2-native Anthropic call failed (ValidationException)
before the chain reached acc3. By default `_legacy_chain` now never calls `_call_bedrock`;
LLM_LEGACY_ACC2_NATIVE=on restores the old acc2-first order. Catalog routes, the T3 GPT-4.1 gate and
shadows are unchanged."""
from unittest.mock import MagicMock, patch

import pytest

from shared.llm_client import client as cl
from shared.llm_client.client import LLMClient, legacy_acc2_native_enabled
from shared.llm_client.models import LLMRequest, LLMResponse
from shared.llm_client.pricing import BEDROCK_HAIKU, BEDROCK_SONNET
from shared.llm_client.role_config import StageConfig

FLAG = "LLM_LEGACY_ACC2_NATIVE"
ACC2_REFUSAL = RuntimeError("ValidationException: Access to this model is not available for channel program accounts")


@pytest.fixture(autouse=True)
def _flag_off(monkeypatch):
    monkeypatch.delenv(FLAG, raising=False)


def _client():
    c = LLMClient.__new__(LLMClient)
    c._bedrock = MagicMock()
    c._openai = MagicMock()
    return c


def _sat_resp(model, account):
    return LLMResponse(content="ok", model_used=f"satellite-{model}", provider="bedrock-satellite",
                       input_tokens=10, output_tokens=5, cost_usd=0.001)


class _Recorder:
    """Records every provider attempt in order; `fail` lists (kind, model, account) that raise."""

    def __init__(self, fail=()):
        self.calls = []
        self.fail = set(fail)

    def native(self, request, model, use_cache=False):
        self.calls.append(("native", model, "acc2"))
        if ("native", model, "acc2") in self.fail:
            raise ACC2_REFUSAL
        return LLMResponse(content="native", model_used=model, provider="bedrock")

    def satellite(self, request, model, account="acc1"):
        self.calls.append(("sat", model, account))
        if ("sat", model, account) in self.fail:
            raise RuntimeError("Satellite Bedrock failed: boom")
        return _sat_resp(model, account)


def _run(tier, rec, stage_cfg=None, stage=None):
    c = _client()
    with patch.object(c, "_call_bedrock", side_effect=rec.native), \
            patch.object(c, "_call_bedrock_satellite", side_effect=rec.satellite), \
            patch.object(cl, "get_stage_config_sync", return_value=stage_cfg), \
            patch.object(cl, "openai_platform_allowed", return_value=False):
        return c.generate(LLMRequest(system_prompt="s", user_prompt="u", model_tier=tier, stage=stage))


# ── the flag ─────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("value,expected", [
    (None, False), ("", False), ("off", False), ("0", False), ("false", False),
    ("on", True), ("ON", True), (" true ", True), ("1", True), ("yes", True),
])
def test_flag_parsing(monkeypatch, value, expected):
    if value is not None:
        monkeypatch.setenv(FLAG, value)
    assert legacy_acc2_native_enabled() is expected


# ── default: no acc2-native attempt ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("tier,model", [("haiku", BEDROCK_HAIKU), ("sonnet", BEDROCK_SONNET)])
def test_default_starts_at_primary_satellite_without_acc2(tier, model):
    rec = _Recorder()
    resp = _run(tier, rec)
    assert rec.calls == [("sat", model, "acc3")]
    assert resp.satellite_account == "acc3"
    assert resp.fallback_used is False


def test_default_account_route_acc1_goes_first():
    cfg = StageConfig(stage="t9_write", role="writer", provider="bedrock", model_id="haiku", account_route="acc1")
    rec = _Recorder()
    resp = _run(None, rec, stage_cfg=cfg, stage="t9_write")
    assert rec.calls == [("sat", BEDROCK_HAIKU, "acc1")]
    assert resp.satellite_account == "acc1"


def test_default_haiku_primary_down_uses_other_satellite():
    rec = _Recorder(fail={("sat", BEDROCK_HAIKU, "acc3")})
    resp = _run("haiku", rec)
    assert rec.calls == [("sat", BEDROCK_HAIKU, "acc3"), ("sat", BEDROCK_HAIKU, "acc1")]
    assert resp.satellite_account == "acc1"
    assert resp.fallback_used is False


def test_default_sonnet_drops_to_haiku_satellites_unchanged():
    rec = _Recorder(fail={("sat", BEDROCK_SONNET, "acc3"), ("sat", BEDROCK_SONNET, "acc1")})
    resp = _run("sonnet", rec)
    assert rec.calls == [("sat", BEDROCK_SONNET, "acc3"), ("sat", BEDROCK_SONNET, "acc1"),
                         ("sat", BEDROCK_HAIKU, "acc3")]
    assert resp.satellite_account == "acc3"
    assert resp.fallback_used is True  # Sonnet was intended, Haiku served it


def test_default_all_satellites_down_raises_without_acc2_or_openai():
    rec = _Recorder(fail={("sat", m, a) for m in (BEDROCK_SONNET, BEDROCK_HAIKU) for a in ("acc3", "acc1")})
    c = _client()
    with patch.object(c, "_call_bedrock", side_effect=rec.native), \
            patch.object(c, "_call_bedrock_satellite", side_effect=rec.satellite), \
            patch.object(cl, "get_stage_config_sync", return_value=None), \
            pytest.raises(RuntimeError, match="GPT-4.1 retired"):
        c.generate(LLMRequest(system_prompt="s", user_prompt="u", model_tier="sonnet"))
    assert all(kind == "sat" for kind, _, _ in rec.calls)
    assert len(rec.calls) == 4
    c._openai.chat.completions.create.assert_not_called()


def test_default_logs_no_acc2_failure_events():
    rec = _Recorder()
    with patch.object(cl, "logger") as log:
        _run("sonnet", rec)
        _run("haiku", rec)
    events = [c.args[0] for c in log.warning.call_args_list + log.info.call_args_list]
    assert "t1_failed_trying_t2" not in events
    assert "t2_failed_trying_t2_5a" not in events
    assert "t1_5a_satellite_used" in events and "t2_5a_satellite_used" in events
    reasons = {c.args[0]: c.kwargs.get("reason") for c in log.info.call_args_list}
    assert reasons["t2_5a_satellite_used"] == "acc2 native off (AA-843)"


# ── flag on: the old order comes back ────────────────────────────────────────────────────────

def test_flag_restores_acc2_first_order(monkeypatch):
    monkeypatch.setenv(FLAG, "on")
    rec = _Recorder(fail={("native", BEDROCK_SONNET, "acc2"), ("sat", BEDROCK_SONNET, "acc3"),
                          ("sat", BEDROCK_SONNET, "acc1"), ("native", BEDROCK_HAIKU, "acc2")})
    with patch.object(cl, "logger") as log:
        resp = _run("sonnet", rec)
    assert rec.calls == [("native", BEDROCK_SONNET, "acc2"), ("sat", BEDROCK_SONNET, "acc3"),
                         ("sat", BEDROCK_SONNET, "acc1"), ("native", BEDROCK_HAIKU, "acc2"),
                         ("sat", BEDROCK_HAIKU, "acc3")]
    assert resp.fallback_used is True and resp.satellite_account == "acc3"
    events = [c.args[0] for c in log.warning.call_args_list]
    assert "t1_failed_trying_t2" in events and "t2_failed_trying_t2_5a" in events


def test_flag_on_acc2_success_is_returned_as_before(monkeypatch):
    monkeypatch.setenv(FLAG, "on")
    rec = _Recorder()
    resp = _run("haiku", rec)
    assert rec.calls == [("native", BEDROCK_HAIKU, "acc2")]
    assert resp.provider == "bedrock" and resp.satellite_account is None


# ── untouched paths ──────────────────────────────────────────────────────────────────────────

def test_catalog_route_order_unchanged_and_legacy_fallback_skips_acc2():
    # Route sonnet-5 (catalog) -> haiku (legacy): a catalog failure moves on to haiku, which now
    # starts at the satellite; position 1 marks fallback_used.
    cfg = StageConfig(stage="t9_write", role="writer", provider="bedrock", model_id="sonnet-5",
                      account_route=None, fallback_model_ids=("haiku",))
    rec = _Recorder()
    c = _client()
    enabled = MagicMock(enabled=True)
    with patch.object(c, "_call_bedrock", side_effect=rec.native), \
            patch.object(c, "_call_bedrock_satellite", side_effect=rec.satellite), \
            patch.object(c, "_call_catalog_model", side_effect=RuntimeError("converse down")) as cat, \
            patch.object(cl, "get_model_sync", return_value=enabled), \
            patch.object(cl, "get_stage_config_sync", return_value=cfg):
        resp = c.generate(LLMRequest(system_prompt="s", user_prompt="u", stage="t9_write"))
    cat.assert_called_once()
    assert cat.call_args.args[1:] == ("sonnet-5", "acc3")
    assert rec.calls == [("sat", BEDROCK_HAIKU, "acc3")]
    assert resp.fallback_used is True


def test_shadow_runs_on_the_satellite_and_is_recorded():
    cfg = StageConfig(stage="t9_write", role="writer", provider="bedrock", model_id="sonnet",
                      account_route=None, shadow_model_id="haiku", shadow_sample_pct=100)
    rec = _Recorder()
    c = _client()
    primary = _sat_resp("sonnet-4-6", "acc3")
    with patch.object(c, "_call_bedrock", side_effect=rec.native), \
            patch.object(c, "_call_bedrock_satellite", side_effect=rec.satellite), \
            patch("shared.llm_client.shadow.record_shadow_sync") as rec_shadow:
        c._run_shadow_bound(LLMRequest(system_prompt="s", user_prompt="u", stage="t9_write"),
                            cfg, primary, "acc3", "acc1")
    assert rec.calls == [("sat", BEDROCK_HAIKU, "acc3")]
    kw = rec_shadow.call_args.kwargs
    assert kw["shadow_model"] == "haiku" and kw["error"] is None and kw["shadow"].content == "ok"


def test_generate_starts_shadow_with_the_same_accounts():
    cfg = StageConfig(stage="t9_write", role="writer", provider="bedrock", model_id="sonnet",
                      account_route="acc1", shadow_model_id="haiku", shadow_sample_pct=100)
    rec = _Recorder()
    c = _client()
    with patch.object(c, "_call_bedrock", side_effect=rec.native), \
            patch.object(c, "_call_bedrock_satellite", side_effect=rec.satellite), \
            patch.object(c, "_start_shadow") as start, \
            patch.object(cl, "get_stage_config_sync", return_value=cfg):
        c.generate(LLMRequest(system_prompt="s", user_prompt="u", stage="t9_write"))
    assert rec.calls == [("sat", BEDROCK_SONNET, "acc1")]
    assert start.call_args.args[3:] == ("acc1", "acc3")


def test_satellite_stream_restarts_once_and_feeds_deltas():
    """Live writing progress: by default one provider attempt (one restart), deltas from the satellite."""
    c = _client()
    seen = []
    result = MagicMock(text="Hello", usage={"input_tokens": 3, "output_tokens": 2}, stop_reason="end_turn",
                       model_used="haiku-4-5", latency_ms=5.0)

    def fake_invoke(prompt, **kw):
        for piece in ("Hel", "lo"):
            kw["on_delta"](piece)
        return result

    with patch("shared.llm_client.bedrock_satellite.invoke_claude", side_effect=fake_invoke) as inv, \
            patch.object(cl.stream_sink, "delta_callback", return_value=seen.append), \
            patch.object(cl.stream_sink, "emit_restart") as restart, \
            patch.object(cl, "get_stage_config_sync", return_value=None):
        resp = c.generate(LLMRequest(system_prompt="s", user_prompt="u", stage="t9_write", model_tier="haiku"))
    assert seen == ["Hel", "lo"] and resp.content == "Hello"
    assert inv.call_args.kwargs["account"] == "acc3"
    assert restart.call_count == 1
    c._bedrock.invoke_model_with_response_stream.assert_not_called()
