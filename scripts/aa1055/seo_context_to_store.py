#!/usr/bin/env python3
"""AA-1055 — copy S1's October seo_context (keyword ideas with volume, PAA, related searches) into the shared demand
store acp_contract.search_demand. Free: no DataForSEO, no LLM call. Logic: services/acp_contract/seo_context_copy.py.

Default = DRY RUN (reads only, prints the counts it would write). APPLY=1 writes, in one transaction, merging: never
over a newer volume or a newer SERP. Run by the orchestrator inside the CIS api container (cis_app DATABASE_URL), with
the ECS exec helper of the root repo (tools/ecs/ecsx.sh <this file> [APPLY=1]).

Needs CIS 393 applied and the AA-1055 PR 2 image deployed.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

sys.path.insert(0, os.getcwd())


async def _pool():
    import asyncpg
    from urllib.parse import urlparse

    from shared.secrets import get_database_url

    u = urlparse(get_database_url().strip())
    return await asyncpg.create_pool(host=u.hostname, port=u.port or 5432, user=u.username, password=u.password,
                                     database=u.path.lstrip("/"), ssl="require", min_size=1, max_size=2)


async def main() -> dict:
    from services.acp_contract.seo_context_copy import run_copy

    pool = await _pool()
    try:
        return await run_copy(pool, apply=os.environ.get("APPLY", "").strip() == "1")
    finally:
        await pool.close()


if __name__ == "__main__":
    print(json.dumps(asyncio.run(main()), indent=2, default=str))
