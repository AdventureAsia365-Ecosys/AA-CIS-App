"""AA-651 — `python -m worker` entrypoint for the standalone job-worker ECS service.

Thin shim: everything (asyncpg pool, redis, SIGTERM/SIGINT handling, load_kinds, graceful drain)
is already implemented in shared.jobs.worker._main — reused as-is so the worker behaves identically
whether it runs here or in-API. The API process stops starting its in-process worker once
JOB_WORKER_IN_API=false (set on the api task definition in AA-CIS-Infra, AA-651).
"""
import asyncio

from shared.jobs.worker import _main

if __name__ == "__main__":
    asyncio.run(_main())
