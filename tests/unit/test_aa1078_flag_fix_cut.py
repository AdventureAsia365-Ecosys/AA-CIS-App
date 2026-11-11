"""AA-1078 (S277) — a flag_fix reply cut at its token ceiling is a lost fix, never a pass; the flag_fix stages carry
their task in quality_signal; the T2 job does not report QA passed over a lost fix.

Reproduces the S276 audit §4.7 case (t2_flag_fix on Haiku 5.5, a reply cut at max_tokens after the gateway retry:
"succeeded, passed, 10.0 (fix not applied)") through the production A1 graph (the S1 harness, switch off = main's
flag_fix) and the production T2 save (the T2 harness). Invented texts only; no network, DB or AWS."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch


from tests.unit.fixtures import aa1049_s1_harness as h1
from tests.unit.fixtures import aa1049_t2_harness as h2

# a reply cut mid-field: its JSON is partial, json_repair would salvage a field cut short
CUT_ITINERARY = '{"itineraries": "Day 1 — Arrive in Velmora\\nArrive in Velmora and transfer to the guest'


def _cut_stage(stage: str):
    """A ScriptedLLM.generate that answers ``stage`` with a reply stopped on its token ceiling."""
    orig = h1.ScriptedLLM.generate

    def generate(self, request):
        resp = orig(self, request)
        if getattr(request, "stage", None) == stage:
            return SimpleNamespace(**{**vars(resp), "content": CUT_ITINERARY, "stop_reason": "max_tokens",
                                      "output_tokens": 4096})
        return resp
    return generate


def test_a1_cut_flag_fix_reply_is_not_applied_and_not_publishable(monkeypatch):
    monkeypatch.setattr(h1.ScriptedLLM, "generate", _cut_stage("s1_flag_fix"))
    t = h1.run("meal_in_prose", extra_keys=("fix_not_applied",))
    r = t["result"]
    assert [c[0] for c in t["llm"]] == ["s1_generate", "s1_generate", "s1_flag_fix"]
    assert r["fix_pass_applied"] is False and r["fix_pass_fields"] == []
    itin = r["generated"]["itineraries"]
    assert "guesthouse near the old market" in itin and "Day 3" in itin     # never the field cut short
    assert "Lunch is included at the station cafe" in itin                  # the flagged text stays for review
    assert r["fix_not_applied"] == {"reason": "reply_cut", "stage": "s1_flag_fix", "fields": ["itineraries"],
                                    "stop_reason": "max_tokens"}
    assert r["publishable"] is False                                         # flagged and not fixed: held


def test_a1_uncut_reply_has_no_fix_not_applied():
    t = h1.run("meal_in_prose", extra_keys=("fix_not_applied",))
    assert t["result"]["fix_not_applied"] is None and t["result"]["fix_pass_applied"] is True


def test_the_cut_reply_row_and_the_field_fix_row_carry_the_task(monkeypatch):
    rows = []
    monkeypatch.setattr(h1.ScriptedLLM, "generate", _cut_stage("s1_flag_fix"))

    def grab(st):
        from services.content_generation import flag_fix_node
        st.enter_context(patch.object(flag_fix_node, "record_call_sync", lambda **kw: rows.append(kw)))
    h1.run("meal_in_prose", extra=grab)
    assert len(rows) == 1
    qs = rows[0]["quality_signal"]
    assert qs["task"] == "field_fix" and qs["fix_not_applied"] is True and rows[0]["stop_reason"] == "max_tokens"


def test_bind_task_tags_every_row_written_while_bound():
    from shared.llm_client import call_log
    with call_log.bind_task("meta_rerepair"):
        _, _, qs, _, _ = call_log._relabel("t2_flag_fix", None, {"attempt": 1}, None, None)
        _, _, own, _, _ = call_log._relabel("t2_flag_fix", None, {"task": "sentence_repair"}, None, None)
        gateway = call_log._insert_args(call_log.cut_reply_row(
            SimpleNamespace(model_used="m", input_tokens=1, output_tokens=4096, cost_usd=0.0, stop_reason="max_tokens",
                            satellite_account=None, fallback_used=False, provider="anthropic"),
            stage="t2_flag_fix", role="writer", max_tokens=4096, retry_max_tokens=8192))
    assert qs == {"attempt": 1, "task": "meta_rerepair"} and own == {"task": "sentence_repair"}
    assert json.loads(gateway[7])["task"] == "meta_rerepair"
    _, _, unbound, _, _ = call_log._relabel("t2_flag_fix", None, {"attempt": 1}, None, None)
    assert unbound == {"attempt": 1}


def test_grounding_sentence_repair_rows_name_their_task():
    from services.content_generation import grounding as g
    rows, seen = [], []

    class LLM:
        def __init__(self, *a, **k):
            pass

        def generate(self, req):
            from shared.llm_client import call_log
            seen.append(call_log.current_task())
            return SimpleNamespace(content='{"1": "Velmora is a market town."}', cost_usd=0.0, model_used="m",
                                   input_tokens=1, output_tokens=1, stop_reason="end_turn")
    gen = {"summary": "Velmora is 412 years old. It sits on a canal."}
    vio = [{"field": "summary", "sentence": "Velmora is 412 years old.", "code": "UNSUPPORTED_NUMBER"}]
    with patch("shared.llm_client.client.LLMClient", LLM), \
            patch("shared.llm_client.call_log.record_call_sync", lambda **kw: rows.append(kw)):
        g.repair(gen, vio, {"summary": "Velmora sits on a canal."})
    assert seen == ["sentence_repair"] and rows[0]["quality_signal"]["task"] == "sentence_repair"


def _with_lost_fix(st):
    """The T2 kept draft's graph lost its flag_fix fix (what a cut reply leaves): the save must not say QA passed."""
    from api.routers import v1_pipeline
    orig = v1_pipeline._rewrite_tour

    async def rewrite(*a, **k):
        out = await orig(*a, **k)
        return {**out, "fix_not_applied": {"reason": "reply_cut", "stage": "t2_flag_fix", "fields": ["seo_meta"],
                                           "stop_reason": "max_tokens"}}
    st.enter_context(patch.object(v1_pipeline, "_rewrite_tour", rewrite))


def _saved_qa(trace) -> tuple:
    for sql, args in trace["sql"]:
        if "UPDATE gold_aa_internal.tenant_tour_versions SET rewritten_content" in sql:
            return args[3], args[5]                       # qa_status, qa_auto_passed
    raise AssertionError("no version save")


def test_t2_save_reports_escalated_over_a_lost_fix():
    base = h2.run("clean")
    assert _saved_qa(base) == ("passed", False)
    t = h2.run("clean", extra=_with_lost_fix)
    assert _saved_qa(t) == ("escalated", True)
    assert t["diag"]["fix_not_applied"]["reason"] == "reply_cut"
    assert "fix_not_applied" not in base["diag"]
