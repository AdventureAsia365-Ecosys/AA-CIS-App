"""AA-884 (S241 GĐ7) — health / version of each ecosystem app, for /admin/platform/apps (and, read-only, AA-858's
/admin/overview).

    collect_apps(pool, redis, refresh=False, traffic=None) -> {"apps": [cis, booking, tripplanner, crm], "generated_at",
                                                               "_cached"}
    collect_app(pool, redis, app_id, refresh=False) -> one card, or None for an unknown id

One payload, cached in Redis for ``CACHE_TTL_S`` (60 s) under ``CACHE_KEY``; ``refresh=True`` rebuilds it (the page's
Refresh button) and rewrites the cache. No poller: it is built only when someone opens a page. Every card has
``status`` in ok / degraded / down / unknown and never raises: a probe that does not answer keeps the error class
on the card and counts as "degraded" in the overall status ("health check did not answer", app_health).

One health definition (S243 finding 1): a card's ``status`` is ``overall.status`` from
``api.services.app_health.overall_status`` — the worst of the app's own probe (``probe``) and its CloudWatch traffic
of the last 24 h (``traffic``, the same cached section /admin/overview shows). The overview copies ``overall`` onto
its tiles, so both pages always say the same thing about an app.

What each card reads (S241 constraints: no ECS / Lambda describe calls, no new IAM, no cross-schema reads):
- ``cis``: this API task's git SHA (APP_GIT_SHA) and ECS task revision, the live workers from ``shared.job_worker``
  (their git_sha, CIS 248), parity = every live worker runs the API's SHA, the deploy gate (the same helper as
  GET /admin/job-runner/deploy-gate) and the schema readiness of /health/ready.
- ``booking``: GET {health_base}/booking/health and /booking/health/ready over HTTP (2 s timeout each).
- ``crm`` (AA-936): GET {health_base}/crm/health and /crm/health/ready over HTTP, the same probe as Booking
  (``_http_health_card``). Its jobs are a separate, uncached read (``crm_jobs``): ``crm.ops.v1``
  ``/jobs/summary`` called with the staff member's own Cognito access token (never cached: it is per person).
- ``tripplanner``: no health endpoint yet, so its status comes from traffic alone (its two Lambdas: invocations,
  errors, throttles, alarms); plus today's customer-facing LLM spend (TP ONLINE_STAGES, rows with
  ``quality_signal->>'app' = 'tripplanner'`` since UTC midnight) against the mirrored default daily cap.
"""
from __future__ import annotations

import asyncio
import base64
import json
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

import httpx
import structlog

from api.core import readiness
from api.core.app_registry import APPS, TP_DEFAULT_DAILY_CAP_USD, TP_ONLINE_STAGES
from api.services import app_health

logger = structlog.get_logger()

CACHE_KEY = "admin_platform_apps:v3"   # v2 (S243): cards carry probe / traffic / health; v3 (AA-936): + crm
CACHE_TTL_S = 60
HTTP_TIMEOUT_S = 2.0
TP_WARN_PCT = 80.0

# Since UTC midnight; the created_at index serves the range, stage = ANY narrows to the online stages.
TP_SPEND_TODAY_SQL = """
    SELECT stage, count(*)::int AS calls, coalesce(sum(cost_usd), 0)::float AS cost_usd
      FROM shared.llm_call_log
     WHERE created_at >= (date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC')
       AND stage = ANY($1::text[])
       AND quality_signal ->> 'app' = $2
     GROUP BY stage
     ORDER BY stage
"""

_task_revision: dict[str, Any] = {}


def _iso(v: Any) -> Any:
    return v.isoformat() if isinstance(v, datetime) else v


async def _api_task_revision() -> Optional[int]:
    """This task's ECS revision (metadata endpoint, read once per process: it never changes for a task)."""
    if "value" not in _task_revision:
        from shared.jobs.worker import ecs_task_revision
        _task_revision["value"] = await asyncio.to_thread(ecs_task_revision)
    return _task_revision["value"]


async def _cis_card(pool: Any) -> dict:
    from shared.jobs import queue
    from shared.jobs.worker import deploy_gate_status

    info = APPS["cis"]
    sha = readiness.git_sha()
    card: dict[str, Any] = {"id": "cis", "label": info.label, "repo_url": info.repo_url,
                            "api": {"git_sha": sha, "commit_url": info.commit_url(sha),
                                    "task_revision": await _api_task_revision()}}
    problems: list[str] = []
    try:
        health = await queue.worker_health(pool)
        workers = [{"worker_id": w["worker_id"], "git_sha": w.get("git_sha"),
                    "commit_url": info.commit_url(w.get("git_sha")), "task_revision": w.get("task_revision"),
                    "running_jobs": w.get("running_jobs"), "last_seen_at": _iso(w.get("last_seen_at"))}
                   for w in health["workers"] if w.get("alive")]
        card["workers"] = workers
        if not workers:
            card["parity"] = False
            problems.append("no live worker")
        elif sha == "unknown":
            card["parity"] = None  # a local / CI process: nothing to compare
        else:
            card["parity"] = all(w["git_sha"] == sha for w in workers)
            if not card["parity"]:
                problems.append("a live worker runs another commit than the API")
    except Exception as e:
        logger.warning("platform_apps_workers_failed", error=repr(e)[:200])
        card["workers"], card["parity"] = None, None
        problems.append(f"workers unreadable ({type(e).__name__})")
    try:
        # AA-993 G5: unsafe only while a rerun wave is open or a deploy-blocking kind runs (`blocked_by` says which);
        # `active` / `by_status` are the jobs a deploy releases and the new revision re-claims (shown as info).
        card["deploy_gate"] = await deploy_gate_status(pool)
    except Exception as e:
        logger.warning("platform_apps_deploy_gate_failed", error=repr(e)[:200])
        card["deploy_gate"] = None
    _, ready = await readiness.readiness(pool)
    card["schema"] = {"version": ready.get("schema_version"), "required": ready.get("required_schema"),
                      "status": ready.get("status"), "db": ready.get("db")}
    if ready.get("status") != "ok":
        problems.append(f"schema/db not ready ({ready.get('status')})")
    card["problems"] = problems
    card["status"] = "ok" if not problems else "degraded"
    card["probe"] = {"status": card["status"],
                     "reason": "; ".join(problems) if problems else "Workers on the API's commit, schema ready"}
    return card


async def _get_json(client: httpx.AsyncClient, url: str) -> dict:
    r = await client.get(url)
    try:
        body = r.json()
    except ValueError:
        body = None
    return {"http_status": r.status_code, "body": body if isinstance(body, dict) else None}


async def _http_health_card(app_id: str, client: Optional[httpx.AsyncClient] = None) -> dict:
    """An app probed over its public health + readiness paths (Booking, the partner CRM)."""
    info = APPS[app_id]
    health_path, ready_path = info.health_paths
    card: dict[str, Any] = {"id": app_id, "label": info.label, "repo_url": info.repo_url,
                            "health_url": f"{info.health_base}{health_path}"}
    own = client is None
    client = client or httpx.AsyncClient(timeout=HTTP_TIMEOUT_S)
    try:
        health, ready = await asyncio.gather(
            *(_get_json(client, f"{info.health_base}{p}") for p in info.health_paths))
    except Exception as e:  # timeout, DNS, TLS: the page says unknown, never fails
        logger.warning("platform_apps_http_probe_failed", app=app_id, error=repr(e)[:200])
        card.update(status="unknown", error=type(e).__name__,
                    probe={"status": "unknown", "reason": f"health endpoint did not answer ({type(e).__name__})"})
        return card
    finally:
        if own:
            await client.aclose()
    hb, rb = health["body"] or {}, ready["body"] or {}
    sha = hb.get("git_sha")
    card.update(
        git_sha=sha, commit_url=info.commit_url(sha),
        health={"http_status": health["http_status"], "status": hb.get("status")},
        ready={"http_status": ready["http_status"], "status": rb.get("status"), "db": rb.get("db"),
               "schema_version": rb.get("schema_version")},
    )
    if health["http_status"] != 200 or hb.get("status") != "ok":
        card["status"] = "down"
    elif ready["http_status"] != 200 or rb.get("status") != "ok":
        card["status"] = "degraded"
    else:
        card["status"] = "ok"
    card["probe"] = {"status": card["status"], "reason": {
        "down": f"{health_path} answered {health['http_status']}",
        "degraded": f"{ready_path} answered {ready['http_status']}",
        "ok": "Health and readiness endpoints answer 200",
    }[card["status"]]}
    return card


async def _booking_card(client: Optional[httpx.AsyncClient] = None) -> dict:
    return await _http_health_card("booking", client)


async def _crm_card(client: Optional[httpx.AsyncClient] = None) -> dict:
    return await _http_health_card("crm", client)


def relay_authorization(request: Any) -> Optional[str]:
    """AA-941 — the ``Authorization`` value to relay to crm.ops.v1: the staff member's own verified Cognito access
    token. StaffBearerMiddleware drops the request's Authorization header once it has verified it (so the routes'
    own guards see a plain staff request) and keeps the token in ``scope["state"]["staff_access_token"]``; a request
    it did not verify (legacy session, secret only) falls back to the header, which ``_cognito_bearer`` then refuses
    when it is not a Cognito token."""
    from api.middleware.staff_bearer import STAFF_ACCESS_TOKEN

    state = request.scope.get("state")
    token = state.get(STAFF_ACCESS_TOKEN) if isinstance(state, dict) else None
    if isinstance(token, str) and token:
        return f"Bearer {token}"
    return request.headers.get("authorization")


def _cognito_bearer(authorization: Optional[str]) -> Optional[str]:
    """The request's Bearer when it is a Cognito access token (has ``iss``); the legacy HS256 admin JWT has none and
    the CRM would refuse it. The signature is not checked here: the CRM verifies it (route class OPS)."""
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    token = authorization.split(" ", 1)[1].strip()
    try:
        part = token.split(".")[1]
        payload = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except Exception:
        return None
    return token if isinstance(payload, dict) and isinstance(payload.get("iss"), str) and payload["iss"] else None


async def _crm_ops_get(authorization: Optional[str], path: str, params: Optional[dict] = None,
                       client: Optional[httpx.AsyncClient] = None) -> tuple[Optional[dict], dict]:
    """GET ``crm.ops.v1`` ``path`` with the staff member's own WORKFORCE token. ``(body, {})`` on a 200 JSON object,
    else ``(None, problem)`` with ``status`` unavailable | refused | error. Never raises."""
    info = APPS["crm"]
    token = _cognito_bearer(authorization)
    if token is None:
        return None, {"status": "unavailable", "reason": "cognito_session_required"}
    own = client is None
    client = client or httpx.AsyncClient(timeout=HTTP_TIMEOUT_S)
    try:
        r = await client.get(f"{info.extra['ops_base']}{path}", params=params,
                             headers={"Authorization": f"Bearer {token}"})
    except Exception as e:
        logger.warning("platform_apps_crm_ops_failed", path=path, error=repr(e)[:200])
        return None, {"status": "unavailable", "reason": type(e).__name__}
    finally:
        if own:
            await client.aclose()
    if r.status_code != 200:
        try:
            detail = r.json().get("detail")
        except Exception:
            detail = None
        return None, {"status": "refused" if r.status_code in (401, 403) else "error", "http_status": r.status_code,
                      "reason": detail if isinstance(detail, str) else None}
    try:
        body = r.json()
    except Exception as e:  # a 200 that is not JSON (a proxy page): an error, never a 500
        logger.warning("platform_apps_crm_ops_not_json", path=path, error=type(e).__name__)
        return None, {"status": "error", "http_status": r.status_code, "reason": "invalid_json"}
    if not isinstance(body, dict):
        return None, {"status": "error", "http_status": r.status_code, "reason": "unexpected_shape"}
    return body, {}


async def crm_jobs(authorization: Optional[str], client: Optional[httpx.AsyncClient] = None) -> dict:
    """AA-936 — the CRM's crm.job backlog per kind (crm.ops.v1 GET /jobs/summary), with the staff member's own
    WORKFORCE token. Never raises; never cached (the answer depends on the caller's token)."""
    body, problem = await _crm_ops_get(authorization, "/jobs/summary", client=client)
    if body is None:
        return {**problem, "kinds": []}
    kinds = body.get("kinds")
    return {"status": "ok", "generated_at": body.get("generated_at"), "kinds": kinds if isinstance(kinds, list) else []}


async def crm_llm_usage(authorization: Optional[str], start: date, end_exclusive: date,
                        client: Optional[httpx.AsyncClient] = None) -> dict:
    """AA-936 — the CRM's LLM usage for ``[start, end_exclusive)`` from its own gateway rollup (crm.ops.v1
    GET /llm/usage, ``crm.llm_usage_daily``: summed over every agency, no personal data), totalled per model for the
    cost page's CRM row. Never raises; ``status`` != ok when the CRM cannot be asked (no Cognito session, refused,
    down)."""
    last = end_exclusive - timedelta(days=1)
    body, problem = await _crm_ops_get(authorization, "/llm/usage",
                                       {"from": start.isoformat(), "to": max(start, last).isoformat()}, client)
    if body is None:
        return {**problem, "calls": 0, "cost_usd": 0.0, "by_model": []}
    by_model: dict[str, dict[str, Any]] = {}
    for row in body.get("rows") if isinstance(body.get("rows"), list) else []:
        if not isinstance(row, dict):
            continue
        try:
            calls, cost = int(row.get("calls") or 0), float(row.get("cost_usd") or 0)
        except (TypeError, ValueError):
            continue
        m = by_model.setdefault(str(row.get("model") or "unknown"), {"model": str(row.get("model") or "unknown"),
                                                                      "calls": 0, "cost_usd": 0.0})
        m["calls"] += calls
        m["cost_usd"] += cost
    models = sorted(by_model.values(), key=lambda m: -m["cost_usd"])
    for m in models:
        m["cost_usd"] = round(m["cost_usd"], 6)
    return {"status": "ok", "calls": sum(m["calls"] for m in models),
            "cost_usd": round(sum(m["cost_usd"] for m in models), 6), "by_model": models}


async def _tripplanner_card(pool: Any) -> dict:
    info = APPS["tripplanner"]
    card: dict[str, Any] = {"id": "tripplanner", "label": info.label, "repo_url": info.repo_url,
                            "status": "unknown", "probe": None,
                            "note": "TripPlanner has no health endpoint yet, so its status comes from its traffic "
                                    "(AWS CloudWatch, last 24 hours)."}
    try:
        rows = await pool.fetch(TP_SPEND_TODAY_SQL, list(TP_ONLINE_STAGES), info.llm_app)
    except Exception as e:
        logger.warning("platform_apps_tp_spend_failed", error=repr(e)[:200])
        card["spend_today"] = None
        card["error"] = type(e).__name__
        return card
    by_stage = [{"stage": r["stage"], "calls": r["calls"], "cost_usd": round(float(r["cost_usd"]), 6)}
                for r in rows]
    total = round(sum(s["cost_usd"] for s in by_stage), 6)
    pct = round(100.0 * total / TP_DEFAULT_DAILY_CAP_USD, 1) if TP_DEFAULT_DAILY_CAP_USD else None
    card["spend_today"] = {
        "cost_usd": total, "calls": sum(s["calls"] for s in by_stage), "by_stage": by_stage,
        "cap_usd": TP_DEFAULT_DAILY_CAP_USD, "pct_of_cap": pct,
        "cap_label": f"default cap {TP_DEFAULT_DAILY_CAP_USD:g} USD (TP env may differ)",
        "level": "ok" if pct is None or pct < TP_WARN_PCT else ("warn" if pct < 100 else "cap_reached"),
        "since": "UTC midnight", "stages": list(TP_ONLINE_STAGES),
    }
    return card


def apply_health(card: dict, traffic_section: Optional[dict]) -> dict:
    """Set the card's traffic summary and its one status (app_health.overall_status). Pure; unit-tested."""
    card["traffic"] = app_health.traffic_summary(traffic_section, card["id"])
    card["overall"] = app_health.overall_status(card.get("probe"), app_health.traffic_for(traffic_section, card["id"]))
    card["status"] = card["overall"]["status"]
    return card


async def _build(pool: Any, redis: Any = None, traffic: Optional[dict] = None) -> dict:
    async def no_traffic() -> Optional[dict]:
        return traffic

    traffic_call = no_traffic() if traffic is not None else app_health.traffic_section(redis)
    cis, booking, tp, crm, section = await asyncio.gather(
        _cis_card(pool), _booking_card(), _tripplanner_card(pool), _crm_card(), traffic_call)
    cards = [apply_health(c, section) for c in (cis, booking, tp, crm)]
    return {"apps": cards, "generated_at": datetime.now(timezone.utc).isoformat()}


async def collect_apps(pool: Any, redis: Any = None, refresh: bool = False, traffic: Optional[dict] = None) -> dict:
    """Every app's card, cached CACHE_TTL_S in Redis (fail-open: no Redis = build every time). ``traffic``: an
    already fetched traffic section (the overview passes its own, so one page view reads CloudWatch once)."""
    if redis is not None and not refresh:
        try:
            cached = await redis.get(CACHE_KEY)
            if cached:
                data = json.loads(cached)
                data["_cached"] = True
                return data
        except Exception as e:
            logger.warning("platform_apps_cache_read_failed", error=repr(e)[:200])
    data = await _build(pool, redis, traffic)
    if redis is not None:
        try:
            await redis.set(CACHE_KEY, json.dumps(data, default=str), ex=CACHE_TTL_S)
        except Exception as e:
            logger.warning("platform_apps_cache_write_failed", error=repr(e)[:200])
    data["_cached"] = False
    return data


async def collect_app(pool: Any, redis: Any, app_id: str, refresh: bool = False) -> Optional[dict]:
    if app_id not in APPS:
        return None
    data = await collect_apps(pool, redis, refresh=refresh)
    card = next((a for a in data["apps"] if a["id"] == app_id), None)
    if card is None:
        return None
    return {**card, "generated_at": data["generated_at"], "_cached": data["_cached"]}
