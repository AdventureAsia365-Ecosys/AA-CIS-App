"""AA-1079 (S277) — the serving_first route-control preset: on the judge routes (t2_judge, t7_debate_judge, t10_judge)
the model that serves the stage moves to the front while the rungs ahead of it are closed (refused by the account or
circuit open), so calls stop taking a refusal first. api/services/llm_route_control.py: serving_first_changes (pure),
the rule exemption in preview_world, the _proposal wiring. Invented stage and model keys, except the three judge
stage names the preset is for.
"""
from __future__ import annotations

import asyncio

import pytest

from api.services import llm_route_control as rc


def _snap(routes):
    return {"order_mode": "model_first", "routes": routes, "priorities": {}}


def _route(*chain):
    return {"model_id": chain[0], "fallback_model_ids": list(chain[1:])}


JUDGES = ["t2_judge", "t7_debate_judge", "t10_judge"]
CLOSED = {"model": "zz-luna-a", "connection": "acc3", "reason": "refused (access_denied)"}
CIRCUIT = {"model": "zz-luna-b", "connection": None, "reason": "circuit open: acc3 (AccessDenied)"}


def test_the_preset_names_the_three_judge_stages():
    assert rc.PRESETS["serving_first"]["stages"] == JUDGES
    assert rc.SERVING_FIRST == "serving_first"


def test_the_serving_model_moves_first_and_the_closed_rungs_keep_their_order():
    live = _snap({"t2_judge": _route("zz-luna-a", "zz-luna-b", "zz-luna-api")})
    resolved = {"t2_judge": {"first": "zz-luna-api@oai", "skipped": [CLOSED, CIRCUIT]}}
    out = rc.serving_first_changes(live, resolved, ["t2_judge"])
    assert out["routes"] == {"t2_judge": ["zz-luna-api", "zz-luna-a", "zz-luna-b"]}
    assert "zz-luna-a, zz-luna-b" in out["exempt"]["t2_judge"]


def test_a_stage_served_by_its_primary_is_left_alone():
    live = _snap({"t10_judge": _route("zz-luna-a", "zz-luna-api")})
    out = rc.serving_first_changes(live, {"t10_judge": {"first": "zz-luna-a@acc3", "skipped": []}}, ["t10_judge"])
    assert out == {"routes": {}, "exempt": {}}


@pytest.mark.parametrize("reason", ["disabled in the catalog", "no enabled deployment in this scope",
                                    "budget reached: acc3", "role not served: acc3"])
def test_a_rung_skipped_for_an_admin_reason_is_not_overridden(reason):
    """Only a closed rung (refused / circuit open) moves the serving model up; a switched-off or out-of-scope rung is
    an admin decision and the stage keeps its chain."""
    live = _snap({"t7_debate_judge": _route("zz-luna-a", "zz-luna-api")})
    resolved = {"t7_debate_judge": {"first": "zz-luna-api@oai",
                                    "skipped": [{"model": "zz-luna-a", "connection": None, "reason": reason}]}}
    assert rc.serving_first_changes(live, resolved, ["t7_debate_judge"])["routes"] == {}


def test_no_serving_model_or_a_stage_outside_the_list_changes_nothing():
    live = _snap({"t2_judge": _route("zz-luna-a", "zz-luna-api"), "zz_other": _route("zz-luna-a", "zz-luna-api")})
    resolved = {"t2_judge": {"first": None, "skipped": [CLOSED]},
                "zz_other": {"first": "zz-luna-api@oai", "skipped": [CLOSED]}}
    assert rc.serving_first_changes(live, resolved, ["t2_judge"])["routes"] == {}


def test_the_provider_order_presets_do_not_take_serving_first():
    with pytest.raises(KeyError):
        rc.preset_changes("serving_first", _snap({}), [], {})


# ── preview: the exemption is scoped to the preset's stages and rules ───────────────────────────────────────────

def _world(stages, live, exempt=None):
    return rc._World(settings={"current_version": 7, "order_mode": "model_first"}, live=live, stages=stages,
                     models=[], deployments=[], connections={}, reach={}, rule_exempt=dict(exempt or {}))


STAGES = [{"stage": s, "role": "judge", "provider": "openai", "model_id": "zz-luna-a",
           "fallback_model_ids": ["zz-luna-api"], "allowed_scopes": ["aws_only", "vendor_direct"], "is_active": True}
          for s in ("t2_judge", "zz_judge")]


@pytest.fixture
def problems(monkeypatch):
    """chain_problems always reports the vendor-first rule; the resolver serves the chain's first model."""
    def fake_problems(row, chain, models, reach, pii_usable=None):
        if chain and chain[0] == "zz-luna-api":
            return [{"rule": "vendor_last_resort", "message": "vendor model leads", "stage": row["stage"]},
                    {"rule": "duplicate", "message": "listed twice", "stage": row["stage"]}][:1]
        return []

    def fake_resolve(rows, priorities, order_mode, evidence=None):
        return {r["stage"]: {"first": "zz-luna-api@oai", "attempts": [], "skipped": []} for r in rows}
    monkeypatch.setattr(rc, "chain_problems", fake_problems)
    monkeypatch.setattr(rc, "_resolve_all", fake_resolve)
    monkeypatch.setattr(rc.llm_route_chains, "violations", lambda *a, **k: [])


def test_an_exempt_stage_gets_a_warning_and_another_stage_still_an_error(problems):
    live = _snap({"t2_judge": _route("zz-luna-a", "zz-luna-api"), "zz_judge": _route("zz-luna-a", "zz-luna-api")})
    target, errors = rc.build_target(live, routes={"t2_judge": ["zz-luna-api", "zz-luna-a"],
                                                   "zz_judge": ["zz-luna-api", "zz-luna-a"]},
                                     stages={"t2_judge", "zz_judge"})
    world = _world(STAGES, live, exempt={"t2_judge": "serving_first: zz-luna-a refused by the account"})
    out = asyncio.run(rc.preview_world(world, target, errors))
    assert [(e["stage"], e["rule"]) for e in out["errors"]] == [("zz_judge", "vendor_last_resort")]
    warn = [w for w in out["warnings"] if w.get("exempt")]
    assert [(w["stage"], w["rule"]) for w in warn] == [("t2_judge", "vendor_last_resort")]
    assert "allowed: serving_first" in warn[0]["message"]
    assert out["needs_evidence"] is True and "t2_judge" in out["evidence_stages"]   # a judge chain change (D7)


def test_a_non_exempt_rule_stays_an_error_on_an_exempt_stage(monkeypatch, problems):
    monkeypatch.setattr(rc, "chain_problems", lambda row, chain, models, reach, pii_usable=None: [
        {"rule": "duplicate", "message": "listed twice", "stage": row["stage"]}])
    live = _snap({"t2_judge": _route("zz-luna-a", "zz-luna-api")})
    target, errors = rc.build_target(live, routes={"t2_judge": ["zz-luna-api", "zz-luna-a"]}, stages={"t2_judge"})
    out = asyncio.run(rc.preview_world(_world(STAGES[:1], live, exempt={"t2_judge": "x"}), target, errors))
    assert [e["rule"] for e in out["errors"]] == ["duplicate"]


def test_the_proposal_of_serving_first_reads_the_live_resolver(monkeypatch):
    live = _snap({"t2_judge": _route("zz-luna-a", "zz-luna-api"), "t10_judge": _route("zz-luna-a", "zz-luna-api")})
    stages = [{**STAGES[0], "stage": s} for s in ("t2_judge", "t10_judge")]
    world = _world(stages, live)

    async def fake_world(pool):
        return world

    async def fake_serving(w):
        assert w is world
        return {"t2_judge": {"first": "zz-luna-api@oai", "skipped": [CLOSED]},
                "t10_judge": {"first": "zz-luna-api@oai", "skipped": [CLOSED]}}
    monkeypatch.setattr(rc, "_world", fake_world)
    monkeypatch.setattr(rc, "serving", fake_serving)
    w, target, errors = asyncio.run(rc._proposal(None, preset="serving_first"))
    assert errors == []
    assert rc.route_versions.chain_of(target["routes"]["t2_judge"]) == ["zz-luna-api", "zz-luna-a"]
    assert rc.route_versions.chain_of(target["routes"]["t10_judge"]) == ["zz-luna-api", "zz-luna-a"]
    assert set(w.rule_exempt) == {"t2_judge", "t10_judge"}            # t7_debate_judge has no row here: skipped
    assert target["priorities"] == {} and target["order_mode"] == "model_first"
