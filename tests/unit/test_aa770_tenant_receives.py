"""AA-770 (S233) — the tenant always receives the content; quality verdicts stay admin-side."""
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.routers import v1_marketplace
from api.routers import v1_publish as mod
from services.acp_content_writing import quality_gates as qg
from services.acp_content_writing import service
from services.acp_s4_blog.cms.wordpress import WordPressAdapter
from tests.unit.test_aa458_publish import (PIECE_ID, TENANT, _claimed, _integ_row, _make_request, _piece_row,
                                           _sequential_pool)


@pytest.mark.asyncio
async def test_held_piece_publishes_like_an_approved_one():
    write_result = {"publish_id": str(uuid.uuid4()), "status": "published", "external_id": "7",
                    "external_url": "https://b/7", "published_at": datetime(2026, 10, 13, tzinfo=timezone.utc),
                    "last_error": None}
    held = {**_piece_row(), "status": "held"}
    pool, _ = _sequential_pool(held, _integ_row(), write_result)
    from services.acp_s4_blog.cms.base import CMSPostResult
    create = AsyncMock(return_value=CMSPostResult(post_id=7, post_url="https://b/7", status="publish",
                                                  cms_type="wordpress"))
    with patch.object(mod, "_get_secret", return_value={"wp_url": "https://b", "username": "u", "app_password": "p"}), \
            patch.object(mod, "claim", _claimed()), patch.object(WordPressAdapter, "create_post", create):
        result = await mod.publish(PIECE_ID, _make_request(pool), TENANT)
    assert result["success"] is True and create.await_count == 1


def test_tenant_projection_has_no_gate_data():
    row = {"piece_id": uuid.uuid4(), "tenant_id": uuid.uuid4(), "angle_gate_request_id": uuid.uuid4(),
           "attempt_number": 2, "content_text": "Text.", "status": "held", "held_reason": "F9: x",
           "gate_ledger": "[]", "repair_log": "[]", "created_at": datetime(2026, 10, 13, tzinfo=timezone.utc),
           "seo_title": None, "meta_description": None, "slug": None, "channel": "linkedin",
           "route_hub_name": None, "route_segment_count": None, "flags": '[{"gate": "F8"}]'}
    out = service._tenant_safe_piece(row)
    assert out["ready_state"] == "ready" and out["content_text"] == "Text."
    assert not {"flags", "held_reason", "gate_ledger", "repair_log", "status"} & set(out)


@pytest.mark.parametrize("raw,shown", [("ai_generated", "ready"), ("needs_review", "ready"), ("pending", "writing"),
                                       ("failed", "failed"), ("approved", "approved"), ("rejected", "rejected")])
def test_marketplace_shows_written_state_not_qa(raw, shown):
    assert v1_marketplace.tenant_version_state(raw) == shown


class TestGatesAdvisory:
    def _blog(self, text):
        f9 = {"status": "pass", "brand_fit": "1", "cta_clear": "1", "human_read": "1", "failure_codes": [],
              "flagged_phrases": [], "notes": ""}
        rubric = qg.get_framework_rubric("promotion")
        f8 = {"items": [{"criterion": c, "score": "1", "evidence": "q"} for c in rubric]}
        import json
        with patch.object(qg, "invoke_judge", side_effect=[{"text": json.dumps(f8)}, {"text": json.dumps(f9)}]):
            return qg.run_quality_gates(content_text=text, atom_text="seed", cta="Ask us", goal_key="promotion",
                                        brand_rubric_text="r", channel="blog", seo_title="x" * 50,
                                        meta_description="x" * 130 + ".", slug="a-b", keyword=None)

    def test_blog_shape_gates_only_warn(self):
        out = self._blog("## One\n" + "Word after word here. " * 300)  # no tags, no rhythm
        ledger = {g["gate"]: g for g in out["gate_ledger"]}
        for gate in ("F5_atom_density", "F3_structural_variance", "F7_faq_dedup"):
            assert ledger[gate]["blocking"] is False
        assert (out["first_failure"] or {}).get("gate") not in ("F5_atom_density", "F3_structural_variance",
                                                                "F7_faq_dedup")

    def test_judge_outage_does_not_hold(self):
        with patch.object(qg, "invoke_judge", side_effect=RuntimeError("timeout")):
            results = qg.gate_brand_voice("Text.", "Ask us", "rubric", source_text="seed")
        assert results[0]["gate"] == "F9_cta_fact" and results[0]["blocking"] is False
        assert "not checked" in results[0]["violations"][0]


def test_restated_faq_pairs_are_dropped():
    intro = "Weavers from across the valley sell handloom textiles at the Thimphu weekend market."
    text = (f"{intro}\n\n## FAQ\n\n"
            "**Q: What?**\nA: Weavers from across the valley sell handloom textiles at the weekend market.\n\n"
            "**Q: When?**\nA: Weekends between Friday and Sunday mornings.\n")
    out, dropped = qg.drop_restated_faq(text)
    assert dropped == 1 and "**Q: What?**" not in out and "**Q: When?**" in out
    only = f"{intro}\n\n## FAQ\n\n**Q: What?**\nA: {intro}"
    out, dropped = qg.drop_restated_faq(only)
    assert dropped == 1 and "## FAQ" not in out


def _outcome(passed, gate="F9_cta_fact", violations=None):
    g = {"gate": gate, "passed": passed, "repairable": True, "blocking": True,
         "violations": violations or []}
    return {"passed": passed, "gate_ledger": [g], "first_failure": None if passed else g, "flags": []}


@pytest.mark.asyncio
async def test_attempt_three_is_a_sentence_repair_only():
    claim = ('audit manual_check: FACT_CHECK_MANUAL_CHECK — unsupported claim(s): '
             '"The monastery sits above Luang Prabang today." (claims: Luang Prabang; source: not in seed)')
    text = "Opening line here. The monastery sits above Luang Prabang today. End line."
    ctx = {"atom_text": "seed", "goal": {"key": "promotion", "name": "P", "description": "d", "logic": "l"},
           "channel_style": {"channel": "linkedin"}, "brand_audience": {}, "chosen": {"name": "a"}, "cta": "Ask",
           "destination": None, "trip_name": None, "brand_rubric_text": "r", "channel": "linkedin",
           "atom_id": "a1", "tenant_id": None}
    with patch.object(service, "write_content", return_value=(text, 0.01, {}, None)), \
            patch.object(service, "rewrite_with_feedback") as rewrite, \
            patch.object(service, "repair_sentences", return_value=(["The monastery sits on a hill."], 0.01)) as rep, \
            patch.object(service, "run_quality_gates", side_effect=[
                _outcome(False, violations=[claim]),
                _outcome(False, violations=[claim.replace("Luang Prabang today", "Luang Prabang today")]),
                _outcome(True)]), \
            patch.object(service, "observe_t10", new=AsyncMock()), \
            patch.object(service.sentence_repair, "plan", side_effect=[
                [{"start": 19, "end": 64, "sentence": "s", "problem": "p"}],
                [{"start": 19, "end": 64, "sentence": "s", "problem": "p"}],
                [{"start": 19, "end": 64, "sentence": "s", "problem": "p"}]]), \
            patch.object(service, "_finalize_piece", new=AsyncMock()) as fin:
        await service.run_write_background(uuid.uuid4(), uuid.uuid4(), ctx, pool=MagicMock())
    rewrite.assert_not_called()
    assert rep.call_count == 2
    assert fin.await_args.kwargs["status"] == "approved" and fin.await_args.kwargs["attempt_number"] == 3
