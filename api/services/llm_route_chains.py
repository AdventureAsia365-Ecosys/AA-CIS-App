"""AA-1026 (S267, tech lead: family fallback chains) — the chain rules every stage route keeps, and a $0 resolver that
says which model would serve each stage right now. A report module beside ``llm_availability`` (not on the gateway's
call path: ``shared/llm_client`` never imports it).

Since migration 366 every active stage route is an ordered chain by model family: the stage's primary, then the
working sibling of each family tier on the route (Claude Sonnet 5.5 -> Sonnet 4.6, Claude Haiku 5.5 -> Haiku 4.5,
GPT-6 Luna <-> GPT-5.6 Luna), then (writers) the other Claude tier, then the AWS models of the OTHER failure domain the
route already had (acc2: Nova / Llama, migrations 331-333 / 339), then the vendor API last resort (GPT-6 Luna on the
OpenAI API) only on a stage allowed ``vendor_direct``. The gateway runs a ``vendor_direct`` route on AWS first across
all its models (``client.route_attempts``), so a vendor API outside AWS is called only after every AWS deployment.

``violations(routes, models, reach)`` (pure) checks the rules a route must keep, whatever an admin or a later
migration does; the unit tests run it on the decided routes (``tests/unit/snapshots/aa1026_routes_after_366.json``)
and ``scripts/llm_ops/route_chain_check.py`` on the live rows:

- ``no_fallback``: an active route with no fallback at all (one refused model stops the stage);
- ``vendor_on_aws_only``: an ``aws_only`` stage names a model reached only outside AWS;
- ``vendor_not_last``: a model reached only outside AWS is followed by a model reachable inside AWS (the vendor API
  rung is the suffix of the chain);
- ``judge_writer_family``: a judge route names a model of the writers' family (``family_rule.WRITER_FAMILY``, ADR 0009
  decision 12 / TL-10: a judge never scores text of its own family);
- ``writer_family``: a writer route names a model of another family, except a model reached only outside AWS at the
  end of a ``vendor_direct`` writer (the T-3 last resort of migrations 327 / 344);
- ``writer_acc2``: a writer route names a chat model served only by connection acc2 (ADR 0009 decision 14);
- ``check_pair_family``: a check stage shares a model family with the stage whose output it checks (``CHECK_PAIRS``:
  the place card and dedupe pairs, kept on disjoint makers by migrations 331 / 339);
- ``attempt_cap`` (when ``deployment_scopes`` is given): the route could make more provider attempts than the gateway
  starts per request (``errors.MAX_CHAIN_ATTEMPTS``, ADR 0009 decision 5), counting every deployment of every model
  in the stage's scopes (the Anthropic API included, on or off): its last rungs (the OpenAI API) would never run.

``resolve_stage(...)`` walks a stage the way the gateway would (``client.route_attempts`` for the order and the
pass scopes, ``vendor_route.deployment_plan`` for the deployments a model would be called on now: enabled, connection
on, role served, data scope allowed, circuit not open, budget not reached) WITHOUT calling any model, and returns the
first (model, connection) that would be called, plus why each earlier attempt is skipped. The first entry is "would be
tried first", not "will answer": a closed circuit says nothing about a refusal that has not happened yet.
"""
from __future__ import annotations

from typing import Iterable, Optional

from shared.llm_client import data_scope, family_rule
from shared.llm_client.errors import MAX_CHAIN_ATTEMPTS

# A check stage -> the stage whose output it checks: their routes keep disjoint model families (migrations 331, 339).
CHECK_PAIRS: dict[str, str] = {
    "a3_place_card_check": "a3_place_card",
    "a3_place_dedupe_check": "a3_place_dedupe",
}
ACC2_CONNECTION = "acc2"


def _family(models: dict, key: Optional[str]) -> Optional[str]:
    m = models.get(key) if key else None
    return getattr(m, "family_key", None) if m is not None else None


def _keys(route: dict) -> list[str]:
    return [k for k in [route.get("model_id"), *(route.get("fallback_model_ids") or [])] if k]


def violations(routes: Iterable[dict], models: Iterable, reach: dict[str, frozenset],
               acc2_only: Iterable[str] = (),
               deployment_scopes: Optional[dict[str, list[str]]] = None) -> list[tuple[str, str, str]]:
    """Pure: ``(stage, rule, detail)`` for every broken chain rule (see the module docstring). ``routes``: rows of
    ``shared.llm_role_config`` (stage, role, model_id, fallback_model_ids, allowed_scopes, is_active); ``models``:
    catalog rows with ``model_key`` / ``family_key`` / ``api_style``; ``reach``: model key -> the data scopes of the
    connections its deployments are on (every row, enabled or not: the policy, as the admin counts it); ``acc2_only``:
    the chat model keys whose every deployment is on connection acc2; ``deployment_scopes``: model key -> the data
    scope of each of its deployments (one entry per row, enabled or not; a model without rows counts as one AWS
    deployment). A switched-off route (``is_active`` false) is not checked."""
    by_key = {m.model_key: m for m in models}
    outside = data_scope.outside_aws_only(reach)
    acc2_only = set(acc2_only)
    rows = {r["stage"]: r for r in routes}
    out: list[tuple[str, str, str]] = []
    for stage in sorted(rows):
        r = rows[stage]
        if r.get("is_active") is False:
            continue
        keys = _keys(r)
        scopes = data_scope.normalize(r.get("allowed_scopes")) or data_scope.AWS_ONLY_SCOPES
        role = r.get("role")
        if not (r.get("fallback_model_ids") or []):
            out.append((stage, "no_fallback", f"{r.get('model_id')} has no fallback"))
        if data_scope.VENDOR_DIRECT not in scopes:
            bad = [k for k in keys if k in outside]
            if bad:
                out.append((stage, "vendor_on_aws_only", ", ".join(bad)))
        seen_vendor = None
        for k in keys:
            if k in outside:
                seen_vendor = seen_vendor or k
            elif seen_vendor:
                out.append((stage, "vendor_not_last", f"{k} after {seen_vendor}"))
                break
        if role == "judge":
            judged = keys + [r.get("shadow_model_id")]
            bad = [k for k in judged if k and _family(by_key, k) == family_rule.WRITER_FAMILY]
            if bad:
                out.append((stage, "judge_writer_family", ", ".join(bad)))
        if role == "writer":
            last_resort = keys[-1] if (data_scope.VENDOR_DIRECT in scopes and keys and keys[-1] in outside) else None
            bad = [k for k in keys if k != last_resort and _family(by_key, k) not in (None, family_rule.WRITER_FAMILY)]
            if bad:
                out.append((stage, "writer_family", ", ".join(bad)))
            bad = [k for k in keys if k in acc2_only]
            if bad:
                out.append((stage, "writer_acc2", ", ".join(bad)))
        if deployment_scopes is not None:
            attempts = sum(len([sc for sc in deployment_scopes.get(k) or [data_scope.AWS_ONLY] if sc in scopes])
                           for k in dict.fromkeys(keys))
            if attempts > MAX_CHAIN_ATTEMPTS:
                out.append((stage, "attempt_cap", f"{attempts} possible attempts > {MAX_CHAIN_ATTEMPTS}"))
        producer = rows.get(CHECK_PAIRS.get(stage, ""))
        if producer is not None and producer.get("is_active") is not False:
            mine = {_family(by_key, k) for k in keys} - {None}
            theirs = {_family(by_key, k) for k in _keys(producer)} - {None}
            common = sorted(mine & theirs)
            if common:
                out.append((stage, "check_pair_family", f"shares {', '.join(common)} with {producer['stage']}"))
    return out


# ── the $0 resolver (live registry, no model call) ──────────────────────────────────────────────────────────────────

def _skip_reason(plan) -> str:
    if plan is None:
        return "registry does not describe it (today's path)"
    if getattr(plan, "circuit_skips", None):
        return "circuit open: " + ", ".join(f"{c} ({cls or '?'})" for c, cls in plan.circuit_skips)
    if getattr(plan, "budget_skips", None):
        return "budget reached: " + ", ".join(c for c, _ in plan.budget_skips)
    if getattr(plan, "role_skips", None):
        return "role not served: " + ", ".join(c for c, _ in plan.role_skips)
    return "no enabled deployment in this scope"


def resolve_stage(cfg) -> dict:
    """The gateway's attempts for one stage config (``role_config.StageConfig``) without calling a model: each attempt
    ``{"model", "scope", "status", "connection"|"reason"}`` in order, and ``first`` = "<model>@<connection>" of the
    first attempt that has a callable deployment now (None when nothing would be called). Reads the registry, the
    catalog and the breaker state (cached, read-only). Never raises: a failure is reported in ``error``."""
    from shared.llm_client.catalog import LEGACY_KEYS, get_model_sync
    from shared.llm_client.client import _STAGE_SCOPES, _row_role, deployment_routing_enabled, route_attempts
    from shared.llm_client.models import LLMRequest
    from shared.llm_client.vendor_route import deployment_plan

    stage = cfg.stage
    scopes = data_scope.normalize(getattr(cfg, "allowed_scopes", None))
    role = _row_role(cfg)
    chain = [cfg.model_id, *(cfg.fallback_model_ids or ())]
    out = {"stage": stage, "role": cfg.role, "scopes": list(scopes) if scopes else None, "chain": chain,
           "attempts": [], "first": None}
    token = _STAGE_SCOPES.set((stage, scopes, role))
    try:
        request = LLMRequest(system_prompt="route check", user_prompt="route check", stage=stage)
        for key, narrowed in route_attempts(request, chain):
            step = {"model": key, "scope": (narrowed or ("stage",))[0]}
            out["attempts"].append(step)
            if key in LEGACY_KEYS:
                step.update(status="legacy", reason="legacy key: the hard-coded acc3 chain (not planned here)")
                out["first"] = out["first"] or f"{key}@acc3 (legacy chain)"
                continue
            model = get_model_sync(key)
            if model is None or not model.enabled:
                step.update(status="skip", reason="not in the catalog" if model is None else "disabled in the catalog")
                continue
            plan = deployment_plan(model, narrowed or scopes, role=role) if deployment_routing_enabled() else None
            if plan:
                conns = [t.connection.connection_key for t in plan]
                step.update(status="call", connection=conns[0], deployments=conns)
                out["first"] = out["first"] or f"{key}@{conns[0]}"
            else:
                step.update(status="skip", reason=_skip_reason(plan))
    except Exception as e:  # noqa: BLE001 — a report, never a failure
        out["error"] = f"{type(e).__name__}: {str(e)[:200]}"
    finally:
        _STAGE_SCOPES.reset(token)
    return out


__all__ = ["CHECK_PAIRS", "resolve_stage", "violations"]
