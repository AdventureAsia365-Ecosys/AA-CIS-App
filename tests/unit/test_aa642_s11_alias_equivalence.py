"""AA-642 S11 prep (AA-1018 part 2) — the legacy stages reach the same wire ids through their aliases. NO flip.

S11 moves the legacy ``haiku`` / ``sonnet`` stage keys off the hard-coded ``_legacy_chain`` onto the alias path
(``shared.llm_model_alias``, migration 307, behind ``LLM_LEGACY_ALIASES``). Before anyone switches it on, this proves
that for every SAFE_DEFAULTS stage whose primary model is a legacy key, the alias path sends the very same provider
requests (API, account, inference profile, in order) and returns the very same reply as the chain, when everything
answers and when every account is down. It changes nothing: the switch stays off by default (asserted), no route row
is touched, no model is called (fake providers, S3 World).

The alias rows and the legacy deployment rows the World uses are read from migrations 307 and 304 themselves, so the
test follows the data Dev runs on.
"""
import re
from pathlib import Path
from unittest.mock import patch

import pytest

from shared.llm_client import client as client_mod
from shared.llm_client.client import LLMClient
from shared.llm_client.deployments import ModelAlias
from shared.llm_client.role_config import SAFE_DEFAULTS
from tests.unit.test_aa1002_s3_deployment_routing import SEED_ALIASES, SEED_DEPS, World

MIGRATIONS = Path(__file__).resolve().parents[2] / "api" / "migrations"
LEGACY = ("haiku", "sonnet")
LEGACY_STAGES = sorted(k for k, c in SAFE_DEFAULTS.items() if c.model_id in LEGACY)


def test_the_legacy_stage_set_is_the_seventeen_of_the_plan():
    # AA-1024 A2 (migration 335): + segment_research / segment_research_batch, which called the legacy haiku through
    # model_tier before they had a stage (same calls; S11 now covers them too).
    assert len(LEGACY_STAGES) == 19, LEGACY_STAGES
    assert {"segment_research", "segment_research_batch"} <= set(LEGACY_STAGES)


def test_the_switch_is_off_by_default(monkeypatch):
    monkeypatch.delenv("LLM_LEGACY_ALIASES", raising=False)
    assert client_mod.legacy_aliases_enabled() is False


def test_the_world_uses_the_migration_rows():
    text = (MIGRATIONS / "307_llm_model_alias.sql").read_text(encoding="utf-8")
    rows = re.findall(r"\('(haiku|sonnet)',\s*(\d+),\s*'([a-z0-9.-]+)'", text)
    assert {ModelAlias(a, int(p), m) for a, p, m in rows} == set(SEED_ALIASES) and len(rows) == len(SEED_ALIASES)
    seed304 = (MIGRATIONS / "304_llm_connection_deployment.sql").read_text(encoding="utf-8")
    legacy = set(re.findall(r"\('(haiku|sonnet)',\s*'(acc3|acc2)',\s*'([^']+)',\s*(\d+),\s*(true|false)", seed304))
    world = {(d.model_key, d.connection_key, d.wire_id, str(d.priority), str(d.enabled).lower())
             for d in SEED_DEPS if d.model_key in LEGACY}
    enabled = {r for r in legacy if r[4] == "true"}          # a disabled row is never on a path
    assert enabled == {r for r in world if r[4] == "true"} and len(enabled) == 2


@pytest.fixture
def world(monkeypatch):
    from shared.llm_client import catalog, vendor_route
    hook = vendor_route.circuit_open
    w = World(monkeypatch)
    yield w
    w.reset()
    catalog.invalidate()
    vendor_route.circuit_open = hook


def _run(world, monkeypatch, stage, aliases, fail):
    monkeypatch.setenv("LLM_LEGACY_ALIASES", aliases)
    cfg = SAFE_DEFAULTS[stage]
    monkeypatch.setattr("shared.llm_client.client.get_stage_config_sync", lambda s: cfg)
    world.calls.clear()
    world.fail = {"acc3": RuntimeError("acc3 outage")} if fail else {}
    world.reset()
    try:
        r = world.generate(stage=stage)
        out = (r.content, r.model_used, r.provider, r.satellite_account, r.fallback_used, r.cost_usd,
               r.input_tokens, r.output_tokens, r.stop_reason)
    except Exception as e:  # noqa: BLE001
        out = (type(e).__name__,)
    return list(world.calls), out


@pytest.mark.parametrize("fail", [False, True], ids=["answers", "all_down"])
@pytest.mark.parametrize("stage", LEGACY_STAGES)
def test_the_alias_path_sends_what_the_chain_sends(world, monkeypatch, stage, fail):
    chain = _run(world, monkeypatch, stage, "off", fail)
    with patch.object(LLMClient, "_legacy_chain", side_effect=AssertionError("legacy chain used")):
        alias = _run(world, monkeypatch, stage, "on", fail)
    assert alias == chain
    assert chain[0], "the stage made no provider call"
    wires = [c[2] for c in chain[0]]
    assert all(w.endswith(("global.anthropic.claude-sonnet-4-6", "global.anthropic.claude-haiku-4-5-20251001-v1:0"))
               for w in wires if "anthropic" in w), wires
