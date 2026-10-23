"""AA-984 — T9 blog holds from the S247 round-9 checkpoint, as regression shapes.

- 781b5de7 (terra-family, Mongolia): the F9 judge flagged a section HEADING ('## What a Ruined Monastery Tells
  Families About Mongolia'). locate_sentence cannot locate a line with no end mark, so plan() returned None and
  the piece was held at attempt 2 with no repair and no attempt-3 truth cut. Now: a heading repair item, a
  heading-safe splice, and a place-name fallback at the blog attempt-3 truth cut (a heading is never deleted).
- 32789658 (atlas-hearth, Bhutan): F4_extreme_length at 14,576 chars after the attempt-2 full rewrite. Now:
  length_trim.trim_to_band drops whole low-value paragraphs before the gates (never a heading, the FAQ or the
  CTA), and an over-max rewrite is told the target size.
- 3712d4dd (atlas-hearth, 5 prose F9 claims): unchanged by this issue — its claims are prose plan() already
  locates; checked here so the heading path never swallows them."""
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from services.acp_content_writing import length_trim, sentence_repair, service, trip_facts
from services.acp_content_writing import quality_gates as qg

# ---------------------------------------------------------------- 781b5de7: F9 claim on a heading

RUINED = "What a Ruined Monastery Tells Families About Mongolia"
F9_RUINED = ('audit manual_check: FACT_CHECK_MANUAL_CHECK — unsupported claim(s): '
             f'"{RUINED}" (claims: That the piece concerns a ruined monastery in Mongolia and draws lessons for '
             'families from it.; source: The SOURCE contains no monastery, ruined or otherwise, and provides no '
             'facts about such a site.)')
MONG_SEED = ("a local Buddhist monastery — visit\nLocation: a Buddhist monastery near Bulgan, Mongolia\n"
             "Day 3 of the trip\nSource itinerary: Visit a local Buddhist monastery on the way north.\n\n"
             "Uran Togoo — hike\nLocation: Uran Togoo, Bulgan Province, Mongolia\nDay 4 of the trip\n"
             "Source itinerary: Hike up the Uran Togoo extinct volcano.")
MONG_CTA = "Ask Terra Family Expeditions about dates for your family."
FAQ = ("## FAQ\n\n"
       "**Q: Is the monastery open to visitors?**\nA: The Buddhist monastery welcomes visitors on the route north.\n\n"
       "**Q: How hard is the Uran Togoo hike?**\nA: The Uran Togoo path climbs gently to the crater rim.\n\n"
       "**Q: Which province is Uran Togoo in?**\nA: Uran Togoo stands in Bulgan Province in northern Mongolia.\n")


def _mong_blog(filler_paragraphs: int = 0) -> str:
    filler = "\n\n".join(" ".join(_unique_sentences(5, seed=200 + 7 * p)) for p in range(filler_paragraphs))
    return (f"## {RUINED}\n\n"
            "The route north opens at the Buddhist monastery near Bulgan [R:a1]. Monks still keep its prayer "
            "halls in use.\n\n"
            "## Uran Togoo: A Volcano for Curious Children\n\n"
            "Uran Togoo is an extinct volcano with a crater rim walk [R:a2]."
            + (f"\n\n{filler}" if filler else "") + f"\n\n{MONG_CTA}\n\n{FAQ}")


def _ledger(*violations):
    return [{"gate": "F9_cta_fact", "passed": False, "blocking": True, "repairable": True,
             "violations": list(violations)}]


MOMENTS = trip_facts.parse_moments(MONG_SEED)


class TestHeadingClaim:
    def test_the_old_locator_misses_the_heading(self):
        assert sentence_repair.locate_sentence(_mong_blog(), RUINED) is None

    def test_plan_returns_a_heading_item_after_the_marker(self):
        text = _mong_blog()
        items = sentence_repair.plan(text, _ledger(F9_RUINED))
        assert items is not None and len(items) == 1
        it = items[0]
        assert it["heading"] is True and it["gate"] == "F9_cta_fact"
        assert text[it["start"]:it["end"]] == RUINED and text[it["start"] - 3:it["start"]] == "## "
        assert "HEADING" in it["problem"] and "ruined monastery" in it["problem"]

    def test_heading_and_prose_claims_are_both_planned(self):
        prose = "Monks still keep its prayer halls in use."
        v = F9_RUINED + f'; "{prose}" (claims: monks in use; source: not stated)'
        items = sentence_repair.plan(_mong_blog(), _ledger(v))
        assert sorted(bool(it.get("heading")) for it in items) == [False, True]

    def test_faq_heading_is_never_a_target(self):
        assert sentence_repair.locate_heading(_mong_blog(), "FAQ heading of the piece") is None
        assert sentence_repair.locate_heading(_mong_blog(), "FAQ") is None

    def test_partial_quote_matches_one_heading_only(self):
        text = _mong_blog()
        span = sentence_repair.locate_heading(text, "Ruined Monastery Tells Families About Mongolia")  # 6 of 8
        assert span is not None and text[span[0]:span[1]] == RUINED
        assert sentence_repair.locate_heading(text, "Ruined Monastery Tells Families") is None  # 4 of 8 < 60 %
        assert sentence_repair.locate_heading(text, "Families About") is None  # 2-word run: too loose

    def test_a_prose_quote_that_contains_a_heading_is_not_a_heading_claim(self):
        """S248 review: a paraphrased prose claim that repeats a heading inside a longer quote is a prose claim;
        rewriting (or at attempt 3 replacing) the correct heading would leave the real claim in place."""
        text = _mong_blog()
        quote = ("Uran Togoo: A Volcano for Curious Children is where kids learn the volcano erupted 8,000 "
                 "years ago")
        assert sentence_repair.locate_heading(text, quote) is None
        assert sentence_repair.plan(text, _ledger(f'audit: unsupported claim(s): "{quote}" (claims: x)')) is None

    def test_a_short_fragment_of_a_long_heading_is_not_a_heading_claim(self):
        text = _mong_blog()
        assert sentence_repair.locate_heading(text, "for curious children") is None  # 3 of 7 words
        span = sentence_repair.locate_heading(text, "## Uran Togoo: A Volcano for Curious Children [R:a2]")
        assert span is not None and text[span[0]:span[1]] == "Uran Togoo: A Volcano for Curious Children"

    def test_splice_keeps_the_marker_and_drops_the_end_mark(self):
        text = _mong_blog()
        items = sentence_repair.plan(text, _ledger(F9_RUINED))
        out = sentence_repair.splice(text, items, ["## A Buddhist Monastery Near Bulgan."])
        assert out.startswith("## A Buddhist Monastery Near Bulgan\n\nThe route north")

    def test_empty_repair_takes_the_place_name_not_a_cut(self):
        text = _mong_blog()
        items = sentence_repair.plan(text, _ledger(F9_RUINED))
        out = sentence_repair.splice_checked(text, items, [""], cta=MONG_CTA, moments=MOMENTS)
        assert out.startswith("## A local Buddhist monastery\n\nThe route north")
        assert items[0]["fallback_used"] is True

    def test_without_moments_an_empty_repair_keeps_the_heading(self):
        text = _mong_blog()
        items = sentence_repair.plan(text, _ledger(F9_RUINED))
        assert sentence_repair.splice_checked(text, items, [""], cta=MONG_CTA) == text
        assert items[0]["guard"] == "heading_kept"

    def test_repeat_guard_never_turns_a_heading_into_a_cut(self):
        text = _mong_blog()
        items = sentence_repair.plan(text, _ledger(F9_RUINED))
        out = sentence_repair.splice(text, items, ["The route north opens at the Buddhist monastery near Bulgan"])
        assert out.startswith("## The route north opens at the Buddhist monastery near Bulgan\n")

    def test_clean_heading(self):
        assert sentence_repair.clean_heading("## **A Buddhist Monastery.**") == "A Buddhist Monastery"
        assert sentence_repair.clean_heading("Why visit Uran Togoo?") == "Why visit Uran Togoo?"
        assert sentence_repair.clean_heading("") == "" and sentence_repair.clean_heading("## .") == ""
        assert sentence_repair.clean_heading("word " * 30) == ""  # a paragraph, not a heading

    def test_delete_flagged_replaces_the_heading_never_cuts_it(self):
        text = _mong_blog()
        items = sentence_repair.plan(text, _ledger(F9_RUINED))
        out = sentence_repair.delete_flagged(text, items, cta=MONG_CTA, moments=MOMENTS)
        assert out is not None and out.startswith("## A local Buddhist monastery\n")
        assert out.count("\n## ") == text.count("\n## ")
        assert sentence_repair.delete_flagged(text, items, cta=MONG_CTA) is None  # no moments → stays

    def test_fallback_is_none_when_another_heading_already_reads_the_same(self):
        text = _mong_blog().replace("## Uran Togoo: A Volcano for Curious Children", "## A local Buddhist monastery")
        items = sentence_repair.plan(text, _ledger(F9_RUINED))
        assert sentence_repair.heading_fallback(text, items[0], MOMENTS) is None

    def test_blog_truth_cut_replaces_the_heading(self):
        text = _mong_blog(filler_paragraphs=9)
        assert service._plain(text).__len__() > qg._length_bounds_for_channel("blog")[0]
        items = sentence_repair.plan(text, _ledger(F9_RUINED))
        out, rest, entry = service._cut_blog_truth_items(text, items, cta=MONG_CTA, moments=MOMENTS)
        assert entry["repair_mode"] == "truth_cut" and entry["headings_replaced"] == 1
        assert entry["sentences_cut"] == 0 and entry["replaced_headings"] == [RUINED]
        assert rest == [] and out.startswith("## A local Buddhist monastery\n") and RUINED not in out


# ---------------------------------------------------------------- 3712d4dd: prose claims are unchanged

def test_prose_claims_still_locate_as_sentences():
    text = ("## The Textile Museum as Cultural Text\n\nA length of hand-woven fabric is not simply a textile [R:t1]. "
            "The museum sits near the clock tower [R:t1].\n\n" + FAQ)
    v = ('audit manual_check: FACT_CHECK_MANUAL_CHECK — unsupported claim(s): "A length of hand-woven fabric is '
         'not simply a textile." (claims: rhetoric; source: not this object)')
    items = sentence_repair.plan(text, _ledger(v))
    assert len(items) == 1 and not items[0].get("heading")
    assert text[items[0]["start"]:items[0]["end"]] == "A length of hand-woven fabric is not simply a textile [R:t1]."


# ---------------------------------------------------------------- the loop: 781b5de7 ends approved

def _context(seed, cta, text_len_hint=None):
    return {"atom_text": seed, "facts_text": "",
            "goal": {"key": "promotion", "name": "P", "description": "d", "logic": "AIDA"},
            "channel_style": {"key": "blog"}, "brand_audience": {}, "chosen": {"name": "A"},
            "cta": cta, "destination": None, "trip_name": None, "keyword": None,
            "brand_rubric_text": "rubric", "channel": "blog", "atom_id": "atom_1"}


def _outcome(ledger):
    first = next((g for g in ledger if not g["passed"] and g.get("blocking", True)), None)
    return {"passed": first is None, "gate_ledger": ledger, "first_failure": first, "flags": []}


async def _run(text, seed, cta, gates, repair=None):
    def same(*, sentences, **_):
        return [s["sentence"] for s in sentences], 0.0
    ident = (lambda t, *a, **k: (t, []))
    seo = {"seo_title": "Family travel in northern Mongolia", "meta_description": "m", "slug": "mongolia"}
    with patch.object(service, "write_content", return_value=(text, 0.01, seo, None)), \
         patch.object(service, "rewrite_with_feedback") as rw, \
         patch.object(service, "repair_sentences", side_effect=repair or same) as rep, \
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
        await service.run_write_background(uuid4(), uuid4(), _context(seed, cta), pool=MagicMock())
    return fin.call_args.kwargs, rep, rqg, rw


@pytest.mark.asyncio
async def test_heading_claim_piece_is_repaired_not_held():
    """781b5de7's path: attempt 1 F9 on a prose sentence + the heading; attempt 2's repair leaves the heading
    (the fake repair returns every sentence unchanged); attempt 3's truth cut gives it the place name."""
    prose = "Monks still keep its prayer halls in use."
    both = F9_RUINED + f'; "{prose}" (claims: monks in use; source: not stated)'

    def gates(**kw):
        t = kw["content_text"]
        if RUINED in t:
            return _outcome(_ledger(both if prose in t else F9_RUINED))
        return _outcome([])
    kw, rep, rqg, rw = await _run(_mong_blog(filler_paragraphs=9), MONG_SEED, MONG_CTA, gates)
    rw.assert_not_called()
    assert rep.call_count == 1 and any(s.get("heading") for s in rep.call_args.kwargs["sentences"])
    assert kw["status"] == "approved" and kw["attempt_number"] == 3
    assert kw["content_text"].startswith("## A local Buddhist monastery\n")
    entry = next(e for e in kw["repair_log"] if e.get("repair_mode") == "truth_cut")
    assert entry["headings_replaced"] == 1


@pytest.mark.asyncio
async def test_heading_repair_reply_is_spliced_as_a_heading():
    """A heading claim seen first at attempt 1 is repaired at attempt 2 by the repair call, as a heading."""
    def gates(**kw):
        return _outcome(_ledger(F9_RUINED) if RUINED in kw["content_text"] else [])

    def heading_reply(*, sentences, **_):
        return ["A Buddhist Monastery Near Bulgan." if s.get("heading") else s["sentence"] for s in sentences], 0.0
    kw, rep, _, rw = await _run(_mong_blog(filler_paragraphs=9), MONG_SEED, MONG_CTA, gates, repair=heading_reply)
    rw.assert_not_called()
    assert kw["status"] == "approved" and kw["attempt_number"] == 2
    assert kw["content_text"].startswith("## A Buddhist Monastery Near Bulgan\n\nThe route north")


# ---------------------------------------------------------------- 32789658: F4 over the blog maximum

_SYL = ("ka", "lo", "mi", "ten", "sor", "va", "dru", "pel", "zin", "go", "rha", "tu", "bey", "nok", "shi", "wam")


def _word(n: int) -> str:
    out = ""
    for _ in range(3):
        out += _SYL[n % len(_SYL)]
        n //= len(_SYL)
    return out + "an"


def _unique_sentences(k: int, seed: int) -> list[str]:
    """k sentences of never-repeated pseudo-words (no near-duplicate, echo or orphan line between any two)."""
    out = []
    for i in range(k):
        base = (seed * 37 + i * 11) * 9
        w = [_word(base + j) for j in range(9)]
        out.append(f"{w[0].capitalize()} {w[1]} {w[2]} beside the {w[3]} {w[4]} and {w[5]} {w[6]} "
                   f"the {w[7]} {w[8]}.")
    return out


BHU_CTA = "Plan your private journey through Bhutan with Atlas & Hearth."
TSAATAN_SEED = ("Tsaatan reindeer herders' settlements — excursions to Tsaatan reindeer herders' settlements\n"
                "Location: Tsaatan Reindeer Herder Settlements, Khövsgöl Province, Mongolia\nDay 5 of the trip\n"
                "Source itinerary: excursions to Tsaatan reindeer herders' settlements\n"
                "Place background (checked facts about the place itself — you may use these; they say nothing "
                "about what this trip does):\n"
                "- Their territory lies deep in the taiga, and access requires a multi-hour horse or reindeer ride, "
                "often crossing rivers and boggy forest terrain.")
FAMILY_AUDIENCE = "Affluent families with children aged 7–16"


def _para(seed: int, tagged: bool, k: int = 5) -> str:
    s = _unique_sentences(k, seed)
    if tagged:
        s = [x[:-1] + f" [R:b{seed}]." for x in s]
    return " ".join(s)


def _bhutan_blog(sections: int = 8, paras: int = 3, untagged_last: bool = True) -> str:
    out = [f"Bhutan reveals itself slowly through its dzongs and temples [R:b0]. {_para(1, True, 3)}"]
    for n in range(sections):
        out.append(f"## Section {_word(900 + n).capitalize()}")
        for p in range(paras):
            out.append(_para(1000 + 50 * n + p, tagged=not (untagged_last and p == paras - 1)))
    out.append("## Plan Your Private Journey Through Bhutan")
    out.append(f"{_para(5000, True, 2)} {BHU_CTA}")
    faq = ("## FAQ\n\n**Q: When do the dzongs open?**\nA: The dzongs open to visitors on weekday afternoons.\n\n"
           "**Q: Is a guide required?**\nA: A licensed guide accompanies every visitor in Bhutan.\n\n"
           "**Q: What should we wear?**\nA: Long sleeves and covered knees suit every dzong visit.\n")
    return "\n\n".join(out) + "\n\n" + faq


def _headings(text):
    return [ln for ln in text.splitlines() if ln.startswith("#")]


class TestTrimToBand:
    def test_fixture_is_over_the_band(self):
        assert length_trim.measure(_bhutan_blog()) > qg._BLOG_MAX_CHARS

    def test_over_long_blog_is_trimmed_into_the_band(self):
        text = _bhutan_blog()
        out, log = length_trim.trim_to_band(text, channel="blog", cta=BHU_CTA)
        n = length_trim.measure(out)
        assert qg._BLOG_MIN_CHARS <= n <= length_trim.BLOG_TRIM_TARGET_CHARS
        assert qg.gate_extreme_length(qg.strip_citation_tags(out), "blog")["passed"]
        assert _headings(out) == _headings(text)  # every heading kept
        assert out[out.index("## FAQ"):] == text[text.index("## FAQ"):]  # the FAQ untouched
        assert BHU_CTA in out and qg.gate_faq_contract(out)["passed"]
        assert len(log) == 1 and log[0].startswith("trimmed ") and "max 13,000" in log[0]

    def test_each_section_keeps_its_first_paragraph_and_untagged_go_first(self):
        text = _bhutan_blog()
        out, _ = length_trim.trim_to_band(text, channel="blog", cta=BHU_CTA)
        for n in range(8):
            assert _para(1000 + 50 * n, tagged=True) in out
        dropped = [p for p in text.split("\n\n") if p not in out]
        assert dropped and all("[R:" not in p for p in dropped)  # 8 untagged paragraphs cover the cut

    def test_inside_the_band_or_another_channel_is_a_no_op(self):
        short = _bhutan_blog(sections=4)
        assert length_trim.measure(short) <= qg._BLOG_MAX_CHARS
        assert length_trim.trim_to_band(short, channel="blog", cta=BHU_CTA) == (short, [])
        text = _bhutan_blog()
        assert length_trim.trim_to_band(text, channel="linkedin", cta=BHU_CTA) == (text, [])

    def test_unreachable_band_leaves_the_text_unchanged(self):
        text = _bhutan_blog(sections=26, paras=1)  # one paragraph per section: nothing is droppable
        assert length_trim.measure(text) > qg._BLOG_MAX_CHARS
        assert length_trim.trim_to_band(text, channel="blog", cta=BHU_CTA) == (text, [])

    def test_a_paragraph_before_a_pronoun_opener_stays(self):
        p1, p2 = 1000 + 50 * 7 + 1, 1000 + 50 * 7 + 2
        it_para = "It " + _para(p2, True)[0].lower() + _para(p2, True)[1:]
        text = _bhutan_blog().replace(_para(p1, True), _para(p1, False)).replace(_para(p2, False), it_para)
        out, _ = length_trim.trim_to_band(text, channel="blog", cta=BHU_CTA)
        assert length_trim.measure(out) < length_trim.measure(text)
        assert _para(p1, False) in out  # dropping it would hand 'It …' a new referent

    def test_the_only_paragraph_naming_a_moment_stays(self):
        seed = "Punakha Dzong — visit\nDay 5 of the trip\nSource itinerary: Visit Punakha Dzong."
        moments = trip_facts.parse_moments(seed)
        text = _bhutan_blog()
        target = _para(1000 + 50 * 7 + 2, tagged=False)
        text = text.replace(target, "Punakha Dzong " + target[0].lower() + target[1:])
        out, _ = length_trim.trim_to_band(text, channel="blog", cta=BHU_CTA, moments=moments)
        assert "Punakha Dzong" in out

    def test_the_family_access_caveat_paragraph_stays(self):
        """S248 review: the trim runs after add_family_caveats; dropping the paragraph that says the access
        caveat would reopen a blocking F17_trip_fact ('add ONE sentence') for a family brand."""
        ctx = trip_facts.TripFactContext(seed=TSAATAN_SEED, audience=FAMILY_AUDIENCE, cta=BHU_CTA)
        first, last = _para(1000, tagged=True), _para(1002, tagged=False)
        caveat = ("Access requires a multi-hour horse or reindeer ride, often crossing rivers and boggy forest "
                  "terrain. " + last)
        text = (_bhutan_blog().replace(first, "The Tsaatan herd reindeer in the taiga [R:t5]. " + first)
                .replace(last, caveat))
        assert trip_facts.missing_family_caveat(text, ctx.moments, ctx.audience, BHU_CTA) == []
        # without the guard the caveat paragraph (untagged, in the longest section) is the first one dropped
        bare, _ = length_trim.trim_to_band(text, channel="blog", cta=BHU_CTA)
        assert caveat not in bare and trip_facts.missing_family_caveat(bare, ctx.moments, ctx.audience, BHU_CTA)
        out, log = length_trim.trim_to_band(text, channel="blog", cta=BHU_CTA, moments=ctx.moments, trip_ctx=ctx)
        assert log and length_trim.measure(out) <= length_trim.BLOG_TRIM_TARGET_CHARS
        assert caveat in out and trip_facts.missing_family_caveat(out, ctx.moments, ctx.audience, BHU_CTA) == []

    def test_the_cta_paragraph_and_lists_are_never_dropped(self):
        text = _bhutan_blog().replace("## Plan Your Private Journey Through Bhutan\n\n",
                                      "## Plan Your Private Journey Through Bhutan\n\nWhat to pack:\n\n"
                                      "- Long sleeves\n- Walking shoes\n\n")
        out, _ = length_trim.trim_to_band(text, channel="blog", cta=BHU_CTA)
        assert BHU_CTA in out and "What to pack:" in out and "- Walking shoes" in out


def test_f4_over_max_feedback_carries_the_target():
    ledger = [{"gate": "F4_extreme_length", "passed": False, "blocking": True,
               "violations": ["content is 14576 chars — far beyond any real channel example"]},
              {"gate": "F9_cta_fact", "passed": False, "blocking": True, "violations": ["claim x"]}]
    fb = service._repair_feedback(ledger, "blog")
    assert "under 12,000 characters" in fb[0] and "keep every heading, the CTA and the FAQ" in fb[0]
    assert fb[1] == "F9_cta_fact: claim x"
    short = [{"gate": "F4_extreme_length", "passed": False, "blocking": True,
              "violations": ["content is only 3000 chars — effectively empty"]}]
    assert "under 12,000" not in service._repair_feedback(short, "blog")[0]


def test_f4_over_max_feedback_on_another_channel_has_no_blog_target():
    """S248 review: the 12,000 target is the blog band's; a LinkedIn post over its 6,000 max must not be told
    it may keep 12,000 characters."""
    ledger = [{"gate": "F4_extreme_length", "passed": False, "blocking": True,
               "violations": ["content is 7100 chars — far beyond any real channel example"]}]
    for channel in ("linkedin", "facebook", None):
        fb = service._repair_feedback(ledger, channel)
        assert fb == ["F4_extreme_length: content is 7100 chars — far beyond any real channel example"]


@pytest.mark.asyncio
async def test_over_long_blog_ships_at_attempt_one_without_a_rewrite():
    """32789658's shape: a 15k-char blog is trimmed before its gates, so F4 passes and no rewrite runs."""
    def gates(**kw):
        return _outcome([qg.gate_extreme_length(qg.strip_citation_tags(kw["content_text"]), "blog")])
    kw, rep, rqg, rw = await _run(_bhutan_blog(), "Bhutan seed — visit\nSource itinerary: Visit.", BHU_CTA, gates)
    rw.assert_not_called()
    rep.assert_not_called()
    assert rqg.call_count == 1 and kw["status"] == "approved" and kw["attempt_number"] == 1
    assert length_trim.measure(kw["content_text"]) <= length_trim.BLOG_TRIM_TARGET_CHARS
    autofix = next(f for f in kw["flags"] if f["gate"] == "F13_autofix")
    assert any(v.startswith("trimmed ") for v in autofix["violations"])
