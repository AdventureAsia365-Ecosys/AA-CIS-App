"""AA-1080 — the T0 brief import fills Brand context / default CTA / signature words and reports what it could not
map; unsupported markets are recorded one by one and explained; T2 keywords follow the agency's first supported
market. Invented brands and places only."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.brand_intake.brief import (
    brief_cta_sentence, draft_brand_context, draft_default_cta, parse_brief_paragraphs,
)
from services.brand_intake.markets import REASON_NO_SEARCH_DATA, market_summary, target_market_json
from services.brand_intake.save import CUSTOM_VALIDATORS_VALUE_SQL, record_unsupported_markets, signature_words_json

# The AA brief template, as the four audit briefs use it: no Brand context, CTA or Signature words section.
TEMPLATE_BRIEF = [
    "Quillfern Journeys",
    "Brand Identity",
    "Brand type: Slow cultural travel brand\nCore idea: Unhurried days with local makers.",
    "Target Market",
    "Primary markets:\nEurope, Australia, Marovia",
    "Customer segment:\nCouples 45–65",
    "Customer mindset:\nThey want time to talk with the people they meet.",
    "Tone of Voice",
    "Calm\nCurious\nWarm",
    "Writing Style",
    "Write in full sentences.",
    "Good Example",
    "“Spend the afternoon at a weaver's loom in Tessaly.”",
    "Should Write",
    "Use our words “slow road” and “maker visit”.",
    "Should Not Write",
    "Avoid “once in a lifetime”.",
]


def test_template_brief_drafts_context_and_cta_and_keeps_its_own_words():
    out = parse_brief_paragraphs(TEMPLATE_BRIEF)
    f = out["fields"]
    assert f["system_prompt"].startswith("Write as Quillfern Journeys, slow cultural travel brand.")
    assert "Core idea: Unhurried days with local makers." in f["system_prompt"]
    assert "Readers: Couples 45–65" in f["system_prompt"] and "Voice: Calm, Curious, Warm." in f["system_prompt"]
    assert f["default_cta"] == "Get in touch with Quillfern Journeys to plan this trip."
    assert f["signature_words"] == ["slow road", "maker visit"]       # short quotes under Should Write
    assert set(out["drafted"]) == {"system_prompt", "default_cta", "signature_words"}
    assert out["unmapped_fields"] == []
    # a whole example sentence in quotes is not a signature word; the forbidden phrase stays forbidden
    assert f["forbidden_words"] == ["once in a lifetime"]


def test_explicit_sections_win_and_are_not_marked_drafted():
    brief = [*TEMPLATE_BRIEF,
             "Brand context", "You write for travellers who distrust hype.",
             "Call to action", "Ask Quillfern for a slow-road itinerary.",
             "Signature words", "slow road, maker visit; kiln morning"]
    out = parse_brief_paragraphs(brief)
    f = out["fields"]
    assert f["system_prompt"] == "You write for travellers who distrust hype."
    assert f["default_cta"] == "Ask Quillfern for a slow-road itinerary."
    assert f["signature_words"] == ["slow road", "maker visit", "kiln morning"]
    assert out["drafted"] == []


def test_inline_labels_map_too():
    brief = ["Ember & Fjell", "Brand Identity", "CTA: Plan your crossing with Ember & Fjell.",
             "Signature words: fjell walk, ember night"]
    f = parse_brief_paragraphs(brief)["fields"]
    assert f["default_cta"] == "Plan your crossing with Ember & Fjell."
    assert f["signature_words"] == ["fjell walk", "ember night"]


def test_unknown_styled_heading_is_reported_and_not_merged_into_the_previous_section():
    paras = [*TEMPLATE_BRIEF, "Booking notes", "Deposits are taken by the partner office in Velmora."]
    flags = [False] * len(TEMPLATE_BRIEF) + [True, False]
    out = parse_brief_paragraphs(paras, flags)
    assert out["unmapped_fields"] == [{"heading": "Booking notes", "chars": 52}]
    assert "Velmora" not in json.dumps(out["fields"])                # not folded into Avoid / forbidden words
    assert any("not imported: Booking notes" in w for w in out["warnings"])


def test_loose_colon_heading_without_styles_is_reported_but_known_labels_are_not():
    paras = ["Brightmoor Treks", "Brand Identity", "Customer segment:", "Families with teens",
             "Insurance:", "Partners arrange cover."]
    out = parse_brief_paragraphs(paras)
    assert out["fields"]["customer_segment"] == "Families with teens"
    assert [u["heading"] for u in out["unmapped_fields"]] == ["Insurance"]


def test_brief_cta_sentence_is_used_before_a_drafted_line():
    brief = [*TEMPLATE_BRIEF[:-2], "Should Write", "Close every page warmly. Talk to our makers' desk to plan it."]
    f = parse_brief_paragraphs(brief)["fields"]
    assert f["default_cta"] == "Talk to our makers' desk to plan it."


def test_cta_sentence_skips_negations_and_draft_needs_a_name():
    assert brief_cta_sentence(["Do not ask us to discount."]) == ""
    assert draft_default_cta("") == ""
    assert draft_brand_context({"brand_name": "Only Name"}) == ""


def test_declared_positioning_word_becomes_a_signature_word_unless_forbidden():
    brief = ["Saffron Hollow", "Brand Identity", "Brand type: Luxury river travel brand"]
    assert parse_brief_paragraphs(brief)["fields"]["signature_words"] == ["luxury"]
    brief_forbidden = [*brief, "Forbidden words", "luxury"]
    out = parse_brief_paragraphs(brief_forbidden)
    assert out["fields"]["signature_words"] == []
    assert any("No signature words" in w for w in out["warnings"])


# ── markets ──────────────────────────────────────────────────────────────────────────────────────────────

def test_market_summary_names_each_unsupported_market_with_its_reason_and_the_seo_market():
    s = market_summary(["UAE", "Singapore", "UK", "Australia"])
    assert s["supported"] == ["UK", "AU"] and s["unsupported"] == ["AE", "SG"]
    assert s["seo_market"] == "UK" and s["defaulted"] is False
    assert s["notes"] == [{"country": "AE", "reason": REASON_NO_SEARCH_DATA, "uses": "UK"},
                          {"country": "SG", "reason": REASON_NO_SEARCH_DATA, "uses": "UK"}]
    none = market_summary(["UAE"])
    assert none["seo_market"] == "US" and none["defaulted"] is True


def test_t2_keyword_market_is_the_first_supported_declared_market():
    from services.seo_intelligence.seed_builder import first_supported_market, resolve_buyer_market
    europe_first = target_market_json(["Europe", "Australia", "North America", "Singapore"])
    assert first_supported_market(europe_first) == "UK"
    assert resolve_buyer_market(europe_first)[1] == "United Kingdom"
    gulf_first = target_market_json(["UAE", "Saudi Arabia", "US", "UK"])
    assert resolve_buyer_market(gulf_first)[1] == "United States"
    assert resolve_buyer_market({"countries": ["AE"]})[1] == "United States"      # nothing supported: US default


@pytest.mark.asyncio
async def test_record_unsupported_markets_writes_one_row_each_and_never_raises():
    conn = AsyncMock()
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    tm = target_market_json(["US", "UK", "UAE", "Hong Kong"])
    with patch("shared.dfs_client.unmapped_market.record_unmapped_market_request", new=AsyncMock()) as rec:
        assert await record_unsupported_markets(pool, "t-1", tm) == ["AE", "HK"]
    assert [c.args[1:] for c in rec.await_args_list] == [("t-1", "AE"), ("t-1", "HK")]
    with patch("shared.dfs_client.unmapped_market.record_unmapped_market_request",
               new=AsyncMock(side_effect=RuntimeError("db down"))):
        assert await record_unsupported_markets(pool, "t-1", tm) == ["AE", "HK"]
    assert await record_unsupported_markets(pool, "t-1", target_market_json(["UK"])) == []


# ── save helpers ─────────────────────────────────────────────────────────────────────────────────────────

def test_signature_words_json_sanitises_dedupes_and_keeps_none_as_carry_over():
    assert signature_words_json(None) is None
    assert json.loads(signature_words_json([])) == []
    assert json.loads(signature_words_json(["Slow Road", "slow road", " maker visit "])) == ["Slow Road", "maker visit"]
    assert len(json.loads(signature_words_json([f"w{i}" for i in range(30)]))) == 20


def test_custom_validators_sql_keeps_other_keys_and_carries_over():
    sql = CUSTOM_VALIDATORS_VALUE_SQL.format(param="$15", tenant="$1", version="$5")
    assert sql.startswith("CASE WHEN $15::jsonb IS NULL THEN (SELECT p.custom_validators")
    assert "|| jsonb_build_object('signature_words', $15::jsonb)" in sql and "'{}'::jsonb" in sql


# ── the routes ───────────────────────────────────────────────────────────────────────────────────────────

class _Txn:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *a):
        return False


def _pool(conn):
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    return pool


@pytest.mark.asyncio
async def test_staff_brand_save_sends_signature_words_and_returns_the_market_note():
    from api.routers import admin_pipeline
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=2)
    conn.transaction = MagicMock(return_value=_Txn())
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=_pool(conn))))
    body = admin_pipeline.BrandIdentityUpdate(brand_name="Quillfern Journeys", target_markets=["UAE", "UK"],
                                              signature_words=["slow road"])
    with patch("services.writing_memory.releases.reverify_scan_in_tx", new=AsyncMock()), \
         patch("services.acp_shared.cta_review_notice.check_saved_cta", new=AsyncMock(return_value=None)), \
         patch("services.brand_intake.save.record_unsupported_markets", new=AsyncMock()) as rec:
        out = await admin_pipeline.update_brand_identity(body, req, tenant_id="7a6b7c1e-0000-4000-8000-000000000001",
                                                         tenant_caller=False)
    insert = next(c for c in conn.execute.await_args_list if "INSERT INTO shared.tenant_brand_rules" in c.args[0])
    assert "custom_validators" in insert.args[0]
    assert json.loads(insert.args[15]) == ["slow road"]
    assert out["markets"]["seo_market"] == "UK" and out["markets"]["unsupported"] == ["AE"]
    assert out["markets"]["notes"][0]["reason"] == REASON_NO_SEARCH_DATA
    rec.assert_awaited_once()


def test_get_brand_identity_shape_carries_signature_words_and_markets():
    from api.routers import admin_pipeline
    assert "custom_validators" in admin_pipeline._BRAND_COLS
    assert admin_pipeline._row_get({"a": 1}, "custom_validators") is None


# ── re-import of a stored brief (scripts/aa1080/brief_reimport.py) ──────────────────────────────────────────

def test_plan_fill_only_fills_empty_fields_and_keeps_other_validator_keys():
    from services.brand_intake.reimport import plan_fill
    parsed = {"system_prompt": "Write as Quillfern.", "default_cta": "Get in touch.", "signature_words": ["slow road"]}
    empty = {"system_prompt": None, "default_cta": "", "custom_validators": '{"voice_rubric": {"profile": "calm"}}'}
    plan = plan_fill(empty, parsed)
    assert plan["fill"] == parsed and plan["kept"] == []
    assert plan["custom_validators"] == {"voice_rubric": {"profile": "calm"}, "signature_words": ["slow road"]}
    full = {"system_prompt": "Own context", "default_cta": "Own CTA",
            "custom_validators": {"signature_words": ["own word"]}}
    plan = plan_fill(full, parsed)
    assert plan["fill"] == {} and plan["kept"] == ["system_prompt", "default_cta", "signature_words"]


@pytest.mark.asyncio
async def test_apply_fill_writes_one_new_version_in_a_transaction():
    from services.brand_intake import reimport
    conn = AsyncMock()
    conn.transaction = MagicMock(return_value=_Txn())
    conn.fetchval = AsyncMock(return_value=3)
    active = {"id": "11111111-1111-4111-8111-111111111111", "system_prompt": None, "default_cta": "Own CTA",
              "custom_validators": None}
    plan = reimport.plan_fill(active, {"system_prompt": "Write as Quillfern.", "default_cta": "x",
                                       "signature_words": []})
    assert await reimport.apply_fill(conn, "t-1", active, plan, "brand-identity/t-1/brief.docx") == 4
    sqls = [c.args[0] for c in conn.execute.await_args_list]
    assert sqls == [reimport.LOCK_SQL, reimport.DEACTIVATE_SQL, reimport.COPY_SQL]
    copy_args = conn.execute.await_args_list[2].args
    assert copy_args[3:] == (4, "Write as Quillfern.", None, "Own CTA", "brand-identity/t-1/brief.docx")
    conn.reset_mock()
    assert await reimport.apply_fill(conn, "t-1", active, {"fill": {}, "custom_validators": None}) is None
    conn.execute.assert_not_awaited()


# ── $0 keyword-market replay (scripts/aa1080/t2_keyword_market_replay.py) ───────────────────────────────────

def _replay_module():
    import importlib.util
    import pathlib
    path = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "aa1080" / "t2_keyword_market_replay.py"
    spec = importlib.util.spec_from_file_location("aa1080_replay", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_replay_compares_the_rank_rule_with_the_first_declared_rule():
    mod = _replay_module()
    europe_first = target_market_json(["Europe", "Australia", "North America"])
    assert (mod.market_before(europe_first), mod.market_after(europe_first)) == ("US", "UK")
    assert mod.market_before({"countries": ["AE"]}) == mod.market_after({"countries": ["AE"]}) == "US"
    assert mod.parse_args(["TENANTS=a, b", "TOURS=50"]) == {"tenants": ["a", "b"], "tours": 20}
    with pytest.raises(SystemExit):
        mod.parse_args([])


@pytest.mark.asyncio
async def test_replay_tenant_runs_the_cached_pick_for_both_markets():
    mod = _replay_module()
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[{"tour_id": "t1"}])
    cfg = SimpleNamespace(target_market=target_market_json(["Europe", "US"]))
    service = MagicMock(get_seo_config=AsyncMock(return_value=cfg))

    async def pick(_conn, market, tour_id):
        return {"top_keywords": [f"{market.lower()} kw {i}" for i in range(7)]}

    with patch("shared.services.tenant_config_service.TenantConfigService", return_value=service), \
         patch("services.seo_intelligence.s1_prefetch.market_keyword_seo", new=pick):
        out = await mod.replay_tenant(conn, "t-1", "quillfern", 3)
    assert out["market_before"] == "US" and out["market_after"] == "UK" and out["changed"] is True
    assert out["tours"][0]["after"] == {"market": "UK", "keywords": [f"uk kw {i}" for i in range(5)]}
