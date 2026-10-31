"""AA-1003 R2 (OPS-12) — the daily platform health digest: is anything broken or drifting that needs work today?

Rendered by report_generate for ICT day D right after the daily KPI snapshot (00:30 UTC of D + 1, 07:30 ICT), stored in
shared.report_run, e-mailed to the OPS-12 opt-in list (services/jobs/report_ops_jobs.py) and shown on /admin/reports.
A digest, not an alarm: SEV-worthy signals still page through the SNS alerts in real time. It is written every day
and is short when all is green, so a missing digest is itself a signal.

Sections and where each comes from (no LLM, no paid API; two cheap AWS reads the task role already has or is asked
for in docs/implementation-notes/AA-1003.md, "R2 follow-up requests"):
  apps        live at render time: CIS (its public origin, app_registry.cis_api_origin()), Booking and the partner
              CRM over their public /health + /health/ready (platform_apps.http_health_card, 2 s timeout each), the
              CIS workers alive in shared.job_worker and their commits (api and worker on the same commit; not
              compared when the CIS probe got no answer), TripPlanner listed as "no probe"; K-OPS-APPSGREEN = apps
              answering 200 + ok on both.
  alarms      CloudWatch DescribeAlarms (cloudwatch:DescribeAlarms, granted for /admin/overview): alarms in ALARM
              now per app. "Fired in 24 h" (K-OPS-ALARMS) needs DescribeAlarmHistory: not measured yet.
  jobs        shared.job: failed rows of day D per kind, queued rows overdue now, running rows past their lease.
  llm         api.services.llm_availability (shared.llm_call_log + shared.job, last 24 h) and K-LLM-FALLBACK-24H.
              AA-1018 provider section: per LLM connection its status, connection circuit and failure domain, the
              last daily probe (shared.job kind llm_connection_probe) and the report-only failure-domain rule; an
              enabled connection that refuses, a failed probe or a rule breach is an attention line.
              `blocked` / `degraded` need a look; so does `unknown` with no answered text call in 24 h (an idle route
              after a refusal reads `unknown` once its failed jobs leave the window: never counted green).
  probes      DATA-01: the nightly probe verdicts of day D (services/reports/probes.py, CloudWatch Logs).
  spend       K-LLM-COST-DAY of day D vs the median of the 14 days before (shared.kpi_daily).
  deploys     CIS worker tasks started and migrations registered (shared.schema_versions, every owner) in the 24 h
              before the digest.
  handoff     TP-03: K-TP-HANDOFFGAP / K-BK-PARKED of day D and the latest hourly check.
  waves       open shared.rerun_wave rows (information: a wave is planned work).
  reports     the register's overdue reports and failed runs (EX-06 rule).

Every section is read on its own: one that fails becomes a "section unavailable" line and an attention item, never a
failed digest (`collect`). `render` is pure and unit-tested.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from statistics import median
from typing import Any, Awaitable, Callable, Optional

import structlog

from services.reports import generate, handoff, periods, probes, store
from services.reports.generate import Rendered, fmt
from services.reports.registry import Registry, Report

logger = structlog.get_logger()

SPEND_MEDIAN_DAYS = 14
SPEND_FLAG_FACTOR = 2.0          # spend flagged above 2x the 14-day median ...
SPEND_FLAG_MIN_USD = 1.0         # ... and above 1 USD (Dev runs are bursty; cents are noise)
FALLBACK_RED = 0.05              # K-LLM-FALLBACK-24H threshold (< 5 %)
OVERDUE_QUEUED_AFTER = timedelta(minutes=30)
HTTP_TIMEOUT_S = 2.0
DIGEST_KPIS = ("K-LLM-COST-DAY", "K-LLM-FALLBACK-24H", "K-JOB-FAIL", "K-JOB-QUEUEAGE", "K-TP-HANDOFFGAP",
               "K-BK-PARKED", "K-DATA-PROBES", "K-DATA-STALE", "K-DATA-LIVENESS")

FAILED_BY_KIND_SQL = """
    SELECT kind, count(*)::int AS n FROM shared.job
     WHERE status = 'failed' AND finished_at >= $1 AND finished_at < $2
     GROUP BY kind ORDER BY 2 DESC, 1 LIMIT 10
"""
# job_queued_due_idx (run_after, created_at) WHERE status = 'queued'
OVERDUE_SQL = """
    SELECT count(*)::int AS n, min(run_after) AS oldest_due_at FROM shared.job
     WHERE status = 'queued' AND run_after <= $1
"""
# job_running_idx (kind, locked_until) WHERE status = 'running'
EXPIRED_LEASE_SQL = "SELECT count(*)::int FROM shared.job WHERE status = 'running' AND locked_until < now()"
WORKERS_STARTED_SQL = """
    SELECT coalesce(git_sha, 'unknown') AS git_sha, task_revision, count(*)::int AS tasks,
           min(started_at) AS first_started_at
      FROM shared.job_worker
     WHERE started_at >= $1
     GROUP BY 1, 2 ORDER BY 4
"""
MIGRATIONS_SQL = """
    SELECT version, description, applied_at FROM shared.schema_versions
     WHERE applied_at >= $1 ORDER BY applied_at LIMIT 50
"""
OPEN_WAVES_SQL = """
    SELECT name, country, created_at, cardinality(expected_tour_ids) AS tours FROM shared.rerun_wave
     WHERE status = 'open' ORDER BY created_at
"""
LAST_CHECK_SQL = """
    SELECT created_at, result FROM shared.job
     WHERE kind = $1 AND status = 'succeeded'
     ORDER BY created_at DESC LIMIT 1
"""


def _iso(v: Any) -> Any:
    return v.isoformat() if isinstance(v, (date, datetime)) else v


def _err(e: BaseException) -> str:
    return f"{type(e).__name__}: {str(e)[:200]}"


# ── loaders (each returns a JSON-able dict; collect() isolates failures) ────────────────────────────────────────


def _app_item(card: dict) -> dict:
    """A platform_apps health card -> the digest's app line (the card says `unknown` when nothing answered)."""
    status = "no_answer" if card.get("status") == "unknown" else card.get("status")
    return {"id": card["id"], "label": card["label"], "status": status,
            "reason": (card.get("probe") or {}).get("reason") or status, "git_sha": card.get("git_sha"),
            "schema_version": (card.get("ready") or {}).get("schema_version")}


def worker_parity(live_workers: list[dict], api_sha: Optional[str]) -> dict:
    """{"alive", "commits", "same_commit_as_api"}: True / False, or None when the API's commit is unknown (the CIS
    probe got no answer): nothing to compare, so no parity verdict. Pure."""
    shas: dict[str, int] = {}
    for w in live_workers:
        sha = w.get("git_sha") or "unknown"
        shas[sha] = shas.get(sha, 0) + 1
    same = None if not api_sha or not live_workers else set(shas) == {api_sha}
    return {"alive": len(live_workers), "commits": shas, "same_commit_as_api": same}


async def load_apps(conn) -> dict:
    import httpx

    from api.core.app_registry import APPS, CIS_HEALTH_PATHS, cis_api_origin
    from api.services import platform_apps
    from shared.jobs import queue

    targets: list[tuple[str, dict]] = [("cis", {"base": cis_api_origin(), "paths": CIS_HEALTH_PATHS})]
    targets += [(a, {}) for a in ("booking", "crm") if APPS[a].health_base and len(APPS[a].health_paths) == 2]
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S) as client:
        cards = await asyncio.gather(*(platform_apps.http_health_card(a, client, **kw) for a, kw in targets))
    items = [_app_item(c) for c in cards]
    items.append({"id": "tripplanner", "label": APPS["tripplanner"].label, "status": "no_probe",
                  "reason": "no health endpoint yet"})
    health = await queue.worker_health(conn)
    live = [w for w in health["workers"] if w.get("alive")]
    return {"items": items, "workers": worker_parity(live, items[0].get("git_sha"))}


async def load_alarms() -> dict:
    from api.services.ops_overview import cloudwatch

    env = cloudwatch.environment()
    try:
        alarms, _calls = await asyncio.to_thread(cloudwatch.fetch_alarms, cloudwatch.aws_client("cloudwatch"), env)
    except Exception as e:  # noqa: BLE001
        return {"status": "unavailable", "reason": "not_permitted" if cloudwatch.is_denied(e) else _err(e)}
    firing = [{"app": app, "name": a["name"], "since": a.get("since")}
              for app, bucket in sorted(alarms.items()) for a in bucket.get("firing") or []]
    return {"status": "ok", "firing": firing, "total": sum(b.get("total", 0) for b in alarms.values())}


async def load_jobs(conn, day: date, now: datetime) -> dict:
    start, end = periods.day_bounds(day)
    failed = [dict(r) for r in await conn.fetch(FAILED_BY_KIND_SQL, start, end)]
    overdue = await conn.fetchrow(OVERDUE_SQL, now - OVERDUE_QUEUED_AFTER)
    expired = await conn.fetchval(EXPIRED_LEASE_SQL)
    return {"failed_by_kind": failed, "failed_total": sum(r["n"] for r in failed),
            "overdue_queued": int((overdue or {}).get("n") or 0),
            "oldest_due_at": _iso((overdue or {}).get("oldest_due_at")), "expired_leases": int(expired or 0)}


# AA-1018: the latest daily LLM connection probe (services/jobs/llm_probe_job.py; off unless LLM_DAILY_PROBE=on).
LAST_PROBE_SQL = """
    SELECT id::text AS id, status, result, finished_at FROM shared.job
     WHERE kind = 'llm_connection_probe' AND finished_at >= $1 ORDER BY finished_at DESC LIMIT 1
"""


def _provider_rows(connections: list[dict]) -> list[dict]:
    """AA-1018: the provider section, one row per LLM connection (pure)."""
    out = []
    for c in connections:
        circuit = c.get("circuit") or {}
        out.append({"connection_key": c["connection_key"], "kind": c.get("kind"), "enabled": bool(c.get("enabled")),
                    "status": c.get("status"), "failure_domain": c.get("failure_domain"),
                    "open_models": c.get("open_models", 0), "last_ok_at": c.get("last_ok_at"),
                    "circuit": circuit.get("state", "closed"), "refusing": bool(circuit.get("refusing")),
                    "circuit_class": circuit.get("error_class"), "circuit_until": circuit.get("open_until")})
    return out


def _probe_summary(row) -> Optional[dict]:
    if row is None:
        return None
    res = handoff._json(row["result"]) or {}
    return {"job_id": row["id"], "status": row["status"], "finished_at": _iso(row["finished_at"]),
            "dry_run": res.get("dry_run"), "probed": res.get("probed"), "ok": res.get("ok"),
            "failed": res.get("failed"),
            "failures": [{k: f.get(k) for k in ("connection_key", "model_key", "error_class")}
                         for f in (res.get("results") or []) if f.get("ok") is False][:10]}


async def load_llm(conn, now: datetime) -> dict:
    from api.services import llm_availability

    out = await llm_availability.read(conn, hours=24, now=now)
    inv = out.get("invariant") or {}
    probe = await conn.fetchrow(LAST_PROBE_SQL, now - timedelta(hours=24))
    return {"status": out["status"], "last_ok_at": out["last_ok_at"], "cause": out["cause"],
            "calls": sum(p["calls"] for p in out["providers"]),
            "text_ok_calls": sum(p["ok_calls"] for p in out["providers"] if not p["embed"]),
            "providers": [{k: p[k] for k in ("provider", "account", "embed", "calls", "ok_calls")}
                          for p in out["providers"]],
            "failures": [{k: f.get(k) for k in ("error_class", "jobs", "last_at")} for f in out["failures"]],
            # AA-1018: per connection (circuit, failure domain), the failure-domain rule, the last daily probe
            "connections": _provider_rows(out.get("connections") or []),
            "invariant": {k: inv.get(k) for k in ("ready", "ok", "stages_checked", "breaches", "classes", "domains")},
            "probe": _probe_summary(probe)}


async def load_probes(day: date, now: datetime) -> dict:
    try:
        got = await probes.read_day(day, now=now)
    except probes.LogsUnavailable as e:
        return {"status": "unavailable", "reason": e.reason, "message": str(e)}
    return {"status": "ok", "window": got["window"], "passed": got["passed"], "expected": got["expected"],
            "probes": [r.as_dict() for r in got["probes"]]}


async def load_deploys(conn, now: datetime) -> dict:
    since = now - timedelta(hours=24)
    workers = [{**dict(r), "first_started_at": _iso(r["first_started_at"])}
               for r in await conn.fetch(WORKERS_STARTED_SQL, since)]
    migrations = [{"version": r["version"], "description": (r["description"] or "")[:120],
                   "applied_at": _iso(r["applied_at"])} for r in await conn.fetch(MIGRATIONS_SQL, since)]
    return {"since": since.isoformat(), "workers": workers, "migrations": migrations}


async def load_waves(conn) -> dict:
    rows = await conn.fetch(OPEN_WAVES_SQL)
    return {"open": [{"name": r["name"], "country": r["country"], "created_at": _iso(r["created_at"]),
                      "tours": r["tours"]} for r in rows]}


async def load_handoff(conn) -> dict:
    row = await conn.fetchrow(LAST_CHECK_SQL, handoff.CHECK_KIND)
    if row is None:
        return {"last_check": None}
    res = handoff._json(row["result"])
    return {"last_check": {"at": _iso(row["created_at"]), "gap": res.get("gap"), "sent": res.get("sent"),
                           "received": res.get("received"), "persistent": res.get("persistent"),
                           "window": res.get("window")}}


async def load_reports(conn, reg: Registry, now: datetime) -> dict:
    rows = generate.register_rows(reg, await store.last_runs(conn), now)
    flagged = [{"id": r["id"], "name": r["name_en"], "due": r["due"]["state"], "failed_7d": r["failed_7d"]}
               for r in rows if r["id"] != "OPS-12" and (r["due"]["state"] == "overdue" or r["failed_7d"])]
    return {"flagged": flagged}


async def collect(conn, reg: Registry, day: date, now: datetime) -> dict:
    """Every section, each isolated: a failure becomes facts['errors'][section]."""
    sections: dict[str, Callable[[], Awaitable[dict]]] = {
        "apps": lambda: load_apps(conn),
        "alarms": load_alarms,
        "jobs": lambda: load_jobs(conn, day, now),
        "llm": lambda: load_llm(conn, now),
        "probes": lambda: load_probes(day, now),
        "deploys": lambda: load_deploys(conn, now),
        "waves": lambda: load_waves(conn),
        "handoff": lambda: load_handoff(conn),
        "reports": lambda: load_reports(conn, reg, now),
    }
    facts: dict[str, Any] = {"errors": {}}
    for name, load in sections.items():   # one connection: sequential reads
        try:
            facts[name] = await load()
        except Exception as e:  # noqa: BLE001 — a digest is always written
            facts["errors"][name] = _err(e)
            logger.warning("ops_digest_section_failed", section=name, error=facts["errors"][name])
    return facts


# ── pure rendering ──────────────────────────────────────────────────────────────────────────────────────────────


def _series(rows: list[dict]) -> dict[tuple[str, str], dict[date, float]]:
    out: dict[tuple[str, str], dict[date, float]] = {}
    for r in rows:
        out.setdefault((r["kpi_id"], r["scope"]), {})[r["day"]] = generate._num(r["value"])
    return out


def _value(series: dict, kpi_id: str, day: date, scope: str = "all") -> Optional[float]:
    return series.get((kpi_id, scope), {}).get(day)


def spend_line(series: dict, day: date) -> dict:
    points = series.get(("K-LLM-COST-DAY", "all"), {})
    value = points.get(day)
    history = [points[d] for d in (day - timedelta(days=i) for i in range(1, SPEND_MEDIAN_DAYS + 1)) if d in points]
    med = median(history) if history else None
    flagged = (value is not None and value > SPEND_FLAG_MIN_USD
               and (med is None or value > SPEND_FLAG_FACTOR * med))
    by_app = {s.split(":", 1)[1]: v.get(day) for (k, s), v in series.items()
              if k == "K-LLM-COST-DAY" and s.startswith("app:") and v.get(day) is not None}
    return {"value": value, "median_14d": med, "days_in_median": len(history), "flagged": flagged, "by_app": by_app}


def attention(facts: dict, series: dict, day: date) -> list[str]:
    """The items that need a look today, in display order. Pure."""
    items: list[str] = []
    for name, err in (facts.get("errors") or {}).items():
        items.append(f"Section {name} could not be read ({err})")
    apps = facts.get("apps")
    if apps:
        for a in apps["items"]:
            if a["status"] not in ("ok", "no_probe"):
                items.append(f"{a['label']}: {a['status']} ({a['reason']})")
        w = apps["workers"]
        if not w["alive"]:
            items.append("CIS: no live worker")
        elif w["same_commit_as_api"] is False:      # None: the API's commit is unknown, nothing to compare
            items.append(f"CIS: workers run {', '.join(sorted(w['commits']))}, not the API's commit")
    alarms = facts.get("alarms")
    if alarms and alarms.get("status") == "ok" and alarms["firing"]:
        items.append(f"{len(alarms['firing'])} CloudWatch alarm(s) in ALARM: "
                     + ", ".join(a["name"] for a in alarms["firing"][:5]))
    elif alarms and alarms.get("status") != "ok":
        items.append(f"Alarms could not be read ({alarms.get('reason')})")
    jobs = facts.get("jobs")
    if jobs:
        if jobs["failed_total"]:
            items.append(f"{jobs['failed_total']} job(s) failed on {day.isoformat()}: "
                         + ", ".join(f"{r['kind']} {r['n']}" for r in jobs["failed_by_kind"][:5]))
        if jobs["overdue_queued"]:
            items.append(f"{jobs['overdue_queued']} queued job(s) overdue by more than 30 min")
        if jobs["expired_leases"]:
            items.append(f"{jobs['expired_leases']} running job(s) past their lease")
    llm = facts.get("llm")
    if llm and llm["status"] in ("blocked", "degraded"):
        cause = llm.get("cause") or {}
        items.append(f"LLM route {llm['status']}: {cause.get('error_class')} at {cause.get('at')}")
    elif llm and llm["status"] == "unknown" and not _text_ok_calls(llm):
        # 29/10/2026: Dev idle after the refusal, the failed jobs out of the window: "unknown" is not green
        items.append(f"LLM route unknown: no answered text call in the last 24 h (last answered "
                     f"{llm.get('last_ok_at') or 'never in the window'}); nothing shows the route works")
    if llm:
        items += provider_attention(llm)
    fallback = _value(series, "K-LLM-FALLBACK-24H", day)
    if fallback is not None and fallback >= FALLBACK_RED:
        items.append(f"LLM fallback rate {fallback * 100:.1f} % (target < 5 %)")
    pr = facts.get("probes")
    if pr and pr.get("status") == "ok":
        for p in pr["probes"]:
            if p["status"] != "passed":
                extra = f", {p['value_field']}={p['value']}" if p.get("value") is not None else ""
                items.append(f"Nightly probe {p['label']}: {p['status']}{extra}")
    elif pr:
        items.append(f"Nightly probe results unavailable ({pr.get('reason')})")
    spend = spend_line(series, day)
    if spend["flagged"]:
        med = "no history" if spend["median_14d"] is None else fmt(spend["median_14d"], "usd")
        items.append(f"LLM spend {fmt(spend['value'], 'usd')} on {day.isoformat()} (14-day median {med})")
    gap = _value(series, "K-TP-HANDOFFGAP", day)
    if gap:
        items.append(f"{gap:.0f} TripPlanner inquiries not received by Booking on {day.isoformat()} (UTC day)")
    last = (facts.get("handoff") or {}).get("last_check")
    if last and last.get("persistent"):
        items.append(f"Inquiry handoff gap {last.get('gap')} at the last hourly check")
    parked = _value(series, "K-BK-PARKED", day)
    if parked:
        items.append(f"{parked:.0f} inquiries parked or failed in the Booking inbox")
    for r in (facts.get("reports") or {}).get("flagged", []):
        items.append(f"Report {r['id']} {r['name']}: {r['due']}, {r['failed_7d']} failed run(s) in 7 days")
    return items


def provider_attention(llm: dict) -> list[str]:
    """AA-1018: attention lines of the provider section (pure): an enabled connection whose circuit refuses, a failed
    daily probe, a failure-domain rule breach (report only)."""
    items = []
    for c in llm.get("connections") or []:
        if c["enabled"] and c["refusing"]:
            until = f" until {c['circuit_until']}" if c["circuit"] == "open" and c.get("circuit_until") else ""
            items.append(f"LLM connection {c['connection_key']} paused ({c['circuit_class']}, circuit "
                         f"{c['circuit']}{until})")
    probe = llm.get("probe") or {}
    if probe.get("failed"):
        items.append(f"Daily LLM probe: {probe['failed']} of {probe.get('probed')} deployment(s) failed: "
                     + ", ".join(f"{f['connection_key']}/{f['model_key']} ({f['error_class']})"
                                 for f in probe.get("failures") or []))
    inv = llm.get("invariant") or {}
    if inv.get("ready") and inv.get("breaches"):
        by_class = ", ".join(f"{c['role']} {c['stages'] - c['ok']} of {c['stages']}" for c in inv.get("classes") or []
                             if c["stages"] - c["ok"])
        items.append(f"Failure-domain rule (report only): {inv['breaches']} stage(s) have no enabled, funded fallback "
                     f"in another failure domain ({by_class})")
    return items


def _text_ok_calls(llm: dict) -> int:
    if isinstance(llm.get("text_ok_calls"), int):
        return llm["text_ok_calls"]
    return sum(int(p.get("ok_calls") or 0) for p in llm.get("providers") or [] if not p.get("embed"))


def _short(sha: Optional[str]) -> str:
    return (sha or "?")[:8]


def render_digest(reg: Registry, report: Report, day: date, facts: dict, kpi_rows: list[dict],
                  generated_at: datetime) -> Rendered:
    series = _series(kpi_rows)
    items = attention(facts, series, day)
    headline = (f"{report.name_en} {day.isoformat()}: "
                + ("all green" if not items else f"{len(items)} item{'s' if len(items) > 1 else ''} need a look"))
    apps = facts.get("apps")
    with_probe = [a for a in (apps or {}).get("items", []) if a["status"] != "no_probe"]
    apps_green = sum(1 for a in with_probe if a["status"] == "ok")
    spend = spend_line(series, day)
    kpis_out = {k: _value(series, k, day) for k in DIGEST_KPIS}
    output = {"report_id": report.id, "name": report.name_en, "period": {"start": day.isoformat(),
                                                                         "end": day.isoformat()},
              "generated_at": generated_at.isoformat(), "generated_by": generate.GENERATED_BY, "headline": headline,
              "attention": items, "apps_green": {"green": apps_green, "with_probe": len(with_probe)},
              "kpis": kpis_out, "spend": spend, "facts": facts}
    md = [f"# {headline}", "",
          f"ICT day {day.isoformat()}; live checks at {generated_at.strftime('%Y-%m-%d %H:%M UTC')}. "
          "A digest, not an alarm: urgent signals still page in real time.", ""]
    md += ["## Needs a look", ""] + ([f"- {i}" for i in items] or ["- Nothing: all green."]) + [""]

    md += ["## Apps", ""]
    if apps:
        md += [f"Apps green (K-OPS-APPSGREEN): {apps_green} of {len(with_probe)} with a health check.", "",
               "| App | Status | Commit | Note |", "|---|---|---|---|"]
        for a in apps["items"]:
            md.append(f"| {a['label']} | {a['status']} | {_short(a.get('git_sha')) if a.get('git_sha') else '—'} | "
                      f"{a['reason']} |")
        w = apps["workers"]
        md += ["", f"CIS workers alive: {w['alive']} on "
                   + (", ".join(f"{_short(s)} ×{n}" for s, n in sorted(w['commits'].items())) or "—")
                   + {True: "; same commit as the API.", False: "; NOT the API's commit.",
                      None: "; the API's commit is unknown (no answer), not compared."}[w["same_commit_as_api"]], ""]
    alarms = facts.get("alarms")
    if alarms and alarms.get("status") == "ok":
        md += [f"Alarms in ALARM now: {len(alarms['firing'])} of {alarms['total']}"
               + (": " + ", ".join(a["name"] for a in alarms["firing"]) if alarms["firing"] else "") + ". "
               "Alarms fired in the last 24 h (K-OPS-ALARMS): not measured yet.", ""]

    jobs = facts.get("jobs")
    if jobs:
        md += ["## Jobs (shared.job)", "",
               f"Failed on {day.isoformat()}: {jobs['failed_total']}"
               + (" (" + ", ".join(f"{r['kind']} {r['n']}" for r in jobs["failed_by_kind"]) + ")"
                  if jobs["failed_by_kind"] else "")
               + f". Queued and overdue now: {jobs['overdue_queued']}. Running past the lease: "
               f"{jobs['expired_leases']}. Queue age p95 of the day: {fmt(kpis_out['K-JOB-QUEUEAGE'], 'seconds')}.",
               "Booking, TripPlanner and CRM job tables are not read here yet.", ""]

    llm = facts.get("llm")
    if llm:
        md += ["## LLM route", "",
               f"Availability (last 24 h): {llm['status']}; {llm['calls']} calls; last answered "
               f"{llm['last_ok_at'] or 'never in the window'}; fallback rate of the day "
               f"{fmt(kpis_out['K-LLM-FALLBACK-24H'], 'ratio')}.", ""]
        if llm["providers"]:
            md += ["| Provider | Account | Embeddings | Calls | Answered |", "|---|---|---|---|---|"]
            md += [f"| {p['provider']} | {p['account'] or '—'} | {'yes' if p['embed'] else 'no'} | {p['calls']} | "
                   f"{p['ok_calls']} |" for p in llm["providers"]]
            md.append("")
        md += render_providers(llm)

    pr = facts.get("probes")
    md += ["## Nightly probes (DATA-01)", ""]
    if pr and pr.get("status") == "ok":
        md += [f"Passed {pr['passed']} of {pr['expected']} readable probes (K-DATA-PROBES "
               f"{fmt(kpis_out['K-DATA-PROBES'], 'ratio')}).", "",
               "| Probe | UTC | Status | Value | Ran at |", "|---|---|---|---|---|"]
        md += [f"| {p['label']} | {p['cron_utc']} | {p['status']} | "
               f"{'—' if p['value'] is None else str(p['value_field']) + '=' + str(p['value'])} | "
               f"{p['ran_at'] or '—'} |" for p in pr["probes"]]
    elif pr:
        md.append(f"Probe results unavailable: {pr.get('message') or pr.get('reason')}.")
    md += ["", "Not visible from CIS: " + "; ".join(f"{n} ({t} UTC, {why})" for n, t, why in probes.NOT_VISIBLE)
           + ".", ""]

    md += ["## Spend", "",
           f"LLM spend on {day.isoformat()}: {fmt(spend['value'], 'usd')}; 14-day median "
           f"{fmt(spend['median_14d'], 'usd')} ({spend['days_in_median']} days)"
           + ("; by app: " + ", ".join(f"{a} {fmt(v, 'usd')}" for a, v in sorted(spend["by_app"].items()))
              if spend["by_app"] else "") + ". AWS spend is in the cost reports.", ""]

    deploys = facts.get("deploys")
    if deploys:
        md += ["## Deploys and migrations (24 h)", ""]
        md += [f"- CIS worker tasks started on {_short(w['git_sha'])} (revision {w['task_revision']}): {w['tasks']}, "
               f"first at {w['first_started_at']}" for w in deploys["workers"]] or ["- No CIS worker task started."]
        md += [f"- Migration {m['version']} registered at {m['applied_at']}: {m['description']}"
               for m in deploys["migrations"]] or ["- No migration registered."]
        md.append("")

    md += ["## Inquiry handoff (TP-03)", "",
           f"Gap on UTC day {day.isoformat()}: {fmt(kpis_out['K-TP-HANDOFFGAP'], 'count')}; parked or failed: "
           f"{fmt(kpis_out['K-BK-PARKED'], 'count')}."]
    last = (facts.get("handoff") or {}).get("last_check")
    md += ([f"Last hourly check {last['at']}: sent {last['sent']}, received {last['received']}, gap {last['gap']}"
            f"{' (persistent)' if last.get('persistent') else ''}.", ""] if last else ["No hourly check yet.", ""])

    waves = (facts.get("waves") or {}).get("open") or []
    md += ["## Open waves", ""] + ([f"- {w['name']} ({w['country'] or 'all'}, {w['tours']} tours) since "
                                     f"{w['created_at']}" for w in waves] or ["- None."]) + [""]
    flagged = (facts.get("reports") or {}).get("flagged") or []
    md += ["## Reports", ""] + ([f"- {r['id']} {r['name']}: {r['due']}, {r['failed_7d']} failed in 7 days"
                                 for r in flagged] or ["- No report overdue or failed."]) + [""]
    summary = headline[:1000]
    return Rendered(summary, output, "\n".join(md))


def render_providers(llm: dict) -> list[str]:
    """AA-1018: the provider section (Markdown lines, pure). Absent from a digest written before AA-1018."""
    conns = llm.get("connections")
    if conns is None:
        return []
    md = ["### Providers", "",
          "| Connection | Kind | On | Status | Connection circuit | Failure domain | Last answered |",
          "|---|---|---|---|---|---|---|"]
    for c in conns:
        circuit = c["circuit"] + (f" ({c['circuit_class']}{', refusing' if c['refusing'] else ''})"
                                  if c.get("circuit_class") else "")
        md.append(f"| {c['connection_key']} | {c.get('kind') or '—'} | {'yes' if c['enabled'] else 'no'} | "
                  f"{c.get('status') or '—'} | {circuit} | {c.get('failure_domain') or '—'} | "
                  f"{c.get('last_ok_at') or '—'} |")
    md.append("")
    probe = llm.get("probe")
    if probe:
        kind = "dry run" if probe.get("dry_run") else "live"
        md += [f"Daily probe ({kind}, {probe['status']}, {probe.get('finished_at')}): "
               f"{probe.get('probed') or 0} deployment(s), {probe.get('ok') or 0} answered, "
               f"{probe.get('failed') or 0} failed.", ""]
    else:
        md += ["Daily probe: no run in the last 24 h (the schedule is off unless LLM_DAILY_PROBE=on).", ""]
    inv = llm.get("invariant") or {}
    if inv.get("ready"):
        md += [f"Failure-domain rule (report only): {inv.get('stages_checked')} stage(s) checked, "
               f"{inv.get('breaches')} without a fallback in another failure domain; domains enabled: "
               + (", ".join(inv.get("domains") or []) or "—") + ".", ""]
    return md


async def render(conn, reg: Registry, report: Report, first: date, last: date, now: datetime) -> Rendered:
    facts = await collect(conn, reg, last, now)
    rows = await store.kpi_series(conn, list(DIGEST_KPIS), last - timedelta(days=SPEND_MEDIAN_DAYS), last)
    return render_digest(reg, report, last, facts, rows, now)
