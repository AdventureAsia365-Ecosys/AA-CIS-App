"""AA-900 UI (slice C4 of the S262 build plan) — the read-only Experiments section of AI providers › Models & trials.

``GET /admin/ai-gateway/experiments`` answers two lists, each read on its own (a failing or missing one leaves only
its list empty and says so):

  prompt_versions  ``shared.prompt_version`` (CIS migration 321): the prompt register, one row per (stage, version)
                   with the sha256 of the prompt file. It never holds prompt text. Before 321: ``ready`` False.
  eval_runs        the newest ``shared.prompt_eval_runs`` rows (regression, writing-memory releases, shadow, probe):
                   what was compared, on how many tours, the verdict and the cost.

The shadow comparisons of real calls already have their own read (``GET /admin/llm-shadow/report``), shown next to
these lists. Database only, $0; nothing is written and no provider is called.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

import asyncpg

PROMPT_VERSION_LIMIT = 200
EVAL_RUN_LIMIT = 50

PROMPT_VERSIONS_SQL = """
    SELECT stage, version, prompt_sha256, source_path, notes, created_at, created_by
      FROM shared.prompt_version
     ORDER BY stage, created_at DESC, version
     LIMIT $1
"""

EVAL_RUNS_SQL = """
    SELECT id::text AS id, pipeline, prompt_version, baseline_prompt_version, run_kind, writer, tour_count,
           avg_quality_score::float AS avg_quality_score,
           baseline_avg_quality_score::float AS baseline_avg_quality_score,
           gate_pass_count, gate_fail_count, cost_usd::float AS cost_usd, cost_cap_usd::float AS cost_cap_usd,
           regression_detected, verdict, stop_reason, judge_kappa::float AS judge_kappa, created_at, finished_at
      FROM shared.prompt_eval_runs
     ORDER BY created_at DESC
     LIMIT $1
"""


def _iso(v: Any) -> Any:
    return v.isoformat() if isinstance(v, datetime) else v


def prompt_version_row(r: dict) -> dict:
    """One register row for the page: the hash shortened to 12 characters (the full one stays in the API). Pure."""
    sha = r.get("prompt_sha256") or ""
    return {"stage": r["stage"], "version": r["version"], "prompt_sha256": sha, "sha_short": sha[:12],
            "source_path": r.get("source_path"), "notes": r.get("notes"), "created_at": _iso(r.get("created_at")),
            "created_by": r.get("created_by")}


def eval_run_row(r: dict) -> dict:
    """One evaluation run for the page (timestamps as ISO text). Pure."""
    return {k: _iso(v) for k, v in r.items()}


async def _read(pool, sql: str, limit: int) -> tuple[bool, list[dict], Optional[str]]:
    try:
        async with pool.acquire() as conn:
            return True, [dict(r) for r in await conn.fetch(sql, limit)], None
    except (asyncpg.exceptions.UndefinedTableError, asyncpg.exceptions.UndefinedColumnError):
        return False, [], None


async def read(pool) -> dict:
    from api.services.ai_gateway import _guard

    (pv, pv_err), (er, er_err) = (
        await _guard("Prompt register", lambda: _read(pool, PROMPT_VERSIONS_SQL, PROMPT_VERSION_LIMIT)),
        await _guard("Evaluation runs", lambda: _read(pool, EVAL_RUNS_SQL, EVAL_RUN_LIMIT)),
    )
    pv_ready, pv_rows, _ = pv or (False, [], None)
    er_ready, er_rows, _ = er or (False, [], None)
    return {
        "prompt_versions": {"ready": pv_ready and pv_err is None, "rows": [prompt_version_row(r) for r in pv_rows],
                            "limit": PROMPT_VERSION_LIMIT, "error": pv_err},
        "eval_runs": {"ready": er_ready and er_err is None, "rows": [eval_run_row(r) for r in er_rows],
                      "limit": EVAL_RUN_LIMIT, "error": er_err},
    }
