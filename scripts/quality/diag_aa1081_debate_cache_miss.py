"""AA-1081 (S278) — read-only check of WHY a tenant's T7 Debate brand-fit cache missed on a `t7_slate` run.

$0, no LLM, no write (every session is ``default_transaction_read_only``). Standalone (asyncpg + stdlib only) so it
runs in the api container through the S3-mediated ECS exec pattern (``scripts/`` is not in the image):

    python3 diag_aa1081_debate_cache_miss.py --run-start 2026-11-11T17:00:00Z [--tenant <uuid> ...]

``--run-start``: when the slow run began. The tenants default to every tenant with a ``t7_slate`` job created since
then. Per tenant the window is (the tenant's last succeeded ``t7_slate`` that finished before ``--run-start``,
``--run-start``): the earlier run should have filled the cache; ``--since`` is the fallback start when there is none.

The Segments the slow run judged are the tenant's cache rows written since ``--run-start`` (a hit is not rewritten).
Each one gets ONE cause, checked in this order (a cache row is only ever deleted by the A3 atomize clear, AA-645
step 0; a brand version change or a new Segment id leaves the old rows where they were):

1. ``brand_version``         — the tenant still has an older row for this Segment under another brand version.
2. ``new_or_merged_segment`` — the Segment id was created in the window, or another id was merged into it there.
3. ``cleared_by_atomize``    — an ``a3_atomize`` job that finished in the window cleared rows
                               (``brand_fit_cache_cleared`` > 0) of a tour whose atoms are members of this Segment now.
4. ``not_cached_before``     — none of the above: the earlier run never stored a score for it (not a candidate then,
                               or its judge call failed).

Output: one JSON document (ids, counts, versions and timestamps only, no text) between DIAG-BEGIN / DIAG-END.
The DB login is ``DATABASE_URL`` (cis_app in the api container reads every table used here), ssl required unless
``DIAG_DB_SSL=disable`` (local tests).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter
from datetime import datetime
from typing import Optional

SCHEMA = "aa1081-debate-cache-miss/1"
CAUSES = ("brand_version", "new_or_merged_segment", "cleared_by_atomize", "not_cached_before")
DEFAULT_SINCE = "2026-11-09T17:00:00+00:00"   # S275 day start (ICT)

_TENANTS_SQL = """
    SELECT DISTINCT payload->>'tenant_id' AS tenant_id FROM shared.job
    WHERE kind = 't7_slate' AND created_at >= $1 AND payload ? 'tenant_id'
    ORDER BY 1
"""
_PREV_SLATE_SQL = """
    SELECT coalesce(started_at, created_at) AS started_at, finished_at FROM shared.job
    WHERE kind = 't7_slate' AND payload->>'tenant_id' = $1 AND status = 'succeeded' AND finished_at < $2
    ORDER BY finished_at DESC LIMIT 1
"""
_SLATE_JOBS_SQL = """
    SELECT id, status, created_at, started_at, finished_at FROM shared.job
    WHERE kind = 't7_slate' AND payload->>'tenant_id' = $1 AND created_at >= $2
    ORDER BY created_at
"""
# The brand row Debate keys the cache on (services/acp_shared/debate._fetch_brand_profile ->
# api/routers/admin_pipeline._resolve_brand_rule with no id / name).
_CURRENT_BRAND_SQL = """
    SELECT version FROM shared.tenant_brand_rules
    WHERE tenant_id = $1::uuid AND is_active = true
    ORDER BY (brand_name = 'default') DESC, version DESC LIMIT 1
"""
_BRAND_ROWS_SQL = """
    SELECT brand_name, version, is_active, updated_at FROM shared.tenant_brand_rules
    WHERE tenant_id = $1::uuid ORDER BY brand_name, version
"""
_CACHE_BY_VERSION_SQL = """
    SELECT brand_version, count(*) AS n_rows, count(*) FILTER (WHERE computed_at < $2) AS n_before_run,
           min(computed_at) AS first_computed, max(computed_at) AS last_computed
    FROM acp_shared.debate_brand_fit_cache WHERE tenant_id = $1::uuid
    GROUP BY brand_version ORDER BY brand_version
"""
_JUDGE_CALLS_SQL = """
    SELECT job_id, status, coalesce(is_shadow, false) AS is_shadow, count(*) AS n, min(created_at) AS first_at
    FROM shared.llm_call_log
    WHERE stage = 't7_debate_judge' AND tenant_id = $1::uuid AND created_at >= $2
    GROUP BY job_id, status, coalesce(is_shadow, false) ORDER BY min(created_at)
"""
_ATOMIZE_SQL = """
    SELECT count(*) AS jobs, count(DISTINCT payload->>'tour_id') AS tours,
           count(*) FILTER (WHERE jsonb_typeof(result->'brand_fit_cache_cleared') = 'number'
                              AND (result->>'brand_fit_cache_cleared')::int > 0) AS jobs_clearing,
           coalesce(sum((result->>'brand_fit_cache_cleared')::int)
                    FILTER (WHERE jsonb_typeof(result->'brand_fit_cache_cleared') = 'number'), 0) AS rows_cleared
    FROM shared.job
    WHERE kind = 'a3_atomize' AND finished_at >= $1 AND finished_at < $2
"""
# $1 tenant, $2 run start, $3 window start. One row per Segment the slow run judged.
_SEGMENT_FACTS_SQL = """
    WITH judged AS (
        SELECT DISTINCT segment_id FROM acp_shared.debate_brand_fit_cache
        WHERE tenant_id = $1::uuid AND segment_id IS NOT NULL AND computed_at >= $2
    ), cleared_tours AS (
        SELECT DISTINCT (payload->>'tour_id')::uuid AS tour_id FROM shared.job
        WHERE kind = 'a3_atomize' AND finished_at >= $3 AND finished_at < $2
          AND jsonb_typeof(result->'brand_fit_cache_cleared') = 'number'
          AND (result->>'brand_fit_cache_cleared')::int > 0
    ), cleared_segments AS (
        SELECT DISTINCT m.segment_id FROM cleared_tours ct
        JOIN acp_contract.tour_atoms ta ON ta.tour_id = ct.tour_id
        JOIN acp_contract.atom_segment_member m ON m.atom_id = ta.atom_id
        WHERE m.segment_id IN (SELECT segment_id FROM judged)
    )
    SELECT j.segment_id,
           s.created_at AS segment_created_at,
           EXISTS (SELECT 1 FROM acp_contract.atom_segment_alias a
                   WHERE a.segment_id_canonical = j.segment_id AND a.merged_at >= $3) AS merged_in_window,
           coalesce((SELECT array_agg(DISTINCT c.brand_version ORDER BY c.brand_version)
                     FROM acp_shared.debate_brand_fit_cache c
                     WHERE c.tenant_id = $1::uuid AND c.segment_id = j.segment_id AND c.computed_at < $2),
                    '{}'::int[]) AS older_versions,
           (cs.segment_id IS NOT NULL) AS cleared_by_atomize
    FROM judged j
    LEFT JOIN acp_contract.atom_segment s ON s.segment_id = j.segment_id
    LEFT JOIN cleared_segments cs ON cs.segment_id = j.segment_id
    ORDER BY j.segment_id
"""


def classify_segment(facts: dict, window_start: datetime) -> str:
    """The one cause of a judged Segment's cache miss (pure; order as in the module doc)."""
    if facts.get("older_versions"):
        return "brand_version"
    created = facts.get("segment_created_at")
    if created is None or created >= window_start or facts.get("merged_in_window"):
        return "new_or_merged_segment"
    if facts.get("cleared_by_atomize"):
        return "cleared_by_atomize"
    return "not_cached_before"


def summarize(facts: list[dict], window_start: datetime, current_version: Optional[int],
              versions_before_run: list[int]) -> dict:
    """Per-tenant counts per cause and the verdict (pure). `brand_version_changed`: the version Debate keys on now
    is not one the tenant had rows for before the run (a bump whose old rows a clear has since removed shows here
    even when no Segment keeps an older row)."""
    counts = Counter(classify_segment(f, window_start) for f in facts)
    by_cause = {c: counts.get(c, 0) for c in CAUSES}
    verdict = max(CAUSES, key=lambda c: (by_cause[c], -CAUSES.index(c))) if facts else "no_misses"
    changed = bool(versions_before_run) and current_version is not None and current_version not in versions_before_run
    return {"judged_segments": len(facts), "by_cause": by_cause, "verdict": verdict,
            "brand_version_changed": changed}


def _iso(v):
    return v.isoformat() if isinstance(v, datetime) else v


async def diagnose_tenant(conn, tenant_id: str, run_start: datetime, since: datetime) -> dict:
    prev = await conn.fetchrow(_PREV_SLATE_SQL, tenant_id, run_start)
    window_start = prev["finished_at"] if prev else since
    # the listings (slate jobs, judge calls) start with the earlier slate itself: did it judge, and how much?
    listed_from = prev["started_at"] if prev else since
    current_version = await conn.fetchval(_CURRENT_BRAND_SQL, tenant_id)
    by_version = [dict(r) for r in await conn.fetch(_CACHE_BY_VERSION_SQL, tenant_id, run_start)]
    versions_before_run = [r["brand_version"] for r in by_version if r["n_before_run"]]
    facts = [dict(r) for r in await conn.fetch(_SEGMENT_FACTS_SQL, tenant_id, run_start, window_start)]
    for f in facts:
        f["older_versions"] = list(f["older_versions"] or [])
    out = {
        "tenant_id": tenant_id, "window_start": _iso(window_start), "window_from_previous_slate": prev is not None,
        "listed_from": _iso(listed_from),
        "current_brand_version": current_version,
        "brand_rows": [{k: _iso(v) for k, v in dict(r).items()} for r in await conn.fetch(_BRAND_ROWS_SQL, tenant_id)],
        "cache_by_version": [{k: _iso(v) for k, v in r.items()} for r in by_version],
        "slate_jobs": [{k: _iso(v) if k != "id" else str(v) for k, v in dict(r).items()}
                       for r in await conn.fetch(_SLATE_JOBS_SQL, tenant_id, listed_from)],
        "judge_calls": [{"job_id": str(r["job_id"]) if r["job_id"] else None, "status": r["status"],
                         "is_shadow": r["is_shadow"], "n": r["n"], "first_at": _iso(r["first_at"])}
                        for r in await conn.fetch(_JUDGE_CALLS_SQL, tenant_id, listed_from)],
        "atomize_in_window": dict(await conn.fetchrow(_ATOMIZE_SQL, window_start, run_start)),
        **summarize(facts, window_start, current_version, versions_before_run),
    }
    out["segments"] = [{"segment_id": f["segment_id"], "cause": classify_segment(f, window_start)} for f in facts]
    return out


async def run(args) -> dict:
    import asyncpg

    ssl = None if os.environ.get("DIAG_DB_SSL") == "disable" else "require"
    conn = await asyncpg.connect(os.environ["DATABASE_URL"], ssl=ssl,
                                 server_settings={"default_transaction_read_only": "on"})
    try:
        run_start = datetime.fromisoformat(args.run_start.replace("Z", "+00:00"))
        since = datetime.fromisoformat(args.since.replace("Z", "+00:00"))
        tenants = list(dict.fromkeys(args.tenant)) or [
            r["tenant_id"] for r in await conn.fetch(_TENANTS_SQL, run_start)]
        rows = []
        for t in tenants:
            try:
                rows.append(await diagnose_tenant(conn, t, run_start, since))
            except Exception as exc:          # one tenant's failure is a row, not a stop
                rows.append({"tenant_id": t, "error": f"{type(exc).__name__}: {exc}"[:300]})
    finally:
        await conn.close()
    ok = [r for r in rows if "error" not in r]
    total = Counter()
    for r in ok:
        total.update(r["by_cause"])
    return {"schema": SCHEMA, "cost_usd": 0, "run_start": _iso(run_start), "tenants": rows,
            "summary": {"tenants": len(rows), "errors": len(rows) - len(ok),
                        "by_cause": {c: total.get(c, 0) for c in CAUSES},
                        "brand_version_changed_tenants": sum(1 for r in ok if r["brand_version_changed"])}}


def parse_args(argv: Optional[list[str]] = None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--run-start", required=True, help="ISO timestamp the slow t7_slate run began")
    p.add_argument("--since", default=DEFAULT_SINCE, help="window start when a tenant has no earlier slate")
    p.add_argument("--tenant", action="append", default=[], help="tenant uuid (repeatable)")
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    result = asyncio.run(run(parse_args(argv)))
    print("DIAG-BEGIN")
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    print("DIAG-END")
    return 1 if result["summary"]["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
