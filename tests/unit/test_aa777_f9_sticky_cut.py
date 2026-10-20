"""AA-777 — F9 judge consistency.

Scope 1 (sticky pass): a job keeps the body sentences an earlier F9 verdict passed; a later verdict that
flags one of them, unchanged, is dropped (logged `F9_sticky_pass`). Scope 2 (blog, attempt 3): an F1/F9
truth item still failing after one sentence repair is cut in code (sentence_repair.delete_flagged +
tidy_after_cut), never leaving the blog under the F4 blog minimum — a cut that would keeps the hold."""
import json
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from services.acp_content_writing import quality_gates as qg
from services.acp_content_writing import service

CTA = "Ask us about the dates."
PIECE = ("The dzong stands where two rivers meet in Punakha. It was built in 1637 by the Zhabdrung. "
         "Every guest gets a private chef at dinner each night. " + CTA)
CHEF = "Every guest gets a private chef at dinner each night."
PASS = {"status": "pass", "brand_fit": "1", "cta_clear": "1", "human_read": "1",
        "failure_codes": [], "flagged_phrases": [], "notes": ""}
CHEF_FLAG = {"status": "manual_check", "failure_codes": ["FACT_CHECK_MANUAL_CHECK"], "flagged_phrases": [CHEF],
             "unsupported_claims": [{"sentence": CHEF, "claim": "private chef", "why_not_in_source": "no meals"}]}


def _f9(data: dict, text: str = PIECE, passed=None, raw_text=None, stop_reason=None):
    raw = {"text": raw_text if raw_text is not None else json.dumps({**PASS, **data}), "stop_reason": stop_reason}
    with patch.object(qg, "invoke_judge", return_value=raw), \
         patch.object(qg, "_log_t10_judge_call") as log, patch.object(qg, "logger") as lg:
        results = qg.gate_brand_voice(text, CTA, "rubric", source_text="seed", passed_sentences=passed)
    return {r["gate"]: r for r in results}["F9_cta_fact"], log, lg


def _key(sentence: str, text: str = PIECE) -> str:
    return qg._f9_located_key(text, sentence)


# ---------------------------------------------------------------- scope 1: sticky pass

class TestStickyPass:
    def test_a_pass_records_the_body_sentences(self):
        passed: set[str] = set()
        f9, _, _ = _f9({}, passed=passed)
        assert f9["passed"]
        assert _key(CHEF) in passed
        assert _key("It was built in 1637 by the Zhabdrung.") in passed

    def test_reflag_of_an_unchanged_passed_sentence_is_dropped(self):
        passed: set[str] = set()
        _f9({}, passed=passed)  # attempt 1: the judge passes every sentence
        f9, log, lg = _f9(CHEF_FLAG, passed=passed)  # attempt 2: the same sentence, now flagged
        assert f9["passed"] is True and f9["violations"] == []
        assert f9["sticky_pass"] == [CHEF]
        assert log.call_args.kwargs["extra"]["sticky_pass"] == 1
        lg.info.assert_any_call("F9_sticky_pass", dropped=1, sentences=[CHEF])

    def test_without_memory_the_same_flag_still_blocks(self):
        f9, _, _ = _f9(CHEF_FLAG)  # passed_sentences=None → behaviour before AA-777
        assert f9["passed"] is False and f9["blocking"] is True and "private chef" in f9["violations"][0]

    def test_a_changed_sentence_is_judged_again(self):
        passed: set[str] = set()
        _f9({}, passed=passed)
        cook = "Every guest gets a private cook and a sommelier at dinner each night."
        flag = {**CHEF_FLAG, "flagged_phrases": [cook],
                "unsupported_claims": [{"sentence": cook, "claim": "cook", "why_not_in_source": "no meals"}]}
        f9, _, _ = _f9(flag, text=PIECE.replace(CHEF, cook), passed=passed)
        assert f9["passed"] is False and f9["blocking"] is True and "sticky_pass" not in f9

    def test_a_flagged_sentence_is_never_recorded_as_passed(self):
        passed: set[str] = set()
        f9, _, _ = _f9(CHEF_FLAG, passed=passed)
        assert f9["passed"] is False
        assert _key(CHEF) not in passed
        assert _key("It was built in 1637 by the Zhabdrung.") in passed
        again, _, _ = _f9(CHEF_FLAG, passed=passed)  # flagged twice → still a real failure
        assert again["passed"] is False and again["blocking"] is True

    def test_an_unjustified_flag_still_counts_as_flagged(self):
        """A claim the gate drops for having no reason is still not a pass of that sentence."""
        passed: set[str] = set()
        _f9({**CHEF_FLAG, "unsupported_claims": [{"sentence": CHEF, "claim": "chef", "why_not_in_source": ""}]},
            passed=passed)
        assert _key(CHEF) not in passed

    def test_reasonless_verdict_and_outage_record_nothing(self):
        passed: set[str] = set()
        f9, _, _ = _f9({"status": "manual_check", "notes": "Something feels off."}, passed=passed)
        assert f9["violations"][0].startswith(qg.F9_NO_REASON) and passed == set()
        with patch.object(qg, "invoke_judge", side_effect=RuntimeError("down")):
            qg.gate_brand_voice(PIECE, CTA, "rubric", source_text="seed", passed_sentences=passed)
        assert passed == set()

    def test_title_claim_is_never_sticky(self):
        """A claim found only in the search title / meta stays with the SEO fit (AA-786), memory or not."""
        passed: set[str] = set()
        _f9({}, passed=passed)
        title = "Private chefs every night in Punakha valley"
        flag = {**CHEF_FLAG, "unsupported_claims": [{"sentence": title, "claim": "chefs", "why_not_in_source": "x"}]}
        with patch.object(qg, "invoke_judge", return_value={"text": json.dumps({**PASS, **flag})}), \
             patch.object(qg, "_log_t10_judge_call"):
            res = qg.gate_brand_voice(PIECE, CTA, "rubric", source_text="seed", seo_title=title,
                                      meta_description="m", passed_sentences=passed)
        f9 = res[0]
        assert "sticky_pass" not in f9 and f9["violations"][0].startswith("title unsupported claim")

    # -- PR review on cc265e22 --------------------------------------------------------------------

    def test_paraphrased_claim_quote_passes_nothing(self):
        """A claim quote that locates in no body sentence flags a sentence nobody can name: the verdict
        adds nothing, so the exact re-flag of that sentence next attempt still blocks."""
        butter = "Monks serve butter tea to every visitor at noon."
        text = PIECE.replace(CHEF, butter)
        para = "Visitors get butter tea from the monks around midday"
        passed: set[str] = set()
        f9, _, lg = _f9({**CHEF_FLAG, "flagged_phrases": [para], "unsupported_claims": [
            {"sentence": para, "claim": "butter tea", "why_not_in_source": "no tea"}]}, text=text, passed=passed)
        assert f9["passed"] is False and passed == set()
        lg.info.assert_any_call("F9_sticky_pass_skipped", truncated=False, unlocated_quote=True)
        exact = {**CHEF_FLAG, "flagged_phrases": [butter],
                 "unsupported_claims": [{"sentence": butter, "claim": "butter tea", "why_not_in_source": "no tea"}]}
        again, _, _ = _f9(exact, text=text, passed=passed)
        assert again["passed"] is False and again["blocking"] is True and "sticky_pass" not in again

    def test_title_claim_does_not_block_the_passed_set(self):
        """A claim quoting the search title is located (in the title), so the body still records."""
        passed: set[str] = set()
        title = "Private chefs every night in Punakha valley"
        flag = {**CHEF_FLAG, "unsupported_claims": [{"sentence": title, "claim": "chefs", "why_not_in_source": "x"}]}
        with patch.object(qg, "invoke_judge", return_value={"text": json.dumps({**PASS, **flag})}), \
             patch.object(qg, "_log_t10_judge_call"):
            qg.gate_brand_voice(PIECE, CTA, "rubric", source_text="seed", seo_title=title,
                                meta_description="m", passed_sentences=passed)
        assert _key("It was built in 1637 by the Zhabdrung.") in passed

    def test_truncated_verdict_passes_nothing(self):
        """json_repair salvages a reply cut before its second claim: that claim's sentence must not
        read as passed."""
        full = {**PASS, **CHEF_FLAG, "unsupported_claims": [
            {"sentence": "It was built in 1637 by the Zhabdrung.", "claim": "1637", "why_not_in_source": "x"},
            {"sentence": CHEF, "claim": "private chef", "why_not_in_source": "no meals"}]}
        body = json.dumps(full)
        cut = body[:body.index('{"sentence": "Every guest')]
        passed: set[str] = set()
        f9, _, _ = _f9({}, passed=passed, raw_text=cut)
        assert f9["passed"] is False  # the salvaged flag still counts
        assert passed == set()

    @pytest.mark.parametrize("reason", ["length", "max_tokens"])
    def test_verdict_cut_at_max_tokens_passes_nothing(self, reason):
        passed: set[str] = set()
        _f9({}, passed=passed, stop_reason=reason)
        assert passed == set()

    def test_a_json_list_reply_is_judge_unavailable(self):
        passed: set[str] = set()
        f9, _, _ = _f9({}, passed=passed, raw_text="[1, 2]")
        assert f9["violations"][0].startswith("judge unavailable") and f9["blocking"] is False
        assert passed == set()

    def test_changed_neighbour_makes_the_sentence_new(self):
        """'It was founded in 1692.' passed after 'We visit Taktsang Monastery.' is a different claim
        after 'We visit Kyichu Lhakhang.': a justified re-flag is kept."""
        founded = "It was founded in 1692 by the lama."
        before = f"We visit Taktsang Monastery on day two. {founded} " + CTA
        after = f"We visit Kyichu Lhakhang on day two. {founded} " + CTA
        passed: set[str] = set()
        _f9({}, text=before, passed=passed)
        flag = {**CHEF_FLAG, "flagged_phrases": [founded],
                "unsupported_claims": [{"sentence": founded, "claim": "1692", "why_not_in_source": "wrong temple"}]}
        f9, _, _ = _f9(flag, text=after, passed=passed)
        assert f9["passed"] is False and f9["blocking"] is True and "sticky_pass" not in f9
        same, _, _ = _f9(flag, text=before, passed=passed)  # unchanged context → still sticky
        assert same["passed"] is True and same["sticky_pass"] == [founded]

    def test_a_new_symbol_makes_the_sentence_new(self):
        small = "Groups of 12 travellers share two guides on the trek."
        text = PIECE.replace(CHEF, small)
        passed: set[str] = set()
        _f9({}, text=text, passed=passed)
        plus = small.replace("12", "12+")
        flag = {**CHEF_FLAG, "flagged_phrases": [plus],
                "unsupported_claims": [{"sentence": plus, "claim": "12+", "why_not_in_source": "max 12"}]}
        f9, _, _ = _f9(flag, text=PIECE.replace(CHEF, plus), passed=passed)
        assert f9["passed"] is False and "sticky_pass" not in f9

    def test_sticky_sentence_quoted_in_notes_is_not_text_integrity(self):
        passed: set[str] = set()
        _f9({}, passed=passed)
        flag = {**CHEF_FLAG, "failure_codes": ["FACT_CHECK_MANUAL_CHECK", "TEXT_INTEGRITY"],
                "broken_sentences": [], "notes": f"'{CHEF}' is not supported by the source."}
        f9, _, _ = _f9(flag, passed=passed)
        assert f9["sticky_pass"] == [CHEF]
        assert not any(v.startswith("TEXT_INTEGRITY") for v in f9["violations"])
        assert f9["blocking"] is False  # no checkable reason left: a warn, never a hold

    def test_a_loose_note_quote_keeps_the_sentences_it_copies_out(self):
        """A note quote that locates nowhere (here: ambiguous between two sentences) still keeps out
        every sentence it copies."""
        lunch = "Every guest gets a private chef at lunch each day."
        text = PIECE.replace(CHEF, f"{CHEF} {lunch}")
        quote = "every guest gets a private chef each night"
        assert qg._locate_in(text, quote) is None
        passed: set[str] = set()
        _f9({"status": "pass", "notes": f"Fine, though '{quote}' is bold."}, text=text, passed=passed)
        assert _key(CHEF, text) not in passed and _key(lunch, text) not in passed
        assert _key("It was built in 1637 by the Zhabdrung.", text) in passed

    def test_run_quality_gates_hands_the_set_to_f9(self):
        passed: set[str] = {"x"}
        ok = [qg._result("F9_cta_fact", []), qg._result("F9_brand_style", [], blocking=False)]
        with patch.object(qg, "gate_brand_voice", return_value=ok) as bv:
            qg.run_quality_gates(content_text=PIECE, atom_text="seed", cta=CTA,
                                 brand_rubric_text="r", channel="facebook", f9_passed_sentences=passed)
        assert bv.call_args.kwargs["passed_sentences"] is passed


# ---------------------------------------------------------------- scope 2: blog attempt-3 truth cut

_WORDS = ("amber basalt cedar dunes ember fjord glacier harbour iris juniper kestrel lantern meadow "
          "nettle orchard pebble quartz rowan saffron thistle umber vellum willow yarrow zephyr birch "
          "copper delta falcon granite heron indigo jasmine kelp larch marble nutmeg osprey").split()


def _filler(n: int) -> list[str]:
    out = []
    for i in range(n):
        a, b, c = _WORDS[i % len(_WORDS)], _WORDS[(i * 7 + 3) % len(_WORDS)], _WORDS[(i * 11 + 5) % len(_WORDS)]
        out.append(f"Stop {i + 1} pairs the {a} terrace with a {b} courtyard and a quiet {c} garden.")
    return out


def _blog(n_filler: int) -> str:
    body = _filler(n_filler)
    paras = [" ".join(body[i:i + 5]) for i in range(0, len(body), 5)]
    return ("Punakha Dzong sits where two rivers meet below the pass.\n\n## The valley\n\n"
            + "\n\n".join(paras) + f"\n\n{CHEF} The kitchen closes late.\n\n{CTA}\n\n## FAQ\n\n"
            "**Q: When is the festival?**\nA: The festival runs in early spring each year.\n\n"
            "**Q: How do we reach the dzong?**\nA: A short road links the town to the bridge.\n\n"
            "**Q: Is the dzong still in use?**\nA: Monks still live and pray inside its walls.\n")


LONG_BLOG, SHORT_BLOG = _blog(80), _blog(10)
F9_CHEF = {"gate": "F9_cta_fact", "passed": False, "blocking": True, "repairable": True,
           "violations": [f'audit manual_check: FACT_CHECK_MANUAL_CHECK — unsupported claim(s): "{CHEF}" '
                          "(claims: private chef; source: no meals)"]}


def _items(text):
    from services.acp_content_writing import sentence_repair
    return sentence_repair.plan(text, [F9_CHEF])


class TestCutBlogTruthItems:
    def test_fixture_lengths(self):
        lo, _ = qg._length_bounds_for_channel("blog")
        assert len(service._plain(LONG_BLOG)) - len(CHEF) > lo + 200
        assert len(service._plain(SHORT_BLOG)) < lo
        assert qg.gate_faq_contract(LONG_BLOG)["passed"]

    def test_truth_item_is_cut(self):
        text, rest, entry = service._cut_blog_truth_items(LONG_BLOG, _items(LONG_BLOG), cta=CTA)
        assert CHEF not in text and "The kitchen closes late." in text and CTA in text
        assert rest == [] and entry["repair_mode"] == "truth_cut" and entry["sentences_cut"] == 1
        assert entry["cut_sentences"] == [CHEF]

    def test_cut_under_the_blog_minimum_is_refused(self):
        items = _items(SHORT_BLOG)
        text, rest, entry = service._cut_blog_truth_items(SHORT_BLOG, items, cta=CTA)
        assert text == SHORT_BLOG and rest == items
        assert entry["repair_mode"] == "truth_cut_refused" and entry["sentences_cut"] == 0
        assert "minimum" in entry["truth_cut_refused"]

    def test_non_truth_items_go_on_to_the_repair(self):
        items = _items(LONG_BLOG)
        style = dict(items[0], gate="F15_voice", problem="stock phrase")
        style_text = "Stop 3 pairs"
        s, e = LONG_BLOG.index(style_text), LONG_BLOG.index(".", LONG_BLOG.index(style_text)) + 1
        style.update(start=s, end=e, sentence=LONG_BLOG[s:e])
        text, rest, entry = service._cut_blog_truth_items(LONG_BLOG, items + [style], cta=CTA)
        assert entry["repair_mode"] == "truth_cut" and CHEF not in text
        assert [r["gate"] for r in rest] == ["F15_voice"]
        assert text[rest[0]["start"]:rest[0]["end"]] == style["sentence"]  # relocated in the cut text

    def test_no_truth_item_is_a_no_op(self):
        items = [dict(it, gate="F15_voice") for it in _items(LONG_BLOG)]
        assert service._cut_blog_truth_items(LONG_BLOG, items, cta=CTA) == (LONG_BLOG, items, None)

    def test_truth_repaired_before(self):
        snap = [{"gate": "F9_cta_fact", "passed": False, "blocking": True}]
        assert service._truth_repaired_before([{"repair_mode": "sentences", "gate_ledger": snap}])
        assert not service._truth_repaired_before([{"repair_mode": None, "gate_ledger": snap}])  # full rewrite
        style = [{"gate": "F15_voice", "passed": False, "blocking": True}]
        assert not service._truth_repaired_before([{"repair_mode": "sentences", "gate_ledger": style}])


def _context():
    return {"atom_text": "Punakha Dzong sits where two rivers meet.", "facts_text": "",
            "goal": {"key": "promotion", "name": "P", "description": "d", "logic": "AIDA"},
            "channel_style": {"key": "blog"}, "brand_audience": {}, "chosen": {"name": "A"},
            "cta": CTA, "destination": None, "trip_name": None, "keyword": None,
            "brand_rubric_text": "rubric", "channel": "blog", "atom_id": "atom_1"}


def _outcome(ledger):
    first = next((g for g in ledger if not g["passed"] and g.get("blocking", True)), None)
    return {"passed": first is None, "gate_ledger": ledger, "first_failure": first, "flags": []}


async def _run_blog(text, repair_side_effect=None):
    """Every attempt fails F9 on the chef sentence while it is in the text; the repair keeps it."""
    def same_sentences(*, sentences, **_):
        return [s["sentence"] for s in sentences], 0.0

    def gates(**kw):
        return _outcome([dict(F9_CHEF)] if CHEF in kw["content_text"] else [])
    ident = (lambda t, *a, **k: (t, []))
    seo = {"seo_title": "Punakha Dzong where two rivers meet", "meta_description": "m", "slug": "punakha"}
    with patch.object(service, "write_content", return_value=(text, 0.01, seo, None)), \
         patch.object(service, "rewrite_with_feedback") as rw, \
         patch.object(service, "repair_sentences", side_effect=repair_side_effect or same_sentences) as rep, \
         patch.object(service, "run_quality_gates", side_effect=gates) as rqg, \
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
    return fin.call_args.kwargs, rep, rqg


@pytest.mark.asyncio
async def test_blog_attempt_three_cuts_the_truth_item_instead_of_a_second_repair():
    kw, rep, rqg = await _run_blog(LONG_BLOG)
    assert rep.call_count == 1  # attempt 2's sentence repair only; attempt 3 cuts
    assert rqg.call_count == 3 and CHEF not in rqg.call_args_list[2].kwargs["content_text"]
    assert kw["status"] == "approved" and kw["attempt_number"] == 3
    assert CHEF not in kw["content_text"] and "The kitchen closes late." in kw["content_text"]
    entry = kw["repair_log"][-1]
    assert entry["repair_mode"] == "truth_cut" and entry["sentences_cut"] == 1
    # one sticky-pass set for the whole job, handed to every attempt's gates
    sets = [c.kwargs["f9_passed_sentences"] for c in rqg.call_args_list]
    assert isinstance(sets[0], set) and all(s is sets[0] for s in sets)
    assert isinstance(sets[0], qg.F9PassMemory)  # AA-865 — it also keeps the last judged text


@pytest.mark.asyncio
async def test_blog_cut_that_would_go_under_the_minimum_keeps_the_hold():
    kw, rep, _ = await _run_blog(SHORT_BLOG)
    assert rep.call_count == 2  # refused cut → the old path: one more repair of the item
    assert kw["status"] == "held" and kw["held_reason"].startswith("F9_cta_fact")
    assert CHEF in kw["content_text"]
    refused = [e for e in kw["repair_log"] if e.get("truth_cut_refused")]
    assert len(refused) == 1 and "minimum" in refused[0]["truth_cut_refused"]
    assert refused[0]["repair_mode"] == "sentences"  # what then ran: the repair


@pytest.mark.asyncio
async def test_blog_failed_attempt_three_repair_never_cuts_under_the_minimum():
    """The S234 r3 fallback (repair call fails → delete_flagged) must not take a blog under its minimum."""
    first = (lambda *, sentences, **_: ([s["sentence"] for s in sentences], 0.0))
    kw, rep, _ = await _run_blog(SHORT_BLOG, repair_side_effect=[
        first(sentences=_items(SHORT_BLOG)), service.SentenceRepairError("bad reply")])
    assert rep.call_count == 2
    assert kw["status"] == "held" and CHEF in kw["content_text"]
    assert not any(e.get("repair_mode") == "sentences_cut" for e in kw["repair_log"])


# ---------------------------------------------------------------- AA-786 review leftover: tag before the mark

TAGGED_HOOK = "The dzong stands where two rivers meet in Punakha [R:a1]."
BOTH = ('audit manual_check: FACT_CHECK_MANUAL_CHECK — unsupported claim(s): '
        '"The dzong stands where two rivers meet in Punakha." (claims: rivers; source: silent); '
        f'"{CHEF}" (claims: chef; source: no meals)')


def test_linkedin_kept_sentence_with_a_tag_before_its_full_stop_is_not_counted_as_cut():
    """The hook is never cut; quoted as '… Punakha .' it was not found in the cut text and counted as cut,
    so F9 was waived while the flagged hook was still in the post."""
    text = PIECE.replace("The dzong stands where two rivers meet in Punakha.", TAGGED_HOOK)
    f9 = {"gate": "F9_cta_fact", "passed": False, "blocking": True, "repairable": True, "violations": [BOTH]}
    out, ledger, _, entry = service._cut_truth_items(text, [f9], [], channel="linkedin", cta=CTA)
    assert CHEF not in out and TAGGED_HOOK in out
    assert entry["sentences_cut"] == 1 and len(entry["sentences_left"]) == 1
    assert ledger[0]["blocking"] is True and "waived" not in ledger[0]


def test_blog_cut_counting_ignores_a_tag_before_the_mark():
    hook = "Punakha Dzong sits where two rivers meet below the pass."
    tagged = (LONG_BLOG.replace("The kitchen closes late.", "The kitchen closes late [R:a1].")
              .replace(hook, hook[:-1] + " [R:a2]."))
    flag = ('audit manual_check: FACT_CHECK_MANUAL_CHECK — unsupported claim(s): '
            f'"{hook}" (claims: pass; source: silent); "{CHEF}" (claims: chef; source: no meals)')
    from services.acp_content_writing import sentence_repair
    items = sentence_repair.plan(tagged, [dict(F9_CHEF, violations=[flag])])
    assert len(items) == 2
    out, rest, entry = service._cut_blog_truth_items(tagged, items, cta=CTA)
    assert entry["sentences_cut"] == 1 and CHEF not in out
    assert len(rest) == 1 and rest[0]["sentence"].startswith("Punakha Dzong sits")  # the hook goes to the repair
