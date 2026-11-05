"""AA-1026 (S267, tech lead: family fallback chains) — the chain rules every stage route keeps, and a $0 resolver that
says which model would serve each stage right now. A report module beside ``llm_availability`` (not on the gateway's
call path: ``shared/llm_client`` never imports it).

Since migration 366 every active stage route is an ordered chain by model family: the stage's primary, then the
working sibling of each family tier on the route (Claude Sonnet 5.5 -> Sonnet 4.6, Claude Haiku 5.5 -> Haiku 4.5,
GPT-6 Luna <-> GPT-5.6 Luna), then (writers) the other Claude tier, then the AWS models of the OTHER failure domain the
route already had (acc2: Nova / Llama, migrations 331-333 / 339), then the vendor API last resort (GPT-6 Luna on the
OpenAI API, a model reached only outside AWS) at the end of a writer route. On a GPT route the same OpenAI API row is
GPT-6 Luna's vendor twin and sits right behind GPT-6 Luna (model first: the same model on acc3, then on its vendor
API, before the next model). Since the S267 policy "AWS preferred, not AWS only" (tech
lead, root ADR 0009 decision 5 amendment, replacing T-3) every active text stage (writer, judge, validate) allows
both data scopes, so the vendor APIs are reachable from every one of them after their AWS deployments; only the
embedding stages stay ``aws_only`` (no vendor embedding deployment exists). The gateway runs a route MODEL FIRST:
every deployment of a model (acc3 by priority, then the same model on its vendor API, e.g. Sonnet 5.5 on the
Anthropic API) before the route's next model; ``LLM_ROUTE_AWS_FIRST=on`` (``client.route_attempts``, default off)
runs every AWS deployment of the route first instead. An ``aws_only`` stage never leaves AWS in either order.

``violations(routes, models, reach)`` (pure) checks the rules a route must keep, whatever an admin or a later
migration does; the unit tests run it on the decided routes (``tests/unit/snapshots/aa1026_routes_after_366.json``)
and ``scripts/llm_ops/route_chain_check.py`` on the live rows:

- ``no_fallback``: an active route with no fallback at all (one refused model stops the stage);
- ``aws_only_text_stage``: an active text stage (role writer, judge or validate) that does not allow ``vendor_direct``
  (S267: AWS preferred, not AWS only; it replaced S263's ``vendor_on_aws_only``, which checked the T-3 rule that kept
  tenant content, TripPlanner and the validate stages inside AWS). The detail names any model reached only outside
  AWS the stage could then never call. An embedding stage stays ``aws_only`` and is not checked;
- ``vendor_not_last``: a model reached only outside AWS is followed by a model reachable inside AWS (the vendor-only
  rung is the suffix of the chain; a model with an AWS AND a vendor API deployment may sit anywhere, S267). Except
  the VENDOR TWIN of the rung right before it (``data_scope.vendor_twins``: the same model on its vendor API as a
  separate catalog row, e.g. ``gpt-6-luna-openai`` right behind ``gpt-6-luna``): it is part of that model's slot in
  the model-first order (tech lead S267 Q2: "openai-direct for a GPT twin if one exists");
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
  The count does not depend on the order (model first or AWS first make the same attempts).

``product_of(stage)`` names the product a stage's spend belongs to (S267, tech lead: ONE Anthropic API key for every
product, the per-product split stays in our ledger): ``catalogue`` (A-series admin pipeline), ``tenant_content``
(T-series), ``tripplanner``, ``booking`` (the supply descriptions Booking shows), ``shared`` (the writing-memory loop
both content lines use), and the ledger ``app`` its rows carry (``shared.llm_call_log.app``, what the admin Spend > By
app page groups by): ``tripplanner`` for the stages TripPlanner's own gateway calls, ``cis`` for every stage the CIS
gateway calls. The CRM has no stage here (its own gateway and ledger, ``crm.llm_call``; the By app page reads it
through ``crm.ops.v1``). ``None`` for a stage no rule knows: the unit test keeps every route stage mapped.

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
# The roles of the text stages that may reach a vendor API after their AWS deployments (S267, tech lead: AWS preferred,
# not AWS only). An embed stage stays aws_only: no vendor embedding deployment exists.
TEXT_ROLES = frozenset({"writer", "judge", "validate"})


# Stage -> product (S267: one Anthropic key, per-product tracking in the ledger). Exact stages first, then prefixes.
_PRODUCT_BY_STAGE: dict[str, str] = {
    "supply_content_writer": "booking",          # AA-835: the hotel / service descriptions Booking shows
    "segment_research": "catalogue",             # AA-642 A2: the admin demand-research loop
    "segment_research_batch": "catalogue",
    "f10_embed": "catalogue",
}
_PRODUCT_BY_PREFIX: tuple[tuple[str, str], ...] = (
    ("tp_", "tripplanner"),
    ("writing_memory_", "shared"),
    ("a0_", "catalogue"), ("a3_", "catalogue"), ("s1_", "catalogue"),
    ("t2_", "tenant_content"), ("t7_", "tenant_content"), ("t8_", "tenant_content"), ("t9_", "tenant_content"),
    ("t10_", "tenant_content"),
)
# The ledger app of a product's rows: TripPlanner's gateway writes 'tripplanner', the CIS gateway call_log.APP 'cis'.
_LEDGER_APP = {"tripplanner": "tripplanner"}


def product_of(stage: str) -> Optional[str]:
    """The product a stage's spend belongs to (see the module docstring), or None for a stage no rule knows."""
    if stage in _PRODUCT_BY_STAGE:
        return _PRODUCT_BY_STAGE[stage]
    return next((prod for prefix, prod in _PRODUCT_BY_PREFIX if stage.startswith(prefix)), None)


def ledger_app(stage: str) -> Optional[str]:
    """The ``shared.llm_call_log.app`` value a stage's rows carry (the Spend > By app row), None when unmapped."""
    prod = product_of(stage)
    return None if prod is None else _LEDGER_APP.get(prod, "cis")


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
    models = list(models)
    by_key = {m.model_key: m for m in models}
    outside = data_scope.outside_aws_only(reach)
    twins = data_scope.vendor_twins(models, reach)
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
        if role in TEXT_ROLES and data_scope.VENDOR_DIRECT not in scopes:
            bad = [k for k in keys if k in outside]
            out.append((stage, "aws_only_text_stage",
                        f"allowed_scopes {', '.join(scopes)}" + (f"; never called: {', '.join(bad)}" if bad else "")))
        seen_vendor = None
        for i, k in enumerate(keys):
            if k in outside:
                if not data_scope.in_twin_slot(keys, i, twins):      # a twin right behind its AWS model: its slot
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


def resolve_stage(cfg, evidence=None) -> dict:
    """The gateway's attempts for one stage config (``role_config.StageConfig``) without calling a model: each attempt
    ``{"model", "scope", "status", "connection"|"reason"}`` in order, and ``first`` = "<model>@<connection>" of the
    first deployment that would SERVE the stage now (None when none would). Reads the registry, the catalog and the
    breaker state (cached, read-only). Never raises: a failure is reported in ``error``.

    AA-1042 (S268 follow-up): "would serve", not "would be called". The gateway's plan (``deployment_plan``) already
    leaves out a switched-off deployment or connection, a role or scope the stage may not use, an open circuit and a
    budget stop; ``evidence`` (``stage_coverage.RefusalEvidence``, read once by the caller) adds the deployments the
    account refuses (``stage_coverage.refusal``: the probe state of CIS 367, the breaker's refusals, the connection's
    access state; the rule of the Stage coverage card). The gateway still tries such a deployment when its circuit is
    half-open (one call per breaker period, its own recovery check), gets the refusal and moves on, so the request is
    served by the next one: that one is ``first``. A planned deployment that is refused stays in the attempt's
    ``deployments`` and is listed in its ``refused`` ({connection, reason}); an attempt whose every deployment is
    refused has ``status`` "refused". ``skipped`` lists, in order, every model@connection before ``first`` that will
    not serve it, with the reason (catalog off, plan skip, refused). ``called_first`` keeps the old answer: the first
    model@connection the gateway would call. Without ``evidence`` nothing counts as refused (the old behaviour)."""
    from shared.llm_client.catalog import LEGACY_KEYS, get_model_sync
    from shared.llm_client.client import (_STAGE_SCOPES, LLMClient, _row_role, deployment_routing_enabled,
                                          legacy_aliases_enabled, route_attempts)
    from shared.llm_client.models import LLMRequest
    from shared.llm_client.vendor_route import deployment_plan

    stage = cfg.stage
    scopes = data_scope.normalize(getattr(cfg, "allowed_scopes", None))
    role = _row_role(cfg)
    pii = data_scope.normalize_pii_class(getattr(cfg, "pii_class", None))     # AA-1039: content | pii (CIS 373)
    chain = [cfg.model_id, *(cfg.fallback_model_ids or ())]
    out = {"stage": stage, "role": cfg.role, "scopes": list(scopes) if scopes else None, "pii_class": pii,
           "chain": chain, "attempts": [], "first": None, "called_first": None, "skipped": []}
    token = _STAGE_SCOPES.set((stage, scopes, role, pii))

    def skipped(key: str, connection: Optional[str], reason: str) -> None:
        if out["first"] is None:
            out["skipped"].append({"model": key, "connection": connection, "reason": reason})

    try:
        request = LLMRequest(system_prompt="route check", user_prompt="route check", stage=stage)
        for key, narrowed in route_attempts(request, chain):
            step = {"model": key, "scope": (narrowed or ("stage",))[0]}
            out["attempts"].append(step)
            if key in LEGACY_KEYS and pii == data_scope.PII_PERSONAL:
                # AA-1039 (review S268): as LLMClient._legacy_call does for a pii stage: the alias targets planned with
                # the class (LLM_LEGACY_ALIASES on and an alias the registry describes), else refused (the
                # hard-coded chain calls global. profiles; its GPT-4.1 last resort is a vendor API).
                steps = (LLMClient._alias_steps(key, narrowed or scopes, role=role, pii_class=pii)
                         if legacy_aliases_enabled() else None)
                if steps is None:
                    step.update(status="skip",
                                reason="legacy key: a personal-data stage never takes the hard-coded chain")
                    skipped(key, None, step["reason"])
                    continue
                pairs = [(m.model_key, t.connection.connection_key) for m, t in steps]
                if not pairs:
                    step.update(status="skip", reason=("legacy key: no alias target has a deployment a personal-data "
                                                       "stage may use"))
                    skipped(key, None, step["reason"])
                    continue
                refused = [{"model": mk, "connection": ck, "reason": why} for mk, ck in pairs
                           if evidence is not None and (why := evidence.refused(mk, ck)) is not None]
                refused_set = {(r["model"], r["connection"]) for r in refused}
                serving = [p for p in pairs if p not in refused_set]
                out["called_first"] = out["called_first"] or f"{pairs[0][0]}@{pairs[0][1]}"
                for r in refused:
                    if not serving or pairs.index((r["model"], r["connection"])) < pairs.index(serving[0]):
                        skipped(r["model"], r["connection"], r["reason"])
                step["deployments"] = [f"{mk}@{ck}" for mk, ck in pairs]
                if serving:
                    step.update(status="call", connection=serving[0][1], alias_model=serving[0][0])
                    out["first"] = out["first"] or f"{serving[0][0]}@{serving[0][1]}"
                else:
                    step.update(status="refused", reason="refused: " + ", ".join(
                        f"{r['model']}@{r['connection']} ({r['reason']})" for r in refused))
                if refused:
                    step["refused"] = refused
                continue
            if key in LEGACY_KEYS:
                step.update(status="legacy", reason="legacy key: the hard-coded acc3 chain (not planned here)")
                out["first"] = out["first"] or f"{key}@acc3 (legacy chain)"
                out["called_first"] = out["called_first"] or out["first"]
                continue
            model = get_model_sync(key)
            if model is None or not model.enabled:
                step.update(status="skip", reason="not in the catalog" if model is None else "disabled in the catalog")
                skipped(key, None, step["reason"])
                continue
            plan = (deployment_plan(model, narrowed or scopes, role=role, pii_class=pii)
                    if deployment_routing_enabled() else None)
            if not plan:
                step.update(status="skip", reason=_skip_reason(plan))
                skipped(key, None, step["reason"])
                continue
            conns = [t.connection.connection_key for t in plan]
            out["called_first"] = out["called_first"] or f"{key}@{conns[0]}"
            refused = []
            for ck in conns:
                why = evidence.refused(key, ck) if evidence is not None else None
                if why is not None:
                    refused.append({"connection": ck, "reason": why})
            serving = [ck for ck in conns if ck not in {r["connection"] for r in refused}]
            for r in refused:
                if not serving or conns.index(r["connection"]) < conns.index(serving[0]):
                    skipped(key, r["connection"], r["reason"])
            if serving:
                step.update(status="call", connection=serving[0], deployments=conns)
                out["first"] = out["first"] or f"{key}@{serving[0]}"
            else:
                step.update(status="refused", deployments=conns,
                            reason="refused: " + ", ".join(f"{r['connection']} ({r['reason']})" for r in refused))
            if refused:
                step["refused"] = refused
    except Exception as e:  # noqa: BLE001 — a report, never a failure
        out["error"] = f"{type(e).__name__}: {str(e)[:200]}"
    finally:
        _STAGE_SCOPES.reset(token)
    return out


__all__ = ["CHECK_PAIRS", "TEXT_ROLES", "ledger_app", "product_of", "resolve_stage", "violations"]
