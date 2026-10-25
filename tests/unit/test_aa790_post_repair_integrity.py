"""AA-790 (slice A) — repair-contract invariant 7: the post-repair integrity check on the deterministic T9 edits.

The S234 r4 panel found defects the repair steps themselves left after the last check that could see them: a split
paragraph (#18), sentences cut mid-clause (#4, #12) and an orphan line (#16, fixed at its source since, see
test_aa768_s234_round3 TestFamilyCaveat). The detectors live in text_integrity (`split_paragraphs`,
`dangling_tails`, read through `hygiene.integrity_problems`); the invariant lives in
services/quality/repair_contract.py (`integrity_problems`, `enforce_step`); service.run_write_background wraps each
deterministic edit of the attempt loop in `_StepGuard`.
"""
from __future__ import annotations

import pytest

from services.acp_content_writing import hygiene as h
from services.acp_content_writing import service
from services.acp_content_writing import text_integrity as ti
from services.quality import degraded
from services.quality import repair_contract as rc

CTA = "Get the full Trail Pulse itinerary and dates"

# r4 #18 shape: a repair / trim joined two paragraphs' halves across the blank line
SPLIT = ("## Day Two\n\nThe trek on Day 2 ends at the tiered pools of Kuang Si Falls, where the water\n\n"
         "turns a vivid turquoise before lunch at the View Café.")
SPLIT_OPENER = ("## Day Two\n\nThe itinerary keeps you at the pools for a swim before lunch at the café\n\n"
                "Which is why Day 2 starts before eight in the morning.")
# r4 #4 / #12 shape: a cut that closed the sentence mid-clause
TAIL = "## Day Two\n\nThe trek ends at the pools. Lunch is served at the café overlooking the.\n\nThe pools are cold."
TAIL_NO_MARK = "## Day Two\n\nThe trek ends at the pools. The itinerary leaves time to swim and"
CLEAN = ("## Day Two\n\nThe trek on Day 2 ends at the tiered pools of Kuang Si Falls. The water turns a vivid "
         "turquoise.\n\nThe itinerary keeps you there before lunch at the View Café.")


# ---------------------------------------------------------------- detectors


class TestDetectors:
    def test_a_sentence_split_across_two_paragraphs_is_found(self):
        found = ti.split_paragraphs(SPLIT)
        assert len(found) == 1 and found[0]["span"] is None
        assert "where the water ¶ turns a vivid turquoise" in found[0]["text"]

    def test_a_capitalised_continuation_opener_after_an_open_paragraph_is_found(self):
        assert len(ti.split_paragraphs(SPLIT_OPENER)) == 1

    @pytest.mark.parametrize("text", [TAIL, TAIL_NO_MARK])
    def test_a_sentence_cut_mid_clause_is_found(self, text):
        found = ti.dangling_tails(text)
        assert len(found) == 1 and found[0]["why"].startswith("ends mid-clause on '")

    def test_a_paragraph_left_without_its_end_is_found(self):
        """What the final pass leaves of SPLIT: it cuts the lower-case half (`fix_fragments`), so the first half
        stops on 'where the water' — the split paragraph is gone, the unfinished one is not."""
        settled = ti.settle(SPLIT, None)
        assert "turns a vivid" not in settled and ti.split_paragraphs(settled) == []
        found = ti.open_paragraphs(settled)
        assert len(found) == 1 and found[0]["text"].endswith("where the water")
        assert ti.open_paragraphs(SPLIT) == []  # reported once, as the split

    def test_structure_findings_reads_every_detector(self):
        assert len(ti.structure_findings(SPLIT + "\n\n" + TAIL.split("\n\n", 1)[1])) == 2
        assert ti.structure_findings(CLEAN) == []

    @pytest.mark.parametrize("text", [
        # headings, list items, hashtag lines, a CTA line without a period, FAQ lines
        "## Why Day 2 matters\n\nbecause the pools are the reason for the trip.",
        "Kuang Si Falls\n\nAnd the pools below it.",
        "What to pack:\n\n- a swimsuit\n- and a towel",
        "- Swim time before lunch\n- and a café stop",
        "The pools are cold. Bring a towel.\n\n#LuangPrabang #KuangSiFalls #Laos",
        f"The pools are cold. Bring a towel.\n\n{CTA}\n\nand see how Day 2 is built.",
        "## FAQ\n\n**Q: Can children swim?**\nA: Yes, in the lower pools.\n\n**Q: Is there lunch?**\nA: Yes.",
        "**The short version**\n\nand the long one follows.",
        # prepositions English strands, possessive pronouns, an upper-case 'A', emoticons and enumerators
        "The pools are a place worth coming back to. Ask what the travertine is made of.",
        "These are the people you travel with. The choice is yours.",
        "Grade A. Plan A is the morning trek.",
        "Pack two things: 1) a towel, 2) sandals :) and the trek is easy.",
        "The trail is fine for kids. It is easier than it looks.",
    ])
    def test_ordinary_shapes_do_not_fire(self, text):
        assert ti.structure_findings(text, CTA) == []

    def test_integrity_problems_reports_only_what_the_edit_added(self):
        assert h.integrity_problems(CLEAN, SPLIT) and h.integrity_problems(CLEAN, TAIL)
        assert h.integrity_problems(SPLIT, SPLIT) == []  # the writer's own split is not the edit's
        assert h.integrity_problems(SPLIT, SPLIT + "\n\nThe café opens at noon.") == []

    def test_the_admin_edit_path_reads_the_new_detectors_too(self):
        assert any("splits one sentence" in f for f in ti.edit_findings(CLEAN, SPLIT, None))


# ---------------------------------------------------------------- the invariant


class TestEnforceStep:
    def test_invariant_7_is_registered(self):
        assert rc.INVARIANTS[-1] == "integrity_kept"

    @pytest.mark.parametrize("broken", [SPLIT, TAIL, TAIL_NO_MARK])
    def test_an_edit_that_breaks_the_text_is_reverted(self, broken):
        text, log, problems = rc.enforce_step("length_trim", CLEAN, broken)
        assert text == CLEAN and problems
        assert log and all(line.startswith("reverted length_trim: ") for line in log) and len(log) <= 3

    def test_a_clean_edit_is_kept_byte_for_byte(self):
        after = CLEAN + "\n\nThe café opens at noon."
        assert rc.enforce_step("hygiene.autofix", CLEAN, after) == (after, [], [])
        assert rc.enforce_step("hygiene.autofix", CLEAN, CLEAN) == (CLEAN, [], [])

    def test_an_orphan_line_left_by_an_edit_is_reverted(self):
        before = ("The itinerary keeps you at the pools for a long swim before lunch.\n\n"
                  "Trail Pulse builds trips around knowing when to move and when to stop.")
        after = ("The itinerary keeps you at the pools for a long swim before lunch.\n\nThe itinerary keeps the swim."
                 "\n\nTrail Pulse builds trips around knowing when to move and when to stop.")
        text, log, _ = rc.enforce_step("fix_trip_facts", before, after)
        assert text == before and any("adds nothing" in line for line in log)

    def test_a_must_keep_step_is_never_reverted(self):
        text, log, problems = rc.enforce_step("hygiene.ensure_cta", CLEAN, SPLIT, must_keep=True)
        assert text == SPLIT and problems and log[0].startswith("kept hygiene.ensure_cta (must keep): ")

    def test_settle_compares_the_texts_the_final_pass_leaves(self):
        dup = "The water turns a vivid turquoise before lunch at the View Café near Luang Prabang."
        before = f"## Day Two\n\nThe trek ends at the pools. {dup}"
        after = f"{before} {dup}"   # an exact repeat: the final pass deletes it
        assert rc.enforce_step("fix_trip_facts", before, after)[0] == before
        assert rc.enforce_step("fix_trip_facts", before, after,
                               settle=lambda t: ti.settle(t, None))[0] == after

    def test_settle_is_final_pass_without_its_residue(self):
        hyg = h.HygieneContext(seed="", cta=CTA, brand_name="Trail Pulse")
        text = f"\n\nThe pools are cold.  Bring a towel.\n\n{CTA}."
        assert ti.settle(text, hyg) == ti.final_pass(text, hyg)[0]


# ---------------------------------------------------------------- the service wrapper


class TestStepGuard:
    def _guard(self):
        return service._StepGuard(h.HygieneContext(seed="", cta=CTA, brand_name="Trail Pulse"),
                                  ignore=h.brand_words("Trail Pulse"), cta=CTA)

    def test_a_revert_replaces_the_steps_log_and_is_counted(self):
        g = self._guard()
        text, log, undone = g("length_trim", CLEAN, SPLIT, ["trimmed a paragraph"], settle=False)
        assert (text, undone) == (CLEAN, True) and log[0].startswith("reverted length_trim: ")
        assert g.reverts == ["length_trim"]

    def test_a_clean_or_unchanged_step_keeps_its_log(self):
        g = self._guard()
        after = CLEAN + "\n\nThe café opens at noon."
        assert g("hygiene.autofix", CLEAN, after, ["split the first line"]) == (after, ["split the first line"], False)
        assert g("hygiene.autofix", CLEAN, CLEAN, []) == (CLEAN, [], False)
        assert g.reverts == []

    def test_ensure_cta_is_never_reverted(self):
        g = self._guard()
        text, log, undone = g("hygiene.ensure_cta", CLEAN, SPLIT, ["added the CTA"], must_keep=True)
        assert text == SPLIT and not undone and log[0] == "added the CTA" and len(log) > 1 and not g.reverts

    def test_step_reverts_are_a_soft_degraded_entry(self):
        status, reason, flags, log = service._degraded_final([], [], [], "approved", None,
                                                              step_reverts=["length_trim", "length_trim"])
        assert status == "approved" and reason is None
        entry = log[-1]["degraded"][0]
        assert (entry["name"], entry["detector"], entry["severity"], entry["count"]) == \
            (degraded.REPAIR_CONTRACT, "t9_step", "soft", 2)
        assert flags[-1]["gate"] == "degraded"

    def test_no_step_revert_adds_nothing(self):
        assert service._degraded_final([], [], [], "approved", None, step_reverts=[]) == ("approved", None, [], [])


# ---------------------------------------------------------------- review round (S250): no false positives


EMOJI = "That's not a hike. That's a pilgrimage, and you feel it in your legs 🌄"
LINK = "Lunch is at the café above the cascade. The full plan is at trailpulse.travel/laos"
HOOK = "Bhutan's quietest valley, and why we keep going back"


class TestReviewFalsePositives:
    @pytest.mark.parametrize("para", [
        EMOJI,
        "Swim first, lunch after, and the trail home is downhill 👇",
        "The plan for Day 2 is simple ➡",
        "Every guide says the same thing: “go early and stay late”",
        "The day ends at the café (lunch is included)",
        LINK,
        "Write to hello@wild.travel",
        "Book through https://trailpulse.travel/laos/day-2",
    ])
    def test_a_paragraph_that_ends_in_a_symbol_or_a_link_is_not_open(self, para):
        text = f"## Day Two\n\nThe trek ends at the pools.\n\n{para}\n\nThe pools are cold."
        assert ti.structure_findings(text, CTA) == []

    def test_a_list_lead_in_and_a_one_line_hook_are_not_open(self):
        text = (f"{HOOK}\n\nThe trek ends at the pools.\n\nHere is what to pack for the swim\n\n"
                "- a swimsuit\n- a dry shirt")
        assert ti.open_paragraphs(text, CTA) == []

    def test_a_letter_ended_paragraph_mid_piece_is_still_open(self):
        text = "## Day Two\n\nThe trek ends at the pools.\n\nThe water turns a vivid turquoise where the\n\n## Lunch"
        found = ti.open_paragraphs(text, CTA)
        assert len(found) == 1 and found[0]["kind"] == "open_paragraph"

    @pytest.mark.parametrize("before_para,old,new", [
        # the review's cases: a repair of the emoji line itself, and a repair next to a link-ended line
        (EMOJI, "That's not a hike.", "The trail is a real climb."),
        (LINK, "Lunch is at the café above the cascade.", "Lunch is at the café by the falls."),
        # an edit inside a paragraph that was ALREADY open: same count of open paragraphs → not new
        ("The pools sit below the falls. The water turns a vivid turquoise near the",
         "The pools sit below the falls.", "The pools are below the falls."),
    ])
    def test_splice_checked_keeps_a_repair_beside_an_ended_or_already_open_paragraph(self, before_para, old, new):
        from services.acp_content_writing import sentence_repair as sr
        text = f"{HOOK}\n\nThe trek ends at the pools on Day 2.\n\n{before_para}\n\n## Lunch\n\nThe café opens at noon."
        item = {"start": text.index(old), "end": text.index(old) + len(old), "sentence": old, "problem": "x"}
        out = sr.splice_checked(text, [item], [new], cta=CTA)
        assert new in out and "guard" not in item

    def test_a_hook_rewrite_without_an_end_mark_is_not_a_problem(self):
        body = "\n\nThe trek ends at the pools on Day 2. The café opens at noon."
        assert h.integrity_problems("Bhutan's quietest valley is worth the trip." + body,
                                    "Bhutan's quietest valley, and why we keep going back 👀" + body) == []
        assert h.integrity_problems("Bhutan's quietest valley is worth the trip." + body, HOOK + body) == []

    def test_paragraph_kinds_are_new_by_count_sentence_kinds_by_text(self):
        open_a = "The water turns a vivid turquoise near the lower pools"
        open_b = "The water turns a bright turquoise near the lower pools"
        base = "## Day Two\n\nThe trek ends at the pools.\n\n{}\n\n## Lunch\n\nThe café opens at noon."
        assert ti.new_structure_findings(base.format(open_a), base.format(open_b)) == []
        grew = ti.new_structure_findings(base.format("The pools are cold."), base.format(open_b))
        assert [f["kind"] for f in grew] == ["open_paragraph"]
        tail_a = base.format("Lunch is served at the café overlooking the.")
        tail_b = base.format("Lunch is served at the café beside the.")
        assert [f["kind"] for f in ti.new_structure_findings(tail_a, tail_b)] == ["dangling_tail"]


class TestStepGuardPerAttempt:
    def _guard(self):
        return service._StepGuard(h.HygieneContext(seed="", cta=CTA, brand_name="Trail Pulse"),
                                  ignore=h.brand_words("Trail Pulse"), cta=CTA)

    def test_only_the_kept_attempts_reverts_are_reported(self):
        g = self._guard()
        g.begin_attempt()
        g("length_trim", CLEAN, SPLIT, [], settle=False)  # attempt 1 reverted a trim
        g.begin_attempt()
        assert g.reverts == []  # attempt 2 starts clean
        g("fix_trip_facts", CLEAN, TAIL, [], settle=False)
        assert g.reverts == ["fix_trip_facts"]
        g.keep_previous()  # the AA-528 guard keeps attempt 1's text
        assert g.reverts == ["length_trim"]

    def test_the_attempt_loop_resets_and_restores_the_guard(self):
        import inspect
        src = inspect.getsource(service.run_write_background)
        assert "step_guard.begin_attempt()" in src and src.count("step_guard.keep_previous()") == 2
