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
         "Walk to Pawangka, one of the oldest monasteries near Lhasa."],
    )
    assert seed.splitlines()[0] == "Pawangka Monastery — visit"
    assert "Source itinerary: Visit Pawangka Monastery, built in the seventh century" in seed
    assert "Why it matters: ancient sacred site" in seed
    assert "Season:" not in seed
    assert seed.count("seventh century") == 1  # sibling duplicate dropped
    assert "- Walk to Pawangka, one of the oldest monasteries near Lhasa." in seed


def test_compose_label_only_when_no_fields():
    assert compose_seed("Tiger's Nest — hike", None, " ", None, []) == "Tiger's Nest — hike"


def test_siblings_capped():
    seed = compose_seed("x", None, None, None, [f"ev {i}" for i in range(10)])
    assert seed.count("\n- ") == MAX_SIBLINGS


@pytest.mark.asyncio
async def test_atom_seed_text_reads_atom_and_siblings():
    conn = AsyncMock()
    conn.fetchrow.return_value = {"text": "Label", "evidence": "Own source.", "emotional_hook": None,
                                  "season_note": "Best Oct–Nov"}
    conn.fetch.return_value = [{"evidence": "B sibling."}, {"evidence": "A sibling."}]
    seed = await atom_seed_text(conn, "a1", "t1")
    assert seed == ("Label\nSource itinerary: Own source.\nSeason: Best Oct–Nov\n"
                    "Other itineraries with the same moment:\n- A sibling.\n- B sibling.")
    assert conn.fetchrow.call_args.args[1:] == ("a1", "t1")
    assert "atom_segment_member" in conn.fetch.call_args.args[0]


@pytest.mark.asyncio
async def test_atom_seed_text_none_when_not_visible():
    conn = AsyncMock()
    conn.fetchrow.return_value = None
    assert await atom_seed_text(conn, "a1", "t1") is None
    conn.fetch.assert_not_called()
