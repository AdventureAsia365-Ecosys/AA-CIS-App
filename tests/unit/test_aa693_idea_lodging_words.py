"""AA-693 (S206) — lodging words drop a keyword idea before Jev; getting-there searches stay."""
import asyncio
from unittest.mock import patch

from services.acp_contract import segment_research_batch as srb


def test_lodging_words_from_the_first_enforced_run():
    for kw in ["hotel bhutan", "druk hotel paro", "galing resort paro", "bhutan boutique residency thimphu",
               "Tina Guesthouse", "agoda bhutan", "cheap hotels in thimphu below 1000"]:
        assert srb.is_lodging_search(kw), kw


def test_trip_searches_and_transfers_are_kept():
    for kw in ["incheon airport to hotel", "seoul airport to hotel", "tiger's nest hike", "flights to ulaanbaatar",
               "Golgulsa Temple stay", "punakha dzong", "amankora paro bhutan"]:   # brand-only: a known gap
        assert not srb.is_lodging_search(kw), kw


def test_gate_ideas_skips_jev_for_lodging_rows():
    asked = []

    async def fake(stage, key, state, qs, **kw):
        asked.append(state["keyword"])
        from shared.llm_client.decide import Decision
        return Decision()

    rows = [("hotel bhutan", "US", 100), ("tiger's nest hike", "US", 900)]
    with patch.object(srb, "decide", fake):
        kept = asyncio.run(srb._gate_ideas(rows, ["Paro"], pool=None))
    assert kept == [("tiger's nest hike", "US", 900)] and asked == ["tiger's nest hike"]
