"""AA-1024 G3 (c) — ECS exec scripts still running, from the heartbeats the root ecsx runner writes.

Lesson S234 (root lessons log, ECS exec family): a deploy replaced the task under a long ECS exec script twice; the
script died with it. The root wrapper (`AA-Ecosys/tools/ecs/`, S3 mode for the CIS containers) runs a script detached
in the api or worker container and, while it runs, rewrites `scripts/out/<run id>.txt.running` on the bronze bucket
every 30 s; at the end it writes `<run id>.txt` and `<run id>.txt.done`, and the local side removes all three.

`running_exec_scripts()` lists `scripts/out/` and returns the heartbeats younger than `FRESH_S` that have no `.done`
next to them: a script that is running now. A heartbeat older than that is a run that died (a deploy, OOM) or whose
local side never cleaned up; it does not block. `shared.jobs.worker.deploy_gate_status` adds one `blocked_by` line per
running script, so the CI pre-deploy guard (`.github/scripts/deploy_job_guard.sh`) refuses the roll.

The api and worker task role may list the bucket (AA-CIS-Infra modules/ecs, ecs_task_policy). A failed list raises
here; `deploy_gate_status` turns it into a `blocked_by` line ("heartbeat unreadable", fail closed) and keeps the rest
of its answer. `DEPLOY_GATE_ECSX=off` turns the check off (the test conftests do: no AWS).

A hung script blocks every CIS deploy until it ends or its `ECSX_SCRIPT_TIMEOUT` passes (the root runner caps it at
10,800 s); to clear it sooner, stop the script in the container (runbook `deploy-gate-override.md`, row "ECS exec
script running").
"""
from __future__ import annotations

import datetime as dt
import os
from typing import Any, Optional

OUT_PREFIX = "scripts/out/"
RUNNING_SUFFIX = ".running"
DONE_SUFFIX = ".done"
# Four missed beats of the runner's 30 s heartbeat (ecsx_runner.HEARTBEAT_S).
FRESH_S = 120


def enabled() -> bool:
    return os.environ.get("DEPLOY_GATE_ECSX", "on").strip().lower() not in ("off", "0", "false")


def running_from_listing(objects: list[dict], now: dt.datetime, fresh_s: int = FRESH_S) -> list[dict[str, Any]]:
    """The running scripts in one listing of `scripts/out/` (each object: Key, LastModified), oldest first."""
    keys = {o["Key"] for o in objects}
    out = []
    for o in objects:
        key = o["Key"]
        if not key.endswith(RUNNING_SUFFIX):
            continue
        base = key[: -len(RUNNING_SUFFIX)]
        if base + DONE_SUFFIX in keys:
            continue
        age = (now - o["LastModified"]).total_seconds()
        if age <= fresh_s:
            run_id = base[len(OUT_PREFIX):].rsplit(".", 1)[0]
            out.append({"run_id": run_id, "age_s": max(0, int(age))})
    return sorted(out, key=lambda r: (-r["age_s"], r["run_id"]))


def running_exec_scripts(client=None, now: Optional[dt.datetime] = None) -> list[dict[str, Any]]:
    """The ECS exec scripts running now (see the module doc). Blocking: call it through asyncio.to_thread."""
    if not enabled():
        return []
    from shared.aws_client.bronze import bronze_bucket

    if client is None:
        import boto3
        client = boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-west-1"))
    objects: list[dict] = []
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bronze_bucket(), Prefix=OUT_PREFIX):
        objects.extend({"Key": o["Key"], "LastModified": o["LastModified"]} for o in page.get("Contents") or [])
    return running_from_listing(objects, now or dt.datetime.now(dt.timezone.utc))
