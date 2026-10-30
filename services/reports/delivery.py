"""AA-1003 R4 part 2 — delivery and state reports: DEV-01 monthly delivery and DORA, DEV-03 weekly engineering flow,
OPS-15 session-close state snapshot.

  DEV-03  weekly (ISO week): CIS deploys to Dev (a worker start whose commit differs from the commit of the worker
          started before it, shared.job_worker git_sha; a deploy back to a commit seen earlier is a rollback), the
          migrations every owner registered (shared.schema_versions, DATA-03's reader). Linear (issues shipped, In
          Progress age, quota), open PRs and the master plan slots are not read by the worker: "Source not
          available" with where they live. The deploy list is kept in the output so DEV-01 can count a month after
          shared.job_worker has pruned its rows (7 days after a worker's last heartbeat).
  DEV-01  monthly: K-DORA-DF (CIS deploys per week) and the rollback part of K-DORA-CFR from the month's DEV-03 outputs
          plus the worker rows still kept; migrations per owner of the month. Lead time, the rest of the change
          failure rate, time to restore, CI failure rate on main, release versions: GitHub / postmortem sources the
          worker does not read.
  OPS-15  event (semi): the state at generation, machine-readable (the output JSON is the state-snapshot.json the
          session close needs): ECS services of the cluster (task definition, image tag, desired / running, rollout
          state; ecs:DescribeServices + ecs:DescribeTaskDefinition, already granted, $0), app health over the public
          /health + /health/ready (the OPS-12 probe), CIS worker commit parity, the newest migration per owner, CIS
          jobs queued / running, open rerun waves. Other owners' job tables, Lambda versions, Vercel commits, open PRs
          and repo heads are named "Source not available".

Stored rows, the ECS describe calls of OPS-15 and the public health probes only; no LLM, no paid API.
"""
from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta
from typing import Any, Optional

from services.reports import money_sources as src
from services.reports import periods
from services.reports.generate import Rendered
from services.reports.money import NOT_WIRED, _kpi_line, failed, ratio, table
from services.reports.registry import Registry, Report
from services.reports.reliability import (ICT_NOTE, _iso, _render, _unreadable, schema_changes, schema_versions)

NOT_AVAILABLE = "Source not available"
CFR_FLAG = 0.15                  # K-DORA-CFR: < 15 %
DAYS_PER_WEEK = 7                # K-DORA-DF is deploys per week; not a quality pass line
HISTORY_BEFORE = timedelta(days=8)   # worker rows read before the window, to know the commit live at its start

# Every worker start of the window and the week before (shared.job_worker keeps 7 days after the last heartbeat),
# plus the newest start with a commit before that cutoff (`baseline`): a worker that started earlier and still
# heartbeats keeps its row, and without it the first deploy after a quiet stretch of 8+ days has no predecessor and is
# not counted. The baseline row gives the commit live at the start only; it does not extend the covered days.
WORKER_STARTS_SQL = """
    (SELECT coalesce(git_sha, 'unknown') AS git_sha, task_revision, started_at, false AS baseline
       FROM shared.job_worker
      WHERE started_at >= $1 AND started_at < $2)
    UNION ALL
    (SELECT git_sha, task_revision, started_at, true AS baseline
       FROM shared.job_worker
      WHERE started_at < $1 AND git_sha IS NOT NULL
      ORDER BY started_at DESC
      LIMIT 1)
    ORDER BY started_at
"""
DEV03_RUNS_SQL = """
    SELECT period_start, period_end, output FROM shared.report_run
     WHERE report_id = 'DEV-03' AND status = 'succeeded' AND period_end >= $1 AND period_start <= $2
     ORDER BY period_start, created_at DESC
"""
JOBS_ACTIVE_SQL = """
    SELECT kind, status, count(*)::int AS n, min(created_at) AS oldest
      FROM shared.job
     WHERE status IN ('queued', 'running')
     GROUP BY 1, 2 ORDER BY 1, 2
"""
OPEN_WAVES_SQL = """
    SELECT name, country, created_at, cardinality(expected_tour_ids) AS tours FROM shared.rerun_wave
     WHERE status = 'open' ORDER BY created_at
"""


def _short(sha: Any) -> str:
    return (str(sha) if sha else "unknown")[:12]


# ── deploys (pure) ───────────────────────────────────────────────────────────────────────────────────────────────


def deploys(starts: Any, start: datetime, end: datetime) -> Optional[list[dict]]:
    """Worker starts -> CIS deploys inside [start, end): a start whose commit differs from the commit live before it.
    `rollback` = the commit was live earlier in the rows read. None when the rows could not be read."""
    if starts is None or failed(starts):
        return None
    out: list[dict] = []
    seen: list[str] = []
    live: Optional[str] = None
    for r in sorted(starts, key=lambda r: r["started_at"]):
        sha = r["git_sha"] or "unknown"
        if sha == live or sha == "unknown":
            continue
        if live is not None:
            if start <= r["started_at"] < end:
                out.append({"git_sha": sha, "task_revision": r.get("task_revision"), "at": _iso(r["started_at"]),
                            "previous": live, "rollback": sha in seen})
            seen.append(live)
        live = sha
    return out


def merge_deploys(*lists: Optional[list[dict]]) -> list[dict]:
    """Union by (commit, start time), oldest first (DEV-03 outputs overlap the live rows)."""
    by_key: dict[tuple, dict] = {}
    for items in lists:
        for d in items or []:
            by_key.setdefault((d.get("git_sha"), str(d.get("at"))), d)
    return sorted(by_key.values(), key=lambda d: str(d.get("at")))


# ═══ DEV-03 weekly engineering flow ═══════════════════════════════════════════════════════════════════════════

DEV03_UNAVAILABLE = (
    ("Issues Done with live verify", "K-DEL-SHIPPED", "Linear (team AA_Ecosys); the session close counts them"),
    ("In Progress older than 7 days", "K-FLOW-WIPAGE", "Linear"),
    ("Open PRs per repo and age", "K-FLOW-PRAGE", "GitHub, six repos (no GitHub token on the worker)"),
    ("Linear quota (Free plan, 250 unarchived)", "K-FLOW-QUOTA", "Linear; the session close exports and archives"),
    ("Master plan slot progress", None, "root docs/backlog/tracker/plan.json (not in the docs bundle the API reads)"),
)


async def worker_starts(conn, start: datetime, end: datetime) -> list[dict]:
    return [dict(r) for r in await conn.fetch(WORKER_STARTS_SQL, start - HISTORY_BEFORE, end)]


async def collect_dev03(conn, first: date, last: date) -> dict:
    start, end = periods.range_bounds(first, last)
    return await src.run_parts({"starts": lambda: worker_starts(conn, start, end),
                                "versions": lambda: schema_versions(conn)})


def _deploys_md(dl: Optional[list[dict]], why: str) -> list[str]:
    if dl is None:
        return [f"- Not readable: {why}"]
    if not dl:
        return ["- No CIS deploy in the period."]
    return table(["Started (UTC)", "Commit", "Task revision", "Replaced", "Rollback"],
                 [[str(d["at"])[:16], _short(d["git_sha"]), d.get("task_revision"), _short(d.get("previous")),
                   "yes" if d.get("rollback") else ""] for d in dl])


def _schema_md(ch: dict) -> list[str]:
    if "error" in ch:
        return [f"- Not readable: {ch['error']}"]
    return table(["Owner", "Registered in the period", "Newest version"],
                 [[o, v["applied"], v["latest"]] for o, v in sorted(ch["owners"].items())])


def render_dev03(reg: Registry, report: Report, first: date, last: date, facts: dict,
                 generated_at: datetime) -> Rendered:
    start, end = periods.range_bounds(first, last)
    dl = deploys(facts.get("starts"), start, end)
    ch = schema_changes(facts.get("versions"), first, last)
    kpis = [_kpi_line(kid, None, NOT_WIRED, f"{NOT_AVAILABLE.lower()}: {where}")
            for _s, kid, where in DEV03_UNAVAILABLE if kid]
    applied = None if "error" in ch else len(ch["applied"])
    headline = (f"Engineering flow {first.isoformat()} to {last.isoformat()}: "
                + (f"{len(dl)} CIS deploy(s)" if dl is not None else "CIS deploys not readable")
                + (f", {applied} migration(s) registered" if applied is not None else "")
                + "; Linear and GitHub counts not available to the worker")
    body = ["## CIS deploys to Dev", "",
            "A deploy is a worker start on a commit other than the one live before it (shared.job_worker); a deploy "
            "back to a commit live earlier is a rollback. Booking, CRM and TripPlanner deploys are GitHub workflow "
            "runs: not counted here.", ""]
    body += _deploys_md(dl, failed(facts.get("starts")) or "not read")
    body += ["", "## Schema and contract changes (DATA-03)", ""] + _schema_md(ch)
    body += ["", f"## {NOT_AVAILABLE}", ""]
    body += table(["Section", "KPI", "Where it lives today"],
                  [[s, kid or "—", where] for s, kid, where in DEV03_UNAVAILABLE])
    body.append("")
    return _render(reg, report, first, last, generated_at, f"Engineering flow {first.isoformat()} to "
                   f"{last.isoformat()}", headline, kpis, body, ICT_NOTE, cis_deploys=dl, schema=ch,
                   not_available=[s for s, _k, _w in DEV03_UNAVAILABLE], sources_unreadable=_unreadable(facts))


# ═══ DEV-01 monthly delivery and DORA ══════════════════════════════════════════════════════════════════════════

DEV01_UNAVAILABLE = (
    ("Lead time for changes", "K-DORA-LT", "GitHub: PR first commit to the deploy run holding the merge commit"),
    ("Time to restore", "K-DORA-MTTR", "OPS-04 postmortems (root docs/ops/postmortems/)"),
    ("CI failure rate on main", "K-CI-MAINFAIL", "GitHub Actions ci.yml runs on main"),
    ("Release versions shipped (DEV-02)", None, "GitHub releases (release-please)"),
    ("Post-deploy verify failures", None, "Deploy Dev workflow runs (GitHub Actions)"),
    ("Deploys of Booking, CRM, TripPlanner and the Vercel apps", None, "their deploy workflow runs and Vercel"),
)


async def dev03_outputs(conn, first: date, last: date) -> list[dict]:
    out = []
    for r in await conn.fetch(DEV03_RUNS_SQL, first, last):
        o = r["output"]
        o = json.loads(o) if isinstance(o, str) else (o or {})
        out.append({"period_start": r["period_start"], "period_end": r["period_end"],
                    "cis_deploys": o.get("cis_deploys")})
    return out


async def collect_dev01(conn, first: date, last: date) -> dict:
    start, end = periods.range_bounds(first, last)
    return await src.run_parts({"starts": lambda: worker_starts(conn, start, end),
                                "weekly": lambda: dev03_outputs(conn, first, last),
                                "versions": lambda: schema_versions(conn)})


def month_deploys(facts: dict, first: date, last: date) -> dict:
    """The month's CIS deploys from the DEV-03 outputs and the worker rows still kept; which days are covered."""
    start, end = periods.range_bounds(first, last)
    live = deploys(facts.get("starts"), start, end)
    weekly = facts.get("weekly")
    weekly = [] if weekly is None or failed(weekly) else weekly
    lists = [w["cis_deploys"] for w in weekly if isinstance(w.get("cis_deploys"), list)]
    merged = [d for d in merge_deploys(*lists, live) if _in(d.get("at"), start, end)]
    covered: set[date] = set()
    for w in weekly:
        if isinstance(w.get("cis_deploys"), list):
            d = max(periods.parse_day(w["period_start"]), first)
            while d <= min(periods.parse_day(w["period_end"]), last):
                covered.add(d)
                d += timedelta(days=1)
    starts = facts.get("starts")
    kept = [] if starts is None or failed(starts) else [r for r in starts if not r.get("baseline")]
    if kept:
        oldest = min(r["started_at"] for r in kept).astimezone(periods.ICT).date()
        d = max(oldest, first)
        while d <= last:
            covered.add(d)
            d += timedelta(days=1)
    days = (last - first).days + 1
    return {"deploys": merged, "days": days, "covered_days": len(covered & {first + timedelta(days=i)
                                                                              for i in range(days)}),
            "weekly_runs": len(weekly), "live_rows": None if live is None else len(live)}


def _in(at: Any, start: datetime, end: datetime) -> bool:
    try:
        t = datetime.fromisoformat(str(at))
    except ValueError:
        return False
    return start <= t < end


def dev01_kpis(md: dict) -> list[dict]:
    dl, weeks = md["deploys"], md["days"] / DAYS_PER_WEEK
    known = md["covered_days"] > 0
    rollbacks = sum(1 for d in dl if d.get("rollback"))
    cfr = ratio(rollbacks, len(dl)) if dl else None
    gap = "" if md["covered_days"] == md["days"] else (f"; {md['days'] - md['covered_days']} of {md['days']} days not "
                                                       "covered (no DEV-03 run and worker rows already pruned)")
    return [
        _kpi_line("K-DORA-DF", round(len(dl) / weeks, 2) if known else None, "info" if known else "no_data",
                  f"CIS only: {len(dl)} deploy(s) to Dev in the month{gap}"),
        _kpi_line("K-DORA-LT", None, NOT_WIRED, f"{NOT_AVAILABLE.lower()}: GitHub PR and deploy run times"),
        _kpi_line("K-DORA-CFR", cfr, "no_data" if cfr is None else "ok" if cfr < CFR_FLAG else "amber",
                  f"rollbacks only ({rollbacks} of {len(dl)} CIS deploys); fix PRs, failed post-deploy verifies and "
                  "incidents are not read by the worker"),
        _kpi_line("K-DORA-MTTR", None, NOT_WIRED, f"{NOT_AVAILABLE.lower()}: OPS-04 postmortems"),
        _kpi_line("K-CI-MAINFAIL", None, NOT_WIRED, f"{NOT_AVAILABLE.lower()}: GitHub Actions runs"),
    ]


def render_dev01(reg: Registry, report: Report, first: date, last: date, facts: dict,
                 generated_at: datetime) -> Rendered:
    md = month_deploys(facts, first, last)
    ch = schema_changes(facts.get("versions"), first, last)
    kpis = dev01_kpis(md)
    headline = (f"Delivery {first.isoformat()} to {last.isoformat()}: {len(md['deploys'])} CIS deploy(s) "
                f"({md['covered_days']} of {md['days']} days covered), "
                + (f"{len(ch['applied'])} migration(s) registered" if "error" not in ch else "migrations not read"))
    body = ["## CIS deploys to Dev", "",
            f"From {md['weekly_runs']} DEV-03 run(s) of the month and the worker rows still kept (7 days); "
            f"{md['covered_days']} of {md['days']} days covered.", ""]
    body += _deploys_md(md["deploys"], "not read")
    body += ["", "## Migrations registered per owner", ""] + _schema_md(ch)
    body += ["", f"## {NOT_AVAILABLE}", ""]
    body += table(["Section", "KPI", "Where it lives today"],
                  [[s, kid or "—", where] for s, kid, where in DEV01_UNAVAILABLE])
    body += ["", "## Improvement actions", "", "- To write before approval (tech lead), with the GitHub numbers.", ""]
    return _render(reg, report, first, last, generated_at, f"Delivery and DORA {first.isoformat()} to "
                   f"{last.isoformat()}", headline, kpis, body, ICT_NOTE, cis_deploys=md["deploys"],
                   coverage={k: md[k] for k in ("days", "covered_days", "weekly_runs", "live_rows")}, schema=ch,
                   not_available=[s for s, _k, _w in DEV01_UNAVAILABLE], sources_unreadable=_unreadable(facts))


# ═══ OPS-15 session-close state snapshot ═══════════════════════════════════════════════════════════════════════

# The checks of the snapshot (catalog sections); K-SNAP-COVERAGE = captured / all.
CHECKS = ("ecs_services", "app_health", "worker_parity", "migrations", "cis_jobs", "open_waves",
          "other_job_tables", "lambda_versions", "vercel_commits", "open_prs")
OPS15_UNAVAILABLE = {
    "other_job_tables": "booking.job, crm.job, tripplanner.job: not granted to the CIS role",
    "lambda_versions": "TripPlanner Lambda versions / aliases: no lambda:GetFunction on the CIS role",
    "vercel_commits": "Vercel production commits: no Vercel token on the worker",
    "open_prs": "open PRs and repo heads: GitHub, no token on the worker",
}


def ecs_service_names(env: str) -> tuple[str, list[str]]:
    """(cluster, services) of the shared cluster (root CLAUDE.md RESOURCES; AA-CIS-Infra modules/ecs)."""
    return f"aa-cis-{env}-cluster", [f"aa-cis-{env}-api", f"aa-cis-{env}-worker", f"aa-booking-{env}-api",
                                     f"aa-crm-{env}-api", f"aa-crm-{env}-web"]


def _image_tag(image: Any) -> dict:
    """ECR image -> {repository, tag}: the repository name without the registry host (no account ID kept)."""
    text = str(image or "")
    name = text.rsplit("/", 1)[-1]
    repo, _, tag = name.partition(":")
    if "@" in repo:
        repo, _, digest = repo.partition("@")
        return {"repository": repo, "tag": None, "digest": digest[:19]}
    return {"repository": repo, "tag": tag or None}


def describe_ecs(client: Any = None, env: Optional[str] = None) -> dict:
    """Sync (run it in a thread): one DescribeServices call, one DescribeTaskDefinition per task definition."""
    from api.services.ops_overview import cloudwatch

    env = env or cloudwatch.environment()
    cluster, names = ecs_service_names(env)
    client = client or cloudwatch.aws_client("ecs")
    resp = client.describe_services(cluster=cluster, services=names)
    images: dict[str, list[dict]] = {}
    out = []
    for s in resp.get("services") or []:
        td = s.get("taskDefinition") or ""
        if td and td not in images:
            d = client.describe_task_definition(taskDefinition=td).get("taskDefinition") or {}
            images[td] = [{"container": c.get("name"), **_image_tag(c.get("image"))}
                          for c in d.get("containerDefinitions") or []]
        primary = next((d for d in s.get("deployments") or [] if d.get("status") == "PRIMARY"), {})
        out.append({"service": s.get("serviceName"), "status": s.get("status"),
                    "task_definition": td.rsplit("/", 1)[-1], "desired": s.get("desiredCount"),
                    "running": s.get("runningCount"), "pending": s.get("pendingCount"),
                    "rollout": primary.get("rolloutState"), "deployments": len(s.get("deployments") or []),
                    "containers": images.get(td, [])})
    missing = [f.get("arn", "").rsplit("/", 1)[-1] for f in resp.get("failures") or []]
    return {"cluster": cluster, "services": out, "missing": missing}


async def ecs_state() -> dict:
    from api.services.ops_overview import cloudwatch

    try:
        return await asyncio.to_thread(describe_ecs)
    except Exception as e:  # noqa: BLE001 — AWS / network: the snapshot names it
        return {"error": "not permitted (ecs:DescribeServices)" if cloudwatch.is_denied(e) else
                f"ECS did not answer ({type(e).__name__})"}


async def jobs_active(conn) -> list[dict]:
    return [{**dict(r), "oldest": _iso(r["oldest"])} for r in await conn.fetch(JOBS_ACTIVE_SQL)]


async def open_waves(conn) -> list[dict]:
    return [{**dict(r), "created_at": _iso(r["created_at"])} for r in await conn.fetch(OPEN_WAVES_SQL)]


async def collect_ops15(conn) -> dict:
    from services.reports import digest

    return await src.run_parts({"ecs": ecs_state, "apps": lambda: digest.load_apps(conn),
                                "versions": lambda: schema_versions(conn), "jobs": lambda: jobs_active(conn),
                                "waves": lambda: open_waves(conn)})


def latest_per_owner(rows: Any) -> Optional[dict]:
    if rows is None or failed(rows):
        return None
    from services.reports.reliability import _vkey, owner_of

    out: dict[str, dict] = {}
    for r in rows:
        o = owner_of(r["version"])
        if o not in out or _vkey(r["version"]) > _vkey(out[o]["version"]):
            out[o] = {"version": r["version"], "applied_at": _iso(r["applied_at"])}
    return out


def snapshot(facts: dict, generated_at: datetime) -> dict:
    """The machine-readable state (state-snapshot.json) and which checks were captured."""
    ecs, apps = facts.get("ecs"), facts.get("apps")
    ok = lambda part: part is not None and not failed(part)  # noqa: E731
    state: dict[str, Any] = {"generated_at": generated_at.isoformat()}
    captured: dict[str, Any] = {}
    if ok(ecs):
        state["ecs"] = ecs
        captured["ecs_services"] = not ecs.get("missing")
    if ok(apps):
        state["apps"] = [{k: a.get(k) for k in ("id", "status", "git_sha", "reason", "schema_version")}
                         for a in apps.get("items") or []]
        state["cis_workers"] = apps.get("workers")
        captured["app_health"] = True
        captured["worker_parity"] = (apps.get("workers") or {}).get("same_commit_as_api") is not None
    latest = latest_per_owner(facts.get("versions"))
    if latest is not None:
        state["migrations"] = latest
        captured["migrations"] = True
    if ok(facts.get("jobs")):
        state["cis_jobs"] = facts["jobs"]
        captured["cis_jobs"] = True
    if ok(facts.get("waves")):
        state["open_waves"] = facts["waves"]
        captured["open_waves"] = True
    done = [c for c in CHECKS if captured.get(c)]
    return {"state": state, "captured": done, "missing": [c for c in CHECKS if c not in done]}


def render_ops15(reg: Registry, report: Report, first: date, last: date, facts: dict,
                 generated_at: datetime) -> Rendered:
    snap = snapshot(facts, generated_at)
    st = snap["state"]
    cover = ratio(len(snap["captured"]), len(CHECKS))
    workers = st.get("cis_workers") or {}
    kpis = [
        _kpi_line("K-SNAP-COVERAGE", cover, "ok" if cover == 1 else "amber",
                  f"{len(snap['captured'])} of {len(CHECKS)} checks captured; not captured: "
                  + (", ".join(snap["missing"]) or "none")),
        _kpi_line("K-SNAP-DRIFT", None, NOT_WIRED,
                  "needs each repo's main head (GitHub); CIS api / worker parity: "
                  + {True: "same commit", False: "DIFFERENT commits", None: "not compared"}[
                      workers.get("same_commit_as_api")]),
    ]
    headline = (f"State at {generated_at.strftime('%Y-%m-%d %H:%M UTC')}: {len(snap['captured'])} of {len(CHECKS)} "
                f"checks captured; CIS jobs active {sum(j['n'] for j in st.get('cis_jobs') or [])}, open waves "
                f"{len(st.get('open_waves') or [])}")
    body = ["The state at generation (not the period); the output JSON is the machine-readable "
            "state-snapshot.json for the session close.", "", "## ECS services", ""]
    ecs = facts.get("ecs")
    if ecs is None or failed(ecs):
        body += [f"- Not readable: {failed(ecs) or 'not read'}"]
    else:
        body += table(["Service", "Task definition", "Image tag", "Desired / running", "Rollout"],
                      [[s["service"], s["task_definition"],
                        ", ".join(str(c.get("tag") or c.get("digest") or "—") for c in s["containers"]) or "—",
                        f"{s['desired']} / {s['running']}", s["rollout"] or "—"] for s in ecs["services"]])
        if ecs.get("missing"):
            body.append(f"- Not found in {ecs['cluster']}: {', '.join(ecs['missing'])}")
    body += ["", "## Health", ""]
    if "apps" in st:
        body += table(["App", "Status", "Commit", "Why"],
                      [[a["id"], a["status"], _short(a.get("git_sha")) if a.get("git_sha") else "—",
                        a.get("reason") or ""] for a in st["apps"]])
        body.append(f"- CIS workers alive {workers.get('alive')}, commits {workers.get('commits')}, same commit as "
                    f"the API: {workers.get('same_commit_as_api')}.")
    else:
        body += [f"- Not readable: {failed(facts.get('apps')) or 'not read'}"]
    body += ["", "## Newest migration per owner", ""]
    body += table(["Owner", "Version", "Applied (UTC)"], [[o, v["version"], str(v["applied_at"])[:16]]
                                                          for o, v in sorted(st["migrations"].items())]) \
        if "migrations" in st else [f"- Not readable: {failed(facts.get('versions')) or 'not read'}"]
    body += ["", "## CIS jobs queued or running", ""]
    if "cis_jobs" in st:
        body += table(["Kind", "Status", "Jobs", "Oldest (UTC)"],
                      [[j["kind"], j["status"], j["n"], str(j["oldest"])[:16]] for j in st["cis_jobs"]]) \
            if st["cis_jobs"] else ["- None: shared.job is idle."]
    else:
        body += [f"- Not readable: {failed(facts.get('jobs')) or 'not read'}"]
    body += ["", "## Open rerun waves", ""]
    if "open_waves" in st:
        body += [f"- {w['name']} ({w['country']}, {w['tours']} tours, since {str(w['created_at'])[:10]})"
                 for w in st["open_waves"]] or ["- None."]
    else:
        body += [f"- Not readable: {failed(facts.get('waves')) or 'not read'}"]
    body += ["", f"## {NOT_AVAILABLE}", ""] + [f"- {v}" for v in OPS15_UNAVAILABLE.values()] + [""]
    return _render(reg, report, first, last, generated_at, f"State snapshot "
                   f"{generated_at.strftime('%Y-%m-%d %H:%M UTC')}", headline, kpis, body,
                   "Times are UTC; the snapshot is the state at generation.", snapshot=snap,
                   sources_unreadable=_unreadable(facts))


# ── dispatch (generate.RENDERERS) ────────────────────────────────────────────────────────────────────────────────


async def render(conn, reg: Registry, report: Report, first: date, last: date, now: datetime) -> Rendered:
    if report.id == "DEV-03":
        return render_dev03(reg, report, first, last, await collect_dev03(conn, first, last), now)
    if report.id == "DEV-01":
        return render_dev01(reg, report, first, last, await collect_dev01(conn, first, last), now)
    if report.id == "OPS-15":
        return render_ops15(reg, report, first, last, await collect_ops15(conn), now)
    raise LookupError(f"delivery.render has no report {report.id}")
