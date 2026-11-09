"""AA-1026 I6 — what the gateway calls for every Dev stage once migration 331 has added the acc2 fallbacks.

The routes are ``snapshots/aa1026_routes_after_331.json`` (the post-327 routes with the eight platform judge / validate
picks appended; ``tests/integration/test_aa1026_migration_331.py`` proves the file produces them on real Postgres).
Each route is walked through ``LLMClient.generate`` with every provider failing (``aa1002_s3_route_capture``), under
the registry rows of migrations 304 / 306 plus the five acc2 candidate deployments of migration 324 (enabled), with the
Anthropic API connection off and ON. No provider, AWS or database is reached.

What this proves:
- a changed stage tries every acc3 deployment of its old route first, in the old order, and only then its acc2 model,
  once, through Converse on the acc2 task role (with the Anthropic API on too: these stages are aws_only);
- the failure-domain rule (invariant.check) reports none of the eight as a breach any more;
- every other stage makes exactly the calls it made after migration 327.
"""
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.unit import aa1002_s3_route_capture as rc

SNAPSHOTS = Path(__file__).resolve().parent / "snapshots"
AFTER_327 = SNAPSHOTS / "aa1026_routes_after_327.json"
AFTER_331 = SNAPSHOTS / "aa1026_routes_after_331.json"
PICKS = {"s1_brand_audit": "nova-lite", "s1_source_facts": "nova-lite", "a3_atomize": "nova-lite",
         "a3_place_card": "nova-lite", "a3_place_card_check": "llama4-scout", "a3_place_days": "nova-lite",
         "a3_place_locate": "nova-lite", "a3_place_dedupe_check": "nova-lite"}
BEDROCK_APIS = ("converse", "converse_stream")
# Migration 324's five catalog rows (prices and profiles of that file; family = vendor).
ACC2_MODELS = {
    "nova-lite": ("Amazon Nova Lite", "amazon", "us.amazon.nova-lite-v1:0", 0.077, 0.308, True),
    "nova-2-lite": ("Amazon Nova 2 Lite", "amazon", "us.amazon.nova-2-lite-v1:0", 0.429, 3.351, True),
    "llama4-maverick": ("Meta Llama 4 Maverick", "meta", "us.meta.llama4-maverick-17b-instruct-v1:0", 0.24, 0.97,
                        True),
    "llama4-scout": ("Meta Llama 4 Scout", "meta", "us.meta.llama4-scout-17b-instruct-v1:0", 0.17, 0.66, True),
    "kimi-k3": ("Moonshot Kimi K3", "moonshot", "us.moonshotai.kimi-k3", 3.3, 16.5, False),
}


def _routes(path: Path) -> dict:
    return {r["stage"]: r for r in json.loads(path.read_text(encoding="utf-8"))["routes"]}


def _fixture(routes: dict) -> dict:
    base = rc.load_fixture()
    models = list(base["models"])
    for key, (label, vendor, profile, p_in, p_out, temp) in ACC2_MODELS.items():
        models.append({"api_style": "converse", "bedrock_profile_ids": {"acc2": profile}, "blocked_reason": None,
                       "callable_via": ["llm_client"], "enabled": True, "label": label, "max_output_tokens": None,
                       "model_key": key, "price_cache_read_per_mtok": None, "price_cache_write_per_mtok": None,
                       "price_in_per_mtok": p_in, "price_out_per_mtok": p_out, "provider": "bedrock",
                       "supports_temperature": temp, "vendor": vendor, "wire_model": None})
    return {**base, "models": models,
            "routes": [{k: v for k, v in r.items() if k != "allowed_scopes"} for r in routes.values()]}


def _deployments_with_324(original):
    """304 / 306 rows, with the acc2 rows of the 324 models enabled (324 inserts them enabled, priority 20)."""
    import dataclasses

    def seed(models):
        return [dataclasses.replace(d, enabled=True) if d.model_key in ACC2_MODELS and d.connection_key == "acc2"
                else d for d in original(models)]
    return seed


_CALLS: dict = {}


def _capture(routes: dict) -> dict:
    fixture = _fixture(routes)
    current: list = []

    class Recorder(rc.Recorder):
        def __init__(self):
            super().__init__()
            current[:] = [self]

    with patch.object(rc, "load_fixture", return_value=fixture), \
            patch.object(rc, "seed_deployments", _deployments_with_324(rc.seed_deployments)), \
            patch.object(rc, "Recorder", Recorder), \
            patch("shared.llm_client.bedrock_satellite.native_runtime",
                  side_effect=lambda conn, **_: current[0].runtime(conn.connection_key)):
        return {state: json.loads(json.dumps(rc.capture(state, {}, scoped=True)))
                for state in ("seed", "seed_anthropic_on")}


@pytest.fixture
def calls() -> dict:
    """{"327" | "331": {registry state: capture()}}, captured once inside the first test that needs it (function scope,
    so the conftest's breaker-off and no-DB stubs are active)."""
    if not _CALLS:
        _CALLS["327"] = _capture(_routes(AFTER_327))
        _CALLS["331"] = _capture(_routes(AFTER_331))
    return _CALLS


def test_the_snapshot_appends_exactly_the_picks():
    before, after = _routes(AFTER_327), _routes(AFTER_331)
    assert set(before) == set(after)
    for stage, r in after.items():
        expected = [*before[stage]["fallback_model_ids"], PICKS[stage]] if stage in PICKS \
            else before[stage]["fallback_model_ids"]
        assert r == {**before[stage], "fallback_model_ids": expected}, stage
        if stage in PICKS:
            assert r["role"] in ("judge", "validate") and r["allowed_scopes"] == ["aws_only"]


def test_no_writer_or_tenant_stage_names_an_acc2_model():
    for stage, r in _routes(AFTER_331).items():
        keys = {r["model_id"], r["shadow_model_id"], *r["fallback_model_ids"]}
        if r["role"] == "writer" or stage.startswith(("t2_", "t7_", "t8_", "t9_", "t10_", "tp_")):
            assert not keys & set(ACC2_MODELS), stage


def test_the_capture_scopes_are_the_ones_on_the_rows():
    """capture(scoped=True) derives migration 311's scopes; the acc2 models are aws_only, so they match the rows."""
    routes = _routes(AFTER_331)
    derived = rc.stage_scopes(_fixture(routes))
    assert {s: tuple(r["allowed_scopes"]) for s, r in routes.items()} == derived


@pytest.mark.parametrize("state", ["seed", "seed_anthropic_on"])
@pytest.mark.parametrize("stage", sorted(PICKS))
def test_a_changed_stage_tries_acc3_first_then_its_acc2_model_once(calls, state, stage):
    profile = ACC2_MODELS[PICKS[stage]][2]
    for variant in ("plain", "structured"):
        old = calls["327"][state][stage][variant]["calls"]
        new = calls["331"][state][stage][variant]["calls"]
        assert old and all(c[0] in BEDROCK_APIS and c[1] == "acc3" for c in old), (stage, variant, old)
        assert new[:len(old)] == old                                # the old route, unchanged and first
        tail = new[len(old):]
        assert len(tail) == 1 and tail[0][0] in BEDROCK_APIS and tail[0][1:] == ["acc2", profile], tail
        assert calls["331"][state][stage][variant]["raised"] == calls["327"][state][stage][variant]["raised"]


@pytest.mark.parametrize("state", ["seed", "seed_anthropic_on"])
def test_every_other_stage_makes_the_calls_it_made_after_327(calls, state):
    for stage in sorted(set(_routes(AFTER_331)) - set(PICKS)):
        assert calls["331"][state][stage] == calls["327"][state][stage], stage


def _report(routes: dict) -> dict:
    from shared.llm_client import invariant
    fixture = _fixture(routes)
    models = rc.catalog_models(fixture)
    conns = rc.seed_connections()
    deps = _deployments_with_324(rc.seed_deployments)(models)
    return invariant.check(
        [{**r, "allowed_scopes": r["allowed_scopes"]} for r in routes.values()],
        [{"alias_key": a, "position": p, "model_key": m} for a, p, m in rc.SEED_ALIASES],
        [{"model_key": d.model_key, "connection_key": d.connection_key, "priority": d.priority,
          "enabled": d.enabled} for d in deps],
        [{"connection_key": k, "kind": c.kind, "enabled": c.enabled, "data_scope": c.data_scope,
          "account_id": c.account_id, "account_id_env": c.account_id_env} for k, c in conns.items()])


def test_the_failure_domain_rule_clears_the_eight_stages():
    before = {s["stage"]: s for s in _report(_routes(AFTER_327))["stages"]}
    after = {s["stage"]: s for s in _report(_routes(AFTER_331))["stages"]}
    for stage in PICKS:
        assert before[stage]["status"] == "breach", stage
        assert after[stage]["status"] == "ok" and after[stage]["other_domains"] == ["bedrock:acc2:aws_only"], stage
    # the four tenant judge / validate stages stay in one domain until migration 332 (ADR 0009 decision 14)
    for stage in ("t2_brand_audit", "t10_claims", "t9_faq_answer_fit", "t9_faq_filter"):
        assert after[stage]["status"] == "breach", stage
