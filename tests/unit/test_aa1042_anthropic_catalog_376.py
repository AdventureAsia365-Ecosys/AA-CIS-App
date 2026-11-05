"""AA-1042 follow-up (tech lead S269: "Fable 5.1, Fable 5, Opus 5, Opus 4.8, 4.7, 4.6, 4.5, Sonnet 4.6, Haiku 4.5: put
them in the catalog as fallbacks") — the registry CIS migration 376 leaves, checked with the chain rules and the admin
route control, without a database or a provider.

The registry: the 366 test's catalog (tests/unit/test_aa1026_family_fallback_chains.py) plus 376's rows (Fable 5.1 /
5, Opus 5 / 4.8 / 4.7 reached only by the Anthropic API; the Opus 4.6 / 4.5 candidates of 367 switched on, their acc3
rows still off) and nine anthropic-direct deployments at priority 100 (Sonnet 4.6 and Haiku 4.5 included). The routes:
``snapshots/aa1042_routes_after_376.json`` (tests/integration/test_aa1042_migration_376.py proves the migration
produces it on real Postgres).

What this proves:
- every active route keeps the chain rules (``llm_route_chains.violations``), the six attempts per request included;
- the 366 routes on the post-376 registry would break ONLY the attempt cap, on exactly the 20 stages 376 changes (the
  reason for each change), and each new chain is its 366 chain minus rungs, order kept;
- the API-only rows are nobody's vendor twin and sit on no chain; no stage loses its last live model;
- the route control (``llm_route_control``): the prefer_anthropic_api preset in force since S269 (route version 4)
  still validates on the new registry and would move only the four new two-deployment models API-first; the Prefer
  AWS preset validates; a revert to the 366 chains is refused by the attempt cap.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import time
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.unit import aa1002_s3_route_capture as rc
from tests.unit.test_aa1026_family_fallback_chains import REFUSED_ON_ACC3, _model_rows

SNAPSHOTS = Path(__file__).resolve().parent / "snapshots"
BEFORE = SNAPSHOTS / "aa1026_routes_after_366.json"
AFTER = SNAPSHOTS / "aa1042_routes_after_376.json"
OPENAI_HOP = "gpt-6-luna-openai"
# 376's catalog rows reached only by the Anthropic API: key -> (label, API model id, in, out)
API_ONLY = {
    "fable-5-1": ("Claude Fable 5.1", "claude-fable-5-1", 10.0, 50.0),
    "fable-5": ("Claude Fable 5", "claude-fable-5", 10.0, 50.0),
    "opus-5": ("Claude Opus 5", "claude-opus-5", 5.0, 25.0),
    "opus-4-8": ("Claude Opus 4.8", "claude-opus-4-8", 5.0, 25.0),
    "opus-4-7": ("Claude Opus 4.7", "claude-opus-4-7", 5.0, 25.0),
}
# 367's candidates, switched on by 376 (their acc3 deployment stays disabled)
CANDIDATES = {
    "opus-4-6": ("Claude Opus 4.6", "global.anthropic.claude-opus-4-6-v1", "claude-opus-4-6"),
    "opus-4-5": ("Claude Opus 4.5", "global.anthropic.claude-opus-4-5-20251101-v1:0", "claude-opus-4-5-20251101"),
}
API_WIRES = {**{k: v[1] for k, v in API_ONLY.items()}, **{k: v[2] for k, v in CANDIDATES.items()},
             "sonnet-4-6": "claude-sonnet-4-6", "haiku-4-5": "claude-haiku-4-5-20251001"}
# stage -> 376's fallbacks (the migration's table)
DECIDED = {
    **{s: ["sonnet-4-6", OPENAI_HOP] for s in ("s1_generate", "supply_content_writer", "t2_generate", "t8_angle_gen",
                                               "t9_write")},
    "tp_compose": ["sonnet-4-6", "haiku-4-5"],
    **{s: ["haiku-4-5", OPENAI_HOP] for s in ("a0_column_map", "s1_flag_fix", "s1_itinerary_nudge", "t2_flag_fix",
                                              "t2_itinerary_nudge", "t9_repair", "segment_research",
                                              "segment_research_batch")},
    "a3_place_card_check": ["haiku-4-5", "llama4-scout"],
    "a3_place_days": ["haiku-4-5", "nova-lite"],
    "a3_place_locate": ["sonnet-4-6", "nova-lite"],
    "t9_faq_answer_fit": ["sonnet-4-6", "nova-lite"],
    "a3_atomize": [OPENAI_HOP, "haiku-5-5", "nova-lite"],
    "s1_source_facts": [OPENAI_HOP, "haiku-5-5", "nova-lite"],
}
_SEED = rc.seed_deployments                    # the 304 / 306 seed (patched below with the 376 rows)
V4_API_FIRST = ("haiku-5-5", "sonnet-5", "sonnet-5-5", "opus-5-5")     # route version 4 (S269 preset)


def _routes(path: Path) -> dict:
    return {r["stage"]: r for r in json.loads(path.read_text(encoding="utf-8"))["routes"]}


def _rows_376() -> list[dict]:
    rows = list(_model_rows())
    base = {"blocked_reason": None, "callable_via": ["llm_client"], "enabled": True, "max_output_tokens": 128000,
            "price_cache_read_per_mtok": None, "price_cache_write_per_mtok": None, "supports_temperature": False,
            "vendor": "anthropic", "family": "anthropic"}
    for key, (label, wire, p_in, p_out) in API_ONLY.items():
        rows.append({**base, "model_key": key, "label": label, "provider": "anthropic",
                     "api_style": "anthropic_messages", "bedrock_profile_ids": {}, "wire_model": wire,
                     "price_in_per_mtok": p_in, "price_out_per_mtok": p_out})
    for key, (label, profile, _) in CANDIDATES.items():
        rows.append({**base, "model_key": key, "label": label, "provider": "bedrock", "api_style": "converse",
                     "bedrock_profile_ids": {"acc3": profile}, "wire_model": None, "max_output_tokens": None,
                     "price_in_per_mtok": 5.0, "price_out_per_mtok": 25.0})
    return rows


def _models() -> dict:
    return rc.catalog_models({"models": _rows_376()})


def _deployments(models: dict) -> list:
    """The seed rows (304 / 306 / 324 / 366: Sonnet 4.6 / Haiku 4.5 on acc3 only, 367: the candidates' acc3 rows off)
    plus 376's nine Anthropic API rows."""
    from shared.llm_client.deployments import ModelDeployment
    out = []
    for d in _SEED(models):
        if d.model_key in API_WIRES and d.connection_key != "acc3":
            continue                                # 306's derivation never ran for them on Dev: 376 names them
        if d.model_key in ("nova-lite", "nova-2-lite", "llama4-maverick", "llama4-scout", "kimi-k3"):
            d = dataclasses.replace(d, enabled=True)    # 324's acc2 rows, enabled by 331 / 332 / 333 / 339
        if d.model_key in CANDIDATES:
            d = dataclasses.replace(d, enabled=False, origin="seed")
        out.append(d)
    out += [ModelDeployment(k, "anthropic-direct", w, 100, True, "seed") for k, w in API_WIRES.items()]
    return out


def _registry():
    models = _models()
    conns = rc.seed_connections(anthropic_enabled=True)
    deps = _deployments(models)
    reach: dict = {}
    scopes: dict = {}
    where: dict = {}
    for d in deps:
        reach.setdefault(d.model_key, set()).add(conns[d.connection_key].data_scope)
        scopes.setdefault(d.model_key, []).append(conns[d.connection_key].data_scope)
        where.setdefault(d.model_key, set()).add(d.connection_key)
    acc2_only = {k for k, c in where.items() if c == {"acc2"} and models[k].api_style != "embed"}
    return models, conns, deps, {k: frozenset(v) for k, v in reach.items()}, scopes, acc2_only


def _violations(routes: dict) -> list:
    from api.services.llm_route_chains import violations
    models, _, _, reach, scopes, acc2_only = _registry()
    return violations(routes.values(), models.values(), reach, acc2_only, scopes)


# ── the decided table ──────────────────────────────────────────────────────────────────────────────────────────────

def test_only_the_decided_fallbacks_change():
    before, after = _routes(BEFORE), _routes(AFTER)
    assert set(before) == set(after)
    for stage, r in after.items():
        assert r == {**before[stage], "fallback_model_ids": DECIDED.get(stage, before[stage]["fallback_model_ids"])}
    assert len(DECIDED) == 20


def test_each_new_chain_is_its_366_chain_minus_rungs_in_the_same_order():
    before = _routes(BEFORE)
    for stage, new in DECIDED.items():
        old = before[stage]["fallback_model_ids"]
        assert [k for k in old if k in new] == new and len(new) < len(old), stage
        if stage != "tp_compose" and old[-1] == OPENAI_HOP:
            assert new[-1] == OPENAI_HOP, stage           # the other-maker last rung of a writer stays (ADR 0009)
        if old[-1] in ("nova-lite", "llama4-scout"):
            assert new[-1] == old[-1], stage              # the acc2 last rung of a validate stage stays
    assert DECIDED["tp_compose"] == ["sonnet-4-6", "haiku-4-5"]   # TripPlanner's gateway calls Bedrock only


def test_the_api_only_rows_and_sonnet_5_sit_on_no_chain():
    for r in _routes(AFTER).values():
        keys = [r["model_id"], *r["fallback_model_ids"], r["shadow_model_id"]]
        assert not set(keys) & (set(API_ONLY) | set(CANDIDATES) | {"sonnet-5"}), r["stage"]


# ── the chain rules ────────────────────────────────────────────────────────────────────────────────────────────────

def test_every_route_keeps_the_chain_rules_after_376():
    assert _violations(_routes(AFTER)) == []


def test_the_366_routes_on_the_376_registry_break_only_the_attempt_cap_of_the_decided_stages():
    found = _violations(_routes(BEFORE))
    assert {rule for _, rule, _ in found} == {"attempt_cap"}
    assert {stage for stage, _, _ in found} == set(DECIDED)
    assert all(detail.startswith("7 possible attempts") for _, _, detail in found)


def test_the_new_rows_are_nobodys_vendor_twin():
    from shared.llm_client import data_scope
    models, _, _, reach, _, _ = _registry()
    assert data_scope.vendor_twins(models.values(), reach) == {OPENAI_HOP: frozenset({"gpt-6-luna"})}
    assert data_scope.outside_aws_only(reach) >= set(API_ONLY)
    assert not set(CANDIDATES) & data_scope.outside_aws_only(reach)     # their (disabled) acc3 row counts as AWS


def test_the_anthropic_adapter_knows_the_effort_of_every_new_wire_id_but_opus_4_5():
    """Opus 4.5's family is not in the adapter's table: its deployment states effort high (request_defaults)."""
    from shared.llm_client.adapters.anthropic_messages import _effort_facts, resolve_effort
    for key, wire in API_WIRES.items():
        known, _ = _effort_facts(wire)
        assert known is (key != "opus-4-5"), key
    assert resolve_effort("claude-opus-4-5-20251101", "s1_generate", {"effort": "high"}) == "high"
    assert resolve_effort("claude-haiku-4-5-20251001", "s1_generate", {}) is None     # Haiku 4.5 takes no effort


# ── the route control (api/services/llm_route_control.py) ──────────────────────────────────────────────────────────

def _live(routes: dict, deps: list, api_first=V4_API_FIRST) -> dict:
    """A route config snapshot: the routes, the 376 priorities, the v4 preset's order on its four models."""
    prios: dict = {}
    for d in deps:
        prios.setdefault(d.model_key, {})[d.connection_key] = d.priority
    for key in api_first:
        prios[key] = {"anthropic-direct": 10, "acc3": 20}
    return {"order_mode": "model_first",
            "routes": {s: {"model_id": r["model_id"], "fallback_model_ids": list(r["fallback_model_ids"])}
                       for s, r in routes.items()},
            "priorities": prios}


def _world(live: dict):
    from api.services import llm_route_control as rcl
    models, conns, deps, reach, _, _ = _registry()
    stages = [{**r, "pii_class": "content"} for r in _routes(AFTER).values()]
    return rcl._World(
        settings={"current_version": 5, "order_mode": live["order_mode"]}, live=live, stages=stages,
        models=list(models.values()),
        deployments=[{"model_key": d.model_key, "connection_key": d.connection_key, "priority": d.priority,
                      "enabled": d.enabled, "access_state": "unknown"} for d in deps],
        connections={k: {"connection_key": k, "kind": c.kind, "label": c.label, "data_scope": c.data_scope,
                         "enabled": c.enabled} for k, c in conns.items()},
        reach=reach)


@contextmanager
def _gateway_registry():
    """The gateway's registry reads served from the 376 registry (Anthropic API on), acc3 refusing the 5.x / GPT rows
    (open circuits), so the preview's $0 resolver walks each stage as the gateway would."""
    from shared.llm_client import catalog, connections, vendor_route
    models = _models()
    rc._reset_registry()
    catalog._models = dict(models)
    catalog._loaded_at, catalog._failed_at = time.monotonic(), 0.0
    try:
        with patch.object(rc, "seed_deployments", _deployments), \
                patch.object(connections, "run_sync", rc._registry_fetch("seed_anthropic_on", models)), \
                patch.object(vendor_route, "circuit_open",
                             lambda conn, model: conn == "acc3" and model in REFUSED_ON_ACC3), \
                patch.object(vendor_route, "connection_circuit_open", lambda conn: None), \
                patch("shared.llm_client.breaker.open_class", return_value="access_denied"):
            yield
    finally:
        catalog.invalidate()
        catalog._models = {}
        rc._reset_registry()


def _preview(live: dict, **proposal) -> dict:
    from api.services import llm_route_control as rcl
    world = _world(live)
    if "preset" in proposal:
        changes = rcl.preset_changes(proposal["preset"], live, world.models, world.connections)
        proposal = {"order_mode": changes.get("order_mode"), "provider_order": changes["provider_order"]}
    target, errors = rcl.build_target(live, deployments=world.deployments, stages=set(world.by_stage), **proposal)
    with _gateway_registry():
        return asyncio.run(rcl.preview_world(world, target, errors))


def test_the_prefer_anthropic_api_preset_moves_only_the_four_new_two_deployment_models():
    from api.services import llm_route_control as rcl
    models, conns, deps, _, _, _ = _registry()
    connections = {k: {"kind": c.kind, "data_scope": c.data_scope} for k, c in conns.items()}
    out = rcl.preset_changes("prefer_anthropic_api", _live(_routes(AFTER), deps), list(models.values()), connections)
    assert out == {"order_mode": "model_first",
                   "provider_order": {k: ["anthropic-direct", "acc3"]
                                      for k in ("haiku-4-5", "opus-4-5", "opus-4-6", "sonnet-4-6")}}
    # before 376 (route version 4 in force) the preset had nothing left to move
    deps_366 = [d for d in deps if not (d.connection_key == "anthropic-direct" and d.model_key in API_WIRES)]
    assert rcl.preset_changes("prefer_anthropic_api", _live(_routes(BEFORE), deps_366), list(models.values()),
                              connections) == {"order_mode": "model_first", "provider_order": {}}


@pytest.mark.parametrize("preset", ["prefer_anthropic_api", "prefer_aws"])
def test_each_preset_still_validates_and_every_stage_keeps_a_serving_model(preset):
    _, _, deps, _, _, _ = _registry()
    out = _preview(_live(_routes(AFTER), deps), preset=preset)
    assert out["valid"] is True and out["errors"] == [], out["errors"]
    assert not [w for w in out["warnings"] if not w.get("existing")], out["warnings"]
    served = {s["stage"]: s["first"] for s in out["stages"]}
    assert served and all(served.values()), served
    if preset == "prefer_anthropic_api":
        assert served["s1_generate"] == "sonnet-5-5@anthropic-direct"
        assert served["a3_atomize"] == f"{OPENAI_HOP}@openai-direct"     # GPT-6 Luna refused on acc3: its twin


def test_the_live_config_after_376_serves_every_stage():
    _, _, deps, _, _, _ = _registry()
    out = _preview(_live(_routes(AFTER), deps))
    assert out["valid"] is True and out["unchanged"] is True
    assert all(s["first"] for s in out["stages"]), [s["stage"] for s in out["stages"] if not s["first"]]


def test_a_revert_to_the_366_chains_is_refused_by_the_attempt_cap():
    """After 376 the S269 "revert to version 3" would bring the 366 chains back on the new registry: refused; the
    Prefer AWS preset is the way back to AWS first."""
    _, _, deps, _, _, _ = _registry()
    live = _live(_routes(AFTER), deps)
    out = _preview(live, routes={s: [r["model_id"], *r["fallback_model_ids"]] for s, r in _routes(BEFORE).items()})
    assert out["valid"] is False
    assert {e["stage"] for e in out["errors"] if e["rule"] == "attempt_cap"} == set(DECIDED)


# ── the daily probe (services/jobs/llm_probe_job.py) ───────────────────────────────────────────────────────────────

def test_the_daily_probe_plans_every_enabled_deployment_after_376():
    """Review fix (S269): 376's nine API rows bring the enabled deployments on enabled connections to 33; at the old
    cap of 30 the probe skipped the last anthropic-direct row and both openai-direct rows (the last-resort writer
    rung) as over_limit, and their access_state went stale. The companion PR raised MAX_PROBES to 50."""
    from api.services.llm_connections import _testable
    from services.jobs import llm_probe_job as probe
    _, conns, deps, _, _, _ = _registry()
    with _gateway_registry():
        plan = probe.build_plan(conns, deps, _testable, probe._embeddable)
    over = [(s["connection_key"], s["model_key"]) for s in plan["skipped"] if s["why"].startswith("over_limit")]
    assert over == []
    planned = {(p["connection_key"], p["model_key"]) for p in plan["probes"]}
    assert {("anthropic-direct", k) for k in API_WIRES} <= planned
    assert ("openai-direct", OPENAI_HOP) in planned
    assert len(plan["probes"]) == 33 and len(plan["probes"]) <= probe.MAX_PROBES - 10     # room for the next rows
    assert plan["estimated_usd"] < 0.03                                                  # the S262 daily bound
