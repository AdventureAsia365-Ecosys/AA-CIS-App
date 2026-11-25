"""AA-1167 (OWASP LLM01) — the CI guard of the data-not-instructions fence in the writer prompts (S1 v2, T2, T9).

1. Canary renders. Every untrusted input of a writer prompt builder carries its own invented marker word; with the
   fence on, no marker may be left in the prompt outside an <untrusted_data> block (prompt_fence.outside_fences), and
   the system prompt carries prompt_fence.SYSTEM_RULE. A builder that injects such a field without the fence fails.
2. Classification. Every parameter of a builder and every tour field its source reads is either fed a marker here
   (untrusted) or listed as trusted with the reason. A new parameter or tour field fails until it is classified, so a
   new untrusted input cannot slip in unfenced.
3. Off = main. With the fence off (the setting's seed default) every builder renders exactly what it rendered before
   the fence existed (the fence=False default), and the live states carry the off mode.
4. Live wiring. The live callers read the setting cis.llm.prompt_data_fence: the S1 v2 writer_prompts, the T2 / A1
   graph state (v1_pipeline.initial_state_for) and the T9 write and rewrite calls.
Invented text only."""
import ast
import inspect
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from services.acp_angle_gate.channel_style import get_channel_style
from services.acp_angle_gate.goals import get_goal
from services.acp_content_writing import prompts as t9
from services.ai_safety import prompt_fence as pf
from services.content_generation import prompts as v1
from services.content_generation import s1_writer_v2 as v2
from services.quality.s1_facts import build_fact_table
from shared import settings

ROOT = Path(__file__).resolve().parents[2]
MARK = re.compile(r"QZM[A-Z]{2}\d\d")


def _leaks(prompt: str) -> list[str]:
    return MARK.findall(pf.outside_fences(prompt))


# ── the invented inputs: one marker word per untrusted field ──────────────────────────────────────────────────

def _tour() -> dict:
    return {
        "name": "Velor Reed Loop QZMNA01 — 3 Days",
        "country": "Valdoria QZMCO02",
        "duration": "3 days QZMDU03",
        "summary": "A short trip around Lake Velor. QZMSU04",
        "description": "Boat to the reed villages and walk the shore. QZMDE05",
        "highlights": ["Boat to Isle Marrow QZMHI06", "Reed market at Corran"],
        "itineraries": ("Day 1: Drive to Lake Velor (4 hours) QZMIT07. Overnight: Velor guesthouse.\n"
                        "Day 2: Boat to Isle Marrow QZMIT08. Meals: breakfast, lunch.\n"
                        "Day 3: Walk to Tessa Point, then drive back QZMIT09."),
        "inclusions": "Guide, boat crossing QZMIN10",
        "exclusions": "Drinks QZMEX11",
        "price": "USD 900 QZMPR12",
        "group_size": "2-10 QZMGS13",
        "best_time_to_go": "March to May QZMBT14",
        "subtitle": "Through Valdoria QZMSB15",
        "seo_title": "Velor tour QZMST16",
        "seo_meta": "An invented meta. QZMSM17",
    }


# Tour fields a builder reads that are NOT untrusted text (with the reason); everything else read from `tour` must be
# fed a marker in _tour().
TRUSTED_TOUR_KEYS = {
    "itinerary": "legacy alias of itineraries (same untrusted text, fed through itineraries)",
}


def _facts(tour: dict) -> dict:
    facts = build_fact_table(tour)
    facts.setdefault("conflicts", []).append({"value": "conflict QZMCF18", "detail": "two start towns QZMCF18"})
    facts.setdefault("do_not_use", []).append({"value": "Brightwater Tours QZMDN19"})
    facts.setdefault("optional", []).append({"value": "Optional kayak QZMOP20"})
    return facts


def _s1_prompts(fence: bool) -> list[tuple[str, str]]:
    tour = v2.with_agency_fields(_tour(), {"group_size": "2-10 QZMGS13", "best_time_to_go": "March QZMBT14"})
    facts = _facts(tour)
    plan = v2.day_plan(tour, facts)
    page = {"name": "Velor Reed Loop QZMPG21", "subtitle": "Lake and villages QZMPG22", "summary": "Walks QZMPG23"}
    return [
        (v2.w_system_prompt(["epic"], fence=fence), v2.w_user_prompt(tour, facts, plan, fence=fence)),
        (v2.e_system_prompt(["epic"], fence=fence), v2.e_user_prompt(tour, facts, plan, fence=fence)),
        (v2.meta_system_prompt(["epic"], fence=fence),
         v2.meta_user_prompt(page, tour, facts, ["velor tour QZMKW24"], fence=fence)),
    ]


V1_TRUSTED_PARAMS = {
    "few_shots": "curated examples of our own catalogue (never tenant or supplier input)",
    "subtitle_focus": "a mode key",
    "per_day_targets": "a mode switch",
    "voice": "a mode key",
    "fence": "the fence mode itself",
}


def _v1_prompts(fence: bool) -> list[str]:
    seo = {"keywords": {"top_keywords": ["velor tour QZMKW25"]}, "people_also_ask": ["Is Velor open QZMPA26?"]}
    sfacts = {"days": [{"day": 1, "times": ["8am QZMSF27"]}]}
    out = []
    for voice in ("neutral", "tenant"):
        out.append(v1.build_rewrite_prompt(_tour(), seo, [], structured_facts=sfacts, voice=voice,
                                           facts="Ferry tickets cost 4 coins QZMFE28", fence=fence))
    return out


T9_TRUSTED_PARAMS = {
    "goal": "our goal registry (services/acp_angle_gate/goals.py)",
    "channel_style": "our channel registry",
    "brand_audience": "the tenant brand pack: its own guard (brand_pack render), never supplier text",
    "angle": "our angle gate's own output, ground_angle keeps only what the seed supports",
    "cta": "the tenant's CTA: an instruction to include it, brand pack guard",
    "revision_feedback": "our gate verdicts",
    "atom_id": "our atom id",
    "keyword": "a keyword from our research cache, matched by keyword_fit",
    "scope": "a SCOPE line built by code",
    "sibling_openers": "our own writer's earlier openers",
    "memory_lines": "the writing-memory release (our approved lines)",
    "brief_grounding": "a mode key",
    "claim_rule": "a mode switch (AA-1169, setting cis.t9.blog_claim_rule)",
    "reader_checklist": "a mode key (off by default; its quotes come from the fenced seed)",
    "walk_focus": "our atom id",
    "fence": "the fence mode itself",
}
T9_UNTRUSTED_PARAMS = {"content_seed", "route_segments", "facts_text", "destination", "trip_name",
                       "search_questions", "lead_focus"}
ANGLE = {"name": "Morning market", "why_it_works": "A scene", "formula_fit": "AIDA", "best_final_style": "calm"}
AUDIENCE = {"brand_name": "Northstep Journeys", "customer_segment": "Active couples", "voice": ["calm"]}


def _t9_prompts(fence: bool) -> list[str]:
    base = dict(goal=get_goal("promotion"), brand_audience=AUDIENCE, angle=ANGLE, cta="Ask Northstep for dates",
                destination="Lake Velor QZMDS29", trip_name="Velor Reed Loop QZMTR30",
                facts_text="[Fact id=f1] Ferry tickets cost 4 coins QZMFT31", fence=fence)
    seed = "Corran reed market. Traders sell woven mats QZMSE32."
    return [
        t9.build_user_prompt(content_seed=seed, channel_style=get_channel_style("blog"), atom_id="a1",
                             search_questions=["What is sold at Corran QZMSQ33?"], **base),
        t9.build_user_prompt(content_seed="unused", channel_style=get_channel_style("blog"),
                             route_segments=[("a1", "Corran market QZMRS34"), ("a2", "Isle Marrow boat QZMRS35")],
                             walk_focus="a2", **base),
        t9.build_user_prompt(content_seed=seed, channel_style=get_channel_style("facebook"),
                             lead_focus="Traders sell woven mats QZMLF36", **base),
    ]


# ── 1. canary renders ─────────────────────────────────────────────────────────────────────────────────────────

def test_the_markers_reach_the_prompts():
    """The fixture is live: with the fence off every marker family shows up in the prompt (else the guard is blind)."""
    text = "\n".join(u for _, u in _s1_prompts(False)) + "\n".join(_v1_prompts(False)) + "\n".join(_t9_prompts(False))
    expected = {"QZMNA01", "QZMCO02", "QZMDU03", "QZMSU04", "QZMDE05", "QZMHI06", "QZMIT07", "QZMIT08", "QZMIN10",
                "QZMEX11", "QZMCF18", "QZMDN19", "QZMOP20", "QZMPG21", "QZMKW24", "QZMKW25", "QZMPA26", "QZMSF27",
                "QZMFE28", "QZMDS29", "QZMTR30", "QZMFT31", "QZMSE32", "QZMSQ33", "QZMRS34", "QZMRS35", "QZMLF36"}
    assert expected <= set(MARK.findall(text)), sorted(expected - set(MARK.findall(text)))


def test_s1_v2_prompts_fence_every_source_value():
    for system, user in _s1_prompts(True):
        assert _leaks(user) == [] and pf.SYSTEM_RULE in system and _leaks(system) == []


def test_t2_and_v1_prompts_fence_every_source_value():
    for user in _v1_prompts(True):
        assert _leaks(user) == []


def test_t9_prompts_fence_every_source_value():
    for user in _t9_prompts(True):
        assert _leaks(user) == []


def test_our_instructions_stay_outside_the_fence():
    _, user = _s1_prompts(True)[0]
    outside = pf.outside_fences(user)
    for heading in ("DAYS TO WRITE (checked by code)", "NOT IN THE SOURCE (never state or guess these)",
                    "DO NOT USE (never repeat)", "FIGURES (the only numbers you may use"):
        assert heading in outside
    blog = pf.outside_fences(_t9_prompts(True)[0])
    assert "FACTS: additional citable facts" in blog and "CALL TO ACTION TO INCLUDE" in blog


# ── 2. classification ─────────────────────────────────────────────────────────────────────────────────────────

def _params(fn) -> set[str]:
    return set(inspect.signature(fn).parameters)


def test_every_t9_parameter_is_classified():
    assert _params(t9.build_user_prompt) == set(T9_TRUSTED_PARAMS) | T9_UNTRUSTED_PARAMS


def test_every_v1_parameter_is_classified():
    assert _params(v1.build_rewrite_prompt) == set(V1_TRUSTED_PARAMS) | {"tour", "seo", "structured_facts", "facts"}


def test_every_s1_meta_parameter_is_classified():
    assert _params(v2.meta_user_prompt) == {"page", "tour", "facts", "keywords", "fence"}
    assert _params(v2.render_fact_sheet) == {"tour", "facts", "plan", "fence"}


def _tour_keys(path: Path, functions: set[str]) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    keys = set()
    for fn in ast.walk(tree):
        if not (isinstance(fn, ast.FunctionDef) and fn.name in functions):
            continue
        for node in ast.walk(fn):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get"
                    and isinstance(node.func.value, ast.Name) and node.func.value.id == "tour"
                    and node.args and isinstance(node.args[0], ast.Constant)):
                keys.add(node.args[0].value)
            if (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id == "tour"
                    and isinstance(node.slice, ast.Constant)):
                keys.add(node.slice.value)
    return keys


def test_every_tour_field_a_builder_reads_is_fed_a_marker():
    read = (_tour_keys(ROOT / "services/content_generation/s1_writer_v2.py",
                       {"render_fact_sheet", "meta_user_prompt", "w_user_prompt", "e_user_prompt"})
            | _tour_keys(ROOT / "services/content_generation/prompts.py", {"build_rewrite_prompt"}))
    assert read, "the AST scan found no tour field: the guard is blind"
    missing = read - set(_tour()) - set(TRUSTED_TOUR_KEYS)
    assert missing == set(), f"tour fields read by a writer prompt without a marker here: {sorted(missing)}"


# ── 3. off = main ─────────────────────────────────────────────────────────────────────────────────────────────

def test_fence_off_is_the_default_render():
    tour = _tour()
    facts = _facts(tour)
    plan = v2.day_plan(tour, facts)
    assert v2.w_user_prompt(tour, facts, plan, fence=False) == v2.w_user_prompt(tour, facts, plan)
    assert v2.w_system_prompt(["epic"], fence=False) == v2.w_system_prompt(["epic"])
    assert pf.SYSTEM_RULE not in v2.w_system_prompt(["epic"]) and "untrusted_data" not in v2.w_user_prompt(
        tour, facts, plan)
    assert v1.build_rewrite_prompt(tour, {}, voice="tenant", facts="f", fence=False) == v1.build_rewrite_prompt(
        tour, {}, voice="tenant", facts="f")
    for p in _t9_prompts(False) + _v1_prompts(False) + [u for _, u in _s1_prompts(False)]:
        assert "untrusted_data" not in p


# ── 4. live wiring ────────────────────────────────────────────────────────────────────────────────────────────

def _setting(on: bool):
    real_get = settings.get
    return patch.object(settings, "get", side_effect=lambda k, *a: on if k == pf.KEY else real_get(k, *a))


@pytest.mark.parametrize("on", [False, True])
def test_s1_v2_live_prompts_follow_the_setting(on):
    from services.content_generation import s1_writer_v2_live as live
    state = {"tour": _tour(), "tour_agency": None, "brand_forbidden_words": [], "writing_memory": None}
    with _setting(on):
        wp = live.writer_prompts(state)
    assert wp["fence"] is on and (pf.SYSTEM_RULE in wp["system"]) is on
    assert ("untrusted_data" in wp["user"]) is on
    pinned = live.writer_prompts({**state, "prompt_fence": not on})        # a pinned state wins (trap run, tests)
    assert pinned["fence"] is (not on)


@pytest.mark.parametrize("on", [False, True])
def test_t2_state_and_prompts_follow_the_setting(on):
    from api.routers.v1_pipeline import initial_state_for
    from services.content_generation.batch_prompt import build_s1_system_prompt, build_s1_user_prompt
    with _setting(on):
        state = initial_state_for(_tour(), brand_rules={"brand_name": "Northstep"}, is_tenant_rewrite=True,
                                  tenant_id="00000000-0000-4000-8000-000000001167", generate_stage="t2_generate")
    assert state["prompt_fence"] is on
    system, user = build_s1_system_prompt(state), build_s1_user_prompt(state)
    assert (pf.SYSTEM_RULE in system) is on and ("untrusted_data" in user) is on
    if on:
        assert _leaks(user) == []


def test_the_graph_state_keeps_the_fence_key():
    from services.content_generation.graph import ContentState
    assert "prompt_fence" in ContentState.__annotations__               # LangGraph drops undeclared keys (AA-209)


def test_a_state_without_the_key_renders_main():
    from services.content_generation.batch_prompt import build_s1_system_prompt, build_s1_user_prompt
    state = {"tour": _tour(), "seo": {}, "few_shots": [], "is_tenant_rewrite": False}
    assert pf.SYSTEM_RULE not in build_s1_system_prompt(state) and "untrusted_data" not in build_s1_user_prompt(state)


@pytest.mark.parametrize("on", [False, True])
def test_t9_write_and_rewrite_follow_the_setting(on):
    from services.acp_content_writing import generate
    sent = []

    def fake_full(request, channel, **kw):
        sent.append(request)
        return MagicMock(content="A short post about the market."), 0.0

    kw = dict(content_seed="Corran market QZMSE32.", goal=get_goal("promotion"),
              channel_style=get_channel_style("facebook"), brand_audience=AUDIENCE, angle=ANGLE, cta="Ask us")
    with _setting(on), patch.object(generate, "_generate_full", side_effect=fake_full):
        generate.write_content(**kw)
        generate.rewrite_with_feedback(revision_feedback=["shorter"], **kw)
    assert len(sent) == 2
    for request in sent:
        assert (pf.SYSTEM_RULE in request.system_prompt) is on
        assert ("untrusted_data" in request.user_prompt) is on
        if on:
            assert _leaks(request.user_prompt) == []


def test_the_prompt_fingerprint_counts_the_switch():
    """With the fence on, the ledger's prompt_version carries '.s<fp>' (prompt_registry.settings_suffix)."""
    from services.quality import quality_settings
    assert quality_settings.prompt_fp() == ""
    with _setting(True):
        assert quality_settings.prompt_fp() != ""
