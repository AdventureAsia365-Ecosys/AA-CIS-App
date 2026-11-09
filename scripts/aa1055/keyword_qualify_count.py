#!/usr/bin/env python3
"""AA-1055 (S274) — how many places of the stored keyword maps the qualification rules flag (READ ONLY).

Counts, over every place with a map in acp_contract.place_keyword (research run R1 and later):
  - namesake places (keyword_qualify.is_namesake_name) and not-a-place places (place card what_it_is, else the head
    noun of the name), and how many stored keywords of them are unqualified;
  - garbled stored keywords (keyword_qualify.garbled_reason, the plain place name excepted);
  - the places the next research run re-proposes (segment_research_batch.needs_requalify) and their Segments;
  - per market, the bought volume of the unqualified namesake keywords the demand claim now ignores, and how many
    of them alone were at or above the Blog bar volume (1,000).
No write, no LLM, no DataForSEO, no Jev call. Prints JSON with counts and up to 20 sample names per category.

Run by the orchestrator inside the CIS api container (cis_app DATABASE_URL), with the ECS exec helper of the root repo
(tools/ecs/ecsx.sh <this file>), after the image carrying services/acp_contract/keyword_qualify.py is deployed.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

sys.path.insert(0, os.getcwd())

BLOG_BAR_VOLUME = 1000
SAMPLE = 20

MAPS_SQL = """
    SELECT subject, keyword, rank, jev_zone, jev_mode, prompt_version
    FROM acp_contract.place_keyword WHERE subject_kind = 'place'
    ORDER BY subject, rank, keyword
"""
VOLUMES_SQL = """
    SELECT keyword, market, search_volume FROM acp_contract.search_demand
    WHERE keyword = ANY($1::text[]) AND search_volume IS NOT NULL
"""
SEGMENTS_SQL = "SELECT count(*) AS n FROM acp_contract.atom_segment WHERE canonical_place = ANY($1::text[])"


async def _pool():
    import asyncpg
    from urllib.parse import urlparse

    from shared.secrets import get_database_url

    u = urlparse(get_database_url().strip())
    return await asyncpg.create_pool(host=u.hostname, port=u.port or 5432, user=u.username, password=u.password,
                                     database=u.path.lstrip("/"), ssl="require", min_size=1, max_size=2)


def _bought(row: dict) -> bool:
    return not (row.get("jev_zone") == "reject" and row.get("jev_mode") == "enforce")


async def count(pool) -> dict:
    from services.acp_contract import keyword_qualify as kq
    from services.acp_contract import segment_research_batch as rb

    async with pool.acquire() as conn:
        rows = [dict(r) for r in await conn.fetch(MAPS_SQL)]
    maps: dict[str, list[dict]] = {}
    for r in rows:
        maps.setdefault(r["subject"], []).append(r)
    names = sorted(maps)
    where = await rb.load_locations(pool, names)

    namesake = [n for n in names if kq.is_namesake_name(n)]
    not_place = [n for n in names if kq.not_a_place(n, (where.get(n) or kq.PlaceLocation()).what_it_is)]
    with_card = sum(1 for n in names if (where.get(n) or kq.PlaceLocation()).locality)
    no_locality = [n for n in namesake if not (where.get(n) or kq.PlaceLocation()).phrases(n)]

    unqualified: list[str] = []
    for n in namesake:
        phrases = (where.get(n) or kq.PlaceLocation()).phrases(n)
        unqualified += [r["keyword"] for r in maps[n] if _bought(r) and not kq.carries(r["keyword"], phrases)]
    garbled: dict[str, int] = {}
    garbled_sample: list[str] = []
    for n in names:
        plain = rb.clean_keyword(n)
        for r in maps[n]:
            reason = r["keyword"] != plain and _bought(r) and kq.garbled_reason(r["keyword"])
            if reason:
                garbled[reason] = garbled.get(reason, 0) + 1
                if len(garbled_sample) < SAMPLE:
                    garbled_sample.append(f"{n} | {r['keyword']}")
    requalify = [n for n in names if rb.needs_requalify(n, maps[n])]

    per_market: dict[str, dict] = {}
    async with pool.acquire() as conn:
        vols = await conn.fetch(VOLUMES_SQL, sorted(set(unqualified))) if unqualified else []
        segments = (await conn.fetchval(SEGMENTS_SQL, requalify)) if requalify else 0
    for v in vols:
        m = per_market.setdefault(v["market"], {"keywords": 0, "volume": 0, "at_or_above_blog_bar": 0})
        m["keywords"] += 1
        m["volume"] += v["search_volume"]
        m["at_or_above_blog_bar"] += int(v["search_volume"] >= BLOG_BAR_VOLUME)

    return {
        "prompt_version_now": rb.PROMPT_VERSION,
        "places_with_map": len(names),
        "places_with_card_locality": with_card,
        "namesake_places": len(namesake),
        "namesake_places_without_locality": len(no_locality),
        "not_a_place_places": len(not_place),
        "unqualified_namesake_keywords": len(set(unqualified)),
        "garbled_keywords": garbled,
        "places_requalify_next_run": len(requalify),
        "segments_of_requalify_places": segments,
        "unqualified_volume_by_market": per_market,
        "sample_namesake": namesake[:SAMPLE],
        "sample_namesake_without_locality": no_locality[:SAMPLE],
        "sample_not_a_place": not_place[:SAMPLE],
        "sample_garbled": garbled_sample,
    }


async def main() -> dict:
    pool = await _pool()
    try:
        return await count(pool)
    finally:
        await pool.close()


if __name__ == "__main__":
    print(json.dumps(asyncio.run(main()), indent=2, default=str))
