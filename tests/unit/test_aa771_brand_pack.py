"""AA-771 (S233) — tenant brand pack in T8/T9/T10; no Adventure Asia defaults in tenant prompts."""
import asyncio
import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.acp_angle_gate import generate as angle_generate
from services.acp_angle_gate import prompts as angle_prompts
from services.acp_angle_gate.channel_style import CHANNEL_STYLES, get_channel_style
from services.acp_angle_gate.goals import GOALS, get_goal
from services.acp_content_writing import quality_gates as qg
from services.acp_content_writing import sentence_repair
from services.acp_content_writing.prompts import SYSTEM_PROMPT, build_user_prompt
from services.acp_produce import brand as brand_mod
from services.acp_shared.brand_pack import (compile_rubric, foreign_brand_mentions, pack_from_row,
                                            render_brand_block)

ROW = {
    "brand_name": "Terra Family Expeditions", "brand_type": "family adventure",
    "core_idea": "Slow, safe family journeys where children see the world at their own pace.",
    "customer_segment": "Parents with children aged 7-16", "customer_mindset": "Wants safety and pace",
    "voice_examples": '["warm", "reassuring"]', "style_guide": "Do not make the tour sound like a school trip.",
    "forbidden_words": '["curriculum", "lesson plan"]', "good_examples": "We stop when the kids need to.",
    "default_cta": "Ask us for the family itinerary", "system_prompt": None, "version": 3,
}


class TestPack:
    def test_pack_from_row_parses_json_lists(self):
        pack = pack_from_row(ROW)
        assert pack["voice"] == ["warm", "reassuring"]
        assert pack["forbidden_words"] == ["curriculum", "lesson plan"]
        assert pack["default_cta"] == "Ask us for the family itinerary"

    def test_default_brand_name_is_not_a_name(self):
        assert pack_from_row({**ROW, "brand_name": "default"})["brand_name"] is None

    def test_row_without_default_cta_column(self):
        row = {k: v for k, v in ROW.items() if k != "default_cta"}
        assert pack_from_row(row)["default_cta"] is None

    def test_block_carries_style_guide_and_self_reference_rule(self):
        block = render_brand_block(pack_from_row(ROW))
        assert "Do not make the tour sound like a school trip." in block
        assert "Never use these words or phrases: curriculum, lesson plan" in block
        assert "never invent how the brand works" in block
        # AA-775 — the style guide is fenced as style, never a source
        assert "This is style and never a source" in block
        assert "never repeat one of its example sentences" in block
        assert "unless the content seed says so" in block
        assert "follow every rule in it, including each \"do not\"" in block

    def test_empty_pack_block_says_no_brand(self):
        assert "No brand identity is set up" in render_brand_block({})

    def test_compile_rubric_names_the_tenant(self):
        rubric = compile_rubric(pack_from_row(ROW))
        assert rubric.startswith("BRAND IDENTITY: Terra Family Expeditions")
        assert "Adventure Asia" not in rubric
        assert compile_rubric({}) is None


class TestRubricNeverFallsBackToAA:
    def _run(self, row):
        conn = MagicMock()
        conn.fetchrow = AsyncMock(return_value=row)
        return asyncio.run(brand_mod.fetch_brand_rubric_text(conn, "t1"))

    def test_empty_system_prompt_compiles_from_t0_fields(self):
        text = self._run(ROW)
        assert "ADVENTURE ASIA" not in text.upper()
        assert "Terra Family Expeditions" in text and "STYLE GUIDE" in text
        assert "FORBIDDEN WORDS/PHRASES: curriculum, lesson plan" in text

    def test_hand_written_system_prompt_still_wins(self):
        assert self._run({**ROW, "system_prompt": "Custom rubric."}).startswith("Custom rubric.")

    def test_no_brand_row_gets_neutral_rubric(self):
        assert self._run(None) == brand_mod.NEUTRAL_RUBRIC


class TestNoPlatformBrandInTenantPrompts:
    def _texts(self):
        yield angle_prompts.SYSTEM_PROMPT
        yield SYSTEM_PROMPT
        for g in GOALS:
            yield from g.values()
        for c in CHANNEL_STYLES:
            yield from c.values()

    def test_no_adventure_asia_aa_premium_or_vietnamese(self):
        viet = re.compile(r"[ăâđêôơưạảấầẩẫậắằẳẵặẹẻẽếềểễệỉịọỏốồổỗộớờởỡợụủứừửữựỳỵỷỹ]", re.IGNORECASE)
        for text in self._texts():
            assert "Adventure Asia" not in text
            assert not re.search(r"\bAA\b", text), text
            assert "premium" not in text.lower(), text
            assert not viet.search(text), text

    def test_blog_puts_cta_before_faq(self):
        assert get_channel_style("blog")["structure"].index("call-to-action") < \
            get_channel_style("blog")["structure"].index("FAQ")


class TestBrandGates:
    def test_forbidden_word_blocks_and_quotes_sentence(self):
        r = qg.gate_brand_forbidden_words("The kids follow a lesson plan here. Then lunch.", "seed",
                                         ["lesson plan"])
        assert r["passed"] is False and r["blocking"] and r["repairable"]
        assert "'The kids follow a lesson plan here.'" in r["violations"][0]

    def test_forbidden_word_in_source_is_exempt(self):
        assert qg.gate_brand_forbidden_words("A lesson plan.", "the lesson plan day", ["lesson plan"])["passed"]

    def test_other_brand_name_blocks(self):
        r = qg.gate_brand_name("At Arcadia Adventures, we build around that gap.", "seed", "Trail Pulse")
        assert r["passed"] is False and "Arcadia Adventures" in r["violations"][0]

    def test_platform_name_blocks_for_a_tenant(self):
        assert not qg.gate_brand_name("This is how AA builds the visit.", "seed", "Terra")["passed"]

    def test_own_name_and_places_pass(self):
        assert qg.gate_brand_name("At Trail Pulse, we keep it social.", "seed", "Trail Pulse")["passed"]
        assert qg.gate_brand_name("At Paro, we stop for tea.", "Location: Paro, Bhutan", "Terra")["passed"]

    def test_brand_failures_take_sentence_repair(self):
        text = "Intro line here. At Arcadia Adventures, we build itineraries around that gap. End."
        ledger = [qg.gate_brand_name(text, "seed", "Trail Pulse")]
        items = sentence_repair.plan(text, ledger)
        assert items and items[0]["sentence"].startswith("At Arcadia Adventures")
        assert "Trail Pulse" in items[0]["problem"]


def test_detector_on_s232_examples():
    assert foreign_brand_mentions("At Adventurous Afternoons, this is precisely what we build.",
                                  "Atlas & Hearth") == ["Adventurous Afternoons"]


class TestWriterPrompt:
    def test_writer_gets_the_pack(self):
        prompt = build_user_prompt(
            content_seed="seed", goal=get_goal("promotion"), channel_style=get_channel_style("linkedin"),
            brand_audience=pack_from_row(ROW), angle={"name": "a", "why_it_works": "w", "best_final_style": "s"},
            cta="Ask us",
        )
        assert "Core idea: Slow, safe family journeys" in prompt
        assert "discerning travellers" not in prompt
        assert "plain text only" in prompt  # linkedin

    def test_blog_gets_no_plain_text_rule(self):
        prompt = build_user_prompt(
            content_seed="seed", goal=get_goal("promotion"), channel_style=get_channel_style("blog"),
            brand_audience={}, angle={"name": "a", "why_it_works": "w", "best_final_style": "s"}, cta="Ask us",
        )
        assert "plain text only" not in prompt
        assert "BEFORE any FAQ" in prompt


class TestAngleContract:
    ANGLES = [{"name": f"A{i}", "why_it_works": "w", "formula_fit": "f", "best_final_style": "s",
               "answers": []} for i in range(3)]

    def test_violations_found(self):
        bad = [dict(self.ANGLES[0], why_it_works="shows how AA builds this visit"),
               dict(self.ANGLES[1], best_final_style="frame it as a lesson plan"), self.ANGLES[2]]
        out = angle_generate.angle_contract_violations(bad, pack_from_row(ROW))
        assert any("'AA'" in v for v in out) and any("lesson plan" in v for v in out)

    def test_clean_angles_make_one_call(self):
        with patch.object(angle_generate, "_generate_once",
                          AsyncMock(return_value=(self.ANGLES, 0, "r", 0.1))) as gen:
            out = asyncio.run(angle_generate.generate_angles(brand_audience=pack_from_row(ROW), content_seed="s"))
        assert gen.await_count == 1 and out[3] == 0.1

    def test_violating_angles_regenerate_once_with_feedback(self):
        bad = [dict(self.ANGLES[0], name="How AA does it")] + self.ANGLES[1:]
        with patch.object(angle_generate, "_generate_once",
                          AsyncMock(side_effect=[(bad, 0, "r", 0.1), (self.ANGLES, 1, "r", 0.2)])) as gen:
            angles, idx, _, cost = asyncio.run(
                angle_generate.generate_angles(brand_audience=pack_from_row(ROW), content_seed="s"))
        assert gen.await_count == 2 and idx == 1 and cost == pytest.approx(0.3)
        assert gen.await_args.kwargs["revision_feedback"] and gen.await_args.kwargs["attempt"] == 2

    def test_angle_prompt_has_channel_and_brand(self):
        prompt = angle_prompts.build_user_prompt(
            content_seed="seed", goal=get_goal("promotion"), brand_audience=pack_from_row(ROW),
            destination="Bhutan", trip_name="T", channel_style=get_channel_style("linkedin"))
        assert "CHANNEL: LinkedIn" in prompt and "Core idea:" in prompt
