"""AA-741 (S229) — T0 brand intake: brief parser, market normalisation, no silent truncation,
markets synced to tenant_seo_config (the store T2/T7 actually read)."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from services.brand_intake.brief import parse_brief_paragraphs
from services.brand_intake.markets import normalize_markets, target_market_json

BRIEF = [
    "Trail Pulse",
    "cis_abcdefghijklmnopqrstuvwxyz0123456789",            # a pasted key must be dropped
    "Brand Identity",
    "Brand type: Young active adventure travel brand\nCore idea: Movement and social discovery.",
    "Trail Pulse creates active trips for travellers who want to feel alive.",
    "Target Market",
    "Primary markets:\nEurope, Australia, North America, Singapore",
    "Customer segment:\n25–40 years old\nSolo travellers",
    "Customer mindset:\nThey want active days.",
    "Tone of Voice",
    "Energetic\nFresh\nDirect",
    "Writing Style",
    "Use active verbs.\nKeep sentences short.",
    "Good Example",
    "“Ride quiet backroads.”",
    "Should Write",
    "“Start the morning on a forest trail.”",
    "Should Not Write",
    "Do not sound too premium.\nDo not use luxury wording like “discreet elegance,” or “refined access.”",
]


def test_brief_sections_map_to_form_fields():
    out = parse_brief_paragraphs(BRIEF)
    f = out["fields"]
    assert f["brand_name"] == "Trail Pulse"
    assert f["brand_type"] == "Young active adventure travel brand"
    assert f["core_idea"].startswith("Movement and social discovery.") and "feel alive" in f["core_idea"]
    assert f["target_markets"] == ["Europe", "Australia", "North America", "Singapore"]
    assert f["customer_segment"] == "25–40 years old\nSolo travellers"
    assert f["tone_of_voice"] == ["Energetic", "Fresh", "Direct"]
    assert f["forbidden_words"] == ["discreet elegance", "refined access"]


def test_should_not_write_never_lands_in_should_write():
    """The old parser folded 'Should Not Write' into 'Should Write' (opposite meaning)."""
    f = parse_brief_paragraphs(BRIEF)["fields"]
    assert "Do not" not in f["good_examples"]
    assert "Write like this:\n“Start the morning" in f["style_guide"]
    assert "Avoid:\nDo not sound too premium." in f["style_guide"]


def test_pasted_api_key_is_dropped_and_flagged():
    out = parse_brief_paragraphs(BRIEF)
    assert "cis_" not in json.dumps(out["fields"])
    assert any("API key" in w for w in out["warnings"])


def test_missing_sections_are_reported():
    out = parse_brief_paragraphs(["Only A Name"])
    assert any("Core idea" in w for w in out["warnings"])
    assert any("Writing style" in w for w in out["warnings"])


def test_market_normalisation_follows_the_agreed_mapping():
    m = normalize_markets(["Europe", "Australia", "North America", "Singapore", "Atlantis"])
    assert m["supported"] == ["UK", "DE", "FR", "NL", "AU", "US"]
    assert m["unsupported"] == ["CA", "SG"]
    assert m["unknown"] == ["Atlantis"]


def test_all_unsupported_markets_keep_the_aa629_banner_signal():
    from services.seo_intelligence.seed_builder import resolve_buyer_markets, unmatched_countries
    tm = target_market_json(["UAE", "Saudi Arabia"])
    assert unmatched_countries(tm) == ["AE", "SA"]           # banner + unmapped request
    tm2 = target_market_json(["UAE", "UK"])
    assert unmatched_countries(tm2) == [] and [x[0] for x in resolve_buyer_markets(tm2)] == [2826]


# ── endpoint: reject over-long instead of cutting; sync markets ────────────────────────────

class _Txn:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc):
        return False


def _pool(conn):
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    return pool


def _request(pool):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=pool)))


@pytest.mark.asyncio
async def test_long_style_guide_is_rejected_not_truncated():
    from api.routers import admin_pipeline
    conn = AsyncMock()
    conn.transaction = MagicMock(return_value=_Txn())
    body = admin_pipeline.BrandIdentityUpdate(style_guide="A" * 3000)
    with pytest.raises(HTTPException) as e:
        await admin_pipeline.update_brand_identity(body, _request(_pool(conn)), tenant_id="t-1")
    assert e.value.status_code == 422 and "style guide" in e.value.detail
    conn.execute.assert_not_called()


@pytest.mark.asyncio
async def test_real_brief_length_style_guide_is_kept_whole_and_markets_synced():
    from api.routers import admin_pipeline
    conn = AsyncMock()
    conn.transaction = MagicMock(return_value=_Txn())
    conn.fetchval.return_value = 1
    style = "Use active verbs. " * 60                     # ~1,080 chars, like a real brief
    body = admin_pipeline.BrandIdentityUpdate(style_guide=style, target_markets=["Europe", "UAE"])
    out = await admin_pipeline.update_brand_identity(body, _request(_pool(conn)), tenant_id="t-1")
    insert = next(c for c in conn.execute.call_args_list if "INSERT INTO shared.tenant_brand_rules" in c.args[0])
    assert insert.args[3] == style.strip()
    seo = next(c for c in conn.execute.call_args_list if "tenant_seo_config" in c.args[0])
    tm = json.loads(seo.args[2])
    assert tm["countries"] == ["UK", "DE", "FR", "NL", "AE"] and tm["unsupported"] == ["AE"]
    assert out["markets"]["unsupported"] == ["AE"]


@pytest.mark.asyncio
async def test_aa_internal_markets_stay_owned_by_admin_settings():
    from api.routers import admin_pipeline
    conn = AsyncMock()
    conn.transaction = MagicMock(return_value=_Txn())
    conn.fetchval.return_value = 1
    body = admin_pipeline.BrandIdentityUpdate(target_markets=["UK"])
    await admin_pipeline.update_brand_identity(body, _request(_pool(conn)),
                                               tenant_id=admin_pipeline._AA_INTERNAL_TENANT_ID)
    assert not any("tenant_seo_config" in c.args[0] for c in conn.execute.call_args_list)
