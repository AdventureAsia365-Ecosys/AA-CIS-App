"""AA-685 — the gateway's embedding path: `embed(stage, texts)`.

The stage route (shared.llm_role_config: model_id + fallback_model_ids) picks catalog models with
`api_style='embed'`; the catalog row gives the Bedrock profile per account and the price. Same
contract as LLMClient.generate(): raises when every model in the route fails, and the CALLER
writes shared.llm_call_log (quality_signal is the caller's to compute).

Two Bedrock request/response shapes: Cohere (Embed v4, up to 96 texts per request, input_type) and, since AA-1026,
Amazon (Titan Text Embeddings V2, the fallback embedding model: one text per request, ``dimensions``, normalized,
no input type). A new vendor gets its own body builder here, not a new call site.

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

AA-1026 D5 (embedding space registry, ``embedding_space.py``): a caller that writes or searches a vector column passes
the column's space (``space=``). Only the space's models are tried — the stage route filtered to them, or the space's
own models when the route names none of them (a column swapped to another space needs no route change) — at the
space's dimension, so one call can never fall back into another vector space. The response carries ``space_key`` for
the ledger row (``embedding_space.ledger_signal``).

AA-642 (S263 gw-core, A9 live follow-up): the daily probe's rows lacked the attempt facts (``probe`` now sets them like
``embed``: connection, latency, attempt 1, wire id, pricing mode) and an answered probe was logged at 0 USD (a few
tokens x 0.12 USD per Mtok rounded to 6 decimals). A billed embedding is priced by ``price_embedding``: the deployment's
``price_in_override``, else the catalog row, else ``EMBED_ESTIMATE_PER_MTOK`` (flagged ``estimate``, never 0), and the
amount is rounded UP to the ledger's micro-dollar (``cost_usd`` numeric(10,6)), so a billed call is never 0 USD.
Embeddings are billed per input token: ``units`` / ``unit_kind`` (non-token units, AA-804) stay empty.

AA-642 A3: the stage's ``route_deadline_s`` is checked before every attempt after the first, and at most
``errors.MAX_CHAIN_ATTEMPTS`` attempts run per call (both end as a 0 USD 'deadline' row and RouteDeadlineExceeded /
RouteAttemptCapReached). AA-642 A5: ``deployment_plan`` is asked with role ``embed`` (a connection whose
``served_roles`` leave embeddings out is not used).
"""
from __future__ import annotations

import functools
import json
import math
import os
import time
from dataclasses import dataclass
from typing import Optional

import structlog

from . import breaker, connections, data_scope
from .catalog import CatalogModel, get_model_sync
from .errors import MAX_CHAIN_ATTEMPTS, RouteAttemptCapReached, RouteDeadlineExceeded
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
    # AA-1026: the vector space of ``vectors`` when the caller passed one (embedding_space.ledger_signal).
    space_key: Optional[str] = None


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
    plan = deployment_plan(model, allowed_scopes, role=EMBED_ROLE)
    if plan is None:
        account = model.account_for(account_route)
        return ([(account, model.bedrock_profile_ids[account], None)] if account else []), []
    return ([(t.connection.connection_key, t.deployment.wire_id, t.connection) for t in plan],
            list(getattr(plan, "circuit_skips", [])))


def _resolve(key: str) -> Optional[CatalogModel]:
    m = get_model_sync(key)
    return m if m is not None else _CATALOG_FALLBACK.get(key)


# AA-642 A9: the price of an embedding no source can price (Cohere Embed v4 on Bedrock, the dearer of the two
# embedding models in the catalog: never 0, flagged 'estimate').
EMBED_ESTIMATE_PER_MTOK = 0.12


def price_embedding(model: CatalogModel, connection_key: Optional[str], tokens: int) -> tuple[float, str]:
    """(cost_usd, pricing_mode) of ``tokens`` input tokens of ``model`` served on ``connection_key``: the deployment's
    ``price_in_override`` ('override'), else the catalog row ('catalog'; 'code_table' for the built-in row used while
    the catalog is unreadable), else EMBED_ESTIMATE_PER_MTOK ('estimate'). Rounded UP to the micro-dollar, so a billed
    call is never logged at 0 USD (module docstring). Never raises."""
    rate, mode = None, None
    try:
        from .pricing import deployment_for
        dep = deployment_for(model.model_key, connection_key)
        override = getattr(dep, "price_in_override", None) if dep is not None else None
        if override is not None:
            rate, mode = float(override), "override"
    except Exception as e:  # noqa: BLE001 — pricing never fails a call
        logger.warning("llm_embed_price_override_lookup_failed", model=model.model_key, error=str(e)[:200])
    if rate is None and model.price_in_per_mtok is not None:
        rate = float(model.price_in_per_mtok)
        mode = "code_table" if model is _CATALOG_FALLBACK.get(model.model_key) else "catalog"
    if rate is None:
        rate, mode = EMBED_ESTIMATE_PER_MTOK, "estimate"
        logger.warning("llm_embed_price_unknown", model=model.model_key, priced_at=rate)
    micro = max(0, int(tokens or 0)) * rate                      # micro-dollars (USD per Mtok x tokens / 1e6 x 1e6)
    return (math.ceil(round(micro, 6)) / 1_000_000 if micro > 0 else 0.0), mode


def _cohere_body(texts: list[str], input_type: str, dimensions: Optional[int]) -> str:
    body = {"texts": texts, "input_type": input_type, "embedding_types": ["float"]}
    if dimensions:
        body["output_dimension"] = dimensions
    return json.dumps(body)


# AA-1026: Titan Text Embeddings V2 answers 256, 512 or 1024 dimensions (1024 when none is sent).
TITAN_DIMENSIONS = (256, 512, 1024)
# The vendors whose embedding request shape _call builds (the daily probe reads it).
EMBED_VENDORS = ("cohere", "amazon")


def _call(model: CatalogModel, account: str, texts: list[str], input_type: Optional[str],
          dimensions: Optional[int], wire_id: Optional[str] = None) -> EmbedResponse:
    if model.vendor == "amazon":
        return _titan_call(model, account, texts, dimensions, wire_id)
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
    # Priced from the resolved row itself (and the deployment's override): pricing.calc_cost() would fall back to
    # Sonnet rates for a model it cannot find while the catalog is unreachable.
    cost, mode = price_embedding(model, account, tokens)
    return EmbedResponse(
        vectors=vectors, model_used=model.model_key, account=account, input_tokens=tokens,
        tokens_estimated=estimated, cost_usd=cost, pricing_mode=mode,
    )


# AA-642 A5: the role an embedding call asks of a connection (llm_provider_connection.served_roles).
EMBED_ROLE = "embed"


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


def _titan_call(model: CatalogModel, account: str, texts: list[str], dimensions: Optional[int],
                wire_id: Optional[str]) -> EmbedResponse:
    """AA-1026: Amazon Titan Text Embeddings V2 — one InvokeModel per text (the model takes one ``inputText``),
    normalized vectors, the token count from each answer. No input type (the same vector for stored and search
    text)."""
    if dimensions and dimensions not in TITAN_DIMENSIONS:
        raise RuntimeError(f"{model.model_key} answers {TITAN_DIMENSIONS} dimensions, not {dimensions}")
    runtime = _runtime_for(account)
    vectors: list[list[float]] = []
    tokens, estimated = 0, False
    for text in texts:
        body = {"inputText": text, "normalize": True}
        if dimensions:
            body["dimensions"] = dimensions
        resp = runtime.invoke_model(modelId=wire_id or model.bedrock_profile_ids[account], body=json.dumps(body),
                                    contentType="application/json", accept="application/json")
        payload = json.loads(resp["body"].read())
        vector = payload.get("embedding")
        if (not isinstance(vector, list) or not vector or (dimensions and len(vector) != dimensions)):
            raise RuntimeError(f"{model.model_key} returned a malformed embedding response")
        vectors.append(vector)
        count = payload.get("inputTextTokenCount")
        if isinstance(count, int) and not isinstance(count, bool):
            tokens += count
        else:
            tokens, estimated = tokens + max(1, len(text) // 4), True
    cost, mode = price_embedding(model, account, tokens)
    return EmbedResponse(vectors=vectors, model_used=model.model_key, account=account, input_tokens=tokens,
                         tokens_estimated=estimated, cost_usd=cost, pricing_mode=mode)


def _route_bound(stage: str, model_key: str, account: str, attempts: int, ends_at: Optional[float],
                 deadline_s: Optional[int], errors: list, position: int) -> None:
    """AA-642 A3: before an attempt after the first, stop the route when the stage's route deadline passed or the
    call already made MAX_CHAIN_ATTEMPTS attempts: its 0 USD 'deadline' row (model = the attempt not started) and
    RouteDeadlineExceeded / RouteAttemptCapReached naming what failed so far."""
    if attempts == 0:
        return                                    # the first attempt always runs
    exc = None
    if attempts >= MAX_CHAIN_ATTEMPTS:
        exc = RouteAttemptCapReached(f"attempt cap of {MAX_CHAIN_ATTEMPTS} reached for stage {stage!r} before "
                                     f"{model_key} on {account}")
    elif ends_at is not None and time.monotonic() >= ends_at:
        exc = RouteDeadlineExceeded(f"route deadline of {deadline_s} s for stage {stage!r} passed before "
                                    f"{model_key} on {account}")
    if exc is None:
        return
    if errors:
        exc.args = (f"{exc.args[0]}; tried: " + "; ".join(errors)[:600],)
    logger.warning("llm_embed_route_bound", stage=stage, model=model_key, account=account, attempts=attempts,
                   error=str(exc)[:200])
    _log_failure(stage, model_key, exc, connection_key=account, latency_ms=0, position=position)
    raise exc


def _space_chain(chain: list[str], space) -> list[str]:
    """AA-1026: the route models of ``space`` in route order, else the space's own models (primary first)."""
    keys = tuple(space.model_keys)
    return [k for k in chain if k in keys] or list(keys)


def embed(stage: str, texts: list[str], *, input_type: Optional[str] = "search_document",
          dimensions: Optional[int] = None, space=None) -> EmbedResponse:
    """Embeds `texts` with the stage's route. `input_type` is Cohere's: "search_document" for
    text that is stored, "search_query" for text searched against stored documents.
    AA-1026: ``space`` (an ``embedding_space.EmbeddingSpace``) keeps the call inside one vector space (module
    docstring); its dimension is used when ``dimensions`` is not given."""
    if not texts:
        raise ValueError("embed() needs at least one text")
    cfg = get_stage_config_sync(stage)
    chain = [cfg.model_id, *cfg.fallback_model_ids]
    if getattr(cfg, "enabled", True) is False:          # AA-642 A1: the stage kill switch, before any attempt
        from .errors import LLMStageDisabled
        reason = getattr(cfg, "disabled_reason", None)
        refusal = LLMStageDisabled(stage, reason if isinstance(reason, str) else None)
        _log_failure(stage, cfg.model_id, refusal)
        raise refusal
    if space is not None:
        chain = _space_chain(chain, space)
        if dimensions is None:
            dimensions = space.dimension
    errors = []
    attempts = 0
    deadline_s = getattr(cfg, "route_deadline_s", None)
    if not isinstance(deadline_s, int) or isinstance(deadline_s, bool) or deadline_s <= 0:
        deadline_s = None                       # a test double or no deadline: nothing bound
    ends_at = time.monotonic() + deadline_s if deadline_s else None
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
            _route_bound(stage, key, account, attempts, ends_at, deadline_s, errors, position)   # AA-642 A3
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
            if not resp.pricing_mode:             # a response built before A9's price_embedding (test doubles)
                resp.pricing_mode = "code_table" if model is _CATALOG_FALLBACK.get(key) else "catalog"
            resp.space_key = space.space_key if space is not None else None
            logger.info("llm_embed_success", stage=stage, model=key, account=account,
                        texts=len(texts), in_tokens=resp.input_tokens, cost_usd=resp.cost_usd)
            return resp
    raise RuntimeError(f"All models in the route failed for stage {stage!r}: " + "; ".join(errors))


def probe(model: CatalogModel, conn: connections.ProviderConnection, wire_id: str, texts: list[str], *,
          input_type: str = "search_query") -> EmbedResponse:
    """AA-642 (S263 L2, B2): the daily probe of one embedding deployment — ``model`` on ``conn`` / ``wire_id`` only, no
    stage route, no fallback. Like ``LLMClient.probe`` it is the operator's half-open probe: an open circuit is not in
    the way and the outcome is counted on the deployment's circuit (a success closes it). Raises the provider's own
    exception; the caller writes the ledger row (stage adhoc_conn_test, role embed) from the response's attempt facts
    (``call_log.billed_fields``: connection, latency, attempt 1, wire id, pricing mode)."""
    if model.api_style != "embed" or conn.kind != "bedrock":
        raise RuntimeError(f"{model.model_key!r} on {conn.connection_key!r} is not a Bedrock embedding deployment")
    started = time.monotonic()
    resp = breaker.guarded(conn, model.model_key, functools.partial(
        _call, model, conn.connection_key, texts, input_type, None, wire_id), admit_call=False)
    # AA-642 A9 (S263 live: the probe's ok row had no latency / attempt number): the attempt facts, as embed() sets
    # them.
    try:
        resp.connection_key = conn.connection_key
        resp.latency_ms = int((time.monotonic() - started) * 1000)
        resp.attempt_no = 1
        resp.deployment_wire_id = wire_id or model.bedrock_profile_ids.get(conn.connection_key)
    except AttributeError:  # a test double that is not an EmbedResponse
        pass
    return resp
