"""S235 (Nghiệp, 14/10/2026) — Bedrock first; the OpenAI platform API only as the LAST-RESORT fallback.

Guards:
1. In SAFE_DEFAULTS a model whose catalog provider is 'openai' (OpenAI platform API) appears only as
   the LAST fallback of a route — never as the primary or the shadow. The provider of every key is
   read from the catalog seed rows in api/migrations, so a key with no catalog row fails here too.
2. GPT-4.1 is retired: no SAFE_DEFAULTS route names it, the legacy Haiku/Sonnet chain no longer
   falls back to it, and `_call_openai` refuses it (its catalog row is disabled by migration 224).
3. A stage with no OpenAI-API last resort never reaches the OpenAI client, even with every Bedrock
   call failing; a judge route reaches it only after both Bedrock models failed, and the response
   says fallback_used=True / provider='openai' (what llm_call_log records), with a fixed log event.
4. Admin: an OpenAI-API model cannot be saved as a primary, a shadow or a non-last fallback, and a
   retired catalog row cannot be re-enabled.
"""
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from shared.llm_client import catalog as catalog_mod
from shared.llm_client.call_log import _normalize_model_provider
from shared.llm_client.catalog import CatalogModel, CatalogPolicyBlocked
from shared.llm_client.client import LLMClient, OpenAIPlatformBlocked, openai_platform_allowed
from shared.llm_client.models import LLMRequest, LLMResponse
from shared.llm_client.role_config import SAFE_DEFAULTS

MIGRATIONS = Path(__file__).resolve().parents[2] / "api" / "migrations"
# ('key', 'label', 'vendor', 'provider', 'api_style', ... — the catalog seed rows (169, 170, 181).
_ROW = re.compile(r"\(\s*'([a-z0-9.\-]+)',\s*'[^']*',\s*'(\w+)',\s*'(\w+)',\s*'(\w+)'")
OPENAI_PLATFORM_KEYS = {"gpt-4.1", "gpt-6-luna-openai"}


def _catalog_providers() -> dict[str, str]:
    out = {}
    for path in sorted(MIGRATIONS.glob("*.sql")):
        text = path.read_text(encoding="utf-8")
        if "llm_model_catalog" not in text:
            continue
        for key, _vendor, provider, api_style in _ROW.findall(text):
            if api_style in ("anthropic_native", "converse", "openai_chat", "embed", "decide"):
                out[key] = provider
    return out


def test_migration_catalog_parse_finds_the_openai_platform_rows():
    providers = _catalog_providers()
    assert {k for k, p in providers.items() if p == "openai"} == OPENAI_PLATFORM_KEYS
    assert providers["gpt-5.6-luna"] == "bedrock" and providers["gpt-6-luna"] == "bedrock"


@pytest.mark.parametrize("stage", sorted(SAFE_DEFAULTS))
def test_openai_api_model_only_as_last_fallback_in_safe_defaults(stage):
    providers = _catalog_providers()
    cfg = SAFE_DEFAULTS[stage]
    keys = [cfg.model_id, *cfg.fallback_model_ids] + ([cfg.shadow_model_id] if cfg.shadow_model_id else [])
    for key in keys:
        assert key in providers, f"{stage}: {key!r} has no catalog seed row"
        assert key != "gpt-4.1", f"{stage}: gpt-4.1 is retired (S235)"
    assert providers[cfg.model_id] != "openai", f"{stage}: OpenAI-API primary"
    if cfg.shadow_model_id:
        assert providers[cfg.shadow_model_id] != "openai", f"{stage}: OpenAI-API shadow"
    for key in cfg.fallback_model_ids[:-1]:
        assert providers[key] != "openai", f"{stage}: OpenAI-API model {key!r} is not the last fallback"


# ── runtime: the adapter is reached only as the last resort ─────────────────────────────────────

def _m(key, **kw):
    base = dict(model_key=key, label=key, vendor="openai", provider="bedrock", api_style="converse",
                bedrock_profile_ids={"acc3": f"global.openai.{key}"}, callable_via=("llm_client",),
                price_in_per_mtok=0.1, price_out_per_mtok=0.5, enabled=True)
    base.update(kw)
    return CatalogModel(**base)


POST_224_CATALOG = {m.model_key: m for m in (
    _m("gpt-5.6-luna"), _m("gpt-6-luna"),
    _m("haiku", vendor="anthropic", api_style="anthropic_native"),
    _m("sonnet", vendor="anthropic", api_style="anthropic_native"),
    _m("cohere-embed-v4", vendor="cohere", api_style="embed", bedrock_profile_ids={}),
    _m("gpt-4.1", provider="openai", api_style="openai_chat", bedrock_profile_ids={},
       enabled=False, blocked_reason="S235: retired"),
    _m("gpt-6-luna-openai", provider="openai", api_style="openai_chat", bedrock_profile_ids={}),
)}


def _client():
    with patch("shared.llm_client.client.boto3"), patch("shared.llm_client.client.openai"):
        c = LLMClient()
    c._openai = MagicMock()
    return c


def _bedrock_down(c):
    down = RuntimeError("bedrock down")
    return [patch.object(c, "_call_bedrock", side_effect=down),
            patch.object(c, "_call_bedrock_satellite", side_effect=down),
            patch.object(c, "_call_bedrock_converse", side_effect=down)]


def _generate(c, request, stage_cfg, catalog_get):
    patches = [patch("shared.llm_client.client.get_model_sync", side_effect=catalog_get), *_bedrock_down(c)]
    if stage_cfg is not None:
        patches.append(patch("shared.llm_client.client.get_stage_config_sync", return_value=stage_cfg))
    for p in patches:
        p.start()
    try:
        return c.generate(request)
    finally:
        for p in reversed(patches):
            p.stop()


NO_LAST_RESORT = sorted(s for s, c in SAFE_DEFAULTS.items()
                        if c.role != "embed" and "gpt-6-luna-openai" not in c.fallback_model_ids)
WITH_LAST_RESORT = sorted(s for s, c in SAFE_DEFAULTS.items() if "gpt-6-luna-openai" in c.fallback_model_ids)


@pytest.mark.parametrize("catalog_get", [POST_224_CATALOG.get, lambda _key: None],
                         ids=["post_224_catalog", "catalog_unreachable"])
@pytest.mark.parametrize("stage", NO_LAST_RESORT)
def test_stage_without_last_resort_never_calls_openai(stage, catalog_get):
    c = _client()
    with pytest.raises(RuntimeError):
        _generate(c, LLMRequest(system_prompt="s", user_prompt="u", stage=stage), SAFE_DEFAULTS[stage],
                  catalog_get)
    c._openai.chat.completions.create.assert_not_called()


@pytest.mark.parametrize("stage", WITH_LAST_RESORT)
def test_catalog_unreachable_never_calls_openai(stage):
    c = _client()
    with pytest.raises(RuntimeError):
        _generate(c, LLMRequest(system_prompt="s", user_prompt="u", stage=stage), SAFE_DEFAULTS[stage],
                  lambda _key: None)
    c._openai.chat.completions.create.assert_not_called()


@pytest.mark.parametrize("stage", WITH_LAST_RESORT)
def test_bedrock_outage_reaches_openai_last_and_is_flagged(stage):
    c = _client()
    order = []

    def fake_openai(request, model, catalog_model=None):
        order.append(model)
        return LLMResponse(content="{}", model_used=catalog_model.model_key, provider="openai")

    with patch.object(c, "_call_openai", side_effect=fake_openai), \
            patch("shared.llm_client.client.logger") as log:
        resp = _generate(c, LLMRequest(system_prompt="s", user_prompt="u", stage=stage),
                         SAFE_DEFAULTS[stage], POST_224_CATALOG.get)
    assert order == ["gpt-6-luna-openai"]
    assert resp.fallback_used is True and resp.provider == "openai"
    assert any(call.args and call.args[0] == "llm_openai_fallback_used" for call in log.warning.call_args_list)
    # What a caller's record_call writes to llm_call_log for this response.
    assert _normalize_model_provider(resp.model_used, resp.provider) == ("gpt-6-luna-openai", "openai")
    assert _normalize_model_provider(resp.model_used, None) == ("gpt-6-luna-openai", "openai")


@pytest.mark.parametrize("tier", ["haiku", "sonnet", "gpt-4.1"])
def test_legacy_tiers_have_no_gpt41_last_resort(tier):
    c = _client()
    with pytest.raises(RuntimeError):
        _generate(c, LLMRequest(system_prompt="s", user_prompt="u", model_tier=tier), None, POST_224_CATALOG.get)
    c._openai.chat.completions.create.assert_not_called()


@pytest.mark.parametrize("catalog_get", [POST_224_CATALOG.get, lambda _key: None],
                         ids=["retired", "catalog_unreachable"])
def test_call_openai_refuses_gpt41(catalog_get):
    c = _client()
    with patch("shared.llm_client.client.get_model_sync", side_effect=catalog_get):
        assert openai_platform_allowed("gpt-4.1") is False
        with pytest.raises(OpenAIPlatformBlocked):
            c._call_openai(LLMRequest(system_prompt="s", user_prompt="u"), model="gpt-4.1")
    c._openai.chat.completions.create.assert_not_called()


def test_gate_open_only_for_an_enabled_openai_row():
    with patch("shared.llm_client.client.get_model_sync", side_effect=POST_224_CATALOG.get):
        assert openai_platform_allowed("gpt-6-luna-openai") is True
        assert openai_platform_allowed("gpt-4.1") is False
        assert openai_platform_allowed("gpt-5.6-luna") is False  # Bedrock rows never use the adapter
    with patch("shared.llm_client.client.get_model_sync", return_value=None):
        assert openai_platform_allowed("gpt-6-luna-openai") is False  # fails closed


# ── admin ───────────────────────────────────────────────────────────────────────────────────────

JUDGE_ROW = {"stage": "t10_judge", "role": "judge", "model_id": "gpt-5.6-luna",
             "fallback_model_ids": ["gpt-6-luna", "gpt-6-luna-openai"], "shadow_model_id": None,
             "shadow_sample_pct": 0}


async def _route(body, current=JUDGE_ROW):
    from api.routers import admin_llm_ops as ops

    async def rows():
        return [current]

    async def models():
        return list(POST_224_CATALOG.values())

    async def setter(stage, **kw):
        return {"stage": stage, "model_id": current["model_id"], "updated_at": None, **kw}

    with patch.object(ops, "verify_admin_secret"), patch.object(ops, "list_stage_configs", rows), \
            patch.object(ops, "_load_catalog", models), patch.object(ops, "set_stage_route", setter):
        return await ops.patch_llm_route("t10_judge", ops.LlmRoutePatch(**body),
                                         x_admin_secret="x", x_admin_user_id="a")


@pytest.mark.asyncio
async def test_admin_accepts_openai_api_as_last_fallback():
    out = await _route({"fallback_model_ids": ["gpt-6-luna", "gpt-6-luna-openai"]})
    assert out["fallback_model_ids"] == ["gpt-6-luna", "gpt-6-luna-openai"]


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [
    {"fallback_model_ids": ["gpt-6-luna-openai", "gpt-6-luna"]},
    {"shadow_model_id": "gpt-6-luna-openai", "shadow_sample_pct": 10},
], ids=["not_last", "shadow"])
async def test_admin_rejects_openai_api_outside_the_last_fallback(body):
    with pytest.raises(HTTPException) as exc:
        await _route(body)
    assert exc.value.status_code == 422 and "last fallback" in exc.value.detail


@pytest.mark.asyncio
async def test_admin_rejects_openai_api_primary():
    from api.routers import admin_llm_ops as ops

    async def rows():
        return [JUDGE_ROW]

    async def models():
        return list(POST_224_CATALOG.values())

    with patch.object(ops, "verify_admin_secret"), patch.object(ops, "list_stage_configs", rows), \
            patch.object(ops, "_load_catalog", models), patch.object(ops, "set_stage_config") as setter:
        with pytest.raises(HTTPException) as exc:
            await ops.patch_llm_config("t10_judge", ops.LlmConfigPatch(model_id="gpt-6-luna-openai"),
                                       x_admin_secret="x", x_admin_user_id="a")
    assert exc.value.status_code == 422
    setter.assert_not_called()


class _TxnCM:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


@pytest.mark.asyncio
async def test_set_catalog_price_refuses_to_re_enable_gpt41():
    conn = MagicMock()
    conn.transaction = MagicMock(return_value=_TxnCM())
    conn.execute = AsyncMock()
    conn.close = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=[
        {"price_in_per_mtok": 2.0, "price_out_per_mtok": 8.0, "price_source": "x", "enabled": False},
        AssertionError("must not UPDATE"),
    ])
    with patch.object(catalog_mod.asyncpg, "connect", AsyncMock(return_value=conn)), \
            patch.object(catalog_mod, "get_database_url", return_value="postgresql://x"), \
            patch.object(catalog_mod, "invalidate"):
        with pytest.raises(CatalogPolicyBlocked):
            await catalog_mod.set_catalog_price("gpt-4.1", fields={"enabled": True},
                                                updated_by="u", audit_actor="a")
    conn.execute.assert_not_called()  # no audit row for a refused change


@pytest.mark.asyncio
async def test_catalog_patch_maps_policy_block_to_422():
    from api.routers import admin_llm_ops as ops

    async def raiser(model_key, **kw):
        raise CatalogPolicyBlocked("gpt-4.1 is retired (S235) and cannot be re-enabled")

    with patch.object(ops, "verify_admin_secret"), patch.object(ops, "set_catalog_price", raiser):
        with pytest.raises(HTTPException) as exc:
            await ops.patch_llm_catalog("gpt-4.1", ops.LlmCatalogPatch(enabled=True),
                                        x_admin_secret="x", x_admin_user_id="a")
    assert exc.value.status_code == 422
