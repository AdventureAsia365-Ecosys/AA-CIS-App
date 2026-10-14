"""AA-786 — F9_cta_fact v1.1: the judge sees the piece without citation tags, broken sentences are
quoted (TEXT_INTEGRITY) and repairable, a reason-less verdict is a warn (not a hold), the blog's
search title / meta are judged (and never hold on their own), and on LinkedIn an F9 product-truth
failure is cut and the piece ships instead of being held."""
import json
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from services.acp_content_writing import quality_gates as qg
from services.acp_content_writing import sentence_repair, service

PIECE = ("The dzong stands where two rivers meet in Punakha. It was built in 1637 by the Zhabdrung. "
         "Every guest gets a private chef at dinner each night. Ask us about the dates.")
PASS = {"status": "pass", "brand_fit": "1", "cta_clear": "1", "human_read": "1",
        "failure_codes": [], "flagged_phrases": [], "notes": ""}


def _judge(**fields) -> dict:
    return {"text": json.dumps({**PASS, **fields})}


def _run_f9(data: dict, text: str = PIECE, **kw):
    with patch.object(qg, "invoke_judge", return_value=_judge(**data)) as mock_judge, \
         patch.object(qg, "_log_t10_judge_call") as mock_log:
        results = qg.gate_brand_voice(text, "Ask us about the dates.", "rubric", source_text="seed", **kw)
    by_gate = {r["gate"]: r for r in results}
    return by_gate["F9_cta_fact"], mock_judge.call_args[0][1], mock_log


# ---------------------------------------------------------------- 1. tag strip

class TestTagStrip:
    def test_f9_prompt_has_no_citation_tags(self):
        tagged = ("The dzong stands where two rivers meet in Punakha. [R:atom_1] It was built in 1637 "
                  "by the Zhabdrung.[F:fact_9] Ask us about the dates.")
        captured = {}

        def fake_judge(system, user, **_):
            captured["user"] = user
            return _judge()
        with patch.object(qg, "invoke_judge", side_effect=fake_judge), \
             patch.object(qg, "_log_t10_judge_call"), \
             patch.object(qg, "gate_framework", return_value=qg._result("F8_framework", [])):
            qg.run_quality_gates(content_text=tagged, atom_text="SEED [R:keep] TEXT", cta="Ask us about the dates.",
                                 goal_key="promotion", brand_rubric_text="r", channel="blog")
        piece_block = captured["user"].split("SOURCE (everything", 1)[0]
        assert "[R:" not in piece_block and "[F:" not in piece_block
        assert "It was built in 1637 by the Zhabdrung. Ask us" in piece_block
        # the SOURCE block is what the writer got, unchanged
        assert "SOURCE (everything the writer was given as fact):\nSEED [R:keep] TEXT" in captured["user"]

    def test_gate_strips_tags_itself_and_logs_the_version(self):
        _, prompt, log = _run_f9({}, text="Punakha Dzong sits at the confluence. [R:a1] Ask us about the dates.")
        assert "[R:" not in prompt
        extra = log.call_args.kwargs["extra"]
        assert extra["prompt_version"] == qg.F9_PROMPT_VERSION == "f9_v1.1"
        assert extra["piece_tag_free"] is True

    def test_grounding_still_sees_the_tags(self):
        """F1/F5 read the tags — only the F9 call gets the stripped text."""
        tagged = "It was built in 1637 by the Zhabdrung. [R:atom_1] Ask us about the dates."
        with patch.object(qg, "gate_grounding", wraps=qg.gate_grounding) as mock_f1, \
             patch.object(qg, "gate_brand_voice", return_value=[qg._result("F9_cta_fact", []),
                                                                 qg._result("F9_brand_style", [], blocking=False)]), \
             patch.object(qg, "gate_framework", return_value=qg._result("F8_framework", [])):
            qg.run_quality_gates(content_text=tagged, atom_text="1637", cta="Ask us", goal_key="promotion",
                                 brand_rubric_text="r", channel="linkedin")
        assert "[R:atom_1]" in mock_f1.call_args[0][0]


# ---------------------------------------------------------------- 2. TEXT_INTEGRITY + reason-less verdicts

class TestTextIntegrity:
    def test_text_integrity_code_is_quoted_and_repairable(self):
        f9, prompt, _ = _run_f9({"status": "flagged", "failure_codes": ["TEXT_INTEGRITY"],
                                 "broken_sentences": [{"sentence": "It was built in 1637 by the Zhabdrung.",
                                                       "problem": "fine"},
                                                      {"sentence": "A sentence that is not in the piece at all.",
                                                       "problem": "x"}]})
        assert "TEXT_INTEGRITY" in prompt and "broken_sentences" in prompt
        assert f9["violations"] == ["TEXT_INTEGRITY: 'It was built in 1637 by the Zhabdrung.'"]
        assert f9["blocking"] is True and f9["repairable"] is True
        items = sentence_repair.plan(PIECE, [f9])
        assert items is not None and len(items) == 1
        assert items[0]["sentence"] == "It was built in 1637 by the Zhabdrung."
        assert "broken or garbled" in items[0]["problem"]

    def test_text_integrity_quote_from_the_middle_is_widened(self):
        f9, _, _ = _run_f9({"status": "flagged", "failure_codes": ["TEXT_INTEGRITY"],
                            "broken_sentences": [{"sentence": "private chef at dinner each night",
                                                  "problem": "doubled"}]})
        assert f9["violations"] == ["TEXT_INTEGRITY: 'Every guest gets a private chef at dinner each night.'"]

    def test_notes_only_with_locatable_quote_becomes_text_integrity(self):
        f9, _, _ = _run_f9({"status": "manual_check",
                            "notes": "The sentence 'Every guest gets a private chef at dinner each night' "
                                     "has a grammar error."})
        assert f9["violations"] == ["TEXT_INTEGRITY: 'Every guest gets a private chef at dinner each night.'"]
        assert f9["blocking"] is True
        assert sentence_repair.plan(PIECE, [f9])[0]["sentence"].startswith("Every guest gets")

    def test_notes_only_without_quote_is_warn_not_hold(self):
        f9, _, log = _run_f9({"status": "manual_check", "notes": "Contains visible citation markers."})
        assert f9["passed"] is False and f9["blocking"] is False and f9["repairable"] is False
        assert f9["violations"][0].startswith(qg.F9_NO_REASON)
        assert qg.hold_failure([f9]) is None
        assert log.call_args.kwargs["extra"]["reasonless"] is True
        # the warn still reaches `flags` through run_quality_gates
        style = qg._result("F9_brand_style", [], blocking=False)
        with patch.object(qg, "gate_brand_voice", return_value=[f9, style]), \
             patch.object(qg, "gate_framework", return_value=qg._result("F8_framework", [])):
            result = qg.run_quality_gates(content_text="Ask us about the dates today.", atom_text="seed", cta="Ask us",
                         goal_key="promotion", brand_rubric_text="r", channel="facebook")
        assert any(g["gate"] == "F9_cta_fact" for g in result["flags"])
        assert result["first_failure"] is None or result["first_failure"]["gate"] != "F9_cta_fact"

    def test_unquoted_text_integrity_is_reasonless(self):
        f9, _, _ = _run_f9({"status": "flagged", "failure_codes": ["TEXT_INTEGRITY"], "broken_sentences": []})
        assert f9["blocking"] is False and "TEXT_INTEGRITY" in f9["violations"][0]

    def test_cta_not_reflected_unchanged(self):
        f9, _, _ = _run_f9({"status": "flagged", "failure_codes": ["CTA_NOT_REFLECTED"]})
        assert f9["violations"] == ["audit flagged: CTA_NOT_REFLECTED"]
        assert f9["blocking"] is True
        assert sentence_repair.plan(PIECE, [f9]) is None  # full rewrite, as before

    def test_fact_check_claim_still_repairable(self):
        f9, _, _ = _run_f9({"status": "manual_check", "failure_codes": ["FACT_CHECK_MANUAL_CHECK"],
                            "unsupported_claims": [{"sentence": "Every guest gets a private chef at dinner each night.",
                                                    "claim": "private chef", "why_not_in_source": "no meals"}]})
        assert f9["blocking"] is True and "unsupported claim(s)" in f9["violations"][0]
        assert sentence_repair.plan(PIECE, [f9])[0]["problem"].startswith("claims private chef")


# ---------------------------------------------------------------- 3. title / meta

class TestTitleMeta:
    TITLE = "Punakha Dzong: Where Two Rivers Meet in Bhutan's Old Capital"
    META = "Visit Punakha Dzong, where the Mo Chhu and Pho Chhu meet, with a private chef cooking every night."

    def test_f9_prompt_includes_title_and_meta(self):
        _, prompt, _ = _run_f9({}, seo_title=self.TITLE, meta_description=self.META)
        assert f"Title: {self.TITLE}" in prompt and f"Meta description: {self.META}" in prompt
        _, plain, _ = _run_f9({})
        assert "SEARCH TITLE AND META DESCRIPTION" not in plain

    def test_title_meta_only_claim_is_non_blocking_and_seo_owned(self):
        f9, _, _ = _run_f9({"status": "manual_check", "failure_codes": ["FACT_CHECK_MANUAL_CHECK"],
                            "unsupported_claims": [{"sentence": self.META, "claim": "a private chef",
                                                    "why_not_in_source": "no meals in the source"}]},
                           seo_title=self.TITLE, meta_description=self.META)
        assert len(f9["violations"]) == 1 and f9["violations"][0].startswith("meta unsupported claim:")
        assert f9["blocking"] is False
        assert qg.hold_failure([f9]) is None
        assert sentence_repair._seo_fields_only(f9)
        assert sentence_repair.plan(PIECE, [{**f9, "blocking": True}]) == []  # never a full rewrite

    def test_body_claim_plus_meta_claim_blocks_and_repairs_only_the_body(self):
        f9, _, _ = _run_f9({"status": "manual_check", "failure_codes": ["FACT_CHECK_MANUAL_CHECK"],
                            "unsupported_claims": [
                                {"sentence": "Every guest gets a private chef at dinner each night.",
                                 "claim": "private chef", "why_not_in_source": "no meals"},
                                {"sentence": self.TITLE, "claim": "old capital", "why_not_in_source": "silent"}]},
                           seo_title=self.TITLE, meta_description=self.META)
        assert f9["blocking"] is True
        assert any(v.startswith("title unsupported claim:") for v in f9["violations"])
        items = sentence_repair.plan(PIECE, [f9])
        assert [i["sentence"] for i in items] == ["Every guest gets a private chef at dinner each night."]

    def test_run_quality_gates_passes_title_meta_for_blog_only(self):
        res = [qg._result("F9_cta_fact", []), qg._result("F9_brand_style", [], blocking=False)]
        for channel, expect in (("blog", self.TITLE), ("linkedin", None)):
            with patch.object(qg, "gate_brand_voice", return_value=res) as mock_bv, \
                 patch.object(qg, "gate_framework", return_value=qg._result("F8_framework", [])):
                qg.run_quality_gates(content_text="A short piece.", atom_text="seed", cta="Ask us",
                                     goal_key="promotion", brand_rubric_text="r", channel=channel,
                                     seo_title=self.TITLE, meta_description=self.META)
            assert mock_bv.call_args.kwargs.get("seo_title") == expect


# ---------------------------------------------------------------- 4. LinkedIn repair-not-hold

CLAIM_V = ('audit manual_check: FACT_CHECK_MANUAL_CHECK — unsupported claim(s): '
           '"Every guest gets a private chef at dinner each night." (claims: private chef; source: no meals)')


def _f9_fail(violations=None):
    return {"gate": "F9_cta_fact", "passed": False, "blocking": True, "repairable": True,
            "violations": violations or [CLAIM_V]}


class TestCutTruthItems:
    def test_linkedin_truth_failure_is_cut_and_waived(self):
        ledger = [{"gate": "F6_cta_present", "passed": True, "violations": [], "blocking": True}, _f9_fail()]
        text, new_ledger, flags, entry = service._cut_truth_items(PIECE, ledger, [], channel="linkedin",
                                                                  cta="Ask us about the dates.")
        assert "private chef" not in text and "Ask us about the dates." in text
        f9 = next(g for g in new_ledger if g["gate"] == "F9_cta_fact")
        assert f9["blocking"] is False and f9["waived"] is True
        assert entry["repair_mode"] == "f9_truth_cut" and entry["sentences_cut"] == 1
        assert service._hold_or_ship(new_ledger, flags)[0] == "approved"
        assert any(g["gate"] == "F9_cta_fact" for g in flags)

    def test_linkedin_cut_impossible_ships_flagged(self):
        hook = ('audit manual_check: FACT_CHECK_MANUAL_CHECK — unsupported claim(s): '
                '"The dzong stands where two rivers meet in Punakha." (claims: rivers; source: silent)')
        text, ledger, flags, entry = service._cut_truth_items(PIECE, [_f9_fail([hook])], [], channel="linkedin",
                                                              cta="Ask us about the dates.")
        assert text == PIECE  # the hook is never cut
        assert entry["repair_mode"] == "f9_waived"
        status, reason, flags = service._hold_or_ship(ledger, flags)
        assert (status, reason) == ("approved", None)
        assert any(g["gate"] == "F9_cta_fact" and g["waived"] for g in flags)

    def test_text_integrity_on_linkedin_is_cut(self):
        v = "TEXT_INTEGRITY: 'It was built in 1637 by the Zhabdrung.'"
        text, ledger, _, _ = service._cut_truth_items(PIECE, [_f9_fail([v])], [], channel="linkedin",
                                                      cta="Ask us about the dates.")
        assert "1637" not in text and ledger[0]["waived"] is True

    def test_cta_failure_on_linkedin_still_holds(self):
        ledger = [_f9_fail(["audit flagged: CTA_NOT_REFLECTED"])]
        text, new_ledger, _, entry = service._cut_truth_items(PIECE, ledger, [], channel="linkedin")
        assert entry is None and text == PIECE
        assert service._hold_or_ship(new_ledger, [])[0] == "held"

    @pytest.mark.parametrize("channel", ["blog", "facebook"])
    def test_other_channels_unchanged(self, channel):
        ledger = [_f9_fail()]
        text, new_ledger, _, entry = service._cut_truth_items(PIECE, ledger, [], channel=channel)
        assert entry is None and text == PIECE and new_ledger is ledger
        assert service._hold_or_ship(new_ledger, [])[0] == "held"


def _context(channel):
    return {"atom_text": "The dzong stands where two rivers meet in Punakha. It was built in 1637 by the Zhabdrung.",
            "goal": {"key": "promotion", "name": "P", "description": "d", "logic": "AIDA"},
            "channel_style": {"key": channel}, "brand_audience": {}, "chosen": {"name": "A"},
            "cta": "Ask us about the dates.", "destination": None, "trip_name": None,
            "brand_rubric_text": "rubric", "channel": channel, "atom_id": "atom_1"}


def _failing_gates(**kw):
    f9 = _f9_fail()
    return {"passed": False, "gate_ledger": [f9], "first_failure": f9, "flags": []}


@pytest.mark.asyncio
@pytest.mark.parametrize("channel,expect", [("linkedin", "approved"), ("facebook", "held")])
async def test_service_linkedin_f9_is_never_held(channel, expect):
    """Every attempt fails F9 on one claim and the repair keeps the sentence: LinkedIn ships with the
    sentence cut and F9 waived; another channel is held as before."""
    def same_sentences(*, sentences, **_):
        return [s["sentence"] for s in sentences], 0.0
    with patch.object(service, "write_content", return_value=(PIECE, 0.01, {}, None)), \
         patch.object(service, "rewrite_with_feedback", return_value=(PIECE, 0.01, {}, None)), \
         patch.object(service, "repair_sentences", side_effect=same_sentences), \
         patch.object(service, "run_quality_gates", side_effect=_failing_gates), \
         patch.object(service, "observe_t10", new=AsyncMock()), \
         patch("services.acp_content_writing.service.hygiene.ensure_cta", side_effect=lambda t, c: (t, [])), \
         patch("services.acp_content_writing.service.text_integrity.final_pass",
               side_effect=lambda text, hyg, **kw: (text, [], [])), \
         patch.object(service, "_finalize_piece", new=AsyncMock(return_value={})) as fin:
        await service.run_write_background(uuid4(), uuid4(), _context(channel), pool=MagicMock())
    kw = fin.call_args.kwargs
    assert kw["status"] == expect
    f9 = next(g for g in kw["gate_ledger"] if g["gate"] == "F9_cta_fact")
    if expect == "approved":
        assert kw["held_reason"] is None
        assert "private chef" not in kw["content_text"]
        assert f9["blocking"] is False and f9["waived"] is True
        assert any(r.get("repair_mode") == "f9_truth_cut" for r in kw["repair_log"])
    else:
        assert kw["held_reason"].startswith("F9_cta_fact")
        assert "private chef" in kw["content_text"]
