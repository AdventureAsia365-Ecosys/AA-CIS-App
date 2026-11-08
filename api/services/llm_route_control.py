"""AA-1042 (S268, route control center, slice "Admin controls") — the admin's route control of the AI gateway.

One config (``shared/llm_client/route_versions.py``): the route ORDER MODE (``shared.llm_route_settings``), each stage's
CHAIN (primary + fallbacks, ``shared.llm_role_config``) and each model's PROVIDER ORDER (the ``priority`` of its
deployments, ``shared.llm_model_deployment``: which connection of the same model is tried first, e.g. acc3 or the
Anthropic API). Every save is a new version with who / when / diff; any version is restored in one click.

- ``read_state(pool)``: the page's read (GET /admin/ai-gateway/route-control).
- ``preview(pool, proposal)``: what the proposed config would do, BEFORE saving: per active stage the first
  model@connection that would SERVE it (``llm_route_chains.resolve_stage``, $0, no model call; AA-1042: a
  deployment the account refuses on record, ``stage_coverage.refusal``, is skipped and listed with its reason in the
  stage's ``skipped``) now and with the proposal, the rule check (errors block a save, warnings do not), whether the
  change needs eval evidence (AA-1026 D7: a writer / judge stage whose chain OR first serving model@connection
  changes, so a provider-order change or a preset that moves such a stage to another deployment counts too) and the
  diff.
- ``apply(pool, proposal, ...)``: the save, a preset or a revert: validated again, written in ONE transaction with its
  version row and its ``acp_shared.audit_log`` row (and the break-glass bell of each changed writer / judge stage).
- ``preset_proposal`` / ``revert_proposal``: the proposal a preset or a version stands for.

Rules (server side, on preview and on every write):
- per changed stage, the Routing tab's own PATCH rules (``api/routers/admin_llm_ops.py``): a selectable model for the
  stage (family rule, enabled, data scopes), no model twice, a model reached only outside AWS only as the last rung
  or right behind its AWS twin, never before an AWS model;
- the chain rules of ``llm_route_chains.violations`` the tech lead named: a judge never of the writers' family
  (``judge_writer_family``), check pairs on different makers (``check_pair_family``), at most
  ``errors.MAX_CHAIN_ATTEMPTS`` attempts (``attempt_cap``). The other chain rules are warnings;
- every active stage keeps a model that would be called now (``no_live_model``).
A rule the LIVE config already breaks is a warning (``existing``): a save is refused only for what it breaks itself
(today ``s1_atom_writer`` / ``tp_extract`` have no live model until CIS 336 switches them off).
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Optional

import asyncpg
import structlog

from api.services import llm_route_chains
from shared.llm_client import data_scope, family_rule, route_evidence, route_settings, route_versions, stage_coverage
from shared.llm_client.errors import MAX_CHAIN_ATTEMPTS

logger = structlog.get_logger()

ORDER_MODES = route_settings.ORDER_MODES
BLOCKING_CHAIN_RULES = frozenset({"judge_writer_family", "check_pair_family", "attempt_cap"})
ANTHROPIC_KIND = "anthropic"
PRIORITY_STEP = 10

# The two presets (tech lead S267). Labels and descriptions: the page's English copy (the frontend translates by key).
PRESETS: dict[str, dict] = {
    "prefer_anthropic_api": {
        "label": "Prefer Anthropic API for Claude (sponsored)",
        "description": "Every Claude model is tried on the Anthropic API first, then on AWS (acc3). Sets the order "
                       "mode to model first, so the API is tried before the next model. Chains are not changed.",
    },
    "prefer_aws": {
        "label": "Prefer AWS",
        "description": "Every model is tried on its AWS deployments first, then on its vendor API. The order mode and "
                       "the chains are not changed.",
    },
}

_AUDIT_ACTIONS = {"save": "llm_route_config_saved", "preset": "llm_route_config_preset_applied",
                  "revert": "llm_route_config_reverted"}
_AUDIT_SQL = """
    INSERT INTO acp_shared.audit_log (actor, action, resource_type, resource_id, details)
    VALUES ($1, $2, 'llm_route_config', $3, $4::jsonb)
"""


class RouteControlUnavailable(RuntimeError):
    """Migration 370 is not applied (no settings row): the route control cannot read or write."""


class RouteControlRefused(ValueError):
    """The proposal breaks a rule, or the evidence is missing (422)."""


class RouteControlConflict(RuntimeError):
    """Another change was saved since the version the caller looked at (409)."""


class RouteControlFrozen(RouteControlConflict):
    """AA-1052 W6: the change touches a gated stage while a rerun wave is open (409, services/quality/edit_gate.py)."""


# ── reads ────────────────────────────────────────────────────────────────────────────────────────────────────────

_SETTINGS_SQL = ("SELECT order_mode, current_version, updated_at, updated_by FROM shared.llm_route_settings "
                 "WHERE singleton")
_DEPLOYMENTS_SQL = ("SELECT model_key, connection_key, priority, enabled, access_state "
                    "FROM shared.llm_model_deployment ORDER BY model_key, priority, connection_key")
_CONNECTIONS_SQL = "SELECT connection_key, kind, label, data_scope, enabled FROM shared.llm_provider_connection"
_VERSIONS_SQL = ("SELECT version, created_at, created_by, action, preset, reverted_from, note, diff "
                 "FROM shared.llm_route_config_version ORDER BY version DESC LIMIT $1")
_VERSION_SQL = ("SELECT version, created_at, created_by, action, preset, reverted_from, note, diff, snapshot "
                "FROM shared.llm_route_config_version WHERE version = $1")


async def _settings(conn) -> dict:
    try:
        row = await conn.fetchrow(_SETTINGS_SQL)
    except (asyncpg.exceptions.UndefinedTableError, asyncpg.exceptions.UndefinedColumnError):
        row = None
    if row is None:
        raise RouteControlUnavailable("route control is not set up on this server yet (migration 370)")
    return dict(row)


@dataclass
class _World:
    """Everything a preview reads once: the live config, the stage rows, the catalog and the registry."""
    settings: dict
    live: dict                              # route_versions snapshot of the live rows
    stages: list                            # list_stage_configs rows
    models: list                            # CatalogModel
    deployments: list                       # dict rows of _DEPLOYMENTS_SQL
    connections: dict                       # connection_key -> dict row
    reach: dict = field(default_factory=dict)
    evidence: Optional[object] = None       # stage_coverage.RefusalEvidence (AA-1042: refused deployments skipped)

    @property
    def by_stage(self) -> dict:
        return {r["stage"]: r for r in self.stages}

    @property
    def by_model(self) -> dict:
        return {m.model_key: m for m in self.models}


async def _world(pool) -> _World:
    from shared.llm_client.catalog import list_models
    from shared.llm_client.role_config import list_stage_configs
    async with pool.acquire() as conn:
        settings = await _settings(conn)
        live = await route_versions.read_live(conn, settings["order_mode"])
        deps = [dict(r) for r in await conn.fetch(_DEPLOYMENTS_SQL)]
        conns = {r["connection_key"]: dict(r) for r in await conn.fetch(_CONNECTIONS_SQL)}
        evidence = await stage_coverage.load_evidence(conn)
    stages = await list_stage_configs()
    models = await list_models()
    reach: dict[str, frozenset] = {}
    for d in deps:
        c = conns.get(d["connection_key"])
        if c is not None:
            reach[d["model_key"]] = reach.get(d["model_key"], frozenset()) | {c["data_scope"]}
    return _World(settings=settings, live=route_versions.normalize(live), stages=stages, models=models,
                  deployments=deps, connections=conns, reach=reach, evidence=evidence)


def _provider_rows(world: _World, priorities: Optional[dict] = None) -> list[dict]:
    """One entry per model with deployments: its connections in the order the gateway tries them."""
    labels = {m.model_key: m.label for m in world.models}
    families = {m.model_key: m.family_key for m in world.models}
    by_model: dict[str, list] = {}
    for d in world.deployments:
        by_model.setdefault(d["model_key"], []).append(d)
    out = []
    for model in sorted(by_model):
        prio = (priorities or world.live["priorities"]).get(model) or {}
        rows = sorted(by_model[model], key=lambda d: (int(prio.get(d["connection_key"], d["priority"])),
                                                      d["connection_key"]))
        out.append({
            "model_key": model, "label": labels.get(model, model), "family": families.get(model),
            "deployments": [{
                "connection_key": d["connection_key"],
                "label": (world.connections.get(d["connection_key"]) or {}).get("label") or d["connection_key"],
                "kind": (world.connections.get(d["connection_key"]) or {}).get("kind"),
                "data_scope": (world.connections.get(d["connection_key"]) or {}).get("data_scope"),
                "priority": int(prio.get(d["connection_key"], d["priority"])),
                "enabled": bool(d["enabled"]),
                "connection_enabled": bool((world.connections.get(d["connection_key"]) or {}).get("enabled")),
                "access_state": d.get("access_state"),
            } for d in rows],
        })
    return out


async def read_state(pool) -> dict:
    """GET /admin/ai-gateway/route-control: the live config with what the editor needs (options per stage)."""
    return state_of(await _world(pool))


async def read_world(pool) -> _World:
    """AA-1042 (Guide tab): the live config, stage rows, catalog, registry and refusal evidence, read once."""
    return await _world(pool)


def state_of(world: _World) -> dict:
    """The GET /admin/ai-gateway/route-control payload of one ``world`` (the Guide tab reads the same)."""
    from api.routers.admin_llm_ops import _options_for
    stages = []
    for r in world.stages:
        scopes = data_scope.normalize(r.get("allowed_scopes"))
        stages.append({
            "stage": r["stage"], "role": r["role"], "is_active": r.get("is_active") is not False,
            "allowed_scopes": list(scopes) if scopes else None,
            "pii_class": data_scope.normalize_pii_class(r.get("pii_class")),     # AA-1039 (CIS 373)
            "chain": route_versions.chain_of(world.live["routes"].get(r["stage"])) or [r["model_id"]],
            "options": _options_for(r["role"], r["stage"], world.models, scopes, world.reach),
            "product": llm_route_chains.product_of(r["stage"]),
        })
    s = world.settings
    return {
        "ready": True,
        "version": int(s["current_version"]),
        "order_mode": s["order_mode"],
        "env_order_mode": route_settings.env_order_mode(),
        "updated_at": s["updated_at"].isoformat() if s.get("updated_at") else None,
        "updated_by": s.get("updated_by"),
        "max_attempts": MAX_CHAIN_ATTEMPTS,
        "stages": stages,
        "models": {m.model_key: {"label": m.label, "family": m.family_key, "enabled": m.enabled}
                   for m in world.models},
        "provider_order": _provider_rows(world),
        "presets": [{"key": k, **v} for k, v in PRESETS.items()],
    }


async def list_versions(pool, limit: int = 50) -> dict:
    async with pool.acquire() as conn:
        settings = await _settings(conn)
        rows = await conn.fetch(_VERSIONS_SQL, max(1, min(limit, 200)))
    return {"current": int(settings["current_version"]), "versions": [_version_row(r) for r in rows]}


async def read_version(pool, version: int) -> Optional[dict]:
    async with pool.acquire() as conn:
        await _settings(conn)
        row = await conn.fetchrow(_VERSION_SQL, version)
    if row is None:
        return None
    out = _version_row(row)
    out["snapshot"] = route_versions.normalize(row["snapshot"])
    return out


def _version_row(r) -> dict:
    diff = r["diff"]
    return {"version": int(r["version"]), "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            "created_by": r["created_by"], "action": r["action"], "preset": r["preset"],
            "reverted_from": r["reverted_from"], "note": r["note"],
            "diff": json.loads(diff) if isinstance(diff, (str, bytes)) else (diff or [])}


# ── proposals ────────────────────────────────────────────────────────────────────────────────────────────────────

def _problem(rule: str, message: str, *, stage: Optional[str] = None, model: Optional[str] = None,
             existing: bool = False) -> dict:
    out = {"rule": rule, "message": message, "stage": stage, "model": model}
    if existing:
        out["existing"] = True
    return out


def build_target(world_live: dict, *, order_mode: Optional[str] = None, routes: Optional[dict] = None,
                 provider_order: Optional[dict] = None, priorities: Optional[dict] = None,
                 deployments: Optional[list] = None, stages: Optional[set] = None) -> tuple[dict, list]:
    """The proposed config on top of the live one (pure), and the shape errors. ``routes``: stage -> chain (primary
    first); ``provider_order``: model -> connection keys in the wanted order (a permutation of the model's
    deployments; renumbered 10, 20 … only when the order changes); ``priorities``: model -> {connection: priority}
    (a revert: exact numbers, connections that no longer exist are dropped); ``deployments``: the deployment rows
    (model_key, connection_key); ``stages``: the stage names that exist."""
    target = route_versions.normalize(world_live)
    errors: list = []
    if order_mode is not None:
        if order_mode not in ORDER_MODES:
            errors.append(_problem("order_mode", f"unknown order mode {order_mode!r} (model_first or aws_first)"))
        else:
            target["order_mode"] = order_mode
    for stage, chain in (routes or {}).items():
        if stages is not None and stage not in stages:
            errors.append(_problem("unknown_stage", f"unknown stage {stage!r}", stage=stage))
            continue
        keys = [k for k in (chain or []) if isinstance(k, str) and k.strip()]
        if not keys:
            errors.append(_problem("empty_chain", "keep at least one model (the primary)", stage=stage))
            continue
        target["routes"][stage] = {"model_id": keys[0], "fallback_model_ids": keys[1:]}
    conns_of: dict[str, set] = {}
    for d in deployments or []:
        conns_of.setdefault(d["model_key"], set()).add(d["connection_key"])
    for model, order in (provider_order or {}).items():
        have = conns_of.get(model)
        order = list(order or [])
        if not have:
            errors.append(_problem("unknown_model", f"{model} has no deployment", model=model))
            continue
        if sorted(order) != sorted(have) or len(order) != len(set(order)):
            errors.append(_problem("provider_order", f"the provider order of {model} must list each of its "
                                                     f"connections once ({', '.join(sorted(have))})", model=model))
            continue
        current = target["priorities"].get(model) or {}
        if route_versions.provider_order(current) == order:
            continue
        target["priorities"][model] = {c: (i + 1) * PRIORITY_STEP for i, c in enumerate(order)}
    for model, prio in (priorities or {}).items():
        have = conns_of.get(model) if deployments is not None else set((target["priorities"].get(model) or {}))
        if not have:
            continue
        merged = dict(target["priorities"].get(model) or {})
        merged.update({c: int(p) for c, p in (prio or {}).items() if c in have})
        target["priorities"][model] = merged
    return target, errors


def preset_changes(preset: str, live: dict, models: list, connections: dict) -> dict:
    """The proposal of a preset (pure): ``{"provider_order": {model: [conn, ...]}, "order_mode": ...}`` with only the
    models whose order changes. Unknown preset: KeyError."""
    if preset not in PRESETS:
        raise KeyError(preset)
    live = route_versions.normalize(live)
    families = {m.model_key: m.family_key for m in models}
    out: dict = {"provider_order": {}}
    for model, prio in live["priorities"].items():
        order = route_versions.provider_order(prio)
        if len(order) < 2:
            continue
        if preset == "prefer_anthropic_api":
            if families.get(model) != family_rule.WRITER_FAMILY:
                continue
            first = [c for c in order if (connections.get(c) or {}).get("kind") == ANTHROPIC_KIND]
        else:
            first = [c for c in order if (connections.get(c) or {}).get("data_scope") == data_scope.AWS_ONLY]
        wanted = first + [c for c in order if c not in first]
        if wanted != order:
            out["provider_order"][model] = wanted
    if preset == "prefer_anthropic_api":
        out["order_mode"] = route_settings.MODEL_FIRST
    return out


def revert_changes(snapshot: dict) -> dict:
    """The proposal of a revert (pure): the snapshot's order mode, chains and exact priorities."""
    snap = route_versions.normalize(snapshot)
    return {"order_mode": snap["order_mode"],
            "routes": {s: route_versions.chain_of(r) for s, r in snap["routes"].items()},
            "priorities": snap["priorities"]}


# ── validation ───────────────────────────────────────────────────────────────────────────────────────────────────

def chain_problems(row: dict, chain: list, models: list, reach: dict,
                   pii_usable: Optional[frozenset] = None) -> list[dict]:
    """The Routing tab's PATCH rules on one stage's proposed chain (``admin_llm_ops``): selectable models, data
    scopes, the vendor-only rungs, no model twice. Pure but for the catalog objects it is given and, for a pii stage,
    ``pii_usable`` (``admin_llm_ops._pii_usable_models``: a registry read, so an async caller computes it once in a
    thread and passes it; None = read it here)."""
    from fastapi import HTTPException

    from api.routers import admin_llm_ops as ops
    stage, role = row["stage"], row["role"]
    scopes = data_scope.normalize(row.get("allowed_scopes"))
    out: list[dict] = []
    if len(chain) != len(set(chain)):
        out.append(_problem("duplicate", "a model is listed twice", stage=stage))
    options = {o["model_id"]: o for o in ops._options_for(role, stage, models, scopes, reach)}
    outside = data_scope.outside_aws_only(reach)
    twins = data_scope.vendor_twins(models or [], reach)
    if chain and chain[0] in outside:
        out.append(_problem("vendor_last_resort", ops._VENDOR_DIRECT_LAST_RESORT_ONLY, stage=stage, model=chain[0]))
    for i, m in enumerate(chain):
        if 0 < i < len(chain) - 1 and m in outside and not data_scope.in_twin_slot(chain, i, twins):
            out.append(_problem("vendor_last_resort", ops._VENDOR_DIRECT_LAST_RESORT_ONLY, stage=stage, model=m))
    for i, m in enumerate(chain):
        opt = options.get(m)
        if opt is not None and opt.get("available"):
            continue
        if i > 0 and i == len(chain) - 1 and ops._writer_last_resort(role, scopes, models, outside, m):
            continue
        why = (opt or {}).get("reason") or "not in the model catalog for this kind of stage"
        out.append(_problem("not_selectable", f"{m} cannot serve this stage: {why}", stage=stage, model=m))
    blocked = [k for k in chain if not ops._scope_reaches(scopes, reach, k)]
    if blocked:
        out.append(_problem("data_scope", f"{', '.join(blocked)} is reached only through a vendor API, and this "
                                          f"stage may not leave AWS (data scopes: {', '.join(scopes or ())})",
                            stage=stage))
    try:
        ops._check_aws_before_vendor(stage, scopes, reach, chain, twins)
    except HTTPException as e:
        out.append(_problem("vendor_not_last", str(e.detail), stage=stage))
    # AA-1039 (CIS 373): a personal-data stage names only models with a deployment on our AWS accounts in the US.
    if data_scope.normalize_pii_class(row.get("pii_class")) == data_scope.PII_PERSONAL:
        usable = pii_usable if pii_usable is not None else ops._pii_usable_models(models)
        no_us = [k for k in chain if k not in usable]
        if no_us:
            out.append(_problem("pii_class", f"{', '.join(no_us)} has no deployment on our AWS accounts in the US, "
                                             "and this stage handles personal data", stage=stage))
    return out


def _rows_with(stages: list, routes: dict) -> list[dict]:
    """The stage rows with the chains of ``routes`` (snapshot routes)."""
    out = []
    for r in stages:
        route = routes.get(r["stage"])
        row = dict(r)
        if route:
            row["model_id"] = route["model_id"]
            row["fallback_model_ids"] = list(route["fallback_model_ids"])
        out.append(row)
    return out


def _deployment_maps(world: _World) -> tuple[set, dict]:
    """The chat models served only by acc2, and model -> the data scope of each deployment row (attempt cap)."""
    acc2: dict[str, set] = {}
    per_row: dict[str, list] = {}
    styles = {m.model_key: m.api_style for m in world.models}
    for d in world.deployments:
        acc2.setdefault(d["model_key"], set()).add(d["connection_key"])
        c = world.connections.get(d["connection_key"])
        if c is not None:
            per_row.setdefault(d["model_key"], []).append(c["data_scope"])
    acc2_only = {m for m, cs in acc2.items() if cs == {llm_route_chains.ACC2_CONNECTION} and styles.get(m) != "embed"}
    return acc2_only, per_row


def _config(row: dict):
    from shared.llm_client.role_config import StageConfig
    return StageConfig(stage=row["stage"], role=row["role"], provider=row["provider"], model_id=row["model_id"],
                       account_route=row.get("account_route"),
                       fallback_model_ids=tuple(row.get("fallback_model_ids") or ()),
                       shadow_model_id=row.get("shadow_model_id"),
                       shadow_sample_pct=row.get("shadow_sample_pct") or 0,
                       allowed_scopes=tuple(row["allowed_scopes"]) if row.get("allowed_scopes") else None,
                       enabled=row.get("is_active") is not False,
                       pii_class=data_scope.normalize_pii_class(row.get("pii_class")))


def _resolve_all(rows: list[dict], priorities: dict, order_mode: str, evidence=None) -> dict:
    """Sync: stage -> resolve_stage(...) for every active row, with the proposed priorities and order mode in force
    for THIS context only (deployments.priority_override / route_settings.override_order_mode). ``evidence``: the
    refusals on record (AA-1042), the same for now and the proposal: "first" is the deployment that would serve."""
    from shared.llm_client import deployments
    out = {}
    with deployments.priority_override(priorities), route_settings.override_order_mode(order_mode):
        for row in rows:
            if row.get("is_active") is False:
                continue
            out[row["stage"]] = llm_route_chains.resolve_stage(_config(row), evidence)
    return out


async def serving(world: _World) -> dict:
    """AA-1042 (Guide tab): stage -> ``resolve_stage`` of every active stage with the LIVE config, the resolver the
    preview's "first" comes from (the deployment that would serve, refused ones skipped), never the chain's first
    key. $0, no model call."""
    live = world.live
    rows = _rows_with(world.stages, live["routes"])
    return await asyncio.to_thread(_resolve_all, rows, live["priorities"], live["order_mode"], world.evidence)


async def preview_world(world: _World, target: dict, shape_errors: list) -> dict:
    """The preview of ``target`` against the live config in ``world`` (see the module docstring)."""
    live = world.live
    changes = route_versions.diff(live, target)
    errors = list(shape_errors)
    warnings: list = []
    by_stage = world.by_stage
    changed_stages = sorted({c["stage"] for c in changes if c["kind"] == "route"})
    # AA-1039 (review S268): the pii-usable models are a registry read (sync, cached): once, off the event loop, and
    # only when a changed stage is pii.
    pii_usable = None
    if any(data_scope.normalize_pii_class((by_stage.get(s) or {}).get("pii_class")) == data_scope.PII_PERSONAL
           for s in changed_stages):
        from api.routers import admin_llm_ops as ops
        pii_usable = await asyncio.to_thread(ops._pii_usable_models, world.models)
    for stage in changed_stages:
        row = by_stage.get(stage)
        chain = route_versions.chain_of(target["routes"].get(stage)) or []
        if row is not None:
            errors.extend(chain_problems(row, chain, world.models, world.reach, pii_usable))

    acc2_only, per_row = _deployment_maps(world)
    rows_now = _rows_with(world.stages, live["routes"])
    rows_new = _rows_with(world.stages, target["routes"])
    before = set(llm_route_chains.violations(rows_now, world.models, world.reach, acc2_only, per_row))
    after = llm_route_chains.violations(rows_new, world.models, world.reach, acc2_only, per_row)
    for stage, rule, detail in after:
        existing = (stage, rule, detail) in before
        p = _problem(rule, detail, stage=stage, existing=existing)
        (errors if rule in BLOCKING_CHAIN_RULES and not existing else warnings).append(p)

    resolved_now, resolved_new = await asyncio.gather(
        asyncio.to_thread(_resolve_all, rows_now, live["priorities"], live["order_mode"], world.evidence),
        asyncio.to_thread(_resolve_all, rows_new, target["priorities"], target["order_mode"], world.evidence))
    stages_out = []
    serving_changed: set = set()
    for row in rows_new:
        stage = row["stage"]
        if row.get("is_active") is False:
            continue
        now, new = resolved_now.get(stage) or {}, resolved_new.get(stage) or {}
        if now.get("first") != new.get("first"):
            serving_changed.add(stage)
        if new.get("first") is None:
            had = now.get("first") is not None
            p = _problem("no_live_model", "no model of the chain would be called now (every deployment is off, "
                                          "refused or over its cap)", stage=stage, existing=not had)
            (errors if had else warnings).append(p)
        stages_out.append({"stage": stage, "role": row["role"],
                           "chain": route_versions.chain_of(target["routes"].get(stage)),
                           "changed": stage in changed_stages, "first": new.get("first"),
                           "first_before": now.get("first"), "serving_changed": stage in serving_changed,
                           "skipped": list(new.get("skipped") or []),
                           "attempts": [{k: a.get(k) for k in ("model", "scope", "status", "connection", "reason",
                                                               "refused")}
                                        for a in new.get("attempts") or []]})

    # AA-1026 D7 evidence: a writer / judge stage whose chain changes, OR whose first serving model@connection changes
    # (a provider-order change, a preset or the order mode moving it to another deployment), per the $0 resolver.
    # AA-1052 W6: and any stage of the writing path's gated prefixes (change_control.GATED_STAGE_PREFIXES).
    evidence_stages = sorted(s for s in set(changed_stages) | serving_changed
                             if route_evidence.needs_evidence((by_stage.get(s) or {}).get("role"), s))
    return {"valid": not errors, "errors": errors, "warnings": warnings, "unchanged": not changes,
            "diff": changes, "needs_evidence": bool(evidence_stages), "evidence_stages": evidence_stages,
            "version": int(world.settings["current_version"]), "order_mode": target["order_mode"],
            "stages": stages_out, "provider_order": _provider_rows(world, target["priorities"])}


async def preview(pool, *, order_mode: Optional[str] = None, routes: Optional[dict] = None,
                  provider_order: Optional[dict] = None, preset: Optional[str] = None,
                  revert_to: Optional[int] = None) -> dict:
    world, target, errors = await _proposal(pool, order_mode=order_mode, routes=routes, provider_order=provider_order,
                                            preset=preset, revert_to=revert_to)
    out = await preview_world(world, target, errors)
    # AA-1052 W6: the open waves that would refuse this save (409), so the page says it before the admin saves.
    out["frozen_by_waves"] = await _frozen_by(pool, out["evidence_stages"])
    return out


async def _frozen_by(pool, evidence_stages) -> list[dict]:
    """The open rerun waves when the change touches a gated stage (edit_gate), else []. Raises on a read error."""
    if not evidence_stages:
        return []
    from services.quality import edit_gate
    return await edit_gate.read_open_waves(pool)


async def _proposal(pool, *, order_mode=None, routes=None, provider_order=None, preset=None,
                    revert_to=None) -> tuple[_World, dict, list]:
    world = await _world(pool)
    priorities = None
    if preset is not None:
        try:
            changes = preset_changes(preset, world.live, world.models, world.connections)
        except KeyError:
            raise RouteControlRefused(f"unknown preset {preset!r}")
        order_mode, provider_order = changes.get("order_mode"), changes["provider_order"]
    elif revert_to is not None:
        snap = await read_version(pool, revert_to)
        if snap is None:
            raise LookupError(f"no route config version {revert_to}")
        changes = revert_changes(snap["snapshot"])
        order_mode, priorities = changes["order_mode"], changes["priorities"]
        routes = {s: c for s, c in changes["routes"].items() if s in world.by_stage}
    target, errors = build_target(world.live, order_mode=order_mode, routes=routes, provider_order=provider_order,
                                  priorities=priorities, deployments=world.deployments, stages=set(world.by_stage))
    return world, target, errors


# ── the write ────────────────────────────────────────────────────────────────────────────────────────────────────

async def apply(pool, *, actor: str, action: str, expected_version: int, note: Optional[str] = None,
                eval_run_id: Optional[str] = None, break_glass_reason: Optional[str] = None,
                order_mode: Optional[str] = None, routes: Optional[dict] = None,
                provider_order: Optional[dict] = None, preset: Optional[str] = None,
                revert_to: Optional[int] = None) -> dict:
    """Save a proposal (``action`` save), a preset (``preset``) or a revert (``revert_to``) as a new version. Raises
    RouteControlRefused (422: a rule, or evidence missing), RouteControlConflict (409), RouteControlUnavailable (503),
    LookupError (404: no such version)."""
    world, target, shape = await _proposal(pool, order_mode=order_mode, routes=routes, provider_order=provider_order,
                                           preset=preset, revert_to=revert_to)
    result = await preview_world(world, target, shape)
    if result["unchanged"]:
        raise RouteControlRefused("nothing to change: the proposal is the config in force")
    if not result["valid"]:
        msgs = "; ".join(f"{e['stage'] + ': ' if e.get('stage') else ''}{e['message']}" for e in result["errors"][:8])
        raise RouteControlRefused(f"the change breaks a routing rule: {msgs}")
    waves = await _frozen_by(pool, result["evidence_stages"])
    if waves:
        from services.quality import edit_gate
        raise RouteControlFrozen(str(edit_gate.frozen_error("the route config", result["evidence_stages"], waves)))
    evidence = None
    if result["needs_evidence"]:
        try:
            evidence = await route_evidence.require("writer", eval_run_id=eval_run_id,
                                                    break_glass_reason=break_glass_reason)
        except route_evidence.EvidenceRefused as e:
            raise RouteControlRefused(str(e))

    by_stage = world.by_stage
    async with pool.acquire() as conn:
        async with conn.transaction():
            state = await route_versions.begin(conn)
            if state is None:
                raise RouteControlUnavailable("route control is not set up on this server yet (migration 370)")
            if state["before_external"] != expected_version:
                raise RouteControlConflict(
                    f"the route config changed since you opened it (version {state['before_external']} is in force, "
                    f"you edited version {expected_version}): reload and check the preview again")
            # Apply ONLY what the proposal changes against the config the preview read (result["diff"]): a stage or a
            # model the admin did not touch keeps whatever it has now, even if a migration changed it in between
            # (begin() recorded that as an external version).
            live = route_versions.normalize(await route_versions.read_live(conn))
            for change in result["diff"]:
                if change["kind"] == "route" and change["stage"] in by_stage and change["after"]:
                    want = target["routes"][change["stage"]]
                    await conn.execute(
                        "UPDATE shared.llm_role_config SET model_id = $2, fallback_model_ids = $3, updated_at = now(), "
                        "updated_by = $4 WHERE stage = $1",
                        change["stage"], want["model_id"], list(want["fallback_model_ids"]), actor[:128])
                elif change["kind"] == "provider_order":
                    now = live["priorities"].get(change["model"]) or {}
                    for conn_key, p in sorted((target["priorities"].get(change["model"]) or {}).items()):
                        if conn_key in now and now[conn_key] != p:
                            await conn.execute(
                                "UPDATE shared.llm_model_deployment SET priority = $3, updated_at = now(), "
                                "updated_by = $4 WHERE model_key = $1 AND connection_key = $2",
                                change["model"], conn_key, p, actor[:128])
                elif change["kind"] == "order_mode" and change["after"] in ORDER_MODES:
                    await conn.execute("UPDATE shared.llm_route_settings SET order_mode = $1 WHERE singleton",
                                       change["after"])
            version = await route_versions.finish(conn, state, actor=actor,
                                                  action="revert" if revert_to is not None else action,
                                                  note=note, preset=preset, reverted_from=revert_to)
            if version is None:
                raise RouteControlRefused("nothing to change: the proposal is the config in force")
            audit_action = _AUDIT_ACTIONS["revert" if revert_to is not None else action]
            await conn.execute(_AUDIT_SQL, actor[:128], audit_action, str(version["version"]), json.dumps(
                {"version": version["version"], "diff": version["diff"], "note": version.get("note"),
                 "preset": preset, "reverted_from": revert_to, "evidence": evidence}, default=str))
            served = {s["stage"]: s for s in result["stages"]}
            for stage in result["evidence_stages"]:
                await route_evidence.record_break_glass(
                    conn, stage=stage, actor=actor, evidence=evidence,
                    change={"chain": route_versions.chain_of(target["routes"].get(stage)),
                            "before": route_versions.chain_of(live["routes"].get(stage)),
                            "first": (served.get(stage) or {}).get("first"),
                            "first_before": (served.get(stage) or {}).get("first_before"),
                            "route_config_version": version["version"]})
    _drop_caches()
    logger.info("llm_route_config_changed", version=version["version"], action=version["action"], actor=actor,
                changes=len(version["diff"]))
    out = {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in version.items()}
    out["changed_stages"] = sorted({c["stage"] for c in version["diff"] if c["kind"] == "route"})
    out["warnings"] = [w["message"] if not w.get("stage") else f"{w['stage']}: {w['message']}"
                       for w in result["warnings"] if not w.get("existing")]
    return out


def _drop_caches() -> None:
    """This process at once; the other tasks within route_settings' TTL (they see the new version number)."""
    from shared.llm_client import deployments, role_config
    role_config.invalidate()
    deployments.invalidate()
    route_settings.invalidate()


__all__ = ["PRESETS", "RouteControlConflict", "RouteControlRefused", "RouteControlUnavailable", "apply",
           "build_target", "chain_problems", "list_versions", "preset_changes", "preview", "preview_world",
           "read_state", "read_version", "revert_changes"]
