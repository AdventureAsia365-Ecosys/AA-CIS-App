"""AA-1042 (S268, route control center, slice "Admin controls") — the pure parts and the HTTP edge of the route
control: route config diffs (shared/llm_client/route_versions.py), the order mode read (route_settings.py, DB wins,
env var only as the bootstrap default), the preview priority override (deployments.py), proposals, presets, reverts,
the rule check (api/services/llm_route_control.py) and the router (api/routers/admin_llm_route_control.py).

The DB side (migration 370, the trigger, versions, the audit row, revert, presets on real rows) is
tests/integration/test_aa1042_route_control_db.py. Invented stage and model keys only.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.core import staff_authz
from api.routers import admin_ai_gateway
from api.routers import admin_llm_route_control as router_mod
from api.services import llm_route_control as rc
from shared.llm_client import client as client_mod
from shared.llm_client import connections, deployments, route_settings, route_versions
from shared.llm_client.catalog import CatalogModel
from shared.llm_client.deployments import ModelDeployment


def _snap(order="model_first", routes=None, prios=None):
    return {"order_mode": order, "routes": routes or {}, "priorities": prios or {}}


def _route(*chain):
    return {"model_id": chain[0], "fallback_model_ids": list(chain[1:])}


# ── route_versions (pure) ────────────────────────────────────────────────────────────────────────────────────────

def test_diff_lists_order_mode_routes_and_provider_order_in_a_stable_order():
    before = _snap(routes={"zz_b": _route("m1", "m2"), "zz_a": _route("m1")},
                   prios={"m1": {"acc3": 10, "api": 20}})
    after = _snap("aws_first", routes={"zz_b": _route("m2", "m1"), "zz_a": _route("m1"), "zz_c": _route("m3")},
                  prios={"m1": {"acc3": 20, "api": 10}})
    assert route_versions.diff(before, after) == [
        {"kind": "order_mode", "before": "model_first", "after": "aws_first"},
        {"kind": "route", "stage": "zz_b", "before": ["m1", "m2"], "after": ["m2", "m1"]},
        {"kind": "route", "stage": "zz_c", "before": None, "after": ["m3"]},
        {"kind": "provider_order", "model": "m1", "before": ["acc3", "api"], "after": ["api", "acc3"]},
    ]


def test_renumbered_priorities_in_the_same_order_are_no_change():
    a = _snap(prios={"m1": {"acc3": 10, "api": 20}})
    b = _snap(prios={"m1": {"acc3": 1, "api": 900}})
    assert route_versions.diff(a, b) == []
    assert route_versions.diff(a, a) == []


def test_provider_order_breaks_priority_ties_by_connection_key():
    assert route_versions.provider_order({"zeta": 10, "alpha": 10, "mid": 5}) == ["mid", "alpha", "zeta"]


def test_normalize_reads_a_json_string_and_fills_missing_parts():
    out = route_versions.normalize('{"routes": {"zz": {"model_id": "m1"}}}')
    assert out == {"order_mode": None, "routes": {"zz": {"model_id": "m1", "fallback_model_ids": []}},
                   "priorities": {}}


# ── route_settings: DB wins, env var only as the bootstrap default ──────────────────────────────────────────────

def _settings_row(monkeypatch, row):
    monkeypatch.setattr(connections, "run_sync", lambda fn: row)
    route_settings.invalidate()
    route_settings._state = None


def test_no_settings_row_falls_back_to_the_env_var(monkeypatch):
    _settings_row(monkeypatch, None)
    monkeypatch.delenv("LLM_ROUTE_AWS_FIRST", raising=False)
    assert route_settings.order_mode_sync() == "model_first" and route_settings.order_mode_source() == "env"
    monkeypatch.setenv("LLM_ROUTE_AWS_FIRST", "on")
    assert route_settings.order_mode_sync() == "aws_first" and client_mod.route_aws_first_enabled() is True
    assert route_settings.current_version_sync() is None


def test_the_db_row_wins_over_the_env_var(monkeypatch):
    monkeypatch.setenv("LLM_ROUTE_AWS_FIRST", "on")
    _settings_row(monkeypatch, {"order_mode": "model_first", "current_version": 4})
    assert route_settings.order_mode_sync() == "model_first" and route_settings.order_mode_source() == "db"
    assert client_mod.route_aws_first_enabled() is False
    assert route_settings.current_version_sync() == 4


def test_a_read_failure_uses_the_env_var(monkeypatch):
    def boom(fn):
        raise OSError("db down")
    monkeypatch.setattr(connections, "run_sync", boom)
    route_settings.invalidate()
    route_settings._state = None
    monkeypatch.setenv("LLM_ROUTE_AWS_FIRST", "on")
    assert route_settings.order_mode_sync() == "aws_first"


def test_the_preview_override_is_this_context_only(monkeypatch):
    _settings_row(monkeypatch, {"order_mode": "model_first", "current_version": 1})
    with route_settings.override_order_mode("aws_first"):
        assert route_settings.order_mode_sync() == "aws_first"
    assert route_settings.order_mode_sync() == "model_first"


def test_a_new_version_drops_the_route_caches_of_this_process(monkeypatch):
    calls = []
    monkeypatch.setattr(route_settings, "_seen_version", 3)
    from shared.llm_client import role_config
    monkeypatch.setattr(role_config, "invalidate", lambda stage=None: calls.append("stages"))
    monkeypatch.setattr(deployments, "invalidate", lambda: calls.append("deployments"))
    _settings_row(monkeypatch, {"order_mode": "aws_first", "current_version": 4})
    route_settings.order_mode_sync()
    assert calls == ["stages", "deployments"]
    calls.clear()
    route_settings.invalidate()
    route_settings.order_mode_sync()                     # same version again: nothing dropped
    assert calls == []


def test_every_route_read_watches_the_version_so_another_process_change_lands_within_the_ttl(monkeypatch):
    """A provider-order save in another task: this process's deployment cache (5 min) is fresh, but the next
    deployment or stage read sees the new version (20 s cache) and reloads, whatever the stage or order mode."""
    from shared.llm_client import role_config
    state = {"version": 3, "prio": {"acc3": 10, "api": 20}}
    fetches = {"settings": 0, "deployments": 0}

    def run_sync(fn):
        if fn is route_settings._fetch:
            fetches["settings"] += 1
            return {"order_mode": "model_first", "current_version": state["version"]}
        if fn is deployments._fetch_all:
            fetches["deployments"] += 1
            return [_dep(c, p) for c, p in state["prio"].items()]
        return []
    monkeypatch.setattr(connections, "run_sync", run_sync)
    clock = {"t": 1000.0}
    monkeypatch.setattr(route_settings.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(deployments.time, "monotonic", lambda: clock["t"])
    route_settings.invalidate()
    deployments.invalidate()

    assert [d.connection_key for d in deployments.deployments_for_sync("zz-m")] == ["acc3", "api"]
    for _ in range(5):                                   # cache hits: a clock read, no DB
        deployments.deployments_for_sync("zz-m")
    assert fetches == {"settings": 1, "deployments": 1}

    state.update(version=4, prio={"acc3": 20, "api": 10})          # saved by the api task
    clock["t"] += route_settings._CACHE_TTL_SECONDS - 1
    assert [d.connection_key for d in deployments.deployments_for_sync("zz-m")] == ["acc3", "api"]  # within 20 s
    clock["t"] += 2                                      # 21 s later, the deployment cache is still "fresh" (5 min)
    assert [d.connection_key for d in deployments.deployments_for_sync("zz-m")] == ["api", "acc3"]
    assert fetches == {"settings": 2, "deployments": 2}

    dropped = []
    monkeypatch.setattr(role_config, "invalidate", lambda stage=None: dropped.append(stage))
    state["version"] = 5
    clock["t"] += route_settings._CACHE_TTL_SECONDS + 1
    route_settings.watch_version()
    assert dropped == [None]


def test_the_version_watch_never_fails_a_route_read(monkeypatch):
    def boom():
        raise RuntimeError("lock gone")
    monkeypatch.setattr(route_settings, "_ensure_loaded", boom)
    route_settings.watch_version()                       # logged, not raised
    monkeypatch.setattr(deployments, "_by_model", {"zz-m": [_dep("acc3", 10)]})
    monkeypatch.setattr(deployments, "_loaded_at", 1e18)
    assert [d.connection_key for d in deployments.deployments_for_sync("zz-m")] == ["acc3"]


# ── deployments: the preview's priority override ─────────────────────────────────────────────────────────────────

def _dep(conn, prio, enabled=True):
    return ModelDeployment(model_key="zz-m", connection_key=conn, wire_id=f"w-{conn}", priority=prio,
                           enabled=enabled, origin="seed")


def test_priority_override_reorders_one_model_for_this_context_only(monkeypatch):
    monkeypatch.setattr(deployments, "_by_model", {"zz-m": [_dep("acc3", 10), _dep("api", 20)]})
    monkeypatch.setattr(deployments, "_loaded_at", 1e18)
    assert [d.connection_key for d in deployments.deployments_for_sync("zz-m")] == ["acc3", "api"]
    with deployments.priority_override({"zz-m": {"api": 5}}):
        assert [d.connection_key for d in deployments.deployments_for_sync("zz-m")] == ["api", "acc3"]
    assert [d.connection_key for d in deployments.deployments_for_sync("zz-m")] == ["acc3", "api"]


# ── proposals (pure) ─────────────────────────────────────────────────────────────────────────────────────────────

DEPS = [{"model_key": "zz-claude", "connection_key": "acc3"}, {"model_key": "zz-claude", "connection_key": "api"},
        {"model_key": "zz-gpt", "connection_key": "acc3"}, {"model_key": "zz-gpt", "connection_key": "oai"}]
LIVE = _snap(routes={"zz_writer": _route("zz-claude", "zz-gpt")},
             prios={"zz-claude": {"acc3": 10, "api": 20}, "zz-gpt": {"acc3": 10, "oai": 20}})


def test_build_target_renumbers_only_a_changed_order_and_checks_the_shape():
    target, errors = rc.build_target(LIVE, provider_order={"zz-claude": ["api", "acc3"], "zz-gpt": ["acc3", "oai"]},
                                     deployments=DEPS, stages={"zz_writer"})
    assert errors == []
    assert target["priorities"]["zz-claude"] == {"api": 10, "acc3": 20}
    assert target["priorities"]["zz-gpt"] == {"acc3": 10, "oai": 20}           # unchanged order: untouched
    _, errors = rc.build_target(LIVE, provider_order={"zz-claude": ["api"]}, deployments=DEPS)
    assert [e["rule"] for e in errors] == ["provider_order"]
    _, errors = rc.build_target(LIVE, routes={"zz_nope": ["zz-claude"], "zz_writer": []}, order_mode="sideways",
                                deployments=DEPS, stages={"zz_writer"})
    assert sorted(e["rule"] for e in errors) == ["empty_chain", "order_mode", "unknown_stage"]


def test_build_target_sets_chains_and_exact_priorities_of_a_revert():
    target, errors = rc.build_target(LIVE, routes={"zz_writer": ["zz-gpt", "zz-claude"]},
                                     priorities={"zz-claude": {"api": 1, "acc3": 2, "gone": 3}}, deployments=DEPS,
                                     stages={"zz_writer"})
    assert errors == []
    assert target["routes"]["zz_writer"] == _route("zz-gpt", "zz-claude")
    assert target["priorities"]["zz-claude"] == {"api": 1, "acc3": 2}          # a removed connection is dropped


def _model(key, family):
    return CatalogModel(model_key=key, label=key, vendor=family, provider="bedrock", api_style="converse",
                        enabled=True, family=family, callable_via=("llm_client",))


CONNS = {"acc3": {"kind": "bedrock", "data_scope": "aws_only"}, "api": {"kind": "anthropic", "data_scope":
         "vendor_direct"}, "oai": {"kind": "openai", "data_scope": "vendor_direct"}}
MODELS = [_model("zz-claude", "anthropic"), _model("zz-gpt", "openai")]


def test_prefer_anthropic_api_moves_the_api_first_for_claude_only_and_sets_model_first():
    live = _snap("aws_first", prios={"zz-claude": {"acc3": 10, "api": 20}, "zz-gpt": {"oai": 10, "acc3": 20}})
    out = rc.preset_changes("prefer_anthropic_api", live, MODELS, CONNS)
    assert out == {"provider_order": {"zz-claude": ["api", "acc3"]}, "order_mode": "model_first"}


def test_prefer_aws_puts_every_aws_deployment_first_and_keeps_the_order_mode():
    live = _snap("aws_first", prios={"zz-claude": {"acc3": 20, "api": 10}, "zz-gpt": {"oai": 10, "acc3": 20}})
    out = rc.preset_changes("prefer_aws", live, MODELS, CONNS)
    assert out == {"provider_order": {"zz-claude": ["acc3", "api"], "zz-gpt": ["acc3", "oai"]}}
    assert rc.preset_changes("prefer_aws", LIVE, MODELS, CONNS) == {"provider_order": {}}
    with pytest.raises(KeyError):
        rc.preset_changes("zz_unknown", LIVE, MODELS, CONNS)


def test_revert_changes_carry_the_whole_snapshot():
    snap = _snap("aws_first", routes={"zz_writer": _route("zz-claude")}, prios={"zz-claude": {"acc3": 7}})
    assert rc.revert_changes(snap) == {"order_mode": "aws_first", "routes": {"zz_writer": ["zz-claude"]},
                                       "priorities": {"zz-claude": {"acc3": 7}}}


# ── the rule check of a preview ──────────────────────────────────────────────────────────────────────────────────

def _world(stages, live, models=MODELS, deps=DEPS):
    return rc._World(settings={"current_version": 5, "order_mode": live["order_mode"]}, live=live,
                     stages=stages, models=models,
                     deployments=[{**d, "priority": 10, "enabled": True, "access_state": "ok"} for d in deps],
                     connections={k: {**v, "connection_key": k, "label": k, "enabled": True}
                                  for k, v in CONNS.items()},
                     reach={"zz-claude": frozenset({"aws_only", "vendor_direct"}),
                            "zz-gpt": frozenset({"aws_only", "vendor_direct"})})


STAGES = [{"stage": "zz_writer", "role": "writer", "provider": "claude", "model_id": "zz-claude",
           "fallback_model_ids": ["zz-gpt"], "allowed_scopes": ["aws_only", "vendor_direct"], "is_active": True},
          {"stage": "zz_judge", "role": "judge", "provider": "openai", "model_id": "zz-gpt",
           "fallback_model_ids": [], "allowed_scopes": ["aws_only", "vendor_direct"], "is_active": True}]


@pytest.fixture
def resolved(monkeypatch):
    """resolve_stage without the registry: the first model of the chain unless it is marked dead."""
    dead: set = set()

    def fake(rows, priorities, order_mode, evidence=None):
        return {r["stage"]: {"first": next((f"{k}@acc3" for k in [r["model_id"], *r["fallback_model_ids"]]
                                            if k not in dead), None), "attempts": []}
                for r in rows if r.get("is_active") is not False}
    monkeypatch.setattr(rc, "_resolve_all", fake)
    monkeypatch.setattr(rc, "chain_problems", lambda row, chain, models, reach, pii_usable=None: [])
    return dead


def test_a_new_judge_of_the_writers_family_blocks_and_needs_evidence(resolved):
    live = _snap(routes={"zz_writer": _route("zz-claude", "zz-gpt"), "zz_judge": _route("zz-gpt")})
    world = _world(STAGES, live)
    target, errors = rc.build_target(live, routes={"zz_judge": ["zz-gpt", "zz-claude"]}, deployments=DEPS,
                                     stages={"zz_writer", "zz_judge"})
    out = asyncio.run(rc.preview_world(world, target, errors))
    assert out["valid"] is False and out["needs_evidence"] and out["evidence_stages"] == ["zz_judge"]
    assert any(e["rule"] == "judge_writer_family" and e["stage"] == "zz_judge" for e in out["errors"])


def test_an_existing_broken_rule_is_a_warning_not_an_error(resolved):
    live = _snap(routes={"zz_writer": _route("zz-claude", "zz-gpt"), "zz_judge": _route("zz-gpt", "zz-claude")})
    stages = [STAGES[0], {**STAGES[1], "fallback_model_ids": ["zz-claude"]}]
    world = _world(stages, live)
    target, errors = rc.build_target(live, order_mode="aws_first", deployments=DEPS, stages={"zz_writer", "zz_judge"})
    out = asyncio.run(rc.preview_world(world, target, errors))
    assert out["valid"] is True and out["needs_evidence"] is False
    assert any(w["rule"] == "judge_writer_family" and w.get("existing") for w in out["warnings"])
    assert out["diff"] == [{"kind": "order_mode", "before": "model_first", "after": "aws_first"}]


def test_losing_the_last_live_model_blocks_but_an_already_dead_stage_only_warns(resolved):
    live = _snap(routes={"zz_writer": _route("zz-claude", "zz-gpt"), "zz_judge": _route("zz-gpt")})
    world = _world(STAGES, live)
    resolved.add("zz-gpt")
    target, errors = rc.build_target(live, routes={"zz_writer": ["zz-gpt"]}, deployments=DEPS,
                                     stages={"zz_writer", "zz_judge"})
    out = asyncio.run(rc.preview_world(world, target, errors))
    rules = {(e["stage"], e["rule"]) for e in out["errors"]}
    assert ("zz_writer", "no_live_model") in rules
    assert ("zz_judge", "no_live_model") not in rules                             # dead before this change
    assert any(w["stage"] == "zz_judge" and w["rule"] == "no_live_model" and w["existing"] for w in out["warnings"])


@pytest.fixture
def served_by_priority(monkeypatch):
    """resolve_stage without the registry: the first model of the chain on its lowest-priority connection."""
    def fake(rows, priorities, order_mode, evidence=None):
        out = {}
        for r in rows:
            if r.get("is_active") is False:
                continue
            key = r["model_id"]
            prios = priorities.get(key) or {}
            conn = min(prios, key=lambda c: (prios[c], c)) if prios else "acc3"
            out[r["stage"]] = {"first": f"{key}@{conn}", "attempts": []}
        return out
    monkeypatch.setattr(rc, "_resolve_all", fake)
    monkeypatch.setattr(rc, "chain_problems", lambda row, chain, models, reach, pii_usable=None: [])


def test_a_provider_order_change_that_moves_a_writer_needs_evidence(served_by_priority):
    live = _snap(routes={"zz_writer": _route("zz-claude", "zz-gpt"), "zz_judge": _route("zz-gpt")},
                 prios={"zz-claude": {"acc3": 10, "api": 20}, "zz-gpt": {"acc3": 10, "oai": 20}})
    world = _world(STAGES, live)
    target, errors = rc.build_target(live, provider_order={"zz-claude": ["api", "acc3"]}, deployments=DEPS,
                                     stages={"zz_writer", "zz_judge"})
    out = asyncio.run(rc.preview_world(world, target, errors))
    assert out["valid"] is True and out["needs_evidence"] is True and out["evidence_stages"] == ["zz_writer"]
    writer = next(s for s in out["stages"] if s["stage"] == "zz_writer")
    assert writer["changed"] is False and writer["serving_changed"] is True
    assert (writer["first_before"], writer["first"]) == ("zz-claude@acc3", "zz-claude@api")


def test_a_provider_order_change_of_a_fallback_only_model_needs_no_evidence(served_by_priority):
    live = _snap(routes={"zz_writer": _route("zz-claude", "zz-gpt"), "zz_judge": _route("zz-gpt")},
                 prios={"zz-claude": {"acc3": 10, "api": 20}, "zz-gpt": {"acc3": 10, "oai": 20}})
    world = _world([STAGES[0]], {**live, "routes": {"zz_writer": live["routes"]["zz_writer"]}})
    target, errors = rc.build_target(world.live, provider_order={"zz-gpt": ["oai", "acc3"]}, deployments=DEPS,
                                     stages={"zz_writer"})
    out = asyncio.run(rc.preview_world(world, target, errors))
    assert out["unchanged"] is False and out["needs_evidence"] is False and out["evidence_stages"] == []


def test_a_preset_that_moves_a_judge_needs_evidence(served_by_priority):
    live = _snap(routes={"zz_writer": _route("zz-claude"), "zz_judge": _route("zz-gpt")},
                 prios={"zz-claude": {"acc3": 10, "api": 20}, "zz-gpt": {"oai": 10, "acc3": 20}})
    world = _world(STAGES, live)
    changes = rc.preset_changes("prefer_aws", live, MODELS, CONNS)
    target, errors = rc.build_target(live, provider_order=changes["provider_order"], deployments=DEPS,
                                     stages={"zz_writer", "zz_judge"})
    out = asyncio.run(rc.preview_world(world, target, errors))
    assert out["needs_evidence"] is True and out["evidence_stages"] == ["zz_judge"]


def test_the_attempt_cap_blocks_a_too_long_chain(resolved):
    models = [_model(f"zz-c{i}", "anthropic") for i in range(4)] + MODELS
    deps = [{"model_key": f"zz-c{i}", "connection_key": c} for i in range(4) for c in ("acc3", "api")] + DEPS
    live = _snap(routes={"zz_writer": _route("zz-claude", "zz-gpt"), "zz_judge": _route("zz-gpt")})
    world = _world(STAGES, live, models=models, deps=deps)
    world.reach.update({f"zz-c{i}": frozenset({"aws_only", "vendor_direct"}) for i in range(4)})
    target, errors = rc.build_target(live, routes={"zz_writer": [f"zz-c{i}" for i in range(4)]}, deployments=deps,
                                     stages={"zz_writer", "zz_judge"})
    out = asyncio.run(rc.preview_world(world, target, errors))
    assert any(e["rule"] == "attempt_cap" and e["stage"] == "zz_writer" for e in out["errors"])


def test_apply_refuses_an_invalid_or_empty_proposal_before_touching_the_db(monkeypatch):
    async def fake_proposal(pool, **kw):
        return "world", "target", []

    async def fake_preview(world, target, shape):
        return {"unchanged": kw_state["unchanged"], "valid": False,
                "errors": [{"stage": "zz_judge", "message": "boom", "rule": "x"}], "needs_evidence": False}
    kw_state = {"unchanged": True}
    monkeypatch.setattr(rc, "_proposal", fake_proposal)
    monkeypatch.setattr(rc, "preview_world", fake_preview)
    with pytest.raises(rc.RouteControlRefused, match="nothing to change"):
        asyncio.run(rc.apply(None, actor="admin:zz", action="save", expected_version=1, order_mode="aws_first"))
    kw_state["unchanged"] = False
    with pytest.raises(rc.RouteControlRefused, match="zz_judge: boom"):
        asyncio.run(rc.apply(None, actor="admin:zz", action="save", expected_version=1, order_mode="aws_first"))


# ── the router ───────────────────────────────────────────────────────────────────────────────────────────────────

ROUTES = {
    ("GET", "/admin/ai-gateway/route-control"): staff_authz.SPEND_READ,
    ("POST", "/admin/ai-gateway/route-control/preview"): staff_authz.AI_ADMIN,
    ("POST", "/admin/ai-gateway/route-control/save"): staff_authz.AI_ADMIN,
    ("POST", "/admin/ai-gateway/route-control/presets/{preset}"): staff_authz.AI_ADMIN,
    ("GET", "/admin/ai-gateway/route-control/versions"): staff_authz.SPEND_READ,
    ("GET", "/admin/ai-gateway/route-control/versions/{version}"): staff_authz.SPEND_READ,
    ("POST", "/admin/ai-gateway/route-control/versions/{version}/revert"): staff_authz.AI_ADMIN,
}


def test_every_route_has_its_capability():
    for route, cap in ROUTES.items():
        assert staff_authz.POLICY[route] == cap, route


def test_the_page_access_names_the_route_control_save():
    assert admin_ai_gateway.PAGE_WRITES["route_control"] == ("POST", "/admin/ai-gateway/route-control/save")
    principal = staff_authz.principal_from_role("ai_ops", SimpleNamespace(auth="cognito", is_owner=False,
                                                                          e2e_read_only=False))
    assert admin_ai_gateway.page_access(principal)["actions"]["route_control"] is True
    finance = staff_authz.principal_from_role("finance", SimpleNamespace(auth="cognito", is_owner=False,
                                                                         e2e_read_only=False))
    assert admin_ai_gateway.page_access(finance)["actions"]["route_control"] is False


@pytest.fixture
def api(monkeypatch):
    app = FastAPI()
    app.state.pool = object()
    for dep in router_mod.router.dependencies:
        app.dependency_overrides[dep.dependency] = lambda: None
    app.include_router(router_mod.router)
    calls = []

    async def fake_apply(pool, **kw):
        calls.append(kw)
        if kw.get("expected_version") == 99:
            raise rc.RouteControlConflict("changed since")
        return {"version": 7, "action": kw["action"]}

    async def unavailable(pool):
        raise rc.RouteControlUnavailable("not set up")
    monkeypatch.setattr(rc, "apply", fake_apply)
    monkeypatch.setattr(rc, "read_state", unavailable)
    return TestClient(app), calls


def test_router_maps_errors_and_passes_the_actor(api):
    client, calls = api
    assert client.get("/admin/ai-gateway/route-control").status_code == 503
    r = client.post("/admin/ai-gateway/route-control/save", json={"order_mode": "aws_first", "expected_version": 3},
                    headers={"x-admin-user-id": "zz-staff"})
    assert r.status_code == 200 and calls[-1]["actor"] == "admin:zz-staff" and calls[-1]["action"] == "save"
    assert client.post("/admin/ai-gateway/route-control/save",
                       json={"order_mode": "aws_first", "expected_version": 99}).status_code == 409
    assert client.post("/admin/ai-gateway/route-control/save", json={"expected_version": 3}).status_code == 422
    assert client.post("/admin/ai-gateway/route-control/save",
                       json={"order_mode": "sideways", "expected_version": 3}).status_code == 422
    assert client.post("/admin/ai-gateway/route-control/presets/zz_unknown",
                       json={"expected_version": 3}).status_code == 404
    r = client.post("/admin/ai-gateway/route-control/presets/prefer_aws", json={"expected_version": 3})
    assert r.status_code == 200 and calls[-1]["preset"] == "prefer_aws" and calls[-1]["action"] == "preset"
    assert client.post("/admin/ai-gateway/route-control/versions/4/revert", json={"note": "back"}).status_code == 422
    r = client.post("/admin/ai-gateway/route-control/versions/4/revert", json={"note": "back", "expected_version": 5})
    assert r.status_code == 200 and calls[-1]["revert_to"] == 4 and calls[-1]["action"] == "revert"


def test_a_preview_names_one_kind_of_change():
    with pytest.raises(ValueError):
        router_mod.PreviewBody(preset="prefer_aws", order_mode="aws_first")
    with pytest.raises(ValueError):
        router_mod.PreviewBody(preset="prefer_aws", revert_to=2)
    assert router_mod.PreviewBody(routes={"zz": ["m"]}).routes == {"zz": ["m"]}
    with pytest.raises(ValueError):
        router_mod.Proposal(routes={"zz": ["m"] * 21})
