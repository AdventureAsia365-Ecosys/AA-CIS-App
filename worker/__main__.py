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
from shared.log_config import configure_for_process
from shared.sentry_init import init_sentry

if __name__ == "__main__":
    # AA-871: JSON log lines (service=cis-worker); each job's lines carry job_id / job_kind.
    configure_for_process("cis-worker")
    # AA-870: job errors logged at ERROR level reach Sentry (logging integration), tagged service=worker.
    init_sentry(service="worker")
    asyncio.run(_main())
