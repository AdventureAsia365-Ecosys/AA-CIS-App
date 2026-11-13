"""AA-1093 part 3 — queue one `tenant_backup` job from a shell of the CIS API container (ECS exec), or wait for it:

    python -m services.tenant_backup.enqueue --tenant <uuid>                  # queue; prints {"job_id": ...}
    python -m services.tenant_backup.enqueue --wait <job id> [--timeout 900]  # wait; prints status + result counts

The worker service runs the job. Output: ids, status, counts and digests only.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from typing import Optional

STATUS_SQL = "SELECT status, result, error FROM shared.job WHERE id = $1::uuid AND kind = 'tenant_backup'"
RESULT_KEYS = ("backup_id", "tenant_id", "prefix", "tables", "rows", "bytes", "gz_bytes", "skipped", "unregistered",
               "missing", "complete", "sha256sums_sha256")


async def _main(argv: list[str]) -> int:  # pragma: no cover — a thin shell around enqueue_manual (unit-tested)
    import asyncpg

    from services.jobs.tenant_backup_job import enqueue_manual

    parser = argparse.ArgumentParser(prog="python -m services.tenant_backup.enqueue")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--tenant")
    group.add_argument("--wait")
    parser.add_argument("--timeout", type=int, default=900)
    args = parser.parse_args(argv)
    pool = await asyncpg.create_pool(os.environ["DATABASE_URL"], ssl=os.environ.get("DB_SSL", "require"),
                                     min_size=1, max_size=1, timeout=10,
                                     server_settings={"application_name": "cis-tenant-backup-enqueue"})
    try:
        if args.tenant:
            job_id, created = await enqueue_manual(pool, args.tenant, "cli:aa-1093")
            print(json.dumps({"job_id": job_id, "created": created}))
            return 0
        deadline = time.monotonic() + args.timeout
        while True:
            row = await pool.fetchrow(STATUS_SQL, args.wait)
            if row is None:
                print(json.dumps({"error": "no_such_backup_job"}))
                return 1
            if row["status"] in ("succeeded", "failed", "cancelled", "stopped_budget") \
                    or time.monotonic() > deadline:
                result = json.loads(row["result"]) if isinstance(row["result"], str) else (row["result"] or {})
                out = {"status": row["status"], **{k: result.get(k) for k in RESULT_KEYS if k in result}}
                if row["status"] == "failed":
                    # the code before the first colon only: a database error text can quote a row value
                    out["error"] = (row["error"] or "").split(":", 1)[0][:80]
                print(json.dumps(out, sort_keys=True, default=str))
                return 0 if row["status"] == "succeeded" else 1
            await asyncio.sleep(5)
    finally:
        await pool.close()


def cli(argv: Optional[list[str]] = None) -> int:  # pragma: no cover
    return asyncio.run(_main(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(cli())
