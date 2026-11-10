"""AA-1049 W3 (S1 train) — a READ-ONLY export of stored S1 Masters for the free offline replay
(``scripts/quality/replay_aa1049_s1.py --masters``).

Runs in the CIS api container through ``tools/ecs/ecsx.sh`` (cwd /app, the container DATABASE_URL = cis_app,
ssl=require), inside one read-only transaction. No model call, no write. The output carries tour text, so it is printed
as one gzip+base64 block (``=== BEGIN s1_masters.json`` ... ``=== END s1_masters.json``) that the local runner
(``scripts/quality/replay_aa1049_s1_local.sh``) decodes into the gitignored .tmp-session (never committed, never on a
tracked path).

What it reads: per tour, the NEWEST ``silver_aa_internal.generated_content`` version of the AA master tenant written by
the S1 writer v2 (``metadata.writer_version = 'v2'``), not edited by hand, created in the last DAYS days; its
``silver_aa_internal.raw_tours`` source row turned into the tour dict exactly as ``admin_pipeline._execute_run_tour``
builds it (plus the agency fields the v2 fact sheet reads); and the forbidden words of the master tenant's active brand
rule (the same row ``_resolve_brand_rule`` picks without an explicit brand). Trashed / superseded / deleted sources are
left out (the job refuses them).

Env: DAYS (default 60), LIMIT (default 120, newest first). Prints a summary line first (counts and the block size).
"""
from __future__ import annotations

import asyncio
import base64
import gzip
import json
import os
import sys
from collections import Counter

sys.path.insert(0, "/app")

MASTER_TENANT = "00000000-0000-0000-0000-000000000001"   # aa_internal (admin_pipeline TENANT_SLUG_MAP)

MASTERS_SQL = """
    SELECT DISTINCT ON (gc.tour_id)
           gc.tour_id::text AS tour_id, gc.id::text AS version_id, gc.version_num, gc.status::text AS status,
           gc.aa_name, gc.aa_subtitle, gc.aa_summary, gc.aa_description, gc.aa_highlights, gc.aa_itineraries,
           gc.seo_title, gc.seo_meta, gc.metadata, gc.created_at,
           rt.src_name, rt.src_subtitle, rt.src_summary, rt.src_description, rt.src_highlights, rt.src_itineraries,
           rt.country, rt.duration, rt.price_raw, rt.inclusions, rt.exclusions, rt.group_size, rt.best_time_to_go
      FROM silver_aa_internal.generated_content gc
      JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = gc.tour_id
     WHERE gc.tenant_id = $1::uuid AND gc.metadata->>'writer_version' = 'v2' AND NOT gc.human_edited
       AND gc.created_at > now() - make_interval(days => $2)
       AND rt.deleted_at IS NULL AND rt.source_status::text NOT IN ('trashed', 'superseded')
     ORDER BY gc.tour_id, gc.version_num DESC
"""
BRAND_SQL = """
    SELECT forbidden_words FROM shared.tenant_brand_rules
     WHERE tenant_id = $1::uuid AND is_active = true
     ORDER BY (brand_name = 'default') DESC, version DESC LIMIT 1
"""


def _j(v, default):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return default
    return v if v is not None else default


def master_of(r: dict, forbidden: list) -> dict:
    meta = _j(r["metadata"], {}) or {}
    return {
        "tour_id": r["tour_id"], "version_id": r["version_id"], "version_num": r["version_num"],
        "stored_status": r["status"], "created_at": str(r["created_at"]),
        "tour": {"name": r["src_name"], "subtitle": r["src_subtitle"], "summary": r["src_summary"],
                 "description": r["src_description"], "highlights": _j(r["src_highlights"], []) or [],
                 "itineraries": r["src_itineraries"], "country": r["country"], "duration": r["duration"],
                 "price": r["price_raw"], "inclusions": r["inclusions"], "exclusions": r["exclusions"]},
        "tour_agency": {"group_size": r["group_size"], "best_time_to_go": r["best_time_to_go"]},
        "brand_rules": {"forbidden_words": forbidden, "rewrite_language": "en-US"},
        "stored": {"name": r["aa_name"], "subtitle": r["aa_subtitle"], "summary": r["aa_summary"],
                   "description": r["aa_description"], "highlights": _j(r["aa_highlights"], []) or [],
                   "itineraries": r["aa_itineraries"], "seo_title": r["seo_title"], "seo_meta": r["seo_meta"],
                   "trip_type": meta.get("trip_type")},
        "stored_contract_verdict": (meta.get("final_contract") or {}).get("verdict"),
        "stored_exit_path": meta.get("exit_path"),
    }


async def main() -> int:
    import asyncpg

    days, limit = int(os.environ.get("DAYS", "60")), int(os.environ.get("LIMIT", "120"))
    conn = await asyncpg.connect(os.environ["DATABASE_URL"], ssl="require")
    try:
        async with conn.transaction(readonly=True):
            rows = [dict(r) for r in await conn.fetch(MASTERS_SQL, MASTER_TENANT, days)]
            brand = await conn.fetchrow(BRAND_SQL, MASTER_TENANT)
    finally:
        await conn.close()
    forbidden = _j(brand["forbidden_words"], []) if brand else []
    rows.sort(key=lambda r: r["created_at"], reverse=True)
    masters = [master_of(r, list(forbidden or [])) for r in rows[:limit]]
    raw = json.dumps({"schema": "aa1049-s1-masters/1", "masters": masters}, default=str,
                     ensure_ascii=False).encode("utf-8")
    b64 = base64.b64encode(gzip.compress(raw, 9)).decode("ascii")
    print("EXPORT_SUMMARY " + json.dumps({
        "masters": len(masters), "candidates": len(rows), "days": days,
        "by_status": dict(Counter(m["stored_status"] for m in masters)),
        "by_contract_verdict": dict(Counter(m["stored_contract_verdict"] for m in masters)),
        "forbidden_words": len(forbidden or []), "bytes": len(raw)}))
    print("=== BEGIN s1_masters.json")
    print("\n".join(b64[i:i + 76] for i in range(0, len(b64), 76)))
    print("=== END s1_masters.json")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
