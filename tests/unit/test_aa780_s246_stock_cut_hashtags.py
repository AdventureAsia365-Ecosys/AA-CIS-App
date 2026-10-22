"""AA-780 (S246 panel) — two deterministic fixes, no new gate:
1. a STOCK_PATTERNS sentence left in the final attempt's text is cut in code (F15_voice blocks on attempt 1
   only; t9l-010 shipped "That's the detail worth paying attention to …" after attempt 2);
2. hygiene._apply_hashtag_layout removes repeated hashtags (t9l-002 / t9l-014 shipped
   "#Bhutan #PunakhaDzong #PunakhaDzong").
Fixtures are the real S246 posts (run dab254c4)."""
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.acp_content_writing import hygiene, quality_gates as qg
from services.acp_content_writing import service as writing_service
from services.acp_shared.channel_spec import STOCK_PATTERNS, linkedin_spec, post_words

TRAIL = {"brand_name": "Trail Pulse", "brand_type": "Young active adventure travel brand",
         "customer_segment": "25–40 years old\nSolo travellers, couples, small groups of friends",
         "voice": ["Energetic", "Fresh", "Direct", "Friendly", "Modern", "Confident", "Lightly playful"]}
SPEC = linkedin_spec(TRAIL)
CTA_010 = "Get the full Trail Pulse itinerary and dates"
STOCK_010 = ("That's the detail worth paying attention to as a traveller deciding whether a brand knows what "
             "it's doing.")

# S246 t9l-010 (Trail Pulse, approved after attempt 2 — the one exact STOCK_PATTERNS hit of the 15)
T9L_010 = ("Active travel plans that actually account for recovery don't just happen by chance.\n\n"
           "On Day 2, a short trek leads to Kuang Si Falls, where travertine limestone terraces step down into a "
           "series of turquoise pools. The vivid colour comes from calcium carbonate in the surrounding limestone, "
           "and the lower pools are open for swimming. The plan keeps you there to swim and rest before lunch.\n\n"
           f"{STOCK_010} Rest before lunch, not after exhaustion sets in. Time at the pools is written into the "
           "day, not left as a maybe-if-you-have-energy afterthought.\n\n"
           "Trail Pulse builds trips around movement and energy delivered well, and that means knowing when to "
           "stop as clearly as knowing when to go.\n\n"
           "Get the full Trail Pulse itinerary and dates to see how the rest of the trip is structured.\n\n"
           "#LuangPrabang #KuangSiFalls #Laos")

# S246 t9l-002 / t9l-014 (WildKind) tails
TAIL_DUP = "#Bhutan #PunakhaDzong #PunakhaDzong"
T9L_002 = ("Zhabdrung Ngawang Namgyal founded Bhutan's administrative and religious centre at the confluence of "
           "the Pho Chhu and Mo Chhu rivers.\n\n"
           "Visitors wear traditional Bhutanese dress or formal attire as a condition of access.\n\n"
           "If that kind of travel interests you, ask WildKind Travel for the full itinerary.\n\n" + TAIL_DUP)
T9L_014 = ("In 1637, Zhabdrung Ngawang Namgyal built Punakha Dzong on a peninsula between two rivers.\n\n"
           "If geography and meaning matter to how you travel, ask WildKind Travel for the full itinerary.\n\n"
           + TAIL_DUP + "\n")


def _f15(text, *, blocking=False):
    return qg.gate_voice(text, spec=SPEC, stock_blocking=blocking)


def _stock_hits(text):
    return [m.group(0) for p in STOCK_PATTERNS for m in p.finditer(text)]


# ---------------------------------------------------------------- 1. stock-line cut

class TestStockLineItems:
    def test_finds_the_t9l_010_sentence(self):
        items = hygiene.stock_line_items(T9L_010)
        assert [it["sentence"] for it in items] == [STOCK_010]
        assert items[0]["phrase"] == "worth paying attention to"
        assert T9L_010[items[0]["start"]:items[0]["end"]] == STOCK_010

    def test_a_two_sentence_match_is_one_item(self):
        text = "Hook here today.\n\nNot as filler. Not as an afterthought. Pools at noon."
        items = hygiene.stock_line_items(text)
        assert len(items) == 1 and items[0]["sentence"] == "Not as filler. Not as an afterthought."

    def test_hashtag_line_and_clean_text_give_nothing(self):
        assert hygiene.stock_line_items("Pools at noon.\n\n#WorthPausingOn") == []
        assert hygiene.stock_line_items(T9L_010.replace(STOCK_010 + " ", "")) == []


class TestCutStockLines:
    def _cut(self, text, ledger=None, flags=None):
        ledger = ledger if ledger is not None else [_f15(text), qg.gate_social_shape(text, SPEC)]
        flags = flags if flags is not None else [g for g in ledger if not g["passed"] and not g["blocking"]]
        return writing_service._cut_stock_lines(text, ledger, flags, channel="linkedin", spec=SPEC, cta=CTA_010,
                                                ignore=hygiene.brand_words("Trail Pulse"))

    def test_t9l_010_ships_without_the_stock_line(self):
        assert _stock_hits(T9L_010) == ["worth paying attention to"]
        f15 = _f15(T9L_010)
        assert not f15["passed"] and not f15["blocking"]  # attempt ≥ 2: a warn, it shipped
        text, ledger, flags, entry = self._cut(T9L_010)
        assert _stock_hits(text) == []
        assert STOCK_010 not in text
        assert "Rest before lunch, not after exhaustion sets in." in text  # the neighbours stay
        assert text.startswith("Active travel plans") and CTA_010 in text
        assert text.rstrip().endswith("#LuangPrabang #KuangSiFalls #Laos")
        assert post_words(qg.strip_citation_tags(text)) >= 120
        assert entry["repair_mode"] == "stock_cut" and entry["sentences_cut"] == 1
        assert entry["cut_sentences"] == [STOCK_010]
        new_f15 = next(g for g in ledger if g["gate"] == "F15_voice")
        assert new_f15["passed"] and not new_f15["blocking"] and new_f15["violations"] == []
        assert not any(f["gate"] == "F15_voice" for f in flags)
        note = next(f for f in flags if f["gate"] == "F15_stock_cut")
        assert note["blocking"] is False and "worth paying attention to" in note["violations"][0]

    def test_other_f15_findings_are_kept(self):
        other = "stock line 'x' — say it plainly or cut it: 'Rest before lunch, not after exhaustion sets in.'"
        f15 = _f15(T9L_010)
        f15 = {**f15, "violations": f15["violations"] + [other]}
        _, ledger, flags, _ = self._cut(T9L_010, ledger=[f15], flags=[f15])
        new_f15 = next(g for g in ledger if g["gate"] == "F15_voice")
        assert new_f15["violations"] == [other] and not new_f15["passed"]
        assert [f["violations"] for f in flags if f["gate"] == "F15_voice"] == [[other]]

    def test_the_hook_and_the_cta_are_never_cut(self):
        hook = "Here's the thing about Kuang Si Falls on Day 2 of this trip."
        text = hook + T9L_010[T9L_010.index("\n\n"):].replace(STOCK_010 + " ", "")
        text, _, _, entry = self._cut(text)
        assert text.startswith(hook)
        assert entry["repair_mode"] == "stock_cut_refused" and entry["sentences_cut"] == 0

    def test_a_cut_that_leaves_the_post_under_the_floor_is_refused(self):
        short = ("Kuang Si Falls is on Day 2.\n\nThe pools are open for swimming. " + STOCK_010
                 + "\n\nGet the full Trail Pulse itinerary and dates.")
        text, ledger, _, entry = self._cut(short)
        assert text == short and entry["repair_mode"] == "stock_cut_refused"
        assert not next(g for g in ledger if g["gate"] == "F15_voice")["passed"]

    def test_no_spec_or_no_hit_is_a_no_op(self):
        assert writing_service._cut_stock_lines(T9L_010, [], [], channel="blog", spec=None)[3] is None
        clean = T9L_010.replace(STOCK_010 + " ", "")
        assert self._cut(clean)[0] == clean and self._cut(clean)[3] is None


@pytest.mark.asyncio
async def test_t9_loop_cuts_the_stock_line_left_after_attempt_2():
    """Attempt 1 blocks on F15 (stock_blocking); the rewrite still has the stock line (F15 only warns now)
    and ships — the post-loop cut removes it, the piece stays approved, the repair_log says so."""
    ctx = {"atom_text": "seed", "goal": {"key": "promotion", "name": "P", "description": "d", "logic": "l"},
           "channel_style": {"channel": "linkedin"}, "brand_audience": TRAIL, "chosen": {"name": "a"},
           "cta": CTA_010, "destination": None, "trip_name": None, "brand_rubric_text": "r",
           "channel": "linkedin", "atom_id": "a1", "tenant_id": None}

    def fake_gates(*, content_text, stock_blocking=True, **_):
        ledger = [_f15(content_text, blocking=stock_blocking), qg.gate_social_shape(content_text, SPEC)]
        first = next((g for g in ledger if not g["passed"] and g["blocking"]), None)
        return {"passed": first is None, "gate_ledger": ledger, "first_failure": first,
                "flags": [g for g in ledger if not g["passed"] and not g["blocking"]]}

    with patch.object(writing_service, "write_content", return_value=(T9L_010, 0.01, {}, None)), \
            patch.object(writing_service, "repair_sentences", return_value=([STOCK_010], 0.001)), \
            patch.object(writing_service, "rewrite_with_feedback", return_value=(T9L_010, 0.01, {}, None)), \
            patch.object(writing_service, "run_quality_gates", new=MagicMock(side_effect=fake_gates)), \
            patch.object(writing_service, "_finalize_piece", new=AsyncMock()) as fin:
        await writing_service.run_write_background(uuid.uuid4(), uuid.uuid4(), ctx, pool=MagicMock())
    kw = fin.await_args.kwargs
    assert kw["status"] == "approved"
    assert _stock_hits(kw["content_text"]) == []
    assert any(e.get("repair_mode") == "stock_cut" for e in kw["repair_log"])
    assert any(f["gate"] == "F15_stock_cut" for f in kw["flags"])


# ---------------------------------------------------------------- 2. repeated hashtags

class TestHashtagDedup:
    @pytest.mark.parametrize("text", [T9L_002, T9L_014])
    def test_s246_tails_lose_the_repeat(self, text):
        out, log = hygiene._apply_hashtag_layout(text, SPEC.hashtags_max)
        assert out.rstrip("\n").split("\n")[-1] == "#Bhutan #PunakhaDzong"
        assert out.count("#PunakhaDzong") == 1
        assert log == ["removed 1 repeated hashtag(s)"]
        assert out.endswith("\n") == text.endswith("\n")
        assert out.split("\n\n#")[0] == text.split("\n\n#")[0]  # the body is untouched

    def test_case_insensitive_first_spelling_kept(self):
        out, _ = hygiene._apply_hashtag_layout("Body line.\n\n#Laos #KuangSi #kuangsi #LAOS", 5)
        assert out == "Body line.\n\n#Laos #KuangSi"

    def test_clean_last_tag_line_is_returned_as_written(self):
        text = "Body line.\n\n#Bhutan #PunakhaDzong"
        assert hygiene._apply_hashtag_layout(text, 5) == (text, [])

    def test_autofix_removes_the_repeat(self):
        ctx = hygiene.HygieneContext(seed="Punakha Dzong — visit courtyards and assembly hall",
                                     brand_name="WildKind Travel", hashtags_max=SPEC.hashtags_max)
        out, log = hygiene.autofix(T9L_002, ctx)
        assert out.rstrip().endswith("#Bhutan #PunakhaDzong") and out.count("#PunakhaDzong") == 1
        assert "removed 1 repeated hashtag(s)" in log


# ---------------------------------------------------------------- rerun cost stage (scripts/aa729)

def test_harness_stage_can_be_set_per_issue():
    """The 15-post rerun logs its spend under adhoc_aa780 (AA729_STAGE); a bad value is refused. A fresh
    interpreter each time: reloading scripts.aa729.common in-process would replace its classes for other tests."""
    import os
    import subprocess
    import sys

    def stage(value):
        env = {k: v for k, v in os.environ.items() if k != "AA729_STAGE"}
        if value is not None:
            env["AA729_STAGE"] = value
        return subprocess.run([sys.executable, "-c", "from scripts.aa729 import common; print(common.STAGE)"],
                              env=env, capture_output=True, text=True, timeout=60)

    assert stage("adhoc_aa780").stdout.strip() == "adhoc_aa780"
    assert stage(None).stdout.strip() == "adhoc_aa729"
    bad = stage("adhoc_x'; drop")
    assert bad.returncode != 0 and "AA729_STAGE must be adhoc_<issue>" in bad.stderr
