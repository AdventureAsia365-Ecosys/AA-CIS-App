"""AA-865 — F9 sticky pass carried through a repair edit.

A T9 retry must not judge again a sentence an earlier F9 verdict of the same piece passed when the repair did not
change what it claims: a deletion-only edit of it, or an unchanged sentence whose key changed only because the
sentence before it was repaired. The before/after pairs come from the pooled S239 retry rounds (tenant and piece
names removed); a style edit that adds a word, a real new fault and a pronoun after a rewritten neighbour are
still judged again.
"""
import json
from unittest.mock import patch

import pytest

from services.acp_content_writing import quality_gates as qg

CTA = "Ask us about the dates."
PASS = {"status": "pass", "brand_fit": "1", "cta_clear": "1", "human_read": "1",
        "failure_codes": [], "flagged_phrases": [], "notes": ""}

# r6, blog: an F17 thin-coda repair cut the added clause; F9 had passed the long sentence at attempt 1 and
# flagged the shorter one at attempt 2 ("does not specify their position in the route").
CODA_BEFORE = ("Before leaving this part of northern Mongolia, the route also passes the ruins of an ancient monastery "
               "destroyed during the political purges of 1937: a single, spare reminder that the region's layers of "
               "cultural memory extend well beyond its landscape.")
CODA_AFTER = ("Before leaving this part of northern Mongolia, the route passes the ruins of an ancient monastery "
              "destroyed during the political purges of 1937.")
# r7, blog: an F15 style repair rewrote a figure of speech with new words; the judge then flagged a causal claim.
LAKE_BEFORE = ("A lake this large, this remote, this ecologically distinct, exerts a gravitational pull on everything "
               "around it: the climate, the soil, the species, and the human communities who have organised their "
               "lives around its shores and the forests to its north.")
LAKE_AFTER = ("A lake this large, this remote, this ecologically distinct, shapes everything around it: the climate, "
              "the soil, the species, and the communities sustained by its shores and the forests to its north.")
# r7, blog: a real new fault — the repair turned an overnight stop into a lunch stop.
CAMP_BEFORE = ("It is a practical overnight stop that also gives families time to rest before the longer drive north "
               "on day four.")
CAMP_AFTER = "It is a practical stop for lunch before the group continues north toward the lakeshore."
# r5, blog: a thin-coda repair rewrote the coda and added 'within'.
RUINS_BEFORE = ("The journey through this part of northern Mongolia closes with a single further moment: the ruins of "
                "an ancient monastery, destroyed during the political purges of 1937, which stand as a quiet register "
                "of what this region has also lost.")
RUINS_AFTER = ("The ruins of an ancient monastery, destroyed during the political purges of 1937, stand within this "
               "region.")
# r7, LinkedIn: an F14/F15 repair rewrote the hook; the next sentence was left unchanged, but it opens on a definite
# noun phrase, so its key carried the old hook and F9 judged it again.
HOOK_BEFORE = ("Guru Rinpoche is said to have flown to this cliff on the back of a tigress in the 8th century, and "
               "that founding story still shapes why Paro Taktsang matters.")
HOOK_AFTER = ("In the 8th century, Guru Rinpoche meditated in a sacred cave here, a connection central to the site's "
              "spiritual significance.")
CAVE = ("The sacred cave at the heart of the site draws Bhutanese Buddhist pilgrims who consider a visit here a "
        "spiritual obligation.")
CLIMB = "The climb up to the monastery takes most of the morning at an easy pace."


class TestDeletionOnly:
    @pytest.mark.parametrize("old,new", [
        (CODA_BEFORE, CODA_AFTER),  # r6: the added clause cut, 'also' dropped
        ("The route also passes the old ferry landing near the camp.",
         "The route passes the old ferry landing near the camp."),
        ("The camp, which sits on a quiet bay, has twelve gers and a dining tent.",
         "The camp has twelve gers and a dining tent."),
        ("Unlike the southern route, the drive north follows the river valley.",
         "The drive north follows the river valley."),
        (CAMP_BEFORE, CAMP_BEFORE),
    ])
    def test_a_pure_deletion_keeps_the_claim(self, old, new):
        assert qg._f9_deletion_only(old, new)

    @pytest.mark.parametrize("old,new", [
        (LAKE_BEFORE, LAKE_AFTER),  # r7: new words 'shapes', 'sustained'
        (CAMP_BEFORE, CAMP_AFTER),  # r7: a real new fault
        (RUINS_BEFORE, RUINS_AFTER),  # r5: 'within' is new
        # a run cut from mid-phrase re-attaches the modifier: the museum is now what was founded in 1586
        ("The tour visits the museum near the monastery founded in 1586.",
         "The tour visits the museum founded in 1586."),
        # a removed hedge, negation or option makes a stronger claim
        ("Optional boat rides on the lake are available in the afternoon.",
         "Boat rides on the lake are available in the afternoon."),
        ("The dzong is not open to visitors during the winter months.",
         "The dzong is open to visitors during the winter months."),
        ("The trail isn't steep once you cross the bridge.", "The trail is steep once you cross the bridge."),
        ("The drive takes about four hours on a paved road.", "The drive takes four hours on a paved road."),
        # a new symbol and a reorder are not deletions
        ("Groups of 12 travellers share two guides on the trek.", "Groups of 12+ travellers share guides on the trek."),
        ("We drive from Moron to Khatgal on day three.", "We drive from Khatgal to Moron on day three."),
    ])
    def test_anything_else_is_judged_again(self, old, new):
        assert not qg._f9_deletion_only(old, new)


class TestAntecedentKept:
    def test_a_definite_noun_phrase_with_no_word_from_the_old_neighbour_keeps_its_pass(self):
        assert qg._f9_antecedent_kept(CAVE, HOOK_BEFORE, HOOK_AFTER)

    def test_a_pronoun_after_a_rewritten_neighbour_is_judged_again(self):
        it = "It was founded in 1692 by the lama."
        assert not qg._f9_antecedent_kept(it, "We visit Taktsang Monastery on day two.",
                                          "We visit Kyichu Lhakhang on day two.")

    def test_a_definite_noun_phrase_that_lost_its_antecedent_is_judged_again(self):
        the = "The monastery was founded in 1692 by the lama."
        assert not qg._f9_antecedent_kept(the, "We visit Taktsang Monastery on day two.",
                                          "We visit Kyichu Lhakhang on day two.")

    def test_a_pronoun_after_a_deletion_only_neighbour_keeps_its_pass(self):
        it = "It was founded in 1692 by the lama."
        assert qg._f9_antecedent_kept(it, "We visit Taktsang Monastery, high on its cliff, on day two.",
                                      "We visit Taktsang Monastery on day two.")

    def test_a_sentence_that_needs_no_antecedent_always_keeps_it(self):
        assert qg._f9_antecedent_kept(CODA_AFTER, "Anything at all here.", "Something else entirely now.")


def _piece(*sentences: str) -> str:
    return " ".join(sentences) + " " + CTA


def _flag(sentence: str) -> dict:
    return {"status": "manual_check", "failure_codes": ["FACT_CHECK_MANUAL_CHECK"], "flagged_phrases": [sentence],
            "unsupported_claims": [{"sentence": sentence, "claim": "c", "why_not_in_source": "the source is silent"}]}


def _f9(data: dict, text: str, passed):
    raw = {"text": json.dumps({**PASS, **data}), "stop_reason": None}
    with patch.object(qg, "invoke_judge", return_value=raw), \
         patch.object(qg, "_log_t10_judge_call"), patch.object(qg, "logger") as lg:
        results = qg.gate_brand_voice(text, CTA, "rubric", source_text="seed", passed_sentences=passed)
    return {r["gate"]: r for r in results}["F9_cta_fact"], lg


class TestCarryInTheGate:
    def test_a_deletion_only_repair_of_a_passed_sentence_stays_sticky(self):
        memory = qg.F9PassMemory()
        before = _piece(CLIMB, CODA_BEFORE)
        _f9({}, before, memory)  # attempt 1: F9 passes the whole piece
        assert memory.last_text == before
        after = _piece(CLIMB, CODA_AFTER)  # F17 repair: the added clause cut
        f9, lg = _f9(_flag(CODA_AFTER), after, memory)
        assert f9["passed"] is True and f9["sticky_pass"] == [CODA_AFTER]
        lg.info.assert_any_call("F9_sticky_carry", carried=1)
        assert memory.last_text == after

    def test_an_unchanged_sentence_after_a_rewritten_hook_stays_sticky(self):
        memory = qg.F9PassMemory()
        _f9({}, _piece(HOOK_BEFORE, CAVE, CLIMB), memory)
        f9, _ = _f9(_flag(CAVE), _piece(HOOK_AFTER, CAVE, CLIMB), memory)
        assert f9["passed"] is True and f9["sticky_pass"] == [CAVE]

    @pytest.mark.parametrize("before,after", [(LAKE_BEFORE, LAKE_AFTER), (CAMP_BEFORE, CAMP_AFTER)])
    def test_a_repair_that_adds_words_is_judged_again(self, before, after):
        memory = qg.F9PassMemory()
        _f9({}, _piece(CLIMB, before), memory)
        f9, _ = _f9(_flag(after), _piece(CLIMB, after), memory)
        assert f9["passed"] is False and f9["blocking"] is True and "sticky_pass" not in f9

    def test_a_pronoun_after_a_rewritten_neighbour_is_judged_again(self):
        it = "It was founded in 1692 by the lama."
        memory = qg.F9PassMemory()
        _f9({}, _piece("We visit Taktsang Monastery on day two.", it), memory)
        f9, _ = _f9(_flag(it), _piece("We visit Kyichu Lhakhang on day two.", it), memory)
        assert f9["passed"] is False and f9["blocking"] is True

    def test_a_sentence_flagged_before_is_never_carried(self):
        """An F9 repair that deletes the unsupported part must be judged again: the old sentence never passed."""
        memory = qg.F9PassMemory()
        _f9(_flag(CODA_BEFORE), _piece(CLIMB, CODA_BEFORE), memory)
        f9, _ = _f9(_flag(CODA_AFTER), _piece(CLIMB, CODA_AFTER), memory)
        assert f9["passed"] is False and f9["blocking"] is True

    def test_a_plain_set_keeps_the_old_behaviour(self):
        passed: set[str] = set()
        _f9({}, _piece(CLIMB, CODA_BEFORE), passed)
        f9, lg = _f9(_flag(CODA_AFTER), _piece(CLIMB, CODA_AFTER), passed)
        assert f9["passed"] is False and f9["blocking"] is True
        assert not any(c.args and c.args[0] == "F9_sticky_carry" for c in lg.info.call_args_list)

    def test_attempt_one_is_unchanged(self):
        memory, plain = qg.F9PassMemory(), set()
        text = _piece(CLIMB, CODA_BEFORE)
        a, _ = _f9(_flag(CODA_BEFORE), text, memory)
        b, _ = _f9(_flag(CODA_BEFORE), text, plain)
        assert a == b and set(memory) == plain and a["passed"] is False

    def test_a_judge_outage_leaves_the_last_text(self):
        memory = qg.F9PassMemory()
        before = _piece(CLIMB, CODA_BEFORE)
        _f9({}, before, memory)
        with patch.object(qg, "invoke_judge", side_effect=RuntimeError("down")):
            qg.gate_brand_voice(_piece(CLIMB, CODA_AFTER), CTA, "rubric", source_text="seed",
                                passed_sentences=memory)
        assert memory.last_text == before


def test_carry_keys_maps_only_the_changed_sentences():
    memory = qg.F9PassMemory()
    before = _piece(HOOK_BEFORE, CAVE, CLIMB, CODA_BEFORE)
    _f9({}, before, memory)
    after = _piece(HOOK_AFTER, CAVE, CLIMB, CODA_AFTER)
    carried = qg._f9_carry_keys(before, after, memory)
    assert carried == {qg._f9_key(HOOK_AFTER, CAVE), qg._f9_key(CLIMB, CODA_AFTER)}
    assert qg._f9_carry_keys(before, after, set()) == set()
