"""AA-1047 (S272 adversarial review 2) — the counter-examples that are not synthetic policy twins: real-country
short-meta tails (the place keeps its own country), the country alias, the prompt registry's module-alias reads, the
policy lint's new forms and the admin sentence check running F17 / F18. The synthetic fire / no-fire twins live in
services/quality/writing_policy.yaml (`examples`) and run in test_aa1047_contradictions.py.
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import patch
from uuid import uuid4

import pytest

from services.content_generation import seo_meta_utils as smu
from services.quality import policy_harness as ph
from services.quality import writing_policy as wp

HALONG_SEED = ("Ha Long Bay — cruise\n"
               "Location: Ha Long Bay, Quang Ninh, Vietnam (do not place it in any other town, region or country)\n"
               "Source itinerary: Cruise Ha Long Bay, kayak into a lagoon and visit Sung Sot Cave.")
HALONG_META = ("Cruise Ha Long Bay past limestone karsts and floating villages, then kayak into a quiet lagoon and "
               "climb to the chambers of Sung Sot Cave.")
ANGKOR_SEED = ("Angkor Wat — sunrise visit\n"
               "Location: Angkor Wat, Siem Reap, Cambodia (do not place it in any other town, region or country)\n"
               "Source itinerary: Visit Angkor Wat outside Siem Reap with a local guide.")
ANGKOR_META = ("Visit Angkor Wat outside Siem Reap with a local guide, walk its long galleries of carved reliefs and "
               "see the temple towers from the moat.")


# ---------------------------------------------------------------- T9 short-meta tail: the place's own country


def _fit(meta: str, seed: str, destination: str, trip_name: str):
    title = "A Day Among the Temples and the Bay: What to Expect"
    with patch("services.acp_content_writing.service.fit_seo_fields", return_value=({}, 0.0)):
        from services.acp_content_writing import service as svc
        return asyncio.run(svc._fit_seo_meta({"seo_title": title, "meta_description": meta}, None, body="b",
                                             context={"destination": destination, "atom_text": seed,
                                                      "trip_name": trip_name}, request_id=uuid4()))


def test_cross_border_trip_never_puts_ha_long_bay_in_laos():
    lo, _ = wp.band("meta")
    assert len(HALONG_META) < lo
    out, _cost = _fit(HALONG_META, HALONG_SEED, "Laos", "Mekong Discovery")
    assert "Laos" not in out["meta_description"] and out["meta_description"] == HALONG_META


def test_own_country_tail_still_composes_when_destination_is_the_place_country():
    out, cost = _fit(HALONG_META, HALONG_SEED, "Vietnam", "Ha Long Bay Cruise")
    lo, hi = wp.band("meta")
    assert cost == 0.0 and out["meta_description"].endswith(", in Vietnam.")
    assert lo <= len(out["meta_description"]) <= hi


@pytest.mark.parametrize("destination,expect", [("Thailand", None), ("Cambodia", "Cambodia"),
                                                ("Kingdom of Cambodia", "Kingdom of Cambodia"), ("Laos", None),
                                                ("", None)])
def test_place_country_tail_reads_the_seed_location_line(destination, expect):
    got = smu.place_country_tail(destination, ANGKOR_SEED)
    assert got == (destination if expect else None)
    if got is None:
        assert smu.extend_short_meta(ANGKOR_META, {"country": got}) is None


def test_angkor_never_gets_a_thailand_tail():
    out, _ = _fit(ANGKOR_META, ANGKOR_SEED, "Thailand", "Thailand and Cambodia Temples")
    assert "Thailand" not in out["meta_description"]


def test_no_location_line_means_no_tail():
    assert smu.place_country_tail("Vietnam", "Ha Long Bay — cruise\nSource itinerary: Cruise the bay.") is None


def test_alias_spelling_of_the_named_country_gets_no_second_tail():
    meta = ("Explore Vietnam's Hoi An old town lanes, its lantern-lit riverside and tailor shops, then cycle out "
            "to the rice fields beyond town.")
    got = smu.extend_short_meta(meta, {"country": "Viet Nam"})
    assert got is None or "Viet Nam" not in got


# ---------------------------------------------------------------- prompt registry: module alias reads


def test_reads_policy_follows_a_module_alias(tmp_path):
    from shared.llm_client import prompt_registry as pr
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "bands.py").write_text("from services.quality import writing_policy as _wp\n"
                                  "SEO_META_MIN, SEO_META_MAX = _wp.band('meta')\nPLAIN = 3\n", encoding="utf-8")
    (pkg / "alias_from.py").write_text("from pkg import bands as smu\n"
                                       "P = f'Write {smu.SEO_META_MIN} characters.'\n", encoding="utf-8")
    (pkg / "alias_import.py").write_text("import pkg.bands as smu2\n"
                                         "P = f'Write up to {smu2.SEO_META_MAX} characters.'\n", encoding="utf-8")
    (pkg / "alias_plain.py").write_text("from pkg import bands as b\nP = f'{b.PLAIN} lines'\n", encoding="utf-8")
    (pkg / "alias_in_helper.py").write_text("from pkg import bands as b\nP = 'x'\n"
                                            "def build():\n    return f'{b.SEO_META_MAX}'\n", encoding="utf-8")
    assert pr.reads_policy("pkg/alias_from.py", tmp_path)
    assert pr.reads_policy("pkg/alias_import.py", tmp_path)
    assert not pr.reads_policy("pkg/alias_plain.py", tmp_path)      # a constant that does not read the policy
    # a helper reading one counts for the stage that hashes that helper, not for the whole file
    assert not pr.reads_policy("pkg/alias_in_helper.py", tmp_path)
    assert pr.symbols_read_policy("pkg/alias_in_helper.py", ["build"], tmp_path)
    assert not pr.symbols_read_policy("pkg/alias_in_helper.py", ["P"], tmp_path)
    compiled = tmp_path / pr.POLICY_COMPILED
    compiled.parent.mkdir(parents=True)
    compiled.write_text(json.dumps({"policy_sha": "a" * 16}), encoding="utf-8")
    before = [pr.prompt_hash_of("pkg/alias_from.py", ["P"], root=tmp_path),
              pr.prompt_hash_of("pkg/alias_in_helper.py", ["build"], root=tmp_path)]
    compiled.write_text(json.dumps({"policy_sha": "b" * 16}), encoding="utf-8")
    after = [pr.prompt_hash_of("pkg/alias_from.py", ["P"], root=tmp_path),
             pr.prompt_hash_of("pkg/alias_in_helper.py", ["build"], root=tmp_path)]
    assert before[0] != after[0] and before[1] != after[1]


# ---------------------------------------------------------------- policy lint: the hand-restatement forms


def _compiled() -> dict:
    return json.loads(wp.render_compiled())


@pytest.mark.parametrize("src", [
    "meta_max = 155\n",
    "def trim(meta_description):\n    return meta_description[:155]\n",
    "def ok(description):\n    return 140 <= len(description) <= 155\n",
    'SEO_LIMITS = {"meta": (140, 155), "title": (45, 60)}\n',
    "def fit(meta, limit=155):\n    return meta\n",
    'x = "meta description up to {} characters".format(155)\n',
])
def test_lint_catches_python_band_restatements(src):
    from services.quality import policy_lint as pl
    assert pl.scan_python("services/x.py", src, _compiled())


@pytest.mark.parametrize("src", ["def fetch(timeout=60):\n    pass\n", "items = rows[:60]\n", "PAGE_SIZE = 60\n"])
def test_lint_leaves_unrelated_numbers(src):
    from services.quality import policy_lint as pl
    assert pl.scan_python("services/x.py", src, _compiled()) == []


@pytest.mark.parametrize("src,hit", [("export const META_MAX = 155;", True),
                                     ('<textarea maxLength={155} name="meta" />', True),
                                     ("const BANDS = { meta: [140, 155] }", True),
                                     ("const PAGE = 60;", False)])
def test_lint_catches_ts_band_restatements(src, hit):
    from services.quality import policy_lint as pl
    assert bool(pl.scan_ts("frontend/a.ts", src, _compiled())) is hit


def test_lint_catches_yaml_min_max_under_a_field():
    from services.quality import policy_lint as pl
    found = pl.scan_yaml("services/x.yaml", "meta:\n  min: 140\n  max: 155\nother:\n  max: 155\n", _compiled())
    assert [f.split(":")[1] for f in found] == ["2", "3"]


# ---------------------------------------------------------------- admin sentence check runs F17 / F18


@pytest.mark.parametrize("seed_key,text,gate", [
    ("lake", "You cross Lake Orvine at dawn.", "F17_trip_fact"),
    ("monastery", "Most travellers never find their way to Corran Monastery.", "F18_rarity"),
    ("monastery", "Few groups ever reach the library hall, but yours will.", "F18_rarity"),
])
def test_check_replacement_refuses_what_f17_f18_refuse(seed_key, text, gate):
    from services.acp_content_writing import sentence_repair as sr
    seed = ph._seed(wp.load_source()["example_seeds"][seed_key])
    wi = {"seed": seed, "trip": "Velmora Highlands Walk", "cta": ph.DEFAULT_CTA}
    checks = sr.check_replacement(text, ph.grounding_text(seed), {"brand_name": ph.BRAND}, writer_input=wi)
    assert any(c["gate"] == gate and c["blocking"] for c in checks), checks


def test_check_replacement_skips_piece_level_trip_fact_checks():
    """A single clean sentence is never 'a thin moment' or 'missing the family caveat' (piece-level checks)."""
    from services.acp_content_writing import sentence_repair as sr
    seed = ph._seed(wp.load_source()["example_seeds"]["lake"])
    wi = {"seed": seed, "trip": "Velmora Highlands Walk", "cta": ph.DEFAULT_CTA}
    checks = sr.check_replacement("The ferry crosses Lake Orvine to Brecca village.", ph.grounding_text(seed),
                                  {"brand_name": ph.BRAND, "customer_segment": "Families with children aged 7-16"},
                                  writer_input=wi)
    assert [c for c in checks if c["gate"] in ("F17_trip_fact", "F18_rarity")] == []


# ---------------------------------------------------------------- one trip-frame predicate


@pytest.mark.parametrize("text,frame", [
    ("The ferry crossing to Brecca takes 40 minutes.", True),
    ("The boat ride to Brecca leaves at 7 am.", True),
    ("Overnight in Brecca is 2 nights at the guesthouse.", True),
    ("The visit ends with 3 tastings of monastery wine.", True),
    ("The climb to Corran Monastery takes 3 hours.", True),
    ("The monastery stands 900 metres above the lake.", False),
    ("Corran Monastery was founded in 1937 by monks from the coast.", False),
    # S272 review 3: fail closed — 'boats' is a trip noun, and a number with no background signal is in the frame
    ("Brecca harbour holds 300 fishing boats.", True),
    ("The library hall holds 900 handwritten books.", False),
    ("Lake Orvine is 40 km long.", False),
    ("Only 0.4% of the valley is forest.", False),
    ("Brecca has been a fishing village since 1820.", False),
    ("Afterwards, a 20-minute descent leads back to Orvine.", True),
    ("Boats pull away from the Brecca jetty at 6.30 sharp.", True),
    ("A small boat holds 12 hikers and a skipper.", True),
    ("At most 8 hikers share each dormitory in Brecca.", True),
    ("Brecca lies 2 hours away by water.", True),
    ("Getting back to Orvine is a 3-hour affair.", True),
    ("2 hours are set aside in the library hall.", True),
    ("Brecca lies 40 km from Orvine.", True),
    ("The gate is 300 metres from the guesthouse.", True),
])
def test_trip_number_frame(text, frame):
    from services.acp_content_writing import claim_rules as cr
    assert cr.trip_number_frame(text, ("Cross Lake Orvine by ferry to Brecca village.",)) is frame


def test_audience_line_supports_an_age_only():
    from services.acp_content_writing import claim_rules as cr
    src, aud = ["Walk up to Corran Monastery."], ["AUDIENCE: Walkers aged 25-50"]
    assert cr.novel_trip_numbers("The walk takes 50 minutes.", src, aud) == ["50"]
    assert cr.novel_trip_numbers("The walk suits walkers aged 25 to 50.", src, aud) == []


@pytest.mark.parametrize("text", [
    "Lake Orvine is 1.25 km wide.", "Only 0.45% of the valley is forest.", "The tower rises 12.50 m above the lake."])
def test_a_decimal_measure_is_not_a_clock_time(text):
    from services.acp_content_writing import claim_rules as cr
    assert not cr._CLOCK_RE.search(text)


# ---------------------------------------------------------------- rarity: one trip-vs-place test (review 3)


@pytest.mark.parametrize("seed_key,text", [
    ("lake", "The ferry crossing to Brecca is unparalleled anywhere on earth."),
    ("monastery", "The library hall visit is the only one of its kind in the world."),
    ("monastery", "Corran's library hall tour is unique in the world."),
    ("monastery", "Walking up to Corran Monastery is an experience unmatched in Asia."),
])
def test_rarity_blocks_agrees_with_the_trip_frame(seed_key, text):
    """F18's trip-vs-place test is claim_rules.trip_attached: a rarity claim on a crossing, a visit, a tour or a walk
    holds in F18 (and so in check_replacement), never only warns while the number frame calls the sentence a trip's."""
    from services.acp_content_writing import claim_rules as cr, hygiene
    from services.acp_content_writing import quality_gates as qg
    from services.acp_content_writing.trip_facts import TripFactContext, rarity_blocks, rarity_claims
    seed = ph._seed(wp.load_source()["example_seeds"][seed_key])
    lines = cr.trip_lines_of(hygiene.split_seed(seed)[0])
    found = rarity_claims(text, hygiene.split_seed(seed)[0])
    assert found and all(rarity_blocks(v, lines) for v in found)
    assert cr.trip_attached(text, lines)
    ctx = TripFactContext(seed=seed, cta=ph.DEFAULT_CTA, channel="blog", brand_name=ph.BRAND)
    f18 = [g for g in qg.gate_trip_facts(text, ctx) if g["gate"] == qg.F18_GATE][0]
    assert not f18["passed"] and f18["blocking"]


def test_place_rarity_still_warns():
    from services.acp_content_writing import claim_rules as cr
    from services.acp_content_writing.trip_facts import rarity_blocks, rarity_claims
    text = "Corran Monastery is one of the last walled monasteries in the world."
    found = rarity_claims(text, "Walk up to Corran Monastery above Orvine and visit its library hall.")
    assert found and not any(rarity_blocks(v, ("Walk up to Corran Monastery above Orvine.",)) for v in found)
    assert cr.trip_lines_of("") == ()


# ---------------------------------------------------------------- T9 meta tail: every Location line (review 3)


def _route_seed() -> str:
    from services.acp_shared.atom_seed import compose_seed
    return (compose_seed("Luang Prabang — old town walk", "Walk the old town of Luang Prabang.", None, None, [],
                         place="Luang Prabang", country="Laos", day=2)
            + "\n\n"
            + compose_seed("Hanoi Old Quarter — walk", "Walk the Hanoi Old Quarter lanes.", None, None, [],
                           place="Hanoi Old Quarter", country="Vietnam", day=7))


HANOI_META = ("Take a slow Hanoi old quarter walk through narrow lanes of street food stalls, temples and shophouses "
              "on the final day of the journey.")


def test_cross_border_route_never_puts_hanoi_in_laos():
    """The reviewer's reproduction: a route seed whose first moment is in the DESTINATION (Laos) and whose later
    moment is in Vietnam; a short meta about Hanoi got ', in Laos.' from the first Location line."""
    seed = _route_seed()
    assert smu.location_countries(seed) == ["Laos", "Vietnam"]
    assert smu.place_country_tail("Laos", seed) is None
    assert smu.place_country_tail("Vietnam", seed) is None
    out, cost = _fit(HANOI_META, seed, "Laos", "Mekong Journey: Luang Prabang to Hanoi")
    assert cost == 0.0 and "Laos" not in out["meta_description"] and out["meta_description"] == HANOI_META


def test_route_segments_are_read_too():
    """A route segment in another country (passed as context route_segments) also stops the tail."""
    seed = ph._seed(wp.load_source()["example_seeds"]["monastery_card"])
    other = ph._seed(wp.load_source()["example_seeds"]["harbour_card"])
    assert smu.place_country_tail("Velmora", seed) == "Velmora"
    assert smu.place_country_tail("Velmora", seed, other) is None
    assert smu.place_country_tail("Velmora", seed, seed) == "Velmora"


def test_a_location_line_without_a_country_part_means_no_tail():
    seed = HALONG_SEED + "\n\nCat Ba — boat\nLocation: Cat Ba (do not place it in any other town, region or country)"
    assert smu.place_country_tail("Vietnam", seed) is None


# ---------------------------------------------------------------- review 4: card-backed Location lines only


HANOI_SHORT = ("Wander the lantern-lit lanes of Hanoi's Old Quarter on a guided walk past tube houses, street food "
               "stalls and quiet temple courtyards.")


def test_card_less_fallback_line_never_puts_hanoi_in_laos():
    """S272 review 4 reproduction: an atom with no place card gets the fallback 'Location: <place>, <raw_tours.country>'
    line, and raw_tours.country is the DESTINATION itself; the tail compared the destination with itself and wrote
    ', in Laos.' after a meta about Hanoi. Only a card's Location line counts."""
    from services.acp_shared.atom_seed import compose_seed
    seed = compose_seed("Stroll the lanes of Hanoi's Old Quarter", None, None, None, [], place="Hanoi Old Quarter",
                        country="Laos", card=None)
    assert "(do not name any other country or region for it)" in seed
    lo, _ = wp.band("meta")
    assert len(HANOI_SHORT) < lo
    assert smu.place_country_tail("Laos", seed) is None
    out, cost = _fit(HANOI_SHORT, seed, "Laos", "Mekong Discovery")
    assert cost == 0.0 and out["meta_description"] == HANOI_SHORT and "Laos" not in out["meta_description"]


def test_card_line_with_the_place_country_still_composes():
    from services.acp_shared.atom_seed import compose_seed
    card = {"display_name": "Hanoi Old Quarter", "country": "Vietnam", "status": "verified", "locality": "Hanoi"}
    seed = compose_seed("Hanoi Old Quarter — walk", "Walk the Hanoi Old Quarter lanes.", None, None, [],
                        place="Hanoi Old Quarter", country="Laos", card=card)
    assert smu.place_country_tail("Laos", seed) is None
    assert smu.place_country_tail("Vietnam", seed) == "Vietnam"


def test_a_label_only_route_block_means_no_tail():
    """route_moment_seeds keeps a label-only block for an atom whose seed cannot be read: no Location line in that
    block, so the route's country is unknown and no tail is written (fail closed)."""
    seed = HALONG_SEED + "\n\nHa Long Bay overnight cruise"
    assert smu.place_country_tail("Vietnam", HALONG_SEED) == "Vietnam"
    assert smu.place_country_tail("Vietnam", seed) is None
    assert smu.place_country_tail("Vietnam", HALONG_SEED, "Ha Long Bay overnight cruise") is None


def test_halong_and_angkor_with_cards_keep_their_own_country():
    assert smu.place_country_tail("Laos", HALONG_SEED) is None
    assert smu.place_country_tail("Thailand", ANGKOR_SEED) is None
    assert smu.place_country_tail("Cambodia", ANGKOR_SEED, HALONG_SEED) is None


# ---------------------------------------------------------------- review 4: the signal is bound to each number


LAKE_LINES = ("Lake Orvine — ferry crossing", "Day 3 of the trip",
              "Source itinerary: Cross Lake Orvine by ferry to Brecca village and walk the shore path to the "
              "guesthouse.")
MONASTERY_LINES = ("Corran Monastery — visit", "Day 2 of the trip",
                   "Source itinerary: Walk up to Corran Monastery above Orvine and visit its library hall with a local "
                   "guide.")


@pytest.mark.parametrize("lines,text,frame", [
    (MONASTERY_LINES, "Built in 1388, the monastery gate is reached by 1,200 stone steps.", True),
    (MONASTERY_LINES, "Rising 900 metres above the lake, the monastery gate is reached in four stages.", True),
    (LAKE_LINES, "The shore path to Brecca is 6 km long.", True),
    (LAKE_LINES, "Brecca is 9 km across the water from Orvine.", True),
    (LAKE_LINES, "Brecca sits 9 km across the water from Orvine.", True),
    (MONASTERY_LINES, "The path to the monastery gate rises 400 metres from Orvine.", True),
    (MONASTERY_LINES, "Corran Monastery stands 900 metres above Orvine.", True),
    (LAKE_LINES, "The old sea wall is 2 km long.", False),
    (MONASTERY_LINES, "Built in 1388, the monastery gate stands 30 metres high.", False),
    (MONASTERY_LINES, "The monastery stands 900 metres above the lake.", False),
    (LAKE_LINES, "Lake Orvine is 40 km long.", False),
    (LAKE_LINES, "Lake Orvine is 9 km across.", False),
])
def test_number_frame_is_per_number(lines, text, frame):
    from services.acp_content_writing import claim_rules as cr
    assert cr.trip_number_frame(text, lines) is frame


def test_a_year_never_launders_a_count():
    from services.acp_content_writing import claim_rules as cr
    text = "Built in 1388, the monastery gate is reached by 1,200 stone steps."
    assert cr.number_has_background_signal(text, "1388")
    assert not cr.number_has_background_signal(text, "1200")
    assert cr.trip_number_frame(text, (), novel=["1388"]) is True   # 'steps' is a route noun: the trip
    plain = "Built in 1388, the monastery gate holds 1,200 old icons."
    assert cr.trip_number_frame(plain, (), novel=["1388", "1200"]) is False
    assert cr.trip_number_frame("Built in 1388, the gate is 1200 paces from the square.", (),
                                novel=["1388", "1200"]) is True
    assert not cr.number_has_background_signal("The gate is reached by 1200 steps.", "1200")


@pytest.mark.parametrize("lines,text", [
    (LAKE_LINES, "The shore path to Brecca has no real equivalent anywhere on earth."),
    (LAKE_LINES, "Arrival at Brecca by water is unmatched in Velmora."),
    (LAKE_LINES, "An evening at the Brecca inn is unique in the world."),
    (MONASTERY_LINES, "The way up to Corran Monastery is unmatched anywhere on earth."),
    (MONASTERY_LINES, "The approach to Corran Monastery is unparalleled in Asia."),
    (MONASTERY_LINES, "The welcome at Corran Monastery is unmatched in Velmora."),
])
def test_route_stay_and_experience_are_the_trip(lines, text):
    from services.acp_content_writing import claim_rules as cr
    assert cr.trip_attached(text, lines)


@pytest.mark.parametrize("lines,text", [
    (MONASTERY_LINES, "Corran Monastery is one of the last walled monasteries in the world."),
    (MONASTERY_LINES, "The library hall is unparalleled in Velmora."),
    (LAKE_LINES, "Lake Orvine has no real equivalent anywhere on earth."),
])
def test_a_place_or_its_feature_is_not_the_trip(lines, text):
    from services.acp_content_writing import claim_rules as cr
    assert not cr.trip_attached(text, lines)


def test_own_line_route_object_ties_a_sentence_to_the_trip():
    from services.acp_content_writing import claim_rules as cr
    harbour = ("Source itinerary: Ride a fishing boat around Dunmere Harbour and walk the old sea wall.",)
    assert cr._own_route_objects(harbour) == {"boat", "wall"}
    assert cr.trip_attached("The sea wall is unmatched anywhere on earth.", harbour)
    # a place the trip walks or hikes stays a place: its background (an age, a height) still warns
    assert cr._own_route_objects(("Walk the old town of Luang Prabang.", "Hike the extinct volcano.")) == set()
    assert not cr.trip_attached("The town keeps 30 temples.", ("Walk the old town of Luang Prabang.",))
    assert not cr.trip_attached("The volcano went quiet nine thousand years ago.", ("Hike the extinct volcano.",))
