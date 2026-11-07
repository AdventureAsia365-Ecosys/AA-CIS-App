"""AA-1047 (Quality W1, root ADR 0020) — the FactSheet module (services/quality/factsheet.py).

Parity first: the route builder renders byte for byte what service.grounding_text_from joined before W1 (the frozen
copy below is that code, unchanged), over hand fixtures and a generated grid of inputs; the tour builder renders the
S1 writer v2 FACT SHEET. Then the lines (ids, kinds, provenance, anchors, pointers) and the recorded exclusions. Fixtures use invented places and names only. Pure: no DB, no LLM."""
from __future__ import annotations

import itertools
from typing import Optional

import pytest

from services.acp_content_writing.claim_rules import number_source_lines
from services.content_generation import s1_writer_v2 as v2
from services.quality import factsheet as fs
from services.quality.s1_facts import build_fact_table


def legacy_grounding_text_from(*, seed: str, facts_text: Optional[str], brand_audience: Optional[dict],
                               trip_name: Optional[str], destination: Optional[str]) -> str:
    """service.grounding_text_from as it was on CIS main before AA-1047 (frozen; do not edit)."""
    parts = [seed, facts_text or ""]
    audience = brand_audience or {}
    if audience.get("brand_name"):
        parts.append(f"BRAND: {audience['brand_name']}")
    if audience.get("core_idea"):
        parts.append(f"BRAND CORE IDEA: {audience['core_idea']}")
    if audience.get("customer_segment"):
        parts.append(f"AUDIENCE (who the brand writes for — not a fact about this trip): "
                     f"{audience['customer_segment']}")
    if trip_name:
        parts.append(f"TRIP: {trip_name}")
    if destination:
        parts.append(f"DESTINATION: {destination}")
    return "\n\n".join(p for p in parts if p)


BG = ("Place background (checked facts about the place itself — you may use these; "
      "they say nothing about what this trip does):")
SIB = ("Other trips that include this same moment (for context only — never promise "
       "these details for this trip):")

SEED_ONE = "\n".join([
    "Lumen Gorge — canyon walk",
    "Location: Varn Valley, Zemlia (do not place it in any other town, region or country)",
    "Day 2 of the trip",
    "Source itinerary: Walk the rim path of Lumen Gorge to the Tavo lookout.",
    "Status: included",
    "Why it matters: the gorge is the deepest cut in the Varn plateau",
    "Season: best from May to September",
    BG,
    "- Lumen Gorge is about 900 m deep at its narrowest point.",
    "- The Tavo lookout was built by herders in 1937.",
    SIB,
    "- Cross the gorge by the rope bridge at dawn.",
    "- Picnic lunch at the Tavo lookout.",
])
SEED_TWO = "\n".join([
    "Orsa Lake — boat ride",
    "Location: Orsa, Zemlia",
    "Day 3 of the trip: Orsa Lake — boat ride — Source itinerary: Row across Orsa Lake to the island chapel.",
    "Day 4 of the trip: Orsa village — homestay — Source itinerary: Overnight with a family in Orsa village.",
    "Why it matters: the chapel island is reachable only by boat",
    BG,
    "- Orsa Lake freezes for about three months a year.",
    "Thin moment: say less rather than invent.",
])
FACTS = "\n\n".join([
    "[Fact id=11111111-aaaa-4bbb-8ccc-000000000001]\nEntry permit: Zemlia issues a 30-day e-visa.\n"
    "Source: Zemlia border office; platform fact",
    "[Fact id=11111111-aaaa-4bbb-8ccc-000000000002]\nPrice: from 1,250 USD per person.\nSource: agency fact",
    "[Fact id=11111111-aaaa-4bbb-8ccc-000000000003]\nSeason: rains in October.",
])
BRAND = {"brand_name": "Trailwise", "core_idea": "Slow trips with local hosts.",
         "customer_segment": "families with children aged 7–16"}


SEEDS = ["", "Lumen Gorge — canyon walk", SEED_ONE, SEED_ONE + "\n\n" + SEED_TWO,
         "[Moment id=atom_00af646e46]\n" + SEED_ONE + "\n\n[Moment id=atom_00af646e47]\n" + SEED_TWO,
         SEED_ONE + "\n", "\n\n" + SEED_TWO + "\n\n"]
FACTS_TEXTS = [None, "", FACTS, "[Fact id=x]\nOne-line fact without a source."]
AUDIENCES = [None, {}, BRAND, {"brand_name": "Trailwise"}, {"core_idea": "Hosts first."},
             {"customer_segment": "solo walkers", "brand_name": ""}]
TRIPS = [None, "", "Zemlia: Varn Valley and Orsa Lake — 6 Days"]
DESTS = [None, "", "Zemlia"]


def _route(seed, facts_text, aud, trip, dest, **kw):
    return fs.build_route_factsheet(seed=seed, facts_text=facts_text, brand_audience=aud, trip_name=trip,
                                    destination=dest, **kw)


# ------------------------------------------------------------------ parity

@pytest.mark.parametrize("seed,facts_text,aud,trip,dest",
                         list(itertools.product(SEEDS, FACTS_TEXTS, AUDIENCES, TRIPS, DESTS)))
def test_route_render_is_byte_equal_to_legacy_grounding_text(seed, facts_text, aud, trip, dest):
    expected = legacy_grounding_text_from(seed=seed, facts_text=facts_text, brand_audience=aud, trip_name=trip,
                                          destination=dest)
    assert _route(seed, facts_text, aud, trip, dest).render() == expected


def test_service_grounding_text_from_delegates_to_the_factsheet():
    from services.acp_content_writing import service as svc

    kw = dict(seed=SEED_ONE, facts_text=FACTS, brand_audience=BRAND, trip_name="Zemlia Loop", destination="Zemlia")
    assert svc.grounding_text_from(**kw) == legacy_grounding_text_from(**kw)
    assert svc.grounding_factsheet(**kw).render() == svc.grounding_text_from(**kw)
    wi = {"seed": SEED_ONE, "facts": FACTS, "brand_pack": BRAND, "trip": "Zemlia Loop", "writer_trip": "Zemlia",
          "destination": "Zemlia"}
    assert svc.grounding_from_writer_input(wi) == legacy_grounding_text_from(
        seed=SEED_ONE, facts_text=FACTS, brand_audience=BRAND, trip_name="Zemlia", destination="Zemlia")


TOUR_DAYS = {
    "name": "Varn Valley Walk", "country": "Zemlia", "duration": "4 days",
    "summary": "A walk through the Varn Valley. Contact Varn Trails Ltd for details.",
    "description": "Four days on foot between Lumen Gorge and Orsa Lake.",
    "highlights": ["Rim path of Lumen Gorge", "Boat to the Orsa island chapel"],
    "itineraries": ("Day 1: Arrive in Tavo\nMeet the guide in Tavo. (D)\n"
                    "Day 2-3: Lumen Gorge\nWalk 12 km along the rim each day.\nOvernight: Tavo Lodge\n"
                    "Day 4: Orsa Lake\nRow to the chapel island, then transfer out. (B, L)\n"),
    "inclusions": "Breakfast and dinner\nLodge nights\nGuide", "exclusions": "Lunches\nTips",
    "price": "1,250 USD",
}
TOUR_HOURS = {"name": "Half day at Orsa Lake", "country": "Zemlia", "duration": "4 hours",
              "itineraries": "09:00 – Meet at the Orsa pier\n10:30 – Row to the chapel island\n"
                             "Optional swim if time permits.",
              "inclusions": "Boat\nGuide", "exclusions": "Drinks"}
TOUR_NO_MARKERS = {"name": "Three days in the Varn hills", "country": "Zemlia", "duration": "3 days",
                   "itineraries": "Walk through the hills for three days and sleep in village homes."}
TOUR_EMPTY: dict = {}
AGENCY = {"group_size": "2-12", "best_time_to_go": "May to September"}


@pytest.mark.parametrize("tour", [TOUR_DAYS, TOUR_HOURS, TOUR_NO_MARKERS, TOUR_EMPTY])
@pytest.mark.parametrize("agency", [None, {}, AGENCY])
def test_tour_render_is_byte_equal_to_the_s1_writer_fact_sheet(tour, agency):
    t = v2.with_agency_fields(tour, agency)
    facts = build_fact_table(t)
    expected = v2.render_fact_sheet(t, facts, v2.day_plan(t, facts))
    assert fs.build_tour_factsheet(tour, agency=agency).render() == expected


# ------------------------------------------------------------------ route lines

def test_route_lines_keep_the_claim_rules_ids_and_carry_provenance():
    sheet = _route(SEED_ONE + "\n\n" + SEED_TWO, FACTS, BRAND, "Zemlia Loop", "Zemlia")
    legacy_ids = [sl.id for sl in number_source_lines(sheet.render())]
    assert [ln.id for ln in sheet.lines] == legacy_ids
    prov = {ln.id: ln.provenance for ln in sheet.lines}
    kinds = {ln.kind for ln in sheet.lines}
    assert kinds >= {"S", "L", "P", "O", "F", "B", "A", "T"}
    for ln in sheet.lines:
        if ln.kind in ("S", "L", "P", "O"):
            assert ln.provenance == fs.LLM_DERIVED, ln
        if ln.kind in ("B", "A"):
            assert ln.provenance == fs.TENANT_ASSERTED, ln
    facts = {ln.ref: ln.provenance for ln in sheet.lines if ln.kind == "F"}
    assert facts == {"11111111-aaaa-4bbb-8ccc-000000000001": fs.HUMAN_VERIFIED,
                     "11111111-aaaa-4bbb-8ccc-000000000002": fs.TENANT_ASSERTED,
                     # no owner on the entry: the weaker reading
                     "11111111-aaaa-4bbb-8ccc-000000000003": fs.TENANT_ASSERTED}
    t_lines = {ln.text: ln.provenance for ln in sheet.lines if ln.kind == "T"}
    assert t_lines == {"TRIP: Zemlia Loop": fs.LLM_DERIVED, "DESTINATION: Zemlia": fs.SOURCE_RAW}
    assert prov["P1"] == fs.LLM_DERIVED and set(sheet.by_provenance()) <= set(fs.PROVENANCES)


def test_fact_scopes_override_the_entry_owner():
    sheet = _route("", FACTS, None, None, None,
                   fact_scopes={"11111111-aaaa-4bbb-8ccc-000000000003": "platform",
                                "11111111-aaaa-4bbb-8ccc-000000000001": "tenant"})
    by_ref = {ln.ref: ln.provenance for ln in sheet.lines}
    assert by_ref["11111111-aaaa-4bbb-8ccc-000000000003"] == fs.HUMAN_VERIFIED
    assert by_ref["11111111-aaaa-4bbb-8ccc-000000000001"] == fs.TENANT_ASSERTED


def test_block_anchors_mark_the_seed_lines_of_their_block_only():
    sheet = _route(SEED_ONE + "\n\n" + SEED_TWO, FACTS, None, None, None, block_anchored=[True, False])
    for ln in sheet.lines:
        if ln.kind in ("S", "L"):
            assert ln.anchored is (ln.block == 1), ln
        else:
            assert ln.anchored is None, ln
    assert {ln.block for ln in sheet.unanchored()} == {2}
    unknown = _route(SEED_ONE + "\n\n" + SEED_TWO, None, None, None, None, block_anchored=[True])
    assert all(ln.anchored is None for ln in unknown.lines if ln.block == 2)


def test_excluded_facts_are_recorded_with_a_reason_and_the_render_is_unchanged():
    dropped = [{"fact_id": "f-9", "scope": "tenant", "title": "Deal", "body": "Ten percent off in May."}]
    sheet = _route(SEED_ONE, FACTS, None, None, None, excluded_facts=dropped)
    assert sheet.render() == legacy_grounding_text_from(seed=SEED_ONE, facts_text=FACTS, brand_audience=None,
                                                        trip_name=None, destination=None)
    assert sheet.excluded == ({"id": "fact:f-9", "kind": "F", "reason": fs.EXCLUDED_FACTS_BUDGET, "scope": "tenant",
                               "chars": len("Deal") + len("Ten percent off in May.")},)


# ------------------------------------------------------------------ tour lines

def test_tour_lines_point_into_the_source_and_verify():
    sheet = fs.build_tour_factsheet(TOUR_DAYS, agency=AGENCY)
    assert sheet.pointer_failures == ()
    kinds = {ln.kind for ln in sheet.lines}
    assert {"days", "included", "excluded", "numbers", "duration"} <= kinds
    assert all(ln.provenance == fs.SOURCE_RAW for ln in sheet.lines)
    ids = [ln.id for ln in sheet.lines]
    assert len(ids) == len(set(ids)) and "days.1" in ids and "included.1" in ids
    inc = [ln for ln in sheet.lines if ln.kind == "included"]
    assert inc[0].field == "inclusions" and inc[0].text in inc[0].quote
    # the agency field is a source line too (with_agency_fields)
    assert any("2-12" in ln.text for ln in sheet.lines if ln.kind == "numbers")
    assert sheet.meta["day_plan"] == "source_days" and sheet.meta["days"] == 4


def test_tour_provenance_is_settable_and_checked():
    assert {ln.provenance for ln in fs.build_tour_factsheet(TOUR_HOURS, provenance=fs.HUMAN_VERIFIED).lines} == {
        fs.HUMAN_VERIFIED}
    with pytest.raises(ValueError):
        fs.build_tour_factsheet(TOUR_HOURS, provenance="made_up")


def test_figures_over_the_cap_are_recorded_as_excluded():
    many = {"name": "Count walk", "country": "Zemlia", "duration": "1 day",
            "itineraries": " ".join(f"Then walk {n} km." for n in range(1, v2.MAX_FIGURES + 11))}
    sheet = fs.build_tour_factsheet(many)
    caps = [e for e in sheet.excluded if e["reason"] == fs.EXCLUDED_FIGURES_CAP]
    n_figures = sum(1 for ln in sheet.lines if ln.kind == "numbers")
    assert n_figures > v2.MAX_FIGURES and len(caps) == n_figures - v2.MAX_FIGURES
    assert all(f"{e['id']}" not in sheet.render() for e in caps)
    assert all(e["id"].startswith("numbers.") for e in caps)
    assert fs.build_tour_factsheet(TOUR_DAYS).excluded == ()


# ------------------------------------------------------------------ sha, record, anchors, raw rows

def test_sha_is_stable_and_moves_with_any_change():
    a = _route(SEED_ONE, FACTS, BRAND, "Zemlia Loop", "Zemlia")
    b = _route(SEED_ONE, FACTS, BRAND, "Zemlia Loop", "Zemlia")
    assert a.sha() == b.sha() and len(a.sha()) == 64
    assert a.sha() != _route(SEED_ONE, FACTS, BRAND, "Zemlia Loop", "Zemlia Two").sha()
    assert a.sha() != _route(SEED_ONE, FACTS, BRAND, "Zemlia Loop", "Zemlia", block_anchored=[False]).sha()
    rec = a.record()
    assert rec["schema"] == fs.SCHEMA and rec["builder"] == fs.BUILDER_ROUTE
    assert all(set(ln) >= {"id", "kind", "text", "provenance"} for ln in rec["lines"])
    t = fs.build_tour_factsheet(TOUR_DAYS)
    assert t.sha() == fs.build_tour_factsheet(TOUR_DAYS).sha() != fs.build_tour_factsheet(TOUR_HOURS).sha()


RAW = ("Day 2: Lumen Gorge. Walk the rim path of Lumen Gorge to the Tavo lookout, about 12 km. "
       "Day 4: Row across Orsa Lake to the island chapel.")


@pytest.mark.parametrize("evidence,label,expected", [
    ("Walk the rim path of Lumen Gorge to the Tavo lookout", "Lumen Gorge — walk", True),   # verbatim
    ("walk  the RIM path of lumen gorge", None, True),                                       # folded substring
    ("Rim path walk at Lumen Gorge towards Tavo", None, True),                               # word overlap
    ("Hot-air balloon flight over the Kesra dunes", None, False),                           # not in the source
    (None, "Orsa Lake island chapel", True),                                                # label fallback
    (None, None, False),
])
def test_anchor_check(evidence, label, expected):
    assert fs.anchor_check(evidence, label, RAW) is expected


def test_anchor_check_without_a_raw_source_is_unknown():
    assert fs.anchor_check("anything", None, "") is None
    assert fs.anchor_check("anything", None, None) is None


def test_raw_source_tour_reads_a_row_and_parses_json_highlights():
    row = {"src_name": "Varn Valley Walk", "src_highlights": '["Rim path", "Chapel island"]',
           "src_itineraries": "Day 1: Tavo", "country": "Zemlia", "duration": "4 days", "price_raw": "1,250 USD",
           "inclusions": "Guide", "exclusions": "Tips", "group_size": "2-12", "best_time_to_go": None}
    tour, agency = fs.raw_source_tour(row)
    assert tour["highlights"] == ["Rim path", "Chapel island"] and tour["price"] == "1,250 USD"
    assert tour["name"] == "Varn Valley Walk" and tour["subtitle"] is None
    assert agency == {"group_size": "2-12", "best_time_to_go": None}
    assert "Group size: 2-12" in fs.raw_source_text(tour, agency)
    tour2, _ = fs.raw_source_tour({**row, "src_highlights": "not json ["})
    assert tour2["highlights"] == "not json ["
