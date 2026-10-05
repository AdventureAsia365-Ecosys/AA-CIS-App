"""AA-651 — standalone job-worker entrypoint.

`python -m worker` runs the durable job runner (shared.jobs.worker) as its own process on the
`aa-cis-dev-worker` ECS service, instead of inside the API process. The real loop, pool, SIGTERM
handling and kind registration all live in shared.jobs.worker._main; this package is only the
`-m worker` entrypoint the ECS task's command override points at.
"""
