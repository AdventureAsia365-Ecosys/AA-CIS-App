"""AA-777 r6 fixes (S239) — replays of the real r6 sentences (tour seed lines only, no tenant data).

Fix 1: a sentence repair that ADDS a number / time / (blog) pace word the sentence and the source lack is not
spliced (r6 R1 a3d82ff1, R2 263e5a5a, AA-768 H2 89d60686). Fix 2: plan() merges a second finding on the same
sentence instead of dropping it (r6 a2133050, 35ee6a3d). Fix 3: the F9 sticky key carries the previous sentence
only for a referent-led sentence, and a multi-sentence judge quote is sticky when every sentence is (r6
17125e0c). Fix 4: the thin-coda trim drops a leading 'Before leaving …,' frame and the F17 message names the
extra words (r6 a2133050). Fix 6: the blog's attempt-3 cut also takes F17 'cut it' items once F17 was repaired."""
import json

import pytest
from unittest.mock import patch

from services.acp_content_writing import quality_gates as qg
from services.acp_content_writing import sentence_repair as sr
from services.acp_content_writing import service
from services.acp_content_writing import trip_facts as tf

# ---------------------------------------------------------------- seeds (tour source lines from the r6 pieces)

KUANG_SI = (
    "Kuang Si Falls — swim and rest at pools\n"
    "Location: Kuang Si Falls, Luang Prabang, Luang Prabang Province, Laos\n"
    "Day 2 of the trip\n"
    "Source itinerary: A short trek leads to Kuang Si Falls, where pools descend through limestone in varying "
    "colors. Time is left for swimming and resting before lunch at the View Café overlooking the cascade.\n"
    "Why it matters: turquoise tiered pools in limestone landscape\n"
    "Place background (checked facts about the place itself — you may use these; they say nothing about what "
    "this trip does):\n"
    "- The main upper fall drops roughly 50 metres into a large pool at the base.\n"
    "- The site is set within forested hills and is part of a protected area.")
TP_CTA = "Get the full Trail Pulse itinerary and dates"

URAN_TOGOO = (
    "Uran Togoo — trek into the caldera of a dormant volcano\n"
    "Location: Uran Togoo Volcano, Bulgan, Bulgan Province, Mongolia\n"
    "Day 3 of the trip\n"
    "Source itinerary: Journey to Uran Togoo, a dormant volcano that erupted approximately 24,000 years ago. "
    "The volcano sits within green steppes and forests, the largest of four dormant volcanoes in the surrounding "
    "valleys.\n"
    "Why it matters: descend into an ancient volcanic crater and walk the caldera floor")
RUINS = (
    "ruins of an ancient monastery — visit\n"
    "Location: ruins of an ancient monastery, Mongolia (do not name any other country or region for it)\n"
    "Day 14 of the trip\n"
    "Source itinerary: visit the ruins of an ancient monastery destroyed during the political purges of 1937.\n"
    "Why it matters: encounter historical loss and cultural memory\n"
    "Thin moment: only 1 source fact — mention it in one sentence at most; never make it the hook, the title or "
    "the climax.")
MONGOLIA = f"{URAN_TOGOO}\n\n{RUINS}"
AH_CTA = "Talk to an Atlas & Hearth travel designer about this journey"

# r6 R1 (a3d82ff1, LinkedIn): F9 item → the repair turned an energy line into a schedule claim
R1_BEFORE = ("Building swim and rest time in before lunch means you arrive at the afternoon with energy, "
             "not just photos.")
R1_AFTER = ("Building swim and rest time in before lunch means the afternoon starts with space to keep going, "
            "not just a camera roll to show for the morning.")
# r6 R2 (263e5a5a, LinkedIn): F15 item → 'The afternoon … before lunch'; attempt 3 wrote the source wording
R2_BEFORE = "Time to swim and rest is built into the plan before lunch."
R2_AFTER = "The afternoon has room for swimming and resting at the pools before lunch."
R2_GOOD = "Time is left for swimming and resting at the pools before lunch."
# AA-768 H2 (89d60686, blog): F15 item → a pace claim
H2_BEFORE = ("For travellers who have moved through markets across Asia, the temptation is to pass through these "
             "quickly.")
H2_AFTER = "These markets reward a slower pace than their size might suggest."
# r6 a2133050 (blog) attempt-2 coda
CODA = ("Before leaving this part of northern Mongolia, the route passes the ruins of an ancient monastery destroyed "
        "during the political purges of 1937.")


def _ctx(seed=KUANG_SI, channel="linkedin", cta=TP_CTA):
    return tf.TripFactContext(seed=seed, cta=cta, channel=channel)


# ---------------------------------------------------------------- fix 1: no new specifics in a repair

class TestNewSpecifics:
    def test_r1_schedule_claim_from_an_energy_line(self):
        assert tf.timing_claims(R1_BEFORE, _ctx().moments) == []  # 'at the afternoon' is not a time (S234)
        assert sr.new_specifics(R1_BEFORE, R1_AFTER, trip_ctx=_ctx(), source=KUANG_SI) == [
            "adds a time the source does not give ('afternoon')"]

    def test_r2_new_day_part(self):
        assert sr.new_specifics(R2_BEFORE, R2_AFTER, trip_ctx=_ctx(), source=KUANG_SI) == [
            "adds a time the source does not give ('afternoon')"]

    def test_r2_source_wording_is_clean(self):
        assert sr.new_specifics(R2_BEFORE, R2_GOOD, trip_ctx=_ctx(), source=KUANG_SI) == []

    def test_the_source_morning_anchor_is_not_new(self):
        """'before lunch' in the trip line supports 'the morning' (trip_facts._TIME_ANCHORS)."""
        after = "The morning at Kuang Si Falls leaves time for swimming and resting before lunch."
        assert sr.new_specifics(R2_BEFORE, after, trip_ctx=_ctx(), source=KUANG_SI) == []

    def test_a_time_the_sentence_already_had_is_not_new(self):
        before = "The afternoon is for swimming at the pools."
        after = "The afternoon is for swimming and resting at the pools."
        assert sr.new_specifics(before, after, trip_ctx=_ctx(), source=KUANG_SI) == []

    def test_h2_pace_word_on_a_blog_only(self):
        assert sr.new_specifics(H2_BEFORE, H2_AFTER, trip_ctx=_ctx(channel="blog"), source=KUANG_SI) == [
            "adds a pace or rest promise the source does not make ('pace')"]
        assert sr.new_specifics(H2_BEFORE, H2_AFTER, trip_ctx=_ctx(channel="linkedin"), source=KUANG_SI) == []

    def test_new_number_but_not_a_source_number(self):
        before = "The main fall drops into a large pool at the base."
        assert sr.new_specifics(before, "The main fall drops roughly 50 metres into a large pool.",
                                source=KUANG_SI) == []
        added = sr.new_specifics(before, "The main fall drops 80 metres into a large pool.", source=KUANG_SI)
        assert len(added) == 1 and "80" in added[0]

    def test_a_cut_is_never_flagged(self):
        assert sr.new_specifics(R1_BEFORE, "", trip_ctx=_ctx(), source=KUANG_SI) == []

    def test_splice_checked_keeps_the_old_sentence(self):
        text = (f"Swim time at Kuang Si Falls on Day 2 is the centerpiece of the day. {R1_BEFORE} {R2_BEFORE}"
                f"\n\n{TP_CTA}")
        items = [{"start": text.index(s), "end": text.index(s) + len(s), "sentence": s, "problem": "x",
                  "gate": "F9_cta_fact"} for s in (R1_BEFORE, R2_BEFORE)]
        out = sr.splice_checked(text, items, [R1_AFTER, R2_GOOD], cta=TP_CTA, trip_ctx=_ctx(), source=KUANG_SI)
        assert R1_BEFORE in out and R1_AFTER not in out  # reverted: it added 'the afternoon'
        assert R2_GOOD in out and R2_BEFORE not in out  # the clean repair is kept
        assert items[0]["guard"] == "specifics" and "afternoon" in items[0]["specifics"][0]
        assert "guard" not in items[1]

    def test_splice_checked_without_context_is_unchanged(self):
        text = f"Intro sentence about the falls today. {R1_BEFORE}\n\n{TP_CTA}"
        item = {"start": text.index(R1_BEFORE), "end": text.index(R1_BEFORE) + len(R1_BEFORE),
                "sentence": R1_BEFORE, "problem": "x"}
        assert R1_AFTER in sr.splice_checked(text, [item], [R1_AFTER], cta=TP_CTA)

    def test_repair_prompt_forbids_new_times(self):
        from services.acp_content_writing.generate import _REPAIR_SYSTEM
        assert "Never add a time of day" in _REPAIR_SYSTEM and "pace or rest word" in _REPAIR_SYSTEM


# ---------------------------------------------------------------- fix 2: plan() merges a second finding

F17_CODA = ("closes on a thin moment (one source fact) with an interpretation the source does not give — say it in "
            f"one plain sentence, or cut the added clause: '{CODA}'")
F9_CODA = ('audit manual_check: FACT_CHECK_MANUAL_CHECK — unsupported claim(s): '
           f'"{CODA}" (claims: The monastery ruins are visited before the route leaves northern Mongolia.; '
           'source: does not specify their position in the route)')
PICTURE = "Picture the end of your first evening in Laos."
SUNSET = ("The sunset ride around Luang Prabang is done, bicycles are leaned against a wall, and your family is "
          "seated at Khaiphaen Restaurant for dinner and an informal briefing on the days ahead.")
F17_PICTURE = f"gives a time the source does not ('first evening') — cut it or use the source's own timing: '{PICTURE}'"
F9_TWO = ('audit manual_check: FACT_CHECK_MANUAL_CHECK — unsupported claim(s): '
          f'"{PICTURE} {SUNSET}" (claims: The bicycles are leaned against a wall.; '
          'source: it does not provide this invented scene detail.)')


def _gate(name, violations):
    return {"gate": name, "passed": False, "blocking": True, "repairable": True, "violations": violations}


class TestPlanMerge:
    def test_a2133050_f9_on_the_f17_sentence_is_kept(self):
        text = f"The steppe opens to the north.\n\n{CODA}\n\n## Plan This Journey\n\n{AH_CTA}."
        items = sr.plan(text, [_gate("F17_trip_fact", [F17_CODA]), _gate("F9_cta_fact", [F9_CODA])])
        assert len(items) == 1
        it = items[0]
        assert it["gate"] == "F9_cta_fact" and it["gates"] == ["F17_trip_fact", "F9_cta_fact"]
        assert "cut the added clause" in it["problem"] and "before the route leaves" in it["problem"]
        assert it["sentence"] == CODA

    def test_35ee6a3d_two_sentence_quote_widens_the_item(self):
        text = f"## The Dinner Before the Route Begins\n\n{PICTURE} {SUNSET} The itinerary takes in waterfalls."
        items = sr.plan(text, [_gate("F17_trip_fact", [F17_PICTURE]), _gate("F9_cta_fact", [F9_TWO])])
        assert len(items) == 1
        it = items[0]
        assert it["sentence"] == f"{PICTURE} {SUNSET}"  # the repair now sees the bicycles sentence
        assert it["gate"] == "F9_cta_fact" and "invented scene detail" in it["problem"]
        assert text[it["start"]:it["end"]] == f"{PICTURE} {SUNSET}"

    def test_separate_sentences_stay_separate(self):
        text = f"{PICTURE} {SUNSET} The itinerary takes in waterfalls."
        f9 = ('audit manual_check: FACT_CHECK_MANUAL_CHECK — unsupported claim(s): '
              f'"{SUNSET}" (claims: bicycles; source: none)')
        items = sr.plan(text, [_gate("F17_trip_fact", [F17_PICTURE]), _gate("F9_cta_fact", [f9])])
        assert [i["gate"] for i in items] == ["F17_trip_fact", "F9_cta_fact"]

    def test_a_family_caveat_item_is_never_merged(self):
        sentence = "Families visit the reindeer herders of the taiga on day ten of the trip."
        caveat = (f"pitches the Tsaatan to families but never says what the source says about getting there (a long "
                  f"ride) — keep this sentence and add ONE short sentence that states that caveat in the source's "
                  f"words: '{sentence}'")
        f9 = ('audit manual_check: FACT_CHECK_MANUAL_CHECK — unsupported claim(s): '
              f'"{sentence}" (claims: day ten; source: none)')
        items = sr.plan(f"Intro line of the piece here. {sentence}",
                        [_gate("F17_trip_fact", [caveat]), _gate("F9_cta_fact", [f9])])
        assert len(items) == 1 and "add ONE" in items[0]["problem"] and "day ten" not in items[0]["problem"]
        assert items[0]["gate"] == "F17_trip_fact"


# ---------------------------------------------------------------- fix 6: the blog attempt-3 cut takes F17 'cut it'

_FILL = ("Stop {n} pairs the amber terrace with a cedar courtyard and a quiet ember garden by the river bank "
         "where the herders water their horses before the long ride north.")


def _blog(coda):
    paras = [" ".join(_FILL.format(n=i * 5 + j + 1) for j in range(5)) for i in range(14)]
    return ("Northern Mongolia opens slowly from the steppe toward the taiga.\n\n## The route\n\n"
            + "\n\n".join(paras) + f"\n\nThe herders move camp each season. {coda}\n\n"
            f"## Plan This Journey\n\n{AH_CTA}.\n\n## FAQ\n\n"
            "**Q: When does the trip run?**\nA: The trip runs in the summer months each year.\n\n"
            "**Q: How long is the journey?**\nA: The journey runs for fifteen days in all.\n\n"
            "**Q: Is the volcano active?**\nA: Uran Togoo is a dormant volcano in Bulgan.\n")


class TestBlogCutF17:
    def _items(self, text):
        return sr.plan(text, [_gate("F17_trip_fact", [F17_CODA])])

    def test_f17_cut_it_item_is_cut_once_f17_was_repaired(self):
        text = _blog(CODA)
        text_out, rest, entry = service._cut_blog_truth_items(text, self._items(text), cta=AH_CTA,
                                                              repaired=frozenset({"F17_trip_fact"}))
        assert entry["repair_mode"] == "truth_cut" and CODA not in text_out and rest == []
        assert entry["cut_gates"] == ["F17_trip_fact"]

    def test_default_scope_is_truth_items_only(self):
        text = _blog(CODA)
        items = self._items(text)
        assert service._cut_blog_truth_items(text, items, cta=AH_CTA) == (text, items, None)

    def test_merged_item_is_cut_when_only_f17_was_repaired(self):
        """r6 a2133050: F17 repaired at attempt 1; at attempt 2 F9 joined on the same sentence."""
        text = _blog(CODA)
        items = sr.plan(text, [_gate("F17_trip_fact", [F17_CODA]), _gate("F9_cta_fact", [F9_CODA])])
        out, _, entry = service._cut_blog_truth_items(text, items, cta=AH_CTA, repaired=frozenset({"F17_trip_fact"}))
        assert entry["repair_mode"] == "truth_cut" and entry["cut_gates"] == ["F17_trip_fact", "F9_cta_fact"]
        assert CODA not in out

    def test_hazard_and_caveat_items_are_never_cut(self):
        hazard = {"gate": "F17_trip_fact", "gates": ["F17_trip_fact"], "problem": "plays down a hazard the source "
                  "states, for a family audience — keep the hazard, cut the reassurance"}
        caveat = {"gate": "F17_trip_fact", "gates": ["F17_trip_fact"], "problem": "keep this sentence and add ONE "
                  "short sentence — cut it never"}
        rep = frozenset({"F17_trip_fact"})
        assert not service._cuttable(hazard, rep) and not service._cuttable(caveat, rep)

    def test_cut_gates_repaired(self):
        f17 = [{"gate": "F17_trip_fact", "passed": False, "blocking": True}]
        assert service._cut_gates_repaired([{"repair_mode": "sentences", "gate_ledger": f17}]) == {"F17_trip_fact"}
        assert not service._truth_repaired_before([{"repair_mode": "sentences", "gate_ledger": f17}])
        assert service._cut_gates_repaired([{"repair_mode": None, "gate_ledger": f17}]) == frozenset()


# ---------------------------------------------------------------- fix 4: thin coda leading frame

class TestThinCodaFrame:
    def _thin(self):
        ms = tf.parse_moments(MONGOLIA)
        return ms, next(m for m in ms if m.thin)

    def test_a2133050_frame_is_trimmed(self):
        ms, m = self._thin()
        assert tf._thin_extra(CODA, m) == ["leaving", "part", "northern", "mongolia"]
        assert tf._trim_thin(CODA, m, ms) == ("The route passes the ruins of an ancient monastery destroyed during "
                                              "the political purges of 1937.")

    def test_attempt_one_coda_with_frame_and_tail(self):
        ms, m = self._thin()
        first = CODA.replace("the route passes", "the route also passes")[:-1] + (
            ": a single, spare reminder that the region's layers of cultural memory extend well beyond its landscape.")
        assert tf._trim_thin(first, m, ms) == ("The route also passes the ruins of an ancient monastery destroyed "
                                               "during the political purges of 1937.")

    def test_a_day_frame_is_kept(self):
        """The old head cut wins when it qualifies: 'On Day 14, …' keeps its day."""
        ms, m = self._thin()
        s = ("On Day 14, the journey passes the ruins of an ancient monastery destroyed during the political purges "
             "of 1937 — a quiet coda to two weeks spent moving through a landscape of loss.")
        assert tf._trim_thin(s, m, ms).startswith("On Day 14, the journey passes")

    def test_message_names_the_extra_words_and_fix_clears_it(self):
        ms, _ = self._thin()
        text = f"## Lake\n\nThe herders move camp each season.\n\n{CODA}\n\n## Plan This Journey\n\n{AH_CTA}."
        found = tf.thin_coda(text, ms, AH_CTA)
        assert len(found) == 1 and "leaving, part, northern, mongolia" in found[0]
        assert sr._BRAND_QUOTE_RE.search(found[0]).group(2) == CODA  # still parsed by plan()
        out, log = tf.fix_thin_moments(text, ms, AH_CTA)
        assert "The route passes the ruins of an ancient monastery" in out and "Before leaving" not in out
        assert tf.thin_coda(out, ms, AH_CTA) == [] and log


# ---------------------------------------------------------------- fix 3: sticky key

CTA = "Get the full Trail Pulse itinerary and dates."
OLD_HOOK = "Swim time at Kuang Si Falls isn't a gap in the schedule. It's the schedule."
NEW_HOOK = "Swim time at Kuang Si Falls on Day 2 is the centerpiece of the day."
TREK = ("After a short trek through forested hills, Day 2 brings you to the tiered limestone pools of Kuang Si Falls "
        "near Luang Prabang.")
TAIL = "Lunch is at the View Café overlooking the cascade."
PASS = {"status": "pass", "brand_fit": "1", "cta_clear": "1", "human_read": "1",
        "failure_codes": [], "flagged_phrases": [], "notes": ""}


def _f9(data, text, passed):
    raw = {"text": json.dumps({**PASS, **data}), "stop_reason": None}
    with patch.object(qg, "invoke_judge", return_value=raw), patch.object(qg, "_log_t10_judge_call"), \
         patch.object(qg, "logger"):
        res = qg.gate_brand_voice(text, CTA, "rubric", source_text="seed", passed_sentences=passed)
    return {r["gate"]: r for r in res}["F9_cta_fact"]


def _flag(sentence):
    return {"status": "manual_check", "failure_codes": ["FACT_CHECK_MANUAL_CHECK"], "flagged_phrases": [sentence],
            "unsupported_claims": [{"sentence": sentence, "claim": "forested hills",
                                    "why_not_in_source": "does not state the trek passes through forested hills"}]}


class TestStickyKey:
    def test_17125e0c_unchanged_sentence_after_a_repaired_hook_is_sticky(self):
        passed: set[str] = set()
        _f9({}, f"{OLD_HOOK} {TREK} {TAIL} {CTA}", passed)  # attempt 1 passes the trek sentence
        f9 = _f9(_flag(TREK), f"{NEW_HOOK} {TREK} {TAIL} {CTA}", passed)  # attempt 2: hook repaired (F15)
        assert f9["passed"] is True and f9["sticky_pass"] == [TREK]

    def test_a_referent_led_sentence_still_carries_its_neighbour(self):
        assert qg._f9_key("We visit Taktsang.", "It was founded in 1692.") != qg._f9_key(
            "We visit Kyichu.", "It was founded in 1692.")
        assert qg._f9_key("A.", TREK) == qg._f9_key("B.", TREK)
        assert qg._f9_key("A.", "These are not performances of heritage.") != qg._f9_key(
            "B.", "These are not performances of heritage.")

    def test_two_sentence_quote_is_sticky_only_when_both_were_passed(self):
        text = f"{NEW_HOOK} {TREK} {TAIL} {CTA}"
        passed: set[str] = set()
        _f9({}, text, passed)
        two = f"{TREK} {TAIL}"
        assert qg._f9_located_key(text, two) is None and len(qg._f9_quote_keys(text, two)) == 2
        f9 = _f9(_flag(two), text, passed)
        assert f9["passed"] is True and f9["sticky_pass"]
        # one of the two sentences changed → judged again
        tail2 = "Lunch is at the View Café, the best table in Laos."
        f9 = _f9(_flag(f"{TREK} {tail2}"), f"{NEW_HOOK} {TREK} {tail2} {CTA}", passed)
        assert f9["passed"] is False and "sticky_pass" not in f9


# ---------------------------------------------------------------- the job wires fix 1 (trip_ctx + grounding text)

@pytest.mark.asyncio
async def test_job_reverts_a_repair_that_adds_a_time_and_logs_it():
    """r6 R1 through run_write_background: the attempt-2 repair returns 'the afternoon starts…'; the job keeps
    the old sentence (no new F17), logs `specifics_reverts`, and the gates never see the new sentence.
    AA-790 round 3: the writer-side AI-tell pass would cut R1_BEFORE's ', not just photos.' tail (a spectator
    contrast), so it is held to identity like the other deterministic steps and the fixture reaches the gates."""
    from uuid import uuid4
    from unittest.mock import AsyncMock, MagicMock
    from tests.unit.test_aa777_f9_sticky_cut import _context, _outcome
    text = f"Swim time at Kuang Si Falls on Day 2 is the centerpiece of the day. {R1_BEFORE}\n\n{TP_CTA}"
    f9 = {"gate": "F9_cta_fact", "passed": False, "blocking": True, "repairable": True,
          "violations": ['audit manual_check: FACT_CHECK_MANUAL_CHECK — unsupported claim(s): '
                         f'"{R1_BEFORE}" (claims: energy; source: none)']}
    seen: list[str] = []

    def gates(**kw):
        seen.append(kw["content_text"])
        return _outcome([dict(f9)] if R1_BEFORE in kw["content_text"] else [])
    ctx = {**_context(), "channel": "linkedin", "channel_style": {"key": "linkedin"}, "atom_text": KUANG_SI,
           "cta": TP_CTA}
    ident = (lambda t, *a, **k: (t, []))
    seo = {"seo_title": None, "meta_description": None, "slug": None}
    with patch.object(service, "write_content", return_value=(text, 0.01, seo, None)), \
         patch.object(service, "rewrite_with_feedback"), \
         patch.object(service, "repair_sentences", return_value=([R1_AFTER], 0.0)), \
         patch.object(service, "run_quality_gates", side_effect=gates), \
         patch.object(service, "_fit_seo_meta", new=AsyncMock(return_value=(seo, 0.0))), \
         patch.object(service, "_fix_first_line", new=AsyncMock(side_effect=lambda t, **k: (t, 0.0, []))), \
         patch("services.acp_content_writing.service.hygiene.autofix", side_effect=ident), \
         patch("services.acp_content_writing.service.hygiene.ensure_cta", side_effect=ident), \
         patch("services.acp_content_writing.service.trip_facts.fix_trip_facts", side_effect=ident), \
         patch("services.acp_content_writing.service.ai_tells.rewrite", side_effect=ident), \
         patch("services.acp_content_writing.service.text_integrity.final_pass",
               side_effect=lambda t, hyg, **kw: (t, [], [])), \
         patch.object(service, "_finalize_piece", new=AsyncMock(return_value={})) as fin:
        await service.run_write_background(uuid4(), uuid4(), ctx, pool=MagicMock())
    assert not any(R1_AFTER in t for t in seen)
    entry = next(e for e in fin.call_args.kwargs["repair_log"] if e.get("specifics_reverts"))
    assert entry["specifics_reverts"] == 1 and "afternoon" in entry["specifics_problems"][0]
