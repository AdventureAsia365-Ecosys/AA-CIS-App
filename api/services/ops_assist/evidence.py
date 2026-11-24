"""AA-1154 (A0) — the evidence collector of the CIS admin assistant. Read-only, $0, existing sources only.

``collect()`` reads, side by side and each on its own (a source that fails is listed in ``unavailable`` with its
reason and never stops the others; a missing source is never read as healthy), into one plain snapshot:

- ``status``: the Ecosystem status page's own build (``ops_status.build_status``, the same range as the page, so the
  same numbers and the same Redis cache: the CloudWatch part costs nothing more), the Needs-attention item asked
  about, the attention list and the sources' freshness;
- ``connections``: every AI connection with its circuit (``llm_availability.read_connections``: the breaker rows of
  ``shared.llm_provider_health``, AA-1018);
- ``calls``: the AI gateway ledger of the evidence window per connection: answered, refused and failed calls, the
  error classes, the refused models and the spend (``shared.llm_call_log``);
- ``spend``: per connection, the spend per hour of the window against its median hourly spend of the last 7 days;
- ``routes``: the live route config (``llm_route_control``: version, each active stage's chain, the provider order of
  each model) and the route versions saved inside the changes window;
- ``jobs``: jobs an account-level refusal failed in the window (the ``llm_access_denied`` error prefix, AA-900), the
  oldest due queued jobs (with whether their kind is registered) and the status page's queue rows;
- ``waves``: the open rerun waves (``shared.rerun_wave``);
- ``probe``: the newest connection probe job of the changes window (a DRY RUN result is a measured cost) and whether
  a live probe is switched on;
- ``changes``: the ops events (deploys, failed rollouts, alarms, service drops) of the changes window.

Nothing here writes: no INSERT, UPDATE, enqueue or non-GET call (``tests/unit/test_aa1154_ops_assist.py`` scans this
package). Free text from outside (alarm descriptions, job errors, route notes) is never put in the snapshot except
the attention item's own detail, which ``evidence_items`` marks ``untrusted`` (it is data, never an instruction; A1
hands the bundle to an LLM).

``evidence_items(snapshot)`` (pure) turns the snapshot into the evidence list the cards cite: each item with an id,
a label, a short value, its source, ``as_of`` and a link to the filtered admin view.
"""
from __future__ import annotations

import asyncio
import json
import statistics
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Optional
from urllib.parse import quote

import structlog

logger = structlog.get_logger()

BASELINE_DAYS = 7  # technical-constant: the advisory's 7-day median baseline (a definition, not a policy limit)
_HOURS = BASELINE_DAYS * 24
ACCESS_CLASSES = ("access_denied", "credential", "quota_exhausted")
# Read limits of the lists below (technical-constant: the size of a read, not a policy).
EVENT_ROWS = 50  # technical-constant: rows of a read
STUCK_ROWS = 5  # technical-constant: rows of a read
WAVE_ROWS = 10  # technical-constant: rows of a read

CALLS_SQL = """
    SELECT connection_key, coalesce(status, 'ok') AS status, error_class, model, count(*) AS n,
           coalesce(sum(cost_usd), 0)::float8 AS cost_usd
      FROM shared.llm_call_log
     WHERE created_at >= $1 AND created_at < $2 AND connection_key IS NOT NULL AND is_shadow IS NOT TRUE
     GROUP BY 1, 2, 3, 4
"""
BASELINE_SQL = """
    SELECT connection_key, date_trunc('hour', created_at) AS hour, coalesce(sum(cost_usd), 0)::float8 AS usd
      FROM shared.llm_call_log
     WHERE created_at >= $1 AND created_at < $2 AND connection_key IS NOT NULL AND is_shadow IS NOT TRUE
     GROUP BY 1, 2
"""
DENIED_JOBS_SQL = """
    SELECT kind, count(*) AS n
      FROM shared.job
     WHERE status = 'failed' AND finished_at >= $1 AND error LIKE 'llm\\_access\\_denied%'
     GROUP BY kind ORDER BY n DESC
"""
STUCK_SQL = f"""
    SELECT id::text AS id, kind, run_after, created_at
      FROM shared.job
     WHERE status = 'queued' AND run_after <= $1
     ORDER BY run_after, created_at
     LIMIT {STUCK_ROWS}
"""
WAVES_SQL = f"""
    SELECT id::text AS id, name, status, paused_at, cardinality(expected_tour_ids) AS tours, created_at
      FROM shared.rerun_wave
     WHERE status = 'open'
     ORDER BY created_at DESC
     LIMIT {WAVE_ROWS}
"""
PROBE_SQL = """
    SELECT id::text AS id, status, payload, result, created_at, finished_at
      FROM shared.job
     WHERE kind = 'llm_connection_probe' AND created_at >= $1
     ORDER BY created_at DESC
     LIMIT 1
"""
EVENTS_SQL = f"""
    SELECT event_id, occurred_at, ended_at, kind, severity, app, subject
      FROM acp_shared.ops_event
     WHERE occurred_at >= $1 OR (ended_at IS NULL AND kind <> 'deploy')
     ORDER BY occurred_at DESC
     LIMIT {EVENT_ROWS}
"""
ROUTE_VERSIONS_SQL = """
    SELECT version, created_at, action, preset, reverted_from
      FROM shared.llm_route_config_version
     WHERE created_at >= $1
     ORDER BY version DESC
     LIMIT 10
"""
BUSINESS_OWNER_SQL = "SELECT is_business_owner AS ok FROM shared.admin_users WHERE id = $1::uuid"

PAGES = {
    "status": "/admin/overview",
    "providers": "/admin/ai-gateway?tab=providers",
    "routing": "/admin/ai-gateway?tab=routing",
    "budgets": "/admin/ai-gateway?tab=budgets",
    "jobs": "/admin/jobs",
    "settings": "/admin/platform/settings?app=cis&area=ops",
}


def _obj(v: Any) -> dict:
    """A jsonb value as a dict (asyncpg returns jsonb as text unless a codec is set). Pure."""
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            return {}
    return v if isinstance(v, dict) else {}


def _iso(v: Any) -> Optional[str]:
    return v.isoformat() if isinstance(v, datetime) else (str(v) if v is not None else None)


def calls_href(since: datetime, *, status: Optional[str] = None, error_class: Optional[str] = None) -> str:
    q = f"/admin/ai-gateway?tab=calls&c_since={quote(since.isoformat())}"
    if status:
        q += f"&c_status={status}"
    if error_class:
        q += f"&c_error_class={error_class}"
    return q


# ── pure shaping ─────────────────────────────────────────────────────────────────────────────────────────────────

def calls_by_connection(rows: list[dict]) -> dict[str, dict]:
    """{connection_key: {ok, refused, error, access_refused, classes, refused_models, refused_by_model, models,
    cost_usd}}. Pure."""
    out: dict[str, dict] = {}
    for r in rows:
        c = out.setdefault(r["connection_key"], {"ok": 0, "refused": 0, "error": 0, "access_refused": 0,
                                                 "classes": {}, "refused_models": [], "refused_by_model": {},
                                                 "models": [], "cost_usd": 0.0})
        n = int(r.get("n") or 0)
        status = r.get("status") or "ok"
        bucket = "ok" if status == "ok" else ("refused" if status == "refused" else "error")
        c[bucket] += n
        cls = r.get("error_class")
        if cls:
            c["classes"][cls] = c["classes"].get(cls, 0) + n
            if cls in ACCESS_CLASSES:
                c["access_refused"] += n
                if r.get("model"):
                    c["refused_by_model"][r["model"]] = c["refused_by_model"].get(r["model"], 0) + n
                    if r["model"] not in c["refused_models"]:
                        c["refused_models"].append(r["model"])
        if r.get("model") and r["model"] not in c["models"]:
            c["models"].append(r["model"])
        c["cost_usd"] = round(c["cost_usd"] + float(r.get("cost_usd") or 0.0), 6)
    for c in out.values():
        c["refused_models"].sort()
        c["models"].sort()
    return out


def spend_by_connection(window_rows: dict[str, dict], baseline_rows: list[dict], window_min: int) -> dict[str, dict]:
    """{connection_key: {window_usd, per_hour_usd, baseline_hour_usd}}: the window's spend scaled to an hour against
    the median of the 7 x 24 hourly sums before it (hours with no call count as 0). Pure."""
    hours: dict[str, list[float]] = {}
    for r in baseline_rows:
        hours.setdefault(r["connection_key"], []).append(float(r.get("usd") or 0.0))
    out = {}
    for key in set(hours) | set(window_rows):
        vals = hours.get(key, [])
        padded = vals + [0.0] * max(0, _HOURS - len(vals))
        window = float((window_rows.get(key) or {}).get("cost_usd") or 0.0)
        out[key] = {"window_usd": round(window, 4), "per_hour_usd": round(window * 60.0 / max(window_min, 1), 4),
                    "baseline_hour_usd": round(statistics.median(padded), 4) if padded else 0.0}
    return out


def stages_by_connection(routes: dict) -> dict[str, dict]:
    """{connection_key: {stages: [...], primary: [...]}}: the active stages whose chain holds a model with a
    deployment on the connection, and those whose first model is tried on it first. Pure."""
    order = {p["model_key"]: [d["connection_key"] for d in p.get("deployments") or []
                              if d.get("enabled") and d.get("connection_enabled", True)]
             for p in routes.get("provider_order") or []}
    out: dict[str, dict] = {}
    for s in routes.get("stages") or []:
        if s.get("is_active") is False:
            continue
        chain = s.get("chain") or []
        for i, model in enumerate(chain):
            for j, key in enumerate(order.get(model, [])):
                entry = out.setdefault(key, {"stages": [], "primary": []})
                if s["stage"] not in entry["stages"]:
                    entry["stages"].append(s["stage"])
                if i == 0 and j == 0 and s["stage"] not in entry["primary"]:
                    entry["primary"].append(s["stage"])
    for v in out.values():
        v["stages"].sort()
        v["primary"].sort()
    return out


def probe_summary(row: Optional[dict]) -> Optional[dict]:
    """The newest probe job: status, DRY RUN or live, its estimate and per-connection outcome of a live run. Pure."""
    if not row:
        return None
    payload, result = _obj(row.get("payload")), _obj(row.get("result"))
    by_conn: dict[str, dict] = {}
    for m in result.get("matrix") or []:
        if not isinstance(m, dict) or not m.get("connection_key"):
            continue
        c = by_conn.setdefault(m["connection_key"], {"ok": 0, "refused": 0})
        c["ok" if m.get("ok") else "refused"] += 1
    est = result.get("estimated_usd")
    return {"job_id": row.get("id"), "status": row.get("status"), "dry_run": bool(payload.get("dry_run", True)),
            "estimated_usd": float(est) if isinstance(est, (int, float)) else None,
            "created_at": _iso(row.get("created_at")), "finished_at": _iso(row.get("finished_at")),
            "by_connection": by_conn}


def find_item(status: dict, item_id: str) -> Optional[dict]:
    for i in ((status.get("verdict") or {}).get("attention") or []):
        if i.get("id") == item_id:
            return i
    return None


def _slim_item(i: dict) -> dict:
    return {k: i.get(k) for k in ("id", "severity", "app", "title", "detail", "since", "source", "status", "links",
                                  "msg")}


def status_part(status: dict) -> dict:
    v = status.get("verdict") or {}
    jobs = ((status.get("sections") or {}).get("jobs") or {})
    queues = (jobs.get("data") or {}).get("queues") or [] if jobs.get("status") == "ok" else []
    return {"verdict": v.get("status"), "attention": [_slim_item(i) for i in v.get("attention") or []],
            "unknown": v.get("unknown") or [],
            "sources": [{k: s.get(k) for k in ("section", "state", "as_of")} for s in status.get("sources") or []],
            "queues": [{k: q.get(k) for k in ("id", "name", "app", "status", "queued", "running", "oldest_wait_min",
                                               "workers", "level", "failed")} for q in queues],
            "generated_at": status.get("generated_at")}


# ── reads ─────────────────────────────────────────────────────────────────────────────────────────────────────────

async def _guard(name: str, coro: Awaitable[Any], unavailable: dict) -> Any:
    try:
        return await coro
    except Exception as e:  # noqa: BLE001 — one failed source never stops the others; it is listed, never "healthy"
        logger.warning("ops_assist_source_failed", source=name, error=repr(e)[:200])
        unavailable[name] = type(e).__name__
        return None


async def _connections(pool: Any, now: datetime) -> list[dict]:
    from api.services import llm_availability

    async with pool.acquire() as conn:
        ready, rows = await llm_availability.read_connections(conn, now=now)
    keep = ("connection_key", "kind", "label", "enabled", "status", "open_models", "known_refused_models",
            "last_ok_at", "last_error_at", "last_error_class", "failure_domain")
    return [{**{k: c.get(k) for k in keep}, "circuit": c.get("circuit") or {}} for c in rows] if ready else []


async def _routes(pool: Any) -> dict:
    from api.services import llm_route_control

    world = await llm_route_control.read_world(pool)
    st = llm_route_control.state_of(world)
    return {"version": st.get("version"), "updated_at": st.get("updated_at"), "order_mode": st.get("order_mode"),
            "stages": [{k: s.get(k) for k in ("stage", "role", "is_active", "chain")} for s in st.get("stages") or []],
            "provider_order": [{"model_key": p["model_key"],
                                "deployments": [{k: d.get(k) for k in ("connection_key", "priority", "enabled",
                                                                        "connection_enabled")}
                                                for d in p.get("deployments") or []]}
                               for p in st.get("provider_order") or []]}


async def _rows(pool: Any, sql: str, *args: Any) -> list[dict]:
    return [dict(r) for r in await pool.fetch(sql, *args)]


async def business_owner(pool: Any, staff_id: Optional[str]) -> bool:
    """Is the caller the business owner (``shared.admin_users.is_business_owner``, CIS 462)? False when unknown."""
    if not staff_id:
        return False
    try:
        return bool(await pool.fetchval(BUSINESS_OWNER_SQL, staff_id))
    except Exception as e:  # noqa: BLE001 — unknown = not the business owner: the full view, still read-only
        logger.warning("ops_assist_business_owner_read_failed", error=repr(e)[:200])
        return False


async def _none() -> None:
    return None


async def collect(pool: Any, redis: Any, *, item_id: str, limits: dict, range_key: str = "24h",
                  authorization: Optional[str] = None, now: Optional[datetime] = None,
                  include_status: bool = True) -> dict:
    """The snapshot for one Needs-attention item or assistant signal (see the module docstring). Never raises for a
    source: it is listed in ``unavailable``. ``include_status`` False skips the status page build (the signal list
    only needs the gateway reads)."""
    from api.services import ops_status
    from services.jobs import llm_probe_job
    from shared.jobs.registry import kinds
    from shared.jobs.worker import load_kinds

    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    win = now - timedelta(minutes=int(limits["evidence_window_min"]))
    changes_since = now - timedelta(minutes=int(limits["changes_window_min"]))
    unavailable: dict[str, str] = {}
    g = _guard
    (status, connections, calls_rows, baseline_rows, routes, route_versions, denied, stuck, waves, probe_rows,
     events) = await asyncio.gather(
        g("status", ops_status.build_status(pool, redis, range_key=range_key, authorization=authorization, now=now),
          unavailable) if include_status else _none(),
        g("connections", _connections(pool, now), unavailable),
        g("calls", _rows(pool, CALLS_SQL, win, now), unavailable),
        g("spend_baseline", _rows(pool, BASELINE_SQL, win - timedelta(hours=_HOURS), win), unavailable),
        g("routes", _routes(pool), unavailable),
        g("route_versions", _rows(pool, ROUTE_VERSIONS_SQL, changes_since), unavailable),
        g("denied_jobs", _rows(pool, DENIED_JOBS_SQL, win), unavailable),
        g("stuck_jobs", _rows(pool, STUCK_SQL, now), unavailable),
        g("waves", _rows(pool, WAVES_SQL), unavailable),
        g("probe", _rows(pool, PROBE_SQL, changes_since), unavailable),
        g("changes", _rows(pool, EVENTS_SQL, changes_since), unavailable),
    )
    load_kinds()
    registered = set(kinds())
    calls = calls_by_connection(calls_rows or []) if calls_rows is not None else {}
    st = status_part(status) if status else None
    return {
        "as_of": now.isoformat(), "range": range_key, "item_id": item_id,
        "window": {"since": win.isoformat(), "minutes": int(limits["evidence_window_min"])},
        "changes_window": {"since": changes_since.isoformat(), "minutes": int(limits["changes_window_min"])},
        "status": st,
        "item": find_item(status, item_id) if status else None,
        "connections": connections,
        "calls": calls if calls_rows is not None else None,
        "spend": spend_by_connection(calls, baseline_rows or [], int(limits["evidence_window_min"]))
        if calls_rows is not None and baseline_rows is not None else None,
        "routes": routes,
        "route_versions": [{"version": r["version"], "created_at": _iso(r["created_at"]), "action": r["action"],
                            "preset": r.get("preset"), "reverted_from": r.get("reverted_from")}
                           for r in route_versions or []] if route_versions is not None else None,
        "jobs": None if denied is None and stuck is None else {
            "denied": [{"kind": r["kind"], "n": int(r["n"])} for r in denied or []],
            "stuck": [{"id": r["id"], "kind": r["kind"], "run_after": _iso(r["run_after"]),
                       "registered": r["kind"] in registered} for r in stuck or []],
            "queues": (st or {}).get("queues") or []},
        "waves": [{"id": r["id"], "name": r["name"], "paused": r.get("paused_at") is not None,
                   "tours": int(r.get("tours") or 0), "created_at": _iso(r.get("created_at"))} for r in waves or []]
        if waves is not None else None,
        "probe": {"switch_on": llm_probe_job.schedule_enabled(),
                  "last": probe_summary((probe_rows or [None])[0]) if probe_rows else None},
        "changes": [{"id": f"e{r['event_id']}", "t": _iso(r["occurred_at"]), "t2": _iso(r.get("ended_at")),
                     "kind": r["kind"], "severity": r["severity"], "app": r.get("app") or "shared",
                     "subject": r["subject"]} for r in events or []] if events is not None else None,
        "history": {},   # A2: measured outcomes per action ("right k of n"); none are recorded in A0
        "unavailable": unavailable,
    }


# ── the evidence list the cards cite ──────────────────────────────────────────────────────────────────────────────

def _ev(eid: str, label: str, value: str, source: str, as_of: Optional[str], href: str, **extra: Any) -> dict:
    return {"id": eid, "label": label, "value": value, "source": source, "as_of": as_of, "href": href, **extra}


def _since(s: dict) -> Optional[datetime]:
    return datetime.fromisoformat(s["window"]["since"]) if s.get("window") else None


def _item_ev(s: dict) -> list[dict]:
    item = s.get("item")
    if not item:
        return []
    return [_ev("item", "Needs attention", item.get("title") or item.get("id"), item.get("source") or "status",
                item.get("since") or s.get("as_of"), PAGES["status"], untrusted_detail=item.get("detail") or None,
                severity=item.get("severity"))]


def _conn_ev(s: dict) -> list[dict]:
    out = []
    for c in s.get("connections") or []:
        circ = c.get("circuit") or {}
        refusing = f", refusing ({circ.get('error_class')})" if circ.get("refusing") else ""
        value = (f"circuit {circ.get('state') or 'closed'}{refusing}; {c.get('open_models') or 0} model(s) open; "
                 f"{len(c.get('known_refused_models') or [])} known refused; last answered "
                 f"{c.get('last_ok_at') or 'never'}")
        out.append(_ev(f"conn:{c['connection_key']}", f"Connection {c.get('label') or c['connection_key']}", value,
                       "AI gateway breaker (shared.llm_provider_health)", s.get("as_of"), PAGES["providers"]))
    return out


def _calls_ev(s: dict) -> list[dict]:
    out = []
    since = _since(s)
    for key, c in sorted((s.get("calls") or {}).items()):
        refusals = (f"; account-level refusals {c['access_refused']} on {', '.join(c['refused_models'])}"
                    if c["access_refused"] else "")
        value = f"{c['ok']} answered, {c['refused']} refused, {c['error']} failed in {s['window']['minutes']} min"
        href = calls_href(since, status="refused" if c["refused"] else None) if since else PAGES["providers"]
        out.append(_ev(f"calls:{key}", f"Calls on {key}", value + refusals, "shared.llm_call_log", s.get("as_of"),
                       href))
    return out


def _spend_ev(s: dict) -> list[dict]:
    out = []
    since = _since(s)
    for key, sp in sorted((s.get("spend") or {}).items()):
        if not sp.get("window_usd") and not sp.get("baseline_hour_usd"):
            continue
        out.append(_ev(f"spend:{key}", f"Spend on {key}",
                       f"{sp['per_hour_usd']:g} USD/h now against a median of {sp['baseline_hour_usd']:g} USD/h "
                       f"over {BASELINE_DAYS} days", "shared.llm_call_log", s.get("as_of"),
                       calls_href(since) if since else PAGES["budgets"]))
    return out


def _route_ev(s: dict) -> list[dict]:
    out = []
    routes = s.get("routes")
    if routes:
        for key, v in sorted(stages_by_connection(routes).items()):
            out.append(_ev(f"stages:{key}", f"Stages that can use {key}",
                           f"{len(v['stages'])} stage(s), first choice for {len(v['primary'])}",
                           "route control (live config)", s.get("as_of"), PAGES["routing"], stages=v["stages"],
                           primary=v["primary"]))
        out.append(_ev("routes", "Route config", f"version {routes.get('version')}, order mode "
                       f"{routes.get('order_mode')}", "route control", routes.get("updated_at") or s.get("as_of"),
                       PAGES["routing"]))
    for rv in s.get("route_versions") or []:
        preset = f" ({rv['preset']})" if rv.get("preset") else ""
        out.append(_ev(f"route_version:{rv['version']}", f"Route version {rv['version']} saved",
                       f"{rv['action']}{preset}", "shared.llm_route_config_version", rv["created_at"],
                       PAGES["routing"]))
    return out


def _job_ev(s: dict) -> list[dict]:
    out = []
    jobs = s.get("jobs") or {}
    as_of = s.get("as_of")
    if jobs.get("denied"):
        n = sum(d["n"] for d in jobs["denied"])
        out.append(_ev("jobs:denied", "Jobs failed by an AI account refusal",
                       f"{n} job(s): " + ", ".join(f"{d['kind']} {d['n']}" for d in jobs["denied"]),
                       "shared.job (llm_access_denied)", as_of, f"{PAGES['jobs']}?q=llm_access_denied"))
    for j in jobs.get("stuck") or []:
        unregistered = "" if j["registered"] else "; kind not registered"
        out.append(_ev(f"job:{j['id']}", f"Queued job {j['kind']}", f"due since {j['run_after']}{unregistered}",
                       "shared.job", as_of, f"{PAGES['jobs']}?job={j['id']}"))
    for q in jobs.get("queues") or []:
        if q.get("status") != "ok":
            continue
        workers = q.get("workers") if q.get("workers") is not None else "not measured"
        out.append(_ev(f"queue:{q['id']}", f"Queue {q['name']}",
                       f"{q.get('queued')} queued, {q.get('running')} running, oldest wait {q.get('oldest_wait_min')}"
                       f" min, workers {workers}", "status page (jobs)", as_of, PAGES["jobs"]))
    return out


def _wave_ev(s: dict) -> list[dict]:
    return [_ev(f"wave:{w['id']}", f"Open wave {w['name']}",
                f"{w['tours']} tour(s), {'paused' if w['paused'] else 'running'}", "shared.rerun_wave",
                w.get("created_at") or s.get("as_of"), PAGES["jobs"]) for w in s.get("waves") or []]


def _probe_ev(s: dict) -> list[dict]:
    probe = s.get("probe") or {}
    last = probe.get("last")
    switch = "live probe switched on" if probe.get("switch_on") else "live probe switched off (DRY RUN only)"
    if last:
        estimate = f", estimate {last['estimated_usd']:g} USD" if last.get("estimated_usd") is not None else ""
        run = f"; last run {last['status']} ({'DRY RUN' if last['dry_run'] else 'live'}){estimate}"
    else:
        run = "; no run in the changes window"
    return [_ev("probe", "Connection probe", switch + run, "shared.job (llm_connection_probe)",
                (last or {}).get("finished_at") or s.get("as_of"),
                f"{PAGES['jobs']}?job={last['job_id']}" if last else PAGES["jobs"])]


def _changes_ev(s: dict) -> list[dict]:
    ch = s.get("changes")
    if ch is None:
        return []
    kinds_n: dict[str, int] = {}
    for e in ch:
        kinds_n[e["kind"]] = kinds_n.get(e["kind"], 0) + 1
    value = (", ".join(f"{k} {n}" for k, n in sorted(kinds_n.items()))
             or f"no ops event in {s['changes_window']['minutes']} min")
    return [_ev("changes", "What changed", value, "acp_shared.ops_event", s.get("as_of"), PAGES["status"])]


def _unavailable_ev(s: dict) -> list[dict]:
    return [_ev(f"unavailable:{name}", f"Not read: {name}", why, "evidence collector", s.get("as_of"), PAGES["status"],
                unavailable=True) for name, why in sorted((s.get("unavailable") or {}).items())]


_PARTS = (_item_ev, _conn_ev, _calls_ev, _spend_ev, _route_ev, _job_ev, _wave_ev, _probe_ev, _changes_ev,
          _unavailable_ev)


def evidence_items(s: dict) -> list[dict]:
    """Every evidence item of a snapshot, each with an id the cards cite and a link. Pure."""
    out: list[dict] = []
    for part in _PARTS:
        out += part(s)
    return out
