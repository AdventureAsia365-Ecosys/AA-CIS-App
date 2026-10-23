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
    assert "SOURCE FIDELITY" in sp and "HARD CONSTRAINT: FORBIDDEN WORDS" in sp  # AA-778: no em-dash in T2
    assert "SEO META LENGTH" in sp and "CONTRAST REQUIREMENT" in sp
    # the A1 neutral-catalogue framing is gone
    for a1_only in ("never adopt a persona", "append only", "master content catalog",
                    "NEUTRALIZE PROMOTIONAL SOURCE COPY", "Condé Nast"):
        assert a1_only not in sp


def test_t2_core_forbidden_words_still_bind_the_tenant_voice():
    # AA-800: rule 1 is built from the word layers. Trail Pulse declares no positioning word, so the AA
    # positioning words and platform truth stay NEVER; stock superlatives are capped at one.
    sp = batch_prompt.build_s1_system_prompt(dict(_T2))
    assert ("NEVER use these words: curated, refined, tailored, bespoke, luxury, cheap, deal, book now, "
            "instant booking, discount") in sp
    assert "Stock superlatives (pristine, stunning, breathtaking, magical, paradise): at most one" in sp
    assert "signature words" not in sp
    hard = sp.split("HARD CONSTRAINT: FORBIDDEN WORDS")[1]
    assert "curated" in hard and "conquer" in hard and "discount" in hard
    assert "stunning" not in hard and "luxury" not in hard  # capped / prompt-only, never auto-rejected


def test_t2_brand_system_prompt_is_a_brief_not_an_append():
    sp = batch_prompt.build_s1_system_prompt({**_T2, "brand_system_prompt": "Write for Trail Pulse riders."})
    assert "CLIENT BRAND BRIEF (the brand's own words, part of the brand above):\nWrite for Trail Pulse riders." in sp
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


# ── pilot fix: the core idea is the lens for the trip (same definition as the judge's mission) ─

def test_t2_system_prompt_asks_the_core_idea_to_frame_overview_and_days():
    sp = batch_prompt.build_s1_system_prompt(dict(_T2))
    assert "CORE IDEA AS THE LENS FOR THE WHOLE TRIP" in sp
    lens = sp.split("CORE IDEA AS THE LENS FOR THE WHOLE TRIP")[1]
    assert '("Movement, energy, freedom, and social discovery.")' in lens
    assert "The overview and each itinerary day's framing" in lens
    assert "using only facts from the source" in lens
    assert "This is not brand self-reference" in lens and "no invented claims" in lens
    # the lens sits after the CLIENT BRAND block (whose self-reference limit it clarifies)
    assert sp.index("Brand self-reference:") < sp.index("CORE IDEA AS THE LENS")


@pytest.mark.parametrize("core_idea", ["", "   ", None])
def test_t2_without_core_idea_gets_no_lens_rule(core_idea):
    sp = batch_prompt.build_s1_system_prompt({**_T2, "brand_core_idea": core_idea})
    assert "CORE IDEA AS THE LENS" not in sp


def test_a1_system_prompt_never_gets_the_lens_rule():
    sp = batch_prompt.build_s1_system_prompt({**_T2, "is_tenant_rewrite": False})
    assert "CORE IDEA AS THE LENS" not in sp


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
    assert "BRAND IDENTITY: Trail Pulse" in req.system_prompt and _STYLE in req.system_prompt
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


# ── judge: T2 gates on brand fit + the TENANT's own mission (Nghiệp option B, 14/10/2026) ──────

from services.content_generation import brand_fit, judge_node as jn  # noqa: E402

_GEN = {"name": "N", "subtitle": "S", "summary": "Sum", "highlights": ["a", "b"],
        "itineraries": [{"day": 1, "title": "T", "body": "B"}], "seo_title": "st", "seo_meta": "sm"}


def _judge_resp(fit=8, distinct=3, mission=False):
    return LLMResponse(content=json.dumps({"brand_fit_score": fit, "cross_brand_distinct": distinct,
                                           "mission_present": mission, "feedback": "x"}),
                       model_used="gpt-4.1", cost_usd=0.001, provider="openai")


def _run_judge(state, resp):
    with patch.object(brand_fit, "LLMClient") as client, patch.object(jn, "record_call_sync") as rec:
        client.return_value.generate.return_value = resp
        out = jn.judge_node(dict(state, generated=_GEN, quality_score=9.75, feedback=""))
    return out, client.return_value.generate.call_args.args[0], rec.call_args.kwargs["quality_signal"]


def test_t2_default_gate_is_brand_fit_mission():
    assert jn.t2_judge_gate() == "brand_fit_mission"


def test_t2_judge_names_the_tenant_core_idea_as_the_mission():
    _, request, _ = _run_judge(_T2, _judge_resp(mission=True))
    assert 'its core idea: "Movement, energy, freedom, and social discovery."' in request.user_prompt
    assert "never Adventure Asia's" in request.user_prompt


def test_t2_judge_mission_axis_uses_the_writer_lens_definition():
    _, request, _ = _run_judge(_T2, _judge_resp(mission=True))
    up = request.user_prompt
    assert "clearly shape how the trip is framed" in up
    assert "the overview AND most itinerary days reflect it in" in up
    assert "it does not have to be quoted or named, and not every single day must carry" in up
    assert "never Adventure Asia's or a generic travel mission" in up
    # the old "each day's framing" wording made the judge demand the idea in every single day
    assert "(each day's framing), not only in the summary?" not in up


def test_a1_mission_axis_text_unchanged():
    assert brand_fit._MISSION_AXIS == (
        "- mission_present (true/false): does this brand's mission-hook actually surface in the ITINERARIES\n"
        "  (each day's framing), not only in the summary?")


def test_t2_mission_present_is_not_capped():
    out, _, signal = _run_judge(_T2, _judge_resp(fit=8, mission=True))
    assert out["judge_score"] == 8.0 and out["quality_score"] == 8.0
    assert signal["gate"] == "brand_fit_mission" and signal["mission_source"] == "tenant_core_idea"
    assert signal["mission_gated"] is True


def test_t2_mission_absent_caps_below_the_retry_line():
    out, _, signal = _run_judge(_T2, _judge_resp(fit=8, mission=False))
    assert out["judge_score"] == 6.0 and out["quality_score"] == 6.0
    assert signal["mission_present"] is False and signal["mission_source"] == "tenant_core_idea"


@pytest.mark.parametrize("gate,expected", [("brand_fit_mission", 8.0), ("combined", 3.0), ("brand_fit", 8.0)])
def test_t2_tenant_without_core_idea_is_never_mission_capped(monkeypatch, gate, expected):
    monkeypatch.setenv("T2_JUDGE_GATE", gate)
    state = dict(_T2, brand_core_idea="")             # voice words still make the judge run
    with patch.object(jn.logger, "info") as log:
        out, request, signal = _run_judge(state, _judge_resp(fit=8, distinct=3, mission=False))
    assert out["judge_score"] == expected               # combined = min(8, 3), no 6.0 mission cap
    assert signal["mission_source"] == "none" and signal["mission_gated"] is False
    assert "its core idea:" not in request.user_prompt
    if gate != "brand_fit":
        assert any(c.args[0] == "judge_mission_not_gated" for c in log.call_args_list)


_A1_JUDGE_PROFILE = {"brand_core_idea": "Slow travel for families", "brand_customer_segment": "Families",
                     "brand_customer_mindset": "calm", "brand_voice_examples": ["warm", "plain"],
                     "brand_good_examples": "We wander."}


def test_judge_prompt_without_a_mission_is_byte_identical_to_pre_aa792():
    # SHAs captured from brand_fit._build_judge_prompt on this branch before the mission kwarg.
    import hashlib
    sha = lambda s: hashlib.sha256(s.encode()).hexdigest()  # noqa: E731
    assert sha(brand_fit._build_judge_prompt(_A1_JUDGE_PROFILE, _GEN)) == \
        "8ea22c220b19de1ac626e8813e4611e376130ea487e186b9c6c7a2a10262c1b1"
    assert sha(brand_fit._build_judge_prompt({}, {})) == \
        "732475795e6ea7ff0f4d4e169e2a8adbd1c2a4f95ccbead7fc0f630910875f74"


def test_a1_judge_call_and_signal_unchanged():
    state = dict(_A1_JUDGE_PROFILE, is_tenant_rewrite=False)
    with patch.object(jn, "score_brand_fit") as score, patch.object(jn, "record_call_sync") as rec:
        score.return_value = MagicMock(brand_fit_score=8.0, cross_brand_distinct=3.0, mission_present=False,
                                       judge_score=3.0, feedback="", model_used="m", input_tokens=1,
                                       output_tokens=1, cost_usd=0.0)
        out = jn.judge_node(dict(state, generated=_GEN, quality_score=9.75, feedback=""))
    assert "mission" not in score.call_args.kwargs     # A1 never passes a mission
    assert out["judge_score"] == 8.0
    assert set(rec.call_args.kwargs["quality_signal"]) == {
        "judge_score", "brand_fit_score", "cross_brand_distinct", "mission_present", "passed", "gate"}


# ── flag_fix: T2 repairs are framed with the tenant brand, A1 keeps Adventure Asia ─────────────

from services.content_generation import flag_fix_node as ff  # noqa: E402


def _fix_state(**kw):
    return dict(kw, brand_audit_status="flagged", brand_audit_codes=["SUMMARY_OFF_BRAND"],
                brand_audit_fields=["summary"], brand_audit_issues=["Summary reads generic."],
                failure_codes=[], generated={"name": "N", "summary": "Old summary."}, tour=_TOUR)


def _run_fix(state):
    resp = MagicMock(content=json.dumps({"summary": "New summary."}), cost_usd=0.0)
    with patch.object(ff, "LLMClient") as client, patch.object(ff, "record_call_sync"), \
            patch.object(ff, "_write_lessons_safe"):
        client.return_value.generate.return_value = resp
        out = ff.flag_fix_node(state)
    return out, client.return_value.generate.call_args.args[0]


def test_t2_flag_fix_prompt_is_framed_with_the_tenant_brand():
    out, request = _run_fix(_fix_state(**_T2))
    assert out["fix_pass_applied"] is True
    assert "Adventure Asia" not in request.system_prompt and "Adventure Asia" not in request.user_prompt
    assert request.system_prompt.startswith("You are the editorial fixer for Trail Pulse")
    assert "CLIENT BRAND (you write as this brand" in request.system_prompt
    assert "Core idea: Movement, energy, freedom, and social discovery." in request.system_prompt
    assert request.user_prompt.startswith("Fix these fields for Trail Pulse's brand standards")


def test_t2_flag_fix_without_a_brand_name_stays_tenant_neutral():
    state = dict(_T2, brand_name="")
    assert ff.fix_system_prompt(state).startswith("You are the editorial fixer for this client brand")
    assert "Adventure Asia" not in ff.fix_system_prompt(state) + ff.fix_user_prompt_head(state)


def test_a1_flag_fix_prompt_unchanged():
    _, request = _run_fix(_fix_state(is_tenant_rewrite=False))
    assert request.system_prompt == ff.FIX_SYSTEM
    # AA-645: the A1 fixer is neutral and white-label (no platform name on a page agencies resell).
    assert ff.FIX_SYSTEM.startswith("You are the editorial fixer of a neutral, white-label tour catalogue")
    assert "Adventure Asia" not in ff.FIX_SYSTEM
    assert request.user_prompt.startswith(
        "Fix these fields for the neutral white-label catalogue (no brand or operator names).\n\nFIELDS TO FIX:")


def test_t2_meta_rerepair_uses_the_tenant_fix_system():
    resp = MagicMock(content=json.dumps({"seo_meta": "x"}), cost_usd=0.0)
    with patch.object(ff, "LLMClient") as client, patch.object(ff, "record_call_sync"):
        client.return_value.generate.return_value = resp
        ff._rerepair_meta("short.", _TOUR, {"name": "N"}, None, system_prompt=ff.fix_system_prompt(_T2))
    assert "Trail Pulse" in client.return_value.generate.call_args.args[0].system_prompt
