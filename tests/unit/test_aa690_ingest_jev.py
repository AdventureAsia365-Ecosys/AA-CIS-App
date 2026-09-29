"""AA-690 — A0 ingest Jev gates: what a row's Decision does (drop / fill country / preview notes)."""
from unittest.mock import AsyncMock, patch

import pytest

from services.ingestion import jev_gates as g
from shared.llm_client.decide import Decision, Verdict


def _dec(kind=("multi_day_tour", 0.9, "shadow", "grey"), itin=(0.8, "shadow", "grey"), country=None):
    v = {
        g.ROW_KIND_Q: Verdict(g.ROW_KIND_Q, kind[2], kind[3], probability=kind[1], choice=kind[0]),
        g.ITINERARY_Q: Verdict(g.ITINERARY_Q, itin[1], itin[2], probability=itin[0]),
    }
    if country:
        v[g.COUNTRY_Q] = Verdict(g.COUNTRY_Q, country[2], country[3], probability=country[1], choice=country[0])
    return Decision(verdicts=v)


def test_enforced_confident_non_tour_is_dropped():
    out = g.verdict_from(_dec(kind=("poi_or_attraction", 0.97, "enforce", "accept")), ask_country=False)
    assert out.drop_reason == "not_a_tour" and out.kind == "poi_or_attraction"


def test_enforced_tour_kind_is_kept():
    assert g.verdict_from(_dec(kind=("day_tour", 0.97, "enforce", "accept")), False).drop_reason is None


def test_enforced_thin_itinerary_is_dropped():
    out = g.verdict_from(_dec(itin=(0.05, "enforce", "reject")), ask_country=False)
    assert out.drop_reason == "thin_itinerary"


def test_shadow_never_drops_but_leaves_notes():
    out = g.verdict_from(_dec(kind=("accommodation", 0.92, "shadow", "accept"), itin=(0.1, "shadow", "reject")),
                         ask_country=False)
    assert out.drop_reason is None
    assert any("accommodation" in n for n in out.notes) and any("thin" in n for n in out.notes)


def test_country_filled_only_when_enforced_and_not_other():
    filled = g.verdict_from(_dec(country=("Bhutan", 0.95, "enforce", "accept")), ask_country=True)
    other = g.verdict_from(_dec(country=("other", 0.95, "enforce", "accept")), ask_country=True)
    shadow = g.verdict_from(_dec(country=("Nepal", 0.9, "shadow", "accept")), ask_country=True)
    assert filled.country == "Bhutan" and other.country is None and shadow.country is None
    assert any("Nepal" in n for n in shadow.notes)


def test_errors_fail_open():
    dec = Decision(verdicts={g.ROW_KIND_Q: Verdict(g.ROW_KIND_Q, "enforce", "error"),
                             g.ITINERARY_Q: Verdict(g.ITINERARY_Q, "enforce", "error")})
    out = g.verdict_from(dec, ask_country=False)
    assert out.drop_reason is None and out.notes == []


@pytest.mark.asyncio
async def test_assess_rows_asks_country_only_when_missing_and_keeps_order():
    asked = []

    async def fake(stage, subject, state, keys, **kw):
        asked.append(keys)
        return _dec()

    rows = [{"src_name": "A", "country": "Nepal", "src_itineraries": "Day 1"},
            {"src_name": "B", "country": None, "src_itineraries": "Day 1"}]
    with patch.object(g, "decide", new=AsyncMock(side_effect=fake)):
        out = await g.assess_rows(rows, pool=None, source="file.xlsx")
    assert len(out) == 2
    assert [g.COUNTRY_Q in k for k in asked] == [False, True]
