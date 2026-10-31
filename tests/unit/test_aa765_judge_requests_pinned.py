"""AA-765 — the judge consolidation (one judge client in shared/llm_client/judge.py) is a pure refactor.

Every judge path (S1/T2 brand fit v1 and v2, the anchored T2 prompt, the T7 Debate candidate fit, the
adhoc model override, the T10 F9 judge) is driven with fixed inputs and the LLMRequest it hands the gateway is
pinned by a SHA-256 of `model_dump()` + `model_fields_set` (the gateway reads `model_fields_set` to decide
whether temperature / seed are forwarded, so the set is part of the request). The pins were recorded on
main before the refactor (717c58f1): a changed prompt, temperature, seed, max_tokens, stage or a field that
starts or stops being "set" fails here.

The gateway is patched on the LLMClient class itself, so the capture does not depend on which module holds
the call.
"""
from __future__ import annotations

import hashlib
import json
from unittest.mock import patch

import pytest

from shared.llm_client.client import LLMClient
from shared.llm_client.models import LLMResponse

PROFILE = {
    "brand_name": "River & Ridge",
    "brand_core_idea": "Slow journeys that leave room for the people who live along the route.",
    "brand_customer_segment": "Couples in their 40s who have travelled Asia before",
    "brand_customer_mindset": "Wants depth over coverage; avoids crowds",
    "brand_voice_examples": ["We walk the last kilometre.", "Tea first, photos later."],
    "brand_differentiation": "Every day has one unhurried local encounter.",
}
GENERATED = {
    "name": "Luang Prabang Slow Days — 3 Days",
    "subtitle": "Alms at dawn, a weaving village and the Kuang Si falls without the crowds.",
    "summary": "Three unhurried days around Luang Prabang with a weaver's family and the falls at opening time.",
    "highlights": ["Ban Xang Khong weavers", "Kuang Si at opening time", "Mount Phousi at dusk"],
    "itineraries": json.dumps([
        {"day": 1, "title": "Arrival and Mount Phousi", "description": "Arrive, settle in, climb Phousi at dusk."},
        {"day": 2, "title": "Weaving village", "description": "A morning with a weaving family in Ban Xang Khong."},
        {"day": 3, "title": "Kuang Si", "description": "Reach the falls at opening time, swim, return for lunch."},
    ]),
    "seo_title": "Luang Prabang Slow Days — 3 Days",
    "seo_meta": "Three unhurried days in Luang Prabang: weavers, Kuang Si at opening time and Phousi at dusk.",
}
SOURCE = {
    "name": "Luang Prabang 3 Days",
    "summary": "Three days in Luang Prabang.",
    "description": "Group size: 2-10",
    "highlights": ["Kuang Si", "Mount Phousi"],
    "itineraries": "Day 1\nArrive. Mount Phousi.\nDay 2\nBan Xang Khong weaving village.\nDay 3\nKuang Si falls.",
    "country": "Laos", "duration": "3 days", "inclusions": "Guide, entrance fees", "exclusions": "Tips",
}
CANDIDATE = {"place": "Ban Xang Khong", "action": "spend a morning with a weaving family",
             "evidence": ["Day 2: a morning with a weaving family in Ban Xang Khong."]}

# Recorded on main @ 717c58f1 (before AA-765) with tests/unit/test_aa765_judge_requests_pinned.py::_digest.
PINNED = {
    "s1_v1": "23d08ee466b44367f300fb0e266d8bf030059682fa829769b01bfc20ff2f750e",
    "t2_mission_anchors": "fb269f3097b50d943d98a2261366d05feeea0761f4f84674406641f6362b5c1b",
    "s1_v2": "0fccb8f02d66fa4b2d3bdbe08cb572657335a09bb393864673394ebe9857f387",
    "t7_candidate": "a303c4881d97cb7c419f615b79be07ea4f30cef513b6a69bfdf1707c31f1a374",
    "adhoc_override": "e86c1d544a010d331ec855ecc0730e8fce2807aae28d124f7dd3b326ea2aaf73",
    "t10_f9": "cbf8a39bee9bcc90b889cbebb1d7a720de5786956fa0e0f34d9e99cb905c9478",
}


def _digest(req) -> str:
    dump = req.model_dump()
    # AA-1018 part 2: LLMRequest.role (the ledger role of an adhoc stage) never reaches the provider; unset, it is left
    # out so the pins keep meaning "the provider request did not change". A judge that sets it changes its pin.
    if dump.get("role") is None:
        dump.pop("role", None)
    body = {"dump": dump, "set": sorted(req.model_fields_set)}
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _capture(content: str):
    seen: list = []

    def fake_generate(self, request):
        seen.append(request)
        return LLMResponse(content=content, model_used="satellite-gpt-5.6-luna", provider="openai",
                           input_tokens=10, output_tokens=5, cost_usd=0.001, satellite_account="acc3")

    patches = (patch.object(LLMClient, "__init__", lambda self: None),
               patch.object(LLMClient, "generate", fake_generate))
    return seen, patches


def _run(fn, content: str):
    seen, (p_init, p_gen) = _capture(content)
    with p_init, p_gen:
        fn()
    assert len(seen) == 1, f"expected one judge call, got {len(seen)}"
    return seen[0]


_V1_REPLY = json.dumps({"brand_fit_score": 9, "cross_brand_distinct": 9, "mission_present": True,
                        "mission_evidence": "A morning with a weaving family in Ban Xang Khong.",
                        "feedback": "on brand"})
_V2_REPLY = json.dumps({"unsupported_specifics": [], "brand_fit_score": 9, "cross_brand_distinct": 9,
                        "feedback": "close to the source"})


def _requests() -> dict:
    from services.acp_content_writing import quality_gates as qg
    from services.content_generation import brand_fit as bf

    out = {}
    out["s1_v1"] = _run(lambda: bf.score_brand_fit(PROFILE, GENERATED), _V1_REPLY)
    out["t2_mission_anchors"] = _run(
        lambda: bf.score_brand_fit(PROFILE, GENERATED, stage="t2_judge", mission=PROFILE["brand_core_idea"],
                                   anchors=True), _V1_REPLY)
    out["s1_v2"] = _run(lambda: bf.score_brand_fit({}, GENERATED, rubric="v2", source=SOURCE), _V2_REPLY)
    out["t7_candidate"] = _run(lambda: bf.score_candidate_fit(PROFILE, CANDIDATE, stage="t7_debate_judge"),
                               _V1_REPLY)
    out["adhoc_override"] = _run(
        lambda: bf.score_brand_fit(PROFILE, GENERATED, stage="adhoc_aa765", model_override="gpt-5.6-luna"),
        _V1_REPLY)
    with patch.object(qg, "_log_t10_judge_call"), patch.object(qg, "_log_t10_billed_unusable"):
        out["t10_f9"] = _run(lambda: qg.gate_brand_voice(
            content_text="Spend a morning with a weaving family in Ban Xang Khong. Ask us about dates.",
            cta="Ask us about dates", brand_rubric_text="Warm, unhurried, specific.",
            source_text="Day 2: Ban Xang Khong weaving village.", voice=["warm", "unhurried"],
            seo_title="Luang Prabang weavers", meta_description="A morning with weavers."), "{}")
    return out


@pytest.fixture(scope="module")
def requests_by_path():
    with patch("services.content_generation.brand_fit._seeded_model", return_value=True):
        return _requests()


@pytest.mark.parametrize("path", sorted(PINNED))
def test_judge_request_is_byte_identical(requests_by_path, path):
    assert _digest(requests_by_path[path]) == PINNED[path]


def test_judge_sampling_fields_set_per_path(requests_by_path):
    """Readable form of the part of the pin the gateway branches on."""
    r = requests_by_path
    for key in ("s1_v1", "t2_mission_anchors", "s1_v2", "t7_candidate"):
        assert r[key].model_fields_set == {"system_prompt", "user_prompt", "stage", "temperature", "seed"}
        assert (r[key].temperature, r[key].seed, r[key].max_tokens, r[key].model_tier) == (0.1, 42, 4096, None)
    assert r["adhoc_override"].model_fields_set == {"system_prompt", "user_prompt", "stage", "temperature",
                                                    "seed", "model_tier"}
    assert r["adhoc_override"].model_tier == "gpt-5.6-luna"
    assert r["t10_f9"].model_fields_set == {"system_prompt", "user_prompt", "stage", "temperature", "max_tokens"}
    assert (r["t10_f9"].stage, r["t10_f9"].temperature, r["t10_f9"].max_tokens, r["t10_f9"].seed) == (
        "t10_judge", 0, 8192, None)
