"""AA-768 (S234 round 4) — final-pass text integrity (one CTA, no duplicate, no fragment, normalised
whitespace), thin-moment weight, background place as the trip's base (FAQ included), trip-place
whitelist, hazard polarity for family brands, SEO title uniqueness (round-3 panel §4 fixes 1, 2, 3, 4,
5, 8). Sentences are taken from the S234 round-3 pieces the panel scored."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.acp_content_writing import hygiene as h
from services.acp_content_writing import quality_gates as qg
from services.acp_content_writing import sentence_repair as sr
from services.acp_content_writing import service
from services.acp_content_writing import text_integrity as ti
from services.acp_content_writing import trip_facts as tf

TERRA_AUDIENCE = "Affluent families with children aged 7–16\nParents who want active holidays with safety"
AH_AUDIENCE = "High-net-worth couples, private families, family offices, senior executives"
TERRA_CTA = "Ask Terra Family Expeditions to plan this trip for your family"
AH_CTA = "Talk to an Atlas & Hearth travel designer about this journey"
BG = "Place background (checked facts about the place itself — you may use these; they say nothing about what this trip does):"

SEED = f"""Uran Togoo — trek into the caldera of a dormant volcano
Location: Uran Togoo Volcano, Bulgan, Bulgan Province, Mongolia
Day 3 of the trip
Source itinerary: Journey to Uran Togoo, a dormant volcano.

Moron — stop for lunch
Location: Mörön, Mörön, Khövsgöl Province, Mongolia
Day 4 of the trip
Source itinerary: Stop for lunch near Moron, capital of Khövsgöl province.
{BG}
- It serves as the main hub and gateway town for travellers heading to Khövsgöl Lake.
- Mörön has a small domestic airport with scheduled flights connecting it to Ulaanbaatar.

Lake Khövsgöl — visit the boundary between Central Asian steppes and Siberian Taiga forests
Location: Lake Khövsgöl, Khövsgöl Nuur National Park, Khövsgöl Province, Mongolia
Day 4 of the trip: Lake Khövsgöl — visit the boundary — Source itinerary: The lake sits at the boundary.
{BG}
- Visitors typically arrive via the nearby town of Khatgal at the lake's southern tip, which serves as the main gateway and base for boat trips and horse trekking.
- Summers are short and cool; sudden storms can arise quickly on the lake, making navigation in small boats potentially hazardous.

ruins of an ancient monastery — visit
Location: ruins of an ancient monastery, Mongolia
Day 14 of the trip
Source itinerary: visit the ruins of an ancient monastery destroyed during the political purges of 1937.
Why it matters: encounter historical loss and cultural memory
Thin moment: only 1 source fact — mention it in one sentence at most; never make it the hook, the title or the climax."""


def _hyg(cta=AH_CTA, brand="Atlas & Hearth", **kw):
    return h.HygieneContext(seed=SEED, brand_name=brand, cta=cta, **kw)


def _tctx(audience=TERRA_AUDIENCE, cta=TERRA_CTA, trip_places=("Ulaanbaatar", "Gobi Desert")):
    return tf.TripFactContext(seed=SEED, audience=audience, cta=cta, trip_places=trip_places)


# ---------------------------------------------------------------- fix 6: normaliser + r3 #3

class TestNormalise:
    def test_blank_lines_spaces_and_headings(self):
        raw = "\n\n  ## Lake Khövsgöl\nThe lake is large.   \n   It sits high.\n\n\n\n- a list item\n  - nested item\n## FAQ\n"
        out, log = ti.normalise(raw)
        assert out == ("## Lake Khövsgöl\n\nThe lake is large.\nIt sits high.\n\n- a list item\n  - nested item\n\n"
                       "## FAQ")
        assert log

    def test_clean_text_is_unchanged_and_not_logged(self):
        assert ti.normalise("## A\n\nOne line.\n") == ("## A\n\nOne line.", [])

    def test_leading_blank_lines_no_longer_hide_a_long_hook(self):
        # r3 #3: two blank lines, then a 283-character first line → the split never ran
        line = ("Before you step into the inner temples, cameras and bags stay behind. Modest dress is required. "
                "These aren't rules you can work around on the day — they're conditions the site sets.")
        ctx = h.HygieneContext(seed="Tiger's Nest — hike", first_line_max=140)
        out, log = h.autofix("\n\n" + line + "\n\nMore.", ctx)
        assert out.split("\n", 1)[0] == "Before you step into the inner temples, cameras and bags stay behind."
        assert "split the first line after its first sentence" in log


# ---------------------------------------------------------------- fix 1a: one CTA

R3_12_CLOSE = ("## Plan This Journey with Atlas & Hearth\n\nTalk to an Atlas & Hearth travel designer about how this "
               "journey might be arranged. Talk to an Atlas & Hearth travel designer about this journey.\n\n## FAQ\n\n"
               "**Q: Is it far?**\nA: Yes.")


class TestOneCta:
    def test_reworded_cta_next_to_the_literal_is_deleted(self):
        out, log = ti.fix_cta("The lake is large.\n\n" + R3_12_CLOSE, AH_CTA)
        assert "about how this journey might be arranged" not in out
        assert "Talk to an Atlas & Hearth travel designer about this journey." in out
        assert log and ti.cta_findings(out, AH_CTA) == []

    def test_findings_name_the_problem(self):
        assert "reworded" not in " ".join(ti.cta_findings("No call here.", AH_CTA))
        assert "missing" in ti.cta_findings("No call here.", AH_CTA)[0]
        twice = f"{AH_CTA}. The lake is large.\n\n{AH_CTA}."
        assert "2 times" in ti.cta_findings(twice, AH_CTA)[0]
        assert any("other words" in f for f in ti.cta_findings("The lake.\n\n" + R3_12_CLOSE, AH_CTA))

    def test_second_literal_line_is_deleted(self):
        out, _ = ti.fix_cta(f"{AH_CTA}.\n\nThe lake is large.\n\n{AH_CTA}.", AH_CTA)
        assert out.count("Talk to an Atlas") == 1 and out.endswith(f"{AH_CTA}.")

    def test_the_cta_is_not_exempt_from_the_f13_repeat_gate(self):
        res = qg.gate_repeat("The lake is large.\n\n" + R3_12_CLOSE, _hyg())
        assert not res["passed"] and any("other words" in v for v in res["violations"])


# ---------------------------------------------------------------- fix 1b: duplicates

R3_19_STEPPE = ("At this ecological boundary, the landscape itself becomes a teaching tool: the steppe is open and "
                "legible; the taiga is dense and enclosed. The steppe is open. The taiga is dense. The lake connects them.")
R3_19_SOD = ("After Uran Togoo, the route continues to Sod Camp, where families spend the night in a standard ger camp. "
             "The camp sits opposite a mountain range with trekking access nearby, and there is a mineral spring with "
             "reputed healing properties in the area. Sod Camp offers a quieter rhythm, with a mountain range nearby "
             "for optional trekking and a mineral spring in the area.")
R3_19_REGISTER = ("Descending into a caldera on foot — feeling the scale of volcanic rock, seeing the depth, noticing how "
                  "the surrounding grasslands and forest sit above the crater floor — is something children and adults "
                  "register immediately. The dark volcanic rock, the green steppe grasslands and patches of forest that "
                  "surround the cone, the visible depth of the crater: these are things that register immediately, for "
                  "adults and younger travellers alike.")


class TestDuplicates:
    @pytest.mark.parametrize("text,kind,later", [
        (R3_19_STEPPE, "contained", "The steppe is open."),
        (R3_19_SOD, "restate", "Sod Camp offers a quieter rhythm"),
        (R3_19_REGISTER, "phrase", "The dark volcanic rock"),
    ])
    def test_r3_19_paraphrased_duplicates_are_found_and_deleted(self, text, kind, later):
        dups = h.near_duplicates(text)
        assert any(d["kind"] == kind and d["later"]["clean"].startswith(later) for d in dups)
        out, log = h.dedupe(text)
        assert later not in out and log

    def test_steppe_line_keeps_its_last_sentence(self):
        out, _ = h.dedupe(R3_19_STEPPE)
        assert out.endswith("the taiga is dense and enclosed. The lake connects them.")

    def test_list_items_and_distinct_sentences_are_not_duplicates(self):
        text = ("Optional activities include boat rides, horseback riding and visits to nomadic families.\n\n"
                "- Horseback riding\n- Boat rides\n\nThe lake freezes from January. Summers are short and cool.")
        assert not [d for d in h.near_duplicates(text) if d["kind"] in ("contained", "restate", "phrase")]

    def test_a_far_closing_recap_is_deleted_by_dedupe(self):
        # r3 #12: the CTA paragraph opened on a recap of the intro sentence
        intro = ("The route through northern Mongolia — from the volcanic terrain of Bulgan Province to the reindeer "
                 "herders of the remote taiga — is among the journeys the region offers.")
        recap = ("This route through northern Mongolia — from the volcanic terrain of Bulgan Province to the reindeer "
                 "herders of the remote taiga — rewards those who travel with patience.")
        text = f"{intro}\n\nThe lake is large. It is cold. It freezes. The camps move.\n\n{recap} {AH_CTA}."
        out, log = h.dedupe(text, cta=AH_CTA)
        assert recap not in out and out.endswith(f"{AH_CTA}.") and log

    def test_a_mid_body_repeat_that_adds_a_figure_is_left_for_the_repair(self):
        a = "Lake Khövsgöl holds about seventy percent of Mongolia's fresh water in a high mountain-rimmed basin."
        b = ("Frozen from January to April, the high mountain-rimmed basin of Lake Khövsgöl holds about seventy "
             "percent of Mongolia's fresh water and 0.4 percent of the world's.")
        text = f"{a}\n\nThe camps move. The herds graze. The forest is dense. The rivers run.\n\n{b}\n\n## Plan\n\n{AH_CTA}."
        out, _ = h.dedupe(text, cta=AH_CTA)
        assert b in out and h.find_issues(out, _hyg())["repeat"]

    def test_far_repeat_is_now_blocking_repeat_not_a_hygiene_warning(self):
        a = "Lake Khövsgöl holds about seventy percent of Mongolia's fresh water in a high mountain-rimmed basin."
        b = "Lake Khövsgöl holds about seventy percent of Mongolia's fresh water in a high mountain basin today."
        # 'It' after the copy blocks the safe delete, so the repeat is still there for the gate
        text = f"{a}\n\nThe camps move. The herds graze. The forest is dense. The rivers run.\n\n{b} It matters."
        found = h.find_issues(text, _hyg())
        assert any(b[:60] in v for v in found["repeat"])
        assert not any("repeats" in v for v in found["hygiene"])


# ---------------------------------------------------------------- fix 1c: fragments

class TestFragments:
    def test_lower_case_sentence_is_a_fragment_and_is_deleted_unless_it_is_the_hook(self):
        text = "The lake is large. they mean that it matters. The camps move."
        assert [f["text"] for f in ti.fragments(text)] == ["they mean that it matters."]
        out, log = ti.fix_fragments(text)
        assert out == "The lake is large. The camps move." and log
        assert ti.fix_fragments("draft 1")[0] == "draft 1"  # the only sentence stays

    def test_unbalanced_paragraph_is_reported_not_cut(self):
        text = "The lake (the largest in the region is cold.\n\nThe camps move."
        assert ti.fragments(text)[0]["why"].startswith("has an unbalanced")
        assert ti.fix_fragments(text)[0] == text

    def test_a_repair_that_leaves_a_fragment_is_reverted(self):
        before = "The lake is large. The camps move with the herds. The forest is dense."
        item = {"start": before.index("The camps"), "end": before.index(" The forest"),
                "sentence": "The camps move with the herds.", "problem": "x"}
        out = sr.splice_checked(before, [item], ["the camps move (with herds."])
        assert out == before and item["guard"] == "integrity"


# ---------------------------------------------------------------- fix 1d: the pass + the stored piece

class TestFinalPass:
    def test_residue_is_the_f13_repeat_list(self):
        text = "The lake (the largest in the region is cold.\n\n" + AH_CTA + "."
        out, log, residue = ti.final_pass(text, _hyg())
        assert residue == h.find_issues(out, _hyg())["repeat"] == qg.gate_repeat(out, _hyg())["violations"]
        assert residue

    def test_r3_19_shape_is_clean_after_the_pass(self):
        text = f"## Where\n\n{R3_19_STEPPE}\n\n## Day 3\n\n{R3_19_REGISTER}\n\n{R3_19_SOD}\n\n{TERRA_CTA}.\n"
        out, log, residue = ti.final_pass(text, _hyg(cta=TERRA_CTA, brand="Terra Family Expeditions"))
        assert residue == [] and len(log) >= 4
        for gone in ("The steppe is open.", "The dark volcanic rock", "Sod Camp offers a quieter rhythm"):
            assert gone not in out

    def test_final_integrity_holds_an_approved_piece_with_a_residue(self):
        ledger = [{"gate": "F13_repeat", "passed": True, "blocking": True, "violations": []}]
        text = "The lake (the largest in the region is cold.\n\n" + AH_CTA + "."
        out, led, flags, status, reason = service._final_integrity(text, _hyg(), ledger, [], "approved", None)
        assert status == "held" and reason.startswith("F13_repeat")
        assert [g for g in led if g["gate"] == "F13_repeat"][0]["passed"] is False and len(led) == 1

    def test_final_integrity_logs_edits_and_keeps_a_clean_piece_approved(self):
        text = "\n\nThe lake is large.   \n\n" + AH_CTA + "."
        out, led, flags, status, reason = service._final_integrity(text, _hyg(), [], [], "approved", None)
        assert status == "approved" and out.startswith("The lake is large.\n")
        assert flags[0]["gate"] == "F13_final_pass" and flags[0]["blocking"] is False


# ---------------------------------------------------------------- fix 2: thin moment

THIN_BODY = ("## Lake Khövsgöl\n\nThe lake sits at the boundary.\n\n## Day Fourteen\n\n"
             "The journey closes with a visit to the ruins of an ancient monastery, destroyed during the political "
             "purges of 1937 — a quiet coda to two weeks spent moving through a landscape where loss and endurance "
             "have long occupied the same ground.\n\n## Plan This Journey\n\n" + TERRA_CTA + ".")


class TestThinMoment:
    def test_seed_note_marks_the_moment_thin(self):
        ms = tf.parse_moments(SEED)
        assert [m.name for m in ms if m.thin] == ["ruins of an ancient monastery"]

    def test_heading_and_coda_are_findings(self):
        ms = tf.parse_moments(SEED)
        found = tf.thin_moment_issues(THIN_BODY, ms, TERRA_CTA)
        assert any("its own heading" in f and "Day Fourteen" in f for f in found)
        assert any("closes on a thin moment" in f for f in found)

    def test_fix_merges_the_heading_and_cuts_the_coda(self):
        ms = tf.parse_moments(SEED)
        out, log = tf.fix_thin_moments(THIN_BODY, ms, TERRA_CTA)
        assert "## Day Fourteen" not in out and len(log) == 2
        assert ("The journey closes with a visit to the ruins of an ancient monastery, destroyed during the political "
                "purges of 1937.") in out
        assert tf.thin_moment_issues(out, ms, TERRA_CTA) == []

    def test_a_plain_closing_mention_is_fine(self):
        ms = tf.parse_moments(SEED)
        text = ("## Lake\n\nThe lake sits at the boundary. On Day 14, families visit the ruins of an ancient monastery "
                "destroyed during the political purges of 1937.\n\n" + TERRA_CTA + ".")
        assert tf.thin_moment_issues(text, ms, TERRA_CTA) == []

    def test_the_first_heading_is_never_deleted(self):
        ms = tf.parse_moments(SEED)
        text = "## The Ruins of 1937\n\nThe ruins of an ancient monastery stand there.\n\nThe lake sits at the boundary."
        out, _ = tf.fix_thin_moments(text, ms)
        assert out.startswith("## The Ruins of 1937")
        assert tf.thin_moment_issues(out, ms)

    def test_single_moment_piece_is_never_checked(self):
        one = SEED.split("\n\n")[-1]
        assert tf.thin_moment_issues("## The Ruins\n\nThe ruins of an ancient monastery.", tf.parse_moments(one)) == []

    def test_findings_reach_f17(self):
        res = tf.find_trip_fact_issues(THIN_BODY, _tctx())
        assert any("thin moment" in v for v in res["trip_fact"])


# ---------------------------------------------------------------- fix 4: hazard polarity

STORM = ("One important note: summers here are short and cool, and sudden storms can arise quickly on the lake. That is "
         "not a limitation; it is simply how travel on a high-altitude alpine lake works.")


class TestHazardPolarity:
    def test_family_brand_minimising_a_seed_hazard_is_found_and_the_reassurance_deleted(self):
        hits = tf.minimised_hazards(STORM, _tctx())
        assert len(hits) == 1 and hits[0]["pure"]
        out, log = tf.fix_minimised_hazards(STORM, _tctx())
        assert out == "One important note: summers here are short and cool, and sudden storms can arise quickly on the lake."
        assert any("plays down a hazard" in v for v in tf.find_trip_fact_issues(STORM, _tctx())["trip_fact"])

    def test_not_for_a_brand_without_children(self):
        assert tf.minimised_hazards(STORM, _tctx(audience=AH_AUDIENCE, cta=AH_CTA)) == []

    def test_not_without_a_hazard_nearby(self):
        text = "Cameras stay behind. Bags stay behind. These are not obstacles — they are preparation."
        assert tf.minimised_hazards(text, _tctx()) == []


# ---------------------------------------------------------------- fix 3: background place as base, whitelist

FAQ = ("## FAQ\n\n**Q: Is the lake big?**\nA: Yes, very big.\n\n**Q: What town is the main base?**\nA: Khatgal, at the "
       "lake's southern tip, serves as the primary gateway and base for boat trips.\n\n**Q: How do we fly in?**\n"
       "A: Mörön has a small domestic airport with scheduled flights from Ulaanbaatar. From there, the town of Khatgal "
       "at the lake's southern tip serves as the gateway for boat trips.\n\n**Q: Is it cold?**\nA: Yes, in winter.")


class TestBackgroundBase:
    def test_khatgal_as_gateway_is_found_in_the_faq_ulaanbaatar_and_moron_are_not(self):
        hits = tf.background_bases(FAQ, _tctx())
        assert [x["place"] for x in hits] == ["Khatgal", "Khatgal"]
        assert all("Ulaanbaatar" not in x["span"]["clean"] and "Mörön" not in x["span"]["clean"] for x in hits)

    def test_fix_drops_the_pair_or_the_sentence(self):
        out, log = tf.fix_background_bases(FAQ, _tctx())
        assert "Khatgal" not in out and "What town is the main base" not in out and len(log) == 2
        assert "Mörön has a small domestic airport" in out and qg.faq_pair_count(out) == 3

    def test_pair_is_kept_when_the_faq_would_fall_below_the_contract(self):
        small = FAQ.split("\n\n**Q: Is it cold?**")[0].replace("**Q: Is the lake big?**\nA: Yes, very big.\n\n", "")
        out, _ = tf.fix_background_bases(small, _tctx())
        assert "What town is the main base" in out  # 2 pairs: left for the sentence repair (F17)

    def test_trip_place_named_in_background_is_whitelisted(self):
        lex = h.build_scope_lexicon(SEED, trip_places=("Ulaanbaatar", "Gobi Desert"))
        assert h.places_named(lex, "Mörön has flights connecting the region to Ulaanbaatar.") == []
        assert h.places_named(lex, "The Gobi feels far away.") == ["Gobi"]


# ---------------------------------------------------------------- fix 5: titles

class TestTitles:
    def test_title_key_ignores_case_accents_and_punctuation(self):
        assert qg.title_taken("Lake Khovsgol: Mongolia's Family Adventure Hub",
                              ["Lake Khövsgöl: Mongolia’s family adventure hub"])
        assert not qg.title_taken("Lake Khövsgöl: Where Two Worlds Meet", ["Lake Khövsgöl: Family Adventure Hub"])

    def test_f4_reports_a_title_the_brand_already_used(self):
        res = qg.gate_seo_surface("Lake Khövsgöl: Mongolia's Family Adventure Hub",
                                  "Discover Lake Khövsgöl with your family: a volcano, a vast lake and reindeer herders "
                                  "in the taiga of northern Mongolia.", "lake-khovsgol", None,
                                  taken_titles=["Lake Khövsgöl: Mongolia's Family Adventure Hub"])
        assert any("same as an earlier piece" in v for v in res["violations"])

    @pytest.mark.asyncio
    async def test_seo_fit_is_asked_for_a_new_title_and_falls_back_to_a_brand_suffix(self):
        title = "Lake Khövsgöl: A Family Travel Guide North"  # 42 chars: + ' | Terra' (50) fits the band
        meta = ("Discover Lake Khövsgöl with your family: a volcano, a vast lake and reindeer herders in the taiga "
                "of northern Mongolia.")
        ctx = {"atom_text": SEED, "brand_audience": {"brand_name": "Terra"}, "tenant_id": None}
        with patch.object(service, "fit_seo_fields", return_value=({"seo_title": title, "meta_description": meta}, 0.0)
                          ) as fit:
            out, _ = await service._fit_seo_meta({"seo_title": title, "meta_description": meta, "slug": "x"}, None,
                                                 body="b", context=ctx, request_id="r",
                                                 taken_titles=[title, "Other Title"])
        assert fit.call_args.kwargs["taken_titles"][0] == title
        assert out["seo_title"] == f"{title} | Terra"

    @pytest.mark.asyncio
    async def test_sibling_titles_is_one_read_and_fails_open(self):
        conn = AsyncMock()
        conn.fetch.return_value = [{"seo_title": "A title"}, {"seo_title": None}]
        pool = MagicMock()
        pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
        pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
        import uuid
        assert await service._sibling_titles(str(uuid.uuid4()), "blog", uuid.uuid4(), pool) == ["A title"]
        assert conn.fetch.await_args.args[2] == "blog"
        conn.fetch.side_effect = RuntimeError("db down")
        assert await service._sibling_titles(str(uuid.uuid4()), "blog", uuid.uuid4(), pool) == []
        assert await service._sibling_titles(None, "blog", uuid.uuid4(), pool) == []


# ---------------------------------------------------------------- detector: r3 #18 borderline social proof

def test_a_demand_on_everyone_is_not_social_proof():
    assert not h.SOCIAL_PROOF_RE.search("a forest that asks a great deal of everyone who enters it")
    assert h.SOCIAL_PROOF_RE.search("Everyone who visits says it is unforgettable.")
