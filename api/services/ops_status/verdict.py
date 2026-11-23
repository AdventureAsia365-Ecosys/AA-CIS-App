"""AA-1140 — the one verdict at the top of the Ecosystem status page and its "Needs attention" list. Pure: it reads the
sections ``build_status`` assembled and decides nothing from a source that did not answer (a missing source is
listed in ``unknown``, never counted as healthy: the verdict is ``ok`` only when every core source answered).

Severity (S285 advisory, mockup v2): SEV1 = a runtime is down (no running task, a failing health endpoint, no live
worker); SEV2 = a critical limit is crossed, a page-severity alarm fires, a rollout failed, a nightly probe failed;
SEV3 = a warn limit, a ticket alarm, an ops check bell, a guard that did not run, a red main branch. Each row says what,
since when, how often, and links the page or runbook that resolves it. The limits are the page's settings
(``shared/ops_status_thresholds.py``); the sections already coloured each value (level ok / warn / crit).
"""
from __future__ import annotations

from typing import Any, Optional

RUNBOOK_INCIDENT = "/admin/docs?path=docs/ops/incident-response.md"
RUNBOOK_ROLLBACK = "/admin/docs?path=docs/ops/runbooks/rollback-ecs.md"
RUNBOOK_DB = "/admin/docs?path=docs/ops/database-operations.md"
PAGES = {"apps": "/admin/platform/apps", "jobs": "/admin/jobs", "alerts": "/admin/platform/alerts",
         "ai": "/admin/ai-gateway", "delivery": "/admin/platform/delivery", "spend": "/admin/spend",
         "settings": "/admin/platform/settings?app=cis&area=ops", "reports": "/admin/reports"}
_SEV_RANK = {"sev1": 0, "sev2": 1, "sev3": 2}
# The sources the verdict needs before it may say "All systems normal" (delivery is advisory and not required);
# without both MAIN sources (app traffic from CloudWatch, the sampled services) nothing about health was measured.
CORE = ("signals", "apps", "runtimes", "jobs", "resources", "ai")
MAIN = ("signals", "runtimes")
LEVEL_SEV = {"crit": "sev2", "warn": "sev3"}


def _item(iid: str, sev: str, app: str, title: str, detail: str = "", *, since: Optional[str] = None,
          count: int = 1, links: Optional[list[tuple[str, str]]] = None, source: str = "",
          status: str = "open") -> dict:
    return {"id": iid, "severity": sev, "app": app, "title": title, "detail": detail, "since": since, "count": count,
            "status": status, "source": source,
            "links": [{"label": label, "href": href} for label, href in (links or [])]}


def _ok(section: Optional[dict]) -> bool:
    return isinstance(section, dict) and section.get("status") == "ok"


def _fmt_ms(v: Optional[float]) -> str:
    return "–" if v is None else (f"{v / 1000:.2f} s" if v >= 1000 else f"{v:.0f} ms")


def app_items(apps: list[dict], th: dict) -> list[dict]:
    out = []
    for a in apps:
        app, label = a["app"], a["label"]
        h = a.get("health") or {}
        if h.get("state") == "down":
            out.append(_item(f"health-{app}", "sev1", app, f"{label} health check fails",
                             h.get("reason") or "", since=h.get("sampled_at"), source="health check",
                             links=[("App health", PAGES["apps"]), ("Incident response", RUNBOOK_INCIDENT)]))
        elif h.get("state") == "degraded":
            out.append(_item(f"health-{app}", "sev2", app, f"{label} is not ready", h.get("reason") or "",
                             since=h.get("sampled_at"), source="health check", links=[("App health", PAGES["apps"])]))
        t = a.get("traffic") or {}
        if t.get("status") != "ok":
            continue
        er, p = th["error_rate_pct"], th["p95_ms"]
        if t.get("errors_level") in LEVEL_SEV:
            lv = t["errors_level"]
            lim = er["crit"] if lv == "crit" else er["warn"]
            out.append(_item(f"errors-{app}", LEVEL_SEV[lv], app,
                             f"{label}: {t['error_rate_pct']:g} % server errors, limit {lim:g} %",
                             f"{t.get('errors')} of {t.get('requests')} requests in the range", source="CloudWatch",
                             links=[("App health", PAGES["apps"]), ("Rollback runbooks", RUNBOOK_ROLLBACK)]))
        if t.get("p95_level") in LEVEL_SEV:
            lv = t["p95_level"]
            if app == "tripplanner":
                lim, sev = th["lambda_p95_limit_ms"]["warn"], "sev3"
            else:
                lim, sev = (p["crit"] if lv == "crit" else p["warn"]), LEVEL_SEV[lv]
            out.append(_item(f"p95-{app}", sev, app, f"{label} is slow: p95 {_fmt_ms(t.get('p95_ms'))}, limit "
                             f"{_fmt_ms(lim)}", "", source="CloudWatch", links=[("App health", PAGES["apps"])]))
        if t.get("client_errors_level") == "warn":
            out.append(_item(f"4xx-{app}", "sev3", app,
                             f"{label}: {t['client_error_rate_pct']:g} % client errors, limit "
                             f"{th['client_error_warn_pct']['warn']:g} %",
                             f"{t.get('client_errors')} of {t.get('requests')} requests", source="CloudWatch",
                             links=[("App health", PAGES["apps"])]))
    return out


def runtime_items(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        if r.get("kind") != "ecs_service":
            continue
        if r.get("state") == "down":
            out.append(_item(f"svc-{r['subject']}", "sev1", r.get("app") or "shared", f"{r['subject']}: "
                             f"{r.get('reason') or 'down'}", "", since=r.get("sampled_at"), source="ECS",
                             links=[("App health", PAGES["apps"]), ("Rollback runbooks", RUNBOOK_ROLLBACK)]))
        elif r.get("state") == "degraded":
            out.append(_item(f"svc-{r['subject']}", "sev2", r.get("app") or "shared", f"{r['subject']}: "
                             f"{r.get('reason') or 'degraded'}", "", since=r.get("sampled_at"), source="ECS",
                             links=[("App health", PAGES["apps"]), ("Rollback runbooks", RUNBOOK_ROLLBACK)]))
    return out


def alarm_items(events: list[dict]) -> list[dict]:
    out = []
    for e in events:
        if e.get("kind") == "alarm" and e.get("open"):
            out.append(_item(f"alarm-{e['id']}", e["severity"] if e["severity"] in _SEV_RANK else "sev3",
                             e.get("app") or "shared", e["text"], e.get("detail") or "", since=e["t"],
                             source="CloudWatch alarm", links=[("Alerts catalogue", PAGES["alerts"]),
                                                               ("Incident response", RUNBOOK_INCIDENT)]))
        elif e.get("kind") == "rollout_failed" and e.get("open"):
            out.append(_item(f"rollout-{e['id']}", "sev2", e.get("app") or "shared", e["text"], e.get("detail") or "",
                             since=e["t"], source="ECS", links=[("Rollback runbooks", RUNBOOK_ROLLBACK)]))
    return out


def check_items(events: list[dict]) -> list[dict]:
    """One row per ops check with a bell in the range (newest first; repeats counted, read = acknowledged)."""
    by_check: dict[str, dict] = {}
    for e in events:
        if e.get("kind") != "check":
            continue
        key = str(e.get("check") or e["text"])
        if key in by_check:
            by_check[key]["count"] += 1
            if e.get("open"):
                by_check[key]["status"] = "open"
            continue
        by_check[key] = _item(f"check-{key}", "sev3", e.get("app") or "cis", e["text"], e.get("detail") or "",
                              since=e["t"], source="Ops check", status="open" if e.get("open") else "acked",
                              links=[("Alerts catalogue", PAGES["alerts"]), ("Settings", PAGES["settings"])])
    return list(by_check.values())


def queue_items(queues: list[dict], th: dict) -> list[dict]:
    out = []
    q = th["queue_wait_min"]
    for qu in queues:
        if qu.get("status") != "ok":
            continue
        if qu.get("workers") == 0:
            out.append(_item(f"queue-{qu['id']}", "sev1", qu["app"], f"{qu['name']}: no live worker",
                             "Queued jobs cannot start.", source="job runner", links=[("Jobs", PAGES["jobs"])]))
        elif qu.get("level") in LEVEL_SEV:
            lim = q["crit"] if qu["level"] == "crit" else q["warn"]
            out.append(_item(f"queue-{qu['id']}", LEVEL_SEV[qu["level"]], qu["app"],
                             f"{qu['name']}: oldest job waiting {qu.get('oldest_wait_min'):g} min, limit {lim:g} min",
                             "", source="job runner", links=[("Jobs", PAGES["jobs"])]))
    return out


def resource_items(tiles: list[dict]) -> list[dict]:
    out = []
    for t in tiles:
        lv = t.get("level")
        if lv not in LEVEL_SEV:
            continue
        lim = t.get("crit") if lv == "crit" else t.get("warn")
        if lim is None:   # a capacity tile (ECS tasks below the desired count): no limit, the note says why
            out.append(_item(f"res-{t['id']}", LEVEL_SEV[lv], "shared", f"{t['label']}: {t.get('note') or ''}",
                             "", source="ECS", links=[("App health", PAGES["apps"])]))
            continue
        word = "at or below" if t.get("dir") == "below" else "at or above"
        out.append(_item(f"res-{t['id']}", LEVEL_SEV[lv], "shared", f"{t['label']} {t.get('latest')} {t.get('unit')}, "
                         f"{word} the {'critical' if lv == 'crit' else 'warn'} limit {lim:g}",
                         t.get("note") or "", source="database", links=[("Database operations", RUNBOOK_DB)]))
    return out


def ai_items(apps: list[dict]) -> list[dict]:
    out = []
    for a in apps:
        if a.get("level") == "crit":
            out.append(_item(f"ai-{a['app']}", "sev2", a["app"], f"AI spend of {a['app']} reached its daily cap",
                             f"{a['spent_usd']} of {a['cap_usd']} USD today (UTC)", source="AI gateway",
                             links=[("AI gateway", PAGES["ai"])]))
        elif a.get("level") == "warn":
            out.append(_item(f"ai-{a['app']}", "sev3", a["app"], f"AI spend of {a['app']} at {a['used_pct']:g} % of "
                             f"its daily cap", f"{a['spent_usd']} of {a['cap_usd']} USD today (UTC)",
                             source="AI gateway", links=[("AI gateway", PAGES["ai"])]))
    return out


def delivery_items(data: dict) -> list[dict]:
    out = []
    for g in data.get("guards") or []:
        if g.get("state") == "failed":
            out.append(_item(f"guard-{g['key']}", "sev2", "shared", f"Nightly {g['label']} failed",
                             f"Value {g.get('value')} on {g.get('day')}", source="nightly probe",
                             links=[("Reports", PAGES["reports"])]))
        elif g.get("state") == "missing":
            out.append(_item(f"guard-{g['key']}", "sev3", "shared", f"Nightly {g['label']} did not run",
                             g.get("detail") or (f"Last result {g.get('age_h')} h ago" if g.get("age_h") else ""),
                             source="nightly probe", links=[("Reports", PAGES["reports"])]))
    for r in data.get("repos") or []:
        if r.get("main") == "red":
            out.append(_item(f"main-{r['id']}", "sev3", "shared", f"{r['label']}: main branch is red",
                             f"Red for {r.get('red_hours')} h" if r.get("red_hours") else "", source="CI",
                             links=[("Delivery", PAGES["delivery"])]))
    return out


def _runtimes_measured(rt: Optional[dict]) -> bool:
    """The runtimes section counts only with at least one ECS service sampled recently: before the sampler's first
    tick, or while the worker is down, every row reads no_sample and nothing about the services was measured."""
    if not _ok(rt):
        return False
    rows = (rt.get("data") or {}).get("rows") or []
    return any(r.get("kind") == "ecs_service" and r.get("state") not in ("no_sample", "not_measured", "unknown")
               for r in rows)


# (section, rows of its data, item builder); a section that did not answer is listed under ``unknown``.
_SECTION_ITEMS = (
    ("apps", app_items),
    ("jobs", lambda d, th: queue_items(d["queues"], th)),
    ("resources", lambda d, th: resource_items(d["tiles"])),
    ("ai", lambda d, th: ai_items(d["apps"])),
)


def _collect(sections: dict[str, Any], th: dict, events: list[dict]) -> tuple[list[dict], list[str]]:
    """Every attention item of the sections that answered, and the core sources that did not. Pure."""
    items: list[dict] = alarm_items(events) + check_items(events)
    unknown: list[str] = [] if _ok(sections.get("signals")) else ["signals"]
    rt = sections.get("runtimes")
    if _ok(rt):
        items += runtime_items(rt["data"]["rows"])
    for name, build in _SECTION_ITEMS:
        sec = sections.get(name)
        if _ok(sec):
            items += build(sec["data"], th)
        else:
            unknown.append(name)
    if not _runtimes_measured(rt):
        unknown.append("runtimes")
    dl = sections.get("delivery")
    if _ok(dl):
        items += delivery_items(dl["data"])
    return items, [c for c in CORE if c in unknown]


def overall(open_count: int, unknown: list[str]) -> str:
    """attention when something measured is open; ok only with every core source; else partial / unknown. Pure."""
    if open_count:
        return "attention"
    if not unknown:
        return "ok"
    return "unknown" if set(MAIN) <= set(unknown) else "partial"


def decide(sections: dict[str, Any], th: dict, events: list[dict]) -> dict:
    """{status: ok | attention | partial | unknown, open, attention[], resolved[], chips, unknown[]}.

    A signal that was not measured is never counted as healthy: ``ok`` only when every core source answered
    (``CORE``: the CloudWatch signals, the app tiles, a fresh runtimes sample, jobs, resources, AI). With nothing
    open, a missing core source gives ``partial`` (the page shows a neutral headline naming it), both ``MAIN``
    sources missing ``unknown``. Open items always win (``attention``): what was measured and is broken is shown."""
    items, unknown = _collect(sections, th, events)
    items.sort(key=lambda i: (_SEV_RANK.get(i["severity"], 9), i["status"] != "open", i.get("since") or ""))
    open_items = [i for i in items if i["status"] == "open"]
    resolved = [{"id": e["id"], "app": e.get("app"), "text": e["text"], "t": e["t"], "t2": e.get("t2")}
                for e in events if e.get("kind") in ("alarm", "service_down", "rollout_failed") and e.get("t2")]
    jobs = sections.get("jobs")
    gate = (jobs or {}).get("data", {}).get("deploy_gate", {}) if _ok(jobs) else {}
    firing = sum(1 for e in events if e.get("kind") == "alarm" and e.get("open"))
    return {"status": overall(len(open_items), unknown), "open": len(open_items), "attention": items,
            "resolved": resolved[:20], "unknown": unknown,
            "chips": {"alarms_firing": firing, "open_alerts": len(open_items),
                      "deploy_gate": None if not gate else ("open" if gate.get("safe_to_deploy") else "closed"),
                      "deploy_gate_blocked_by": gate.get("blocked_by") or []}}
