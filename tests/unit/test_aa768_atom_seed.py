"""AA-768 — T8/T9 write from the atom's evidence + hook + Segment siblings, not its 26-char label."""
from unittest.mock import AsyncMock

import pytest

from services.acp_shared.atom_seed import MAX_SIBLINGS, atom_seed_text, compose_seed

pytestmark = pytest.mark.real_atom_seed


def test_compose_full_seed():
    seed = compose_seed(
        "Pawangka Monastery — visit",
        "Visit Pawangka Monastery, built in the seventh century by Songtsen Gampo.",
        "ancient sacred site", None,
        ["Visit Pawangka Monastery, built in the seventh century by Songtsen Gampo.",  # dup of own
         "Walk to Pawangka, one of the oldest monasteries in the valley.",
         "Walk to Pawangka, one of the oldest monasteries near Lhasa."],  # S234: names another place
    )
    assert seed.splitlines()[0] == "Pawangka Monastery — visit"
    assert "Source itinerary: Visit Pawangka Monastery, built in the seventh century" in seed
    assert "Why it matters: ancient sacred site" in seed
    assert "Season:" not in seed
    assert seed.count("seventh century") == 1  # sibling duplicate dropped
    assert "- Walk to Pawangka, one of the oldest monasteries in the valley." in seed
    assert "Lhasa" not in seed


def test_compose_label_only_when_no_fields():
    assert compose_seed("Tiger's Nest — hike", None, " ", None, []) == "Tiger's Nest — hike"


def test_siblings_capped():
    seed = compose_seed("x", None, None, None, [f"ev {i}" for i in range(10)])
    assert seed.count("\n- ") == MAX_SIBLINGS


@pytest.mark.asyncio
async def test_atom_seed_text_reads_atom_and_siblings():
    conn = AsyncMock()
    conn.fetchrow.side_effect = [
        {"text": "Label", "evidence": "Own source.", "emotional_hook": None, "season_note": "Best Oct–Nov",
         "place": "Pawangka Monastery", "country": "Tibet", "itinerary_day": 4},
        None,  # no place card yet
    ]
    conn.fetch.return_value = [{"evidence": "B sibling.", "n": 10}, {"evidence": "A sibling.", "n": 10}]
    seed = await atom_seed_text(conn, "a1", "t1")
    assert seed == ("Label\nLocation: Pawangka Monastery, Tibet (do not name any other country or region for it)\n"
                    "Day 4 of the trip\nSource itinerary: Own source.\nSeason: Best Oct–Nov\n"
                    "Other trips that include this same moment (for context only — never promise these details "
                    "for this trip):\n- A sibling.\n- B sibling.")
    assert conn.fetchrow.call_args_list[0].args[1:] == ("a1", "t1")
    sib_query = conn.fetch.call_args.args[0]
    assert "v_active_tour_atoms" in sib_query and "NOT sib.is_alias" in sib_query
    assert "rt.country IS NOT DISTINCT FROM mrt.country" in sib_query


@pytest.mark.asyncio
async def test_atom_seed_text_none_when_not_visible():
    conn = AsyncMock()
    conn.fetchrow.return_value = None
    assert await atom_seed_text(conn, "a1", "t1") is None
    conn.fetch.assert_not_called()


def test_location_line_needs_a_country():
    assert "Location" not in compose_seed("x", None, None, None, [], place="P", country=None)
    assert "Location: Bhutan (" in compose_seed("x", None, None, None, [], place=None, country="Bhutan")
