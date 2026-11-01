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

AA-642 (S263 lane L2, B2): two more probes in the same run, inside the same approved probe budget (< 0.01 USD a
month together): every ENABLED embedding deployment (cohere-embed-v4 on acc2 / acc3 today) gets one embedding of a
few words through ``embed.probe`` (the deployment's circuit counts it, like a text probe; a ``shared.llm_call_log`` row
stage ``adhoc_conn_test`` role ``embed``, answered or refused at 0 USD), and the Geoapify connection gets ONE geocode
search (``probe_geoapify``: free plan credit, logged in ``geo.geocode_call`` so the daily credit guard counts it, its
access_state and last_probe_at written). Geoapify is skipped when it is switched off, not the configured geocoder,
out of daily credits, or a place job holds the Geoapify lock.

Switch: ``LLM_DAILY_PROBE`` (ECS env, default OFF). Off: the api's ops metrics sampler queues nothing, and a manual
live run is refused (``NonRetryable``); a DRY RUN always works: it lists the deployments, the reason any is skipped and
the estimated cost (catalog or deployment price x ``EST_INPUT_TOKENS`` in + the test's output cap), and makes no call.
On: one run per UTC day from ``SCHEDULE_HOUR_UTC`` (idempotency key ``llm_connection_probe:<day>``). max_attempts 1
(a probe that failed is the answer; tomorrow's run is the retry), background, deploy-gate exempt (it may sit queued
across a deploy; a re-run is harmless).
"""
from __future__ import annotations

import asyncio
import json
import os
import time
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
# AA-642 (S263 L2, B2): the embedding probe text (a few tokens) and the Geoapify probe query (one search = one credit
# of the free plan). A plain public place name: the answer is not stored anywhere, only whether the call worked.
EMBED_PROBE_TEXT = "connectivity test"
EST_EMBED_TOKENS = 8
GEOAPIFY_KEY = "geoapify"
GEOAPIFY_PROBE_QUERY = ("Bangkok", "th")
GEOAPIFY_PROBE_LABEL = "geocode-search"


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


def estimate_embed_usd(dep, model) -> Optional[float]:
    """The estimated cost of one embedding probe (None when neither the deployment nor the catalog has a price)."""
    p_in = _price(dep, model, "price_in_override", "price_in_per_mtok")
    return None if p_in is None else round(EST_EMBED_TOKENS * p_in / 1_000_000, 6)


def build_plan(connections: dict, deployments: list, testable, embeddable=None,
               geoapify: Optional[dict] = None) -> dict:
    """The probe plan (pure but for ``testable`` / ``embeddable``, the catalog-reading filters): ``probes``
    (connection, model, wire, estimated USD; ``probe`` = text | embed | geocode) and ``skipped`` (connection, model or
    None, why). ``testable(conn, deployments)`` returns the (deployment, catalog model) pairs the gateway can call for
    a text test; AA-642 (S263 L2): ``embeddable(conn, deployments)`` the embedding pairs, and ``geoapify`` (from
    ``load_geoapify_state``) whether the one Geoapify search runs."""
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
                     "kind": conn.kind, "probe": "text", "estimated_usd": estimate_usd(dep, model, max_tokens)}
            if len(probes) >= MAX_PROBES:
                skipped.append({**entry, "why": f"over_limit ({MAX_PROBES} per run)"})
            else:
                probes.append(entry)
        for dep, model in (embeddable(conn, mine) if embeddable is not None else []):
            if not dep.enabled:
                skipped.append({"connection_key": key, "model_key": dep.model_key, "why": "deployment disabled"})
                continue
            entry = {"connection_key": key, "model_key": dep.model_key, "wire_id": dep.wire_id,
                     "kind": conn.kind, "probe": "embed", "estimated_usd": estimate_embed_usd(dep, model)}
            if len(probes) >= MAX_PROBES:
                skipped.append({**entry, "why": f"over_limit ({MAX_PROBES} per run)"})
            else:
                probes.append(entry)
    if geoapify is not None:
        entry = {"connection_key": GEOAPIFY_KEY, "model_key": GEOAPIFY_PROBE_LABEL, "wire_id": None,
                 "kind": GEOAPIFY_KEY, "probe": "geocode", "estimated_usd": 0.0}
        why = geoapify.get("skip")
        if why:
            skipped.append({**entry, "why": why})
        else:
            probes.append(entry)
    known = [p["estimated_usd"] for p in probes if p["estimated_usd"] is not None]
    return {"probes": probes, "skipped": skipped, "estimated_usd": round(sum(known), 6),
            "unpriced": sum(1 for p in probes if p["estimated_usd"] is None)}


def _embeddable(conn, deployments: list) -> list:
    """(deployment, catalog model) pairs of ``conn`` the embedding probe can call: Bedrock, an enabled catalog row with
    api_style embed, a Cohere request shape (the only one embed.py builds)."""
    from shared.llm_client.catalog import get_model_sync
    from shared.llm_client.embed import EMBED_VENDORS

    if conn.kind != "bedrock":
        return []
    out = []
    for d in deployments:
        model = get_model_sync(d.model_key)
        # AA-1026: Amazon (Titan V2, the fallback embedding model) has a request shape too (embed.EMBED_VENDORS).
        if model is not None and model.enabled and model.api_style == "embed" and model.vendor in EMBED_VENDORS:
            out.append((d, model))
    return out


async def load_geoapify_state(db) -> dict:
    """Whether the Geoapify probe runs: ``{"skip": why}`` when it must not (switched off, not the configured geocoder,
    no credit left today), else ``{"credits_left": n}``. Never raises (an unreadable row = the row's defaults)."""
    from services.places import geocoder
    from services.places import store as place_store

    if geocoder.provider_name() != GEOAPIFY_KEY:
        return {"skip": f"not the configured geocoder ({geocoder.provider_name()})"}
    limit = await geocoder.refresh_daily_credit_limit(db)
    if geocoder.connection_switched_off():
        return {"skip": "disabled"}
    try:
        used = await place_store.geocoder_requests_today(db, GEOAPIFY_KEY)
    except Exception as e:  # noqa: BLE001 — the guard cannot be read: do not spend
        return {"skip": f"credit count unreadable ({type(e).__name__})"}
    if used >= limit:
        return {"skip": f"daily credit limit reached ({used} of {limit})"}
    return {"credits_left": limit - used, "credits_used_today": used, "daily_credit_limit": limit}


async def load_plan(pool) -> dict:
    from api.services import llm_connections as svc
    from shared.llm_client import connections as conn_registry
    from shared.llm_client import deployments as dep_registry

    async with pool.acquire() as db:
        conns = await conn_registry.fetch_connections(db)
        deps = await dep_registry.fetch_deployments(db)
        geo = await load_geoapify_state(db)
    return await asyncio.to_thread(build_plan, conns, deps, svc._testable, _embeddable, geo)


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


def _bedrock_provider(conn) -> str:
    return "bedrock-native" if conn.auth_mode == "task_role" else "bedrock-satellite"


async def _audit(pool, connection_key: str, details: dict) -> None:
    async with pool.acquire() as db:
        await db.execute(
            "INSERT INTO acp_shared.audit_log (actor, action, resource_type, resource_id, details) "
            "VALUES ($1, $2, $3, $4, $5::jsonb)",
            ACTOR, "llm_connection_tested", "llm_provider_connection", connection_key, json.dumps(details))


async def probe_embedding(pool, connection_key: str, model_key: str, wire_id: str) -> dict:
    """AA-642 (S263 L2, B2): one embedding call on one deployment (``embed.probe``), its llm_call_log row (answered:
    its cost; refused / failed: 0 USD) and its audit row. A provider failure is the result, not an exception."""
    from api.services import llm_connections as svc
    from shared.llm_client import breaker, embed
    from shared.llm_client import connections as conn_registry
    from shared.llm_client.call_log import billed_fields, gateway_failure_row, record_call_with_pool
    from shared.llm_client.catalog import get_model_sync
    from shared.llm_client.errors import classify

    async with pool.acquire() as db:
        conn = (await conn_registry.fetch_connections(db)).get(connection_key)
    model = await asyncio.to_thread(get_model_sync, model_key)
    if conn is None or model is None:
        raise LookupError(f"no embedding deployment {model_key!r} on {connection_key!r}")
    started = time.monotonic()
    out = {"connection_key": connection_key, "model_key": model_key, "probe": "embed"}
    signal = {"connection_test": connection_key, "actor": ACTOR, "probe": "embed"}
    try:
        resp = await asyncio.to_thread(embed.probe, model, conn, wire_id, [EMBED_PROBE_TEXT])
    except Exception as e:  # noqa: BLE001 — a failed provider call is the probe's answer
        cls = breaker.failure_class(e)
        latency = int((time.monotonic() - started) * 1000)
        out.update(ok=False, error_class=cls, error=f"{type(e).__name__}: {e}"[:300], cost_usd=0.0, latency_ms=latency)
        ledger_class = classify(e)
        row = gateway_failure_row(
            stage=svc.TEST_STAGE, role="embed", model=model_key,
            status="refused" if ledger_class in ("access_denied", "quota_exhausted", "throttled") else "error",
            error_class=ledger_class, error=out["error"], provider=_bedrock_provider(conn),
            connection_key=connection_key, latency_ms=latency, deployment_wire_id=wire_id,
            quality_signal_extra=signal)
        await record_call_with_pool(pool, **row)
    else:
        out.update(ok=True, error_class=None, error=None, cost_usd=round(resp.cost_usd, 6),
                   tokens_in=resp.input_tokens, latency_ms=int((time.monotonic() - started) * 1000))
        await record_call_with_pool(
            pool, stage=svc.TEST_STAGE, role="embed", model=resp.model_used, tokens_in=resp.input_tokens,
            tokens_out=0, cost_usd=resp.cost_usd, quality_signal={**signal, "tokens_estimated": resp.tokens_estimated},
            account=connection_key, fallback_used=False, provider=_bedrock_provider(conn), **billed_fields(resp))
    await _audit(pool, connection_key, {k: out.get(k) for k in ("model_key", "probe", "ok", "error_class", "cost_usd",
                                                                 "latency_ms")})
    return out


async def probe_geoapify(pool, job_id: Optional[str] = None) -> dict:
    """AA-642 (S263 L2, B2): ONE Geoapify geocode search (free plan credit), under the Geoapify lock (a place job that
    holds it is not disturbed: the probe is skipped), logged in geo.geocode_call (the daily credit guard counts it),
    with Geoapify's access_state and last_probe_at. No retry: a 429 / 5xx is the answer."""
    from services.places import geocoder
    from services.places import store as place_store
    from services.places.geolock import geoapify_lock
    from shared.llm_client import connections as conn_registry

    out = {"connection_key": GEOAPIFY_KEY, "model_key": GEOAPIFY_PROBE_LABEL, "probe": "geocode", "cost_usd": 0.0}
    async with geoapify_lock(pool) as held:
        if not held:
            return {**out, "ok": None, "skipped": "a place job holds the Geoapify lock", "error_class": None}
        async with pool.acquire() as db:
            used = await place_store.geocoder_requests_today(db, GEOAPIFY_KEY)
            limit = await geocoder.refresh_daily_credit_limit(db)
        g = geocoder.get_geocoder(GEOAPIFY_KEY, max_retries=0, credits_used_today=used, daily_credit_limit=limit)
        started = time.monotonic()
        state = None
        try:
            await g.search(*GEOAPIFY_PROBE_QUERY)
            out.update(ok=True, error_class=None, error=None)
            state = "ok"
        except geocoder.GeocoderUnavailable as e:
            out.update(ok=False, error_class="access_denied" if "HTTP" in str(e) else "unconfigured",
                       error=str(e)[:300])
            state = None if conn_registry.switched_off_reason(GEOAPIFY_KEY) in str(e) else out["error_class"]
        except geocoder.GeocoderError as e:
            out.update(ok=False, error_class="other", error=str(e)[:300])
        finally:
            out["latency_ms"] = int((time.monotonic() - started) * 1000)
            cost = round(g.requests * g.cost_per_request, 6)
            try:
                async with pool.acquire() as db:
                    await place_store.log_geocode_calls(db, GEOAPIFY_KEY, g.requests, cost, job_id,
                                                        failed_requests=g.failed_requests)
            except Exception as e:  # noqa: BLE001 — the credit is spent either way; the job result shows it
                logger.warning("llm_probe_geocode_log_failed", error=str(e)[:200])
            await g.aclose()
        out["requests"], out["cost_usd"] = g.requests, cost
    await conn_registry.note_provider_access(GEOAPIFY_KEY, state, probed=True)
    return out


async def run_probes(pool, plan: dict, *, test=None, embed_test=None, geo_test=None,
                     job_id: Optional[str] = None) -> list[dict]:
    """One ``test_connection`` per planned text deployment, sequential (a few calls; no burst on one account). AA-1018
    part 2: then ``last_probe_at`` on every connection a probe reached (started, whatever its outcome). AA-642 (S263
    L2): an embedding probe per planned embedding deployment, and the one Geoapify search."""
    from api.services import llm_connections as svc

    test = test or svc.test_connection
    embed_test = embed_test or probe_embedding
    geo_test = geo_test or probe_geoapify
    results = []
    reached: list[str] = []
    for p in plan["probes"]:
        if p.get("probe") == "geocode":
            try:
                results.append(await geo_test(pool, job_id))
            except Exception as e:  # noqa: BLE001 — a probe that cannot even start is a result too
                results.append({"connection_key": p["connection_key"], "model_key": p["model_key"], "ok": False,
                                "probe": "geocode", "error_class": "unavailable",
                                "error": f"{type(e).__name__}: {e}"[:200]})
            continue
        if p.get("probe") == "embed":
            try:
                results.append(await embed_test(pool, p["connection_key"], p["model_key"], p["wire_id"]))
                reached.append(p["connection_key"])
            except Exception as e:  # noqa: BLE001
                results.append({"connection_key": p["connection_key"], "model_key": p["model_key"], "ok": False,
                                "probe": "embed", "error_class": "unavailable",
                                "error": f"{type(e).__name__}: {e}"[:200]})
            continue
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
    results = await run_probes(ctx.pool, plan, job_id=ctx.job_id)
    cost = round(sum(float(r.get("cost_usd") or 0) for r in results), 6)
    ran = [r for r in results if not r.get("skipped")]          # AA-642 (S263 L2): a Geoapify probe left to a place job
    result.update(probed=len(ran), ok=sum(1 for r in ran if r["ok"]),
                  failed=sum(1 for r in ran if not r["ok"]), results=results, calls_made=len(ran),
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
