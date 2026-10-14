"""AA-792 — T2 (tenant rewrite) writes in the tenant's brand voice; the judge gates on brand fit.

S234: the T2 writer reused the A1 master prompt (neutral catalogue voice, "never adopt a persona",
the tenant brand only as an "append only" note) and the itinerary nudge re-voiced days as Adventure
Asia. These tests pin the T2 builders; the A1 bytes are pinned by test_aa792_a1_prompt_golden.py.
All mock-only: no AWS, no DB, no LLM.
"""
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.content_generation import batch_prompt, graph, itinerary_utils
from shared.llm_client.models import LLMResponse

_TOUR = {
    "name": "Bhutan: Culinary Trail East to West — 10 Days", "country": "Bhutan", "duration": "10 Days",
    "summary": "A journey from Samdrup Jongkhar to Paro.", "highlights": ["Punakha Dzong"],
    "itineraries": "Day 1: Arrive in Samdrup Jongkhar.\nDay 2: Drive to Tashigang, visit Chorten Kora.",
}
_STYLE = "Use active verbs. Keep sentences short and dynamic. Do not use formal brochure language."
_GOOD = "Start the morning on a forest trail, spend the afternoon cycling between villages."
# The Trail Pulse T0 profile (S234, tenant_brand_rules v3), with system_prompt empty as in production.
_T2 = {
    "tour": _TOUR, "seo": {}, "few_shots": [], "subtitle_focus": "standard",
    "is_tenant_rewrite": True, "tenant_id": "692a3214-5cc2-46c6-a5ca-265389747269",
    "rewrite_language": "en-US",
    "brand_name": "Trail Pulse", "brand_type": "Young active adventure travel brand",
    "brand_system_prompt": "",
    "brand_style_guide": _STYLE,
    "brand_forbidden_words": ["conquer", "push your limits"],
    "brand_core_idea": "Movement, energy, freedom, and social discovery.",
    "brand_customer_segment": "Active travellers 25-40",
    "brand_customer_mindset": "Wants to feel alive and meet people.",
    "brand_voice_examples": ["Energetic", "Fresh", "Direct"],
    "brand_good_examples": _GOOD,
}


@pytest.fixture(autouse=True)
def _flags(monkeypatch):
    monkeypatch.delenv("S1_PER_DAY_TARGETS", raising=False)
    monkeypatch.delenv("S1_STRUCTURED_FACTS", raising=False)
    monkeypatch.delenv("T2_JUDGE_GATE", raising=False)


# ── writer system prompt ────────────────────────────────────────────────────────────────────

def test_t2_system_prompt_is_written_as_the_brand():
    sp = batch_prompt.build_s1_system_prompt(dict(_T2))
    assert "CLIENT BRAND (you write as this brand" in sp
    assert "Name: Trail Pulse" in sp and "Brand type: Young active adventure travel brand" in sp
    assert "Voice: Energetic, Fresh, Direct" in sp
    assert _STYLE in sp and _GOOD in sp
    assert "Never use these words or phrases: conquer, push your limits" in sp
    # the truth/structure rules every brand keeps
    assert "SOURCE FIDELITY" in sp and "HARD CONSTRAINT — FORBIDDEN WORDS" in sp
    assert "SEO META LENGTH" in sp and "CONTRAST REQUIREMENT" in sp
    # the A1 neutral-catalogue framing is gone
    for a1_only in ("never adopt a persona", "append only", "master content catalog",
                    "NEUTRALIZE PROMOTIONAL SOURCE COPY", "Condé Nast"):
        assert a1_only not in sp


def test_t2_core_forbidden_words_still_bind_the_tenant_voice():
    # Gate-1 default: the AA core list stays banned for tenants (validate enforces it too).
    sp = batch_prompt.build_s1_system_prompt(dict(_T2))
    assert "NEVER use these words: curated, pristine, refined" in sp
    hard = sp.split("HARD CONSTRAINT — FORBIDDEN WORDS")[1]
    assert "curated" in hard and "conquer" in hard


def test_t2_brand_system_prompt_is_a_brief_not_an_append():
    sp = batch_prompt.build_s1_system_prompt({**_T2, "brand_system_prompt": "Write for Trail Pulse riders."})
    assert "CLIENT BRAND BRIEF (the brand's own words — part of the brand above):\nWrite for Trail Pulse riders." in sp
    assert "append only" not in sp


def test_t2_without_any_brand_data_still_writes_without_the_a1_voice():
    sp = batch_prompt.build_s1_system_prompt({"tour": _TOUR, "seo": {}, "is_tenant_rewrite": True})
    assert "No brand identity is set up for this tenant yet" in sp
    assert "master content catalog" not in sp and "CONTRAST REQUIREMENT" not in sp


# ── writer user prompt ──────────────────────────────────────────────────────────────────────

def test_t2_user_prompt_asks_for_the_brand_voice_in_every_day():
    up = batch_prompt.build_s1_user_prompt(dict(_T2))
    assert up.startswith("Rewrite the following tour content as this client brand's own tour page")
    assert "NEUTRAL" not in up and "third-person editorial" not in up
    assert "master content catalog" not in up
    assert "The brand voice applies to EVERY day" in up
    assert "opening with a concrete editorial clause" not in up
    # the style guide lives in the system prompt's CLIENT BRAND block — not repeated here
    assert _STYLE not in up and "STYLE GUIDE FOR THIS CLIENT" not in up
    # unchanged blocks
    assert "PER-DAY SOURCE LENGTH" in up and "OUTPUT JSON FORMAT" in up


def test_sync_and_batch_build_the_same_t2_prompt():
    seen = []

    class _Client:
        def generate(self, request):
            seen.append(request)
            raise RuntimeError("stop")

    with patch.object(graph, "LLMClient", _Client):
        out = graph.generate_node(dict(_T2))
    system, user, _ = batch_prompt.materialize_s1_prompt(dict(_T2))
    assert seen[0].system_prompt == system and seen[0].user_prompt == user
    assert seen[0].stage == "s1_generate"  # stage comes from generate_stage, unchanged here
    assert out["prompt_version"] == batch_prompt.prompt_version_of(system)


def test_t2_brand_pack_without_system_prompt_counts_as_branded():
    # "has brand": the T0 pack (system_prompt empty) is a brand — the unbranded warning must not fire.
    class _Client:
        def generate(self, request):
            raise RuntimeError("stop")

    with patch.object(graph, "LLMClient", _Client), patch.object(graph, "logger") as log:
        out = graph.generate_node(dict(_T2))
    assert out["is_branded"] is True
    assert not any(c.args and c.args[0] == "unbranded_generation" for c in log.warning.call_args_list)


# ── itinerary nudge ─────────────────────────────────────────────────────────────────────────

def _nudge_prompt(voice_hint):
    client = MagicMock()
    client.generate.return_value = LLMResponse(content='{"title": "t", "body": "b"}', model_used="m",
                                               provider="bedrock")
    itinerary_utils.nudge_itinerary_day(client, "Ride to Tashigang.", "Tashigang", "Short.", 40,
                                        voice_hint=voice_hint)
    return client.generate.call_args.args[0].system_prompt


def test_t2_nudge_keeps_the_draft_brand_voice():
    hint = itinerary_utils.nudge_voice_hint(_T2)
    assert hint == "Energetic, Fresh, Direct"
    sp = _nudge_prompt(hint)
    assert "keeping the CURRENT DRAFT's brand voice (Energetic, Fresh, Direct)" in sp
    assert "calm, factual, editorial" not in sp and "Adventure Asia" not in sp
    assert "NEVER invent meal names" in sp


def test_a1_nudge_prompt_unchanged_and_no_hint_for_a1():
    assert itinerary_utils.nudge_voice_hint({"brand_voice_examples": ["calm"]}) is None
    assert _nudge_prompt(None) == itinerary_utils._ITINERARY_NUDGE_SYSTEM_PROMPT
    assert "calm, factual, editorial" in itinerary_utils._ITINERARY_NUDGE_SYSTEM_PROMPT


def test_t2_nudge_hint_falls_back_without_voice_words():
    assert itinerary_utils.nudge_voice_hint({"is_tenant_rewrite": True}) == "this brand's voice"


# ── brand load (t2_rewrite_job._prepare) ────────────────────────────────────────────────────

class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False
        return _Ctx()


_PT = {"aa_name": "Bhutan Trail", "country": "Bhutan", "duration": "10 Days"}


@pytest.mark.asyncio
async def test_prepare_loads_the_brand_through_the_brand_pack():
    from services.acp_shared import brand_pack
    from services.jobs import t2_rewrite_job as t2
    conn = AsyncMock()
    conn.fetchrow.return_value = {
        "brand_name": "Trail Pulse", "brand_type": "Young active adventure travel brand",
        "core_idea": "Movement.", "customer_segment": "25-40", "customer_mindset": "Feel alive.",
        "voice_examples": '["Energetic", "Fresh"]', "style_guide": _STYLE,
        "forbidden_words": '["conquer"]', "good_examples": _GOOD, "default_cta": "Ride with us",
        "system_prompt": None, "version": 3,
    }
    _, rules = await t2._prepare(_Pool(conn), _PT, "t-1", "en-GB")
    # one loader: the AA-771 pack query ('default' row first, else newest active)
    assert conn.fetchrow.await_args.args[0] == brand_pack._PACK_QUERY
    assert rules["_brand_loaded"] is True and rules["rewrite_language"] == "en-GB"
    assert rules["brand_name"] == "Trail Pulse" and rules["default_cta"] == "Ride with us"
    assert rules["voice_examples"] == ["Energetic", "Fresh"] and rules["forbidden_words"] == ["conquer"]
    assert rules["system_prompt"] == "" and rules["style_guide"] == _STYLE


@pytest.mark.asyncio
async def test_prepare_still_raises_on_a_db_error():
    from services.jobs import t2_rewrite_job as t2
    conn = AsyncMock()
    conn.fetchrow.side_effect = RuntimeError("pool exhausted")
    with pytest.raises(RuntimeError):
        await t2._prepare(_Pool(conn), _PT, "t-1", "en-US")


@pytest.mark.asyncio
async def test_rewrite_tour_threads_the_brand_identity_into_graph_state():
    from api.routers import v1_pipeline
    seen = {}

    class FakeGraph:
        async def astream(self, state, stream_mode):
            seen.update(state)
            yield {"generate": {**state, "generated": {"name": "x"}}}

    with patch.object(v1_pipeline, "build_graph", return_value=FakeGraph()):
        await v1_pipeline._rewrite_tour({"name": "T"}, idx=0, total=1, is_tenant_rewrite=True,
                                        brand_rules={"brand_name": "Trail Pulse", "brand_type": "active",
                                                     "default_cta": "Ride"})
    assert seen["brand_name"] == "Trail Pulse" and seen["brand_type"] == "active"
    assert seen["brand_default_cta"] == "Ride"
    assert {"brand_name", "brand_type", "brand_default_cta"} <= set(graph.ContentState.__annotations__)


# ── brand audit fallback (judge did not run) ────────────────────────────────────────────────

def _audit_resp():
    return LLMResponse(content=json.dumps({"brand_audit": {
        "status": "pass", "publish_ready": True, "fields_to_fix": [], "failure_codes": [], "issues": [],
        "scores": {"brand_fit": 1, "human_read": 1, "seo_fit": 1, "trip_type_accuracy": 1,
                   "publish_readiness": 1},
        "notes": "", "lessons_extracted": []}}), model_used="m", provider="bedrock")


def test_t2_audit_fallback_uses_the_tenant_rubric_not_adventure_asia():
    from services.content_generation import brand_audit_node as ba
    state = {**_T2, "generated": {"name": "Bhutan Trail Ride", "summary": "S", "seo_meta": "M", "itineraries": ""}}
    with patch.object(ba, "LLMClient") as cls, patch.object(ba, "record_call_sync"):
        cls.return_value.generate.return_value = _audit_resp()
        out = ba.brand_audit_node(state)
    req = cls.return_value.generate.call_args.args[0]
    assert "BRAND IDENTITY — Trail Pulse" in req.system_prompt and _STYLE in req.system_prompt
    assert "ADVENTURE ASIA BRAND IDENTITY" not in req.system_prompt
    assert "Review this tour against THIS client brand's standards." in req.user_prompt
    assert "calm, assured, selective" not in req.user_prompt
    assert out["brand_audit_status"] == "pass"


def test_t2_audit_fallback_without_a_brand_makes_no_llm_call():
    from services.content_generation import brand_audit_node as ba
    state = {"is_tenant_rewrite": True, "tour": _TOUR,
             "generated": {"name": "Bhutan Trail Ride", "summary": "S", "seo_meta": "Discover Bhutan.",
                           "itineraries": ""}}
    with patch.object(ba, "LLMClient") as cls:
        out = ba.brand_audit_node(state)
    cls.assert_not_called()
    assert out["brand_audit_codes"] == ["META_OPENER_ROBOTIC"] and out["brand_audit_status"] == "flagged"


def test_a1_audit_fallback_still_uses_adventure_asia_standards():
    from services.content_generation import brand_audit_node as ba
    state = {"tour": _TOUR,
             "generated": {"name": "Bhutan Trail Ride", "summary": "S", "seo_meta": "M", "itineraries": ""}}
    with patch.object(ba, "LLMClient") as cls, patch.object(ba, "record_call_sync"):
        cls.return_value.generate.return_value = _audit_resp()
        ba.brand_audit_node(state)
    req = cls.return_value.generate.call_args.args[0]
    assert req.system_prompt.startswith(ba.AA_BRAND_IDENTITY_PROMPT)
    assert "Review this tour against Adventure Asia brand standards." in req.user_prompt
    assert "2. Brand fit: calm, assured, selective, precise, private, no hype?" in req.user_prompt
