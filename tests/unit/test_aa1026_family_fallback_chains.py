"""AA-1026 (S267, tech lead: "family fallback chains") — the routes migration 366 decides, the chain rules they keep,
and what the gateway calls for them.

``snapshots/aa1026_routes_before_366.json`` is the live route table of 04/11/2026 (the post-344 snapshot plus the three
rows it predates); ``snapshots/aa1026_routes_after_366.json`` is the same table with the family chains and the S267
data scopes (``tests/integration/test_aa1026_migration_366.py`` proves the migration produces it on real Postgres).
Each route is walked through ``LLMClient.generate`` with every provider failing (``aa1002_s3_route_capture``), with
the snapshot's own ``allowed_scopes``, under the 304 / 306 / 324 registry rows plus migration 366's two catalog rows
(Claude Sonnet 4.6 / Haiku 4.5 on Converse, acc3 only), with the Anthropic API connection off and ON. No provider, AWS
or database is reached.

What this proves:
- only the fallbacks of the decided stages and the scopes of the active text stages change (tech lead S267: "AWS
  preferred, not AWS only", every active writer / judge / validate stage allows aws_only AND vendor_direct; the
  embedding stages and the switched-off rows stay aws_only); primary, shadow and role do not;
- every active route keeps the chain rules (``llm_route_chains.violations``), which the old table broke (no fallback on
  tp_compose / segment_research*, and the text stages T-3 kept on aws_only);
- a changed stage still calls its primary on acc3 first, then the new Claude 4.x rows on acc3 by Converse; the gateway
  runs MODEL FIRST (tech lead S267, root ADR 0009 decision 5 amendment): with the Anthropic API switched on, Sonnet
  5.5 on acc3, then Sonnet 5.5 on the Anthropic API, then Sonnet 4.6 on acc3, ..., the OpenAI API last, within the
  gateway's 6 attempts; with the AWS-first switch on (``LLM_ROUTE_AWS_FIRST``, kept, default off) every AWS call comes
  before any vendor API call; an embedding stage never leaves AWS;
- every stage not changed makes exactly the calls it made before.
"""
import dataclasses
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.unit import aa1002_s3_route_capture as rc
from tests.unit.test_aa1026_acc2_fallback_routes import ACC2_MODELS, BEDROCK_APIS

SNAPSHOTS = Path(__file__).resolve().parent / "snapshots"
BEFORE = SNAPSHOTS / "aa1026_routes_before_366.json"
AFTER = SNAPSHOTS / "aa1026_routes_after_366.json"
OPENAI_HOP = "gpt-6-luna-openai"
# Migration 366's catalog rows: the Converse twins of the legacy keys, on the legacy rows' acc3 profiles.
NEW_MODELS = {
    "sonnet-4-6": ("Claude Sonnet 4.6", "global.anthropic.claude-sonnet-4-6", 3.0, 15.0),
    "haiku-4-5": ("Claude Haiku 4.5", "global.anthropic.claude-haiku-4-5-20251001-v1:0", 1.0, 5.0),
}
LARGE = ["sonnet-4-6", "haiku-4-5"]   # not haiku-5-5: refused with sonnet-5-5, and the 6-attempt cap
SMALL = ["haiku-4-5", "sonnet-4-6"]
BOTH = ["aws_only", "vendor_direct"]
# The decision table (docs/implementation-notes/AA-1026-family-fallback.md): stage -> new fallbacks. S267 (every text
# stage vendor_direct): tp_compose and segment research end on the OpenAI API like the other writers; a validate chain
# keeps ONE 5.x Claude tier (its primary's: the other 5.x tier is refused with it, and with the Anthropic API on a
# second one would pass the 6 attempts per request). Model first (S267 Q2, "openai-direct for a GPT twin if one
# exists"): on every GPT route the OpenAI API row (GPT-6 Luna's vendor twin) sits right behind gpt-6-luna, before the
# next model; a3_atomize / s1_source_facts drop GPT-5.6 Luna for it (refused with GPT-6 Luna, the attempt cap).
DECIDED = {
    **{s: [*LARGE, OPENAI_HOP] for s in ("s1_generate", "supply_content_writer", "t2_generate", "t8_angle_gen",
                                         "t9_write", "tp_compose")},
    **{s: [*SMALL, OPENAI_HOP] for s in ("a0_column_map", "s1_flag_fix", "s1_itinerary_nudge", "t2_flag_fix",
                                         "t2_itinerary_nudge", "t9_repair", "segment_research",
                                         "segment_research_batch")},
    "a3_atomize": [OPENAI_HOP, "haiku-5-5", "haiku-4-5", "nova-lite"],
    "s1_source_facts": [OPENAI_HOP, "haiku-5-5", "haiku-4-5", "nova-lite"],
    "a3_place_card": [OPENAI_HOP, "gpt-5.6-luna", "nova-lite"],
    "t10_claims": [OPENAI_HOP, "gpt-5.6-luna", "nova-lite"],
    "a3_place_card_check": ["haiku-4-5", "sonnet-4-6", "llama4-scout"],
    "a3_place_days": ["haiku-4-5", "sonnet-4-6", "nova-lite"],
    "a3_place_dedupe": [OPENAI_HOP, "gpt-5.6-luna", "llama4-maverick"],
    "a3_place_dedupe_check": ["haiku-4-5", "nova-lite"],
    "a3_place_locate": ["sonnet-4-6", "haiku-4-5", "nova-lite"],
    "t9_faq_answer_fit": ["sonnet-4-6", "haiku-4-5", "nova-lite"],
    "t9_faq_filter": ["haiku-4-5", "nova-lite"],
    **{s: ["gpt-6-luna", OPENAI_HOP, "nova-lite"] for s in ("s1_brand_audit", "t2_brand_audit", "s1_judge", "t2_judge",
                                                           "t7_debate_judge", "t10_judge", "writing_memory_eval",
                                                           "writing_memory_mine")},
    "t10_adjudicate": [OPENAI_HOP, "gpt-5.6-luna", "nova-lite"],
}
GPT_STAGES = {s for s, f in DECIDED.items() if OPENAI_HOP in f and f[-1] != OPENAI_HOP}   # the twin mid-chain


def _text_stage(r: dict) -> bool:
    """An active writer / judge / validate stage: allowed both data scopes since 366 (S267)."""
    return r["is_active"] and r["role"] != "embed"


def _routes(path: Path) -> dict:
    return {r["stage"]: r for r in json.loads(path.read_text(encoding="utf-8"))["routes"]}


def _model_rows() -> list[dict]:
    rows = list(rc.load_fixture()["models"])
    for key, (label, vendor, profile, p_in, p_out, temp) in ACC2_MODELS.items():
        rows.append({"api_style": "converse", "bedrock_profile_ids": {"acc2": profile}, "blocked_reason": None,
                     "callable_via": ["llm_client"], "enabled": True, "label": label, "max_output_tokens": None,
                     "model_key": key, "price_cache_read_per_mtok": None, "price_cache_write_per_mtok": None,
                     "price_in_per_mtok": p_in, "price_out_per_mtok": p_out, "provider": "bedrock",
                     "supports_temperature": temp, "vendor": vendor, "wire_model": None})
    for key, (label, profile, p_in, p_out) in NEW_MODELS.items():
        rows.append({"api_style": "converse", "bedrock_profile_ids": {"acc3": profile}, "blocked_reason": None,
                     "callable_via": ["llm_client"], "enabled": True, "label": label, "max_output_tokens": None,
                     "model_key": key, "price_cache_read_per_mtok": None, "price_cache_write_per_mtok": None,
                     "price_in_per_mtok": p_in, "price_out_per_mtok": p_out, "provider": "bedrock",
                     "supports_temperature": True, "vendor": "anthropic", "wire_model": None})
    return rows


def _fixture(routes: dict) -> dict:
    return {**rc.load_fixture(), "models": _model_rows(),
            "routes": [{k: v for k, v in r.items() if k not in ("allowed_scopes", "is_active")}
                       for r in routes.values()]}


def _seed(original):
    """304 / 306 rows + the acc2 rows of 324 enabled + 366's rows: acc3 only (306 ran before them, so no Anthropic API
    deployment)."""
    def seed(models):
        return [dataclasses.replace(d, enabled=True) if d.model_key in ACC2_MODELS and d.connection_key == "acc2"
                else d for d in original(models)
                if not (d.model_key in NEW_MODELS and d.connection_key != "acc3")]
    return seed


def _scopes_of(routes: dict):
    """The snapshot's own allowed_scopes (rc.stage_scopes derives 311's backfill from the route, which 366 widened)."""
    return lambda fixture: {s: tuple(r["allowed_scopes"]) for s, r in routes.items()}


def _capture(routes: dict, env: dict) -> dict:
    fixture = _fixture(routes)
    current: list = []

    class Recorder(rc.Recorder):
        def __init__(self):
            super().__init__()
            current[:] = [self]

    with patch.object(rc, "load_fixture", return_value=fixture), \
            patch.object(rc, "seed_deployments", _seed(rc.seed_deployments)), \
            patch.object(rc, "stage_scopes", _scopes_of(routes)), \
            patch.object(rc, "Recorder", Recorder), \
            patch("shared.llm_client.bedrock_satellite.native_runtime",
                  side_effect=lambda conn, **_: current[0].runtime(conn.connection_key)):
        return {state: json.loads(json.dumps(rc.capture(state, env, scoped=True)))
                for state in ("seed", "seed_anthropic_on")}


_CALLS: dict = {}


@pytest.fixture
def calls() -> dict:
    if not _CALLS:
        _CALLS["before"] = _capture(_routes(BEFORE), {})
        _CALLS["after"] = _capture(_routes(AFTER), {})
        _CALLS["after_aws_first"] = _capture(_routes(AFTER), {"LLM_ROUTE_AWS_FIRST": "on"})
    return _CALLS


def _models():
    return list(rc.catalog_models({"models": _model_rows()}).values())


def _reach_and_acc2_only():
    models = rc.catalog_models({"models": _model_rows()})
    conns = rc.seed_connections()
    reach: dict = {}
    where: dict = {}
    for d in _seed(rc.seed_deployments)(models):
        reach.setdefault(d.model_key, set()).add(conns[d.connection_key].data_scope)
        where.setdefault(d.model_key, set()).add(d.connection_key)
    acc2_only = {k for k, c in where.items() if c == {"acc2"} and models[k].api_style != "embed"}
    return {k: frozenset(v) for k, v in reach.items()}, acc2_only


def _deployment_scopes() -> dict:
    """Model key -> the data scope of each deployment row (the acc2 rows of the legacy keys included, as in 304)."""
    models = rc.catalog_models({"models": _model_rows()})
    conns = rc.seed_connections()
    out: dict = {}
    for d in _seed(rc.seed_deployments)(models):
        out.setdefault(d.model_key, []).append(conns[d.connection_key].data_scope)
    return out


# ── the decided table ──────────────────────────────────────────────────────────────────────────────────────────────

def test_only_the_decided_fallbacks_and_the_text_stage_scopes_change():
    before, after = _routes(BEFORE), _routes(AFTER)
    assert set(before) == set(after)
    for stage, r in after.items():
        expected = DECIDED.get(stage, before[stage]["fallback_model_ids"])
        scopes = BOTH if _text_stage(before[stage]) else before[stage]["allowed_scopes"]
        assert r == {**before[stage], "fallback_model_ids": expected, "allowed_scopes": scopes}, stage
    assert all(after[s]["is_active"] for s in DECIDED)                      # switched-off routes are not touched
    # embeddings and switched-off rows stay inside AWS (no vendor embedding deployment exists)
    assert {s for s, r in after.items() if r["allowed_scopes"] == ["aws_only"]} == {
        "a3_place_embed", "f10_embed", "tp_search_embed", "tp_component_embed", "s1_atom_writer", "tp_extract"}


def test_every_decided_chain_keeps_the_stage_primary_and_adds_the_working_siblings():
    after = _routes(AFTER)
    for stage, fallbacks in DECIDED.items():
        keys = [after[stage]["model_id"], *fallbacks]
        assert len(keys) == len(set(keys)), stage
        for newer, older in (("sonnet-5-5", "sonnet-4-6"), ("haiku-5-5", "haiku-4-5"), ("gpt-6-luna", OPENAI_HOP)):
            if newer in keys:
                assert older in keys and keys.index(older) == keys.index(newer) + 1, (stage, keys)
        assert "sonnet" not in keys and "haiku" not in keys                 # never a legacy key again (327 T-4)


def test_the_chain_rules_hold_after_366_and_name_the_gaps_before():
    from api.services.llm_route_chains import violations
    reach, acc2_only = _reach_and_acc2_only()
    after = violations(_routes(AFTER).values(), _models(), reach, acc2_only, _deployment_scopes())
    assert after == []
    before = violations(_routes(BEFORE).values(), _models(), reach, acc2_only)
    aws_only_text = {s for s, r in _routes(BEFORE).items()
                     if _text_stage(r) and "vendor_direct" not in r["allowed_scopes"]}
    assert len(aws_only_text) == 15
    assert {(s, rule) for s, rule, _ in before} == {
        ("segment_research", "no_fallback"), ("segment_research_batch", "no_fallback"), ("tp_compose", "no_fallback"),
        *((s, "aws_only_text_stage") for s in aws_only_text)}


@pytest.mark.parametrize("stage,change,rule", [
    ("s1_judge", {"fallback_model_ids": ["gpt-6-luna", "haiku-4-5", "nova-lite", OPENAI_HOP]}, "judge_writer_family"),
    ("t9_write", {"fallback_model_ids": ["sonnet-4-6", "nova-lite", OPENAI_HOP]}, "writer_family"),
    ("t9_write", {"allowed_scopes": ["aws_only"]}, "aws_only_text_stage"),
    ("a3_place_locate", {"allowed_scopes": ["aws_only"]}, "aws_only_text_stage"),
    ("s1_judge", {"fallback_model_ids": ["nova-lite", OPENAI_HOP, "gpt-6-luna"]}, "vendor_not_last"),
    ("a3_place_card", {"fallback_model_ids": ["gpt-5.6-luna", OPENAI_HOP, "nova-lite"]}, "vendor_not_last"),
    ("a3_place_card_check", {"fallback_model_ids": ["haiku-4-5", "gpt-6-luna", "llama4-scout"]},
     "check_pair_family"),
    ("tp_compose", {"fallback_model_ids": []}, "no_fallback"),
    ("s1_generate", {"fallback_model_ids": ["sonnet-4-6", "haiku-5-5", "haiku-4-5", OPENAI_HOP]}, "attempt_cap"),
    ("a3_atomize", {"fallback_model_ids": [OPENAI_HOP, "gpt-5.6-luna", "haiku-5-5", "haiku-4-5", "nova-lite"]},
     "attempt_cap"),
    ("a3_place_locate", {"fallback_model_ids": ["sonnet-4-6", "haiku-5-5", "haiku-4-5", "nova-lite"]}, "attempt_cap"),
], ids=["judge_claude", "writer_acc2_model", "aws_only_writer", "aws_only_validate", "vendor_mid_chain",
        "vendor_not_behind_its_twin", "check_pair",
        "no_fallback", "attempt_cap", "attempt_cap_validate_openai", "attempt_cap_two_5x_tiers"])
def test_each_rule_catches_its_break(stage, change, rule):
    from api.services.llm_route_chains import violations
    reach, acc2_only = _reach_and_acc2_only()
    routes = _routes(AFTER)
    routes[stage] = {**routes[stage], **change}
    found = violations(routes.values(), _models(), reach, acc2_only, _deployment_scopes())
    assert rule in {r for s, r, _ in found if s == stage}


def test_an_embedding_stage_stays_aws_only_without_a_violation():
    from api.services.llm_route_chains import violations
    reach, acc2_only = _reach_and_acc2_only()
    after = _routes(AFTER)
    assert after["f10_embed"]["allowed_scopes"] == ["aws_only"]
    assert not [v for v in violations(after.values(), _models(), reach, acc2_only, _deployment_scopes())
                if v[0] in ("f10_embed", "a3_place_embed", "tp_search_embed")]


def test_the_openai_api_row_is_gpt_6_luna_s_vendor_twin():
    """S267 Q2 ("openai-direct for a GPT twin if one exists"): the catalog says gpt-6-luna-openai is the same model as
    gpt-6-luna (wire model gpt-6-luna vs the acc3 profile global.openai.gpt-6-luna); no other vendor-only row is."""
    from shared.llm_client import data_scope
    reach, _ = _reach_and_acc2_only()
    assert data_scope.vendor_twins(_models(), reach) == {OPENAI_HOP: frozenset({"gpt-6-luna"})}
    chain = ["gpt-6-luna", OPENAI_HOP, "gpt-5.6-luna"]
    assert data_scope.in_twin_slot(chain, 1, {OPENAI_HOP: frozenset({"gpt-6-luna"})})
    assert not data_scope.in_twin_slot(["gpt-5.6-luna", OPENAI_HOP], 1, {OPENAI_HOP: frozenset({"gpt-6-luna"})})
    assert not data_scope.in_twin_slot([OPENAI_HOP, "gpt-6-luna"], 0, {OPENAI_HOP: frozenset({"gpt-6-luna"})})
    assert data_scope.base_model_id("us.anthropic.claude-haiku-4-5-20251001-v1:0", "anthropic") == \
        "claude-haiku-4-5-20251001"
    assert data_scope.base_model_id("gpt-5.6-luna", "openai") == "gpt-5.6-luna"     # a dot in the name is kept


def test_every_route_stage_maps_to_a_product_and_a_ledger_app():
    """S267 (tech lead: ONE Anthropic API key, per-product spend tracked in our ledger): every stage, switched off or
    not, names its product, and the ledger app the Spend > By app page groups it under."""
    from api.services.llm_route_chains import ledger_app, product_of
    products = {s: product_of(s) for s in _routes(AFTER)}
    assert not [s for s, p in products.items() if p is None]
    assert {p for p in products.values()} == {"catalogue", "tenant_content", "tripplanner", "booking", "shared"}
    assert products["s1_generate"] == "catalogue" and products["t2_generate"] == "tenant_content"
    assert products["supply_content_writer"] == "booking" and products["writing_memory_eval"] == "shared"
    assert {s for s in products if ledger_app(s) == "tripplanner"} == {
        "tp_compose", "tp_extract", "tp_search_embed", "tp_component_embed"}
    assert all(ledger_app(s) == "cis" for s in products if not s.startswith("tp_"))
    assert product_of("crm_draft") is None                                   # the CRM has its own gateway


def test_a_switched_off_route_is_not_checked():
    from api.services.llm_route_chains import violations
    reach, acc2_only = _reach_and_acc2_only()
    assert not [v for v in violations(_routes(AFTER).values(), _models(), reach, acc2_only)
                if v[0] in ("tp_extract", "s1_atom_writer", "tp_component_embed")]


# ── what the gateway calls ─────────────────────────────────────────────────────────────────────────────────────────

def _is_aws(call) -> bool:
    return call[0] in (*BEDROCK_APIS, "invoke_model") and call[1] in ("acc3", "acc2")


def _same_model(vendor_call, aws_call) -> bool:
    """The vendor API call is the same model as the acc3 call (wire "claude-sonnet-5-5" vs
    "global.anthropic.claude-sonnet-5-5")."""
    return _is_aws(aws_call) and aws_call[1] == "acc3" and aws_call[2].endswith("." + vendor_call[2])


@pytest.mark.parametrize("state", ["seed", "seed_anthropic_on"])
@pytest.mark.parametrize("stage", sorted(DECIDED))
def test_a_changed_stage_calls_its_primary_first_then_each_model_on_every_deployment(calls, state, stage):
    """Model first (S267): a vendor API call is either the same model as the acc3 call right before it (Sonnet 5.5 on
    the Anthropic API, GPT-6 Luna on the OpenAI API through its twin row), or the route's vendor-only last rung (the
    OpenAI API); the new 4.x rows are called on acc3 by Converse."""
    route = _routes(AFTER)[stage]
    reach, _ = _reach_and_acc2_only()
    vendor_last = route["fallback_model_ids"][-1] == OPENAI_HOP
    for variant in ("plain", "structured"):
        old = calls["before"][state][stage][variant]["calls"]
        new = calls["after"][state][stage][variant]["calls"]
        assert new[:1] == old[:1], (variant, new)                       # the primary on acc3, unchanged and first
        for i, c in enumerate(new):
            if not _is_aws(c):
                assert (i > 0 and _same_model(c, new[i - 1])) or (i == len(new) - 1 and c[:2] == ["openai",
                                                                                                "openai-direct"]), \
                    (variant, new)
        for key in route["fallback_model_ids"]:
            if key in NEW_MODELS:
                assert ["converse", "acc3", NEW_MODELS[key][1]] in [c[:3] for c in new] or \
                    ["converse_stream", "acc3", NEW_MODELS[key][1]] in [c[:3] for c in new], (key, variant, new)
        assert route["allowed_scopes"] == BOTH                              # S267: every text stage
        if vendor_last:
            assert new[-1][:2] == ["openai", "openai-direct"], (variant, new)     # the OpenAI API last
        else:
            assert _is_aws(new[-1]), (variant, new)                         # acc2 last (the other failure domain)
        if state == "seed":                                                 # Anthropic API off: no Anthropic call
            assert all(_is_aws(c) or c[:2] == ["openai", "openai-direct"] for c in new), (variant, new)
        if stage in GPT_STAGES:                                             # GPT-6 Luna, then its OpenAI API twin
            i = next(i for i, c in enumerate(new) if c[:2] == ["openai", "openai-direct"])
            assert new[i - 1][1:3] == ["acc3", "global.openai.gpt-6-luna"] and new[i][2] == "gpt-6-luna", (variant, new)
        assert not any(c[0] == "anthropic" for c in new) or any(
            reach.get(k) == frozenset(BOTH) for k in [route["model_id"], *route["fallback_model_ids"]])
        assert len(new) >= len(old)
        assert len(new) <= 6                                                # errors.MAX_CHAIN_ATTEMPTS: last rung runs
        # the AWS-first switch makes the same calls (only the order may move)
        other = calls["after_aws_first"][state][stage][variant]["calls"]
        assert sorted(map(tuple, other)) == sorted(map(tuple, new)), (variant, other, new)


@pytest.mark.parametrize("stage,large", [("s1_generate", True), ("t9_write", True), ("a0_column_map", False)])
def test_with_the_anthropic_api_on_the_same_model_goes_outside_before_the_next_model(calls, stage, large):
    """The tech lead's example (S267): acc3 Sonnet 5.5 >> API Sonnet 5.5 >> acc3 Sonnet 4.6 >> acc3 Haiku 4.5 >> the
    OpenAI API (small writers: Haiku 5.5 on acc3, then on the Anthropic API, Haiku 4.5, Sonnet 4.6, the OpenAI API)."""
    new = [c[:3] for c in calls["after"]["seed_anthropic_on"][stage]["plain"]["calls"]]
    first, second = ("sonnet-5-5", ("sonnet-4-6", "haiku-4-5")) if large else ("haiku-5-5", ("haiku-4-5", "sonnet-4-6"))
    assert new == [["converse", "acc3", f"global.anthropic.claude-{first}"],
                   ["anthropic", "anthropic-direct", f"claude-{first}"],
                   ["converse", "acc3", NEW_MODELS[second[0]][1]],
                   ["converse", "acc3", NEW_MODELS[second[1]][1]],
                   ["openai", "openai-direct", "gpt-6-luna"]]
    # the AWS-first switch (kept, default off) would call the Anthropic API only after every AWS deployment
    aws_first = calls["after_aws_first"]["seed_anthropic_on"][stage]["plain"]["calls"]
    assert [c[0] for c in aws_first].index("anthropic") == 3


@pytest.mark.parametrize("state", ["seed", "seed_anthropic_on"])
def test_every_other_stage_makes_the_calls_it_made_before(calls, state):
    for stage in sorted(set(_routes(AFTER)) - set(DECIDED)):
        assert calls["after"][state][stage] == calls["before"][state][stage], stage


# ── the $0 resolver (scripts/llm_ops/route_chain_check.py) ────────────────────────────────────────────────────────

REFUSED_ON_ACC3 = {"sonnet-5-5", "haiku-5-5", "gpt-6-luna", "gpt-5.6-luna", "opus-5-5", "sonnet-5"}


def _resolve(stage: str, anthropic_on: bool = False) -> dict:
    import time

    from api.services import llm_route_chains as route_chains
    from shared.llm_client import catalog, connections, vendor_route
    routes = _routes(AFTER)
    fixture = _fixture(routes)
    models = rc.catalog_models(fixture)
    cfg = rc.stage_configs(fixture, _scopes_of(routes)(fixture))[stage]
    rc._reset_registry()
    catalog._models = dict(models)
    catalog._loaded_at, catalog._failed_at = time.monotonic(), 0.0
    try:
        with patch.object(rc, "seed_deployments", _seed(rc.seed_deployments)), \
                patch.object(connections, "run_sync",
                             rc._registry_fetch("seed_anthropic_on" if anthropic_on else "seed", models)), \
                patch.object(vendor_route, "circuit_open",
                             lambda conn, model: conn == "acc3" and model in REFUSED_ON_ACC3), \
                patch.object(vendor_route, "connection_circuit_open", lambda conn: None), \
                patch("shared.llm_client.breaker.open_class", return_value="access_denied"):
            return route_chains.resolve_stage(cfg)
    finally:
        catalog.invalidate()
        catalog._models = {}
        rc._reset_registry()


@pytest.mark.parametrize("stage,first", [
    ("s1_generate", "sonnet-4-6@acc3"),
    ("t9_repair", "haiku-4-5@acc3"),
    ("tp_compose", "sonnet-4-6@acc3"),
    ("a3_place_locate", "sonnet-4-6@acc3"),
    ("a3_atomize", f"{OPENAI_HOP}@openai-direct"),         # GPT-6 Luna refused on acc3: the same model, OpenAI API
    ("s1_judge", f"{OPENAI_HOP}@openai-direct"),
    ("s1_brand_audit", f"{OPENAI_HOP}@openai-direct"),
    ("a3_place_dedupe", f"{OPENAI_HOP}@openai-direct"),
    ("a3_place_card", f"{OPENAI_HOP}@openai-direct"),
    ("t10_adjudicate", f"{OPENAI_HOP}@openai-direct"),
    ("segment_research", "haiku-4-5@acc3"),
])
def test_the_resolver_names_the_first_model_that_would_be_called_without_calling_one(stage, first):
    out = _resolve(stage)
    assert out["first"] == first, out
    assert "error" not in out
    skipped = [a for a in out["attempts"] if a["status"] == "skip"]
    assert skipped and all("circuit open: acc3 (access_denied)" in a["reason"] for a in skipped), out


def test_the_resolver_shows_the_model_first_order_of_a_vendor_direct_writer():
    """Anthropic API on, acc3 refusing 5.x: Sonnet 5.5 is called on the Anthropic API before Sonnet 4.6 on acc3."""
    out = _resolve("s1_generate", anthropic_on=True)
    assert [(a["model"], a["scope"]) for a in out["attempts"]] == [
        ("sonnet-5-5", "stage"), ("sonnet-4-6", "stage"), ("haiku-4-5", "stage"), (OPENAI_HOP, "stage")]
    assert out["attempts"][0] == {"model": "sonnet-5-5", "scope": "stage", "status": "call",
                                  "connection": "anthropic-direct", "deployments": ["anthropic-direct"]}
    assert out["first"] == "sonnet-5-5@anthropic-direct"


@pytest.mark.parametrize("stage,first,rest", [
    ("tp_compose", "sonnet-5-5", ["sonnet-4-6", "haiku-4-5", OPENAI_HOP]),
    ("a3_place_locate", "sonnet-5-5", ["sonnet-4-6", "haiku-4-5", "nova-lite"]),
    ("t9_faq_filter", "haiku-5-5", ["haiku-4-5", "nova-lite"]),
    ("segment_research", "haiku-5-5", ["haiku-4-5", "sonnet-4-6", OPENAI_HOP]),
])
def test_s267_a_former_aws_only_stage_reaches_its_primary_on_the_anthropic_api(stage, first, rest):
    """S267 (AWS preferred, not AWS only): TripPlanner, segment research and the validate stages now allow
    vendor_direct, so with the Anthropic API on and acc3 refusing 5.x their primary runs on the Anthropic API before
    the 4.x sibling on acc3. (TripPlanner's own gateway still calls Bedrock only: it skips that deployment.)"""
    out = _resolve(stage, anthropic_on=True)
    assert [a["model"] for a in out["attempts"]] == [first, *rest]
    assert out["first"] == f"{first}@anthropic-direct"


def test_the_resolver_shows_the_aws_first_passes_with_the_switch_on(monkeypatch):
    monkeypatch.setenv("LLM_ROUTE_AWS_FIRST", "on")
    out = _resolve("s1_generate", anthropic_on=True)
    assert [(a["model"], a["scope"]) for a in out["attempts"]] == [
        ("sonnet-5-5", "aws_only"), ("sonnet-4-6", "aws_only"), ("haiku-4-5", "aws_only"),
        ("sonnet-5-5", "vendor_direct"), (OPENAI_HOP, "vendor_direct")]
    assert out["attempts"][3] == {"model": "sonnet-5-5", "scope": "vendor_direct", "status": "call",
                                  "connection": "anthropic-direct", "deployments": ["anthropic-direct"]}


def test_the_check_script_prints_one_report_with_the_first_model_per_stage(capsys):
    """scripts/llm_ops/route_chain_check.py: the live rows go through the same resolver and rules (readers stubbed)."""
    import sys

    from scripts.llm_ops import route_chain_check as script
    rows = [{**r, "fallback_model_ids": list(r["fallback_model_ids"])} for r in _routes(AFTER).values()]
    models = _models()
    reach, acc2_only = _reach_and_acc2_only()

    def fake_resolve(cfg, evidence=None):
        assert evidence == "evidence"                                   # AA-1042: the refusals on record go along
        return {"stage": cfg.stage, "role": cfg.role, "scopes": list(cfg.allowed_scopes or ()), "attempts": [],
                "first": f"{cfg.model_id}@acc3"}

    async def fake_load():
        return rows, models, "evidence"
    with patch.object(script, "_load", fake_load), \
            patch.object(script, "_reach", return_value=(reach, acc2_only, _deployment_scopes())), \
            patch("api.services.llm_route_chains.resolve_stage", side_effect=fake_resolve), \
            patch.object(sys, "argv", ["route_chain_check.py", "--json"]):
        assert script.main() == 0
    out = capsys.readouterr().out
    report = json.loads(out.split("--- ROUTE CHAIN CHECK ---\n", 1)[1].split("\n--- END ---", 1)[0])
    assert report["violations"] == []
    assert report["unmapped_stages"] == []
    assert report["first_by_stage"]["s1_generate"] == "sonnet-5-5@acc3"
    by_stage = {s["stage"]: s for s in report["stages"]}
    assert (by_stage["t9_write"]["product"], by_stage["t9_write"]["app"]) == ("tenant_content", "cis")
    assert "tp_extract" not in report["first_by_stage"]                      # switched off: listed, not resolved
    assert {s["stage"] for s in report["stages"] if s.get("switched_off")} == {
        "s1_atom_writer", "tp_extract", "tp_component_embed"}


# ── the real connection circuit with acc3's refusals (shared/llm_client/breaker.py) ─────────────────────────────────
# acc3 refuses the 5.x Claude and GPT models one by one ("not available for this account") while the 4.x Claude
# models answer. The connection circuit trips on access_denied of two distinct models, so these refusals alone keep
# it open and would skip the 4.x siblings too. What the tests below pin: before any 4.x success the circuit is open
# and the chain still ends on acc2 (the live step is the admin Test connection on sonnet-4-6 / haiku-4-5, which records
# a success); after one success, the old refusals (a streak that began before it: a failed probe keeps opened_at) no
# longer trip the connection, so half-open probes of refused models do not skip the sibling, even after a quiet night;
# two fresh refusals after the success still trip it (an account-wide block).

from datetime import datetime, timedelta, timezone  # noqa: E402

from shared.llm_client import breaker  # noqa: E402
from shared.llm_client.breaker import Health  # noqa: E402

T_NOW = datetime(2026, 11, 4, 9, 0, tzinfo=timezone.utc)
NOT_AVAILABLE = RuntimeError("AccessDeniedException: The model is not available for this account.")
# a3_atomize's acc3 rungs (its OpenAI API twin between gpt-6-luna and haiku-5-5 is not on acc3: here it fails too)
ATOMIZE_ON_ACC3 = ["gpt-6-luna", "haiku-5-5", "haiku-4-5"]
STILL_REFUSED = ("gpt-6-luna", "gpt-5.6-luna", "haiku-5-5", "sonnet-5-5")


def _refused_for_days(model: str, last_open: datetime) -> Health:
    """A deployment row refused since 29/10 and probed ever since (streak 9, last reopened at ``last_open``; a row
    written before AA-1026 has opened_at = that reopening)."""
    return Health("acc3", model, last_error_at=last_open, last_error_class="access_denied", last_error="refused",
                  consecutive_failures=9, circuit_open_until=last_open + timedelta(seconds=1800), open_streak=9,
                  opened_at=last_open)


def _walk_acc3(chain: list[str]) -> str:
    """The gateway's acc3 part of a route, the way the plan (``is_open``, ``connection_open``) and ``guarded``
    (``admit``) see it: the first model admitted and not refused answers; a refused one records its failure."""
    for model in chain:
        if breaker.connection_open("acc3") or breaker.is_open("acc3", model) or not breaker.admit("acc3", model):
            continue
        if model in STILL_REFUSED:
            breaker.record_failure("acc3", model, NOT_AVAILABLE)
            continue
        breaker.record_success("acc3", model)
        return f"{model}@acc3"
    return "acc2 (nova-lite)"


@pytest.fixture
def acc3_rows(monkeypatch):
    clock = {"now": T_NOW}
    monkeypatch.setattr(breaker, "_now", lambda: clock["now"])
    with breaker._lock:
        breaker._rows.update({("acc3", m): _refused_for_days(m, T_NOW - timedelta(seconds=600))
                              for m in STILL_REFUSED})
    return clock


@pytest.mark.real_breaker
def test_before_any_4x_success_the_refusals_keep_acc3_closed_to_the_siblings(acc3_rows):
    assert breaker.connection_open("acc3") == "access_denied"
    assert _walk_acc3(ATOMIZE_ON_ACC3) == "acc2 (nova-lite)"
    acc3_rows["now"] = T_NOW + timedelta(seconds=1300)          # every row half-open: the first probe re-trips it
    assert breaker.connection_open("acc3") is None
    assert _walk_acc3(ATOMIZE_ON_ACC3) == "acc2 (nova-lite)"
    assert breaker.connection_open("acc3") == "access_denied"


@pytest.mark.real_breaker
def test_after_one_4x_success_the_old_refusals_no_longer_skip_the_sibling(acc3_rows):
    breaker.record_success("acc3", "sonnet-4-6")                 # the admin Test connection (LLMClient.probe)
    assert breaker.connection_open("acc3") is None
    assert _walk_acc3(ATOMIZE_ON_ACC3) == "haiku-4-5@acc3"       # 5.x / GPT rows still open: skipped one by one
    acc3_rows["now"] = T_NOW + timedelta(seconds=1300)          # half-open: each refused row is probed and fails
    assert _walk_acc3(ATOMIZE_ON_ACC3) == "haiku-4-5@acc3"
    assert breaker.connection_open("acc3") is None
    assert all(breaker.is_open("acc3", m) for m in ("gpt-6-luna", "haiku-5-5"))
    assert breaker.connection_snapshot("acc3") is None          # the admin card does not call acc3 blocked
    probed = breaker._get(("acc3", "gpt-6-luna"))
    assert probed.open_streak == 10 and probed.opened_at == T_NOW - timedelta(seconds=600)   # the streak start kept
    acc3_rows["now"] = T_NOW + timedelta(hours=10)               # a quiet night, then the next request
    assert _walk_acc3(ATOMIZE_ON_ACC3) == "haiku-4-5@acc3"
    assert breaker.connection_open("acc3") is None


@pytest.mark.real_breaker
def test_two_fresh_refusals_after_the_success_still_trip_the_connection(acc3_rows):
    breaker.record_success("acc3", "sonnet-4-6")
    acc3_rows["now"] = T_NOW + timedelta(seconds=60)
    for model in ("sonnet-4-6", "haiku-4-5"):                    # the account itself blocked (Error 002)
        breaker.record_failure("acc3", model, NOT_AVAILABLE)
    assert breaker.connection_open("acc3") == "access_denied"
