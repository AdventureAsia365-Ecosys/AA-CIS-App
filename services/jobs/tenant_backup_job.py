"""AA-1093 part 3 (gap M27, plan D11) — job kind ``tenant_backup``: one tenant's CIS rows to the gold bucket
(services/tenant_backup/export.py; restore drill: ``python -m services.tenant_backup.restore_drill``).

Payload: {"tenant_id": "<uuid>", "trigger": "manual" | "schedule"} (ids only). On demand per tenant
(``enqueue_manual``), and nightly for the pilot tenant only (``shared.tenants.is_pilot``, CIS 354; root ADR 0019 d16)
when ``TENANT_BACKUP_PILOT=on`` on the api (default off): the ops metrics sampler queues it once per UTC day from
``SCHEDULE_HOUR_UTC`` (idempotency key ``tenant_backup:pilot:<YYYY-MM-DD>``). A backup is kept 7 days: the gold
bucket's `exports/` rule deletes it, no code does.

Refused (NonRetryable): a bad id, the AA internal tenant (it owns the shared catalogue; PITR restores it), a tenant
with no row. Result: ids, table names, counts, sizes and digests (export.run_backup), never a row value. $0 (database
and S3 only). A re-run writes the same keys again (the backup id is the job id), so a retry is safe.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Optional

import structlog

from services.tenant_backup import catalogue as cat
from services.tenant_backup import export
from shared.jobs.registry import JobContext, NonRetryable, enqueue, job_kind

logger = structlog.get_logger()

KIND = "tenant_backup"
SCHEDULER = "scheduler:aa-1093"
SWITCH_ENV = "TENANT_BACKUP_PILOT"
SCHEDULE_HOUR_UTC = 20          # 03:00 in Vietnam, after the nightly report and cost jobs

_EXISTS_SQL = "SELECT EXISTS (SELECT 1 FROM shared.job WHERE idempotency_key = $1)"
PILOT_SQL = "SELECT tenant_id::text FROM shared.tenants WHERE is_pilot AND cancelled_at IS NULL LIMIT 1"


def schedule_enabled(env: Optional[dict] = None) -> bool:
    """Off unless TENANT_BACKUP_PILOT is on (D11: the nightly copy is a tech lead decision per environment)."""
    return ((env if env is not None else os.environ).get(SWITCH_ENV) or "off").strip().lower() in ("on", "true", "1")


def schedule_key(day) -> str:
    return f"{KIND}:pilot:{day.isoformat()}"


def validate_payload(payload: Optional[dict]) -> dict:
    p = dict(payload or {})
    try:
        p["tenant_id"] = cat.check_tenant_id(p.get("tenant_id"))
    except cat.BackupRefused as e:
        raise NonRetryable(str(e)) from None
    p["trigger"] = p.get("trigger") if p.get("trigger") in ("manual", "schedule") else "manual"
    return p


def _store(ctx: JobContext):
    store = ctx.resources.get("tenant_backup_store")
    if store is None:
        from services.tenant_backup.store import S3Store

        store = S3Store()
    return store


@job_kind(KIND, concurrency=1, max_attempts=2, expected_seconds=900, background=True)
async def run(ctx: JobContext) -> dict:
    payload = validate_payload(ctx.payload)
    try:
        out = await export.run_backup(ctx.pool, _store(ctx), payload["tenant_id"], str(ctx.job_id),
                                      max_bytes=export.max_bytes_from_env(), progress=ctx.progress)
    except cat.BackupRefused as e:
        raise NonRetryable(str(e)) from None
    result = {"kind": KIND, "trigger": payload["trigger"], **out}
    logger.info("tenant_backup_done", job_id=str(ctx.job_id), tenant_id=out["tenant_id"], tables=out["tables"],
                rows=out["rows"], gz_bytes=out["gz_bytes"], skipped=len(out["skipped"]), complete=out["complete"])
    ctx.set_result(result)
    return result


async def enqueue_manual(pool, tenant_id: str, actor: str) -> tuple[str, bool]:
    """One backup of one tenant now (no idempotency key: each request is a backup; the kind runs one at a time)."""
    tid = validate_payload({"tenant_id": tenant_id})["tenant_id"]
    return await enqueue(pool, KIND, {"tenant_id": tid, "trigger": "manual"}, created_by=str(actor)[:64])


async def enqueue_pilot_nightly(pool, now: Optional[datetime] = None) -> tuple[Optional[str], bool, str]:
    """Today's (UTC) backup of the pilot tenant from SCHEDULE_HOUR_UTC on; nothing when no tenant is the pilot.
    Called by the ops metrics sampler on every tick (one indexed EXISTS a tick after the first)."""
    now = now or datetime.now(timezone.utc)
    key = schedule_key(now.astimezone(timezone.utc).date())
    if now.astimezone(timezone.utc).hour < SCHEDULE_HOUR_UTC or await pool.fetchval(_EXISTS_SQL, key):
        return None, False, key
    tenant = await pool.fetchval(PILOT_SQL)
    if tenant is None:
        return None, False, key
    job_id, created = await enqueue(pool, KIND, {"tenant_id": tenant, "trigger": "schedule"},
                                    idempotency_key=key, created_by=SCHEDULER)
    return job_id, created, key
