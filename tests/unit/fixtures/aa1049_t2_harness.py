"""AA-1049 W3 (T2 train) — the T2 full-chain scripted harness: drives the PRODUCTION
``t2_rewrite_job._rewrite_and_save`` (the real ``_rewrite_tour`` LangGraph, the real T3 gate, the real save SQL)
with every model call answered by a script
keyed by stage, and returns a TRACE of what the run did:

- ``llm``: every model request in order, as (stage, sha16 of the exact system + user prompt sent);
- ``sql``: every statement the run executed on the pool (whitespace-folded SQL + its args, JSON args parsed);
- ``diag``: the job.result diag the function returned.

The parity test (tests/unit/test_aa1049_t2_orchestrator.py) compares the trace of every scenario with the golden
recorded on CIS main BEFORE the T2 orchestrator existed (``aa1049_t2_parity_golden.json``): with every flag at its
default the trace must be equal byte for byte (writer calls, retries, statuses, stored fields).

Scenario texts are invented (no DB, audit or prompt text). No network, no DB, no AWS: LLMClient never builds a client,
Jev (``decide``) answers ``skipped``, the call log writers and the lessons writer are no-ops.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import re
from contextlib import ExitStack
from types import SimpleNamespace
from typing import Any, Callable, Optional
from unittest.mock import AsyncMock, MagicMock, patch

TENANT = "00000000-0000-4000-8000-00000000c0de"
VERSION = "00000000-0000-4000-8000-000000001049"
PUBLISHED = "00000000-0000-4000-8000-00000000b2b2"
TOUR_ID = "00000000-0000-4000-8000-00000000a1a1"

_ITIN = ("Day 1 — Arrive in Velmora\nArrive in Velmora and settle into the guesthouse near the old market. "
         "An evening stroll along the canal with your guide.\n"
         "Day 2 — Corran Ridge walk\nA morning walk up Corran Ridge through pine woods to the ridge chapel, then "
         "lunch at a farm kitchen and a slow return to Velmora.\n"
         "Day 3 — Depart Velmora\nA free morning at the market before the transfer to the station.")

MASTER_ROW = {
    "id": PUBLISHED, "tour_id": TOUR_ID, "aa_name": "Velmora and Corran Ridge — 3 Days",
    "aa_subtitle": "Canal town and ridge walks",
    "aa_summary": "Three easy days in Velmora: an old market town on a canal, a ridge walk through pine woods and a "
                  "farm lunch.",
    "aa_description": "",
    "aa_highlights": '["Velmora old market", "Corran Ridge chapel walk", "Farm kitchen lunch"]',
    "aa_itineraries": _ITIN,
    "seo_title": "Velmora and Corran Ridge 3-Day Walking Tour",
    "seo_meta": "Three easy days in Velmora with a canal stroll, a pine-wood walk up Corran Ridge to the chapel and "
                "a farm kitchen lunch before you leave.",
    "seo_keywords_used": None, "country": "Testland", "duration": "3 days",
    "src_name": "Velmora 3D", "src_subtitle": None, "src_summary": None, "src_description": None,
    "src_highlights": None, "src_itineraries": "Day 1: Velmora. Day 2: Corran Ridge walk. Day 3: depart.",
    "inclusions": None, "activities": None, "price_raw": None, "exclusions": None, "group_size": None,
    "best_time_to_go": None,
}

BRAND_RULES = {
    "system_prompt": "", "style_guide": "Warm, plain and short sentences.", "forbidden_words": ["breathtaking"],
    "rewrite_language": "EN-US", "core_idea": "Slow travel that leaves room to breathe.",
    "customer_segment": "Couples over fifty", "customer_mindset": "Curious, unhurried",
    "voice_examples": ["We walk at the pace of the slowest walker."], "good_examples": "",
    "brand_name": "Quillfern Journeys", "brand_type": "boutique operator", "default_cta": "",
    "signature_words": [],
}

_META_OK = ("Spend three slow days in Velmora with a canal stroll, a pine-wood walk up Corran Ridge to its chapel "
            "and a lunch in a farm kitchen near the town.")


_SUMMARY_412 = ("Spend three unhurried days in Velmora, an old market town on a canal that is 412 years old. Walk up "
                "Corran Ridge to its chapel and share a farm lunch before a free morning at the market.")


def writer_draft(**over) -> dict:
    """A clean writer reply (the T2 v1 JSON contract) with optional field overrides."""
    d = {
        "name": "Velmora and Corran Ridge",
        "subtitle": "A canal town and an easy ridge walk at a gentle pace",
        "summary": "Spend three unhurried days in Velmora, an old market town on a canal. Walk through pine woods "
                   "up Corran Ridge to its chapel, share a lunch in a farm kitchen and keep a free morning for the "
                   "market before you leave.",
        "highlights": ["Stroll the canal in Velmora with your guide", "Walk the pine woods up Corran Ridge",
                       "Share lunch in a farm kitchen", "Browse the old market on a free morning"],
        "itineraries": [
            {"day": 1, "title": "Arrive in Velmora",
             "body": "Arrive in Velmora and settle into the guesthouse near the old market. In the evening your "
                     "guide takes you on a stroll along the canal."},
            {"day": 2, "title": "Corran Ridge walk",
             "body": "Walk up Corran Ridge in the morning through pine woods to the ridge chapel. Lunch is at a farm "
                     "kitchen, then you return slowly to Velmora."},
            {"day": 3, "title": "Depart Velmora",
             "body": "Keep the morning free for the market before your transfer to the station."},
        ],
        "seo_title": "Velmora and Corran Ridge Walking Tour",
        "seo_meta": _META_OK,
        "trip_type": "Walking",
        "seo_keywords_used": [],
    }
    d.update(over)
    return d


def judge_reply(score: float = 8.5) -> dict:
    return {"brand_fit_score": score, "cross_brand_distinct": score, "mission_present": True,
            "feedback": "Warm and plain.", "mission_evidence": "at a gentle pace"}


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
            satellite_account=None, fallback_used=False, cache_read_tokens=0, cache_write_tokens=0)


class Conn:
    def __init__(self, rows: Optional[dict] = None):
        self.executed: list[tuple] = []
        self.rows = rows or {}

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        return "OK"

    async def fetchrow(self, sql, *args):
        self.executed.append((sql, args))
        for key, row in self.rows.items():
            if key in sql:
                return row
        return None

    async def fetchval(self, sql, *args):
        self.executed.append((sql, args))
        return None

    async def fetch(self, sql, *args):
        self.executed.append((sql, args))
        return []


# Every env switch of the T2 path (config_fp.MODE_FLAGS["t2"] plus the ones the run reads); cleared before a run.
T2_SWITCHES = ("T2_JUDGE_GATE", "T2_JUDGE_REPEAT", "T2_JUDGE_ANCHORS", "T2_BRAND_CLAIM_CHECK", "T2_FACTS",
               "T2_BEST_DRAFT", "T2_TARGETED_REPAIR", "T3_BACKGROUND_FACTS", "T2_INTERIM", "T2_ORCHESTRATOR")


def pool(conn: Conn):
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    p = MagicMock()
    p.acquire = MagicMock(return_value=ctx)
    return p


def _fold(sql: str) -> str:
    return re.sub(r"\s+", " ", sql or "").strip()


def _arg(a: Any) -> Any:
    if isinstance(a, str) and a[:1] in "[{":
        try:
            return json.loads(a)
        except ValueError:
            return a
    return a


def _skipped_decide():
    from shared.llm_client.decide import Verdict

    async def decide(stage, subject, state, questions, **_kw):
        return SimpleNamespace(verdicts={q: Verdict(q, "off", "skipped") for q in questions},
                               accepted=lambda q: False)
    return decide


def _no_async(*_a, **_k):
    async def _n():
        return None
    return _n()


SCENARIOS: dict[str, dict] = {
    # a clean first draft: one writer call, one judge call, a pass
    "clean": {"script": {"t2_generate": [writer_draft()], "t2_judge": [judge_reply()]}},
    # a novel number in the summary (T3 grounding) -> one sentence repair (t2_flag_fix) that clears it
    "grounding_repair": {"script": {
        "t2_generate": [writer_draft(summary=_SUMMARY_412)], "t2_judge": [judge_reply()],
        "t2_flag_fix": [{"1": "Spend three unhurried days in Velmora, an old market town on a canal."}]}},
    # the same number, and the repair writes another invented figure back (refused: the draft keeps its finding)
    "grounding_unrepaired": {"script": {
        "t2_generate": [writer_draft(summary=_SUMMARY_412)], "t2_judge": [judge_reply()],
        "t2_flag_fix": [{"1": "Spend three unhurried days in Velmora, a market town of 9000 people."}]}},
    # the writer's first reply is not JSON -> the graph retries the writer (two writer calls)
    "writer_retry": {"script": {"t2_generate": ["not json at all", writer_draft()], "t2_judge": [judge_reply()]}},
    # a day dropped by the writer (2 days for a 3-day Master) and a low judge score
    "day_dropped": {"script": {
        "t2_generate": [writer_draft(itineraries=writer_draft()["itineraries"][:2])],
        "t2_judge": [judge_reply(5.0)]}},
    # a forbidden brand word and an over-long meta
    "forbidden_and_meta": {"script": {
        "t2_generate": [writer_draft(subtitle="A breathtaking canal town and an easy ridge walk",
                                     seo_meta=_META_OK + " " + _META_OK)],
        "t2_judge": [judge_reply()]}},
    # an empty summary (MISSING_FIELD, a hard code no sentence repair can clear)
    "missing_summary": {"script": {"t2_generate": [writer_draft(summary="")], "t2_judge": [judge_reply()]}},
    # elephant riding in the raw source -> policy hold (no model call)
    "elephant": {"row": {"src_itineraries": "Day 2: optional elephant riding at Corran Ridge"},
                 "script": {}},
}


LINEAGE_STAMP = {"v": "lineage-v1", "policy_sha": "pinned-by-harness", "route_config_version": None}


def run(name: str, *, env: Optional[dict] = None, extra: Optional[Callable] = None,
        conn: Optional["Conn"] = None) -> dict:
    """Run one scenario through the production ``_rewrite_and_save`` and return its trace. ``env``: the switches
    set for the run (every T2 switch is cleared first, so an unset switch is its default)."""
    from services.content_generation import flag_fix_node
    from services.jobs import t2_rewrite_job as job
    from shared.llm_client import call_log
    from shared.llm_client.client import LLMClient

    sc = SCENARIOS[name]
    row = {**MASTER_ROW, **(sc.get("row") or {})}
    llm = ScriptedLLM(copy.deepcopy(sc.get("script") or {}))
    conn = conn if conn is not None else Conn()
    tour_dict = job.source_tour_dict(row)
    clean_env = {k: v for k, v in os.environ.items() if k not in T2_SWITCHES}
    with ExitStack() as st:
        st.enter_context(patch.dict(os.environ, {**clean_env, **(env or {})}, clear=True))
        st.enter_context(patch.object(LLMClient, "__init__", lambda self, *a, **k: None))
        st.enter_context(patch.object(LLMClient, "generate", lambda self, req: llm.generate(req)))
        st.enter_context(patch.object(call_log, "record_call", AsyncMock(return_value=None)))
        st.enter_context(patch.object(call_log, "record_calls", AsyncMock(return_value=None)))
        st.enter_context(patch.object(flag_fix_node, "_write_lessons_safe", lambda *a, **k: None))
        st.enter_context(patch("services.acp_produce.tenant_pipeline.decide", _skipped_decide()))
        st.enter_context(patch("services.seo_intelligence.s1_prefetch.tenant_market_seo",
                               AsyncMock(return_value=("US", None))))
        st.enter_context(patch("services.writing_memory.insertion.resolve", AsyncMock(return_value=None)))
        # AA-1052: the lineage stamp the store step writes next to the final contract, pinned so the golden does not
        # move with a policy edit (the policy has its own change control) or with another test's route cache
        st.enter_context(patch("services.quality.lineage.stamp", lambda **_: dict(LINEAGE_STAMP)))
        if extra is not None:
            extra(st)
        diag = asyncio.run(job._rewrite_and_save(pool(conn), row, tour_dict, copy.deepcopy(BRAND_RULES), TENANT,
                                                 VERSION, PUBLISHED))
    return {"llm": llm.calls, "unexpected": llm.unexpected,
            "sql": [[_fold(s), [_arg(a) for a in args]] for s, args in conn.executed],
            "diag": json.loads(json.dumps(diag, default=str))}


def run_all(**kw) -> dict:
    return {name: run(name, **kw) for name in SCENARIOS}


def _stored(d: dict) -> dict:
    d = dict(d)
    d["itineraries"] = "\n\n".join(f"Day {x['day']}: {x['title']}\n{x['body']}" for x in d["itineraries"])
    d.pop("seo_keywords_used", None)
    return {**d, "status": "done"}


def replay_versions() -> list[dict]:
    """The synthetic stored-version set of the offline T2 replay test (schema aa1049-t2-versions/1 rows; invented
    tours). Its main-recorded replay is tests/unit/fixtures/aa1049_t2_replay_main_base.json: change this set only
    together with a re-record of that file (see test_aa1049_t2_orchestrator.py, section 4)."""
    jb = {"brand_fit": 8.0, "cross_brand_distinct": 8.0, "mission_present": True, "feedback": "ok"}
    out = []
    for name, over in (("clean", {}), ("number", {"summary": _SUMMARY_412}),
                       ("days", {"itineraries": writer_draft()["itineraries"][:2]}), ("missing", {"summary": ""})):
        out.append({"id": f"v-{name}", "tenant_id": TENANT, "status": "ai_generated", "quality_score": 9.0,
                    "qa_status": "passed", "qa_repair_count": 0, "rewritten_content": _stored(writer_draft(**over)),
                    "judge_breakdown": jb if name != "missing" else None, "pt": dict(MASTER_ROW),
                    "brand_rules": BRAND_RULES})
    out.append({"id": "v-elephant", "tenant_id": TENANT, "rewritten_content": {}, "brand_rules": BRAND_RULES,
                "pt": {**MASTER_ROW, "src_itineraries": "Day 2: elephant riding"}})
    return out


if __name__ == "__main__":   # record the golden: python -m tests.unit.fixtures.aa1049_t2_harness <out.json>
    import sys
    with open(sys.argv[1], "w", encoding="utf-8") as fh:
        json.dump(run_all(), fh, indent=1, sort_keys=True, ensure_ascii=False)
        fh.write("\n")
