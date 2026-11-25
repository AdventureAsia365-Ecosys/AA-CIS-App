"""AA-1141 (P1 feature 1) — "What changed": every change of the range by type, for the chart annotations and the
What changed panel of the Ecosystem status page (S285 review idea 1, Grafana annotations / Datadog Change Tracking).

Five types, each from the table that already records it (no new writer, no new table; free reads):

- ``deploy``    ECS deployments the status sampler recorded (``acp_shared.ops_event`` kind deploy / rollout_failed,
                CIS 475); the link opens the deployed commit of the app's public repo (its page names the PR).
- ``setting``   approved setting values (``shared.setting_value`` status approved / superseded, at ``effective_from``)
                of every app; the link opens Platform › Settings on that key (its history).
- ``migration`` migrations registered in ``shared.schema_versions`` (every owner: CIS NNN, Booking bk-NNN, CRM
                crm-NNN, TripPlanner tp-NNN), at ``applied_at``; the link opens the Data model page.
- ``wave``      rerun waves opened, paused or closed (``shared.rerun_wave``); the link opens Jobs.
- ``route``     route control versions saved (``shared.llm_route_config_version``: save, preset, revert, external);
                the link opens AI gateway › Routing.

The A0 assistant (``api/services/ops_assist/evidence.py``) keeps its own short "what changed" evidence of one item
(ops events and route versions of its changes window); this module is the page's range-wide list. Each source is read
on its own and capped (``ROWS``); a source that fails is listed in ``unavailable``, never hides the others.
``change_rows`` is pure and unit-tested.
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime
from typing import Any, Optional

import asyncpg
import structlog

from api.services.issue_refs import clean, strip_issue_refs
from api.services.ops_status.ranges import TimeRange
from api.services.ops_status.store import _json

logger = structlog.get_logger()

TYPES = ("deploy", "setting", "migration", "wave", "route")
ROWS = 200  # technical-constant: rows of one source read
REPO_BASE = "https://github.com/AdventureAsia365-Ecosys/"
REPO_OF = {"cis": "AA-CIS-App", "booking": "AA-Booking", "crm": "AA-Partner", "tripplanner": "AA-TripPlanner-Web"}
PAGES = {"settings": "/admin/platform/settings", "data_model": "/admin/data-model", "jobs": "/admin/jobs",
         "routing": "/admin/ai-gateway?tab=routing", "delivery": "/admin/platform/delivery"}
_SHA = re.compile(r"^(?:dev-)?([0-9a-f]{7,40})$")

DEPLOYS_SQL = f"""
    SELECT event_id, occurred_at, ended_at, kind, app, subject, title, details
      FROM acp_shared.ops_event
     WHERE kind IN ('deploy', 'rollout_failed') AND occurred_at >= $1 AND occurred_at < $2
     ORDER BY occurred_at DESC
     LIMIT {ROWS}
"""
SETTINGS_SQL = f"""
    SELECT v.id::text AS id, v.key, d.app, d.label, v.version, v.effective_from, v.decided_by, v.proposed_by,
           v.tenant_id IS NOT NULL AS tenant_scoped
      FROM shared.setting_value v
      JOIN shared.setting_definition d ON d.key = v.key
     WHERE v.status IN ('approved', 'superseded') AND v.effective_from >= $1 AND v.effective_from < $2
     ORDER BY v.effective_from DESC
     LIMIT {ROWS}
"""
MIGRATIONS_SQL = f"""
    SELECT version, description, applied_at
      FROM shared.schema_versions
     WHERE applied_at >= $1 AND applied_at < $2
     ORDER BY applied_at DESC
     LIMIT {ROWS}
"""
WAVES_SQL = f"""
    SELECT id::text AS id, name, status, created_by, created_at, paused_at, paused_by, closed_at
      FROM shared.rerun_wave
     WHERE (created_at >= $1 AND created_at < $2) OR (paused_at >= $1 AND paused_at < $2)
        OR (closed_at >= $1 AND closed_at < $2)
     ORDER BY created_at DESC
     LIMIT {ROWS}
"""
ROUTES_SQL = f"""
    SELECT version, created_at, created_by, action, preset, reverted_from
      FROM shared.llm_route_config_version
     WHERE created_at >= $1 AND created_at < $2
     ORDER BY version DESC
     LIMIT {ROWS}
"""
SOURCES = {"deploy": DEPLOYS_SQL, "setting": SETTINGS_SQL, "migration": MIGRATIONS_SQL, "wave": WAVES_SQL,
           "route": ROUTES_SQL}


def _iso(v: Any) -> Optional[str]:
    return v.isoformat() if hasattr(v, "isoformat") else (str(v) if v is not None else None)


def _in(t: Any, rng: TimeRange) -> bool:
    return isinstance(t, datetime) and rng.start <= t < rng.end


def migration_app(version: str) -> str:
    """The owner app of a shared.schema_versions key: bk- Booking, crm- CRM, tp- TripPlanner, a number CIS. Pure."""
    v = str(version or "")
    for prefix, app in (("bk-", "booking"), ("crm-", "crm"), ("tp-", "tripplanner")):
        if v.startswith(prefix):
            return app
    return "cis"


def commit_href(app: Optional[str], image_tag: Optional[str]) -> Optional[str]:
    """The public commit page of a deployed image tag ("dev-<sha>" or a bare sha), None when unknown. Pure."""
    m = _SHA.match(str(image_tag or ""))
    repo = REPO_OF.get(app or "")
    return f"{REPO_BASE}{repo}/commit/{m.group(1)}" if m and repo else None


def _row(rid: str, t: Any, typ: str, app: Optional[str], title: str, *, href: Optional[str], by: Optional[str] = None,
         ref: Optional[str] = None, msg: Optional[dict] = None, t2: Any = None) -> dict:
    return {"id": rid, "t": _iso(t), "t2": _iso(t2), "type": typ, "app": app or "shared",
            "title": clean(title, 300) or title[:300],
            "by": by, "ref": ref, "href": href, "msg": msg or {"key": f"chg_{typ}"}}


def _deploys(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        tag = _json(r.get("details")).get("image_tag")
        failed = r["kind"] == "rollout_failed"
        out.append(_row(f"deploy-{r['event_id']}", r["occurred_at"], "deploy", r.get("app"),
                        f"{'Rollout failed' if failed else 'Deploy'} {r['subject']}"
                        + (f" ({tag})" if tag else ""), t2=r.get("ended_at"),
                        href=commit_href(r.get("app"), tag) or PAGES["delivery"], ref=tag,
                        msg={"key": "chg_deploy_failed" if failed else "chg_deploy", "subject": r["subject"],
                             "tag": tag}))
    return out


def _settings(rows: list[dict], names: dict) -> list[dict]:
    from api.services.ai_gateway import actor_view

    out = []
    for r in rows:
        who = r.get("decided_by") or r.get("proposed_by")
        name = actor_view(who, names).get("updated_by_name")
        scope = " (one tenant)" if r.get("tenant_scoped") else ""
        out.append(_row(f"setting-{r['id']}", r["effective_from"], "setting", r.get("app"),
                        f"Setting {r['label']} v{r['version']}{scope}", by=name, ref=r["key"],
                        href=f"{PAGES['settings']}?key={r['key']}",
                        msg={"key": "chg_setting", "label": r["label"], "version": r["version"],
                             "tenant": bool(r.get("tenant_scoped"))}))
    return out


def migration_title(version: Any, description: Optional[str]) -> str:
    """"Migration 488: Role connections check ..." — the tracker reference is taken off the description first, so a
    description that starts with one never leaves "Migration 488::" behind (S292 follow-up). Pure."""
    desc = strip_issue_refs(description)
    return f"Migration {version}" + (f": {desc}" if desc else "")


def _migrations(rows: list[dict]) -> list[dict]:
    return [_row(f"migration-{r['version']}", r["applied_at"], "migration", migration_app(r["version"]),
                 migration_title(r["version"], r.get("description")),
                 ref=str(r["version"]), href=PAGES["data_model"],
                 msg={"key": "chg_migration", "version": str(r["version"])}) for r in rows]


def _waves(rows: list[dict], rng: TimeRange) -> list[dict]:
    out = []
    for r in rows:
        for what, t, by in (("opened", r.get("created_at"), r.get("created_by")),
                            ("paused", r.get("paused_at"), r.get("paused_by")),
                            ("closed", r.get("closed_at"), None)):
            if _in(t, rng):
                out.append(_row(f"wave-{r['id']}-{what}", t, "wave", "cis", f"Wave {r['name']} {what}",
                                by=None if not by else str(by)[:64], ref=r["id"], href=PAGES["jobs"],
                                msg={"key": f"chg_wave_{what}", "name": r["name"]}))
    return out


def _routes(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        extra = f" ({r['preset']})" if r.get("preset") else (
            f" (back to v{r['reverted_from']})" if r.get("reverted_from") else "")
        out.append(_row(f"route-{r['version']}", r["created_at"], "route", "shared",
                        f"Route control v{r['version']}: {r['action']}{extra}", ref=str(r["version"]),
                        href=PAGES["routing"],
                        msg={"key": "chg_route", "version": r["version"], "action": r["action"],
                             "preset": r.get("preset"), "reverted_from": r.get("reverted_from")}))
    return out


def change_rows(src: dict[str, list[dict]], rng: TimeRange, names: Optional[dict] = None) -> list[dict]:
    """Every change of the range, newest first, from the source rows {type: rows}. Pure."""
    rows = (_deploys(src.get("deploy") or []) + _settings(src.get("setting") or [], names or {})
            + _migrations(src.get("migration") or []) + _waves(src.get("wave") or [], rng)
            + _routes(src.get("route") or []))
    return sorted(rows, key=lambda r: r["t"] or "", reverse=True)


async def _read(pool: Any, typ: str, rng: TimeRange) -> list[dict]:
    return [dict(r) for r in await pool.fetch(SOURCES[typ], rng.start, rng.end)]


async def collect(pool: Any, rng: TimeRange) -> dict:
    """{rows, counts, unavailable}: the range's changes of every type; a source that fails is named, not fatal."""
    from api.services.ai_gateway import actor_names

    async def one(typ: str) -> tuple[str, Optional[list[dict]], Optional[str]]:
        try:
            return typ, await _read(pool, typ, rng), None
        except (asyncpg.PostgresError, OSError) as e:
            logger.warning("ops_status_changes_source_failed", source=typ, error=repr(e)[:200])
            return typ, None, type(e).__name__

    got = await asyncio.gather(*(one(t) for t in TYPES))
    src = {t: rows for t, rows, _ in got if rows is not None}
    unavailable = {t: why for t, rows, why in got if rows is None}
    actors = [r.get("decided_by") or r.get("proposed_by") for r in src.get("setting") or []]
    try:
        async with pool.acquire() as conn:
            names = await actor_names(conn, actors)
    except asyncpg.PostgresError:
        names = {}
    rows = change_rows(src, rng, names)
    counts = {t: sum(1 for r in rows if r["type"] == t) for t in TYPES}
    return {"rows": rows, "counts": counts, "types": list(TYPES), "unavailable": unavailable}
