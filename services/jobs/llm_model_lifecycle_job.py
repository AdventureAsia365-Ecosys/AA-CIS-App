"""AA-1024 D2 (S263 gateway plan, OPS-04) — job kind ``llm_model_lifecycle_sync``: the lifecycle status and the
end-of-life date of every catalog model, from Bedrock ``ListFoundationModels`` on each Bedrock connection (acc2 on the
task role, acc3 through the satellite role), into the lifecycle columns of ``shared.llm_model_catalog`` (CIS 352).

Payload: {"trigger": "schedule" | "manual", "dry_run": bool (default false), "requested_by": "admin:<id>"}.

What it does: one control-plane list per enabled Bedrock connection (free, no model call, nothing in
shared.llm_call_log), then for every catalog row the status of ``shared.llm_client.model_lifecycle.model_lifecycle``
(active / legacy / unlisted / unknown / untracked; a row with no Bedrock deployment is ``untracked``), so every routed
model has a non-NULL status after one run. A connection that refuses (acc3's account-level Bedrock hold) or errors is
recorded per connection in ``lifecycle_detail`` and in the result, never fails the run; a model only that account
serves keeps its earlier answered status. Then one admin bell (``shared.notifications``, ``llm_model_lifecycle``,
entity ``llm_model:<key>`` at most once per 7 days) for each ROUTED model whose end of life is within 90 days (or
past), or that is LEGACY or unlisted. The OPS-12 digest reads the same columns (services/reports/digest.py).

Dry run: lists, computes and returns what it would write; no UPDATE, no bell.
Before migration 352: a scheduled run is a no-op (result ``skipped``), a manual live run is refused (NonRetryable).

Schedule: once per UTC day from ``SCHEDULE_HOUR_UTC`` (idempotency key ``llm_model_lifecycle_sync:<day>``), queued
by the api's ops metrics sampler. Switch ``LLM_MODEL_LIFECYCLE_SYNC`` (ECS env, default ON: the run costs nothing).
max_attempts 2, background, deploy-gate exempt (a re-run is harmless).
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import date, datetime, timezone
from typing import Optional

import structlog

from shared.jobs.registry import JobContext, NonRetryable, enqueue, job_kind

logger = structlog.get_logger()

KIND = "llm_model_lifecycle_sync"
SCHEDULER = "scheduler:aa-1024-d2"
SWITCH_ENV = "LLM_MODEL_LIFECYCLE_SYNC"
SCHEDULE_HOUR_UTC = 4
ACTOR = f"job:{KIND}"
BELL_EVENT = "llm_model_lifecycle"
BELL_EVERY_DAYS = 7
_PLATFORM_TENANT_ID = "00000000-0000-0000-0000-000000000001"

CATALOG_SQL = ("SELECT model_key, provider, bedrock_profile_ids, enabled, lifecycle_status, eol_date, legacy_date "
               "FROM shared.llm_model_catalog ORDER BY model_key")
# Before migration 352: the same rows without the lifecycle columns (a dry run still works).
CATALOG_V1_SQL = ("SELECT model_key, provider, bedrock_profile_ids, enabled FROM shared.llm_model_catalog "
                  "ORDER BY model_key")
DEPLOYMENTS_SQL = "SELECT model_key, connection_key, wire_id, enabled FROM shared.llm_model_deployment"
ROUTES_SQL = ("SELECT stage, model_id, fallback_model_ids, shadow_model_id, is_active FROM shared.llm_role_config "
              "ORDER BY stage")
ALIASES_SQL = "SELECT alias_key, position, model_key FROM shared.llm_model_alias"
UPDATE_SQL = """
    UPDATE shared.llm_model_catalog
       SET lifecycle_status = $2, eol_date = $3, legacy_date = $4, lifecycle_checked_at = now(),
           lifecycle_detail = $5::jsonb
     WHERE model_key = $1
"""
_RECENT_BELL_SQL = f"""
    SELECT 1 FROM shared.notifications
     WHERE event_type = $1 AND entity_id = $2 AND created_at >= now() - interval '{BELL_EVERY_DAYS} days'
     LIMIT 1
"""
_INSERT_BELL_SQL = """
    INSERT INTO shared.notifications (tenant_id, actor_type, event_type, entity_type, entity_id, payload, target_roles)
    VALUES ($1::uuid, 'system', $2, 'llm_model', $3, $4::jsonb, ARRAY['admin'])
"""


def schedule_enabled() -> bool:
    """``LLM_MODEL_LIFECYCLE_SYNC`` (default on): off / false / 0 / no stops the schedule."""
    return (os.environ.get(SWITCH_ENV) or "on").strip().lower() not in ("off", "false", "0", "no")


async def read_state(db) -> dict:
    """Every input of a run from one connection: Bedrock connections, deployments, catalog rows (``ready`` False
    before migration 352), active routes and aliases."""
    import asyncpg

    from shared.llm_client import connections as conn_registry

    conns = await conn_registry.fetch_connections(db)
    deps = [dict(r) for r in await db.fetch(DEPLOYMENTS_SQL)]
    try:
        catalog = [dict(r) for r in await db.fetch(CATALOG_SQL)]
        ready = True
    except asyncpg.exceptions.UndefinedColumnError:
        catalog = [dict(r) for r in await db.fetch(CATALOG_V1_SQL)]
        ready = False
    routes = [dict(r) for r in await db.fetch(ROUTES_SQL)]
    aliases = [dict(r) for r in await db.fetch(ALIASES_SQL)]
    return {"connections": {k: c for k, c in conns.items() if c.kind == "bedrock"}, "deployments": deps,
            "catalog": catalog, "routes": routes, "aliases": aliases, "ready": ready}


def plan_rows(state: dict, listings: dict, *, now: Optional[datetime] = None) -> list:
    """The Lifecycle of every catalog row (pure but for ``model_lifecycle``'s own purity)."""
    from shared.llm_client import model_lifecycle as ml

    now = now or datetime.now(timezone.utc)
    bedrock_keys = set(state["connections"])
    out = []
    for m in state["catalog"]:
        pairs = ml.deployment_pairs(m, state["deployments"], bedrock_keys)
        prev = {"status": m.get("lifecycle_status"), "eol_date": m.get("eol_date"),
                "legacy_date": m.get("legacy_date")}
        out.append(ml.model_lifecycle(m["model_key"], pairs, listings, prev, checked_at=now))
    return out


async def list_all(connections: dict, *, lister=None) -> dict:
    """One ListFoundationModels per Bedrock connection, sequential, each in a thread. Never raises."""
    from shared.llm_client import model_lifecycle as ml

    lister = lister or ml.list_connection
    out = {}
    for key in sorted(connections):
        out[key] = await asyncio.to_thread(lister, connections[key])
    return out


async def write_rows(pool, rows: list) -> int:
    args = [(r.model_key, r.status, r.eol_date, r.legacy_date, json.dumps(r.detail, default=str)) for r in rows]
    if not args:
        return 0
    async with pool.acquire() as db:
        await db.executemany(UPDATE_SQL, args)
    return len(args)


async def ring_bells(pool, found: list[dict]) -> int:
    """One admin bell per warned model, at most one per BELL_EVERY_DAYS. Best effort: a failed bell is logged."""
    rung = 0
    for w in found:
        try:
            async with pool.acquire() as db:
                if await db.fetchval(_RECENT_BELL_SQL, BELL_EVENT, f"llm_model:{w['model_key']}"):
                    continue
                await db.execute(_INSERT_BELL_SQL, _PLATFORM_TENANT_ID, BELL_EVENT, f"llm_model:{w['model_key']}",
                                 json.dumps(w, default=str))
                rung += 1
        except Exception as e:  # noqa: BLE001 — a bell never fails the sync
            logger.warning("llm_lifecycle_bell_failed", model=w["model_key"], error=str(e)[:200])
    return rung


def summarize_run(state: dict, listings: dict, rows: list, today: date) -> dict:
    """The job result (pure)."""
    from shared.llm_client import model_lifecycle as ml

    routed = ml.route_keys(state["routes"], state["aliases"])
    by_status: dict[str, int] = {}
    for r in rows:
        by_status[r.status] = by_status.get(r.status, 0) + 1
    status_of = {r.model_key: r for r in rows}
    found = ml.warnings([{"model_key": r.model_key, "lifecycle_status": r.status, "eol_date": r.eol_date}
                         for r in rows], routed, today)
    return {"connections": [listings[k].as_dict() for k in sorted(listings)],
            "models": len(rows), "by_status": dict(sorted(by_status.items())),
            "kept_from_previous": sorted(r.model_key for r in rows if r.kept),
            "routed": len(routed),
            "routed_without_status": sorted(k for k in routed if k not in status_of),
            "routed_unknown": sorted(k for k in routed if k in status_of and status_of[k].status == ml.UNKNOWN),
            "warnings": found}


@job_kind(KIND, concurrency=1, max_attempts=2, expected_seconds=120, background=True, deploy_gate_exempt=True)
async def run(ctx: JobContext) -> dict:
    payload = ctx.payload or {}
    trigger = payload.get("trigger", "manual")
    dry_run = bool(payload.get("dry_run", False))
    async with ctx.pool.acquire() as db:
        state = await read_state(db)
    if not dry_run and not state["ready"] and trigger == "schedule":
        # deployed before the expand file is applied: tonight's run is a no-op, not a failed job in the digest
        result = {"kind": KIND, "trigger": trigger, "dry_run": False, "migration_ready": False, "skipped":
                  "CIS migration 352 not applied yet", "updated": 0, "bells": 0}
        ctx.set_result(result)
        return result
    if not dry_run and not state["ready"]:
        raise NonRetryable("shared.llm_model_catalog has no lifecycle columns yet (CIS migration 352 not applied); "
                           "a dry run still works")
    now = datetime.now(timezone.utc)
    listings = await list_all(state["connections"])
    rows = plan_rows(state, listings, now=now)
    result = {"kind": KIND, "trigger": trigger, "dry_run": dry_run, "migration_ready": state["ready"],
              **summarize_run(state, listings, rows, now.date())}
    if dry_run:
        result.update(updated=0, bells=0, would_write=[{"model_key": r.model_key, "status": r.status,
                                                         "eol_date": r.eol_date, "legacy_date": r.legacy_date}
                                                        for r in rows])
    else:
        result["updated"] = await write_rows(ctx.pool, rows)
        result["bells"] = await ring_bells(ctx.pool, result["warnings"])
    logger.info("llm_lifecycle_sync_done", job_id=ctx.job_id, dry_run=dry_run, models=result["models"],
                by_status=result["by_status"], warnings=len(result["warnings"]),
                connections={c["connection_key"]: c["access"] for c in result["connections"]})
    result = json.loads(json.dumps(result, default=str))
    ctx.set_result(result)
    return result


def schedule_key(day: date) -> str:
    return f"{KIND}:{day.isoformat()}"


_EXISTS_SQL = "SELECT EXISTS (SELECT 1 FROM shared.job WHERE idempotency_key = $1)"


async def enqueue_nightly(pool, now: Optional[datetime] = None) -> tuple[Optional[str], bool, date]:
    """Today's (UTC) run, from SCHEDULE_HOUR_UTC on, with the switch on (idempotent per day). Called by the ops
    metrics sampler on every tick: before the hour or switched off nothing is read; after it one indexed EXISTS."""
    now = now or datetime.now(timezone.utc)
    today = now.date()
    if not schedule_enabled() or now.hour < SCHEDULE_HOUR_UTC:
        return None, False, today
    key = schedule_key(today)
    if await pool.fetchval(_EXISTS_SQL, key):
        return None, False, today
    job_id, created = await enqueue(pool, KIND, {"trigger": "schedule", "dry_run": False}, idempotency_key=key,
                                    created_by=SCHEDULER)
    return job_id, created, today


async def enqueue_manual(pool, *, dry_run: bool, actor: str) -> tuple[str, bool]:
    """An admin's run (no idempotency key; the kind is concurrency 1)."""
    return await enqueue(pool, KIND, {"trigger": "manual", "dry_run": dry_run, "requested_by": actor[:64]},
                         created_by=actor[:64])
