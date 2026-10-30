"""AA-1002 (gateway slice S6, review fix) — a successful gateway call writes its cache split, pricing basis and
vendor connection to shared.llm_call_log, and Converse caches a long Claude system prompt.

- ``call_log.billed_fields(resp)``: the four billing facts every call site passes next to ``model=resp.model_used``
  (guard: an AST scan of the repo fails on a site that does not); read defensively (test doubles give None).
- End to end through a real caller (``services.places.llm.make_generate``): a Converse success with cache tokens,
  an Anthropic API success and an OpenAI success each write the S6 INSERT with the cache split and pricing_mode;
  the vendor rows store their connection key as the account, Bedrock rows keep the AWS account.
- The connection key never lands in ``satellite_account`` (copied to generated_content, CHECK acc1 / acc3 / NULL).
- Converse ``cachePoint``: only Claude profiles, only at or above the model's minimum, last in the system list,
  dropped by the switch, retried once without it on a ValidationException.
- The admin names a vendor connection by its vendor (backend and frontend maps agree).
No AWS, no vendor, no DB.
"""
import ast
import asyncio
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.llm_client import call_log
from shared.llm_client import client as client_mod
from shared.llm_client.adapters import anthropic_messages
from shared.llm_client.catalog import CatalogModel
from shared.llm_client.client import OPENAI_CONNECTION_KEY, BilledUnusableReply, LLMClient
from shared.llm_client.deployments import ModelDeployment
from shared.llm_client.models import LLMRequest, LLMResponse
from shared.llm_client.vendor_route import VendorDeployment
from tests.unit.aa1002_fake_anthropic import anthropic_connection

ROOT = Path(__file__).resolve().parents[2]
LOG_FUNCS = {"record_call", "record_call_sync", "record_call_with_pool"}

SONNET55 = CatalogModel(model_key="sonnet-5-5", label="Sonnet 5.5", vendor="anthropic", provider="bedrock",
                        api_style="converse", bedrock_profile_ids={"acc3": "global.anthropic.claude-sonnet-5-5"},
                        callable_via=("llm_client",), supports_temperature=False, max_output_tokens=128000,
                        price_in_per_mtok=2.0, price_out_per_mtok=10.0, price_cache_read_per_mtok=0.2,
                        price_cache_write_per_mtok=2.5, enabled=True)
HAIKU45 = CatalogModel(model_key="haiku-4-5", label="Haiku 4.5", vendor="anthropic", provider="bedrock",
                       api_style="converse",
                       bedrock_profile_ids={"acc3": "global.anthropic.claude-haiku-4-5-20251001-v1:0"},
                       callable_via=("llm_client",), price_in_per_mtok=1.0, price_out_per_mtok=5.0, enabled=True)
LUNA = CatalogModel(model_key="gpt-5.6-luna", label="l", vendor="openai", provider="bedrock", api_style="converse",
                    bedrock_profile_ids={"acc3": "global.openai.gpt-5.6-luna"}, callable_via=("llm_client",),
                    supports_temperature=False, price_in_per_mtok=0.2, price_out_per_mtok=1.2, enabled=True)
LUNA_OPENAI = CatalogModel(model_key="gpt-6-luna-openai", label="x", vendor="openai", provider="openai",
                           api_style="openai_chat", bedrock_profile_ids={}, callable_via=("llm_client",),
                           price_in_per_mtok=0.1, price_out_per_mtok=0.5, price_cache_read_per_mtok=0.01,
                           enabled=True)
RATES = {"sonnet-5-5": {"in": 0.002, "out": 0.010, "cache_read": 0.0002, "cache_write": 0.0025},
         "haiku-4-5": {"in": 0.001, "out": 0.005, "cache_read": 0.0001, "cache_write": 0.00125},
         "gpt-5.6-luna": {"in": 0.0002, "out": 0.0012, "cache_read": 0.00002, "cache_write": 0.00025},
         "gpt-6-luna-openai": {"in": 0.0001, "out": 0.0005, "cache_read": 0.00001, "cache_write": 0.000125}}
SCHEMA = {"name": "verdict", "schema": {"type": "object", "required": ["ok"],
                                        "properties": {"ok": {"type": "boolean"}}}}
LONG = "Brand rules. " * 400            # ~5,200 characters: ~1,300 tokens, above Sonnet 5.5's 512 minimum


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv(client_mod.CONVERSE_CACHE_POINT_ENV, raising=False)
    monkeypatch.setattr("shared.llm_client.pricing._catalog_rates",
                        lambda m: RATES.get(__import__("shared.llm_client.pricing", fromlist=["x"])._catalog_key(m)))
    monkeypatch.setattr("shared.llm_client.pricing.deployment_for", lambda *a, **k: None)
    monkeypatch.setattr("shared.llm_client.client.stream_sink.delta_callback", lambda stage: None)
    monkeypatch.setattr(call_log, "_s6_missing_at", 0.0)
    client_mod._CACHE_POINT_REJECTED.clear()
    client_mod._FORCED_TOOL_REJECTED.clear()
    yield
    client_mod._CACHE_POINT_REJECTED.clear()
    client_mod._FORCED_TOOL_REJECTED.clear()


def _client() -> LLMClient:
    with patch("shared.llm_client.client.boto3"), patch("shared.llm_client.client.openai"):
        c = LLMClient()
    c._openai = MagicMock()
    return c


def _reply(text="hello", usage=None, stop="end_turn"):
    return {"output": {"message": {"content": [{"text": text}]}},
            "usage": usage or {"inputTokens": 100, "outputTokens": 20}, "stopReason": stop}


def _converse(model, request, rt, account="acc3"):
    with patch("shared.llm_client.bedrock_satellite.get_satellite_client", return_value=rt):
        return _client()._call_bedrock_converse(request, model, account)


def _openai(usage_cached=8000, content="ok"):
    c = _client()
    usage = MagicMock(prompt_tokens=10_000, completion_tokens=1000)
    usage.prompt_tokens_details = MagicMock(cached_tokens=usage_cached)
    c._openai.chat.completions.create.return_value = MagicMock(
        choices=[MagicMock(message=MagicMock(content=content), finish_reason="stop")], usage=usage)
    return c


def _anthropic_result(**kw):
    base = dict(text="hi", provider_model="claude-sonnet-5-5-20261015", stop_reason="end_turn", stop_details=None,
                input_tokens=100, output_tokens=50, cache_read_tokens=1000, cache_write_tokens=200,
                structured=False, effort="high")
    base.update(kw)
    return anthropic_messages.MessagesResult(**base)


def _vendor():
    dep = ModelDeployment("sonnet-5-5", "anthropic-direct", "claude-sonnet-5-5", 100, True, "seed")
    return VendorDeployment(dep, anthropic_connection())


class _Pool:
    """An asyncpg pool double whose connection records every execute()."""

    def __init__(self):
        self.conn = MagicMock(execute=AsyncMock())

    def acquire(self):
        pool = self

        class _Ctx:
            async def __aenter__(self):
                return pool.conn

            async def __aexit__(self, *a):
                return False
        return _Ctx()


def _logged_row(resp: LLMResponse) -> tuple[str, tuple]:
    """(sql, args) of the llm_call_log INSERT a real caller (services.places.llm.make_generate) writes for resp."""
    from services.places import llm as places_llm
    pool = _Pool()
    with patch.object(places_llm, "_generate_sync", return_value=resp):
        asyncio.run(places_llm.make_generate(pool, places_llm.DAYS_STAGE, 4096)("sys", "user"))
    sql, *args = pool.conn.execute.call_args.args
    return sql, tuple(args)


# ── billed_fields ─────────────────────────────────────────────────────────────────────────────────────────────────

def test_billed_fields_reads_the_response_and_ignores_test_doubles():
    resp = LLMResponse(content="", model_used="m", provider="openai", cache_read_tokens=3, cache_write_tokens=4,
                       pricing_mode="catalog", connection_key="openai-direct")
    assert call_log.billed_fields(resp) == {"cache_read_tokens": 3, "cache_write_tokens": 4,
                                            "pricing_mode": "catalog", "connection_key": "openai-direct"}
    assert call_log.billed_fields(MagicMock()) == dict.fromkeys(call_log.billed_fields(resp))
    assert call_log.billed_fields(object()) == dict.fromkeys(call_log.billed_fields(resp))


def test_row_account_is_the_aws_account_else_the_connection():
    assert call_log._row_account("acc3", "openai-direct") == "acc3"
    assert call_log._row_account(None, "openai-direct") == "openai-direct"
    assert call_log._row_account(None, None) is None and call_log._row_account(None, MagicMock()) is None


def _log_sites_missing_billed_fields() -> list[str]:
    missing = []
    for path in sorted(ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT)
        if rel.parts[0] in ("tests", "node_modules", "frontend") or any(p.startswith(".") for p in rel.parts):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
            model = next((k.value for k in node.keywords if k.arg == "model"), None)
            if name not in LOG_FUNCS or not (isinstance(model, ast.Attribute) and model.attr == "model_used"):
                continue
            want = ast.unparse(model.value)
            ok = any(k.arg is None and isinstance(k.value, ast.Call)
                     and ast.unparse(k.value.func).endswith("billed_fields")
                     and [ast.unparse(a) for a in k.value.args] == [want] for k in node.keywords)
            if not ok:
                missing.append(f"{rel}:{node.lineno}")
    return missing


def test_every_site_that_logs_a_reply_passes_its_billed_fields():
    """A row written with ``model=<reply>.model_used`` must also pass ``**billed_fields(<reply>)``, or its cache
    split, pricing basis and vendor connection are lost (the S6 review finding)."""
    assert _log_sites_missing_billed_fields() == []


def test_the_guard_sees_the_call_sites():
    src = (ROOT / "services" / "places" / "llm.py").read_text(encoding="utf-8")
    assert src.count("**billed_fields(resp)") == 2


# ── end to end through a real caller ──────────────────────────────────────────────────────────────────────────────

def test_a_converse_success_logs_its_cache_split():
    rt = MagicMock()
    rt.converse.return_value = _reply(usage={"inputTokens": 120, "outputTokens": 30, "cacheReadInputTokens": 4000,
                                             "cacheWriteInputTokens": 900})
    resp = _converse(SONNET55, LLMRequest(system_prompt=LONG, user_prompt="tour"), rt)
    assert (resp.cache_read_tokens, resp.cache_write_tokens, resp.pricing_mode) == (4000, 900, "catalog")
    assert resp.connection_key is None and resp.satellite_account == "acc3"
    sql, args = _logged_row(resp)
    assert sql == call_log._INSERT_S6_SQL
    assert args[11] == "acc3" and args[13] == "bedrock-satellite"            # account, provider
    assert args[-3:] == (4000, 900, "catalog")


def test_an_anthropic_api_success_logs_its_cache_split_and_connection():
    with patch.object(anthropic_messages, "send", return_value=_anthropic_result()):
        resp = _client()._call_anthropic_messages(LLMRequest(system_prompt="s", user_prompt="u"), SONNET55,
                                                  _vendor())
    assert resp.connection_key == "anthropic-direct" and resp.satellite_account is None
    sql, args = _logged_row(resp)
    assert sql == call_log._INSERT_S6_SQL
    assert (args[3], args[11], args[13]) == ("sonnet-5-5", "anthropic-direct", "anthropic")
    assert args[-3:] == (1000, 200, resp.pricing_mode) and resp.pricing_mode == "catalog"


def test_an_anthropic_refusal_is_billed_on_its_connection():
    with patch.object(anthropic_messages, "send", return_value=_anthropic_result(stop_reason="refusal")), \
            pytest.raises(BilledUnusableReply) as exc:
        _client()._call_anthropic_messages(LLMRequest(system_prompt="s", user_prompt="u"), SONNET55, _vendor())
    (billed,) = exc.value.billed_usage
    assert billed["account"] == "anthropic-direct"
    assert (billed["cache_read_tokens"], billed["cache_write_tokens"]) == (1000, 200)
    (row,) = call_log.billed_unusable_rows(stage="s", role="judge", billed=[billed], error="refused")
    assert row["account"] == "anthropic-direct" and row["cache_read_tokens"] == 1000


def test_an_openai_success_logs_the_connection_as_its_account():
    c = _openai()
    with patch("shared.llm_client.client.get_model_sync", return_value=LUNA_OPENAI):
        resp = c._call_openai(LLMRequest(system_prompt="s", user_prompt="u"), model="gpt-6-luna-openai",
                              catalog_model=LUNA_OPENAI)
    assert resp.connection_key == OPENAI_CONNECTION_KEY and resp.satellite_account is None
    sql, args = _logged_row(resp)
    assert sql == call_log._INSERT_S6_SQL
    assert (args[11], args[13]) == ("openai-direct", "openai")
    assert args[-3:] == (8000, 0, "catalog")


def test_an_openai_billed_failure_carries_the_connection_and_the_cache():
    c = _openai(content="not json")
    with patch("shared.llm_client.client.get_model_sync", return_value=LUNA_OPENAI), \
            pytest.raises(BilledUnusableReply) as exc:
        c._call_openai(LLMRequest(system_prompt="s", user_prompt="u", json_schema=SCHEMA),
                       model="gpt-6-luna-openai", catalog_model=LUNA_OPENAI)
    (billed,) = exc.value.billed_usage
    assert billed["account"] == OPENAI_CONNECTION_KEY and billed["cache_read_tokens"] == 8000
    assert billed["pricing_mode"] == "catalog"


def test_a_converse_billed_failure_keeps_the_cache_split():
    rt = MagicMock()
    rt.converse.return_value = _reply(text="", usage={"inputTokens": 10, "outputTokens": 0,
                                                      "cacheReadInputTokens": 700})
    with pytest.raises(BilledUnusableReply) as exc:
        _converse(SONNET55, LLMRequest(system_prompt="s", user_prompt="u"), rt)
    (billed,) = exc.value.billed_usage
    assert billed["account"] == "acc3" and billed["cache_read_tokens"] == 700


def test_billed_record_names_the_vendor_connection_and_keeps_the_aws_account():
    vendor = LLMResponse(content="", model_used="gpt-6-luna-openai", provider="openai",
                         connection_key="openai-direct")
    bedrock = LLMResponse(content="", model_used="satellite-sonnet-5-5", provider="bedrock-satellite",
                          satellite_account="acc3")
    assert call_log.billed_record(vendor)["account"] == "openai-direct"
    assert call_log.billed_record(bedrock)["account"] == "acc3"


def test_record_calls_resolves_the_connection_key_of_a_row():
    args = call_log._insert_args({"stage": "t2_judge", "role": "judge", "model": "gpt-6-luna-openai",
                                  "connection_key": "openai-direct"})
    assert args[11] == "openai-direct"
    assert call_log._insert_args({"stage": "s", "role": "judge", "model": "m", "account": "acc3",
                                  "connection_key": "openai-direct"})[11] == "acc3"


def test_judge_runs_carry_the_billed_fields():
    from services.content_generation import brand_fit as bf
    assert set(call_log.billed_fields(LLMResponse(content="", model_used="m", provider="p"))) <= \
        set(bf.RUN_BILLING_KEYS)
    assert set(bf.RUN_BILLING_KEYS) <= set(call_log._ROW_FIELDS)
    r = bf.BrandFitResult(brand_fit_score=8, cross_brand_distinct=8, mission_present=True, feedback="",
                          judge_score=8, model_used="gpt-6-luna-openai", cost_usd=0.01, input_tokens=10,
                          output_tokens=2, stop_reason="stop", account=None, fallback_used=False,
                          cache_read_tokens=6, pricing_mode="catalog", connection_key="openai-direct")
    billing = bf.run_billing(r)
    assert (billing["cache_read_tokens"], billing["connection_key"]) == (6, "openai-direct")
    agg = bf.aggregate_runs([r, r], mission_absent_cap=None)
    assert agg.cache_read_tokens == 12 and agg.connection_key == "openai-direct"


def test_the_t10_judge_logs_the_billed_fields(monkeypatch):
    from services.acp_content_writing import quality_gates as qg
    seen = {}
    monkeypatch.setattr(qg, "record_call_sync", lambda **kw: seen.update(kw))
    qg._log_t10_judge_call({"model_used": "gpt-6-luna-openai", "input_tokens": 10, "output_tokens": 2,
                            "cost_usd": 0.001, "cache_read_tokens": 5, "connection_key": "openai-direct",
                            "pricing_mode": "catalog"}, gate="f9", passed=True, extra={})
    assert (seen["cache_read_tokens"], seen["connection_key"], seen["pricing_mode"]) == (5, "openai-direct",
                                                                                         "catalog")


# ── Converse cachePoint ───────────────────────────────────────────────────────────────────────────────────────────

def _sent_system(model, system, rt=None, **kw):
    if rt is None:
        rt = MagicMock()
        rt.converse.return_value = _reply()
    _converse(model, LLMRequest(system_prompt=system, user_prompt="tour", **kw), rt)
    return rt.converse.call_args.kwargs["system"]


def test_a_long_claude_system_prompt_ends_with_a_cache_point():
    assert _sent_system(SONNET55, LONG) == [{"text": LONG}, {"cachePoint": {"type": "default"}}]


def test_a_short_prompt_is_sent_as_before():
    assert _sent_system(SONNET55, "sys") == [{"text": "sys"}]
    # Haiku 4.5 needs 4096 tokens: the same ~1,300-token prompt is below it.
    assert _sent_system(HAIKU45, LONG) == [{"text": LONG}]


def test_non_claude_models_never_get_a_cache_point():
    assert _sent_system(LUNA, LONG * 4) == [{"text": LONG * 4}]       # Bedrock caches them on its own


def test_the_switch_turns_it_off(monkeypatch):
    monkeypatch.setenv(client_mod.CONVERSE_CACHE_POINT_ENV, "off")
    assert _sent_system(SONNET55, LONG) == [{"text": LONG}]


def test_the_auto_tool_instruction_stays_inside_the_cached_prefix():
    rt = MagicMock()
    rt.converse.return_value = {"output": {"message": {"content": [
        {"toolUse": {"toolUseId": "t1", "name": "verdict", "input": {"ok": True}}}]}},
        "usage": {"inputTokens": 10, "outputTokens": 5}, "stopReason": "tool_use"}
    system = _sent_system(SONNET55, LONG, rt=rt, json_schema=SCHEMA)
    assert system[0] == {"text": LONG} and system[-1] == {"cachePoint": {"type": "default"}}
    assert "verdict" in system[1]["text"] and len(system) == 3
    # a runtime forced-tool rejection (model not in the marker list) also inserts before the point
    kwargs = {"system": [{"text": LONG}, {"cachePoint": {"type": "default"}}],
              "toolConfig": {"tools": [], "toolChoice": {"tool": {"name": "verdict"}}}}
    client_mod._use_auto_tool_choice(kwargs, SCHEMA)
    assert kwargs["system"][-1] == {"cachePoint": {"type": "default"}} and "verdict" in kwargs["system"][1]["text"]


def test_a_rejected_cache_point_is_retried_once_without_it_and_remembered():
    rt = MagicMock()
    err = type("ValidationException", (Exception,), {})(
        "An error occurred (ValidationException) when calling the Converse operation: cachePoint is not supported "
        "for this model")
    rt.converse.side_effect = [err, _reply()]
    resp = _converse(SONNET55, LLMRequest(system_prompt=LONG, user_prompt="tour"), rt)
    assert resp.content == "hello" and rt.converse.call_count == 2
    assert rt.converse.call_args_list[1].kwargs["system"] == [{"text": LONG}]
    assert "sonnet-5-5" in client_mod._CACHE_POINT_REJECTED
    assert _sent_system(SONNET55, LONG) == [{"text": LONG}]              # not sent again in this process


def test_an_unrelated_validation_error_is_not_retried():
    rt = MagicMock()
    rt.converse.side_effect = type("ValidationException", (Exception,), {})("ValidationException: bad maxTokens")
    with pytest.raises(Exception, match="maxTokens"):
        _converse(SONNET55, LLMRequest(system_prompt=LONG, user_prompt="tour"), rt)
    assert rt.converse.call_count == 1


def test_the_stream_path_sends_the_cache_point_too(monkeypatch):
    rt = MagicMock()
    rt.converse_stream.return_value = {"stream": [
        {"contentBlockDelta": {"delta": {"text": "hi"}}}, {"messageStop": {"stopReason": "end_turn"}},
        {"metadata": {"usage": {"inputTokens": 5, "outputTokens": 1, "cacheReadInputTokens": 1300}}}]}
    monkeypatch.setattr("shared.llm_client.client.stream_sink.delta_callback", lambda stage: (lambda t: None))
    resp = _converse(SONNET55, LLMRequest(system_prompt=LONG, user_prompt="tour"), rt)
    assert rt.converse_stream.call_args.kwargs["system"][-1] == {"cachePoint": {"type": "default"}}
    assert resp.cache_read_tokens == 1300


@pytest.mark.parametrize("profile, minimum", [
    ("global.anthropic.claude-sonnet-5-5", 512), ("global.anthropic.claude-haiku-5-5", 512),
    ("global.anthropic.claude-opus-5-5", 512), ("global.anthropic.claude-sonnet-5", 1024),
    ("global.anthropic.claude-sonnet-4-6", 1024), ("global.anthropic.claude-haiku-4-5-20251001-v1:0", 4096),
    ("us.anthropic.claude-opus-4-7", 2048), ("global.anthropic.claude-new-family", 4096),
])
def test_cache_minimums_by_family(profile, minimum):
    assert client_mod._cache_point_min_tokens(profile) == minimum


# ── the admin label ───────────────────────────────────────────────────────────────────────────────────────────────

def test_a_vendor_connection_is_named_by_its_vendor_not_retired():
    from shared.aws_client import accounts
    assert accounts.account_name("openai-direct") == "OpenAI API"
    assert accounts.account_label("anthropic-direct") == "Anthropic API"
    assert accounts.account_name("acc9") == accounts.RETIRED_ACCOUNT_LABEL
    assert OPENAI_CONNECTION_KEY in accounts.VENDOR_CONNECTION_LABELS
    assert anthropic_connection().connection_key in accounts.VENDOR_CONNECTION_LABELS


def test_backend_and_frontend_vendor_labels_agree():
    from shared.aws_client import accounts
    ts = (ROOT / "frontend" / "lib" / "awsAccounts.ts").read_text(encoding="utf-8")
    block = re.search(r"VENDOR_CONNECTION_LABELS[^=]*=\s*\{(.*?)\};", ts, re.S).group(1)
    assert dict(re.findall(r'"([^"]+)":\s*"([^"]+)"', block)) == accounts.VENDOR_CONNECTION_LABELS
