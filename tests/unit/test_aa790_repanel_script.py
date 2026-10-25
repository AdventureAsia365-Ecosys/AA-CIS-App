"""AA-790 (S251) — scripts/aa790/t9_writer_repanel.py offline: the prompt is rebuilt from a stored writer_input, the
non-blog pieces of one request get the lead-fact rotation, the call is logged as adhoc_aa790 / writer, and the
deterministic steps run on the reply. No DB, no model call (the gateway and the call log are faked)."""
import json
from types import SimpleNamespace

from scripts.aa790 import t9_writer_repanel as rp

SEED = ("National Institute for Zorig Chusum — visit\nLocation: Thimphu, Bhutan\n"
        "Source itinerary: The institute teaches Bhutan's thirteen traditional arts. Students enrol in a "
        "multi-year programme under master craftspeople. Graduates restore and decorate dzongs and monasteries.")
PACK = {"brand_name": "Terra Family Expeditions", "brand_type": "Premium family adventure travel brand",
        "customer_segment": "Families with children aged 7-16", "voice": ["Warm", "Family-aware"]}


def _row(pid: str, req: str, channel: str = "linkedin", created: str = "2026-10-25 12:00:00") -> dict:
    return {"piece_id": pid, "tenant_id": "t", "request_id": req, "channel": channel, "status": "approved",
            "created_at": created, "content_text": "Old text. That's the opening. It was written before.",
            "atom_id": "a1", "tenant": "Terra Family Expeditions",
            "writer_input": {"seed": SEED, "goal": "introduction_awareness", "channel": channel, "brand_pack": PACK,
                             "angle": {"name": "Craft Passed On", "why_it_works": "w", "formula_fit": "f",
                                       "best_final_style": "s"},
                             "cta": "Message us to start planning your family trip.", "trip": "Bhutan Trail"}}


def test_write_indexes_follow_each_request():
    rows = [_row("p1", "r1"), _row("p2", "r1"), _row("p3", "r2"), _row("p4", "r1")]
    assert rp.write_indexes(rows) == {"p1": 0, "p2": 1, "p3": 0, "p4": 2}


def test_writer_args_rebuild_the_prompt_with_the_lead_fact():
    from services.acp_content_writing.prompts import build_user_prompt
    first, second = rp.writer_args(_row("p1", "r1"), 0), rp.writer_args(_row("p2", "r1"), 1)
    assert "lead_focus" not in first and second["lead_focus"].startswith("Students enrol")
    prompt = build_user_prompt(**second)
    assert "LEAD WITH THIS FACT" in prompt and "Terra Family Expeditions" in prompt


def test_call_is_logged_as_adhoc_and_the_pass_runs(monkeypatch):
    calls, logged = [], []

    class _Client:
        def generate(self, req):
            calls.append(req)
            return SimpleNamespace(content="Students spend years on one craft. That's the opening. Families watch the "
                                           "workshops in Thimphu.\n\n===SUMMARY===\ns", cost_usd=0.02,
                                   model_used="sonnet-4-6", input_tokens=10, output_tokens=20,
                                   stop_reason="end_turn", satellite_account="acc3", fallback_used=False,
                                   provider="bedrock-satellite")
    monkeypatch.setattr("shared.llm_client.client.LLMClient", _Client)
    monkeypatch.setattr("shared.llm_client.call_log.record_call_sync", lambda **kw: logged.append(kw))
    row = _row("p2", "r1")
    args = rp.writer_args(row, 1)
    w = rp.call_writer(row, args, "sonnet-4-6", "run1")
    assert calls[0].stage == "adhoc_aa790" and calls[0].model_tier == "sonnet-4-6"
    assert logged[0]["stage"] == "adhoc_aa790" and logged[0]["role"] == "writer"
    assert "tenant_id" not in logged[0]  # adhoc spend is never metered to the test tenant
    text, log, writer_census = rp.finish(w["text"], row, args)
    assert "That's the opening." not in text and any("verdict line" in ln for ln in log)
    assert writer_census["fixable_total"] == 1
    json.dumps({"text": text, "log": log})  # the result row is plain JSON


def test_billed_but_unusable_reply_writes_one_billed_unusable_row(monkeypatch):
    """AA-927 canonical pattern: a reply that came back (billed) but is empty writes a billed_unusable row, no
    ordinary row, and the error reaches the per-piece handler."""
    import pytest

    logged, unusable = [], []

    class _Client:
        def generate(self, req):
            return SimpleNamespace(content="   \n\n===SUMMARY===\ns", cost_usd=0.02, model_used="sonnet-4-6",
                                   input_tokens=10, output_tokens=0, stop_reason="end_turn",
                                   satellite_account="acc3", fallback_used=False, provider="bedrock-satellite")
    monkeypatch.setattr("shared.llm_client.client.LLMClient", _Client)
    monkeypatch.setattr("shared.llm_client.call_log.record_call_sync", lambda **kw: logged.append(kw))
    monkeypatch.setattr("shared.llm_client.call_log.record_unusable_sync",
                        lambda resp, **kw: unusable.append((resp, kw)))
    row = _row("p1", "r1")
    with pytest.raises(ValueError):
        rp.call_writer(row, rp.writer_args(row, 0), "sonnet-4-6", "run1")
    assert not logged
    assert len(unusable) == 1 and unusable[0][1]["stage"] == "adhoc_aa790" and unusable[0][1]["role"] == "writer"
    assert unusable[0][0].cost_usd == 0.02
