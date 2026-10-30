"""AA-685 — the gateway's embedding path: `embed(stage, texts)`.

The stage route (shared.llm_role_config: model_id + fallback_model_ids) picks catalog models with
`api_style='embed'`; the catalog row gives the Bedrock profile per account and the price. Same
contract as LLMClient.generate(): raises when every model in the route fails, and the CALLER
writes shared.llm_call_log (quality_signal is the caller's to compute).

Only Cohere's Bedrock request/response shape is implemented — it is the only embedding vendor in
the catalog. A new vendor gets its own body builder here, not a new call site.

AA-1002 (S3): where a model runs comes from the connection registry, not from code. Each model of the
route is tried on its deployments (vendor_route.deployment_plan: enabled, on an enabled Bedrock
connection, priority order) and the client comes from that connection (`_runtime_for`: the task role
for a task_role connection such as acc2, one attempt, no retry; the satellite session for an
assume_role one such as acc3). Without registry rows for the model (DB down, a cold process, a row
added without deployments) the catalog's bedrock_profile_ids is used on the Bedrock
connections (connections.bedrock_connection_keys, the built-in 304 mirror when unreadable) — one
account per model, acc3 before acc2, as before S3. The request body (input_type, output_dimension)
is unchanged.

AA-1002 (S4): each call on a registry deployment goes through the circuit breaker and the connection's limiter
(breaker.guarded); a deployment whose circuit is open is not in the plan.
"""
from __future__ import annotations

import functools
import json
import os
from dataclasses import dataclass
from typing import Optional

import structlog

from . import breaker, connections
from .catalog import CatalogModel, get_model_sync
from .role_config import get_stage_config_sync
from .vendor_route import deployment_plan

logger = structlog.get_logger()


# Used only when the catalog cannot be read, so a DB outage does not stop embeddings (the
# catalog is the source of truth; this mirrors its migration-169 seed row).
_CATALOG_FALLBACK = {
    "cohere-embed-v4": CatalogModel(
        model_key="cohere-embed-v4", label="Cohere Embed v4", vendor="cohere",
        provider="bedrock", api_style="embed",
        bedrock_profile_ids={"acc2": "us.cohere.embed-v4:0"}, callable_via=("embed",),
        price_in_per_mtok=0.12, price_out_per_mtok=0.0, enabled=True,
    ),
}

@dataclass
class EmbedResponse:
    vectors: list[list[float]]
    model_used: str
    account: str
    input_tokens: int
    # True when Bedrock did not return a token count and it was estimated from the text length.
    tokens_estimated: bool
    cost_usd: float
    fallback_used: bool = False


def _runtime_for(connection_key: str):
    """The bedrock-runtime client of a Bedrock connection (registry row, else its built-in mirror).

    A task_role connection (acc2) gets its own client with NO retry (total_max_attempts=1, one attempt): callers pace
    to the account's per-minute quota, and a throttle retried at once spends the same minute's quota again (AA-610).
    AA-862: the legacy `max_attempts` key counts RETRIES in botocore (`max_attempts=1` = 2 attempts), which spent two
    acc2 calls per throttle and delayed the fall-through to acc3. An assume_role connection (acc3) uses the shared
    satellite session (bedrock_satellite.get_satellite_client)."""
    conn = connections.connection_or_builtin(connection_key)
    if conn is None or conn.kind != "bedrock":
        raise RuntimeError(f"{connection_key!r} is not a Bedrock connection")
    if conn.auth_mode == "task_role":
        from .bedrock_satellite import native_runtime
        return native_runtime(conn, read_timeout=30, max_attempts=1)
    from .bedrock_satellite import get_satellite_client
    return get_satellite_client("bedrock-runtime", account=connection_key)


def _targets(model: CatalogModel, account_route: Optional[str]) -> list[tuple[str, str]]:
    """(connection_key, wire id) attempts for `model`, in order (see the module docstring)."""
    return [(account, wire_id) for account, wire_id, _ in _plan_targets(model, account_route)[0]]


def _plan_targets(model: CatalogModel, account_route: Optional[str]):
    """([(connection_key, wire id, connection or None)], circuit skips). The connection is None on the no-registry
    path (today's path, no breaker); circuit skips name the deployments the S4 breaker left out."""
    plan = deployment_plan(model)
    if plan is None:
        account = model.account_for(account_route)
        return ([(account, model.bedrock_profile_ids[account], None)] if account else []), []
    return ([(t.connection.connection_key, t.deployment.wire_id, t.connection) for t in plan],
            list(getattr(plan, "circuit_skips", [])))


def _resolve(key: str) -> Optional[CatalogModel]:
    m = get_model_sync(key)
    return m if m is not None else _CATALOG_FALLBACK.get(key)


def _cohere_body(texts: list[str], input_type: str, dimensions: Optional[int]) -> str:
    body = {"texts": texts, "input_type": input_type, "embedding_types": ["float"]}
    if dimensions:
        body["output_dimension"] = dimensions
    return json.dumps(body)


def _call(model: CatalogModel, account: str, texts: list[str], input_type: str,
          dimensions: Optional[int], wire_id: Optional[str] = None) -> EmbedResponse:
    if model.vendor != "cohere":
        raise RuntimeError(f"No embedding request shape for vendor {model.vendor!r}")
    resp = _runtime_for(account).invoke_model(
        modelId=wire_id or model.bedrock_profile_ids[account],
        body=_cohere_body(texts, input_type, dimensions),
        contentType="application/json", accept="application/json",
    )
    payload = json.loads(resp["body"].read())
    embeddings = payload.get("embeddings")
    vectors = embeddings.get("float") if isinstance(embeddings, dict) else None
    if (not isinstance(vectors, list) or len(vectors) != len(texts)
            or not all(isinstance(v, list) and v for v in vectors)
            or (dimensions and any(len(v) != dimensions for v in vectors))):
        raise RuntimeError(f"{model.model_key} returned a malformed embedding response")

    header = (resp.get("ResponseMetadata", {}).get("HTTPHeaders", {})
              .get("x-amzn-bedrock-input-token-count"))
    if header is not None:
        tokens, estimated = int(header), False
    else:
        tokens, estimated = sum(max(1, len(t) // 4) for t in texts), True
    return EmbedResponse(
        vectors=vectors, model_used=model.model_key, account=account, input_tokens=tokens,
        tokens_estimated=estimated,
        # Priced from the resolved row itself: pricing.calc_cost() would fall back to Sonnet
        # rates for a model it cannot find while the catalog is unreachable.
        cost_usd=round(tokens * (model.price_in_per_mtok or 0.0) / 1_000_000, 6),
    )


def embed(stage: str, texts: list[str], *, input_type: str = "search_document",
          dimensions: Optional[int] = None) -> EmbedResponse:
    """Embeds `texts` with the stage's route. `input_type` is Cohere's: "search_document" for
    text that is stored, "search_query" for text searched against stored documents."""
    if not texts:
        raise ValueError("embed() needs at least one text")
    cfg = get_stage_config_sync(stage)
    chain = [cfg.model_id, *cfg.fallback_model_ids]
    errors = []
    for position, key in enumerate(chain):
        model = _resolve(key)
        if model is None or not model.enabled or model.api_style != "embed":
            reason = ("not in catalog" if model is None else
                      "not an embedding model" if model.api_style != "embed" else
                      model.blocked_reason or "disabled")
            logger.info("llm_route_skip", stage=stage, model=key, reason=reason)
            errors.append(f"{key}: skipped ({reason})")
            continue
        targets, skips = _plan_targets(model, cfg.account_route)
        if not targets:
            errors.append(f"{key}: " + (str(breaker.circuit_error(key, skips)) if skips else
                                        "no Bedrock profile or usable deployment"))
            continue
        for account, wire_id, conn in targets:    # AA-1002 (S3): same model on each deployment first
            try:
                if conn is None:
                    resp = _call(model, account, texts, input_type, dimensions, wire_id)
                else:                             # AA-1002 (S4): breaker + limiter of the deployment
                    resp = breaker.guarded(conn, model.model_key, functools.partial(
                        _call, model, account, texts, input_type, dimensions, wire_id))
            except Exception as e:
                logger.warning("llm_route_attempt_failed", stage=stage, model=key, account=account,
                               position=position, error=str(e)[:300])
                errors.append(f"{key}@{account}: {str(e)[:200]}" if len(targets) > 1 else f"{key}: {str(e)[:200]}")
                continue
            resp.fallback_used = position > 0
            logger.info("llm_embed_success", stage=stage, model=key, account=account,
                        texts=len(texts), in_tokens=resp.input_tokens, cost_usd=resp.cost_usd)
            return resp
    raise RuntimeError(f"All models in the route failed for stage {stage!r}: " + "; ".join(errors))
