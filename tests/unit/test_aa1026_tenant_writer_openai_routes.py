"""AA-1026 (tech lead S264) — what the gateway calls for every Dev stage once migration 344 has opened the six tenant
writer stages to the OpenAI API as their last fallback.

The routes are ``snapshots/aa1026_routes_after_344.json`` (the post-339 routes with the six tenant writers on
``[<Anthropic primary>, gpt-6-luna-openai]`` and both data scopes; ``tests/integration/test_aa1026_migration_344.py``
proves the file produces them on real Postgres). Each route is walked through ``LLMClient.generate`` with every
provider failing (``aa1002_s3_route_capture``, the harness of ``test_aa1026_acc2_fallback_routes``), with the
Anthropic API connection off and ON. No provider, AWS or database is reached.

What this proves:
- a tenant writer first tries its primary on acc3, unchanged, then (only when switched on) the same model on the
  Anthropic API, and the OpenAI API (GPT-6 Luna on openai-direct) last: no AWS call after a vendor call;
- before 344 the same stage never left AWS (acc3 calls only), so tenant content reaches a vendor API only through 344;
- every other stage, TripPlanner included, makes exactly the calls it made after migration 339.
"""
import json
from pathlib import Path

import pytest

from tests.unit import aa1002_s3_route_capture as rc
from tests.unit.test_aa1026_acc2_fallback_routes import BEDROCK_APIS, _capture, _fixture

SNAPSHOTS = Path(__file__).resolve().parent / "snapshots"
AFTER_339 = SNAPSHOTS / "aa1026_routes_after_339.json"
AFTER_344 = SNAPSHOTS / "aa1026_routes_after_344.json"
TENANT_WRITERS = ("t2_flag_fix", "t2_generate", "t2_itinerary_nudge", "t8_angle_gen", "t9_repair", "t9_write")
OPENAI_HOP = "gpt-6-luna-openai"


def _routes(path: Path) -> dict:
    return {r["stage"]: r for r in json.loads(path.read_text(encoding="utf-8"))["routes"]}


_CALLS: dict = {}


@pytest.fixture
def calls() -> dict:
    if not _CALLS:
        _CALLS["339"] = _capture(_routes(AFTER_339))
        _CALLS["344"] = _capture(_routes(AFTER_344))
    return _CALLS


def test_the_snapshot_changes_exactly_the_six_tenant_writers():
    before, after = _routes(AFTER_339), _routes(AFTER_344)
    assert set(before) == set(after)
    for stage, r in after.items():
        if stage in TENANT_WRITERS:
            assert before[stage]["role"] == "writer" and before[stage]["allowed_scopes"] == ["aws_only"]
            assert r == {**before[stage], "fallback_model_ids": [OPENAI_HOP],
                         "allowed_scopes": ["aws_only", "vendor_direct"]}, stage
        else:
            assert r == before[stage], stage


def test_the_capture_scopes_are_the_ones_on_the_rows():
    """capture(scoped=True) derives migration 311's scopes: a route naming the OpenAI API hop gets both."""
    routes = _routes(AFTER_344)
    assert {s: tuple(r["allowed_scopes"]) for s, r in routes.items()} == rc.stage_scopes(_fixture(routes))


@pytest.mark.parametrize("stage", TENANT_WRITERS)
def test_before_344_a_tenant_writer_never_left_aws(calls, stage):
    for state in ("seed", "seed_anthropic_on"):
        for variant in ("plain", "structured"):
            old = calls["339"][state][stage][variant]["calls"]
            assert old and all(c[0] in BEDROCK_APIS and c[1] == "acc3" for c in old), (state, variant, old)


@pytest.mark.parametrize("stage", TENANT_WRITERS)
def test_a_tenant_writer_tries_acc3_then_the_anthropic_api_then_the_openai_api_last(calls, stage):
    for variant in ("plain", "structured"):
        old = calls["339"]["seed"][stage][variant]["calls"]
        off = calls["344"]["seed"][stage][variant]["calls"]
        on = calls["344"]["seed_anthropic_on"][stage][variant]["calls"]
        primary_acc3 = [c for c in old if c[0] in BEDROCK_APIS and c[1] == "acc3"][:1]
        assert off[:1] == primary_acc3, (variant, off)                     # the primary on acc3 first, unchanged
        assert off[-1][:2] == ["openai", "openai-direct"], (variant, off)  # GPT-6 Luna on the OpenAI API last
        assert all(c[0] in BEDROCK_APIS and c[1] == "acc3" for c in off[:-1]), (variant, off)
        # Anthropic API on: the same model there after acc3, before the OpenAI API
        assert on[0] == off[0] and on[-1] == off[-1], (variant, on)
        middle = on[1:-1]
        assert middle and all(c[0] == "anthropic" and c[1] == "anthropic-direct" for c in middle), (variant, on)
        # no AWS call after a vendor call (T-3: outside only when every AWS path failed)
        for calls_ in (off, on):
            first_vendor = next(i for i, c in enumerate(calls_) if c[0] in ("anthropic", "openai"))
            assert all(c[0] not in BEDROCK_APIS for c in calls_[first_vendor:]), (variant, calls_)


@pytest.mark.parametrize("state", ["seed", "seed_anthropic_on"])
def test_every_other_stage_makes_the_calls_it_made_after_339(calls, state):
    for stage in sorted(set(_routes(AFTER_344)) - set(TENANT_WRITERS)):
        assert calls["344"][state][stage] == calls["339"][state][stage], stage
