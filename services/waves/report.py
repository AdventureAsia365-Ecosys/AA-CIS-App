"""AA-727 — rerun wave report and completeness check.

A wave (shared.rerun_wave, migration 217) only stores the expected tour set and a time window
(created_at → closed_at or now). Every tour's state is derived from rows the pipeline already
writes, so no job has to carry a wave id:

  jobs        shared.job of kind s1_rewrite / a3_atomize / revalidate whose payload tour_id is in
              the expected set and that were created inside the window
  master      published_tours.master_status = 'active' AND updated_at inside the window (the last
              publish/promote; published_at is the FIRST publish and stays put on a rewrite — AA-645 d06,
              migration 273)
  review      latest review_queue row of the tour created inside the window, status 'pending'
  trashed     raw_tours.source_status = 'trashed' (or soft-deleted)
  Jev         a decision_log row without error written by one of the tour's wave jobs
              (call counts are not evidence — Jev fails open)

Tour state, first match wins (TERMINAL ones end a wave):
  master → review_pending → trashed → running (a wave job still queued/running)
  → failed / interrupted (latest s1_rewrite failed|stopped_budget / cancelled)
  → unclassified (S1 succeeded but neither Master nor a pending review — e.g. a review already
    approved/dismissed but not published, or Master set inactive) → not_started.

Known limit: tours sent through the Bedrock Batch path (`s1_batch_ingest`, one job for many tours,
no payload tour_id) have no per-tour job here — their state still resolves from Master/review.
"""
from __future__ import annotations

import json
from typing import Optional

import structlog

logger = structlog.get_logger()

WAVE_JOB_KINDS = ("s1_rewrite", "a3_atomize", "revalidate")
TERMINAL_STATES = ("master", "review_pending", "trashed", "failed", "interrupted")
STATES = TERMINAL_STATES + ("running", "unclassified", "not_started")
MASTER_TENANT_ID = "00000000-0000-0000-0000-000000000001"
INCOMPLETE_EVENT = "platform.rerun_wave.incomplete"

_JOBS_CTE = """
    w AS (SELECT * FROM shared.rerun_wave WHERE id = $1::uuid),
    jobs AS (
        SELECT j.id, j.kind, j.status, j.created_at, j.error, j.payload->>'tour_id' AS tour_id
          FROM shared.job j, w
         WHERE j.kind = ANY($2::text[])
           AND j.created_at >= w.created_at
           AND (w.closed_at IS NULL OR j.created_at <= w.closed_at)
           AND j.payload->>'tour_id' = ANY(w.expected_tour_ids::text[])
    )
"""

TOURS_SQL = f"""
    WITH {_JOBS_CTE},
    exp AS (SELECT DISTINCT unnest(w.expected_tour_ids) AS tour_id FROM w),
    jev_jobs AS (
        SELECT DISTINCT d.job_id FROM shared.decision_log d, w
         WHERE d.created_at >= w.created_at AND d.error IS NULL
           AND d.job_id IN (SELECT id FROM jobs)
    )
    SELECT e.tour_id::text AS tour_id,
           rt.src_name, rt.country,
           rt.source_status::text AS source_status,
           (rt.deleted_at IS NOT NULL) AS deleted,
           length(coalesce(rt.src_itineraries, '')) AS src_chars,
           s1.status AS s1_status, left(s1.error, 300) AS s1_error,
           EXISTS (SELECT 1 FROM jobs j WHERE j.tour_id = e.tour_id::text
                                          AND j.status IN ('queued', 'running')) AS any_active,
           EXISTS (SELECT 1 FROM jobs j WHERE j.tour_id = e.tour_id::text
                                          AND j.kind = 'a3_atomize' AND j.status = 'succeeded') AS atomized,
           EXISTS (SELECT 1 FROM jobs j JOIN jev_jobs jj ON jj.job_id = j.id
                    WHERE j.tour_id = e.tour_id::text) AS jev,
           pt.master_status::text AS master_status,
           -- updated_at, not published_at: a re-publish keeps the first published_at (AA-645 d06, mig 273)
           coalesce(pt.updated_at >= w.created_at
                    AND (w.closed_at IS NULL OR pt.updated_at <= w.closed_at), false) AS published_in_wave,
           rq.review_status::text AS review_status, rq.failure_summary, rq.score_overall::float AS score
      FROM exp e CROSS JOIN w
      LEFT JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = e.tour_id
      LEFT JOIN gold_aa_internal.published_tours pt
             ON pt.tour_id = e.tour_id AND pt.tenant_id = $3::uuid AND pt.deleted_at IS NULL
      LEFT JOIN LATERAL (
            SELECT j.status, j.error FROM jobs j
             WHERE j.tour_id = e.tour_id::text AND j.kind = 's1_rewrite'
             ORDER BY j.created_at DESC LIMIT 1) s1 ON true
      LEFT JOIN LATERAL (
            SELECT q.review_status, q.failure_summary, q.score_overall
              FROM silver_aa_internal.review_queue q
             WHERE q.tour_id = e.tour_id AND q.created_at >= w.created_at
               AND (w.closed_at IS NULL OR q.created_at <= w.closed_at)
             ORDER BY q.created_at DESC LIMIT 1) rq ON true
     ORDER BY rt.src_name NULLS LAST
"""

JOB_TOTALS_SQL = f"""
    WITH {_JOBS_CTE}
    SELECT kind, status, count(*)::int AS n FROM jobs GROUP BY kind, status ORDER BY kind, status
"""

LLM_COST_SQL = f"""
    WITH {_JOBS_CTE}
    SELECT coalesce(l.provider, 'unknown') AS provider, count(*)::int AS calls,
           coalesce(sum(l.cost_usd), 0)::float AS cost_usd
      FROM shared.llm_call_log l, w
     WHERE l.created_at >= w.created_at AND l.job_id IN (SELECT id FROM jobs)
     GROUP BY 1 ORDER BY 3 DESC
"""

DFS_COST_SQL = """
    SELECT count(*)::int AS calls, coalesce(sum(d.cost_usd), 0)::float AS cost_usd
      FROM shared.dfs_call_log d, shared.rerun_wave w
     WHERE w.id = $1::uuid AND d.tour_id = ANY(w.expected_tour_ids)
       AND d.created_at >= w.created_at AND (w.closed_at IS NULL OR d.created_at <= w.closed_at)
"""


def classify(t: dict) -> str:
    """One tour's state from the facts TOURS_SQL returns (first match wins, see module doc)."""
    if t.get("master_status") == "active" and t.get("published_in_wave"):
        return "master"
    if t.get("review_status") == "pending":
        return "review_pending"
    if t.get("source_status") == "trashed" or t.get("deleted"):
        return "trashed"
    if t.get("any_active"):
        return "running"
    s1 = t.get("s1_status")
    if s1 in ("failed", "stopped_budget"):
        return "failed"
    if s1 == "cancelled":
        return "interrupted"
    if s1 == "succeeded":
        return "unclassified"
    return "not_started"


def _failure_class(t: dict) -> Optional[str]:
    """Review rows reuse the Review Queue classifier (AA-728) with the facts available here (no
    gate codes) — enough to separate raw_insufficient / writer_tone / transient / low_quality."""
    try:
        from api.routers.admin_pipeline import _review_failure_class
        return _review_failure_class(t.get("score"), [], [], None, t.get("failure_summary"),
                                     t.get("src_chars"))["failure_class"]
    except Exception:            # never fail a report on the label
        return None


def build_report(wave: dict, tours: list[dict], job_totals: list[dict], llm: list[dict],
                 dfs: dict) -> dict:
    expected = len(tours)
    by_state = {s: 0 for s in STATES}
    rows = []
    for t in tours:
        state = classify(t)
        by_state[state] += 1
        rows.append({
            "tour_id": t["tour_id"], "name": t.get("src_name"), "country": t.get("country"),
            "state": state, "s1_status": t.get("s1_status"), "atomized": bool(t.get("atomized")),
            "jev": bool(t.get("jev")),
            "review": ({"score": t.get("score"), "failure_summary": (t.get("failure_summary") or "")[:300],
                        "failure_class": _failure_class(t)} if state == "review_pending" else None),
            "error": t.get("s1_error") if state in ("failed", "interrupted") else None,
        })
    active_jobs = sum(r["n"] for r in job_totals if r["status"] in ("queued", "running"))
    terminal = sum(by_state[s] for s in TERMINAL_STATES)
    jev_tours = sum(1 for r in rows if r["jev"])
    llm_cost = round(sum(r["cost_usd"] for r in llm), 4)
    return {
        "wave": wave,
        "expected": expected,
        # the S209 4-column report
        "columns": {
            "s1_ok": sum(1 for r in rows if r["s1_status"] == "succeeded"),
            "to_master": by_state["master"],
            "to_review": by_state["review_pending"],
            "atomized": sum(1 for r in rows if r["atomized"]),
        },
        "states": by_state,
        "terminal": terminal,
        "jobs_finished": active_jobs == 0 and bool(job_totals),
        "complete": active_jobs == 0 and terminal == expected,
        "not_terminal": [r for r in rows if r["state"] not in TERMINAL_STATES],
        "review": [r for r in rows if r["state"] == "review_pending"],
        "jev_coverage": {"tours": jev_tours, "expected": expected,
                         "pct": round(100.0 * jev_tours / expected, 1) if expected else 0.0},
        "cost": {"llm_usd": llm_cost, "llm_by_provider": llm, "dfs_usd": round(dfs.get("cost_usd", 0.0), 4),
                 "dfs_calls": dfs.get("calls", 0), "total_usd": round(llm_cost + dfs.get("cost_usd", 0.0), 4)},
        "jobs": job_totals,
        "tours": rows,
    }


def _wave_dict(row) -> dict:
    d = dict(row)
    d["id"] = str(d["id"])
    d["expected_count"] = len(d.pop("expected_tour_ids") or [])
    for k in ("created_at", "closed_at", "last_checked_at"):
        if d.get(k) is not None:
            d[k] = d[k].isoformat()
    if isinstance(d.get("last_check"), str):
        d["last_check"] = json.loads(d["last_check"])
    return d


async def fetch_wave(conn, wave_id: str):
    return await conn.fetchrow("SELECT * FROM shared.rerun_wave WHERE id = $1::uuid", wave_id)


async def report(conn, wave_id: str) -> Optional[dict]:
    row = await fetch_wave(conn, wave_id)
    if row is None:
        return None
    kinds = list(WAVE_JOB_KINDS)
    tours = [dict(r) for r in await conn.fetch(TOURS_SQL, wave_id, kinds, MASTER_TENANT_ID)]
    job_totals = [dict(r) for r in await conn.fetch(JOB_TOTALS_SQL, wave_id, kinds)]
    llm = [dict(r) for r in await conn.fetch(LLM_COST_SQL, wave_id, kinds)]
    dfs = dict(await conn.fetchrow(DFS_COST_SQL, wave_id))
    return build_report(_wave_dict(row), tours, job_totals, llm, dfs)


async def check(conn, wave_id: str) -> Optional[dict]:
    """Evaluate one open wave: every tour terminal and no wave job active → `complete`; the wave's
    jobs all finished but some tour is not terminal → `incomplete` and ONE admin notification (the
    open→incomplete transition happens once). Otherwise it stays open."""
    rep = await report(conn, wave_id)
    if rep is None:
        return None
    summary = {k: rep[k] for k in ("expected", "terminal", "complete", "jobs_finished", "columns", "states")}
    summary["not_terminal"] = [{"tour_id": r["tour_id"], "name": r["name"], "state": r["state"]}
                               for r in rep["not_terminal"]][:50]
    new_status = None
    if rep["wave"]["status"] == "open":
        # complete needs no job (batch-path tours resolve from Master/review alone); incomplete
        # only once the wave's own jobs have all finished.
        if rep["complete"]:
            new_status = "complete"
        elif rep["jobs_finished"]:
            new_status = "incomplete"
    async with conn.transaction():
        updated = await conn.fetchval(
            """
            UPDATE shared.rerun_wave
               SET last_checked_at = now(), last_check = $2::jsonb,
                   status = CASE WHEN status = 'open' AND $3::text IS NOT NULL THEN $3 ELSE status END
             WHERE id = $1::uuid
            RETURNING status
            """,
            wave_id, json.dumps(summary, default=str), new_status)
        if new_status == "incomplete" and updated == "incomplete":
            await conn.execute(
                """
                INSERT INTO shared.notifications
                    (tenant_id, actor_type, event_type, entity_type, entity_id, payload, target_roles)
                VALUES ($1::uuid, 'system', $2, 'rerun_wave', $3, $4::jsonb, ARRAY['admin'])
                """,
                MASTER_TENANT_ID, INCOMPLETE_EVENT, wave_id,
                json.dumps({"message": f"Rerun wave '{rep['wave']['name']}' finished with "
                                       f"{rep['expected'] - rep['terminal']} of {rep['expected']} tours "
                                       f"not in a terminal state.", **summary}, default=str))
            logger.error("rerun_wave_incomplete", wave_id=wave_id, name=rep["wave"]["name"],
                         expected=rep["expected"], terminal=rep["terminal"])
    summary["status"] = updated
    return summary
