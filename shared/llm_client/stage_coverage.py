"""AA-1026 (S267) — stage coverage: does every active stage have, right now, at least one model that answers?

Tech lead request (S267): "the system must always have a usable model for each stage". The failure-domain rule
(``invariant.py``) asks whether a stage COULD fall back to another account; this module asks whether one of its models
is PROVEN to answer now. It reads the registry and the evidence the gateway already keeps, makes no provider call
($0), and changes nothing.

Per active stage (``shared.llm_role_config``, ``is_active``): the route's keys (``model_id`` then
``fallback_model_ids``, each alias expanded, ``invariant._keys``), each model's deployments in priority order. One
deployment is in one of these states (the first that applies):

- ``off``: the catalog model, the deployment or its connection is switched off (or missing);
- ``role_skip`` / ``scope_skip``: its connection does not serve the stage's role (``served_roles``), or the stage's
  ``allowed_scopes`` refuse its data scope (no stage policy: the primary model may use any scope, a fallback
  ``aws_only`` only, as ``vendor_route.deployment_plan`` routes);
- ``refused``: the account refuses it: the connection's ``access_state`` is ``unconfigured`` (a credential failure
  stops every model), ``quota_exhausted`` while this deployment has no answer of its own within ``PROOF_MAX_AGE``, or
  ``access_denied`` while it has no answer on record at all; or the deployment's newest refusal (breaker row
  ``llm_provider_health`` of class access_denied / quota_exhausted / credential, or the probe's
  ``llm_model_deployment.access_state``, CIS 367) is newer than its newest answer. The connection state is only a
  fallback for a deployment without evidence of its own: it is written without a time (``breaker.note_access_state``,
  once per change and process) and a quota of 0 on a few models of one account (acc3 sets quotas per model) trips the
  connection circuit and stamps ``quota_exhausted`` although its other models still answer; the gateway does not
  route on it either;
- ``circuit_open``: its breaker circuit, or its connection's circuit (``breaker.connection_health``), is OPEN now;
- ``live``: it answered (breaker ``last_ok_at``, or a probe that wrote ``access_state = 'ok'``) within
  ``PROOF_MAX_AGE`` and nothing above applies;
- ``unproven``: nothing above applies but there is no recent answer on record (the daily probe has not reached it).

A stage is ``live`` when at least one deployment is ``live`` (``live_model`` = the first in route order, the one the
gateway reaches first among the live ones), else ``none``: "NO LIVE MODEL". ``none`` raises the ops check
``llm_stage_coverage.<stage>`` (``shared/ops_checks.py``: admin bell + one e-mail per stage per UTC day) and shows on
the AI gateway Routing tab (Stage coverage card, ``GET /admin/llm-availability`` field ``stage_coverage``).

Before migration 367 the deployment rows have no probe state: the breaker rows alone are the evidence.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from .breaker import (OPEN, SELECT_SQL as HEALTH_SQL, Health, connection_health, row_to_health, state_of)
from .invariant import ALIASES_SQL, ROUTES_SQL, ROUTES_V3_SQL, _allowed, _keys, serves

PROOF_MAX_AGE = timedelta(hours=36)        # the daily probe (05:00 UTC) plus a margin: older proof is "unproven"
REFUSING_CLASSES = ("access_denied", "quota_exhausted", "credential")
DEPLOYMENT_REFUSED_STATES = ("access_denied", "quota_exhausted", "unconfigured")
LLM_KINDS = ("bedrock", "anthropic", "openai", "azure_openai", "mock")
STATES = ("live", "unproven", "circuit_open", "refused", "role_skip", "scope_skip", "off")
MAX_MODELS_LISTED = 12

CATALOG_SQL = "SELECT model_key, enabled FROM shared.llm_model_catalog"
DEPLOYMENTS_V2_SQL = ("SELECT model_key, connection_key, priority, enabled, access_state, last_probe_at "
                      "FROM shared.llm_model_deployment")
DEPLOYMENTS_SQL = "SELECT model_key, connection_key, priority, enabled FROM shared.llm_model_deployment"
CONNECTIONS_SQL = ("SELECT connection_key, kind, enabled, data_scope, served_roles, access_state "
                   "FROM shared.llm_provider_connection WHERE kind = ANY ($1::text[])")


def _latest(*values: Optional[datetime]) -> Optional[datetime]:
    present = [v for v in values if v is not None]
    return max(present) if present else None


def _last_ok(dep: dict, health: Optional[Health]) -> Optional[datetime]:
    """The deployment's newest answer: the breaker's ``last_ok_at`` or a probe that wrote ``access_state = 'ok'``."""
    probe_at = dep.get("last_probe_at")
    return _latest(health.last_ok_at if health else None,
                   probe_at if (dep.get("access_state") or "unknown") == "ok" else None)


def refusal(dep: dict, conn: dict, health: Optional[Health], now: datetime) -> Optional[str]:
    """Why the account refuses this deployment (the ``refused`` state of the module docstring), or None (pure).

    The one refusal rule: the Stage coverage card (``assess``) and the route preview / $0 route check
    (``api/services/llm_route_chains.resolve_stage``, AA-1042) both call it, so the model a preview says serves first
    is the one the card calls live."""
    probe_state = dep.get("access_state") or "unknown"
    probe_at = dep.get("last_probe_at")
    last_ok = _last_ok(dep, health)
    refused_health = health.last_error_at if health and health.last_error_class in REFUSING_CLASSES else None
    last_bad = _latest(refused_health, probe_at if probe_state in DEPLOYMENT_REFUSED_STATES else None)
    conn_state = conn.get("access_state") or "unknown"
    recent_ok = last_ok is not None and now - last_ok <= PROOF_MAX_AGE
    if conn_state == "unconfigured":
        return "connection unconfigured"                         # a credential failure stops every model
    if conn_state == "quota_exhausted" and not recent_ok:
        return "connection quota_exhausted"
    if conn_state == "access_denied" and last_ok is None:
        return "connection access_denied"
    if last_bad is not None and (last_ok is None or last_bad >= last_ok):
        cls = health.last_error_class if refused_health == last_bad else probe_state
        return f"refused ({cls})"
    return None


def assess(dep: dict, conn: Optional[dict], model_enabled: Optional[bool], health: Optional[Health], *,
           connection_open: bool, role: Optional[str], scopes, primary: bool, now: datetime) -> tuple[str, str]:
    """(state, why) of one deployment for one stage (pure). See the module docstring for the states."""
    if not model_enabled:
        return "off", "model switched off" if model_enabled is False else "no catalog row"
    if conn is None or not conn.get("enabled"):
        return "off", "connection switched off"
    if not dep.get("enabled"):
        return "off", "deployment switched off"
    if not serves(conn, role):
        return "role_skip", f"connection does not serve {role}"
    if not _allowed(scopes, conn.get("data_scope") or "", primary):
        return "scope_skip", f"data scope {conn.get('data_scope')} not allowed"
    why = refusal(dep, conn, health, now)
    if why is not None:
        return "refused", why
    last_ok = _last_ok(dep, health)
    recent_ok = last_ok is not None and now - last_ok <= PROOF_MAX_AGE
    if connection_open:
        return "circuit_open", "connection circuit open"
    if health is not None and state_of(health, now) == OPEN:
        return "circuit_open", f"circuit open ({health.last_error_class or 'other'})"
    if recent_ok:
        return "live", "answered " + last_ok.isoformat()
    return "unproven", "no recent answer on record" if last_ok is None else "last answer " + last_ok.isoformat()


def check_stage(route: dict, aliases: dict, deployments: dict, connections: dict, catalog: dict,
                health: dict, open_connections: set, now: datetime) -> dict:
    """One stage (pure)."""
    role, scopes = route.get("role"), route.get("allowed_scopes")
    keys = _keys(route, aliases)
    primary_key = keys[0] if keys else None
    lines: list[dict] = []
    live: Optional[dict] = None
    for model_key in keys:
        deps = deployments.get(model_key) or []
        if not deps:
            lines.append({"model_key": model_key, "connection_key": None, "state": "off", "why": "no deployment"})
            continue
        for dep in deps:
            ck = dep["connection_key"]
            state, why = assess(dep, connections.get(ck), catalog.get(model_key), health.get((ck, model_key)),
                                connection_open=ck in open_connections, role=role, scopes=scopes,
                                primary=model_key == primary_key, now=now)
            line = {"model_key": model_key, "connection_key": ck, "state": state, "why": why}
            lines.append(line)
            if state == "live" and live is None:
                live = line
    return {"stage": route["stage"], "role": role, "status": "live" if live else "none",
            "live_model": live["model_key"] if live else None,
            "live_connection": live["connection_key"] if live else None,
            "unproven": sum(1 for x in lines if x["state"] == "unproven"),
            "models": lines[:MAX_MODELS_LISTED]}


def _connection_open(rows: list[Health], connection_key: str, now: datetime) -> bool:
    h = connection_health(rows, connection_key, now)
    return h is not None and state_of(h, now) == OPEN


def evaluate(routes: Iterable[dict], aliases: Iterable[dict], catalog: Iterable[dict], deployments: Iterable[dict],
             connections: Iterable[dict], health: Iterable[Health], now: Optional[datetime] = None) -> dict:
    """The report (pure). ``routes``: llm_role_config rows; ``aliases``: llm_model_alias rows; ``catalog``: (model_key,
    enabled); ``deployments``: llm_model_deployment rows (with the CIS 367 probe state when present); ``connections``:
    llm_provider_connection rows; ``health``: breaker rows."""
    now = now or datetime.now(timezone.utc)
    routes = list(routes)
    alias_map: dict[str, list[str]] = {}
    for a in sorted(aliases, key=lambda a: (a["alias_key"], a["position"])):
        alias_map.setdefault(a["alias_key"], []).append(a["model_key"])
    dep_map: dict[str, list[dict]] = {}
    for d in sorted(deployments, key=lambda d: (d["model_key"], d["priority"], d["connection_key"])):
        dep_map.setdefault(d["model_key"], []).append(d)
    conn_map = {c["connection_key"]: c for c in connections}
    cat_map = {c["model_key"]: bool(c["enabled"]) for c in catalog}
    rows = list(health)
    health_map = {(h.connection_key, h.model_key): h for h in rows}
    open_conns = {k for k in conn_map if _connection_open(rows, k, now)}
    stages = [check_stage(r, alias_map, dep_map, conn_map, cat_map, health_map, open_conns, now)
              for r in sorted(routes, key=lambda r: r["stage"]) if r.get("is_active", True)]
    uncovered = [s["stage"] for s in stages if s["status"] != "live"]
    return {"ready": True, "ok": not uncovered, "checked_at": now.isoformat(), "stages_checked": len(stages),
            "uncovered": uncovered, "proof_max_age_hours": int(PROOF_MAX_AGE.total_seconds() // 3600),
            "disabled": sorted(r["stage"] for r in routes if not r.get("is_active", True)), "stages": stages}


def summary(report: dict) -> dict:
    """The short form (the probe job's result, the log line)."""
    if not report.get("ready"):
        return {"ready": False}
    return {"ready": True, "ok": report["ok"], "stages_checked": report["stages_checked"],
            "uncovered": report["uncovered"],
            "live": {s["stage"]: f"{s['live_model']}@{s['live_connection']}" for s in report["stages"]
                     if s["status"] == "live"}}


@dataclass(frozen=True)
class RefusalEvidence:
    """AA-1042: the evidence ``refusal`` reads, keyed for a lookup per model@connection: the deployment rows (with the
    CIS 367 probe state when present), the connection rows and the breaker rows, read once at ``now``. The route
    preview and the $0 route check pass it to ``llm_route_chains.resolve_stage``."""
    deployments: dict = field(default_factory=dict)     # (connection_key, model_key) -> llm_model_deployment row
    connections: dict = field(default_factory=dict)     # connection_key -> llm_provider_connection row
    health: dict = field(default_factory=dict)          # (connection_key, model_key) -> breaker Health
    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @classmethod
    def of(cls, deployments: Iterable[dict], connections: Iterable[dict], health: Iterable[Health],
           now: Optional[datetime] = None) -> "RefusalEvidence":
        return cls(deployments={(d["connection_key"], d["model_key"]): d for d in deployments},
                   connections={c["connection_key"]: c for c in connections},
                   health={(h.connection_key, h.model_key): h for h in health},
                   now=now or datetime.now(timezone.utc))

    def refused(self, model_key: str, connection_key: str) -> Optional[str]:
        """``refusal`` of one deployment; None when it has no row here (nothing on record refuses it)."""
        dep = self.deployments.get((connection_key, model_key))
        conn = self.connections.get(connection_key)
        if dep is None or conn is None:
            return None
        return refusal(dep, conn, self.health.get((connection_key, model_key)), self.now)


async def load_evidence(conn, *, now: Optional[datetime] = None) -> Optional[RefusalEvidence]:
    """The evidence from the live tables (three reads on ``conn``: an asyncpg connection, or a pool). None before the
    registry migrations; before 367 the deployments carry no probe state (the breaker rows alone count)."""
    import asyncpg

    try:
        try:
            deps = [dict(r) for r in await conn.fetch(DEPLOYMENTS_V2_SQL)]
        except asyncpg.exceptions.UndefinedColumnError:          # before migration 367: no probe state
            deps = [dict(r) for r in await conn.fetch(DEPLOYMENTS_SQL)]
        conns = [dict(r) for r in await conn.fetch(CONNECTIONS_SQL, list(LLM_KINDS))]
        health = [row_to_health(r) for r in await conn.fetch(HEALTH_SQL)]
    except (asyncpg.exceptions.UndefinedTableError, asyncpg.exceptions.UndefinedColumnError):
        return None
    return RefusalEvidence.of(deps, conns, health, now)


async def read(conn, *, now: Optional[datetime] = None) -> dict:
    """The report from the live tables (six reads on ``conn``: an asyncpg connection, or a pool). Before the registry
    migrations (304 / 309 / 322): ``{"ready": False}``; before 367 the deployments are read without their probe
    state."""
    import asyncpg

    try:
        try:
            routes = [dict(r) for r in await conn.fetch(ROUTES_V3_SQL)]
        except asyncpg.exceptions.UndefinedColumnError:
            routes = [dict(r) for r in await conn.fetch(ROUTES_SQL)]
        aliases = [dict(r) for r in await conn.fetch(ALIASES_SQL)]
        catalog = [dict(r) for r in await conn.fetch(CATALOG_SQL)]
        try:
            deps = [dict(r) for r in await conn.fetch(DEPLOYMENTS_V2_SQL)]
        except asyncpg.exceptions.UndefinedColumnError:          # before migration 367: no probe state
            deps = [dict(r) for r in await conn.fetch(DEPLOYMENTS_SQL)]
        conns = [dict(r) for r in await conn.fetch(CONNECTIONS_SQL, list(LLM_KINDS))]
        health = [row_to_health(r) for r in await conn.fetch(HEALTH_SQL)]
    except (asyncpg.exceptions.UndefinedTableError, asyncpg.exceptions.UndefinedColumnError):
        return {"ready": False}
    return evaluate(routes, aliases, catalog, deps, conns, health, now)
