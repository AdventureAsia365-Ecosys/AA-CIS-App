"""AA-1095 (S278, tech lead 12/11 15:45) — the provider order of each model by FAMILY, as one route-control proposal.

The route control (``api/services/llm_route_control.py``) stores, per model, the order in which the gateway tries the
model's deployments (``shared.llm_model_deployment.priority``). Its presets set one rule for every model
(``prefer_anthropic_api``: the Anthropic API first for Claude; ``prefer_aws``: AWS first). The S278 decision needs a
different rule per family, around one named Bedrock connection (``lead``, the connection that serves Bedrock while the
LLM account is blocked; a registry row, so the caller passes its key and this module names no account):

- Claude models (``family_rule.WRITER_FAMILY``): the Anthropic API connections first, then ``lead``, then the other
  AWS connections (the blocked account last among them: the gateway skips its refused rows at 0 USD), then the rest.
- every other model with a ``lead`` deployment (the GPT models on Bedrock): ``lead`` first, then the OpenAI API
  connections of the model, then the other AWS connections, then the rest. A GPT model's OpenAI API twin is its own
  catalog model right behind it on the chain (CIS 366), so in model-first order the chain tries Bedrock on ``lead``,
  the blocked account (skipped at 0 USD while it refuses), then the OpenAI API twin.
- any other model keeps its order.

``family_provider_order`` is pure (the run-script and the tests call it); the save goes through
``llm_route_control.apply(action="save", provider_order=..., order_mode="model_first", break_glass_reason=...)``,
which validates it, writes ONE audited version and keeps the one-click revert.
"""
from __future__ import annotations

from typing import Mapping, Optional

from shared.llm_client import data_scope, family_rule, route_settings, route_versions

ANTHROPIC_KIND = "anthropic"
OPENAI_KINDS = frozenset({"openai", "azure_openai"})
ORDER_MODE = route_settings.MODEL_FIRST


def _kind(connections: Mapping[str, Mapping], key: str) -> Optional[str]:
    return (connections.get(key) or {}).get("kind")


def _aws(connections: Mapping[str, Mapping], key: str) -> bool:
    return (connections.get(key) or {}).get("data_scope") == data_scope.AWS_ONLY


def wanted_order(order: list[str], family: Optional[str], lead: str, connections: Mapping[str, Mapping]) -> list[str]:
    """One model's wanted provider order from its current ``order`` (pure). Connections keep their current relative
    order inside each group, so a second run changes nothing."""
    if family == family_rule.WRITER_FAMILY:
        groups = ([c for c in order if _kind(connections, c) == ANTHROPIC_KIND],
                  [c for c in order if c == lead])
    elif lead in order:
        groups = ([c for c in order if c == lead],
                  [c for c in order if _kind(connections, c) in OPENAI_KINDS])
    else:
        return list(order)
    head = [c for g in groups for c in g]
    aws = [c for c in order if c not in head and _aws(connections, c)]
    return head + aws + [c for c in order if c not in head and c not in aws]


def family_provider_order(priorities: Mapping[str, Mapping[str, int]], families: Mapping[str, Optional[str]],
                          connections: Mapping[str, Mapping], lead: str) -> dict[str, list[str]]:
    """model -> its wanted connection order, for the models whose order changes (pure). ``priorities``: the live
    ``route_versions`` priorities (model -> {connection: priority}); ``families``: model -> family key;
    ``connections``: connection_key -> row with ``kind`` and ``data_scope``; ``lead``: the Bedrock connection key."""
    out: dict[str, list[str]] = {}
    for model in sorted(priorities):
        order = route_versions.provider_order(priorities[model])
        if len(order) < 2:
            continue
        wanted = wanted_order(order, families.get(model), lead, connections)
        if wanted != order:
            out[model] = wanted
    return out


__all__ = ["ANTHROPIC_KIND", "OPENAI_KINDS", "ORDER_MODE", "family_provider_order", "wanted_order"]
