"""AA-1018 (reliability part 1) — the failure-domain rule, report only.

ADR 0009 / design v2 step 5: every stage must reach at least one enabled, funded deployment in ANOTHER failure domain
than its primary, so one blocked account (acc3 "Error 002" since 29/10/2026) never stops a stage. This module checks
that rule on the live registry and says which stages break it. It changes nothing: no route is edited, no call is
made. Shown by ``GET /admin/llm-availability`` (``invariant``) and the OPS-12 digest provider section.

Failure domain of a connection: ``<kind>:<account>:<data_scope>``, where account is the row's ``account_id``, else the
name of its ``account_id_env`` (the account comes from the task environment), else the connection key (a task-role
Bedrock connection is the main account; an API-key connection is its own vendor account). Migration 322 (registry v2,
AA-1017) adds a ``failure_domain`` column, backfilled with this same derivation; since AA-1018 part 2 the rule reads it
(the derivation stays the fallback for a NULL value and before 322).
Only the LLM kinds take part (322 also stores the decide / data / guard providers in the table).

Per stage (``shared.llm_role_config``, active rows): the route's keys (``model_id`` then ``fallback_model_ids``), each
alias expanded to its targets (``shared.llm_model_alias``, position order), each model's deployments in priority order.
A deployment is usable when it and its connection are enabled, the stage's ``allowed_scopes`` allow the connection's
data scope (no stage policy = ``aws_only`` fallbacks only, as ``vendor_route.deployment_plan`` treats it), and the
connection is funded: not stopped by a credit or key refusal (``blocked`` with class ``quota_exhausted`` or
``credential``). The primary domain is the first usable deployment's; the stage passes when another usable deployment
is in a different domain. Stages are grouped by stage class (the role: writer, judge, validate, embed).

AA-642 A1 (migration 326): ADR 0009 decision 5 accepts a **declared degrade mode** instead of a second domain
(``llm_role_config.degrade_mode``: queue_retry, basic_narration, ...). A stage without another usable domain but with
a degrade mode passes with ``status`` ``degrade`` (counted in ``declared_degrade``, never in ``breaches``); one with a
second domain is ``ok``; any other is ``breach``. A degrade mode stands in for the second domain only, never for the
first (review S263): a stage with no usable deployment at all is a ``breach`` whatever it declares, so a total outage
(e.g. every T-stage writer while its only account is blocked) is never shown as a calm declared degrade. A
switched-off stage (``is_active`` false, the kill switch) is not checked: it is listed in ``disabled``.

AA-642 A5 (gap RW-09): a deployment counts only when its connection serves the stage's role
(``llm_provider_connection.served_roles``, migration 322; NULL = every role), as ``vendor_route.deployment_plan`` now
routes. A stage whose route names a model on such a connection lists it in ``role_skipped`` ("<model>@<connection>"),
so the availability card shows what the role filter left out (e.g. an acc2 model on a writer route).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Optional

UNFUNDED_CLASSES = ("quota_exhausted", "credential")
AWS_ONLY = "aws_only"


def failure_domain(conn: dict) -> str:
    """The row's ``failure_domain`` (migration 322; set by migration only), else the derivation it was backfilled
    with (AA-1018 part 2: the column answers first)."""
    stored = conn.get("failure_domain")
    if isinstance(stored, str) and stored:
        return stored
    account = conn.get("account_id") or conn.get("account_id_env") or conn["connection_key"]
    return f"{conn['kind']}:{account}:{conn['data_scope']}"


@dataclass
class StageResult:
    stage: str
    role: str
    ok: bool
    primary: Optional[str]                  # "<connection_key> (<domain>)" of the first usable deployment
    primary_domain: Optional[str]
    other_domains: list[str] = field(default_factory=list)
    reason: Optional[str] = None
    degrade_mode: Optional[str] = None      # AA-642 A1: the declared degrade mode (None = none declared)
    role_skipped: list[str] = field(default_factory=list)   # AA-642 A5: "<model>@<connection>" not serving the role

    @property
    def status(self) -> str:
        """``ok`` (a usable deployment in another domain), ``degrade`` (one usable domain only, and a declared degrade
        mode) or ``breach`` (no second domain and no degrade mode, or no usable deployment at all)."""
        if self.other_domains:
            return "ok"
        return "degrade" if self.degrade_mode and self.primary_domain else "breach"

    def as_dict(self) -> dict:
        return {"stage": self.stage, "role": self.role, "ok": self.ok, "status": self.status, "primary": self.primary,
                "primary_domain": self.primary_domain, "other_domains": self.other_domains, "reason": self.reason,
                "degrade_mode": self.degrade_mode, "role_skipped": self.role_skipped}


def _keys(route: dict, aliases: dict[str, list[str]]) -> list[str]:
    out: list[str] = []
    for key in [route["model_id"], *(route.get("fallback_model_ids") or [])]:
        for k in aliases.get(key) or [key]:
            if k not in out:
                out.append(k)
    return out


def _allowed(scopes: Optional[Iterable[str]], scope: str, first: bool) -> bool:
    if scopes is None:
        return scope == AWS_ONLY or first
    return scope in set(scopes)


def serves(conn: dict, role: Optional[str]) -> bool:
    """AA-642 A5: the connection row serves ``role`` (NULL / missing served_roles = every role)."""
    roles = conn.get("served_roles")
    return not role or roles is None or role in roles


def check_stage(route: dict, aliases: dict[str, list[str]], deployments: dict[str, list[dict]],
                connections: dict[str, dict], blocked: dict[str, str]) -> StageResult:
    """One stage (pure)."""
    stage, role = route["stage"], route["role"]
    degrade = route.get("degrade_mode") or None
    usable: list[tuple[str, str]] = []           # (connection_key, domain), in route order
    role_skipped: list[str] = []
    for model_key in _keys(route, aliases):
        for dep in deployments.get(model_key, []):
            conn = connections.get(dep["connection_key"])
            if conn is None or not dep.get("enabled") or not conn.get("enabled"):
                continue
            if not serves(conn, role):
                role_skipped.append(f"{model_key}@{conn['connection_key']}")
                continue
            if blocked.get(conn["connection_key"]) in UNFUNDED_CLASSES:
                continue
            if not _allowed(route.get("allowed_scopes"), conn["data_scope"], first=not usable):
                continue
            usable.append((conn["connection_key"], failure_domain(conn)))
    if not usable:
        # A degrade mode never excuses the first domain (review S263): the stage cannot run at all.
        return StageResult(stage, role, False, None, None, reason="no enabled, funded deployment on the route",
                           degrade_mode=degrade, role_skipped=role_skipped)
    key, domain = usable[0]
    others = sorted({d for _, d in usable if d != domain})
    reason = None if others else f"every usable deployment is in {domain}"
    return StageResult(stage, role, bool(others) or bool(degrade), f"{key} ({domain})", domain, others, reason,
                       degrade_mode=degrade, role_skipped=role_skipped)


def check(routes: Iterable[dict], aliases: Iterable[dict], deployments: Iterable[dict], connections: Iterable[dict],
          blocked: Optional[dict[str, str]] = None) -> dict:
    """The report (pure): per stage and per stage class. ``routes``: llm_role_config rows; ``aliases``:
    llm_model_alias rows; ``deployments``: llm_model_deployment rows (model_key, connection_key, priority, enabled);
    ``connections``: llm_provider_connection rows; ``blocked``: {connection_key: class} of connections refusing now."""
    routes = list(routes)
    alias_map: dict[str, list[str]] = {}
    for a in sorted(aliases, key=lambda a: (a["alias_key"], a["position"])):
        alias_map.setdefault(a["alias_key"], []).append(a["model_key"])
    dep_map: dict[str, list[dict]] = {}
    for d in sorted(deployments, key=lambda d: (d["model_key"], d["priority"], d["connection_key"])):
        dep_map.setdefault(d["model_key"], []).append(d)
    conn_map = {c["connection_key"]: c for c in connections}
    stages = [check_stage(r, alias_map, dep_map, conn_map, blocked or {})
              for r in sorted(routes, key=lambda r: r["stage"]) if r.get("is_active", True)]
    classes: dict[str, dict] = {}
    for s in stages:
        c = classes.setdefault(s.role, {"role": s.role, "stages": 0, "ok": 0, "breaches": [], "degrade": []})
        c["stages"] += 1
        if s.ok:
            c["ok"] += 1
        else:
            c["breaches"].append(s.stage)
        if s.status == "degrade":
            c["degrade"].append(s.stage)
    return {"report_only": True, "ok": all(s.ok for s in stages), "stages_checked": len(stages),
            "breaches": sum(1 for s in stages if not s.ok),
            "declared_degrade": sum(1 for s in stages if s.status == "degrade"),
            "disabled": sorted(r["stage"] for r in routes if not r.get("is_active", True)),
            "domains": sorted({failure_domain(c) for c in conn_map.values() if c.get("enabled")}),
            "classes": [classes[k] for k in sorted(classes)], "stages": [s.as_dict() for s in stages]}


ROUTES_SQL = ("SELECT stage, role, model_id, fallback_model_ids, allowed_scopes, is_active "
              "FROM shared.llm_role_config ORDER BY stage")
# AA-642 A1: with migration 326's declared degrade mode.
ROUTES_V3_SQL = ROUTES_SQL.replace("is_active ", "is_active, degrade_mode ")
ALIASES_SQL = "SELECT alias_key, position, model_key FROM shared.llm_model_alias"
DEPLOYMENTS_SQL = "SELECT model_key, connection_key, priority, enabled FROM shared.llm_model_deployment"
CONNECTIONS_SQL = ("SELECT connection_key, kind, enabled, data_scope, account_id, account_id_env "
                   "FROM shared.llm_provider_connection "
                   "WHERE kind IN ('bedrock', 'anthropic', 'openai', 'azure_openai', 'mock')")
# AA-1018 part 2: with migration 322's failure_domain (the stored domain wins over the derivation).
CONNECTIONS_V2_SQL = CONNECTIONS_SQL.replace("account_id_env ", "account_id_env, failure_domain, served_roles ")


async def read(conn, *, blocked: Optional[dict[str, str]] = None, now: Optional[datetime] = None) -> dict:
    """The report from the live tables (four reads on one asyncpg connection). Before migrations 304 / 311 (no
    registry, no ``allowed_scopes``): ``{"ready": False}``."""
    import asyncpg

    try:
        try:
            routes = [dict(r) for r in await conn.fetch(ROUTES_V3_SQL)]
        except asyncpg.exceptions.UndefinedColumnError:          # before migration 326: no degrade modes
            routes = [dict(r) for r in await conn.fetch(ROUTES_SQL)]
        aliases = [dict(r) for r in await conn.fetch(ALIASES_SQL)]
        deps = [dict(r) for r in await conn.fetch(DEPLOYMENTS_SQL)]
        try:
            conns = [dict(r) for r in await conn.fetch(CONNECTIONS_V2_SQL)]
        except asyncpg.exceptions.UndefinedColumnError:          # before migration 322: the derivation
            conns = [dict(r) for r in await conn.fetch(CONNECTIONS_SQL)]
    except (asyncpg.exceptions.UndefinedTableError, asyncpg.exceptions.UndefinedColumnError):
        return {"ready": False, "report_only": True}
    out = check(routes, aliases, deps, conns, blocked)
    out["ready"] = True
    if now is not None:
        out["checked_at"] = now.isoformat()
    return out
