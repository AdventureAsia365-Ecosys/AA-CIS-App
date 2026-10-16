"""AA-777 r7 review fixes (S239) on the r6 follow-up (fixes 1-6).

D1: a played-down hazard ('keep the hazard') merged with another finding on its sentence is never cut (the blog
attempt-3 cut, delete_flagged) and an empty repair of it is not spliced. D2: a multi-sentence cut checks every
sentence inside the item and the sentence AFTER it for an orphaned pronoun; a merge never widens into the hook.
D3: the F9 sticky key carries the previous sentence for more back-referring openers. D4: the thin-coda trim drops
only a sequence frame ('Before / After …,'), never a conditional one ('When …,'). Plus the two test gaps: the
attempt-3 F17 cut through run_write_background, and a family-caveat repair in source words is not reverted."""
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from services.acp_content_writing import quality_gates as qg
from services.acp_content_writing import sentence_repair as sr
from services.acp_content_writing import service
from services.acp_content_writing import trip_facts as tf
from tests.unit.test_aa777_r6_fixes import (AH_CTA, F9_TWO, F17_PICTURE, KUANG_SI, MONGOLIA, PICTURE, SUNSET,
                                            TP_CTA, _FILL, _blog, _ctx, _gate)

F17 = frozenset({"F17_trip_fact"})
HZ = "Sudden storms on the lake are nothing to worry about on this relaxed, unhurried day."
HAZARD = f"plays down a hazard the source states, for a family audience — keep the hazard, cut the reassurance: '{HZ}'"
PACE = f"states a pace or rest promise the source does not make ('relaxed') — cut it, or say only what the source " \
       f"says: '{HZ}'"
TIMING = f"gives a time the source does not ('unhurried day') — cut it or use the source's own timing: '{HZ}'"
F9_HZ = f'audit manual_check: FACT_CHECK_MANUAL_CHECK — unsupported claim(s): "{HZ}" (claims: calm; source: storms)'


# ---------------------------------------------------------------- D1: the hazard sentence is never cut

class TestHazardNeverCut:
    @pytest.mark.parametrize("ledger", [
        [_gate("F17_trip_fact", [HAZARD, PACE])],      # hazard first, pace merged in
        [_gate("F17_trip_fact", [TIMING, HAZARD])],    # hazard second: its keep mark is not dropped
        [_gate("F17_trip_fact", [HAZARD]), _gate("F9_cta_fact", [F9_HZ])],  # merged with an F9 claim
    ])
    def test_merged_hazard_item_keeps_its_sentence(self, ledger):
        text = _blog(HZ)
        items = sr.plan(text, ledger)
        assert len(items) == 1 and "keep the hazard" in items[0]["problem"]
        assert sr.keeps_sentence(items[0]["problem"])
        assert not service._cuttable(items[0], F17 | service.TRUTH_CUT_GATES)
        out, rest, entry = service._cut_blog_truth_items(text, items, cta=AH_CTA,
                                                         repaired=F17 | service.TRUTH_CUT_GATES)
        assert entry is None and out == text and rest == items
        assert sr.delete_flagged(text, items, cta=AH_CTA) is None

    def test_an_empty_repair_of_a_hazard_item_is_not_spliced(self):
        text = _blog(HZ)
        items = sr.plan(text, [_gate("F17_trip_fact", [HAZARD, PACE])])
        out = sr.splice_checked(text, items, [""], cta=AH_CTA)
        assert out == text and items[0]["guard"] == "hazard_kept"

    def test_a_hazard_repair_that_restates_a_neighbour_is_not_turned_into_a_cut(self):
        text = _blog(HZ)
        item = sr.plan(text, [_gate("F17_trip_fact", [HAZARD])])[0]
        out = sr.splice(text, [item], [_FILL.format(n=70)])  # restates the paragraph before
        assert HZ in out and item["guard"] == "repeat_kept"

    def test_a_kept_hazard_repair_is_spliced(self):
        text = _blog(HZ)
        items = sr.plan(text, [_gate("F17_trip_fact", [HAZARD, PACE])])
        new = "Sudden storms can sweep across the lake, so boats stay ashore when the wind picks up."
        out = sr.splice_checked(text, items, [new], cta=AH_CTA)
        assert new in out and HZ not in out and "guard" not in items[0]


# ---------------------------------------------------------------- D2: every sentence of a multi-sentence cut

IT = "It was founded by the first settlers of the valley in the 1600s."


class TestMultiSentenceCut:
    def test_pronoun_after_a_two_sentence_item_refuses_the_cut(self):
        text = _blog("Uran Togoo is a dormant volcano in the Bulgan steppe.").replace(
            "The herders move camp each season.", f"The herders move camp each season. {PICTURE} {SUNSET} {IT}")
        items = sr.plan(text, [_gate("F17_trip_fact", [F17_PICTURE]), _gate("F9_cta_fact", [F9_TWO])])
        assert len(items) == 1 and items[0]["sentence"] == f"{PICTURE} {SUNSET}"
        assert sr.delete_flagged(text, items, cta=AH_CTA) is None
        _, _, entry = service._cut_blog_truth_items(text, items, cta=AH_CTA, repaired=F17 | {"F9_cta_fact"})
        assert entry["repair_mode"] == "truth_cut_refused"

    def test_two_sentence_item_without_a_pronoun_after_it_is_cut(self):
        tail = "The valley road climbs north from here toward the lake."
        text = _blog("Uran Togoo is a dormant volcano in the Bulgan steppe.").replace(
            "The herders move camp each season.", f"The herders move camp each season. {PICTURE} {SUNSET} {tail}")
        items = sr.plan(text, [_gate("F17_trip_fact", [F17_PICTURE]), _gate("F9_cta_fact", [F9_TWO])])
        out = sr.delete_flagged(text, items, cta=AH_CTA)
        assert out is not None and PICTURE not in out and SUNSET not in out and tail in out

    def test_an_item_reaching_into_the_cta_is_never_cut(self):
        s = "The steppe opens wide beyond the last ridge of the pass."
        text = f"Northern Mongolia opens slowly toward the taiga.\n\n{s} {AH_CTA}."
        item = {"start": text.index(s), "end": len(text), "sentence": f"{s} {AH_CTA}.", "problem": "x",
                "gate": "F9_cta_fact"}
        assert sr.delete_flagged(text, [item], cta=AH_CTA) is None

    def test_merge_never_widens_into_the_hook(self):
        """F1 flags S2 (first item); the F9 judge quotes 'S1 S2' — the item stays on S2, so the cut still works."""
        fill = " ".join(f"Stop {i} pairs the amber terrace with a cedar courtyard by the river." for i in range(60))
        s1 = "Northern Mongolia opens slowly from the steppe toward the taiga in every season."
        s2 = "The lake sits 1,645 metres above the sea and freezes in winter."
        text = f"{s1} {s2}\n\n{fill}\n\n## Plan This Journey\n\n{AH_CTA}.\n"
        f1 = f"sentence states ['1,645'] not present in the content seed: '{s2}'"
        f9 = f'audit manual_check: FACT_CHECK_MANUAL_CHECK — unsupported claim(s): "{s1} {s2}" (claims: x; source: y)'
        items = sr.plan(text, [_gate("F1_grounding", [f1]), _gate("F9_cta_fact", [f9])])
        assert len(items) == 1 and items[0]["sentence"] == s2 and items[0]["gates"] == ["F1_grounding", "F9_cta_fact"]
        out = sr.delete_flagged(text, items, cta=AH_CTA)
        assert out is not None and s1 in out and s2 not in out


# ---------------------------------------------------------------- D3: back-referring openers carry the neighbour

class TestStickyKeyOpeners:
    @pytest.mark.parametrize("sentence", [
        "There, monks chant at dawn in the main hall.",
        "Then lunch is served on the boat deck.",
        "The next day you ride camels across the dunes.",
        "Afterwards the road climbs to the pass.",
        "Nearby, a small market sells felt.",
        "From there the trail drops to the river.",
        "After that, the group returns to camp.",
        "But it was founded in 1692.",
        "And they still herd yaks on the slopes.",
        "The monastery was founded in 1586.",
    ])
    def test_carries_the_previous_sentence(self, sentence):
        assert qg._f9_key("We stay at the X homestay.", sentence) != qg._f9_key("We stay in Ulaanbaatar.", sentence)

    def test_a_self_contained_sentence_stays_keyed_on_itself(self):
        trek = ("After a short trek through forested hills, Day 2 brings you to the tiered limestone pools of "
                "Kuang Si Falls near Luang Prabang.")
        assert qg._f9_key("A.", trek) == qg._f9_key("B.", trek)
        assert qg._f9_key("A.", "Lunch is at the View Café.") == qg._f9_key("B.", "Lunch is at the View Café.")


# ---------------------------------------------------------------- D4: a conditional frame is never dropped

class TestConditionalFrameKept:
    def _thin(self):
        ms = tf.parse_moments(MONGOLIA)
        return ms, next(m for m in ms if m.thin)

    @pytest.mark.parametrize("frame", ["When road access allows", "Once the river is low enough",
                                       "While the northern road is open", "As long as the weather holds"])
    def test_conditional_frame_is_not_trimmed(self, frame):
        ms, m = self._thin()
        s = (f"{frame}, the route passes the ruins of an ancient monastery destroyed during the political purges "
             "of 1937.")
        assert tf._LEADING_FRAME_RE.match(s) is None
        trimmed = tf._trim_thin(s, m, ms)
        assert trimmed is None or trimmed.startswith(frame)

    def test_sequence_frame_still_trimmed(self):
        s = "After leaving this part of northern Mongolia, the route passes the ruins."
        assert tf._LEADING_FRAME_RE.match(s) is not None


# ---------------------------------------------------------------- test gap: family caveat in source words

def test_family_caveat_in_source_words_is_not_reverted():
    """'before lunch' is the source's own time (trip_facts._TIME_ANCHORS): the caveat repair is kept."""
    anchor = "Kids love the turquoise pools at Kuang Si Falls."
    caveat = "Time is left for swimming and resting before lunch."
    text = f"Swim time at Kuang Si Falls on Day 2 is the centerpiece of the day. {anchor}\n\n{TP_CTA}"
    item = {"start": text.index(anchor), "end": text.index(anchor) + len(anchor), "sentence": anchor,
            "problem": "pitches Kuang Si Falls to families — keep this sentence and add ONE short sentence",
            "gate": "F17_trip_fact"}
    ctx = _ctx(channel="blog")
    assert sr.new_specifics(anchor, f"{anchor} {caveat}", trip_ctx=ctx, source=KUANG_SI) == []
    out = sr.splice_checked(text, [item], [f"{anchor} {caveat}"], cta=TP_CTA, trip_ctx=ctx, source=KUANG_SI)
    assert caveat in out and item.get("guard") is None


# ---------------------------------------------------------------- test gap: the attempt-3 F17 cut in the job

@pytest.mark.asyncio
async def test_job_attempt_three_cuts_an_f17_cut_it_item_once_f17_was_repaired():
    """F17 'cut it' on one sentence at attempts 1 and 2 (the repair keeps it); attempt 3 cuts it in code."""
    from tests.unit.test_aa777_f9_sticky_cut import LONG_BLOG, CHEF, CTA, _context, _outcome
    f17 = _gate("F17_trip_fact", [f"gives a time the source does not ('each night') — cut it or use the source's "
                                  f"own timing: '{CHEF}'"])

    def same_sentences(*, sentences, **_):
        return [s["sentence"] for s in sentences], 0.0

    def gates(**kw):
        return _outcome([dict(f17)] if CHEF in kw["content_text"] else [])
    ident = (lambda t, *a, **k: (t, []))
    seo = {"seo_title": "Punakha Dzong where two rivers meet", "meta_description": "m", "slug": "punakha"}
    with patch.object(service, "write_content", return_value=(LONG_BLOG, 0.01, seo, None)), \
         patch.object(service, "rewrite_with_feedback") as rw, \
         patch.object(service, "repair_sentences", side_effect=same_sentences) as rep, \
         patch.object(service, "run_quality_gates", side_effect=gates), \
         patch.object(service, "_fit_seo_meta", new=AsyncMock(return_value=(seo, 0.0))), \
         patch.object(service, "drop_restated_faq", side_effect=lambda t: (t, 0)), \
         patch.object(service, "drop_unanswerable_faq", side_effect=lambda t, *a: (t, [])), \
         patch("services.acp_content_writing.service.hygiene.autofix", side_effect=ident), \
         patch("services.acp_content_writing.service.hygiene.ensure_cta", side_effect=ident), \
         patch("services.acp_content_writing.service.trip_facts.fix_trip_facts", side_effect=ident), \
         patch("services.acp_content_writing.service.text_integrity.final_pass",
               side_effect=lambda t, hyg, **kw: (t, [], [])), \
         patch.object(service, "_finalize_piece", new=AsyncMock(return_value={})) as fin:
        await service.run_write_background(uuid4(), uuid4(), _context(), pool=MagicMock())
    rw.assert_not_called()
    kw = fin.call_args.kwargs
    assert rep.call_count == 1  # attempt 2's repair only; attempt 3 cuts
    assert kw["status"] == "approved" and CHEF not in kw["content_text"] and CTA in kw["content_text"]
    entry = next(e for e in kw["repair_log"] if e.get("repair_mode") == "truth_cut")
    assert entry["cut_gates"] == ["F17_trip_fact"] and entry["sentences_cut"] == 1
