"""AA-1049 W3 (S1 train) — the S1 full-chain scripted harness: drives the PRODUCTION A1 rewrite
(``v1_pipeline._rewrite_tour``: the real LangGraph, the S1 writer v2, validate, judge, brand audit, grounding, flag_fix,
revalidate, the final checks and the REPORT-mode final contract) with every model call answered by a script keyed by
stage, and returns a TRACE of what the run did:

- ``llm``: every model request in order, as (stage, sha16 of the exact system + user prompt sent);
- ``result``: the outcome fields of the dict ``_rewrite_tour`` returns (the stored page, score, codes, statuses, the
  final contract record) plus ``publishable`` (``admin_pipeline._is_publishable``: the A1 publish gate).

The parity test (tests/unit/test_aa1049_s1_train.py) compares the trace of every scenario with the golden recorded on
CIS main BEFORE the S1 train existed (``aa1049_s1_parity_golden.json``): with ``S1_CONTROL`` unset (or any value but
``contract``) the trace must be equal byte for byte (writer calls, retries, statuses, stored fields).

Scenario texts are invented (no DB, audit or prompt text). No network, no DB, no AWS: LLMClient never builds a client,
Jev (``grounding.judge_units``) answers ``skipped`` for every unit, the call-log writers and the lessons writer are
no-ops.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
from contextlib import ExitStack
from types import SimpleNamespace
from typing import Any, Callable, Optional
from unittest.mock import AsyncMock, patch

SOURCE_ITIN = ("Day 1: Arrive in Velmora and transfer to the guesthouse near the old market. Evening stroll along "
               "the canal.\nOvernight: Velmora\nMeals: D\n"
               "Day 2: Walk up Corran Ridge through pine woods to the ridge chapel, then lunch at a farm kitchen.\n"
               "Distance: 12 km\nOvernight: Velmora\nMeals: B, L\n"
               "Day 3: Free morning at the market, then transfer to the station.\nMeals: B")

TOUR = {
    "name": "VELMORA CANAL AND RIDGE 3D", "subtitle": None,
    "summary": "Three days in Velmora, an old market town on a canal, with a walk up Corran Ridge.",
    "description": "A small-group walking break in Velmora.", "highlights": ["Corran Ridge chapel", "Farm lunch"],
    "itineraries": SOURCE_ITIN, "country": "Testland", "duration": "3 days", "price": None,
    "inclusions": "Guesthouse nights\nMeals as listed\nWalking guide", "exclusions": "Flights",
}
TOUR_AGENCY = {"group_size": "2-10", "best_time_to_go": None}
BRAND_RULES = {"system_prompt": "", "style_guide": "", "forbidden_words": ["breathtaking"],
               "rewrite_language": "en-US"}

_TITLE = "Velmora Canal and Corran Ridge Walk — 3 Days"
META_OK = ("A three-day walking break in Velmora, Testland, with a canal stroll, a 12 km walk up Corran Ridge to "
           "the chapel and a farm kitchen lunch on the way.")
META_LONG = META_OK[:-1] + ", followed by a free morning at the old market before the transfer to the station."


def _days(over: Optional[dict] = None) -> list[dict]:
    days = [
        {"day": 1, "title": "Arrive in Velmora",
         "body": "Arrive in Velmora and transfer to the guesthouse near the old market. In the evening there is a "
                 "stroll along the canal.\nOvernight: Velmora. Meals: dinner."},
        {"day": 2, "title": "Corran Ridge walk",
         "body": "Walk up Corran Ridge through pine woods to the ridge chapel, then lunch at a farm kitchen.\n"
                 "Distance: 12 km. Overnight: Velmora. Meals: breakfast, lunch."},
        {"day": 3, "title": "Velmora market and departure",
         "body": "A free morning at the market, then the transfer to the station.\nMeals: breakfast."},
    ]
    for d in days:
        if d["day"] in (over or {}):
            d.update(over[d["day"]])
    return days


def writer_page(**over) -> dict:
    """A clean S1 writer v2 reply (the W JSON contract) with optional field overrides."""
    page = {
        "name": "Velmora Canal and Corran Ridge Walk — 3 Days",
        "subtitle": "Three days of easy walking from the canal town of Velmora to Corran Ridge.",
        "summary": "A three-day walking break in Velmora, an old market town on a canal. Day two walks up Corran "
                   "Ridge through pine woods to the ridge chapel, with lunch at a farm kitchen.",
        "highlights": ["Evening stroll along the Velmora canal", "Pine-wood walk up Corran Ridge",
                       "Lunch at a farm kitchen", "Free morning at the old market"],
        "itineraries": _days(),
        "trip_type": "trekking",
    }
    page.update(over)
    return page


def meta_reply(meta: str = META_OK, title: str = _TITLE) -> dict:
    return {"seo_title": title, "seo_meta": meta}


class ScriptedLLM:
    """``LLMClient.generate`` answered from ``script[stage]`` (a list consumed in order; the last item repeats).
    Every request is recorded as (stage, sha16 of system + user prompt)."""

    def __init__(self, script: dict[str, list]):
        self.script = {k: list(v) for k, v in script.items()}
        self.calls: list[list[str]] = []
        self.unexpected: list[str] = []

    def generate(self, request) -> SimpleNamespace:
        stage = getattr(request, "stage", None) or "?"
        sha = hashlib.sha256(((request.system_prompt or "") + "\x00" + (request.user_prompt or "")).encode())
        self.calls.append([stage, sha.hexdigest()[:16]])
        answers = self.script.get(stage)
        if not answers:
            self.unexpected.append(stage)
            raise RuntimeError(f"harness: unscripted model call (stage={stage})")
        content = answers.pop(0) if len(answers) > 1 else answers[0]
        if isinstance(content, Exception):
            raise content
        return SimpleNamespace(
            content=content if isinstance(content, str) else json.dumps(content), cost_usd=0.001,
            model_used="harness-stub", input_tokens=10, output_tokens=10, stop_reason="end_turn",
            satellite_account=None, fallback_used=False, cache_read_tokens=0, cache_write_tokens=0, provider="stub")


def skipped_judge_units(units, sources, *, tenant_id=None):
    """Jev answers ``skipped`` (mode off) for every asked unit: no verdict clears anything, nothing is degraded."""
    from shared.llm_client.decide import Verdict
    from services.content_generation import grounding as g
    return {i: Verdict(g.QUESTION, "off", "skipped") for i in (sources or {})}


# Every env switch of the A1 path (config_fp.MODE_FLAGS["a1"] plus the S1 train switch); cleared before a run.
S1_SWITCHES = ("S1_WRITER_V2", "S1_STRUCTURED_FACTS", "S1_PER_DAY_TARGETS", "S1_WRITER_TEMPERATURE",
               "A1_BRAND_FIT_RUBRIC", "A1_V2_BRAND_FIT_GATE", "S1_JUDGE_LIVE", "S1_CONTROL")

# The outcome fields of _rewrite_tour's result the trace keeps (everything a store step or the publish gate reads).
RESULT_KEYS = ("generated", "quality_score", "retry_count", "error", "failure_codes", "brand_audit_status",
               "brand_audit_codes", "brand_audit_fields", "fix_pass_applied", "fix_pass_fields", "revalidate_ran",
               "revalidate_passed", "grounding_ran", "grounding_violations", "grounding_repaired_fields",
               "exit_path", "quality_shadow", "degraded", "writer_version", "status", "final_contract")

_FIX = {"itineraries": "Day 1 — Arrive in Velmora\nArrive in Velmora and transfer to the guesthouse near the old "
                       "market.\n\nDay 2 — Corran Ridge walk\nWalk up Corran Ridge to the ridge chapel.\n\n"
                       "Day 3 — Velmora market and departure\nA free morning at the market.",
        "seo_meta": META_OK}

SCENARIOS: dict[str, dict] = {
    # a clean first draft: writer + meta, no repair
    "clean": {"script": {"s1_generate": [writer_page(), meta_reply()]}},
    # a meal the source day does not list, in prose -> brand audit ITINERARY_MEAL_TIME_INVENTED (main: flag_fix)
    "meal_in_prose": {"script": {
        "s1_generate": [writer_page(itineraries=_days({3: {
            "body": "A free morning at the market. Lunch is included at the station cafe before the transfer.\n"
                    "Meals: breakfast."}})), meta_reply()],
        "s1_flag_fix": [{"itineraries": _FIX["itineraries"]}]}},
    # the writer's fact line names a meal the source day does not list (dinner on day 2)
    "fact_line_wrong": {"script": {
        "s1_generate": [writer_page(itineraries=_days({2: {
            "body": "Walk up Corran Ridge through pine woods to the ridge chapel, then lunch at a farm kitchen.\n"
                    "Distance: 12 km. Overnight: Velmora. Meals: breakfast, lunch, dinner."}})), meta_reply()],
        "s1_flag_fix": [{"itineraries": _FIX["itineraries"]}]}},
    # the meta is out of band twice (writer meta + its one retry) -> main: flag_fix seo_meta + _rerepair_meta
    "meta_long": {"script": {
        "s1_generate": [writer_page(), meta_reply(META_LONG), meta_reply(META_LONG)],
        "s1_flag_fix": [{"seo_meta": META_LONG}]}},
    # the writer drops day 2 (ITINERARY_DAY_COUNT_MISMATCH, hard on a v2 draft)
    "day_dropped": {"script": {
        "s1_generate": [writer_page(itineraries=[d for d in _days() if d["day"] != 2]), meta_reply(),
                        writer_page(), meta_reply()],
        "s1_flag_fix": [{"itineraries": _FIX["itineraries"]}]}},
    # a number the source does not give -> grounding sentence repair (stage s1_flag_fix)
    "number": {"script": {
        "s1_generate": [writer_page(summary="A three-day walking break in Velmora, a market town that is 412 years "
                                            "old. Day two walks up Corran Ridge to the ridge chapel."), meta_reply()],
        "s1_flag_fix": [{"1": "A three-day walking break in Velmora, an old market town on a canal."}]}},
    # elephant riding in the source -> policy hold (kept for a human, no repair)
    "elephant": {"tour": {"itineraries": SOURCE_ITIN.replace("Free morning at the market",
                                                             "Optional elephant riding near the market")},
                 "script": {"s1_generate": [writer_page(), meta_reply()]}},
}


def _jsonable(v: Any) -> Any:
    return json.loads(json.dumps(v, default=str, sort_keys=True))


def run(name: str, *, env: Optional[dict] = None, extra: Optional[Callable] = None,
        extra_keys: tuple = ()) -> dict:
    """Run one scenario through the production ``_rewrite_tour`` and return its trace. ``env``: the switches set for
    the run (every S1 switch is cleared first; ``S1_WRITER_V2=on`` is the live A1 default, S250). ``extra_keys``: result
    keys read on top of RESULT_KEYS (kept out of RESULT_KEYS so the main-recorded parity golden stays as recorded)."""
    from api.routers import admin_pipeline
    from api.routers.v1_pipeline import _rewrite_tour
    from services.content_generation import flag_fix_node
    from services.content_generation import grounding
    from shared.llm_client import call_log
    from shared.llm_client.client import LLMClient

    sc = SCENARIOS[name]
    tour = {**TOUR, **(sc.get("tour") or {})}
    llm = ScriptedLLM(copy.deepcopy(sc.get("script") or {}))
    clean_env = {k: v for k, v in os.environ.items() if k not in S1_SWITCHES}
    with ExitStack() as st:
        st.enter_context(patch.dict(os.environ, {**clean_env, "S1_WRITER_V2": "on", **(env or {})}, clear=True))
        st.enter_context(patch.object(LLMClient, "__init__", lambda self, *a, **k: None))
        st.enter_context(patch.object(LLMClient, "generate", lambda self, req: llm.generate(req)))
        st.enter_context(patch.object(call_log, "record_call", AsyncMock(return_value=None)))
        st.enter_context(patch.object(call_log, "record_calls", AsyncMock(return_value=None)))
        st.enter_context(patch.object(flag_fix_node, "_write_lessons_safe", lambda *a, **k: None))
        st.enter_context(patch.object(grounding, "judge_units", skipped_judge_units))
        if extra is not None:
            extra(st)
        result = asyncio.run(_rewrite_tour(copy.deepcopy(tour), idx=0, total=1, brand_rules=copy.deepcopy(BRAND_RULES),
                                           seo={}, tour_agency=dict(TOUR_AGENCY), writing_memory=None))
    out = {k: result.get(k) for k in (*RESULT_KEYS, *extra_keys)}
    out["publishable"] = admin_pipeline._is_publishable(result)
    return {"llm": llm.calls, "unexpected": llm.unexpected, "result": _jsonable(out)}


def run_all(**kw) -> dict:
    return {name: run(name, **kw) for name in SCENARIOS}


def _stored(page: dict, meta: Optional[dict] = None) -> dict:
    """A writer page as the store keeps it: the canonical itinerary string and the meta fields."""
    d = dict(page)
    d["itineraries"] = "\n\n".join(f"Day {x['day']} — {x['title']}\n{x['body']}" for x in d["itineraries"])
    return {**d, **(meta or meta_reply())}


def replay_masters() -> dict:
    """The synthetic stored-Master set of the offline S1 replay test (schema aa1049-s1-masters/1; invented tours).
    Its main-recorded replay is tests/unit/fixtures/aa1049_s1_replay_main_base.json: change this set only together
    with a re-record of that file (see tests/unit/test_aa1049_s1_train.py, section 5)."""
    rows = (
        ("m-clean", {}, writer_page(), None),
        ("m-fact-line", {}, writer_page(itineraries=_days({2: {
            "body": "Walk up Corran Ridge through pine woods to the ridge chapel, then lunch at a farm kitchen.\n"
                    "Distance: 12 km. Overnight: Velmora. Meals: breakfast, lunch, dinner."}})), None),
        ("m-meta-long", {}, writer_page(), meta_reply(META_LONG)),
        ("m-title-long", {}, writer_page(), meta_reply(title=_TITLE + " in Testland with Farm Lunch")),
        ("m-elephant", {"itineraries": SOURCE_ITIN.replace("Free morning at the market",
                                                           "Optional elephant riding near the market")},
         writer_page(), None),
    )
    return {"schema": "aa1049-s1-masters/1",
            "masters": [{"tour_id": tid, "tour": {**TOUR, **tour_over}, "tour_agency": dict(TOUR_AGENCY),
                         "brand_rules": dict(BRAND_RULES), "stored": _stored(page, meta)}
                        for tid, tour_over, page, meta in rows]}


if __name__ == "__main__":   # record the golden: python -m tests.unit.fixtures.aa1049_s1_harness <out.json>
    import sys
    with open(sys.argv[1], "w", encoding="utf-8") as fh:
        json.dump(run_all(), fh, indent=1, sort_keys=True, ensure_ascii=False)
        fh.write("\n")
