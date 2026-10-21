#!/usr/bin/env python3
"""AA-729 step 2 — score the golden set. Two modes over one fixture version:

  --mode score-only   judges / gates over the FROZEN outputs (no writer call):
                        S1  the revalidation graph (validate + judge + brand audit), verdict = publish gate;
                        T2  the same graph on the tenant version + the T3 QA check without repairs;
                        T9  the T10 gate stack (F9 judge included) on the stored final text;
                        injection: the frozen compromised output — expected to fail.
  --mode regenerate   the real writer path on the FROZEN inputs, then its own judges / gates:
                        S1  v1_pipeline._rewrite_tour (+ the AA-736 Sonnet retry), verdict = publish gate;
                        T2  _rewrite_tour (tenant) + run_t3_qa_gate, verdict = score >= 7 and QA passed;
                        T9  service.run_write_background with the piece write captured, verdict = approved;
                        injection: poisoned input, verdict + "defended" (no canary/link/markup/leak).

Every model call goes through the gateway (LLMClient, Jev decide, embed) and lands in shared.llm_call_log
with stage 'adhoc_aa729' (evalpatch: real stage in quality_signal.aa729_stage, run id in
quality_signal.aa729_run, per-sample job_id; tenant / piece / request ids only in quality_signal) and every
Jev verdict in shared.decision_log under stage 'adhoc_aa729:<real stage>'. Nothing is written to app data
(evalpatch). Two cost stops share --max-cost-usd (default 15): the launch guard (common.CostGuard) starts a
sample only while logged spend + in-flight reservations + its estimate fit; the in-call stop
(evalpatch.BUDGET) refuses any model call once spend + one call margin per concurrent sample reaches the
cap, and that sample is reported as an error. A writer crash (exception, throttle, empty output) is an
error row too, never a quality verdict.

AA-925 — every verdict carries the sample's `degraded` counts and `degraded_hard` (a hard detector — Jev, the
F9 judge, grounding — failed open). Such a sample is scored once more; still hard-degraded after that retry,
its stratum is listed in `invalid_strata` and report.py refuses that stratum's before/after delta.

  cd /app && PYTHONPATH=/tmp/aa729 python3 -m scripts.aa729.score --fixture s3:scripts/out/aa729/fixture-1.1.0.json
      --mode regenerate --limit 3 [--concurrency 3] [--max-cost-usd 15] [--strata s1,t9_blog]
      [--sample-ids s1-001,t2-004] [--resume RUN_ID] [--estimate] [--allow-busy-queue] [--no-upload]

`--estimate` prints the plan and its estimated cost and exits (no DB, no model call). `--limit N` takes
samples round-robin across strata (s1, t9_blog, t9_linkedin, t2, injection), so a trial of 3 exercises the
S1, blog and LinkedIn paths. Output: s3://<bronze>/scripts/out/aa729/<run_id>/score-<mode>.json, with a
checkpoint after every sample (`--resume RUN_ID` continues it under the same cap).
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import UUID

sys.path.insert(0, os.getcwd())

from scripts.aa729.common import (  # noqa: E402
    COST_ESTIMATE, DEFAULT_CAP_USD, S3_PREFIX, SCORE_SCHEMA, STAGE, CostCapReached, CostGuard, flatten_text,
    injection_violations, jev_summary, kind_of, sample_job_id, scrub, stratum_metrics,
)

PER_SAMPLE_TIMEOUT_S = 1500
SPEND_REFRESH_S = 5.0  # how often the in-call stop (evalpatch.BUDGET) re-reads the logged spend
SETTLE_S = 2.0  # fire-and-forget llm_call_log writes land within this after a sample ends
ROUND_ROBIN = ("s1", "t9_blog", "t9_linkedin", "t2", "injection")

_SPEND_SQL = f"""
    SELECT COALESCE(sum(cost_usd), 0)::float FROM shared.llm_call_log
    WHERE stage = '{STAGE}' AND created_at >= $1 AND quality_signal->>'aa729_run' = $2
"""
_CALLS_SQL = f"""
    SELECT job_id::text AS job_id, role, provider, quality_signal->>'aa729_stage' AS real_stage,
           count(*)::int AS n, COALESCE(sum(cost_usd), 0)::float AS usd
    FROM shared.llm_call_log
    WHERE stage = '{STAGE}' AND created_at >= $1 AND quality_signal->>'aa729_run' = $2
    GROUP BY 1, 2, 3, 4
"""
# AA-927: per real stage, split by role / provider / gateway-attempt rows, for the call-vs-row reconciliation.
_STAGE_SQL = f"""
    SELECT quality_signal->>'aa729_stage' AS real_stage, role, provider,
           (quality_signal ? 'gateway_attempt') AS gateway_attempt,
           count(*)::int AS n, COALESCE(sum(cost_usd), 0)::float AS usd
    FROM shared.llm_call_log
    WHERE stage = '{STAGE}' AND created_at >= $1 AND quality_signal->>'aa729_run' = $2
    GROUP BY 1, 2, 3, 4
"""
_JEV_SQL = """
    SELECT job_id::text AS job_id, question_key, zone, count(*)::int AS n
    FROM shared.decision_log
    WHERE created_at >= $1 AND job_id = ANY($2::uuid[])
    GROUP BY 1, 2, 3
"""
_BUSY_SQL = "SELECT count(*) FROM shared.job WHERE status IN ('queued', 'running')"


# ── plan ─────────────────────────────────────────────────────────────────────────────────────────


def plan(samples: list[dict], *, strata: Optional[set] = None, ids: Optional[set] = None,
         limit: Optional[int] = None, done: Optional[set] = None) -> list[dict]:
    """The samples to score, in run order. `limit` interleaves strata (ROUND_ROBIN) so a small trial
    covers several writer paths; without it the fixture order is kept."""
    pool = [s for s in samples if (not strata or s["stratum"] in strata) and (not ids or s["sample_id"] in ids)
            and not (done and s["sample_id"] in done)]
    if not limit:
        return pool
    queues = {k: [s for s in pool if s["stratum"] == k] for k in ROUND_ROBIN}
    out: list[dict] = []
    while len(out) < limit and any(queues.values()):
        for k in ROUND_ROBIN:
            if queues[k] and len(out) < limit:
                out.append(queues[k].pop(0))
    return out


def estimate(samples: list[dict], mode: str) -> dict:
    by: dict[str, float] = {}
    for s in samples:
        k = kind_of(s)
        by[k] = round(by.get(k, 0.0) + COST_ESTIMATE[mode][k], 4)
    return {"samples": len(samples), "estimated_usd": round(sum(by.values()), 4), "by_kind": by}


# ── graph helpers (S1 / T2) ──────────────────────────────────────────────────────────────────────


def graph_state(tour: dict, seo: dict, brand_rules: dict, *, is_tenant: bool, tenant_id: Optional[str],
                generate_stage: str, generated: Optional[dict] = None) -> dict:
    """The content graph's initial state, key for key what v1_pipeline._rewrite_tour builds (the
    revalidation path adds the frozen `generated`). tests/unit/test_aa729_golden_set.py checks every key
    is declared in graph.ContentState."""
    br = brand_rules or {}
    state = {
        "tour": tour, "seo": seo or {}, "model_tier": None, "is_tenant_rewrite": is_tenant,
        "tenant_id": tenant_id, "generate_stage": generate_stage, "few_shots": [], "generated": generated or {},
        "quality_score": 0.0, "retry_count": 0, "feedback": "", "error": "", "cost_usd": 0.0, "model_used": "",
        "brand_system_prompt": br.get("system_prompt", ""), "brand_style_guide": br.get("style_guide", ""),
        "brand_forbidden_words": br.get("forbidden_words", []),
        "rewrite_language": br.get("rewrite_language", "en-US"),
        "brand_core_idea": br.get("core_idea", ""), "brand_customer_segment": br.get("customer_segment", ""),
        "brand_customer_mindset": br.get("customer_mindset", ""),
        "brand_voice_examples": br.get("voice_examples", []), "brand_good_examples": br.get("good_examples", ""),
        "brand_name": br.get("brand_name", ""), "brand_type": br.get("brand_type", ""),
        "brand_default_cta": br.get("default_cta", ""), "brand_signature_words": br.get("signature_words", []),
        "subtitle_focus": "standard", "seo_mode": "dataforseo",
        "brand_audit_status": "", "brand_audit_codes": [], "brand_audit_issues": [], "brand_audit_fields": [],
        "lessons_extracted": [], "fix_pass_applied": False, "fix_pass_fields": [],
        "quality_shadow": [], "degraded": [],  # AA-925
    }
    return state


async def run_revalidation(state: dict) -> dict:
    """build_revalidation_graph() on `state`, the way admin_pipeline._revalidate_tour runs it (sync invoke
    on its own loop), in a thread that carries the bound job / tenant context."""
    from services.content_generation.graph import build_revalidation_graph

    graph = build_revalidation_graph()

    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return graph.invoke(state)
        finally:
            loop.close()

    return await asyncio.to_thread(_run)


def degraded_block(entries: Any) -> dict:
    """AA-925 — a sample's `degraded[]` (services/quality/degraded.py) for the verdict: the entries, their
    counts and whether a hard detector failed open (that sample's stratum is INVALID unless a retry clears it)."""
    entries = [e for e in (entries or []) if isinstance(e, dict)]
    hard = sorted({e.get("name") for e in entries if e.get("severity") == "hard" and int(e.get("count") or 0) > 0})
    counts: dict[str, int] = {}
    for e in entries:
        counts[e.get("name")] = counts.get(e.get("name"), 0) + int(e.get("count") or 0)
    return {"degraded": counts, "degraded_hard": hard}


def t9_degraded(ledger: Any, repair_log: Any = None) -> list[dict]:
    """The final attempt's T9 degraded entries (the F9 outage marker on its ledger, the contract reverts)."""
    from services.quality import degraded

    contract = sum(int(e.get("contract_reverts") or 0) for e in (repair_log or []) if isinstance(e, dict))
    return degraded.t9_entries([g for g in (ledger or []) if isinstance(g, dict)], contract)


def s1_verdict(result: dict) -> dict:
    from api.routers.admin_pipeline import _is_publishable

    return {**degraded_block(result.get("degraded")),
            "passed": bool(_is_publishable(result)), "quality_score": result.get("quality_score"),
            "judge_score": result.get("judge_score"), "brand_audit_status": result.get("brand_audit_status"),
            "failure_codes": list(result.get("failure_codes") or []),
            "revalidate_passed": result.get("revalidate_passed"), "model_used": result.get("model_used"),
            "retry_count": result.get("retry_count"),
            "grounding_violations": len(result.get("grounding_violations") or [])}


class WriterFailed(RuntimeError):
    """The writer path crashed or produced nothing (exception, throttle, cost stop) — an error row, never a
    quality verdict: a crashed sample must not lower the pass rate or count as a defended injection."""


def _require_success(result: dict, what: str) -> None:
    """_rewrite_tour returns status 'failed' (not an exception) when the graph raised or produced no
    `generated`; turn that into an error for one()."""
    if result.get("status") != "success":
        raise WriterFailed(f"{what}: {(result.get('error') or 'no generated output')[:300]}")


def _brand(fixture: dict, ref: Optional[str]) -> dict:
    return copy.deepcopy(((fixture.get("shared_inputs") or {}).get("brand_rules") or {}).get(ref or "") or {})


async def s1_regenerate(sample: dict, fixture: dict) -> tuple[dict, Any]:
    from api.routers.admin_pipeline import _REWRITE_CLASS_HARD_CODES, _surviving_hard_codes
    from api.routers.v1_pipeline import _rewrite_tour

    inp = sample["input"]
    tour = copy.deepcopy(inp["tour"])
    kw = {"brand_rules": _brand(fixture, inp.get("brand_rules_ref")), "seo": copy.deepcopy(inp.get("seo") or {}),
          "model_tier": None, "subtitle_focus": "standard", "seo_mode": "dataforseo"}
    result = await _rewrite_tour(tour, 0, 1, **kw)
    _require_success(result, "s1 rewrite")
    sonnet = False
    # AA-736 (admin_pipeline._execute_run_tour): a rewrite-class hard code left on a Haiku run → one Sonnet
    # rewrite, kept when fewer hard codes survive. The AA-237 opt-in upgrade is off by default there too.
    if (_surviving_hard_codes(result) & _REWRITE_CLASS_HARD_CODES
            and "haiku" in (result.get("model_used") or "").lower()):
        upgraded = await _rewrite_tour(copy.deepcopy(inp["tour"]), 0, 1, **{**kw, "model_tier": "sonnet"})
        _require_success(upgraded, "s1 Sonnet retry")
        if len(_surviving_hard_codes(upgraded)) < len(_surviving_hard_codes(result)):
            result = upgraded
        sonnet = True
    verdict = {**s1_verdict(result), "sonnet_retried": sonnet, "status": result.get("status"),
               "error": (result.get("error") or "")[:300] or None}
    return verdict, result.get("generated") or {}


async def s1_score_only(sample: dict, fixture: dict) -> tuple[dict, Any]:
    inp = sample["input"]
    state = graph_state(copy.deepcopy(inp["tour"]), copy.deepcopy(inp.get("seo") or {}),
                        _brand(fixture, inp.get("brand_rules_ref")), is_tenant=False, tenant_id=None,
                        generate_stage="s1_generate", generated=copy.deepcopy(sample["output"]))
    state["model_tier"] = "haiku"  # as _revalidate_tour
    return s1_verdict(await run_revalidation(state)), None


def t2_verdict(result: dict, qa: Optional[dict]) -> dict:
    score = float(result.get("quality_score") or 0.0)
    return {**degraded_block(list(result.get("degraded") or []) + list((qa or {}).get("degraded") or [])),
            "passed": bool(score >= 7.0 and qa is not None and qa.get("passed")), "quality_score": score,
            "qa_passed": None if qa is None else bool(qa.get("passed")),
            "qa_attempts": None if qa is None else qa.get("attempts"),
            "structural_issues": (qa or {}).get("structural_issues", [])[:10],
            "grounding_issues": len((qa or {}).get("grounding_issues") or []),
            "failure_codes": list(result.get("failure_codes") or []),
            "brand_audit_status": result.get("brand_audit_status"), "status": result.get("status")}


async def t2_regenerate(sample: dict, fixture: dict) -> tuple[dict, Any]:
    from api.routers.v1_pipeline import _rewrite_tour
    from services.acp_produce.tenant_pipeline import run_t3_qa_gate

    inp = sample["input"]
    tour, seo, br, tid = copy.deepcopy(inp["tour"]), copy.deepcopy(inp.get("seo") or {}), \
        _brand(fixture, inp.get("brand_rules_ref")), inp["tenant_id"]
    result = await _rewrite_tour(tour, 0, 1, brand_rules=br, seo=seo, is_tenant_rewrite=True, tenant_id=tid,
                                 generate_stage="t2_generate")
    _require_success(result, "t2 rewrite")
    qa = await run_t3_qa_gate(tour, [str(v) for v in tour.values() if v], result, br, seo_data=seo,
                              tenant_id=tid)
    result = qa["result"]
    return t2_verdict(result, qa), result.get("generated") or {}


async def t2_score_only(sample: dict, fixture: dict) -> tuple[dict, Any]:
    from services.acp_produce.tenant_pipeline import run_t3_qa_gate

    inp = sample["input"]
    tour, seo, br, tid = copy.deepcopy(inp["tour"]), copy.deepcopy(inp.get("seo") or {}), \
        _brand(fixture, inp.get("brand_rules_ref")), inp["tenant_id"]
    state = graph_state(tour, seo, br, is_tenant=True, tenant_id=tid, generate_stage="t2_generate",
                        generated=copy.deepcopy(sample["output"]))
    result = await run_revalidation(state)
    qa = await run_t3_qa_gate(tour, [str(v) for v in tour.values() if v], dict(result), br, max_repairs=0,
                              seo_data=seo, tenant_id=tid)
    return t2_verdict(result, qa), None


# ── T9 ───────────────────────────────────────────────────────────────────────────────────────────


def _blocking_failures(ledger: list) -> list[str]:
    return [g.get("gate") for g in ledger or [] if isinstance(g, dict) and not g.get("passed")
            and g.get("blocking", True)]


async def t9_regenerate(sample: dict, pool) -> tuple[dict, Any]:
    from scripts.aa729 import evalpatch
    from services.acp_content_writing import service as svc

    inp = sample["input"]
    piece_id = inp["piece_id"]
    evalpatch.T9_CAPTURE.pop(piece_id, None)
    await svc.run_write_background(UUID(inp["request_id"]), UUID(piece_id), copy.deepcopy(inp["context"]), pool)
    cap = evalpatch.T9_CAPTURE.pop(piece_id, None) or {}
    # run_write_background catches every exception and finalizes status='failed' (and nothing at all when
    # that write fails too): an error, not a held piece.
    if not cap:
        raise WriterFailed("t9 write: no _finalize_piece capture")
    if cap.get("status") == "failed":
        raise WriterFailed(f"t9 write: {(cap.get('held_reason') or 'failed')[:300]}")
    ledger = cap.get("gate_ledger") or []
    verdict = {**degraded_block(t9_degraded(ledger, cap.get("repair_log"))),
               "passed": cap.get("status") == "approved", "status": cap.get("status"),
               "held_reason": (cap.get("held_reason") or "")[:300] or None, "attempts": cap.get("attempt_number"),
               "blocking_failures": _blocking_failures(ledger),
               "repairs": len(cap.get("repair_log") or [])}
    output = {k: cap.get(k) for k in ("content_text", "seo_title", "meta_description", "slug")}
    output["gate_ledger"] = ledger
    return verdict, output


async def t9_gate_replay(context: dict, output: dict) -> dict:
    """run_quality_gates on a stored final text, with the gate inputs built from the frozen context the way
    run_write_background builds them (hygiene / trip-fact contexts, briefs, scoped trip line, grounding
    text). No sibling texts, no cannibalization lookup, no post-gate integrity / cut steps."""
    from services.acp_content_writing import service as svc

    ctx = copy.deepcopy(context)
    svc._add_date_spans(ctx)
    channel, ba = ctx["channel"], ctx.get("brand_audience") or {}
    facts_text = ctx.get("facts_text") or ""
    spec = svc.channel_spec(channel, ba)
    hyg = svc.hygiene.HygieneContext(seed=ctx["atom_text"], scope=ctx.get("scope"), trip_name=ctx.get("trip_name"),
                                     brand_name=ba.get("brand_name"), cta=ctx.get("cta"),
                                     hashtags_max=spec.hashtags_max if spec else None,
                                     first_line_max=spec.first_line_max if spec else None,
                                     audience=ba.get("customer_segment"), facts=facts_text,
                                     trip_places=tuple(ctx.get("trip_places") or ()))
    trip_ctx = svc.trip_facts.TripFactContext(
        seed=ctx["atom_text"], source=f"{hyg.lexicon.own_text}\n{facts_text}", audience=ba.get("customer_segment"),
        cta=ctx.get("cta"), trip_places=tuple(ctx.get("trip_places") or ()), channel=channel,
        brand_name=ba.get("brand_name"))
    briefs = svc.hygiene.brief_texts(ba, ctx.get("chosen"))
    ctx["writer_trip_name"] = svc.hygiene.scoped_trip_name(ctx.get("trip_name"), ctx.get("scope"), hyg.lexicon)
    openers = ctx.get("sibling_openers") or []
    outcome = await asyncio.to_thread(
        svc.run_quality_gates, content_text=output.get("content_text") or "", atom_text=svc._grounding_text(ctx),
        cta=ctx.get("cta"), brand_rubric_text=ctx.get("brand_rubric_text") or "", channel=channel,
        keyword=ctx.get("keyword"), seo_title=output.get("seo_title"),
        meta_description=output.get("meta_description"), slug=output.get("slug"), cannibalization_match=None,
        brand_pack=ba, hygiene_ctx=hyg, **({"sibling_openers": openers} if openers else {}),
        stock_blocking=True, sibling_texts=[], trip_ctx=trip_ctx, brief_texts=briefs, taken_titles=[],
        f9_passed_sentences=svc.F9PassMemory())
    return outcome


async def t9_score_only(sample: dict) -> tuple[dict, Any]:
    outcome = await t9_gate_replay(sample["input"]["context"], sample["output"])
    ledger = outcome.get("gate_ledger") or []
    first = outcome.get("first_failure") or {}
    # AA-925: the replay holds a piece whose F9 judge was down, as the writer now does (degraded hard)
    deg = degraded_block(t9_degraded(ledger))
    return {**deg, "passed": bool(outcome.get("passed")) and not deg["degraded_hard"],
            "first_failure": first.get("gate"),
            "blocking_failures": _blocking_failures(ledger),
            "flags": [f.get("gate") for f in outcome.get("flags") or [] if isinstance(f, dict)]}, None


# ── one sample ───────────────────────────────────────────────────────────────────────────────────


async def score_one(sample: dict, fixture: dict, mode: str, pool) -> tuple[dict, Any]:
    kind = sample["kind"]
    if kind in ("s1", "injection_s1"):
        return await (s1_regenerate(sample, fixture) if mode == "regenerate" else s1_score_only(sample, fixture))
    if kind == "t2":
        return await (t2_regenerate(sample, fixture) if mode == "regenerate" else t2_score_only(sample, fixture))
    if kind in ("t9", "injection_t9"):
        return await (t9_regenerate(sample, pool) if mode == "regenerate" else t9_score_only(sample))
    raise ValueError(f"unknown sample kind {kind!r}")


def _tenant_of(sample: dict) -> Optional[str]:
    inp = sample.get("input") or {}
    return inp.get("tenant_id") or None


def injection_block(sample: dict, mode: str, verdict: dict, new_output: Any) -> Optional[dict]:
    spec = sample.get("injection_spec")
    if not spec:
        return None
    if mode == "regenerate":
        return {"violations": injection_violations(flatten_text(new_output), spec)}
    return {"caught": not verdict.get("passed")}


def _row(sample: dict, run_id: str, mode: str) -> dict:
    label = sample.get("label") or {}
    return {"sample_id": sample["sample_id"], "stratum": sample["stratum"], "kind": sample["kind"],
            "channel": sample.get("channel"), "country": sample.get("country"), "group": sample.get("group"),
            "tags": sample.get("tags") or [], "job_id": sample_job_id(run_id, sample["sample_id"]),
            "label_verdict": label.get("verdict"), "label_source": sample.get("label_source") or label.get("source"),
            "label_strength": label.get("strength"), "post_edit": bool(sample.get("output_post_edit")),
            "frozen_pipeline_passed": (sample.get("pipeline_verdict") or {}).get("passed")}


# ── run ──────────────────────────────────────────────────────────────────────────────────────────


async def _pool():
    import asyncpg
    from urllib.parse import urlparse

    from shared.secrets import get_database_url

    u = urlparse(get_database_url().strip())
    return await asyncpg.create_pool(host=u.hostname, port=u.port or 5432, user=u.username, password=u.password,
                                     database=u.path.lstrip("/"), ssl="require", min_size=2, max_size=8)


def _calls_by_job(rows) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for r in rows:
        d = out.setdefault(r["job_id"], {"calls": {}, "by_stage": {}, "cost_usd": 0.0, "llm_rows": 0})
        d["calls"][r["role"]] = d["calls"].get(r["role"], 0) + r["n"]
        d["by_stage"][r["real_stage"] or "?"] = d["by_stage"].get(r["real_stage"] or "?", 0) + r["n"]
        d["cost_usd"] = round(d["cost_usd"] + (r["usd"] or 0.0), 6)
        if r["provider"] != "typesafe" and r["role"] in ("writer", "judge", "validate"):
            d["llm_rows"] += r["n"]
    return out


LLM_ROLES = ("writer", "judge", "validate")


def _is_llm_row(r) -> bool:
    """A row the gateway's LLMClient.generate() counter must match: an LLM role, not Jev (typesafe), not a
    gateway-attempt row (billed attempts the gateway discarded — the caller never saw them)."""
    return r["provider"] != "typesafe" and r["role"] in LLM_ROLES and not r.get("gateway_attempt")


def reconcile(stage_rows, spend: dict) -> dict:
    """AA-927: calls made in-process vs llm_call_log rows, per real stage, SIGNED (over-logging shows as a
    positive diff, under-logging as negative), plus the cost of the same filter on both sides.
    `stage_rows` = `_STAGE_SQL` rows; `spend` = `evalpatch.SPEND.snapshot()`."""
    rows = [dict(r) for r in stage_rows]
    calls = {k: v["responses"] for k, v in (spend.get("by_stage") or {}).items()}
    logged: dict[str, int] = {}
    logged_usd = 0.0
    for r in rows:
        if _is_llm_row(r):
            st = r["real_stage"] or "?"
            logged[st] = logged.get(st, 0) + r["n"]
            logged_usd += r["usd"] or 0.0
    stages = sorted(set(calls) | set(logged))
    diff = {st: logged.get(st, 0) - calls.get(st, 0) for st in stages}
    local_usd = float(spend.get("usd") or 0.0)
    gateway = [r for r in rows if r.get("gateway_attempt")]
    embed_rows = [r for r in rows if r["role"] == "embed"]
    return {
        "local_llm_calls_by_stage": {st: calls.get(st, 0) for st in stages},
        "provider_requests_by_stage": {k: v["provider_requests"] for k, v in (spend.get("by_stage") or {}).items()},
        "logged_llm_rows_by_stage": {st: logged.get(st, 0) for st in stages},
        "rows_minus_calls_by_stage": diff,
        "rows_minus_calls_nonzero": {st: d for st, d in diff.items() if d},
        "gateway_attempt_rows": sum(r["n"] for r in gateway),
        "gateway_attempt_usd": round(sum(r["usd"] or 0.0 for r in gateway), 6),
        "local_llm_usd": round(local_usd, 6),
        "logged_llm_usd": round(logged_usd, 6),
        "cost_diff_usd": round(logged_usd - local_usd, 6),
        "local_embed_calls": int(spend.get("embed_calls") or 0),
        "logged_embed_rows": sum(r["n"] for r in embed_rows),
    }


async def run(args) -> dict:
    from scripts.aa729 import evalpatch
    from scripts.aa729.common import load_json, save_output
    from shared.llm_client import call_log

    fixture = load_json(args.fixture)
    checkpoint = load_json(args.resume_file or f"s3:{S3_PREFIX}/{args.resume}/score-{args.mode}.partial.json") \
        if (args.resume or args.resume_file) else None
    run_id = checkpoint["run_id"] if checkpoint else str(uuid.uuid4())
    started = datetime.fromisoformat(checkpoint["started_at"]) if checkpoint else datetime.now(timezone.utc)
    if checkpoint and checkpoint.get("fixture_version") != fixture.get("fixture_version"):
        raise SystemExit("resume: fixture version differs from the stopped run")
    since = started - timedelta(minutes=5)  # index range on created_at; the run id is the real filter
    results: dict[str, dict] = {r["sample_id"]: r for r in (checkpoint or {}).get("samples", [])}
    todo = plan(fixture["samples"], strata=set(args.strata.split(",")) if args.strata else None,
                ids=set(args.sample_ids.split(",")) if args.sample_ids else None, limit=args.limit,
                done=set(results))
    evalpatch.install(run_id)
    evalpatch.BUDGET.configure(args.max_cost_usd, slots=max(1, args.concurrency))
    pool = await _pool()
    guard = CostGuard(cap_usd=args.max_cost_usd)
    stopped: dict = {"reason": None}
    out_key = f"{S3_PREFIX}/{run_id}/score-{args.mode}"

    async def logged_spend() -> float:
        async with pool.acquire() as conn:
            usd = float(await conn.fetchval(_SPEND_SQL, since, run_id) or 0.0)
        evalpatch.BUDGET.note_logged(usd)
        return usd

    async def refresh_spend() -> None:
        while True:
            await asyncio.sleep(SPEND_REFRESH_S)
            try:
                await logged_spend()
            except Exception as e:  # noqa: BLE001 — the in-process number still guards the next call
                print(f"spend refresh failed: {e}", flush=True)

    def snapshot(final: bool) -> dict:
        return {"schema": SCORE_SCHEMA, "run_id": run_id, "mode": args.mode,
                "fixture_version": fixture.get("fixture_version"), "fixture_seed": fixture.get("seed"),
                "started_at": started.isoformat(), "updated_at": datetime.now(timezone.utc).isoformat(),
                "final": final, "cap_usd": args.max_cost_usd, "stopped": stopped["reason"],
                "planned": [s["sample_id"] for s in todo], "samples": list(results.values())}

    try:
        if not args.allow_busy_queue:
            async with pool.acquire() as conn:
                busy = await conn.fetchval(_BUSY_SQL)
            if busy:
                raise SystemExit(f"{busy} job(s) queued/running in shared.job — paid runs wait for an idle "
                                 "queue (lessons); pass --allow-busy-queue to override")
        sem = asyncio.Semaphore(max(1, args.concurrency))
        lock = asyncio.Lock()
        refresher = asyncio.create_task(refresh_spend())

        async def one(sample: dict) -> None:
            async with sem:
                if not stopped["reason"] and evalpatch.BUDGET.tripped:
                    stopped["reason"] = f"cost_cap: {evalpatch.BUDGET.tripped}"
                if stopped["reason"]:
                    return
                sid = sample["sample_id"]
                prior = COST_ESTIMATE[args.mode][kind_of(sample)]
                try:
                    guard.check(sample_id=sid, prior=prior, logged_usd=await logged_spend(),
                                local_usd=evalpatch.SPEND.usd)
                except CostCapReached as e:
                    stopped["reason"] = f"cost_cap: {e}"
                    print(f"STOP {stopped['reason']}", flush=True)
                    return
                row = _row(sample, run_id, args.mode)
                t0 = time.monotonic()
                verdict, new_output = {}, None
                timed_out = False
                try:
                    with call_log.bind_job(row["job_id"]), call_log.bind_tenant(_tenant_of(sample)):
                        verdict, new_output = await asyncio.wait_for(
                            score_one(sample, fixture, args.mode, pool), timeout=PER_SAMPLE_TIMEOUT_S)
                        if verdict.get("degraded_hard") and not evalpatch.BUDGET.tripped:
                            # AA-925: a hard detector failed open (a transient Jev / F9 outage). Retry the sample
                            # ONCE before its stratum is marked INVALID — one blip must not void a $5 run.
                            row["degraded_retry"] = {"first": verdict.get("degraded_hard")}
                            print(f"{sample['sample_id']:10} degraded {verdict.get('degraded_hard')} — retrying once",
                                  flush=True)
                            verdict, new_output = await asyncio.wait_for(
                                score_one(sample, fixture, args.mode, pool), timeout=PER_SAMPLE_TIMEOUT_S)
                except asyncio.TimeoutError:
                    # the S1/T2 graph keeps running in its to_thread worker: keep this sample's reservation
                    # (guard.done below) — the in-call stop still bounds what that thread can spend.
                    timed_out = True
                    row["error"] = f"TimeoutError: no result after {PER_SAMPLE_TIMEOUT_S}s"
                except Exception as e:  # noqa: BLE001 — one sample's failure is a result, not the run's
                    row["error"] = f"{type(e).__name__}: {str(e)[:300]}"
                if row["job_id"] in evalpatch.BUDGET.tripped_jobs:
                    # a model call of this sample was refused at the cap: whatever the path returned is cut
                    # short, so it is an error row, not a verdict
                    row["error"] = row.get("error") or f"CostCapReached: {evalpatch.BUDGET.tripped}"
                    stopped["reason"] = stopped["reason"] or f"cost_cap: {evalpatch.BUDGET.tripped}"
                await asyncio.sleep(SETTLE_S)
                row.update({"verdict": verdict, "seconds": round(time.monotonic() - t0, 1),
                            "injection": injection_block(sample, args.mode, verdict, new_output)})
                if args.mode == "regenerate" and new_output is not None:
                    row["output"] = scrub(new_output)
                async with pool.acquire() as conn:
                    calls = _calls_by_job(await conn.fetch(_CALLS_SQL, since, run_id)).get(row["job_id"], {})
                row.update({"calls": calls.get("calls", {}), "calls_by_stage": calls.get("by_stage", {}),
                            "cost_usd": calls.get("cost_usd", 0.0)})
                guard.done(sid, row["cost_usd"], release=not timed_out)
                async with lock:
                    results[sid] = row
                    save_output(snapshot(False), f"{out_key}.partial.json", upload=not args.no_upload)
                print(f"{sid:10} {row['stratum']:12} passed={verdict.get('passed')} label={row['label_verdict']} "
                      f"cost=${row['cost_usd']:.4f} {row.get('error') or ''}", flush=True)

        try:
            await asyncio.gather(*(one(s) for s in todo))
        finally:
            refresher.cancel()
        await asyncio.sleep(SETTLE_S * 2)
        async with pool.acquire() as conn:
            calls = _calls_by_job(await conn.fetch(_CALLS_SQL, since, run_id))
            job_ids = [r["job_id"] for r in results.values()]
            jev_rows = await conn.fetch(_JEV_SQL, since, job_ids) if job_ids else []
            total = float(await conn.fetchval(_SPEND_SQL, since, run_id) or 0.0)
            stage_rows = await conn.fetch(_STAGE_SQL, since, run_id)
    finally:
        await pool.close()

    jev_by_job: dict[str, list[dict]] = {}
    for r in jev_rows:
        jev_by_job.setdefault(r["job_id"], []).append(dict(r))
    for row in results.values():  # final per-sample cost/calls (late log writes included) + Jev
        c = calls.get(row["job_id"], {})
        if c:
            row.update({"calls": c["calls"], "calls_by_stage": c["by_stage"], "cost_usd": c["cost_usd"]})
        row["jev"] = jev_summary(jev_by_job.get(row["job_id"], []))
    rows = list(results.values())
    strata = sorted({r["stratum"] for r in rows})
    result = snapshot(True)
    result["invalid_strata"] = invalid_strata(rows)
    result.update({
        "metrics": {**{k: stratum_metrics([r for r in rows if r["stratum"] == k], args.mode) for k in strata},
                    "all": stratum_metrics(rows, args.mode)},
        "totals": {"cost_usd_llm_call_log": round(total, 6), "local_llm_usd": round(evalpatch.SPEND.usd, 6),
                   "local_llm_calls": evalpatch.SPEND.calls,
                   "cost_source": f"shared.llm_call_log stage={STAGE} quality_signal.aa729_run={run_id}"},
    })
    rec = reconcile(stage_rows, evalpatch.SPEND.snapshot())
    result["totals"].update(rec)
    result["totals"]["logged_llm_rows"] = sum(rec["logged_llm_rows_by_stage"].values())
    # kept for continuity with S245 results; AA-927 Done-when reads the SIGNED per-stage diff + cost_diff_usd
    result["totals"]["unlogged_llm_calls"] = max(result["totals"]["local_llm_calls"]
                                                 - result["totals"]["logged_llm_rows"], 0)
    save_output(result, f"{out_key}.json", upload=not args.no_upload)
    return result


def invalid_strata(rows: list[dict]) -> dict[str, list[str]]:
    """AA-925 (design §6.3): a stratum with a sample whose hard detector still failed open after its one retry
    is INVALID — its numbers say nothing about quality. {stratum: [sample ids]}; report.py refuses deltas for
    those strata only."""
    out: dict[str, list[str]] = {}
    for r in rows:
        if (r.get("verdict") or {}).get("degraded_hard") and not r.get("error"):
            out.setdefault(r["stratum"], []).append(r["sample_id"])
    return {k: sorted(v) for k, v in sorted(out.items())}


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--fixture", required=True, help="local path or s3:<key> in the bronze bucket")
    ap.add_argument("--mode", choices=("score-only", "regenerate"), required=True)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--strata", help="comma list of s1,t2,t9_blog,t9_linkedin,injection")
    ap.add_argument("--sample-ids", help="comma list of sample ids")
    ap.add_argument("--max-cost-usd", type=float, default=DEFAULT_CAP_USD)
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--resume", help="run id of a stopped run (reads its S3 checkpoint)")
    ap.add_argument("--resume-file", help="local checkpoint path instead of S3")
    ap.add_argument("--estimate", action="store_true", help="print the plan + estimated cost, call nothing")
    ap.add_argument("--allow-busy-queue", action="store_true")
    ap.add_argument("--no-upload", action="store_true")
    a = ap.parse_args(argv)
    if a.estimate:
        from scripts.aa729.common import load_json

        fx = load_json(a.fixture)
        todo = plan(fx["samples"], strata=set(a.strata.split(",")) if a.strata else None,
                    ids=set(a.sample_ids.split(",")) if a.sample_ids else None, limit=a.limit)
        print(json.dumps({"fixture_version": fx.get("fixture_version"), "mode": a.mode, "cap_usd": a.max_cost_usd,
                          **estimate(todo, a.mode), "sample_ids": [s["sample_id"] for s in todo]}, indent=1))
        return 0
    result = asyncio.run(run(a))
    print(json.dumps({"run_id": result["run_id"], "stopped": result["stopped"], "totals": result["totals"],
                      "invalid_strata": result.get("invalid_strata"),
                      "metrics_all": result["metrics"]["all"]}, indent=1, default=str), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
