"""AA-706 — Jev reviews S1 keyword ideas (shadow keeps the substring result; enforce adds/drops).
AA-707 — T2 takes keywords + PAA for the tenant's own market from the research cache."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from services.seo_intelligence import s1_prefetch as P


def _spec():
    return P.tour_spec({"tour_id": "t1", "src_name": "Paro Taktsang Hike", "country": "Bhutan",
                        "activities": None})


class _Dec:
    def __init__(self, acc=False, rej=False):
        self._a, self._r = acc, rej

    def accepted(self, k):
        return self._a

    def rejected(self, k):
        return self._r


@pytest.mark.asyncio
async def test_jev_shadow_keeps_substring_result():
    spec = _spec()
    cands = [{"keyword": "taktsang hike", "search_volume": 900},
             {"keyword": "tiger's nest bhutan", "search_volume": 5000}]
    kept = [cands[0]]
    with patch("shared.llm_client.decide.decide", AsyncMock(return_value=_Dec())) as d:
        out = await P.jev_review(spec, cands, kept)
    assert out == kept and d.await_count == 2
    state = d.await_args_list[0].args[2]
    assert state["tour"] == "Paro Taktsang Hike" and state["title_places"] == ["paro", "taktsang", "hike"]


@pytest.mark.asyncio
async def test_jev_enforced_accept_adds_synonym_and_reject_drops():
    spec = _spec()
    cands = [{"keyword": "taktsang hike", "search_volume": 900},
             {"keyword": "tiger's nest bhutan", "search_volume": 5000}]

    async def fake(stage, subject, state, qs, **kw):
        return _Dec(acc=True) if "tiger" in state["keyword"] else _Dec(rej=True)
    with patch("shared.llm_client.decide.decide", side_effect=fake):
        out = await P.jev_review(spec, cands, [cands[0]])
    assert [i["keyword"] for i in out] == ["tiger's nest bhutan"]


class _Conn:
    def __init__(self, demand):
        self.demand = demand

    async def fetchrow(self, sql, *a):
        return {"tour_id": "t1", "src_name": "Paro Taktsang Hike", "country": "Bhutan", "activities": None}

    async def fetch(self, sql, *a):
        assert a[0] == "UK"                     # tenant market, not US
        return self.demand


@pytest.mark.asyncio
async def test_t2_uses_tenant_market_keywords_and_paa():
    demand = [{"keyword": "taktsang monastery hike", "search_volume": 700,
               "people_also_ask": '["How hard is the Tiger\'s Nest hike?"]'}]
    cfg = SimpleNamespace(target_market={"countries": ["UK"]})
    with patch("shared.services.tenant_config_service.TenantConfigService.get_seo_config",
               AsyncMock(return_value=cfg)):
        market, seo = await P.tenant_market_seo(_Conn(demand), "tenant-1", "t1")
    assert market == "UK"
    assert seo["keywords"]["top_keywords"] == ["taktsang monastery hike"]
    assert seo["people_also_ask"] == ["How hard is the Tiger's Nest hike?"]


@pytest.mark.asyncio
async def test_t2_market_without_cache_returns_none():
    cfg = SimpleNamespace(target_market={"countries": ["UK"]})
    with patch("shared.services.tenant_config_service.TenantConfigService.get_seo_config",
               AsyncMock(return_value=cfg)):
        market, seo = await P.tenant_market_seo(_Conn([]), "tenant-1", "t1")
    assert market == "UK" and seo is None


def test_migration_196_seeds_shadow_question():
    from pathlib import Path
    sql = (Path(__file__).resolve().parents[2] / "api/migrations/196_a1_seo_jev_question.sql").read_text()
    assert "'a1_keyword_about_tour', 'a1_seo', 'noul'" in sql and "'shadow'" in sql
