"""AA-779 (S247, ADR 0011) — the optional LLM filter of the blog FAQ's real search questions (faq_filter.py).

Flag off = the deterministic list and no model call; shadow = deterministic list + recorded verdicts; on = the
model's KEEP list. Any gateway error, unusable reply or timeout falls back to the deterministic list. Every
reply writes one llm_call_log row on stage t9_faq_filter, role validate.
"""
import asyncio
import json
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from services.acp_content_writing import faq_filter as ff
from services.acp_content_writing import quality_gates as qg
from shared.llm_client.models import LLMResponse
from shared.llm_client.role_config import SAFE_DEFAULTS

SEED = (
    "Lake Khövsgöl — trekking along the shore\n"
    "Location: Khövsgöl Lake, Khövsgöl Province, Mongolia (do not place it in any other town, region or country)\n"
    "Source itinerary: Trek the lakeshore; boat trips and horse riding are offered.\n"
    "Place background (checked facts about the place itself — you may use these):\n"
    "- Lake Khövsgöl holds an estimated 70 percent of Mongolia's fresh water.\n"
    "- The lake freezes solidly in winter, typically from January through April.\n"
)
QUESTIONS = [
    "how deep is lake khovsgol",                                     # deterministic KEEP
    "Is Mongolia safe for tourists?",                                # deterministic DROP (country only)
    "What is the best way to get from Khatgal to the Gobi desert?",  # out of scope
    "How deep is lake Khövsgöl?",                                    # duplicate after folding
    "Hi?",                                                           # too short: never a candidate
]
CANDIDATES = ["How deep is lake khovsgol?", "Is Mongolia safe for tourists?",
              "What is the best way to get from Khatgal to the Gobi desert?"]
CONTEXT = {"destination": "Mongolia", "trip_name": "Khovsgol and Gobi tour", "scope": None,
           "trip_places": ["Gobi Desert", "Khatgal"], "tenant_id": "t-1"}
OUT_OF_SCOPE = ("gobi", "khatgal")


def _reply(labels, **extra):
    items = [{"n": i, "label": lab, "reason_code": "on_subject" if lab == "KEEP" else "outside_piece_scope",
              "confidence": "high", "note": "x"} for i, lab in enumerate(labels, 1)]
    return LLMResponse(content=json.dumps({"items": items}), model_used="satellite-haiku-4-5", provider="bedrock",
                       input_tokens=900, output_tokens=120, cost_usd=0.0015, satellite_account="acc3", **extra)


def _client(resp=None, exc=None):
    client = MagicMock()
    if exc is not None:
        client.generate.side_effect = exc
    else:
        client.generate.return_value = resp
    return client


def _det():
    return qg.paa_for_scope(QUESTIONS, SEED, None, OUT_OF_SCOPE)


def _select(mode, client=None, **kw):
    with patch.object(ff, "LLMClient", return_value=client or _client(_reply(["KEEP", "KEEP", "DROP"]))), \
            patch.object(ff, "record_call_sync") as rec:
        out = asyncio.run(ff.select_faq_questions(QUESTIONS, own_seed=SEED, facts_text=None,
                                                  out_of_scope=OUT_OF_SCOPE, context=CONTEXT,
                                                  piece_id="p-1", request_id="r-1", mode=mode, **kw))
    return out, rec


# ---------------------------------------------------------------- flag

@pytest.mark.parametrize("raw,mode", [(None, "off"), ("", "off"), ("off", "off"), ("bogus", "off"),
                                      ("shadow", "shadow"), ("on", "on"), ("ON", "on"), ("1", "on"),
                                      ("true", "on")])
def test_mode_reads_the_env_flag(monkeypatch, raw, mode):
    if raw is None:
        monkeypatch.delenv(ff.FAQ_FILTER_ENV, raising=False)
    else:
        monkeypatch.setenv(ff.FAQ_FILTER_ENV, raw)
    assert ff.faq_filter_mode() == mode


def test_flag_off_is_the_deterministic_list_and_calls_nothing(monkeypatch):
    monkeypatch.delenv(ff.FAQ_FILTER_ENV, raising=False)
    client = _client(_reply(["KEEP", "KEEP", "KEEP"]))
    (kept, record), rec = _select(None, client=client)
    assert kept == _det() == ["How deep is lake khovsgol?"]
    assert record == {}
    client.generate.assert_not_called()
    rec.assert_not_called()


# ---------------------------------------------------------------- on / shadow

def test_on_applies_the_model_keep_and_drop():
    (kept, record), rec = _select("on")
    assert kept == ["How deep is lake khovsgol?", "Is Mongolia safe for tourists?"]
    assert record["mode"] == "on" and record["fallback"] is False
    assert record["deterministic"] == _det()
    assert [v["q"] for v in record["verdicts"]] == CANDIDATES
    assert [v["label"] for v in record["verdicts"]] == ["KEEP", "KEEP", "DROP"]
    assert record["model"] == "satellite-haiku-4-5" and record["cost_usd"] == 0.0015


def test_shadow_records_verdicts_but_keeps_the_deterministic_list():
    (kept, record), rec = _select("shadow")
    assert kept == _det()
    assert record["llm_kept"] == ["How deep is lake khovsgol?", "Is Mongolia safe for tourists?"]
    rec.assert_called_once()


def test_one_call_per_piece_with_every_candidate_numbered():
    client = _client(_reply(["KEEP", "KEEP", "DROP"]))
    _select("on", client=client)
    client.generate.assert_called_once()
    req = client.generate.call_args.args[0]
    assert req.stage == ff.FAQ_FILTER_STAGE and req.model_tier is None and req.temperature == 0.0
    numbered = re.findall(r"^(\d+)\. (.+)$", req.user_prompt, re.M)
    assert [q for _, q in numbered] == CANDIDATES
    assert "COUNTRY: Mongolia" in req.user_prompt
    assert "Places in this post: Khövsgöl Lake; Khövsgöl Province" in req.user_prompt
    assert "Trip places outside this post (not covered): Gobi Desert; Khatgal" in req.user_prompt
    assert "SCOPE: This piece covers one moment: Lake Khövsgöl — trekking along the shore." in req.user_prompt


def test_keep_list_is_capped_in_candidate_order():
    qs = [f"Question {i} about Lake Khövsgöl?" for i in range(10)]
    with patch.object(ff, "record_call_sync"):
        out = ff.llm_filter_questions(qs, ff.scope_from_context(CONTEXT, SEED),
                                      client=_client(_reply(["DROP"] + ["KEEP"] * 9)))
    assert out.kept == qs[1:1 + qg.PAA_MAX_QUESTIONS]
    assert len(out.verdicts) == 10


def test_no_candidates_means_no_call():
    client = _client(_reply([]))
    with patch.object(ff, "record_call_sync") as rec:
        out = ff.llm_filter_questions(["Hi?", None, 42], ff.scope_from_context(CONTEXT, SEED), client=client)
    assert out.kept == [] and out.verdicts == []
    client.generate.assert_not_called()
    rec.assert_not_called()


# ---------------------------------------------------------------- llm_call_log

def test_call_log_row_has_stage_role_and_ids():
    (kept, record), rec = _select("on")
    kw = rec.call_args.kwargs
    assert kw["stage"] == "t9_faq_filter" and kw["role"] == "validate"
    assert kw["model"] == "satellite-haiku-4-5" and kw["cost_usd"] == 0.0015
    assert kw["tokens_in"] == 900 and kw["tokens_out"] == 120 and kw["account"] == "acc3"
    assert kw["tenant_id"] == "t-1" and kw["content_piece_id"] == "p-1" and kw["angle_gate_request_id"] == "r-1"
    qs = kw["quality_signal"]
    assert qs["issue"] == "AA-779" and qs["mode"] == "on" and qs["outcome"] == "ok"
    assert qs["n_questions"] == 3 and qs["keep"] == 2 and qs["drop_reasons"] == {"outside_piece_scope": 1}


def test_adhoc_stage_and_model_override_for_a_measurement():
    client = _client(_reply(["KEEP", "DROP", "DROP"]))
    with patch.object(ff, "record_call_sync") as rec:
        ff.llm_filter_questions(QUESTIONS, ff.scope_from_context(CONTEXT, SEED), stage="adhoc_aa779",
                                model_tier="haiku", client=client)
    req = client.generate.call_args.args[0]
    assert req.stage == "adhoc_aa779" and req.model_tier == "haiku"
    assert rec.call_args.kwargs["stage"] == "adhoc_aa779" and rec.call_args.kwargs["role"] == "validate"


# ---------------------------------------------------------------- fallback

def test_gateway_error_falls_back_to_the_deterministic_list():
    (kept, record), rec = _select("on", client=_client(exc=RuntimeError("bedrock down")))
    assert kept == _det()
    assert record["fallback"] is True and "bedrock down" in record["error"]
    rec.assert_not_called()  # nothing billed reached the caller; the gateway logs its own billed failures


def test_unusable_reply_is_logged_then_falls_back():
    bad = LLMResponse(content='{"items": [{"n": 1, "label": "KEEP"}]}', model_used="satellite-haiku-4-5",
                      provider="bedrock", cost_usd=0.001)
    (kept, record), rec = _select("on", client=_client(bad))
    assert kept == _det() and record["fallback"] is True
    rec.assert_called_once()
    qs = rec.call_args.kwargs["quality_signal"]
    assert qs["outcome"] == "unusable" and "no verdict" in qs["error"]


def test_timeout_falls_back(monkeypatch):
    monkeypatch.setattr(ff, "TIMEOUT_S", 0.05)

    def slow(*_a, **_k):
        import time
        time.sleep(0.3)
        return ff.FilterOutcome(kept=["never used?"])
    with patch.object(ff, "llm_filter_questions", side_effect=slow):
        kept, record = asyncio.run(ff.select_faq_questions(QUESTIONS, own_seed=SEED, facts_text=None,
                                                           out_of_scope=OUT_OF_SCOPE, context=CONTEXT, mode="on"))
    assert kept == _det() and record["fallback"] is True and "Timeout" in record["error"]


# ---------------------------------------------------------------- parsing + scope

@pytest.mark.parametrize("content,err", [
    ("no json here", "no JSON"),
    ('{"items": "x"}', "no items"),
    ('{"items": [{"n": 1, "label": "MAYBE"}]}', "bad item"),
    ('{"items": [{"n": 4, "label": "KEEP"}]}', "out of range"),
    ('{"items": [{"n": 1, "label": "KEEP"}]}', "no verdict"),
])
def test_parse_rejects_unusable_replies(content, err):
    with pytest.raises(ff.FaqFilterReplyError, match=err):
        ff.parse_verdicts(content, 2)


def test_parse_defaults_unknown_reason_and_confidence():
    out = ff.parse_verdicts('Sure: {"items": [{"n": 1, "label": "DROP", "reason_code": "nope", "confidence": "?"}]}',
                            1)
    assert out == [{"label": "DROP", "reason_code": "other", "confidence": "low", "note": ""}]


def test_scope_view_uses_scope_line_facts_and_caps():
    ctx = dict(CONTEXT, scope="SCOPE: this piece covers days 3-5 of the trip.")
    sc = ff.scope_from_context(ctx, SEED, "Season: June to August.")
    assert sc.scope_text == "SCOPE: this piece covers days 3-5 of the trip."
    assert sc.source_text.endswith("Season: June to August.")
    long = ff.scope_from_context(CONTEXT, SEED * 200)
    assert len(long.source_text) == ff.SOURCE_MAX


def test_normalize_is_what_paa_for_scope_used():
    assert qg.normalize_paa_question("  how deep is [R:a1] lake khovsgol. ") == "How deep is lake khovsgol?"
    assert qg.normalize_paa_question("Hi?") is None and qg.normalize_paa_question(None) is None


# ---------------------------------------------------------------- gateway row

def test_safe_default_and_migration_264_agree():
    cfg = SAFE_DEFAULTS["t9_faq_filter"]
    assert (cfg.role, cfg.provider, cfg.model_id, cfg.account_route) == ("validate", "claude", "haiku", "acc3")
    assert cfg.fallback_model_ids == () and cfg.shadow_model_id is None
    sql = (Path(__file__).resolve().parents[2] / "api" / "migrations" / "264_t9_faq_filter_stage.sql").read_text()
    assert ("('t9_faq_filter', 'validate', 'claude', 'haiku', 'acc3', true, ARRAY[]::text[], NULL, 0, "
            "'migration-264-aa779')") in sql
    assert "ON CONFLICT (stage) DO NOTHING" in sql and "VALUES ('264'" in sql


def test_writer_input_carries_the_filter_record_only_when_present():
    from services.acp_content_writing.service import writer_input_snapshot
    base = {"channel": "blog", "real_questions": [], "search_questions": []}
    assert "faq_filter" not in writer_input_snapshot(base)
    rec = {"mode": "shadow", "fallback": False, "verdicts": []}
    assert writer_input_snapshot(dict(base, faq_filter=rec))["faq_filter"] == rec
