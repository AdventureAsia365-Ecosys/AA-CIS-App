"""AA-1026 I6, "AWS first, outside last" — what the gateway calls for every Dev stage once migration 339 has put
llama4-maverick (acc2) right before the OpenAI API hop of a3_place_dedupe, the last judge / validate route that ended
on the OpenAI API with no acc2 hop.

The routes are ``snapshots/aa1026_routes_after_339.json`` (``tests/integration/test_aa1026_migration_339.py`` proves
the file produces them on real Postgres). Each route is walked through ``LLMClient.generate`` with every provider
failing (the harness of ``test_aa1026_acc2_fallback_routes``), with the Anthropic API connection off and ON. No
provider, AWS or database is reached.
"""
import json
from pathlib import Path

import pytest

from tests.unit.test_aa1026_acc2_fallback_routes import ACC2_MODELS, BEDROCK_APIS, _capture, _report

SNAPSHOTS = Path(__file__).resolve().parent / "snapshots"
AFTER_333 = SNAPSHOTS / "aa1026_routes_after_333.json"
AFTER_339 = SNAPSHOTS / "aa1026_routes_after_339.json"
STAGE = "a3_place_dedupe"
CHECK = "a3_place_dedupe_check"
PICK = "llama4-maverick"
OPENAI_HOP = "gpt-6-luna-openai"


def _routes(path: Path) -> dict:
    return {r["stage"]: r for r in json.loads(path.read_text(encoding="utf-8"))["routes"]}


_CALLS: dict = {}


@pytest.fixture
def calls() -> dict:
    if not _CALLS:
        _CALLS["333"] = _capture(_routes(AFTER_333))
        _CALLS["339"] = _capture(_routes(AFTER_339))
    return _CALLS


def test_the_snapshot_moves_only_the_pick_before_the_openai_api_hop():
    before, after = _routes(AFTER_333), _routes(AFTER_339)
    assert set(before) == set(after)
    for stage, r in after.items():
        if stage == STAGE:
            assert before[stage]["fallback_model_ids"] == [OPENAI_HOP]
            assert r == {**before[stage], "fallback_model_ids": [PICK, OPENAI_HOP]}
        else:
            assert r == before[stage], stage


def test_every_judge_validate_route_that_reaches_the_openai_api_reaches_acc2_first():
    for stage, r in _routes(AFTER_339).items():
        if r["role"] in ("judge", "validate") and OPENAI_HOP in r["fallback_model_ids"]:
            f = r["fallback_model_ids"]
            assert any(k in ACC2_MODELS for k in f[:f.index(OPENAI_HOP)]), stage


def test_the_place_pair_keeps_two_makers():
    routes = _routes(AFTER_339)
    family = {k: v[1] for k, v in ACC2_MODELS.items()}
    family.update({"gpt-6-luna": "openai", OPENAI_HOP: "openai", "haiku-5-5": "anthropic"})

    def makers(stage: str) -> set:
        r = routes[stage]
        return {family[k] for k in [r["model_id"], *r["fallback_model_ids"]]}

    assert makers(STAGE) == {"openai", "meta"}
    assert makers(CHECK) == {"anthropic", "amazon"}


@pytest.mark.parametrize("state", ["seed", "seed_anthropic_on"])
def test_a3_place_dedupe_tries_acc3_then_maverick_on_acc2_then_the_openai_api(calls, state):
    profile = ACC2_MODELS[PICK][2]
    for variant in ("plain", "structured"):
        old = calls["333"][state][STAGE][variant]["calls"]
        new = calls["339"][state][STAGE][variant]["calls"]
        assert old and old[-1][:2] == ["openai", "openai-direct"], (variant, old)
        assert all(c[0] in BEDROCK_APIS and c[1] == "acc3" for c in old[:-1]), (variant, old)
        assert new == [*old[:-1], ["converse", "acc2", profile], old[-1]], (variant, new)
        assert calls["339"][state][STAGE][variant]["raised"] == calls["333"][state][STAGE][variant]["raised"]


@pytest.mark.parametrize("state", ["seed", "seed_anthropic_on"])
def test_every_other_stage_makes_the_calls_it_made_after_333(calls, state):
    for stage in sorted(set(_routes(AFTER_339)) - {STAGE}):
        assert calls["339"][state][stage] == calls["333"][state][stage], stage


def test_the_failure_domain_rule_reports_a3_place_dedupe_with_acc2():
    after = {s["stage"]: s for s in _report(_routes(AFTER_339))["stages"]}
    assert after[STAGE]["status"] == "ok"
    assert "bedrock:acc2:aws_only" in after[STAGE]["other_domains"], after[STAGE]
