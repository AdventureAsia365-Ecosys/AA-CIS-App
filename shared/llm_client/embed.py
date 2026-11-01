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

AA-642 A9 (gap OPS-02, ledger v2 for embeddings): like ``LLMClient._attempt``, every provider attempt is timed and
numbered. A success carries ``connection_key``, ``latency_ms``, ``attempt_no``, ``deployment_wire_id`` and
``pricing_mode`` on the EmbedResponse (the caller's row takes them through ``call_log.billed_fields``). An attempt that
fails gets the gateway's own 0 USD llm_call_log row (role ``embed``, status refused | error and its class from
``client.attempt_outcome``, the connection, latency and attempt number), and so does a model of the route that is not
tried at all (not in the catalog, disabled, every deployment behind an open circuit or none usable: latency 0, no
attempt number). The rows are written on a background thread (``call_log._in_background``) and never raise.
"""
from __future__ import annotations

import functools
import json
import os
import time
from dataclasses import dataclass
from typing import Optional

import structlog

from . import breaker, connections, data_scope
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
    # AA-642 A9: the attempt facts (call_log.billed_fields reads them; None on a test double / older object).
    connection_key: Optional[str] = None
    latency_ms: Optional[int] = None
    attempt_no: Optional[int] = None
    deployment_wire_id: Optional[str] = None
    pricing_mode: Optional[str] = None


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


def _targets(model: CatalogModel, account_route: Optional[str],
             allowed_scopes: Optional[tuple] = None) -> list[tuple[str, str]]:
    """(connection_key, wire id) attempts for `model`, in order (see the module docstring)."""
    return [(account, wire_id) for account, wire_id, _ in _plan_targets(model, account_route, allowed_scopes)[0]]


def _plan_targets(model: CatalogModel, account_route: Optional[str], allowed_scopes: Optional[tuple] = None):
    """([(connection_key, wire id, connection or None)], circuit skips). The connection is None on the no-registry
    path (today's path, no breaker); circuit skips name the deployments the S4 breaker left out. AA-1002 (S5): only
    deployments on connections whose data scope the stage's ``allowed_scopes`` allow (None = no stage policy)."""
    plan = deployment_plan(model, allowed_scopes)
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


class _RouteSkip(RuntimeError):
    """A route model the gateway did not try (its ledger row; never raised to a caller)."""


def _provider(account: str, conn) -> str:
    """The llm_call_log provider of an embed call on ``account`` (the label the callers write for a success)."""
    if conn is not None:
        return "bedrock-native" if conn.auth_mode == "task_role" else "bedrock-satellite"
    return "bedrock-native" if account == "acc2" else "bedrock-satellite"


def _log_failure(stage: str, model_key: str, exc: BaseException, *, connection_key: Optional[str] = None,
                 wire_id: Optional[str] = None, provider: Optional[str] = None, latency_ms: Optional[int] = 0,
                 attempt_no: Optional[int] = None, position: int = 0) -> None:
    """AA-642 A9: the gateway's 0 USD llm_call_log row of one embed attempt that failed, or of a route model that was
    not tried (module docstring). Never raises."""
    from . import call_log
    try:
        from .client import attempt_outcome
        # A route model not tried was refused by the gateway itself (catalog / deployment state), not an outage.
        status, error_class = ("refused", "other") if isinstance(exc, _RouteSkip) else attempt_outcome(exc)
        row = call_log.gateway_failure_row(
            stage=stage, role="embed", model=model_key, status=status, error_class=error_class,
            error=f"{type(exc).__name__}: {exc}", provider=provider, connection_key=connection_key,
            latency_ms=latency_ms, attempt_no=attempt_no, deployment_wire_id=wire_id,
            quality_signal_extra={"position": position})
        call_log._in_background(call_log.record_calls, {"rows": [row]})
    except Exception as e:  # noqa: BLE001 — a log write never breaks the call it records
        logger.warning("llm_embed_failed_attempt_log_failed", stage=stage, error=str(e)[:200])


def embed(stage: str, texts: list[str], *, input_type: str = "search_document",
          dimensions: Optional[int] = None) -> EmbedResponse:
    """Embeds `texts` with the stage's route. `input_type` is Cohere's: "search_document" for
    text that is stored, "search_query" for text searched against stored documents."""
    if not texts:
        raise ValueError("embed() needs at least one text")
    cfg = get_stage_config_sync(stage)
    chain = [cfg.model_id, *cfg.fallback_model_ids]
    errors = []
    attempts = 0
    for position, key in enumerate(chain):
        model = _resolve(key)
        if model is None or not model.enabled or model.api_style != "embed":
            reason = ("not in catalog" if model is None else
                      "not an embedding model" if model.api_style != "embed" else
                      model.blocked_reason or "disabled")
            logger.info("llm_route_skip", stage=stage, model=key, reason=reason)
            errors.append(f"{key}: skipped ({reason})")
            _log_failure(stage, key, _RouteSkip(f"{key}: skipped ({reason})"), position=position)
            continue
        targets, skips = _plan_targets(model, cfg.account_route,
                                       data_scope.normalize(getattr(cfg, "allowed_scopes", None)))
        if not targets:
            refusal = (breaker.circuit_error(key, skips) if skips else
                       _RouteSkip(f"{key}: no Bedrock profile or usable deployment"))
            errors.append(f"{key}: " + (str(refusal) if skips else "no Bedrock profile or usable deployment"))
            _log_failure(stage, key, refusal, position=position)
            continue
        for account, wire_id, conn in targets:    # AA-1002 (S3): same model on each deployment first
            attempts += 1
            started = time.monotonic()
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
                _log_failure(stage, key, e, connection_key=account, wire_id=wire_id,
                             provider=_provider(account, conn), latency_ms=int((time.monotonic() - started) * 1000),
                             attempt_no=attempts, position=position)
                continue
            resp.fallback_used = position > 0
            # AA-642 A9: the attempt facts for the caller's row (call_log.billed_fields).
            resp.connection_key = account
            resp.latency_ms = int((time.monotonic() - started) * 1000)
            resp.attempt_no = attempts
            resp.deployment_wire_id = wire_id or model.bedrock_profile_ids.get(account)
            resp.pricing_mode = "code_table" if model is _CATALOG_FALLBACK.get(key) else "catalog"
            logger.info("llm_embed_success", stage=stage, model=key, account=account,
                        texts=len(texts), in_tokens=resp.input_tokens, cost_usd=resp.cost_usd)
            return resp
    raise RuntimeError(f"All models in the route failed for stage {stage!r}: " + "; ".join(errors))


def probe(model: CatalogModel, conn: connections.ProviderConnection, wire_id: str, texts: list[str], *,
          input_type: str = "search_query") -> EmbedResponse:
    """AA-642 (S263 L2, B2): the daily probe of one embedding deployment — ``model`` on ``conn`` / ``wire_id`` only, no
    stage route, no fallback. Like ``LLMClient.probe`` it is the operator's half-open probe: an open circuit is not in
    the way and the outcome is counted on the deployment's circuit (a success closes it). Raises the provider's own
    exception; the caller writes the ledger row (stage adhoc_conn_test, role embed)."""
    if model.api_style != "embed" or conn.kind != "bedrock":
        raise RuntimeError(f"{model.model_key!r} on {conn.connection_key!r} is not a Bedrock embedding deployment")
    return breaker.guarded(conn, model.model_key, functools.partial(
        _call, model, conn.connection_key, texts, input_type, None, wire_id), admit_call=False)
