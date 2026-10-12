"""AA-768 (S233) — per-channel writer ceiling, one retry on a ceiling stop, the F0_complete gate,
and the regenerated SEO title/meta reaching WordPress."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.acp_angle_gate.channel_style import get_channel_style
from services.acp_angle_gate.goals import get_goal
from services.acp_content_writing import generate
from services.acp_content_writing import quality_gates as qg
from shared.llm_client.models import LLMResponse


def _resp(content: str, stop: str = "end_turn", cost: float = 0.1) -> LLMResponse:
    return LLMResponse(content=content, model_used="m", provider="p", cost_usd=cost, stop_reason=stop)


_KW = dict(
    content_seed="Pawangka Monastery — visit", goal=get_goal("promotion"), brand_audience={},
    angle={"name": "a", "why_it_works": "w", "best_final_style": "s"}, cta="Ask us",
)


def _style(channel: str) -> dict:
    return get_channel_style(channel)


class TestWriterCeiling:
    def test_blog_gets_a_larger_ceiling_than_social(self):
        assert generate.max_tokens_for("blog") == 8000
        assert generate.max_tokens_for("linkedin") == 3000

    def test_ceiling_stop_is_retried_once_with_more_room(self):
        client = MagicMock()
        client.generate.side_effect = [_resp("cut", "max_tokens"), _resp("Full post.", "end_turn", 0.2)]
        with patch.object(generate, "LLMClient", return_value=client), \
                patch.object(generate, "record_call_sync") as log:
            text, cost, _, _ = generate.write_content(channel_style=_style("linkedin"), **_KW)
        assert text == "Full post."
        assert cost == pytest.approx(0.3)
        first, second = (c.args[0] for c in client.generate.call_args_list)
        assert first.max_tokens == 3000 and second.max_tokens == 4500
        assert log.call_count == 2
        assert log.call_args.kwargs["quality_signal"]["truncation_retry"] is True

    def test_openai_length_stop_counts_as_truncated(self):
        client = MagicMock()
        client.generate.side_effect = [_resp("cut", "length"), _resp("Done.")]
        with patch.object(generate, "LLMClient", return_value=client), patch.object(generate, "record_call_sync"):
            generate.write_content(channel_style=_style("facebook"), **_KW)
        assert client.generate.call_count == 2

    def test_normal_stop_makes_one_call(self):
        client = MagicMock()
        client.generate.return_value = _resp("Done.")
        with patch.object(generate, "LLMClient", return_value=client), patch.object(generate, "record_call_sync"):
            generate.write_content(channel_style=_style("linkedin"), **_KW)
        assert client.generate.call_count == 1

    def test_retry_ceiling_is_capped(self):
        client = MagicMock()
        client.generate.side_effect = [_resp('{"body": "cut', "max_tokens"),
                                       _resp('{"body": "Done.", "seo_title": "t"}')]
        with patch.object(generate, "LLMClient", return_value=client), patch.object(generate, "record_call_sync"):
            generate.write_content(channel_style=_style("blog"), **_KW)
        assert client.generate.call_args_list[1].args[0].max_tokens == 12000


class TestCompleteGate:
    @pytest.mark.parametrize("text", [
        "Clear skies, moderate temperatures, and unobstructed t",
        "and what to expect at each stage — starting",
        "Ta Dzong National Museum, among other cultural landmarks",
        "It actively supports and teaches.\\",
    ])
    def test_cut_text_fails(self, text):
        result = qg.gate_complete(text)
        assert result["passed"] is False and result["blocking"] and result["repairable"]

    @pytest.mark.parametrize("text", [
        "A finished sentence.",
        "Ask us what you would choose?",
        "Plan it with us (link in comments)",
        "Great climb.\n\n#Bhutan #TigersNest",
        "## FAQ\n**When?**\nIn spring.\n- a closing list item",
        "Tagged end. [R:atom_abc]",
    ])
    def test_finished_text_passes(self, text):
        assert qg.gate_complete(text)["passed"] is True

    def test_runs_for_every_channel(self):
        f9 = {"status": "pass", "brand_fit": "1", "cta_clear": "1", "human_read": "1",
              "failure_codes": [], "flagged_phrases": [], "notes": ""}
        rubric = qg.get_framework_rubric("promotion")
        f8 = {"items": [{"criterion": c, "score": "1", "evidence": "q"} for c in rubric]}
        import json
        raw = [{"content": json.dumps(f8)}, {"content": json.dumps(f9)}]
        with patch.object(qg, "invoke_judge", side_effect=raw):
            out = qg.run_quality_gates(content_text="The trail climbs to the monastery and", atom_text="the trail",
                                       cta="Ask us", goal_key="promotion", brand_rubric_text="r", channel="linkedin")
        assert out["first_failure"]["gate"] == "F0_complete"


@pytest.mark.asyncio
async def test_wordpress_publish_uses_piece_seo_title_and_meta():
    from api.routers import v1_publish
    wp = MagicMock()
    wp.create_post = AsyncMock(return_value=MagicMock(post_id=7, post_url="https://x/7"))
    piece = {"content_text": "## Hi\nBody.", "slug": "s", "angle_name": "Internal angle name",
             "seo_title": "Thimphu Craft Bazaar: A Weekend Textile Market Guide",
             "meta_description": "A meta that is long enough."}
    with patch.object(v1_publish, "_wordpress", return_value=wp):
        await v1_publish._call_adapter("blog", {}, piece, "k")
    content = wp.create_post.call_args.args[0]
    assert content.title == piece["seo_title"] and content.seo_title == piece["seo_title"]
    assert content.seo_meta == piece["meta_description"]


@pytest.mark.asyncio
async def test_wordpress_publish_falls_back_to_angle_name():
    from api.routers import v1_publish
    wp = MagicMock()
    wp.create_post = AsyncMock(return_value=MagicMock(post_id=7, post_url="https://x/7"))
    piece = {"content_text": "Body.", "slug": "s", "angle_name": "Angle", "seo_title": None,
             "meta_description": None}
    with patch.object(v1_publish, "_wordpress", return_value=wp):
        await v1_publish._call_adapter("blog", {}, piece, "k")
    assert wp.create_post.call_args.args[0].title == "Angle"
