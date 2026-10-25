"""AA-452 — services/acp_content_writing/quality_gates.py's blog-only gates (F5/F3/F7 were dropped or
folded into F16 by AA-787; the F7 check lives on as restated_faq_answers) + the tag strip/leak-prevention mechanism
(strip_citation_tags/deep_strip_citation_tags) + service.write_and_check()'s mandatory
strip-before-persist step. Same conventions test_aa450_quality_gates.py already uses (pure
functions tested directly, judge gates mocked via patch.object(qg, "invoke_judge", ...))."""
import json
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.acp_content_writing import quality_gates as qg
from services.acp_content_writing import service


@pytest.fixture(autouse=True)
def _no_cta_insert():
    """S234 round 3 — run_write_background now restores the configured CTA line word for word
    (hygiene.ensure_cta). These tests pin the retry loop on placeholder texts ('draft 1') that carry
    no CTA; the CTA step has its own tests in test_aa768_s234_round3.py. S234 round 4 — the final
    text-integrity pass (one CTA, fragments) is off here too; it is covered in test_aa768_s234_round4.py."""
    with patch("services.acp_content_writing.service.hygiene.ensure_cta", side_effect=lambda text, cta: (text, [])), \
            patch("services.acp_content_writing.service.text_integrity.final_pass",
                  side_effect=lambda text, hyg, **kw: (text, [], [])):
        yield


# ---------------------------------------------------------------- restated_faq_answers (was F7, AA-787)

class TestRestatedFaqAnswers:
    def test_no_faq_section_passes_as_noop(self):
        assert qg.restated_faq_answers("A body with no FAQ section at all.") == []

    def test_faq_answer_restating_body_fails(self):
        body = (
            "The waterfall trail winds through dense highland forest reaching remote plateau "
            "villages before dawn.\n\n"
            "## FAQ\n\n"
            "**Q: What is the trail like?**\n"
            "A: The waterfall trail winds through dense highland forest reaching remote plateau "
            "villages before dawn."
        )
        assert qg.restated_faq_answers(body) == [1]
        # the fixer the pipeline runs before the gates removes it; F16 then counts what is left
        fixed, removed = qg.drop_restated_faq(body)
        assert removed == 1 and qg.restated_faq_answers(fixed) == []

    def test_faq_answer_with_new_detail_passes(self):
        body = (
            "The waterfall trail winds through dense highland forest.\n\n"
            "## FAQ\n\n"
            "**Q: How long does the visit take?**\n"
            "A: Most travelers spend around ninety minutes at the third tier."
        )
        assert qg.restated_faq_answers(body) == []


# ---------------------------------------------------------------- strip_citation_tags

class TestStripCitationTags:
    def test_strips_tag_and_leading_space(self):
        assert qg.strip_citation_tags("A fact. [R:atom_abc123] Next.") == "A fact. Next."

    def test_strips_tag_with_no_leading_space(self):
        assert qg.strip_citation_tags("A fact.[R:atom_abc123] Next.") == "A fact. Next."

    def test_strips_f_prefixed_tag_too(self):
        assert qg.strip_citation_tags("A fact. [F:fact_9] Next.") == "A fact. Next."

    def test_no_tags_is_unchanged(self):
        assert qg.strip_citation_tags("Nothing tagged here.") == "Nothing tagged here."

    def test_none_returns_empty_string(self):
        assert qg.strip_citation_tags(None) == ""

    def test_deep_strip_walks_gate_ledger_shape(self):
        ledger = [
            {"gate": "F1_grounding", "passed": False,
             "violations": ["sentence quotes [R:atom_abc123] here"], "repairable": True},
            {"gate": "F6_cta_present", "passed": True, "violations": [], "repairable": True},
        ]
        cleaned = qg.deep_strip_citation_tags(ledger)
        assert "[R:" not in cleaned[0]["violations"][0]
        assert cleaned[1] == ledger[1]  # untouched entries pass through unchanged


# ---------------------------------------------------------------- run_quality_gates channel dispatch

def _judge_raw(**fields) -> dict:
    return {"text": json.dumps(fields)}


class TestRunQualityGatesChannelDispatch:
    def test_non_blog_channel_gate_order(self):
        f9_data = {"status": "pass", "brand_fit": "1", "cta_clear": "1", "human_read": "1",
                   "failure_codes": [], "flagged_phrases": [], "notes": ""}
        with patch.object(qg, "invoke_judge", side_effect=[_judge_raw(**f9_data)]):
            outcome = qg.run_quality_gates(
                content_text="A clean piece about the trail.", atom_text="the trail",
                cta="Book now", brand_rubric_text="rubric", channel="tiktok",
            )
        gates = [g["gate"] for g in outcome["gate_ledger"]]
        # AA-514: promises_an_option now runs for every channel (origin's own channels=None).
        # AA-484: F10_cannibalization_cross_tenant runs for every channel too, right after F2 —
        # a `None` cannibalization_match (this test's default) always passes with 0 violations.
        # AA-613: F9 split into F9_cta_fact (block) + F9_brand_style (warn).
        # AA-787: promises_an_option and F8_framework dropped (23 results for a non-blog channel).
        # AA-924: F2_banned_patterns + F2_brand_forbidden → F2_lexicon, F15_voice_warn → F15_voice (21 results).
        assert gates == [
            "F6_cta_present", "F1_grounding", "F2_lexicon",
            "F2_platform_truth", "F2_superlatives", "F2_style_warn",  # AA-800 word layers, every channel
            "F11_brand_name",
            "F19_authenticity",  # AA-819 (AUP 2026-11) — every channel, right after F11
            "F10_cannibalization_cross_tenant",
            "F4_extreme_length", "F0_complete", "F12_scope", "F13_hygiene", "F13_leak",
            "F13_repeat", "F17_trip_fact", "F18_rarity",  # S234 round 3 — pass without contexts
            "F14_social_shape", "F15_voice",  # AA-771 (S234) — pass without a channel spec
            "F9_cta_fact", "F9_brand_style",
        ]

    def test_blog_channel_gate_order(self):
        f9_data = {"status": "pass", "brand_fit": "1", "cta_clear": "1", "human_read": "1",
                   "failure_codes": [], "flagged_phrases": [], "notes": ""}
        body = "## Intro\n" + ("word " * 300) + "[R:atom_abc123]"
        with patch.object(qg, "invoke_judge", side_effect=[_judge_raw(**f9_data)]):
            outcome = qg.run_quality_gates(
                content_text=body, atom_text="the trail", cta="Book now",
                brand_rubric_text="rubric", channel="blog",
                seo_title="Trail Guide", meta_description="x" * 130 + ".", slug="trail-guide",
            )
        gates = [g["gate"] for g in outcome["gate_ledger"]]
        # AA-514: + promises_an_option (after F2) and F4_seo_surface (after F4, blog-only).
        # AA-484: + F10_cannibalization_cross_tenant, right after F2 (before promises_an_option).
        # AA-613: F9 split into F9_cta_fact (block) + F9_brand_style (warn).
        # AA-787: promises_an_option, F5, F3, F8 dropped; F7 folded into F16 (25 results for a blog).
        # AA-924: F2_lexicon and one F15_voice (23 results for a blog).
        assert gates == [
            "F6_cta_present", "F1_grounding", "F2_lexicon",
            "F2_platform_truth", "F2_superlatives", "F2_style_warn",  # AA-800 word layers, every channel
            "F11_brand_name",
            "F19_authenticity",  # AA-819 (AUP 2026-11) — every channel, right after F11
            "F10_cannibalization_cross_tenant",
            "F4_extreme_length", "F0_complete", "F12_scope", "F13_hygiene", "F13_leak",
            "F13_repeat", "F17_trip_fact", "F18_rarity",  # S234 round 3 — pass without contexts
            "F14_social_shape", "F15_voice", "F4_seo_surface",
            "F9_cta_fact", "F9_brand_style",
            "F16_faq",  # S234 round 2 — the blog FAQ contract, last (owned by the FAQ step)
        ]

    def test_extreme_length_measured_on_stripped_text_not_tagged(self):
        # 6000-char ceiling: build content just under it once tags are stripped, but the RAW
        # tagged text (many short tags) pushes it over — must still pass, since F4 measures the
        # stripped length, not the tagged one.
        tag = "[R:atom_abc123]"
        sentence = "A short sentence. "
        tagged_body = f"{tag} ".join([sentence] * 100) + tag * 400  # raw length pushed way over
        assert len(tagged_body) > 6000
        stripped = qg.strip_citation_tags(tagged_body)
        assert len(stripped) < 6000
        assert qg.gate_extreme_length(stripped)["passed"] is True


# ---------------------------------------------------------------- service.run_write_background() leak test
# The single most important test in this file (Nghiep's own framing) — confirms NO [R:/[F: tag
# ever survives into the persisted/returned piece, for a real blog-channel run through the whole
# write/check loop, not just the gate functions in isolation.
#
# AA-466 split the old single write_and_check() into start_write() (fast pre-flight) +
# run_write_background() (the write/check loop, unchanged body) — these tests now target
# run_write_background() directly with a hand-built context dict, and capture what's actually
# passed to _finalize_piece() (an UPDATE by piece_id) instead of _persist_piece() (the old
# INSERT). Same "echo back what was really sent, don't assert against a hardcoded mock return
# value" principle the original _echo_insert_as_returning_row() was written to enforce.

REQUEST_ID = uuid.uuid4()

GOAL = {"key": "promotion", "name": "Promotion", "description": "d", "logic": "AIDA", "marketing_term": "AIDA"}
ANGLE = {"idx": 0, "name": "A", "why_it_works": "wa", "formula_fit": "AIDA",
         "best_final_style": "warm", "recommended": True, "chosen": True}

TAGGED_BLOG_CONTENT = (
    "## Why Southern Laos\n"
    "Cross the bamboo bridge at dawn for a quiet start. [R:atom_abc123]\n\n"
    "A short one.\n\n"
    "## What to Expect\n"
    "The falls draw crowds by midday. [R:atom_abc123] Go early instead.\n\n"
    "## FAQ\n\n"
    "**Q: When is the best time to visit?**\n"
    "A: Early morning, before the tour groups arrive. [R:atom_abc123]"
)


def _context(channel="blog"):
    return {
        "atom_text": "atom text", "goal": GOAL, "channel_style": {"key": channel},
        "brand_audience": {}, "chosen": ANGLE, "cta": "Book a consultation",
        "destination": None, "trip_name": None, "brand_rubric_text": "rubric",
        "channel": channel, "atom_id": "atom_abc123",
    }


def _capturing_finalize(sink: dict):
    """Echoes what run_write_background() actually passed to _finalize_piece() into `sink` —
    a static mock return value can't catch a bug where the loop computes the right value but
    forgets to pass it through (the class of gap the original version of these tests was
    written to catch, adapted from INSERT-args-echo to this kwargs-capture shape for AA-466)."""
    async def _finalize(pool, **kwargs):
        sink.update(kwargs)
        return None
    return _finalize


@pytest.mark.asyncio
class TestWriteAndCheckStripsTagsBeforeOutput:
    async def test_approved_blog_piece_has_no_tag_in_returned_content_or_ledger(self):
        finalized: dict = {}
        tagged_violation_ledger = [
            {"gate": "F6_cta_present", "passed": True, "violations": [], "repairable": True},
            {"gate": "F1_grounding", "passed": True,
             "violations": ["sentence quotes [R:atom_abc123] here, but passed"], "repairable": True},
        ]
        passing_outcome = {
            "passed": True, "gate_ledger": tagged_violation_ledger, "first_failure": None, "flags": [],
        }

        with patch.object(service, "write_content", return_value=(TAGGED_BLOG_CONTENT, 0.02, {}, None)), \
             patch.object(service, "run_quality_gates", return_value=passing_outcome), \
             patch.object(service, "_finalize_piece", new=AsyncMock(side_effect=_capturing_finalize(finalized))):
            await service.run_write_background(REQUEST_ID, uuid.uuid4(), _context(), pool=MagicMock())

        assert "[R:" not in finalized["content_text"]
        assert "[F:" not in finalized["content_text"]
        # confirm the tag-free markdown/prose survived (strip removed only the tag, not content)
        assert "## Why Southern Laos" in finalized["content_text"]
        assert "Cross the bamboo bridge at dawn" in finalized["content_text"]
        # gate_ledger's own violation string must be scrubbed too (F1's own quoted-excerpt path)
        assert "[R:" not in str(finalized["gate_ledger"])

    async def test_held_blog_piece_also_has_no_tag_leak(self):
        # L6 precedent: a held piece is fully visible to the tenant (content + reason + ledger),
        # so it must be scrubbed exactly as thoroughly as an approved one.
        finalized: dict = {}
        failing_ledger = [
            {"gate": "F6_cta_present", "passed": True, "violations": [], "repairable": True},
            {"gate": "F1_grounding", "passed": False,
             "violations": ["'1637' not present in the atom: '[R:atom_abc123] stray'"],
             "repairable": True},
        ]
        held_first_failure = failing_ledger[1]
        failing_outcome = {
            "passed": False, "gate_ledger": failing_ledger, "first_failure": held_first_failure, "flags": [],
        }

        with patch.object(service, "write_content", return_value=(TAGGED_BLOG_CONTENT, 0.02, {}, None)), \
             patch.object(service, "rewrite_with_feedback", return_value=(TAGGED_BLOG_CONTENT, 0.02, {}, None)), \
             patch.object(service, "run_quality_gates", return_value=failing_outcome), \
             patch.object(service, "_finalize_piece", new=AsyncMock(side_effect=_capturing_finalize(finalized))):
            await service.run_write_background(REQUEST_ID, uuid.uuid4(), _context(), pool=MagicMock())

        assert finalized["status"] == "held"
        assert "[R:" not in (finalized["held_reason"] or "")
        assert "[R:" not in str(finalized["gate_ledger"])
        assert "[R:" not in finalized["content_text"]

    async def test_non_blog_channel_write_unaffected_by_strip_step(self):
        # No tags ever produced for non-blog — confirms the unconditional strip call is a true
        # no-op for the other 7 channels, not a behavior change.
        finalized: dict = {}
        passing_outcome = {
            "passed": True,
            "gate_ledger": [{"gate": "F6_cta_present", "passed": True, "violations": [], "repairable": True}],
            "first_failure": None,
            "flags": [],
        }

        with patch.object(service, "write_content",
                          return_value=("Plain facebook post.", 0.02, {}, None)) as mock_write, \
             patch.object(service, "run_quality_gates", return_value=passing_outcome) as mock_gates, \
             patch.object(service, "_finalize_piece", new=AsyncMock(side_effect=_capturing_finalize(finalized))):
            await service.run_write_background(REQUEST_ID, uuid.uuid4(), _context(channel="facebook"), pool=MagicMock())

        assert finalized["content_text"] == "Plain facebook post."
        assert mock_write.call_args.kwargs["atom_id"] == "atom_abc123"
        assert mock_gates.call_args.kwargs["channel"] == "facebook"
