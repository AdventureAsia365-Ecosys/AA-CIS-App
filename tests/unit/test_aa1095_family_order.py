"""AA-1095 (S278, tech lead 12/11 15:45) — the per-family provider order (api/services/llm_family_order.py).

Pins both families' orders around the Bedrock connection that serves while the LLM account is blocked ("acc1", a
registry row): Claude = Anthropic API, then acc1, then acc3; GPT on Bedrock = acc1, then the OpenAI API, then acc3.
The priorities below are the Dev shape after CIS 413 (acc3 10, acc1 90, anthropic-direct 100); invented values only.
"""
from api.services import llm_family_order as fo
from api.services import llm_route_control as rc
from shared.llm_client import route_settings, route_versions

LEAD = "acc1"
CONNECTIONS = {
    "acc3": {"kind": "bedrock", "data_scope": "aws_only"},
    "acc1": {"kind": "bedrock", "data_scope": "aws_only"},
    "acc2": {"kind": "bedrock", "data_scope": "aws_only"},
    "anthropic-direct": {"kind": "anthropic", "data_scope": "vendor_direct"},
    "openai-direct": {"kind": "openai", "data_scope": "vendor_direct"},
}
FAMILIES = {
    "sonnet-5-5": "anthropic", "opus-5-5": "anthropic", "haiku-4-5": "anthropic", "sonnet-4-6": "anthropic",
    "gpt-6-luna": "openai", "gpt-6-astra": "openai", "gpt-6-luna-openai": "openai", "nova-lite": "amazon",
    "maverick-x": "meta",
}
PRIORITIES = {
    "sonnet-5-5": {"acc3": 10, "acc1": 90, "anthropic-direct": 100},
    "opus-5-5": {"acc3": 10, "acc1": 90, "anthropic-direct": 100},
    "haiku-4-5": {"acc3": 20, "anthropic-direct": 100, "acc1": 90},
    "sonnet-4-6": {"acc3": 10, "acc1": 90},                             # no Anthropic API row
    "gpt-6-luna": {"acc3": 10, "acc1": 90},
    "gpt-6-astra": {"acc3": 10, "acc1": 90, "openai-direct": 100},     # an OpenAI API row on the same model
    "gpt-6-luna-openai": {"openai-direct": 10},                         # the twin: one deployment, never reordered
    "nova-lite": {"acc2": 10},
    "maverick-x": {"acc3": 10, "acc2": 20},                             # no acc1 row: kept as it is
}


def _orders(priorities: dict) -> dict:
    return {m: route_versions.provider_order(p) for m, p in priorities.items()}


def test_claude_models_put_the_anthropic_api_first_then_acc1_then_acc3():
    out = fo.family_provider_order(PRIORITIES, FAMILIES, CONNECTIONS, LEAD)
    assert out["sonnet-5-5"] == ["anthropic-direct", "acc1", "acc3"]
    assert out["opus-5-5"] == ["anthropic-direct", "acc1", "acc3"]
    assert out["haiku-4-5"] == ["anthropic-direct", "acc1", "acc3"]
    assert out["sonnet-4-6"] == ["acc1", "acc3"]                        # no API row: acc1 still ahead of acc3


def test_gpt_models_put_acc1_first_then_the_openai_api_then_acc3():
    out = fo.family_provider_order(PRIORITIES, FAMILIES, CONNECTIONS, LEAD)
    assert out["gpt-6-luna"] == ["acc1", "acc3"]
    assert out["gpt-6-astra"] == ["acc1", "openai-direct", "acc3"]


def test_models_without_a_change_are_left_out():
    out = fo.family_provider_order(PRIORITIES, FAMILIES, CONNECTIONS, LEAD)
    assert set(out) == {"sonnet-5-5", "opus-5-5", "haiku-4-5", "sonnet-4-6", "gpt-6-luna", "gpt-6-astra"}
    assert fo.wanted_order(["acc3", "acc2"], "meta", LEAD, CONNECTIONS) == ["acc3", "acc2"]


def test_a_second_run_changes_nothing():
    first = fo.family_provider_order(PRIORITIES, FAMILIES, CONNECTIONS, LEAD)
    after = {m: ({c: (i + 1) * 10 for i, c in enumerate(first[m])} if m in first else p)
             for m, p in PRIORITIES.items()}
    assert fo.family_provider_order(after, FAMILIES, CONNECTIONS, LEAD) == {}


def test_an_unknown_connection_goes_last_in_its_current_order():
    conns = {**CONNECTIONS}
    order = fo.wanted_order(["mystery", "acc3", "acc1", "anthropic-direct"], "anthropic", LEAD, conns)
    assert order == ["anthropic-direct", "acc1", "acc3", "mystery"]


def test_the_model_first_walk_of_a_writer_chain():
    out = fo.family_provider_order(PRIORITIES, FAMILIES, CONNECTIONS, LEAD)
    orders = {**_orders(PRIORITIES), **out}
    writer = ["sonnet-5-5", "sonnet-4-6", "haiku-4-5", "gpt-6-luna-openai"]
    assert [f"{m}@{c}" for m in writer for c in orders[m]] == [
        "sonnet-5-5@anthropic-direct", "sonnet-5-5@acc1", "sonnet-5-5@acc3",
        "sonnet-4-6@acc1", "sonnet-4-6@acc3",
        "haiku-4-5@anthropic-direct", "haiku-4-5@acc1", "haiku-4-5@acc3",
        "gpt-6-luna-openai@openai-direct"]
    validate = ["gpt-6-luna", "gpt-6-luna-openai", "nova-lite"]
    assert [f"{m}@{c}" for m in validate for c in orders[m]] == [
        "gpt-6-luna@acc1", "gpt-6-luna@acc3", "gpt-6-luna-openai@openai-direct", "nova-lite@acc2"]


def test_the_route_control_accepts_the_orders_as_one_proposal():
    deployments = [{"model_key": m, "connection_key": c} for m, p in PRIORITIES.items() for c in p]
    live = {"order_mode": "aws_first", "routes": {}, "priorities": PRIORITIES}
    out = fo.family_provider_order(PRIORITIES, FAMILIES, CONNECTIONS, LEAD)
    target, errors = rc.build_target(live, order_mode=fo.ORDER_MODE, provider_order=out, deployments=deployments)
    assert errors == []
    assert fo.ORDER_MODE == route_settings.MODEL_FIRST and target["order_mode"] == "model_first"
    assert target["priorities"]["sonnet-5-5"] == {"anthropic-direct": 10, "acc1": 20, "acc3": 30}
    assert target["priorities"]["gpt-6-astra"] == {"acc1": 10, "openai-direct": 20, "acc3": 30}
    assert target["priorities"]["maverick-x"] == {"acc3": 10, "acc2": 20}
    kinds = {d["kind"] for d in route_versions.diff(live, target)}
    assert kinds == {"order_mode", "provider_order"}                   # no stage chain changes
