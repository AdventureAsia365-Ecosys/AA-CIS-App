"""AA-768 — attempt 2 repairs only the flagged sentences when the failures are quoted invented
facts, so the rewrite cannot add new ones (S232 re-runs swung 1–3/5 on full rewrites)."""
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from services.acp_content_writing import sentence_repair as sr
from services.acp_content_writing import service

PIECE = ("The trail to Tiger's Nest starts manageable — a dirt track through pine forest. [R:a1]\n\n"
         "Tiger's Nest is among the most sacred sites in Vajrayana Buddhism. "
         "The hike is steep and worth it. Book with us.")
F9 = {"gate": "F9_cta_fact", "passed": False, "blocking": True, "repairable": True, "violations": [
    'audit manual_check: FACT_CHECK_MANUAL_CHECK — unsupported claim(s): "The trail to Tiger\'s Nest starts '
    'manageable — a dirt track through pine forest." (claims: the start is a pine forest track; source: no '
    'pine forest); "Tiger\'s Nest is among the most sacred sites in Vajrayana Buddhism." (claims: most sacred; '
    'source: not stated)']}
F1 = {"gate": "F1_grounding", "passed": False, "blocking": True, "repairable": True, "violations": [
    "sentence states ['900'] not present in the content seed: "
    "'The monastery clings to a cliff face above a 900-metre'"]}


def test_locate_tolerates_citation_tags_and_extends_to_sentence_end():
    text = "Intro. The monastery clings to a cliff [R:a1] face above a 900-metre drop. [R:a1] Next."
    start, end = sr.locate_sentence(text, "The monastery clings to a cliff face above a 900-metre")
    assert text[start:end] == "The monastery clings to a cliff [R:a1] face above a 900-metre drop. [R:a1]"


def test_plan_reads_f9_claims_and_f1_quotes():
    items = sr.plan(PIECE, [F9])
    assert [i["sentence"] for i in items] == [
        "The trail to Tiger's Nest starts manageable — a dirt track through pine forest.",
        "Tiger's Nest is among the most sacred sites in Vajrayana Buddhism."]
    assert "pine forest" in items[0]["problem"]
    text = "Hi. The monastery clings to a cliff face above a 900-metre drop. Bye now."
    assert "states ['900']" in sr.plan(text, [F1])[0]["problem"]


@pytest.mark.parametrize("other", [
    {"gate": "F4_seo_surface", "passed": False, "blocking": True, "violations": ["meta too long"]},
    {"gate": "F9_cta_fact", "passed": False, "blocking": True, "violations": ["audit flagged: CTA_NOT_REFLECTED"]},
])
def test_other_failures_need_the_full_rewrite(other):
    assert sr.plan(PIECE, [F9, other]) is None


def test_unlocatable_quote_means_full_rewrite():
    bad = dict(F9, violations=['audit manual_check: FACT_CHECK_MANUAL_CHECK — unsupported claim(s): '
                               '"A sentence that is not in the piece at all." (claims: x; source: y)'])
    assert sr.plan(PIECE, [bad]) is None


def test_non_blocking_failures_do_not_block_the_plan():
    warn = {"gate": "F8_framework", "passed": False, "blocking": False, "violations": ["hook"]}
    assert sr.plan(PIECE, [F9, warn]) is not None


def test_splice_replaces_keeps_tags_and_deletes():
    items = sr.plan(PIECE, [F9])
    out = sr.splice(PIECE, items, ["The trail to Tiger's Nest is a steep dirt track.", ""])
    assert out == ("The trail to Tiger's Nest is a steep dirt track. [R:a1]\n\n"
                   "The hike is steep and worth it. Book with us.")


def _ctx():
    return {"atom_text": "seed", "goal": {"key": "promotion"}, "channel_style": {"channel": "linkedin"},
            "brand_audience": {}, "chosen": {}, "cta": "Book", "destination": None, "trip_name": None,
            "brand_rubric_text": "", "channel": "linkedin", "atom_id": "a1"}


def _outcome(ledger):
    first = next((g for g in ledger if not g["passed"] and g.get("blocking", True)), None)
    return {"passed": first is None, "gate_ledger": ledger, "first_failure": first, "flags": []}


@pytest.mark.asyncio
async def test_attempt_two_uses_sentence_repair_not_rewrite():
    row = {"piece_id": str(uuid4()), "attempt_number": 2, "status": "approved"}
    with patch.object(service, "write_content", return_value=(PIECE, 0.01, {}, None)), \
         patch.object(service, "repair_sentences", return_value=(["Fixed one.", "Fixed two."], 0.002)) as rep, \
         patch.object(service, "rewrite_with_feedback") as rw, \
         patch.object(service, "run_quality_gates", side_effect=[_outcome([F9]), _outcome([])]) as gates, \
         patch.object(service, "_finalize_piece", new=AsyncMock(return_value=row)) as fin:
        await service.run_write_background(uuid4(), uuid4(), _ctx(), pool=MagicMock())
    rw.assert_not_called()
    assert len(rep.call_args.kwargs["sentences"]) == 2
    checked = gates.call_args_list[1].kwargs["content_text"]
    assert checked.startswith("Fixed one. [R:a1]") and "The hike is steep and worth it." in checked
    assert fin.call_args.kwargs["repair_log"][0]["repair_mode"] == "sentences"


@pytest.mark.asyncio
async def test_bad_repair_reply_falls_back_to_full_rewrite():
    row = {"piece_id": str(uuid4()), "attempt_number": 2, "status": "approved"}
    with patch.object(service, "write_content", return_value=(PIECE, 0.01, {}, None)), \
         patch.object(service, "repair_sentences", side_effect=service.SentenceRepairError("x")), \
         patch.object(service, "rewrite_with_feedback", return_value=("rewritten", 0.01, {}, None)) as rw, \
         patch.object(service, "run_quality_gates", side_effect=[_outcome([F9]), _outcome([])]), \
         patch.object(service, "_finalize_piece", new=AsyncMock(return_value=row)):
        await service.run_write_background(uuid4(), uuid4(), _ctx(), pool=MagicMock())
    rw.assert_called_once()


def _llm(content):
    fake = MagicMock()
    fake.generate.return_value = MagicMock(content=content, model_used="sonnet", cost_usd=0.002,
                                           input_tokens=10, output_tokens=10, stop_reason="end_turn",
                                           satellite_account="acc3", fallback_used=False)
    return fake


def test_repair_sentences_parses_and_validates_count():
    from services.acp_content_writing import generate
    items = [{"sentence": "A.", "problem": "p"}, {"sentence": "B.", "problem": "q"}]
    with patch.object(generate, "LLMClient", return_value=_llm('{"replacements": ["x", ""]}')) as m, \
         patch.object(generate, "record_call_sync"):
        reps, cost = generate.repair_sentences(sentences=items, content_seed="seed", channel="linkedin")
    assert reps == ["x", ""] and cost == 0.002
    assert m.return_value.generate.call_args.args[0].stage == "t9_write"
    with patch.object(generate, "LLMClient", return_value=_llm('{"replacements": ["only one"]}')), \
         patch.object(generate, "record_call_sync"), pytest.raises(generate.SentenceRepairError):
        generate.repair_sentences(sentences=items, content_seed="seed", channel="linkedin")
