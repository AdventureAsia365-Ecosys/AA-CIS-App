"""AA-1002 (gateway slice S3) — no Dev stage route changes its provider calls.

For each of the 37 stage routes of Dev (``snapshots/aa1002_s3_routes_fixture.json``, from the root
``docs/generated/llm-routes.snapshot.json`` of 29/10) a plain and a structured request (an embed call for an embed
stage) is sent with every provider failing, so the whole route is walked; ``aa1002_s3_route_capture.py`` records each
provider attempt in order (API, account / connection, wire model id) and the error class the caller got.

The golden file ``snapshots/aa1002_s3_pre_s3_route_calls.json`` was captured by that script on the pre-S3 checkout
(origin/main ec0dd46b). S3 code must give exactly the same calls under every registry state migrations 304 / 306 / 307
produce — connections acc3, acc2, openai-direct on, anthropic-direct off or ON (the S2 admin switch), the legacy aliases
— with each switch in each position: LLM_DEPLOYMENT_ROUTING default (on) / off, LLM_LEGACY_ALIASES default (off) / on.
"""
import json
from pathlib import Path

import pytest

from tests.unit.aa1002_s3_route_capture import capture, load_fixture

GOLDEN = Path(__file__).resolve().parent / "snapshots" / "aa1002_s3_pre_s3_route_calls.json"

CASES = {
    "missing_registry": ("missing", {}),
    "seed": ("seed", {}),
    "seed_anthropic_on": ("seed_anthropic_on", {}),
    "seed_aliases_on": ("seed", {"LLM_LEGACY_ALIASES": "on"}),
    "seed_anthropic_on_aliases_on": ("seed_anthropic_on", {"LLM_LEGACY_ALIASES": "on"}),
    "seed_routing_off": ("seed", {"LLM_DEPLOYMENT_ROUTING": "off"}),
    "seed_routing_off_aliases_on": ("seed", {"LLM_DEPLOYMENT_ROUTING": "off", "LLM_LEGACY_ALIASES": "on"}),
    "missing_registry_aliases_on": ("missing", {"LLM_LEGACY_ALIASES": "on"}),
}


@pytest.fixture(scope="module")
def golden():
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


def test_the_golden_covers_every_dev_stage(golden):
    stages = {r["stage"] for r in load_fixture()["routes"]}
    assert set(golden) == stages and len(stages) == 37
    # the snapshot walks real routes: a legacy chain, a judge with its OpenAI last resort, an embed fallback
    assert [c[0] for c in golden["s1_judge"]["plain"]["calls"]] == ["converse", "converse", "openai"]
    assert [c[1] for c in golden["f10_embed"]["plain"]["calls"]] == ["acc2", "acc3"]
    assert len(golden["a3_place_locate"]["plain"]["calls"]) == 3          # sonnet -> haiku, then haiku


@pytest.mark.parametrize("case", sorted(CASES))
def test_every_stage_route_makes_the_same_calls_as_before_s3(case, golden):
    registry, env = CASES[case]
    got = json.loads(json.dumps(capture(registry, env)))
    diffs = [f"{stage}/{variant}: {got[stage][variant]} != {golden[stage][variant]}"
             for stage in sorted(golden) for variant in ("plain", "structured")
             if got[stage][variant] != golden[stage][variant]]
    assert not diffs, "\n".join(diffs[:10])
