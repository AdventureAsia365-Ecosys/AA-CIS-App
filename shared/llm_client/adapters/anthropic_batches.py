"""AA-1076 — the Anthropic Message Batches API adapter of the gateway batch lane (``shared/llm_client/batch_lane.py``).

One batch on one ``anthropic`` connection of the registry, through the official SDK (``client.messages.batches``):
``submit`` creates it, ``status`` reads its processing status, ``results`` streams its per-request results, ``cancel``
asks the API to stop it. The lane prices, ledgers and hands the results on; this module only talks to the API.

What it shares with the on-demand adapter (``adapters/anthropic_messages.py``), so a batch request and a live call
of the same stage send the same body:

- **Credential / endpoint / client**: the same ``connections.resolve_api_key`` (the ``aa-llm/<env>/<name>`` secret
  only, never an env var), ``check_base_url`` and the cached SDK client per connection (``_client_for``: the
  connection's timeout, no SDK retries).
- **Request params**: each request's ``params`` is ``anthropic_messages.build_params`` (model, max_tokens, system,
  messages, ``output_config`` effort / format, ``thinking``), the keys ``messages.stream`` takes; a non-streaming
  batch request takes the same ones.
- **Errors**: an ``AnthropicAPIError`` with the AA-900 class (``status_error_class``) on a refused create / retrieve.

Message Batches facts (Anthropic docs via the claude-api skill, 11/11/2026): up to 100,000 requests or 256 MB per
batch; most end within 1 hour, at most 24 hours (then the unfinished requests are ``expired``); results stay
available 29 days; every token is billed at 50% of the standard price, cache reads and writes included; prompt
caching works inside a batch (best effort). Results arrive in any order: keyed by ``custom_id``
(``^[a-zA-Z0-9_-]{1,64}$``), never by position.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

import structlog

from .. import connections
from . import anthropic_messages

logger = structlog.get_logger()

MAX_REQUESTS = 100_000
MAX_BYTES = 256 * 1024 * 1024
ENDED = "ended"


@dataclass
class BatchRequest:
    custom_id: str
    params: dict


@dataclass
class ItemOutcome:
    """One request's result, provider-neutral (the lane's ``ItemResult`` is built from it)."""
    custom_id: str
    outcome: str                       # succeeded | errored | expired | canceled
    text: str = ""
    stop_reason: Optional[str] = None
    stop_details: Optional[dict] = None
    provider_model: Optional[str] = None
    request_id: Optional[str] = None
    input_tokens: int = 0              # uncached part only (Messages API convention)
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cache_write_1h_tokens: int = 0
    inference_geo: Optional[str] = None
    error_type: Optional[str] = None
    error: Optional[str] = None
    extra: dict = field(default_factory=dict)


@dataclass
class BatchState:
    processing_status: str             # in_progress | canceling | ended
    counts: dict
    ended: bool


def _client(connection: connections.ProviderConnection):
    if connection.kind != "anthropic":
        raise connections.ConnectionCredentialError(
            f"connection {connection.connection_key!r} is a {connection.kind!r} connection, not an Anthropic one")
    if not connection.secret_name or connection.credential_env:
        raise connections.ConnectionCredentialError(
            f"connection {connection.connection_key!r}: an Anthropic connection takes its key from secret_name only")
    api_key = connections.resolve_api_key(connection)
    base_url = connection.base_url or anthropic_messages.DEFAULT_BASE_URL
    connections.check_base_url("anthropic", base_url)
    return anthropic_messages._client_for(connection, api_key, base_url)


def _call(connection: connections.ProviderConnection, fn):
    import anthropic
    try:
        return fn(_client(connection))
    except anthropic.APIStatusError as e:
        if getattr(e, "status_code", None) == 401:
            anthropic_messages.forget_connection(connection.connection_key)
        raise anthropic_messages._wrap_status_error(connection.connection_key, e) from e
    except anthropic.APIConnectionError as e:
        raise anthropic_messages.AnthropicAPIError(
            f"Anthropic API connection failed on connection {connection.connection_key!r}: "
            f"{type(e).__name__}: {str(e)[:200]}", anthropic_messages.OTHER) from e


def submit(connection: connections.ProviderConnection, requests: list[BatchRequest]) -> str:
    """Create one Message Batch; returns its id. Raises before any network call when the connection may not be used."""
    if not requests:
        raise ValueError("a batch needs at least one request")
    if len(requests) > MAX_REQUESTS:
        raise ValueError(f"{len(requests)} requests: a Message Batch takes at most {MAX_REQUESTS}")
    body = [{"custom_id": r.custom_id, "params": r.params} for r in requests]
    batch = _call(connection, lambda c: c.messages.batches.create(requests=body))
    logger.info("llm_batch_anthropic_created", connection=connection.connection_key, batch=batch.id,
                requests=len(body))
    return str(batch.id)


def status(connection: connections.ProviderConnection, batch_id: str) -> BatchState:
    batch = _call(connection, lambda c: c.messages.batches.retrieve(batch_id))
    counts_obj = getattr(batch, "request_counts", None)
    counts = {k: int(getattr(counts_obj, k, 0) or 0)
              for k in ("processing", "succeeded", "errored", "canceled", "expired")}
    st = str(getattr(batch, "processing_status", "") or "")
    return BatchState(processing_status=st, counts=counts, ended=st == ENDED)


def cancel(connection: connections.ProviderConnection, batch_id: str) -> None:
    _call(connection, lambda c: c.messages.batches.cancel(batch_id))


def _get(obj, name, default=None):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _int(obj, name) -> int:
    v = _get(obj, name)
    return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else 0


def outcome_of(entry) -> ItemOutcome:
    """One ``MessageBatchIndividualResponse`` (SDK object or its dict form) -> ItemOutcome."""
    custom_id = str(_get(entry, "custom_id") or "")
    result = _get(entry, "result")
    kind = str(_get(result, "type") or "errored")
    if kind == "succeeded":
        msg = _get(result, "message")
        usage = _get(msg, "usage")
        text = "".join(str(_get(b, "text") or "") for b in (_get(msg, "content") or [])
                       if _get(b, "type") == "text")
        details = _get(msg, "stop_details")
        if details is not None and hasattr(details, "model_dump"):
            details = details.model_dump()
        geo = _get(usage, "inference_geo")
        return ItemOutcome(
            custom_id=custom_id, outcome="succeeded", text=text, stop_reason=_get(msg, "stop_reason"),
            stop_details=details if isinstance(details, dict) else None, provider_model=_get(msg, "model"),
            request_id=_get(msg, "id"),
            input_tokens=_int(usage, "input_tokens"), output_tokens=_int(usage, "output_tokens"),
            cache_read_tokens=_int(usage, "cache_read_input_tokens"),
            cache_write_tokens=_int(usage, "cache_creation_input_tokens"),
            cache_write_1h_tokens=_int(_get(usage, "cache_creation"), "ephemeral_1h_input_tokens"),
            inference_geo=geo if isinstance(geo, str) else None)
    if kind == "errored":
        err = _get(result, "error")
        inner = _get(err, "error") or err
        etype = _get(inner, "type")
        return ItemOutcome(custom_id=custom_id, outcome="errored", error_type=str(etype) if etype else None,
                           error=str(_get(inner, "message") or etype or "errored")[:500])
    if kind in ("expired", "canceled"):
        return ItemOutcome(custom_id=custom_id, outcome=kind, error=kind)
    return ItemOutcome(custom_id=custom_id, outcome="errored", error=f"unknown result type {kind!r}")


def results(connection: connections.ProviderConnection, batch_id: str) -> Iterable[ItemOutcome]:
    """Every request's result of an ended batch (any order)."""
    entries = _call(connection, lambda c: list(c.messages.batches.results(batch_id)))
    return [outcome_of(e) for e in entries]
