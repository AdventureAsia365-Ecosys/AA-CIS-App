"""AA-1059 — enqueue ONE platform `recompute` so the `segment_catchup` stage runs now instead of on the next natural
trigger. WRITES one `shared.job` row (or folds into a queued platform job); run by the orchestrator only, after the
AA-1059 deploy, through tools/ecs/ecsx.sh in the root repo:

    tools/ecs/ecsx.sh apps/AA-CIS-App/scripts/aa1059/enqueue_catchup_pass.py

Segment scope = [] (an empty list, NOT None): landing re-lands only the Segments the catch-up touches and the
brand-new ones (questions_count NULL); None would re-land every Segment (a backfill). Same helper every trigger uses
(`services.jobs.recompute_job.enqueue_recompute`), no debounce. Cost: what a platform pass costs (S273: about $0 to
$0.01) plus the catch-up's segment runs. Prints the job id and whether a queued job was reused.
"""
import asyncio
import os

import asyncpg

REASON = "aa1059_segment_catchup"
CREATED_BY = "script:aa1059"


async def main():
    from services.jobs.recompute_job import enqueue_recompute

    conn = await asyncpg.connect(os.environ["DATABASE_URL"], ssl="require",
                                 server_settings={"application_name": "aa1059-enqueue-catchup"})
    try:
        job_id, created = await enqueue_recompute(conn, scope="platform", reason=REASON, created_by=CREATED_BY,
                                                  segment_ids=[], debounce_s=0)
    finally:
        await conn.close()
    print(f"AA1059 enqueued job={job_id} created={created} scope=platform segment_ids=[]")


if __name__ == "__main__":
    asyncio.run(main())
