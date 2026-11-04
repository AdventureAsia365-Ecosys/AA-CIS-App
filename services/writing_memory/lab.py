"""AA-1039 R6 slice 3 — Writing Lab: one read per pipeline stage over the EXISTING learning tables (root ADR 0019 d13,
clarification TL-13 `regroup_lab`). Read-only, $0: no LLM, no job, no write, and no new learning logic (the S262 TL-11
freeze on the learning loop stays; it is lifted per stage later).

A Lab stage is a writing-memory writer (the four values every learning table CHECKs: a1, t2, t9_blog, t9_social). One
call answers five sections, each read on its own so a missing table or a failing read empties only that section and
says so (`sections[name]`):

  samples      the stage's items of the active golden sets (``shared.eval_golden_item`` x ``eval_golden_set``):
               counts per split and kind, and the newest items as ids, tiers and labels only (never the input,
               output or label text: those columns hold tenant text, and the logged ai_ops read of tenant text is R2's)
  labels       how the samples and the writing signals are labelled: golden label verdict / source, and the signals'
               fact status, reason code, action and actor (``shared.writing_signal``, counts only)
  experiments  the stage's evaluation runs (``shared.prompt_eval_runs``), its release shadow runs grouped by run
               (``shared.writing_memory_shadow``) and the model shadow comparisons of its prompt stages over 30 days
               (``shared.llm_shadow_log``, counts and cost only)
  prompts      the prompt register rows of its prompt stages (``shared.prompt_version``: hash and path, no text)
  gate         the eval gate as the release machinery sees it: the memory switch (``writing_memory_writer``), the
               active and newest platform releases, releases per status, tenant releases live, rules per status, the
               newest finished evaluation run OF THE ACTIVE RELEASE (`active_eval`, what the state reads), the newest
               finished evaluation run of any release (`last_eval`, for context only: a rejected candidate's failed r1
               is the gate working, not the live release regressing), and one plain state (`gate_state`)

Every SQL here names only columns of docs/architecture/db-schema-reference.md (live dump 04/11/2026).
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Awaitable, Callable, Optional

import asyncpg
import structlog

logger = structlog.get_logger()

WRITERS = ("a1", "t2", "t9_blog", "t9_social")

# The prompt register stages (shared/llm_client/prompt_versions.json) that make up each writer's stage. A1 is the S1
# master rewrite and its repair / audit passes; T2 the tenant rewrite; T9 the article / social writer with its angle
# step (T8), repair and FAQ passes and its T10 judges (one T9 writer per channel family, the same prompts).
PROMPT_STAGES: dict[str, tuple[str, ...]] = {
    "a1": ("s1_generate", "s1_judge", "s1_brand_audit", "s1_flag_fix", "s1_itinerary_nudge", "s1_source_facts"),
    "t2": ("t2_generate", "t2_judge", "t2_brand_audit", "t2_flag_fix", "t2_itinerary_nudge"),
    "t9_blog": ("t8_angle_gen", "t9_write", "t9_repair", "t9_faq_filter", "t9_faq_answer_fit", "t10_judge",
                "t10_claims", "t10_adjudicate"),
    "t9_social": ("t8_angle_gen", "t9_write", "t9_repair", "t9_faq_filter", "t9_faq_answer_fit", "t10_judge",
                  "t10_claims", "t10_adjudicate"),
}
# The legacy S1 regression runs (pipeline s1_old / s1_from_atom, writer NULL) are A1's evaluation history too.
LEGACY_PIPELINES: dict[str, tuple[str, ...]] = {"a1": ("s1_old", "s1_from_atom")}

SAMPLE_LIMIT = 50
EVAL_RUN_LIMIT = 30
SHADOW_RUN_LIMIT = 20
PROMPT_LIMIT = 100
MODEL_SHADOW_DAYS = 30
SECTIONS = ("samples", "labels", "experiments", "prompts", "gate")

# ── SQL ─────────────────────────────────────────────────────────────────────────────────────────────────────────────

GOLDEN_SETS_SQL = """
    SELECT s.set_id::text AS set_id, s.name, s.version, s.created_at, count(i.item_id) AS items
      FROM shared.eval_golden_set s
      JOIN shared.eval_golden_item i ON i.set_id = s.set_id AND i.writer = $1
     WHERE s.status = 'active'
     GROUP BY s.set_id, s.name, s.version, s.created_at
     ORDER BY s.name
"""
GOLDEN_COUNTS_SQL = """
    SELECT i.split, i.kind, i.label_verdict, i.label_source, count(*) AS n
      FROM shared.eval_golden_item i
      JOIN shared.eval_golden_set s ON s.set_id = i.set_id AND s.status = 'active'
     WHERE i.writer = $1
     GROUP BY 1, 2, 3, 4
"""
GOLDEN_ITEMS_SQL = """
    SELECT i.item_id::text AS item_id, i.sample_id, s.name AS set_name, i.split, i.kind, i.trap, i.tiers, i.channel,
           i.scope, i.country, i.days, i.post_edit, i.label_verdict, i.label_source, i.pick_reason, i.created_at
      FROM shared.eval_golden_item i
      JOIN shared.eval_golden_set s ON s.set_id = i.set_id AND s.status = 'active'
     WHERE i.writer = $1
     ORDER BY i.created_at DESC, i.sample_id
     LIMIT $2
"""
SIGNAL_COUNTS_SQL = """
    SELECT fact_status, reason_code, action, actor_type, scope,
           count(*) AS n, count(*) FILTER (WHERE created_at >= now() - interval '30 days') AS n30
      FROM shared.writing_signal
     WHERE writer = $1
     GROUP BY 1, 2, 3, 4, 5
"""
EVAL_RUNS_SQL = """
    SELECT id::text AS id, pipeline, run_kind, prompt_version, baseline_prompt_version, tour_count,
           avg_quality_score::float AS avg_quality_score, gate_pass_count, gate_fail_count,
           cost_usd::float AS cost_usd, cost_cap_usd::float AS cost_cap_usd, regression_detected, verdict, stop_reason,
           judge_kappa::float AS judge_kappa, release_id::text AS release_id, golden_set_id::text AS golden_set_id,
           tiers, created_at, finished_at
      FROM shared.prompt_eval_runs
     WHERE writer = $1 OR (writer IS NULL AND pipeline = ANY($2::text[]))
     ORDER BY created_at DESC
     LIMIT $3
"""
SHADOW_RUNS_SQL = """
    SELECT run_id::text AS run_id, release_id::text AS release_id, count(*) AS items,
           count(*) FILTER (WHERE verdict = 'pass') AS pass, count(*) FILTER (WHERE verdict = 'fail') AS fail,
           count(*) FILTER (WHERE verdict = 'error') AS errors, count(*) FILTER (WHERE probe) AS probes,
           coalesce(sum(cost_usd), 0)::float AS cost_usd, min(created_at) AS started_at, max(created_at) AS last_at
      FROM shared.writing_memory_shadow
     WHERE writer = $1
     GROUP BY run_id, release_id
     ORDER BY max(created_at) DESC
     LIMIT $2
"""
MODEL_SHADOW_SQL = """
    SELECT stage, primary_model, shadow_model, count(*) AS calls,
           count(*) FILTER (WHERE shadow_error IS NOT NULL) AS errors,
           coalesce(sum(primary_cost_usd), 0)::float AS primary_cost_usd,
           coalesce(sum(shadow_cost_usd), 0)::float AS shadow_cost_usd,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY shadow_latency_ms)::float AS shadow_p50_ms,
           max(created_at) AS last_at
      FROM shared.llm_shadow_log
     WHERE stage = ANY($1::text[]) AND created_at >= now() - make_interval(days => $2)
     GROUP BY stage, primary_model, shadow_model
     ORDER BY max(created_at) DESC
"""
PROMPTS_SQL = """
    SELECT stage, version, prompt_sha256, source_path, notes, created_at, created_by
      FROM shared.prompt_version
     WHERE stage = ANY($1::text[])
     ORDER BY stage, created_at DESC, version
     LIMIT $2
"""
SWITCH_SQL = """
    SELECT memory_enabled, train_closed_month, updated_at FROM shared.writing_memory_writer WHERE writer = $1
"""
_RELEASE_COLS = """
    release_id::text AS release_id, release_no, scope, train_kind, content_class, status,
    cardinality(rule_ids) AS rules, r0_run_id::text AS r0_run_id, eval_run_id::text AS eval_run_id,
    shadow_run_id::text AS shadow_run_id, reverify_run_id::text AS reverify_run_id, reverify, reverify_reasons,
    approved_at, activated_at, retired_at,
    created_at, updated_at
"""
ACTIVE_RELEASE_SQL = f"""
    SELECT {_RELEASE_COLS} FROM shared.writing_memory_release
     WHERE writer = $1 AND scope = 'platform' AND status = 'active'
     ORDER BY activated_at DESC NULLS LAST LIMIT 1
"""
LATEST_RELEASE_SQL = f"""
    SELECT {_RELEASE_COLS} FROM shared.writing_memory_release
     WHERE writer = $1 AND scope = 'platform'
     ORDER BY created_at DESC LIMIT 1
"""
RELEASE_COUNTS_SQL = """
    SELECT scope, status, count(*) AS n FROM shared.writing_memory_release WHERE writer = $1 GROUP BY 1, 2
"""
RULE_COUNTS_SQL = "SELECT status, count(*) AS n FROM shared.writing_rule WHERE writer = $1 GROUP BY 1"
_GATE_EVAL_COLS = """
    id::text AS id, run_kind, verdict, stop_reason, regression_detected, cost_usd::float AS cost_usd,
    tour_count, release_id::text AS release_id, created_at, finished_at
"""
# The writer's newest finished evaluation run of ANY release (or none): context on the page, never the gate state.
LAST_EVAL_SQL = f"""
    SELECT {_GATE_EVAL_COLS}
      FROM shared.prompt_eval_runs
     WHERE writer = $1 AND finished_at IS NOT NULL AND run_kind IN ('r1', 'r2', 'reverify', 'shadow')
     ORDER BY finished_at DESC LIMIT 1
"""
# The newest finished evaluation run of the ACTIVE release ($2): a run the release stores as its evidence (eval /
# shadow / reverify run ids, $3 — this also covers release 0's baseline import, which has no release_id), or an r1 /
# r2 / reverify / shadow run whose release_id is the active release's. An r2 run counts only as releases.py
# `release_link_problem` counts it: not a seeded-fault operator check, not a code-train run (`candidate: current`).
ACTIVE_EVAL_SQL = f"""
    SELECT {_GATE_EVAL_COLS}
      FROM shared.prompt_eval_runs
     WHERE writer = $1 AND finished_at IS NOT NULL
       AND (id = ANY($3::uuid[])
            OR (release_id = $2::uuid AND run_kind IN ('r1', 'r2', 'reverify', 'shadow')
                AND NOT (run_kind = 'r2'
                         AND (coalesce(details->>'seeded_fault', '') NOT IN ('', 'false')
                              OR coalesce(details->>'candidate', 'release') <> 'release'))))
     ORDER BY finished_at DESC LIMIT 1
"""

# ── pure helpers ────────────────────────────────────────────────────────────────────────────────────────────────────


def _iso(v: Any) -> Any:
    return v.isoformat() if isinstance(v, (datetime, date)) else v


def row_out(r: dict) -> dict:
    """A DB row for JSON: dates and timestamps as ISO text, everything else as read. Pure."""
    return {k: _iso(v) for k, v in r.items()}


def tally(rows: list[dict], key: str, count: str = "n") -> dict[str, int]:
    """Sum `count` per value of `key` (NULL counted as "none"), largest first. Pure."""
    out: dict[str, int] = {}
    for r in rows:
        k = r.get(key) or "none"
        out[k] = out.get(k, 0) + int(r.get(count) or 0)
    return dict(sorted(out.items(), key=lambda kv: (-kv[1], kv[0])))


def prompt_row(r: dict) -> dict:
    """One prompt register row: the hash shortened for the page (the full one stays). Pure."""
    sha = r.get("prompt_sha256") or ""
    return {**row_out(r), "sha_short": sha[:12]}


def evidence_run_ids(release: Optional[dict]) -> list[str]:
    """The evaluation runs a release stores as its own evidence (eval, shadow, reverify; not the per-writer r0
    replay). Pure."""
    if not release:
        return []
    return [str(release[k]) for k in ("eval_run_id", "shadow_run_id", "reverify_run_id") if release.get(k)]


def gate_state(*, switch: Optional[dict], active: Optional[dict], active_eval: Optional[dict]) -> dict:
    """One plain state of the stage's eval gate from what the release machinery already stores (no new rule: the
    activation checks stay in releases.py and the CIS 287 guard). Pure.

    `active_eval` is the newest finished evaluation run OF THE ACTIVE RELEASE (ACTIVE_EVAL_SQL), never the writer's
    newest run of any release: a candidate release failing its r1 / r2 is the gate doing its job, while the live
    release keeps passing.

      no_release   no active platform release: the writer runs without memory (its base prompt)
      memory_off   a platform release is active but the writer's memory switch is off
      reverify     the active release is flagged for reverification (golden set, model route, gate taxonomy or
                   prompt version changed); it keeps serving until reverified
      regression   the active release's newest finished evaluation run failed or saw a regression
      passing      an active release, memory on, and its newest finished evaluation run did not fail
    """
    if not active:
        return {"code": "no_release", "ok": False}
    if not (switch or {}).get("memory_enabled"):
        return {"code": "memory_off", "ok": False}
    if active.get("reverify"):
        return {"code": "reverify", "ok": False, "reasons": list(active.get("reverify_reasons") or [])}
    if active_eval and (active_eval.get("verdict") == "fail" or active_eval.get("regression_detected")):
        return {"code": "regression", "ok": False}
    return {"code": "passing", "ok": True}


# ── reads ───────────────────────────────────────────────────────────────────────────────────────────────────────────


async def _section(pool, name: str, reader: Callable[[Any], Awaitable[dict]]) -> tuple[dict, dict]:
    """Run one section's reads on its own connection. Missing table / column (code before its migration): not ready,
    no error. Any other failure: not ready, a short error for the page, logged."""
    try:
        async with pool.acquire() as conn:
            return await reader(conn), {"ready": True, "error": None}
    except (asyncpg.exceptions.UndefinedTableError, asyncpg.exceptions.UndefinedColumnError):
        return {}, {"ready": False, "error": None}
    except Exception as e:  # noqa: BLE001 — one section must not take the stage page down
        logger.warning("aa1039_writing_lab_section_failed", section=name, error=f"{type(e).__name__}: {str(e)[:200]}")
        return {}, {"ready": False, "error": f"{name} could not be read"}


async def _samples(conn, writer: str) -> dict:
    sets = [row_out(dict(r)) for r in await conn.fetch(GOLDEN_SETS_SQL, writer)]
    counts = [dict(r) for r in await conn.fetch(GOLDEN_COUNTS_SQL, writer)]
    items = [row_out(dict(r)) for r in await conn.fetch(GOLDEN_ITEMS_SQL, writer, SAMPLE_LIMIT)]
    return {"golden_sets": [{**s, "items": int(s["items"])} for s in sets],
            "total": sum(int(c["n"]) for c in counts),
            "by_split": tally(counts, "split"), "by_kind": tally(counts, "kind"),
            "items": items, "limit": SAMPLE_LIMIT}


async def _labels(conn, writer: str) -> dict:
    golden = [dict(r) for r in await conn.fetch(GOLDEN_COUNTS_SQL, writer)]
    sig = [dict(r) for r in await conn.fetch(SIGNAL_COUNTS_SQL, writer)]
    return {"golden": {"by_verdict": tally(golden, "label_verdict"), "by_source": tally(golden, "label_source")},
            "signals": {"total": sum(int(r["n"]) for r in sig), "last_30d": sum(int(r["n30"]) for r in sig),
                        "by_fact_status": tally(sig, "fact_status"), "by_reason_code": tally(sig, "reason_code"),
                        "by_action": tally(sig, "action"), "by_actor_type": tally(sig, "actor_type"),
                        "by_scope": tally(sig, "scope")}}


async def _experiments(conn, writer: str) -> dict:
    runs = [row_out(dict(r)) for r in await conn.fetch(EVAL_RUNS_SQL, writer, list(LEGACY_PIPELINES.get(writer, ())),
                                                       EVAL_RUN_LIMIT)]
    shadow = [row_out(dict(r)) for r in await conn.fetch(SHADOW_RUNS_SQL, writer, SHADOW_RUN_LIMIT)]
    model = [row_out(dict(r)) for r in await conn.fetch(MODEL_SHADOW_SQL, list(PROMPT_STAGES[writer]),
                                                        MODEL_SHADOW_DAYS)]
    for r in shadow:
        for k in ("items", "pass", "fail", "errors", "probes"):
            r[k] = int(r[k] or 0)
    for r in model:
        r["calls"], r["errors"] = int(r["calls"] or 0), int(r["errors"] or 0)
    return {"eval_runs": runs, "shadow_runs": shadow, "model_shadow": model,
            "model_shadow_days": MODEL_SHADOW_DAYS, "limits": {"eval_runs": EVAL_RUN_LIMIT,
                                                               "shadow_runs": SHADOW_RUN_LIMIT}}


async def _prompts(conn, writer: str) -> dict:
    stages = list(PROMPT_STAGES[writer])
    rows = [prompt_row(dict(r)) for r in await conn.fetch(PROMPTS_SQL, stages, PROMPT_LIMIT)]
    return {"stages": stages, "versions": rows, "limit": PROMPT_LIMIT}


async def _gate(conn, writer: str) -> dict:
    sw = await conn.fetchrow(SWITCH_SQL, writer)
    switch = row_out(dict(sw)) if sw else None
    active = await conn.fetchrow(ACTIVE_RELEASE_SQL, writer)
    latest = await conn.fetchrow(LATEST_RELEASE_SQL, writer)
    rel_counts = [dict(r) for r in await conn.fetch(RELEASE_COUNTS_SQL, writer)]
    rule_counts = [dict(r) for r in await conn.fetch(RULE_COUNTS_SQL, writer)]
    last = await conn.fetchrow(LAST_EVAL_SQL, writer)
    active_d = row_out(dict(active)) if active else None
    last_d = row_out(dict(last)) if last else None
    active_eval = None
    if active_d:
        ae = await conn.fetchrow(ACTIVE_EVAL_SQL, writer, active_d["release_id"], evidence_run_ids(active_d))
        active_eval = row_out(dict(ae)) if ae else None
    if last_d:
        last_d["for_active"] = bool(active_d) and last_d.get("release_id") == active_d["release_id"]
    return {"switch": switch, "active_release": active_d,
            "latest_release": row_out(dict(latest)) if latest else None,
            "releases": tally([r for r in rel_counts if r["scope"] == "platform"], "status"),
            "tenant_releases_active": sum(int(r["n"]) for r in rel_counts
                                          if r["scope"] == "tenant" and r["status"] == "active"),
            "rules": tally(rule_counts, "status"), "active_eval": active_eval, "last_eval": last_d,
            "state": gate_state(switch=switch, active=active_d, active_eval=active_eval)}


_READERS: dict[str, Callable[[Any, str], Awaitable[dict]]] = {
    "samples": _samples, "labels": _labels, "experiments": _experiments, "prompts": _prompts, "gate": _gate,
}


async def stage_view(pool, writer: str) -> dict:
    """The Writing Lab page of one stage. `writer` must be one of WRITERS (the router answers 404 otherwise)."""
    if writer not in WRITERS:
        raise ValueError(f"unknown writer {writer!r}")
    out: dict[str, Any] = {"writer": writer, "prompt_stages": list(PROMPT_STAGES[writer]), "sections": {}}
    for name in SECTIONS:
        reader = _READERS[name]
        data, status = await _section(pool, name, lambda conn, r=reader: r(conn, writer))
        out[name] = data or None
        out["sections"][name] = status
    return out
