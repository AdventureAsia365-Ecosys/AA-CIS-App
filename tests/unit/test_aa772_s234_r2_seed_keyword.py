"""AA-772 (S234 round 2) — seed hygiene across place cards (one value per fact, an overnight card
the day's geography contradicts, within-day walk order) and SEO keyword / meta grounding."""
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.acp_content_writing import service
from services.acp_content_writing.quality_gates import gate_seo_surface
from services.acp_content_writing.seo_grounding import meta_unsupported_products, unsupported_products
from services.acp_shared.atom_seed import canonicalize_route_cards
from services.acp_shared.place_cards import merged_facts
from services.acp_shared.seed_canon import (canonical_facts, facts_conflict, is_overnight,
                                            overnight_card_off_route, quantities)

pytestmark = pytest.mark.real_atom_seed

SOD_2PCT = ("Lake Khövsgöl, which the camp overlooks, holds approximately two percent of the world's fresh surface "
            "water and is one of the deepest lakes in Asia.")
LAKE_04 = "It holds an estimated 70 percent of Mongolia's fresh water and about 0.4 percent of the world's fresh surface water."


# ---------------------------------------------------------------- fact conflicts

def test_quantities_read_ranges_spelled_numbers_and_the_of_word():
    assert [(q[0], q[1], q[2], q[4]) for q in quantities(LAKE_04)] == [(70, 70, "percent", "mongolia"),
                                                                       (0.4, 0.4, "percent", "world")]
    assert quantities(SOD_2PCT)[0][:3] == (2, 2, "percent")
    assert quantities("It last erupted 20,000–24,000 years ago.")[0][:3] == (20000, 24000, "year")
    assert quantities("It was built in 1637–1638 by Zhabdrung.")[0][2] == "year"
    assert quantities("It is one of the deepest lakes in Asia.") == []


def test_facts_conflict_same_measure_different_numbers():
    assert facts_conflict(SOD_2PCT, LAKE_04)  # 2% vs 0.4% of the world's fresh surface water
    assert facts_conflict("It holds 1–2% of the world's fresh water.", LAKE_04)


def test_facts_do_not_conflict_on_different_measures_or_overlapping_values():
    assert not facts_conflict("It holds 1–2% of the world's fresh water.", SOD_2PCT)  # 2 is inside 1–2
    assert not facts_conflict("It holds 70 percent of Mongolia's fresh water.",
                              "It holds 1–2% of the world's fresh water.")  # Mongolia's vs the world's
    assert not facts_conflict("The volcano last erupted approximately 20,000–24,000 years ago.",
                              "A dormant volcano that erupted approximately 24,000 years ago.")
    assert not facts_conflict("Lake Khövsgöl is geologically ancient.", "Uran Togoo is geologically recent.")


def _card(cid, name, facts, region="Khövsgöl Province", status="auto_checked"):
    return {"card_id": cid, "display_name": name, "locality": None, "region": region, "country": "Mongolia",
            "naming_note": None, "status": status, "facts": [{"text": f} for f in facts]}


def test_canonical_facts_keeps_the_card_with_more_atoms():
    sod = _card("sod", "Sod Camp", ["It has gers.", SOD_2PCT])
    lake = _card("lake", "Lake Khövsgöl", [LAKE_04, "It freezes in winter."])
    kept, dropped = canonical_facts([sod, lake], {"sod": 1, "lake": 5})
    assert [f["text"] for f in kept["sod"]] == ["It has gers."]
    assert [f["text"] for f in kept["lake"]] == [LAKE_04, "It freezes in winter."]
    assert dropped == [{"card_id": "sod", "fact": SOD_2PCT, "kept_card_id": "lake", "kept_fact": LAKE_04}]


def test_canonical_facts_prefers_verified_then_first():
    a = _card("a", "Lake Khövsgöl", ["It holds 2% of the world's fresh surface water."])
    b = _card("b", "Khövsgöl Lake", ["It holds 0.4% of the world's fresh surface water."], status="verified")
    kept, _ = canonical_facts([a, b], {"a": 3, "b": 1})
    assert kept["a"] == [] and len(kept["b"]) == 1
    b["status"] = "auto_checked"
    kept, _ = canonical_facts([a, b], {"a": 1, "b": 1})
    assert len(kept["a"]) == 1 and kept["b"] == []


def test_canonical_facts_ignores_the_same_measure_of_two_different_places():
    lake = _card("lake", "Lake Khövsgöl", ["The lake sits at an elevation of roughly 1,645 metres above sea level."])
    moron = _card("moron", "Mörön", ["The city sits in a broad valley at an elevation of approximately 1,283 "
                                     "metres above sea level."])
    kept, dropped = canonical_facts([lake, moron])
    assert dropped == [] and len(kept["lake"]) == len(kept["moron"]) == 1


def test_merge_never_adds_a_conflicting_donor_fact():
    out = merged_facts([{"text": LAKE_04}], [[{"text": SOD_2PCT}, {"text": "It freezes in winter."}]])
    assert [f["text"] for f in out] == [LAKE_04, "It freezes in winter."]


# ---------------------------------------------------------------- overnight card + day order

def _parts():
    sod = _card("sod", "Sod Camp", ["It faces the Khoridol Saridag range.", SOD_2PCT])
    uran = _card("uran", "Uran Togoo Volcano", ["It is the largest of several dormant volcanoes."], region="Bulgan Province")
    moron = _card("moron", "Mörön", ["Mörön is the capital of Khövsgöl Province."])
    lake = _card("lake", "Lake Khövsgöl", [LAKE_04])
    return [
        {"atom_id": "u", "text": "Uran Togoo — trek into the caldera", "day": 3, "card": uran, "country": "Mongolia"},
        {"atom_id": "s", "text": "Sod Camp — overnight in a standard ger camp", "day": 3, "card": sod,
         "place": "Sod Camp", "country": "Mongolia"},
        {"atom_id": "m", "text": "Moron — stop for lunch", "day": 4, "card": moron, "country": "Mongolia"},
        {"atom_id": "l1", "text": "Lake Khövsgöl — visit", "day": 4, "card": lake, "country": "Mongolia"},
        {"atom_id": "l2", "text": "Khövsgöl Lake — boat rides", "day": 5, "card": lake, "country": "Mongolia"},
    ]


def test_overnight_card_in_another_region_than_the_day_is_dropped():
    parts = _parts()
    assert is_overnight(parts[1]["text"]) and not is_overnight(parts[0]["text"])
    assert overnight_card_off_route(parts[1], parts[:2])
    assert not overnight_card_off_route(parts[1], [parts[1]])  # alone on its day: nothing to compare
    out, report = canonicalize_route_cards(parts)
    assert out[1]["card"] is None and report["overnight_cards_dropped"] == ["Sod Camp"]
    assert report["facts_dropped"] == []  # the 2% line left with the card


def test_canonical_facts_apply_when_the_overnight_card_stays():
    parts = _parts()
    parts[0]["card"] = {**parts[0]["card"], "region": "Khövsgöl Province"}  # same region: the card is kept
    out, report = canonicalize_route_cards(parts)
    assert out[1]["card"] is not None
    assert [f["text"] for f in out[1]["card"]["facts"]] == ["It faces the Khoridol Saridag range."]
    assert [d["fact"] for d in report["facts_dropped"]] == [SOD_2PCT]


def test_route_parts_untouched_without_conflicts():
    parts = [{"atom_id": "a", "text": "A — visit", "day": 1, "card": _card("a", "Punakha Dzong", ["Built 1637."])}]
    out, report = canonicalize_route_cards(parts)
    assert out[0]["card"]["facts"] == parts[0]["card"]["facts"]
    assert report == {"overnight_cards_dropped": [], "facts_dropped": []}


def _pool(conn):
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    return pool


@pytest.mark.asyncio
async def test_route_walk_within_a_day_overnight_last_then_extraction_order():
    t = lambda m: datetime(2026, 10, 1, 0, m, tzinfo=timezone.utc)  # noqa: E731
    conn = AsyncMock()
    conn.fetchrow.side_effect = [
        {"atom_id": "sod", "text": "Sod Camp — overnight in a standard ger camp", "itinerary_day": 3,
         "created_at": t(1), "excluded": False},
        {"atom_id": "moron", "text": "Moron — stop for lunch", "itinerary_day": 4, "created_at": t(5), "excluded": None},
        {"atom_id": "spring", "text": "Mineral spring — visit", "itinerary_day": 3, "created_at": t(3), "excluded": None},
        {"atom_id": "uran", "text": "Uran Togoo — trek", "itinerary_day": 3, "created_at": t(2), "excluded": None},
    ]
    with patch.object(service, "route_moment_seeds", new=AsyncMock(side_effect=lambda c, a, t: list(a))):
        out = await service._fetch_route_segments(uuid.uuid4(), "trip", ["s1", "s2", "s3", "s4"], _pool(conn))
    assert [a for a, _ in out] == ["uran", "spring", "sod", "moron"]
    assert "ta.created_at" in conn.fetchrow.call_args.args[0]


# ---------------------------------------------------------------- keyword grounding

PUNAKHA = ("Punakha Dzong — visit courtyards and assembly hall\n"
           "Location: Punakha Dzong, Punakha, Punakha District, Bhutan (do not place it in any other town, region or "
           "country)\nDay 4 of the trip\nSource itinerary: Visit Punakha Dzong's courtyards and assembly hall.\n"
           "Place background (checked facts about the place itself — you may use these; they say nothing about what "
           "this trip does):\n- It remains the winter residence of the Je Khenpo.")


def test_country_in_a_location_line_with_a_parenthetical_is_not_a_place_match():
    # S234: 'bhutan' matched because the country was read inside '(… region or country)'.
    assert service.choose_keyword(["Bhutan meditation retreat"], PUNAKHA) is None
    assert service.choose_keyword(["bhutan travel"], PUNAKHA) is None


def test_keyword_with_ungrounded_product_falls_to_the_next_candidate():
    kws = ["punakha dzong meditation retreat", "punakha dzong history"]
    assert service.choose_keyword(kws, PUNAKHA) == "punakha dzong history"


def test_keyword_falls_back_to_location_only():
    assert service.choose_keyword(["punakha dzong meditation retreat"], PUNAKHA) == "punakha dzong"
    assert service.choose_keyword(["punakha yoga retreat"], PUNAKHA) is None  # one word left


def test_grounded_product_keyword_is_kept():
    seed = ("Lake Khövsgöl — take boat rides on the water\nLocation: Lake Khövsgöl, Khövsgöl Province, Mongolia "
            "(do not place it elsewhere)\nSource itinerary: Optional activities include boat rides and trekking.")
    assert service.choose_keyword(["khovsgol lake boat trip"], seed) == "khovsgol lake boat trip"
    assert service.choose_keyword(["khovsgol lake hiking"], seed) == "khovsgol lake hiking"  # hike ~ trek
    assert service.choose_keyword(["khovsgol kayak tour", "khovsgol lake mongolia"], seed) == "khovsgol lake mongolia"


def test_unsupported_products_stems():
    assert unsupported_products("Bhutan meditation retreat", PUNAKHA) == ["meditat", "retreat"]
    assert unsupported_products("a diverse landscape", "x") == []  # 'diverse' is not 'diving'
    assert unsupported_products("game drives and a spa", "the spa opens daily") == ["game drive"]


# ---------------------------------------------------------------- meta product nouns

META = ("Discover a Bhutan meditation retreat at Punakha Dzong, where WildKind Travel guides you through this "
        "sacred working monastery and its cultural significance.")


def test_meta_product_nouns_must_be_in_seed_or_body():
    assert meta_unsupported_products(META, PUNAKHA, "A post about the dzong.") == ["meditation", "retreat",
                                                                                   "guides you"]
    assert meta_unsupported_products(META, PUNAKHA, "Monks keep a meditation retreat here; our guides walk you in.") \
        == []
    assert meta_unsupported_products(None, PUNAKHA, "") == []


def test_f4_reports_an_ungrounded_meta_product_only_with_a_seed():
    title = "Punakha Dzong: Bhutan's Palace of Great Happiness Guide"
    meta = ("Discover a Bhutan meditation retreat at Punakha Dzong, the winter seat of the Je Khenpo, with its "
            "courtyards and assembly hall.")
    with_seed = gate_seo_surface(title, meta, "punakha-dzong", None, seed=PUNAKHA, body="The dzong.")
    assert any("promises 'meditation'" in v for v in with_seed["violations"])
    assert any("promises 'retreat'" in v for v in with_seed["violations"])
    without = gate_seo_surface(title, meta, "punakha-dzong", None)
    assert not any("promises" in v for v in without["violations"])


@pytest.mark.asyncio
async def test_seo_fit_never_keeps_an_ungrounded_meta_and_names_the_words():
    body = "Punakha Dzong sits where two rivers meet; the Je Khenpo winters here. " * 3
    seo = {"seo_title": "Punakha Dzong: Where Two Rivers Meet in Bhutan Today", "meta_description": META, "slug": "s"}
    calls = []

    def fake_fit(**kw):
        calls.append(kw)
        return {"seo_title": None, "meta_description": "Discover a Bhutan meditation retreat at Punakha Dzong, where "
                "two rivers meet below the winter home of the Je Khenpo, Bhutan's chief abbot."}, 0.003

    with patch.object(service, "fit_seo_fields", side_effect=fake_fit):
        out, _ = await service._fit_seo_meta(seo, None, body=body, context={"atom_text": PUNAKHA}, request_id="r")
    assert calls and {"meditation", "retreat", "guides you"} <= set(calls[0]["forbidden_words"])
    assert out["meta_description"] == META  # neither fitted meta is grounded: F4 reports the writer's own
