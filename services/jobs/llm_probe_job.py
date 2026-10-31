"""AA-1018 (reliability part 1) — job kind ``llm_connection_probe``: one tiny call per enabled LLM deployment, so a
refused account shows in the admin bell and the e-mail (the connection circuit) before a paid job fails on it.

Payload: {"trigger": "schedule" | "manual", "dry_run": bool (manual default true), "requested_by": "admin:<id>"}.

What it probes: every ENABLED connection without a policy problem (``ProviderConnection.policy_problems``; a
connection with one is listed as ``unconfigured``, never called), and on it every ENABLED deployment of a text model
the gateway can call (``api.services.llm_connections._testable``: no embedding rows), at most ``MAX_PROBES`` in one run
(the rest listed as ``over_limit``). A switched-off connection (anthropic-direct with no key today) is ``disabled``.

How: ``api.services.llm_connections.test_connection`` per deployment — the admin's "Test connection": the gateway's own
adapters through ``LLMClient.probe`` (32 output tokens, bypasses an open circuit, its outcome counts on the deployment's
circuit: a success closes it, a refusal opens it and, account-wide, the connection circuit with its alert), a
``shared.llm_call_log`` row for an answered call (stage ``adhoc_conn_test``), one ``acp_shared.audit_log`` row per
probe. A refused probe costs 0 USD.

Switch: ``LLM_DAILY_PROBE`` (ECS env, default OFF). Off: the api's ops metrics sampler queues nothing, and a manual
live run is refused (``NonRetryable``); a DRY RUN always works: it lists the deployments, the reason any is skipped and
the estimated cost (catalog or deployment price x ``EST_INPUT_TOKENS`` in + the test's output cap), and makes no call.
On: one run per UTC day from ``SCHEDULE_HOUR_UTC`` (idempotency key ``llm_connection_probe:<day>``). max_attempts 1
(a probe that failed is the answer; tomorrow's run is the retry), background, deploy-gate exempt (it may sit queued
across a deploy; a re-run is harmless).
"""
from __future__ import annotations

import asyncio
import os
from datetime import date, datetime, timezone
from typing import Optional

import structlog

from shared.jobs.registry import JobContext, NonRetryable, enqueue, job_kind

logger = structlog.get_logger()

KIND = "llm_connection_probe"
SCHEDULER = "scheduler:aa-1018"
SWITCH_ENV = "LLM_DAILY_PROBE"
SCHEDULE_HOUR_UTC = 5
MAX_PROBES = 30
ACTOR = f"job:{KIND}"
# The test prompt (llm_connections.TEST_SYSTEM + TEST_USER) is a few dozen tokens; a generous round figure.
EST_INPUT_TOKENS = 60


def schedule_enabled() -> bool:
    """``LLM_DAILY_PROBE`` (default off): on / true / 1 / yes."""
    return (os.environ.get(SWITCH_ENV) or "off").strip().lower() in ("on", "true", "1", "yes")


def _price(dep, model, field_dep: str, field_model: str) -> Optional[float]:
    v = getattr(dep, field_dep, None)
    return v if v is not None else getattr(model, field_model, None)


def estimate_usd(dep, model, max_tokens: int) -> Optional[float]:
    """The estimated cost of one probe on ``dep`` (None when the catalog has no price for the model)."""
    p_in = _price(dep, model, "price_in_override", "price_in_per_mtok")
    p_out = _price(dep, model, "price_out_override", "price_out_per_mtok")
    if p_in is None or p_out is None:
        return None
    return round((EST_INPUT_TOKENS * p_in + max_tokens * p_out) / 1_000_000, 6)


def build_plan(connections: dict, deployments: list, testable) -> dict:
    """The probe plan (pure but for ``testable``, the catalog-reading filter): ``probes`` (connection, model, wire,
    estimated USD) and ``skipped`` (connection, model or None, why). ``testable(conn, deployments)`` returns the
    (deployment, catalog model) pairs the gateway can call for a text test."""
    from api.services.llm_connections import TEST_MAX_TOKENS

    probes: list[dict] = []
    skipped: list[dict] = []
    for key in sorted(connections):
        conn = connections[key]
        if not conn.enabled:
            skipped.append({"connection_key": key, "model_key": None, "why": "disabled"})
            continue
        problems = conn.policy_problems()
        if problems:
            skipped.append({"connection_key": key, "model_key": None,
                            "why": "unconfigured: " + "; ".join(problems)})
            continue
        mine = [d for d in deployments if d.connection_key == key]
        for dep, model in testable(conn, mine):
            if not dep.enabled:
                skipped.append({"connection_key": key, "model_key": dep.model_key, "why": "deployment disabled"})
                continue
            max_tokens = min(TEST_MAX_TOKENS, model.max_output_tokens or TEST_MAX_TOKENS)
            entry = {"connection_key": key, "model_key": dep.model_key, "wire_id": dep.wire_id,
                     "kind": conn.kind, "estimated_usd": estimate_usd(dep, model, max_tokens)}
            if len(probes) >= MAX_PROBES:
                skipped.append({**entry, "why": f"over_limit ({MAX_PROBES} per run)"})
            else:
                probes.append(entry)
    known = [p["estimated_usd"] for p in probes if p["estimated_usd"] is not None]
    return {"probes": probes, "skipped": skipped, "estimated_usd": round(sum(known), 6),
            "unpriced": sum(1 for p in probes if p["estimated_usd"] is None)}


async def load_plan(pool) -> dict:
    from api.services import llm_connections as svc
    from shared.llm_client import connections as conn_registry
    from shared.llm_client import deployments as dep_registry

    async with pool.acquire() as db:
        conns = await conn_registry.fetch_connections(db)
        deps = await dep_registry.fetch_deployments(db)
    return await asyncio.to_thread(build_plan, conns, deps, svc._testable)


# AA-1018 part 2: the probe stamps each connection it probed (migration 322 column; cis_app has UPDATE on it). The
# outcome itself goes to access_state through the breaker (a success = 'ok', an account-wide refusal = its class).
_STAMP_PROBE_SQL = ("UPDATE shared.llm_provider_connection SET last_probe_at = now() "
                    "WHERE connection_key = ANY($1::text[])")


async def stamp_probed(pool, connection_keys: list[str]) -> None:
    """Set last_probe_at on the probed connections. Best effort (before migration 322 the column is missing)."""
    if not connection_keys:
        return
    try:
        async with pool.acquire() as db:
            await db.execute(_STAMP_PROBE_SQL, sorted(set(connection_keys)))
    except Exception as e:  # noqa: BLE001 — a stamp never fails the probe
        logger.warning("llm_probe_stamp_failed", error=str(e)[:200])


async def run_probes(pool, plan: dict, *, test=None) -> list[dict]:
    """One ``test_connection`` per planned deployment, sequential (a few calls; no burst on one account). AA-1018
    part 2: then ``last_probe_at`` on every connection a probe reached (started, whatever its outcome)."""
    from api.services import llm_connections as svc

    test = test or svc.test_connection
    results = []
    reached: list[str] = []
    for p in plan["probes"]:
        try:
            r = await test(pool, p["connection_key"], model_key=p["model_key"], actor=ACTOR)
            reached.append(p["connection_key"])
            results.append({"connection_key": p["connection_key"], "model_key": p["model_key"], "ok": bool(r["ok"]),
                            "error_class": r.get("error_class"), "cost_usd": r.get("cost_usd"),
                            "latency_ms": r.get("latency_ms")})
        except Exception as e:  # noqa: BLE001 — a probe that cannot even start is a result too
            results.append({"connection_key": p["connection_key"], "model_key": p["model_key"], "ok": False,
                            "error_class": "unavailable", "error": f"{type(e).__name__}: {e}"[:200]})
    await stamp_probed(pool, reached)
    return results


@job_kind(KIND, concurrency=1, max_attempts=1, expected_seconds=600, background=True, deploy_gate_exempt=True)
async def run(ctx: JobContext) -> dict:
    payload = ctx.payload or {}
    trigger = payload.get("trigger", "manual")
    dry_run = bool(payload.get("dry_run", trigger != "schedule"))
    if not dry_run and not schedule_enabled():
        raise NonRetryable(f"live probes are off ({SWITCH_ENV} is not on); run a dry run, or switch it on first")
    plan = await load_plan(ctx.pool)
    result = {"kind": KIND, "trigger": trigger, "dry_run": dry_run, "switch_on": schedule_enabled(),
              "estimated_usd": plan["estimated_usd"], "unpriced": plan["unpriced"],
              "planned": plan["probes"], "skipped": plan["skipped"], "calls_made": 0}
    if dry_run:
        result.update(probed=0, ok=0, failed=0, results=[])
        logger.info("llm_probe_dry_run", job_id=ctx.job_id, planned=len(plan["probes"]),
                    estimated_usd=plan["estimated_usd"])
        ctx.set_result(result)
        return result
    results = await run_probes(ctx.pool, plan)
    cost = round(sum(float(r.get("cost_usd") or 0) for r in results), 6)
    result.update(probed=len(results), ok=sum(1 for r in results if r["ok"]),
                  failed=sum(1 for r in results if not r["ok"]), results=results, calls_made=len(results),
                  cost_usd=cost)
    logger.info("llm_probe_done", job_id=ctx.job_id, probed=result["probed"], ok=result["ok"],
                failed=result["failed"], cost_usd=cost)
    ctx.set_result(result)
    return result


def schedule_key(day: date) -> str:
    return f"{KIND}:{day.isoformat()}"


_EXISTS_SQL = "SELECT EXISTS (SELECT 1 FROM shared.job WHERE idempotency_key = $1)"


async def enqueue_daily(pool, now: Optional[datetime] = None) -> tuple[Optional[str], bool, date]:
    """Today's (UTC) live probe run, from SCHEDULE_HOUR_UTC on, only with the switch on (idempotent per day). Called
    by the ops metrics sampler on every tick: switch off or before the hour, nothing is read."""
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
    """An admin's run (no idempotency key). A live run needs the switch on (checked here and again in the job)."""
    if not dry_run and not schedule_enabled():
        raise PermissionError(f"live probes are off ({SWITCH_ENV} is not on)")
    return await enqueue(pool, KIND, {"trigger": "manual", "dry_run": dry_run, "requested_by": actor[:64]},
                         created_by=actor[:64])
