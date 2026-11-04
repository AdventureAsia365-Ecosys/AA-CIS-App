"""AA-1042 (S268 follow-up) — the route preview's "first" is the model@connection that would SERVE the stage.

Live Dev (S268): the Route control preview and the $0 route check named ``haiku-5-5@acc3`` first for a0_column_map
while the probe (CIS 367) and the Stage coverage card had it refused (access_denied): its breaker circuit was
half-open, so the gateway's plan still held it. ``llm_route_chains.resolve_stage`` now takes the refusals on record
(``stage_coverage.RefusalEvidence``, the card's own rule ``stage_coverage.refusal``), names the deployment that would
serve as ``first``, lists the ones before it with their reason in ``skipped`` and keeps ``called_first``.

The preview and the gateway run on the SAME fixtures (the AA-1026 route snapshot after migration 366 and the seed
registry of tests/unit/aa1002_s3_route_capture.py): the gateway, given a deployment that refuses (AccessDenied) and
the next one that answers, is served by the deployment the preview named first. No model call, no DB.
"""
from __future__ import annotations

import time
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from shared.llm_client import stage_coverage
from shared.llm_client.breaker import Health
from tests.unit import aa1002_s3_route_capture as rc
from tests.unit.test_aa1026_family_fallback_chains import AFTER, _fixture, _routes, _scopes_of, _seed

NOW = datetime(2026, 11, 5, 9, 0, tzinfo=timezone.utc)
DENIED = RuntimeError("AccessDeniedException: Error 002: Access to Bedrock models is not allowed for this account")


def _dep(model, conn="acc3", state="unknown", probed=None, enabled=True, priority=10):
    return {"model_key": model, "connection_key": conn, "priority": priority, "enabled": enabled,
            "access_state": state, "last_probe_at": probed}


def _conn(key="acc3", state="ok"):
    return {"connection_key": key, "kind": "bedrock", "enabled": True, "data_scope": "aws_only",
            "served_roles": None, "access_state": state}


def _evidence(*deps, conns=None, health=()):
    return stage_coverage.RefusalEvidence.of(
        deps, conns or [_conn(), {**_conn("anthropic-direct"), "kind": "anthropic", "data_scope": "vendor_direct"},
                        {**_conn("openai-direct"), "kind": "openai", "data_scope": "vendor_direct"}],
        health, NOW)


# ── the one refusal rule (stage_coverage.refusal), shared with the coverage card ────────────────────────────────────

def test_a_probe_refusal_newer_than_any_answer_is_refused():
    ev = _evidence(_dep("zz-small", state="access_denied", probed=NOW - timedelta(hours=2)))
    assert ev.refused("zz-small", "acc3") == "refused (access_denied)"


def test_an_answer_after_the_refusal_clears_it():
    health = [Health("acc3", "zz-small", last_ok_at=NOW - timedelta(minutes=5))]
    ev = _evidence(_dep("zz-small", state="access_denied", probed=NOW - timedelta(hours=2)), health=health)
    assert ev.refused("zz-small", "acc3") is None


def test_a_breaker_refusal_counts_like_the_probe():
    health = [Health("acc3", "zz-small", last_error_at=NOW - timedelta(hours=1), last_error_class="quota_exhausted")]
    ev = _evidence(_dep("zz-small"), health=health)
    assert ev.refused("zz-small", "acc3") == "refused (quota_exhausted)"


def test_an_unconfigured_connection_refuses_every_model_and_an_unknown_row_refuses_nothing():
    ev = _evidence(_dep("zz-small"), conns=[_conn(state="unconfigured")])
    assert ev.refused("zz-small", "acc3") == "connection unconfigured"
    assert ev.refused("zz-other", "acc3") is None                    # no deployment row: nothing on record
    assert ev.refused("zz-small", "zz-missing-conn") is None


def test_the_coverage_card_uses_the_same_rule():
    dep, conn = _dep("zz-small", state="access_denied", probed=NOW - timedelta(hours=2)), _conn()
    state, why = stage_coverage.assess(dep, conn, True, None, connection_open=False, role="writer",
                                       scopes=("aws_only",), primary=True, now=NOW)
    assert (state, why) == ("refused", stage_coverage.refusal(dep, conn, None, NOW))


# ── the preview resolver and the gateway on the same fixtures ───────────────────────────────────────────────────────

STAGE = "a0_column_map"                 # haiku-5-5 > haiku-4-5 > sonnet-4-6 > GPT-6 Luna on the OpenAI API


def _registry():
    routes = _routes(AFTER)
    fixture = _fixture(routes)
    models = rc.catalog_models(fixture)
    cfg = rc.stage_configs(fixture, _scopes_of(routes)(fixture))[STAGE]
    return models, cfg


def _enter_registry(stack: ExitStack, models: dict, anthropic_on: bool):
    from shared.llm_client import catalog, connections
    rc._reset_registry()
    catalog._models = dict(models)
    catalog._loaded_at, catalog._failed_at = time.monotonic(), 0.0
    stack.enter_context(patch.object(rc, "seed_deployments", _seed(rc.seed_deployments)))
    stack.enter_context(patch.object(connections, "run_sync",
                                     rc._registry_fetch("seed_anthropic_on" if anthropic_on else "seed", models)))
    stack.callback(rc._reset_registry)
    stack.callback(catalog.invalidate)


def _preview(evidence, anthropic_on: bool = False) -> dict:
    from api.services import llm_route_chains as route_chains
    from shared.llm_client import vendor_route
    models, cfg = _registry()
    with ExitStack() as stack:
        _enter_registry(stack, models, anthropic_on)
        # half-open / closed circuits: the gateway's plan still holds every deployment (the live S268 case)
        stack.enter_context(patch.object(vendor_route, "circuit_open", lambda conn, model: False))
        stack.enter_context(patch.object(vendor_route, "connection_circuit_open", lambda conn: None))
        return route_chains.resolve_stage(cfg, evidence)


def _wires(models: dict) -> dict:
    """(connection, wire id) -> catalog model key, from the same seed deployments the registry serves."""
    return {(d.connection_key, d.wire_id): d.model_key for d in _seed(rc.seed_deployments)(models)}


def _gateway(refused: set, anthropic_on: bool = False) -> tuple[list, str]:
    """Run the stage through LLMClient.generate: a deployment in ``refused`` ((model, connection)) answers
    AccessDenied, every other one answers. Returns the calls made [(model, connection)] and "model@connection" of the
    one that served."""
    from shared.llm_client.client import LLMClient
    from shared.llm_client.models import LLMRequest
    models, cfg = _registry()
    wires = _wires(models)
    made: list = []
    reply = {"output": {"message": {"role": "assistant", "content": [{"text": "ok"}]}},
             "usage": {"inputTokens": 10, "outputTokens": 2}, "stopReason": "end_turn"}

    def runtime(where: str):
        rt = MagicMock()

        def converse(**kwargs):
            key = wires.get((where, kwargs.get("modelId")), kwargs.get("modelId"))
            made.append((key, where))
            if (key, where) in refused:
                raise DENIED
            return reply
        rt.converse.side_effect = converse
        return rt

    def anthropic_send(**kwargs):
        key = wires.get((kwargs["connection"].connection_key, kwargs.get("wire_id")))
        made.append((key, kwargs["connection"].connection_key))
        raise DENIED if (key, kwargs["connection"].connection_key) in refused else RuntimeError("not expected")

    with ExitStack() as stack:
        _enter_registry(stack, models, anthropic_on)
        stack.enter_context(patch("shared.llm_client.breaker.enabled", return_value=False))
        stack.enter_context(patch("shared.llm_client.client.get_stage_config_sync", return_value=cfg))
        stack.enter_context(patch("shared.llm_client.client.random.random", return_value=1.0))
        stack.enter_context(patch("shared.llm_client.client.stream_sink.delta_callback", return_value=None))
        stack.enter_context(patch("shared.llm_client.client.log_gateway_billed_failure"))
        stack.enter_context(patch("shared.llm_client.bedrock_satellite.get_satellite_client",
                                  side_effect=lambda service="bedrock-runtime", account="acc3": runtime(account)))

        def session_for(account="acc3"):
            s = MagicMock()
            s.client.return_value = runtime(account)
            return s
        stack.enter_context(patch("shared.llm_client.bedrock_satellite._get_satellite_session",
                                  side_effect=session_for))
        stack.enter_context(patch("shared.llm_client.bedrock_satellite.native_runtime",
                                  side_effect=lambda conn, **_: runtime(conn.connection_key)))
        stack.enter_context(patch("shared.llm_client.adapters.anthropic_messages.send", side_effect=anthropic_send))
        with patch("shared.llm_client.client.boto3"), patch("shared.llm_client.client.openai"):
            client = LLMClient()
        client._openai = MagicMock()

        def openai_create(**kwargs):
            made.append((wires.get(("openai-direct", kwargs.get("model")), kwargs.get("model")), "openai-direct"))
            return MagicMock(choices=[MagicMock(message=MagicMock(content="ok"), finish_reason="stop")],
                             usage=MagicMock(prompt_tokens=10, completion_tokens=2, prompt_tokens_details=None))
        client._openai.chat.completions.create.side_effect = openai_create
        client._bedrock = runtime("acc2")
        client.generate(LLMRequest(system_prompt="s", user_prompt="u", stage=STAGE))
    return made, f"{made[-1][0]}@{made[-1][1]}"


def test_the_live_case_a_refused_primary_is_not_named_first():
    ev = _evidence(_dep("haiku-5-5", state="access_denied", probed=NOW - timedelta(hours=3)))
    out = _preview(ev)
    assert "error" not in out, out
    assert out["called_first"] == "haiku-5-5@acc3"                 # the gateway still tries it (half-open)
    assert out["first"] == "haiku-4-5@acc3"                        # ... and is served by the next model
    assert out["skipped"] == [{"model": "haiku-5-5", "connection": "acc3", "reason": "refused (access_denied)"}]
    primary = out["attempts"][0]
    assert primary["model"] == "haiku-5-5" and primary["status"] == "refused"
    assert primary["refused"] == [{"connection": "acc3", "reason": "refused (access_denied)"}]
    assert primary["reason"] == "refused: acc3 (refused (access_denied))"


def test_without_evidence_the_old_answer_stays():
    out = _preview(None)
    assert out["first"] == out["called_first"] == "haiku-5-5@acc3"
    assert out["skipped"] == []


@pytest.mark.parametrize("refused", [
    {("haiku-5-5", "acc3")},
    {("haiku-5-5", "acc3"), ("haiku-4-5", "acc3")},
    {("haiku-5-5", "acc3"), ("haiku-4-5", "acc3"), ("sonnet-4-6", "acc3")},
    set(),
])
def test_the_preview_names_the_deployment_the_gateway_is_served_by(refused):
    ev = _evidence(*[_dep(m, c, state="access_denied", probed=NOW - timedelta(hours=1)) for m, c in refused])
    out = _preview(ev)
    made, served = _gateway(refused)
    assert out["first"] == served, (out, made)
    assert f"{made[0][0]}@{made[0][1]}" == out["called_first"]      # the gateway's first call, refused or not
    assert [f"{s['model']}@{s['connection']}" for s in out["skipped"]] == [f"{m}@{c}" for m, c in made[:-1]]


def test_a_refused_deployment_of_a_model_moves_first_to_its_next_deployment():
    """Anthropic API on: Haiku 5.5 refused on acc3 is served by the same model on the Anthropic API (model first)."""
    ev = _evidence(_dep("haiku-5-5", state="access_denied", probed=NOW - timedelta(hours=1)))
    out = _preview(ev, anthropic_on=True)
    primary = out["attempts"][0]
    assert primary["status"] == "call" and primary["connection"] == "anthropic-direct"
    assert primary["deployments"] == ["acc3", "anthropic-direct"]
    assert primary["refused"] == [{"connection": "acc3", "reason": "refused (access_denied)"}]
    assert out["first"] == "haiku-5-5@anthropic-direct" and out["called_first"] == "haiku-5-5@acc3"
    assert out["skipped"] == [{"model": "haiku-5-5", "connection": "acc3", "reason": "refused (access_denied)"}]


def test_every_model_refused_leaves_no_first():
    ev = _evidence(*[_dep(m, "acc3", state="access_denied", probed=NOW) for m in ("haiku-5-5", "haiku-4-5",
                                                                                  "sonnet-4-6")],
                   _dep("gpt-6-luna-openai", "openai-direct", state="quota_exhausted", probed=NOW))
    out = _preview(ev)
    assert out["first"] is None and out["called_first"] == "haiku-5-5@acc3"
    assert [s["model"] for s in out["skipped"]] == ["haiku-5-5", "haiku-4-5", "sonnet-4-6", "gpt-6-luna-openai"]
    assert all(a["status"] == "refused" for a in out["attempts"])


# ── the Route control preview carries it (api/services/llm_route_control.py) ────────────────────────────────────────

def test_the_route_control_preview_resolves_with_the_world_evidence_and_shows_the_skipped(monkeypatch):
    import asyncio

    from api.services import llm_route_control as rcs
    from tests.unit.test_aa1042_route_control import DEPS, STAGES, _route, _snap, _world

    seen: list = []
    skipped = [{"model": "zz-claude", "connection": "acc3", "reason": "refused (access_denied)"}]

    def fake(rows, priorities, order_mode, evidence=None):
        seen.append(evidence)
        return {r["stage"]: {"first": "zz-gpt@acc3", "skipped": skipped if r["stage"] == "zz_writer" else [],
                             "attempts": [{"model": "zz-claude", "scope": "stage", "status": "refused",
                                           "reason": "refused: acc3 (refused (access_denied))",
                                           "refused": [{"connection": "acc3", "reason": "refused (access_denied)"}],
                                           "deployments": ["acc3"]}]}
                for r in rows if r.get("is_active") is not False}
    monkeypatch.setattr(rcs, "_resolve_all", fake)
    monkeypatch.setattr(rcs, "chain_problems", lambda row, chain, models, reach: [])
    live = _snap(routes={"zz_writer": _route("zz-claude", "zz-gpt"), "zz_judge": _route("zz-gpt")})
    world = _world(STAGES, live)
    world.evidence = "the evidence read with the world"
    target, errors = rcs.build_target(live, order_mode="aws_first", deployments=DEPS, stages={"zz_writer", "zz_judge"})
    out = asyncio.run(rcs.preview_world(world, target, errors))
    assert seen == [world.evidence, world.evidence]                   # now and the proposal: the same refusals
    writer = next(s for s in out["stages"] if s["stage"] == "zz_writer")
    assert writer["first"] == "zz-gpt@acc3" and writer["skipped"] == skipped
    assert writer["attempts"][0]["status"] == "refused"
    assert writer["attempts"][0]["refused"] == [{"connection": "acc3", "reason": "refused (access_denied)"}]
    assert "deployments" not in writer["attempts"][0]                 # the preview keeps its short attempt shape
