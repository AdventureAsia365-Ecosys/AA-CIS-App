"""AA-1140 — the one verdict at the top of the Ecosystem status page and its "Needs attention" list. Pure: it reads the
sections ``build_status`` assembled and decides nothing from a source that did not answer (a missing source is
listed in ``unknown``, never counted as healthy: the verdict is ``ok`` only when every core source answered).

Severity (S285 advisory, mockup v2): SEV1 = a runtime is down (no running task, a failing health endpoint, no live
worker); SEV2 = a critical limit is crossed, a page-severity alarm fires, a rollout failed, a nightly probe failed;
SEV3 = a warn limit, a ticket alarm, an ops check bell, a guard that did not run, a red main branch. Each row says what,
since when, how often, and links the page or runbook that resolves it. The limits are the page's settings
(``shared/ops_status_thresholds.py``); the sections already coloured each value (level ok / warn / crit).

Language (S289 live check): every item carries ``msg`` (and ``detail_msg`` when its detail is built from numbers), a
key with the structured fields (app, value, limit, unit, ...) the page words in English or Vietnamese; ``title`` and
``detail`` stay as the English fallback. A detail that is outside free text (an alarm description, an ECS rollout
reason, an ops check message) has ``detail_source`` true: the page shows it as written, labelled, never translated.
That text reaches here already without tracker IDs (``sections.event_rows``, :mod:`api.services.issue_refs`).
"""
from __future__ import annotations

from typing import Any, Optional

from api.services.issue_refs import strip_issue_refs

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
          status: str = "open", msg: Optional[dict] = None, detail_msg: Optional[dict] = None,
          detail_source: bool = False) -> dict:
    # the last net: no tracker ID in a title or detail, whatever source it came from
    title, detail = strip_issue_refs(title), strip_issue_refs(detail)
    return {"id": iid, "severity": sev, "app": app, "title": title, "detail": detail, "since": since, "count": count,
            "status": status, "source": source, "msg": msg or {"key": "other"}, "detail_msg": detail_msg,
            "detail_source": bool(detail_source and detail),
            "links": [{"label": label, "href": href} for label, href in (links or [])]}


def _ok(section: Optional[dict]) -> bool:
    return isinstance(section, dict) and section.get("status") == "ok"


def _fmt_ms(v: Optional[float]) -> str:
    return "–" if v is None else (f"{v / 1000:.2f} s" if v >= 1000 else f"{v:.0f} ms")


def health_items(app: str, label: str, h: dict) -> list[dict]:
    """The health check row of one app (SEV1 down, SEV2 not ready); the check's reason is outside text. Pure."""
    if h.get("state") == "down":
        return [_item(f"health-{app}", "sev1", app, f"{label} health check fails", h.get("reason") or "",
                      since=h.get("sampled_at"), source="health check", msg={"key": "health_down", "app": app},
                      detail_source=True,
                      links=[("App health", PAGES["apps"]), ("Incident response", RUNBOOK_INCIDENT)])]
    if h.get("state") == "degraded":
        return [_item(f"health-{app}", "sev2", app, f"{label} is not ready", h.get("reason") or "",
                      since=h.get("sampled_at"), source="health check", msg={"key": "health_degraded", "app": app},
                      detail_source=True, links=[("App health", PAGES["apps"])])]
    return []


def _of_requests(part: Any, whole: Any) -> dict:
    return {"key": "of_requests", "n": part, "total": whole}


def traffic_items(app: str, label: str, t: dict, th: dict) -> list[dict]:
    """5xx, p95 and 4xx rows of one app's traffic over the range. Pure."""
    out = []
    er, p = th["error_rate_pct"], th["p95_ms"]
    if t.get("errors_level") in LEVEL_SEV:
        lv = t["errors_level"]
        lim = er["crit"] if lv == "crit" else er["warn"]
        out.append(_item(f"errors-{app}", LEVEL_SEV[lv], app,
                         f"{label}: {t['error_rate_pct']:g} % server errors, limit {lim:g} %",
                         f"{t.get('errors')} of {t.get('requests')} requests in the range", source="CloudWatch",
                         msg={"key": "errors", "app": app, "value": t["error_rate_pct"], "limit": lim, "unit": "%"},
                         detail_msg=_of_requests(t.get("errors"), t.get("requests")),
                         links=[("App health", PAGES["apps"]), ("Rollback runbooks", RUNBOOK_ROLLBACK)]))
    if t.get("p95_level") in LEVEL_SEV:
        lv = t["p95_level"]
        if app == "tripplanner":
            lim, sev = th["lambda_p95_limit_ms"]["warn"], "sev3"
        else:
            lim, sev = (p["crit"] if lv == "crit" else p["warn"]), LEVEL_SEV[lv]
        out.append(_item(f"p95-{app}", sev, app, f"{label} is slow: p95 {_fmt_ms(t.get('p95_ms'))}, limit "
                         f"{_fmt_ms(lim)}", "", source="CloudWatch",
                         msg={"key": "p95", "app": app, "value": t.get("p95_ms"), "limit": lim, "unit": "ms"},
                         links=[("App health", PAGES["apps"])]))
    if t.get("client_errors_level") == "warn":
        lim = th["client_error_warn_pct"]["warn"]
        out.append(_item(f"4xx-{app}", "sev3", app,
                         f"{label}: {t['client_error_rate_pct']:g} % client errors, limit {lim:g} %",
                         f"{t.get('client_errors')} of {t.get('requests')} requests", source="CloudWatch",
                         msg={"key": "client_errors", "app": app, "value": t["client_error_rate_pct"], "limit": lim,
                              "unit": "%"},
                         detail_msg=_of_requests(t.get("client_errors"), t.get("requests")),
                         links=[("App health", PAGES["apps"])]))
    return out


def app_items(apps: list[dict], th: dict) -> list[dict]:
    out = []
    for a in apps:
        out += health_items(a["app"], a["label"], a.get("health") or {})
        t = a.get("traffic") or {}
        if t.get("status") == "ok":
            out += traffic_items(a["app"], a["label"], t, th)
    return out


def runtime_items(rows: list[dict]) -> list[dict]:
    out = []
    sev_of = {"down": "sev1", "degraded": "sev2"}
    for r in rows:
        if r.get("kind") != "ecs_service" or r.get("state") not in sev_of:
            continue
        state = r["state"]
        out.append(_item(f"svc-{r['subject']}", sev_of[state], r.get("app") or "shared",
                         f"{r['subject']}: {r.get('reason') or state}", "", since=r.get("sampled_at"), source="ECS",
                         msg={"key": "service", "subject": r["subject"], "state": state,
                              "code": r.get("reason_code"), "running": r.get("running"),
                              "desired": r.get("desired")},
                         links=[("App health", PAGES["apps"]), ("Rollback runbooks", RUNBOOK_ROLLBACK)]))
    return out


def alarm_items(events: list[dict]) -> list[dict]:
    """Open alarm and failed rollout events; their detail (the alarm description, the ECS reason) is outside text,
    already without tracker IDs. Pure."""
    out = []
    for e in events:
        if not e.get("open"):
            continue
        detail = e.get("detail") or ""
        if e.get("kind") == "alarm":
            out.append(_item(f"alarm-{e['id']}", e["severity"] if e["severity"] in _SEV_RANK else "sev3",
                             e.get("app") or "shared", e["text"], detail, since=e["t"], source="CloudWatch alarm",
                             msg=e.get("msg"), detail_source=True,
                             links=[("Alerts catalogue", PAGES["alerts"]), ("Incident response", RUNBOOK_INCIDENT)]))
        elif e.get("kind") == "rollout_failed":
            out.append(_item(f"rollout-{e['id']}", "sev2", e.get("app") or "shared", e["text"], detail,
                             since=e["t"], source="ECS", msg=e.get("msg"), detail_source=True,
                             links=[("Rollback runbooks", RUNBOOK_ROLLBACK)]))
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
                              msg=e.get("msg"), detail_source=True,
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
                             "Queued jobs cannot start.", source="job runner", links=[("Jobs", PAGES["jobs"])],
                             msg={"key": "queue_no_worker", "name": qu["name"]},
                             detail_msg={"key": "queue_no_worker_detail"}))
        elif qu.get("level") in LEVEL_SEV:
            lim = q["crit"] if qu["level"] == "crit" else q["warn"]
            out.append(_item(f"queue-{qu['id']}", LEVEL_SEV[qu["level"]], qu["app"],
                             f"{qu['name']}: oldest job waiting {qu.get('oldest_wait_min'):g} min, limit {lim:g} min",
                             "", source="job runner", links=[("Jobs", PAGES["jobs"])],
                             msg={"key": "queue_wait", "name": qu["name"], "value": qu.get("oldest_wait_min"),
                                  "limit": lim, "unit": "min"}))
    return out


def _resource_msg(t: dict, lv: str, lim: float) -> dict:
    msg = {"key": "resource", "tile": t["id"], "value": t.get("latest"), "unit": t.get("unit"), "limit": lim,
           "dir": t.get("dir"), "level": lv}
    if t.get("basis") == "pct":   # free storage against a share of the allocated storage (CIS 477)
        msg.update({"value_pct": t.get("latest_pct"), "limit_pct": t.get("crit_pct" if lv == "crit" else "warn_pct"),
                    "allocated_gib": t.get("allocated_gib")})
    return msg


def _resource_title(t: dict, lv: str, lim: float) -> str:
    word = "at or below" if t.get("dir") == "below" else "at or above"
    which = "critical" if lv == "crit" else "warn"
    if t.get("basis") == "pct":
        share = t.get("crit_pct" if lv == "crit" else "warn_pct")
        return (f"{t['label']} {t.get('latest')} GiB ({t.get('latest_pct')} % of {t.get('allocated_gib'):g} GiB), "
                f"{word} the {which} limit {share:g} % ({lim:g} GiB)")
    return f"{t['label']} {t.get('latest')} {t.get('unit')}, {word} the {which} limit {lim:g}"


def resource_items(tiles: list[dict]) -> list[dict]:
    out = []
    for t in tiles:
        lv = t.get("level")
        if lv not in LEVEL_SEV:
            continue
        lim = t.get("crit") if lv == "crit" else t.get("warn")
        if lim is None:   # a capacity tile (ECS tasks below the desired count): no limit, the note says why
            out.append(_item(f"res-{t['id']}", LEVEL_SEV[lv], "shared", f"{t['label']}: {t.get('note') or ''}",
                             "", source="ECS", links=[("App health", PAGES["apps"])],
                             msg={"key": "ecs_tasks", "running": t.get("latest"), "desired": t.get("capacity")}))
            continue
        out.append(_item(f"res-{t['id']}", LEVEL_SEV[lv], "shared", _resource_title(t, lv, lim),
                         t.get("note") or "", source="database", links=[("Database operations", RUNBOOK_DB)],
                         msg=_resource_msg(t, lv, lim)))
    return out


def ai_items(apps: list[dict]) -> list[dict]:
    out = []
    for a in apps:
        spend = {"key": "ai_spend", "spent": a.get("spent_usd"), "cap": a.get("cap_usd")}
        if a.get("level") == "crit":
            out.append(_item(f"ai-{a['app']}", "sev2", a["app"], f"AI spend of {a['app']} reached its daily cap",
                             f"{a['spent_usd']} of {a['cap_usd']} USD today (UTC)", source="AI gateway",
                             msg={"key": "ai_cap", "app": a["app"]}, detail_msg=spend,
                             links=[("AI gateway", PAGES["ai"])]))
        elif a.get("level") == "warn":
            out.append(_item(f"ai-{a['app']}", "sev3", a["app"], f"AI spend of {a['app']} at {a['used_pct']:g} % of "
                             f"its daily cap", f"{a['spent_usd']} of {a['cap_usd']} USD today (UTC)",
                             source="AI gateway", msg={"key": "ai_near", "app": a["app"], "value": a["used_pct"]},
                             detail_msg=spend, links=[("AI gateway", PAGES["ai"])]))
    return out


def guard_items(guards: list[dict]) -> list[dict]:
    out = []
    for g in guards:
        if g.get("state") == "failed":
            out.append(_item(f"guard-{g['key']}", "sev2", "shared", f"Nightly {g['label']} failed",
                             f"Value {g.get('value')} on {g.get('day')}", source="nightly probe",
                             msg={"key": "guard_failed", "name": g["label"]},
                             detail_msg={"key": "guard_value", "value": g.get("value"), "day": g.get("day")},
                             links=[("Reports", PAGES["reports"])]))
        elif g.get("state") == "missing":
            age = g.get("age_h")
            out.append(_item(f"guard-{g['key']}", "sev3", "shared", f"Nightly {g['label']} did not run",
                             g.get("detail") or (f"Last result {age} h ago" if age else ""),
                             source="nightly probe", msg={"key": "guard_missing", "name": g["label"]},
                             detail_msg=None if g.get("detail") or not age else {"key": "guard_age", "hours": age},
                             links=[("Reports", PAGES["reports"])]))
    return out


def delivery_items(data: dict) -> list[dict]:
    out = guard_items(data.get("guards") or [])
    for r in data.get("repos") or []:
        if r.get("main") == "red":
            hours = r.get("red_hours")
            out.append(_item(f"main-{r['id']}", "sev3", "shared", f"{r['label']}: main branch is red",
                             f"Red for {hours} h" if hours else "", source="CI",
                             msg={"key": "main_red", "name": r["label"]},
                             detail_msg={"key": "red_for", "hours": hours} if hours else None,
                             links=[("Delivery", PAGES["delivery"])]))
    return out


def forecast_items(rows: list[dict]) -> list[dict]:
    """AA-1141: a capacity forecast at or under its critical days left is a SEV3 item (warn stays on the card). Pure."""
    out = []
    for r in rows:
        if r.get("level") != "crit":
            continue
        days = r.get("days_left")
        if r["id"] == "rds_free":
            title = f"Database storage fills in about {days:g} days at the current pace"
            link = ("Database operations", RUNBOOK_DB)
        else:
            title = f"AI connection {r.get('label')} reaches its monthly cap in about {days:g} days"
            link = ("AI gateway budgets", PAGES["ai"] + "?tab=budgets")
        out.append(_item(f"forecast-{r['id']}", "sev3", "shared", title, "", source="capacity forecast",
                         msg={"key": "forecast", "tile": r["id"], "label": r.get("label"), "days": days},
                         links=[link, ("Settings", PAGES["settings"])]))
    return out


def anomaly_items(rows: list[dict]) -> list[dict]:
    """AA-1141: one SEV3 item per app x provider x route whose AI spend of an hour is abnormal (the newest hour of the
    group; the banner lists every flagged hour). Pure."""
    seen: dict[str, dict] = {}
    for r in rows:
        iid = f"anomaly-{r['app']}-{r['route']}"
        if iid in seen and seen[iid]["since"] >= r["hour"]:
            continue
        times = f"{r['times_median']:g} x its median hour" if r.get("times_median") else "above the floor"
        seen[iid] = _item(iid, "sev3", r["app"] if r["app"] in ("cis", "tripplanner", "booking", "crm") else "shared",
                          f"AI spend {r['usd']:g} USD in one hour on {r['route']} ({times})",
                          f"Median hour {r['median_usd_h']:g} USD, limit {r['threshold_usd']:g} USD",
                          since=r["hour"], source="AI gateway",
                          msg={"key": "anomaly", "app": r["app"], "route": r["route"], "value": r["usd"],
                               "median": r["median_usd_h"], "times": r.get("times_median"),
                               "limit": r["threshold_usd"]},
                          links=[("AI gateway", PAGES["ai"]), ("Spend", PAGES["spend"])])
    return list(seen.values())


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
# AA-1141: advisory sections (not core: a missing one is not listed in ``unknown``)
_EXTRA_ITEMS = (
    ("forecast", lambda d: forecast_items(d.get("rows") or [])),
    ("anomaly", lambda d: anomaly_items(d.get("rows") or [])),
    ("slo", lambda d: _p2_items("slo", d)),   # AA-1142: error budget burn, external checks
    ("external", lambda d: _p2_items("external", d)),
)


def _p2_items(name: str, data: dict) -> list[dict]:
    """AA-1142: the burn rate and external check rows, built by their own modules (imported here, not at the top, so
    this pure module does not load the CloudWatch reader)."""
    from api.services.ops_status import external, slo

    return (slo if name == "slo" else external).verdict_items(data)


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
    for name, build in _EXTRA_ITEMS:
        sec = sections.get(name)
        if _ok(sec):
            items += build(sec["data"])
    return items, [c for c in CORE if c in unknown]


def overall(open_count: int, unknown: list[str]) -> str:
    """attention when something measured is open; ok only with every core source; else partial / unknown. Pure."""
    if open_count:
        return "attention"
    if not unknown:
        return "ok"
    return "unknown" if set(MAIN) <= set(unknown) else "partial"


def decide(sections: dict[str, Any], th: dict, events: list[dict], acks: Optional[dict[str, dict]] = None,
           names: Optional[dict] = None) -> dict:
    """{status: ok | attention | partial | unknown, open, attention[], resolved[], chips, unknown[]}.

    A signal that was not measured is never counted as healthy: ``ok`` only when every core source answered
    (``CORE``: the CloudWatch signals, the app tiles, a fresh runtimes sample, jobs, resources, AI). With nothing
    open, a missing core source gives ``partial`` (the page shows a neutral headline naming it), both ``MAIN``
    sources missing ``unknown``. Open items always win (``attention``): what was measured and is broken is shown.

    AA-1141: ``acks`` {item_id: row in force} (``acks.read_in_force``) marks an item acked / snoozed / muted with its
    ``ack`` block; such an item stays in the list but leaves the open count (an all-acked page reads "ok" when every
    core source answered, the acked items still listed)."""
    from api.services.ops_status import acks as ack_mod

    items, unknown = _collect(sections, th, events)
    items = ack_mod.apply(items, acks or {}, names)
    # open items first (by severity), then the ones set aside (acked, snoozed, muted, a read bell)
    items.sort(key=lambda i: (i["status"] != "open", _SEV_RANK.get(i["severity"], 9), i.get("since") or ""))
    open_items = [i for i in items if i["status"] == "open"]
    resolved = [{"id": e["id"], "app": e.get("app"), "text": e["text"], "msg": e.get("msg"), "t": e["t"],
                 "t2": e.get("t2")}
                for e in events if e.get("kind") in ("alarm", "service_down", "rollout_failed") and e.get("t2")]
    jobs = sections.get("jobs")
    gate = (jobs or {}).get("data", {}).get("deploy_gate", {}) if _ok(jobs) else {}
    firing = sum(1 for e in events if e.get("kind") == "alarm" and e.get("open"))
    return {"status": overall(len(open_items), unknown), "open": len(open_items), "attention": items,
            "resolved": resolved[:20], "unknown": unknown,
            "set_aside": len(items) - len(open_items),
            "chips": {"alarms_firing": firing, "open_alerts": len(open_items),
                      "deploy_gate": None if not gate else ("open" if gate.get("safe_to_deploy") else "closed"),
                      "deploy_gate_blocked_by": gate.get("blocked_by") or []}}
