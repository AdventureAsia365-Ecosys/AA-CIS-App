"""AA-1026 — what the gateway calls for every Dev stage once migration 335 has moved the routes.

The routes are ``snapshots/aa1026_routes_after_335.json``: the 37 Dev routes of the S3 fixture (29/10) after migration
335, as ``tests/integration/test_aa1026_migration_335.py`` proves by applying the file to real Postgres. Each route is
walked through ``LLMClient.generate`` with every provider failing (``aa1002_s3_route_capture``), so every attempt the
gateway would make is recorded in order, under the registry rows of migrations 304 / 306 with the Anthropic API
connection off (today) and ON (once its key is plugged in). No provider, AWS or database is reached.

What the tech lead decided (S263) and this proves:
- T-4: a moved stage resolves first to the same model family as before (Anthropic stays Anthropic, GPT stays GPT),
  on Bedrock acc3, through the Converse API: never the legacy InvokeModel chain again.
- T-3: the five A-series writers try every AWS deployment of a model before that model's vendor API deployment, and
  the OpenAI API only at the very end; T-stages, tp_* and every other moved stage never leave AWS, even with the
  Anthropic API switched on.
- A stage the migration does not touch makes exactly the calls of the S3 golden file.
"""
import json
from pathlib import Path

import pytest

from tests.unit import aa1002_s3_route_capture as rc

SNAPSHOTS = Path(__file__).resolve().parent / "snapshots"
AFTER = SNAPSHOTS / "aa1026_routes_after_335.json"
GOLDEN = SNAPSHOTS / "aa1002_s3_pre_s3_route_calls.json"
LEGACY = ("haiku", "sonnet")
WRITERS = ("a0_column_map", "s1_flag_fix", "s1_generate", "s1_itinerary_nudge", "supply_content_writer")
VENDOR_APIS = ("anthropic", "openai")
BEDROCK_APIS = ("converse", "converse_stream")


def _before() -> dict:
    return {r["stage"]: r for r in rc.load_fixture()["routes"]}


def _after() -> dict:
    return {r["stage"]: r for r in json.loads(AFTER.read_text(encoding="utf-8"))["routes"]}


def _keys(route: dict) -> list:
    return [route["model_id"], *route["fallback_model_ids"], route["shadow_model_id"]]


MOVED = sorted(s for s, r in _before().items() if any(k in LEGACY for k in _keys(r)))
UNTOUCHED = sorted(s for s in _before() if s not in MOVED and s not in WRITERS)


@pytest.fixture(scope="module")
def models() -> dict:
    return {m["model_key"]: m for m in rc.load_fixture()["models"]}


_CALLS: dict = {}


@pytest.fixture
def calls() -> dict:
    """{registry state: capture()} over the post-335 routes, each stage with its post-335 data scopes. Captured once,
    inside the first test that needs it (function scope, so the conftest's breaker-off and no-DB stubs are active;
    a module fixture would run before them)."""
    if not _CALLS:
        _CALLS.update(_capture())
    return _CALLS


def _capture() -> dict:
    original = rc.load_fixture
    fixture = original()
    fixture = {**fixture, "routes": [{k: v for k, v in r.items() if k != "allowed_scopes"}
                                     for r in _after().values()]}
    rc.load_fixture = lambda: fixture
    try:
        return {state: json.loads(json.dumps(rc.capture(state, {}, scoped=True)))
                for state in ("seed", "seed_anthropic_on")}
    finally:
        rc.load_fixture = original


def test_the_moved_set_is_the_twenty_one_legacy_stages():
    assert len(MOVED) == 21, MOVED
    assert {"tp_compose", "tp_extract", "t9_write", "a3_place_locate", "supply_content_writer"} <= set(MOVED)


def test_the_capture_scopes_are_the_ones_the_migration_writes():
    """capture(scoped=True) derives the scopes with migration 311's rule; after 335 that rule gives exactly the scopes
    the migration wrote (the writers end on a model reached only outside AWS)."""
    fixture = {**rc.load_fixture(), "routes": list(_after().values())}
    derived = rc.stage_scopes(fixture)
    assert {s: tuple(r["allowed_scopes"]) for s, r in _after().items()} == derived
    assert sorted(s for s, v in derived.items() if "vendor_direct" in v) == sorted(
        {*WRITERS, "a3_place_dedupe", "s1_judge", "t10_judge", "t2_judge", "t7_debate_judge",
         "writing_memory_eval", "writing_memory_mine"})


def test_no_route_names_a_legacy_key_any_more():
    assert not [s for s, r in _after().items() if any(k in LEGACY for k in _keys(r))]


@pytest.mark.parametrize("stage", MOVED)
def test_a_moved_stage_keeps_its_model_family_first(stage, models):
    before, after = _before()[stage], _after()[stage]
    assert models[after["model_id"]]["vendor"] == models[before["model_id"]]["vendor"]
    tier = {"haiku": "haiku-5-5", "sonnet": "sonnet-5-5"}
    assert after["model_id"] == tier.get(before["model_id"], before["model_id"])


@pytest.mark.parametrize("state", ["seed", "seed_anthropic_on"])
@pytest.mark.parametrize("stage", MOVED)
def test_a_moved_stage_starts_on_acc3_converse_with_its_primary(calls, models, state, stage):
    route = _after()[stage]
    for variant in ("plain", "structured"):
        got = calls[state][stage][variant]["calls"]
        assert got, (stage, variant)
        assert got[0][0] in BEDROCK_APIS and got[0][1] == "acc3"
        assert got[0][2] == models[route["model_id"]]["bedrock_profile_ids"]["acc3"]
        assert not [c for c in got if c[0] == "invoke_model"]          # never the legacy InvokeModel chain


@pytest.mark.parametrize("state", ["seed", "seed_anthropic_on"])
@pytest.mark.parametrize("stage", [s for s in MOVED if s not in WRITERS])
def test_tenant_tripplanner_and_check_stages_never_leave_aws(calls, state, stage):
    for variant in ("plain", "structured"):
        got = calls[state][stage][variant]["calls"]
        assert all(c[0] in BEDROCK_APIS and c[1] == "acc3" for c in got), (stage, variant, got)


def _wire_of(models, call):
    """The catalog key a recorded call served: an acc3 profile, a Claude API id or an OpenAI wire model."""
    api, _, wire = call
    for key, m in models.items():
        if api in BEDROCK_APIS and m["bedrock_profile_ids"].get("acc3") == wire:
            return key
        if api == "anthropic" and m["model_key"] in ("haiku-5-5", "sonnet-5-5") and wire == f"claude-{key}":
            return key
        if api == "openai" and m["provider"] == "openai" and (m["wire_model"] or key) == wire:
            return key
    raise AssertionError(f"unknown call {call}")


@pytest.mark.parametrize("state", ["seed", "seed_anthropic_on"])
@pytest.mark.parametrize("stage", WRITERS)
def test_writers_try_aws_first_and_the_openai_api_last(calls, models, state, stage):
    route = _after()[stage]
    for variant in ("plain", "structured"):
        got = calls[state][stage][variant]["calls"]
        served = [_wire_of(models, c) for c in got]
        # every model of the route, in route order; per model its acc3 deployment, then its Anthropic API one (only
        # when that connection is on); the OpenAI API model last
        expected = []
        for key in [route["model_id"], *route["fallback_model_ids"]]:
            if key == "gpt-6-luna-openai":
                expected.append(["openai", "openai-direct", "gpt-6-luna"])
                continue
            expected.append([got[0][0], "acc3", models[key]["bedrock_profile_ids"]["acc3"]])
            if state == "seed_anthropic_on":
                expected.append(["anthropic", "anthropic-direct", f"claude-{key}"])
        assert [c[1:] for c in got] == [c[1:] for c in expected], (stage, variant)
        assert served[-1] == "gpt-6-luna-openai"
        first_vendor = min(i for i, c in enumerate(got) if c[0] in VENDOR_APIS)
        assert got[0][0] in BEDROCK_APIS and first_vendor >= 1               # an AWS deployment first
        for i, c in enumerate(got):                                          # per model: AWS before its vendor API
            if c[0] == "anthropic":
                assert any(p[0] in BEDROCK_APIS and served[j] == served[i] for j, p in enumerate(got[:i]))


@pytest.mark.parametrize("stage", UNTOUCHED)
def test_untouched_stages_make_the_golden_calls(calls, stage):
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    for state in ("seed", "seed_anthropic_on"):
        for variant in ("plain", "structured"):
            assert calls[state][stage][variant] == golden[stage][variant], (state, stage, variant)
