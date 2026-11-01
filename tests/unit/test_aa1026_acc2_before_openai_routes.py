"""AA-1026 I6, "AWS first, outside last" — what the gateway calls for every Dev stage once migration 333 has put an acc2
pick right before the OpenAI API hop of the judge / validate routes that end on it.

The routes are ``snapshots/aa1026_routes_after_333.json`` (the post-332 routes with the seven picks inserted;
``tests/integration/test_aa1026_migration_333.py`` proves the file produces them on real Postgres, and covers
``t10_adjudicate``, which the 29/10 Dev snapshot predates). Each route is walked through ``LLMClient.generate`` with
every provider failing (``aa1002_s3_route_capture``, the harness of ``test_aa1026_acc2_fallback_routes``), with the
Anthropic API connection off and ON. No provider, AWS or database is reached.

What this proves:
- a changed stage makes its acc3 calls first, unchanged, then ONE acc2 call on its pick (Converse, acc2 task role),
  and only then the OpenAI API call it made before, which stays the last;
- every other stage makes exactly the calls it made after migration 332;
- the failure-domain rule still reports no changed stage as a breach.
"""
import json
from pathlib import Path

import pytest

from tests.unit import aa1002_s3_route_capture as rc
from tests.unit.test_aa1026_acc2_fallback_routes import ACC2_MODELS, BEDROCK_APIS, _capture, _fixture, _report

SNAPSHOTS = Path(__file__).resolve().parent / "snapshots"
AFTER_332 = SNAPSHOTS / "aa1026_routes_after_332.json"
AFTER_333 = SNAPSHOTS / "aa1026_routes_after_333.json"
PICKS = {"s1_judge": "nova-lite", "t2_judge": "nova-lite", "t7_debate_judge": "nova-lite", "t10_judge": "nova-lite",
         "writing_memory_eval": "nova-lite", "writing_memory_mine": "nova-lite", "a3_place_dedupe": "llama4-scout"}
OPENAI_HOP = "gpt-6-luna-openai"


def _routes(path: Path) -> dict:
    return {r["stage"]: r for r in json.loads(path.read_text(encoding="utf-8"))["routes"]}


_CALLS: dict = {}


@pytest.fixture
def calls() -> dict:
    if not _CALLS:
        _CALLS["332"] = _capture(_routes(AFTER_332))
        _CALLS["333"] = _capture(_routes(AFTER_333))
    return _CALLS


def test_the_snapshot_moves_exactly_the_picks_before_the_openai_api_hop():
    before, after = _routes(AFTER_332), _routes(AFTER_333)
    assert set(before) == set(after)
    for stage, r in after.items():
        f = before[stage]["fallback_model_ids"]
        expected = [*f[:-1], PICKS[stage], f[-1]] if stage in PICKS else f
        assert r == {**before[stage], "fallback_model_ids": expected}, stage
        if stage in PICKS:
            assert f[-1] == OPENAI_HOP and r["role"] in ("judge", "validate")
    # every judge / validate route that reaches the OpenAI API now reaches acc2 first
    for stage, r in after.items():
        if r["role"] in ("judge", "validate") and OPENAI_HOP in r["fallback_model_ids"]:
            f = r["fallback_model_ids"]
            assert any(k in ACC2_MODELS for k in f[:f.index(OPENAI_HOP)]), stage


def test_no_writer_names_an_acc2_model():
    for stage, r in _routes(AFTER_333).items():
        if r["role"] == "writer":
            assert not {r["model_id"], r["shadow_model_id"], *r["fallback_model_ids"]} & set(ACC2_MODELS), stage


def test_the_capture_scopes_are_the_ones_on_the_rows():
    routes = _routes(AFTER_333)
    assert {s: tuple(r["allowed_scopes"]) for s, r in routes.items()} == rc.stage_scopes(_fixture(routes))


@pytest.mark.parametrize("state", ["seed", "seed_anthropic_on"])
@pytest.mark.parametrize("stage", sorted(PICKS))
def test_a_changed_stage_tries_aws_first_then_its_acc2_pick_then_the_openai_api(calls, state, stage):
    profile = ACC2_MODELS[PICKS[stage]][2]
    for variant in ("plain", "structured"):
        old = calls["332"][state][stage][variant]["calls"]
        new = calls["333"][state][stage][variant]["calls"]
        assert old and old[-1][:2] == ["openai", "openai-direct"], (stage, variant, old)
        assert all(c[0] in BEDROCK_APIS and c[1] == "acc3" for c in old[:-1]), (stage, variant, old)
        assert new == [*old[:-1], ["converse", "acc2", profile], old[-1]], (stage, variant, new)
        assert calls["333"][state][stage][variant]["raised"] == calls["332"][state][stage][variant]["raised"]


@pytest.mark.parametrize("state", ["seed", "seed_anthropic_on"])
def test_every_other_stage_makes_the_calls_it_made_after_332(calls, state):
    for stage in sorted(set(_routes(AFTER_333)) - set(PICKS)):
        assert calls["333"][state][stage] == calls["332"][state][stage], stage


def test_the_failure_domain_rule_reports_no_changed_stage():
    after = {s["stage"]: s for s in _report(_routes(AFTER_333))["stages"]}
    for stage in PICKS:
        assert after[stage]["status"] == "ok", stage
        assert "bedrock:acc2:aws_only" in after[stage]["other_domains"], (stage, after[stage])
