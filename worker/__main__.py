"""AA-651 — `python -m worker` entrypoint for the standalone job-worker ECS service.

Thin shim: everything (asyncpg pool, redis, SIGTERM/SIGINT handling, load_kinds, graceful drain)
is already implemented in shared.jobs.worker._main — reused as-is so the worker behaves identically
whether it runs here or in-API. The API process does NOT start its in-process worker by default
(JOB_WORKER_IN_API defaults off, AA-735/ADR 0003 nac 5); the api task definition no longer needs to
set JOB_WORKER_IN_API=false, and only an explicit truthy JOB_WORKER_IN_API runs the in-API worker
for local/dev.
"""
import asyncio

from shared.jobs.worker import _main

if __name__ == "__main__":
    asyncio.run(_main())
