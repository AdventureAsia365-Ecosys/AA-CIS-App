"""AA-685 — the gateway's embedding path: `embed(stage, texts)`.

The stage route (shared.llm_role_config: model_id + fallback_model_ids) picks catalog models with
`api_style='embed'`; the catalog row gives the Bedrock profile per account and the price. Same
contract as LLMClient.generate(): raises when every model in the route fails, and the CALLER
writes shared.llm_call_log (quality_signal is the caller's to compute).

Only Cohere's Bedrock request/response shape is implemented — it is the only embedding vendor in
the catalog. A new vendor gets its own body builder here, not a new call site.
"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from typing import Optional

import boto3
import structlog
from botocore.config import Config

from .catalog import CatalogModel, get_model_sync
from .role_config import get_stage_config_sync

logger = structlog.get_logger()

BEDROCK_REGION = os.environ.get("BEDROCK_REGION", "us-west-1")

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

_native_client = None
_native_lock = threading.Lock()


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


def _acc2_client():
    # No retry (max_attempts=1): callers pace to the account's per-minute quota, and a throttle
    # retried at once spends the same minute's quota again (AA-610).
    global _native_client
    with _native_lock:
        if _native_client is None:
            _native_client = boto3.client(
                "bedrock-runtime", region_name=BEDROCK_REGION,
                config=Config(read_timeout=30, connect_timeout=10,
                              retries={"max_attempts": 1, "mode": "standard"}),
            )
        return _native_client


def _runtime_for(account: str):
    if account == "acc2":
        return _acc2_client()
    from .bedrock_satellite import get_satellite_client
    return get_satellite_client("bedrock-runtime", account=account)


def _resolve(key: str) -> Optional[CatalogModel]:
    m = get_model_sync(key)
    return m if m is not None else _CATALOG_FALLBACK.get(key)


def _cohere_body(texts: list[str], input_type: str, dimensions: Optional[int]) -> str:
    body = {"texts": texts, "input_type": input_type, "embedding_types": ["float"]}
    if dimensions:
        body["output_dimension"] = dimensions
    return json.dumps(body)


def _call(model: CatalogModel, account: str, texts: list[str], input_type: str,
          dimensions: Optional[int]) -> EmbedResponse:
    if model.vendor != "cohere":
        raise RuntimeError(f"No embedding request shape for vendor {model.vendor!r}")
    resp = _runtime_for(account).invoke_model(
        modelId=model.bedrock_profile_ids[account],
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
        account = model.account_for(cfg.account_route)
        if account is None:
            errors.append(f"{key}: no Bedrock profile")
            continue
        try:
            resp = _call(model, account, texts, input_type, dimensions)
        except Exception as e:
            logger.warning("llm_route_attempt_failed", stage=stage, model=key,
                           position=position, error=str(e)[:300])
            errors.append(f"{key}: {str(e)[:200]}")
            continue
        resp.fallback_used = position > 0
        logger.info("llm_embed_success", stage=stage, model=key, account=account,
                    texts=len(texts), in_tokens=resp.input_tokens, cost_usd=resp.cost_usd)
        return resp
    raise RuntimeError(f"All models in the route failed for stage {stage!r}: " + "; ".join(errors))
