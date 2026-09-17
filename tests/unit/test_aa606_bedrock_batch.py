"""AA-606: Bedrock Batch Inference for S1 rewrite — pure-unit coverage.

Covers the parts with no AWS/DB/LLM dependency:
  - manifest JSONL assembly + recordId=tour_id contract + dedup guard
  - modelInput body shape (matches invoke_claude's InvokeModel body)
  - MIN_BATCH_RECORDS enforcement signal
  - parse_generated_json shared by generate_node (sync) and seed_generated_node (batch)
  - the extracted prompt builder (batch_prompt) producing the same system prompt the sync
    generate_node used to build inline — including the brand-diff block still resolving via
    the graph re-export (no behavioural drift from the refactor).

The submit/poll/read S3+Bedrock calls are integration-level (need AWS) and are intentionally not
unit-tested here; they're exercised live during the first real batch run.
"""

import json

import pytest

from shared.llm_client import bedrock_batch
from shared.llm_client.bedrock_batch import (
    BatchRecordResult,
    BatchUnavailable,
    build_manifest_jsonl,
    build_model_input,
)
from services.content_generation.batch_prompt import (
    build_s1_system_prompt,
    build_s1_user_prompt,
    materialize_s1_prompt,
    prompt_version_of,
)
from services.content_generation.graph import parse_generated_json, _build_brand_diff_block


# ── manifest ──────────────────────────────────────────────────────────────────

def test_manifest_one_record_per_tour_recordid_is_tour_id():
    recs = [
        ("11111111-1111-1111-1111-111111111111", {"anthropic_version": "bedrock-2023-05-31"}),
        ("22222222-2222-2222-2222-222222222222", {"anthropic_version": "bedrock-2023-05-31"}),
    ]
    jsonl = build_manifest_jsonl(recs)
    lines = [l for l in jsonl.splitlines() if l.strip()]
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["recordId"] == "11111111-1111-1111-1111-111111111111"
    assert first["modelInput"]["anthropic_version"] == "bedrock-2023-05-31"


def test_manifest_rejects_duplicate_record_id():
    recs = [("dup", {"x": 1}), ("dup", {"x": 2})]
    with pytest.raises(BatchUnavailable):
        build_manifest_jsonl(recs)


def test_manifest_trailing_newline():
    jsonl = build_manifest_jsonl([("t1", {"a": 1})])
    assert jsonl.endswith("\n")


# ── modelInput body ─────────────────────────────────────────────────────────────

def test_model_input_shape_matches_invoke_body():
    body = build_model_input("SYS", "USER", max_tokens=1234)
    assert body["anthropic_version"] == bedrock_batch.ANTHROPIC_VERSION
    assert body["max_tokens"] == 1234
    assert body["messages"] == [{"role": "user", "content": "USER"}]
    # system present → cached content-block form (list), never a bare string
    assert isinstance(body["system"], list)


def test_model_input_omits_system_when_empty():
    body = build_model_input("", "USER")
    assert "system" not in body


# ── min-batch signal ────────────────────────────────────────────────────────────

def test_min_batch_records_constant_is_positive():
    # The real Bedrock minimum for Claude batch jobs must be confirmed live before first prod run;
    # this just guards against the constant being accidentally zeroed/removed.
    assert bedrock_batch.MIN_BATCH_RECORDS >= 1


# ── parse_generated_json (shared sync + batch) ───────────────────────────────────

def test_parse_generated_plain_json():
    got = parse_generated_json('{"name": "Sri Lanka by Rail — 10 Days", "summary": "x"}')
    assert got["name"].startswith("Sri Lanka")


def test_parse_generated_strips_markdown_fence():
    raw = '```json\n{"name": "Korea", "summary": "y"}\n```'
    got = parse_generated_json(raw)
    assert got["name"] == "Korea"


def test_parse_generated_salvages_trailing_comma():
    # json-repair (AA-217) recovers a dict with a name from a mildly malformed blob.
    raw = '{"name": "Nepal Trek", "highlights": ["a", "b",],}'
    got = parse_generated_json(raw)
    assert got is not None
    assert got["name"] == "Nepal Trek"


def test_parse_generated_returns_none_on_unrecoverable():
    assert parse_generated_json("not json at all, no braces") is None


def test_parse_generated_valid_json_without_name_is_returned_as_is():
    # VALID json parses on the first try (no salvage) — returned as-is even without a name, exactly
    # like generate_node's inline parser. The name-guard only applies to the json-repair SALVAGE
    # branch (malformed input), so this must NOT be None.
    assert parse_generated_json('{"summary": "no name here"}') == {"summary": "no name here"}


def test_parse_generated_salvage_without_name_returns_none():
    # MALFORMED input whose salvage yields a dict with no name → the salvage guard rejects it (None),
    # matching the sync path's own "json_parse_failed" condition.
    assert parse_generated_json('{"summary": "no name here",,,}') is None


# ── prompt builder (extracted, byte-identical to sync generate_node) ─────────────

_BRANDED_STATE = {
    "tour": {
        "name": "Vietnam Highlands", "country": "Vietnam", "duration": "8 Days",
        "summary": "s", "description": "d", "highlights": ["Sapa trek"],
        "itineraries": "Day 1 — Arrive Hanoi\nCity walk.", "inclusions": "", "exclusions": "",
    },
    "seo": {"keywords": {"top_keywords": ["vietnam trekking"]}, "people_also_ask": []},
    "few_shots": [],
    "subtitle_focus": "standard",
    "rewrite_language": "en-US",
    "brand_system_prompt": "Boutique adventure operator.",
    "brand_style_guide": "Warm, specific.",
    "brand_forbidden_words": ["cheap", "luxury"],
    "brand_core_idea": "Slow immersive highland travel",
    "brand_customer_mindset": "Wants authentic homestays",
    "brand_voice_examples": ["grounded", "curious"],
    "brand_good_examples": "",
    "brand_customer_segment": "",
}


def test_system_prompt_includes_brand_and_forbidden_and_diff():
    system = build_s1_system_prompt(_BRANDED_STATE)
    assert "American English" in system
    assert "Boutique adventure operator." in system
    assert "FORBIDDEN WORDS (never use): cheap, luxury" in system
    assert "BRAND DIFFERENTIATION PROFILE" in system
    assert "Slow immersive highland travel" in system


def test_system_prompt_british_english_branch():
    st = dict(_BRANDED_STATE, rewrite_language="en-GB")
    assert "British English" in build_s1_system_prompt(st)


def test_user_prompt_has_tour_data_and_style_guide():
    user = build_s1_user_prompt(_BRANDED_STATE)
    assert "Vietnam Highlands" in user
    assert "STYLE GUIDE FOR THIS CLIENT:" in user
    assert "Warm, specific." in user


def test_materialize_returns_stable_prompt_version():
    system, user, pv = materialize_s1_prompt(_BRANDED_STATE)
    assert len(pv) == 8
    assert pv == prompt_version_of(system)
    # deterministic for the same inputs
    assert materialize_s1_prompt(_BRANDED_STATE)[2] == pv


def test_brand_diff_block_reexport_unchanged():
    # graph._build_brand_diff_block now delegates to batch_prompt.build_brand_diff_block — verify
    # the graph-level name still works (existing importers/tests) and produces the profile.
    block = _build_brand_diff_block(_BRANDED_STATE)
    assert "BRAND DIFFERENTIATION PROFILE" in block
    assert "Slow immersive highland travel" in block


def test_legacy_brand_no_diff_block_in_system():
    legacy = {
        "tour": _BRANDED_STATE["tour"], "seo": {}, "few_shots": [],
        "subtitle_focus": "standard", "rewrite_language": "en-US",
        "brand_system_prompt": "", "brand_style_guide": "", "brand_forbidden_words": [],
    }
    system = build_s1_system_prompt(legacy)
    assert "BRAND DIFFERENTIATION PROFILE" not in system


# ── BatchRecordResult ────────────────────────────────────────────────────────────

def test_batch_record_ok_flag():
    ok = BatchRecordResult(tour_id="t1", text='{"name":"x"}')
    assert ok.ok is True
    err = BatchRecordResult(tour_id="t2", error="throttled")
    assert err.ok is False
    empty = BatchRecordResult(tour_id="t3", text="")
    assert empty.ok is False
