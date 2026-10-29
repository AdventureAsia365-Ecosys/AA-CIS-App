"""AA-1002 (gateway slice S2) — the Anthropic Messages API adapter (catalog ``api_style`` ``anthropic_messages``).

One request on one ``anthropic`` connection of the registry (``shared.llm_provider_connection``, migration 304),
through the official SDK (``anthropic``, pinned in requirements.txt; 2.x sends API version 2026-10-21). ``send``
returns plain values (``MessagesResult``); ``LLMClient._call_anthropic_messages`` checks the reply, prices it and
builds the ``LLMResponse``.

What the adapter guarantees, whatever the row says:

- **Credential**: the API key is read at call time from the connection's Secrets Manager secret
  (``connections.resolve_api_key``: the name must be ``aa-llm/<env>/<name>``, the connection enabled, no policy
  problem) — never from ``ANTHROPIC_API_KEY`` or another env var of the task: a row without ``secret_name`` or with
  a ``credential_env`` is refused before anything is read.
- **Endpoint**: ``base_url`` must pass ``connections.check_base_url`` (host allowlist per kind, a code constant), and
  it is always passed to the SDK explicitly, so ``ANTHROPIC_BASE_URL`` in the environment cannot redirect a key.
- **Timeout / retries**: per connection (``timeout_s``, ``max_retries``); a NULL column means ``DEFAULT_TIMEOUT_S`` /
  ``DEFAULT_MAX_RETRIES``, never the SDK's 600 s and 2 retries. The SDK honours ``retry-after`` on the retries it
  makes; the error the gateway sees carries ``retry_after_s`` for the breaker of slice S4.
- **Request shape** (``build_params``): no ``temperature`` / ``top_p`` / ``top_k`` (refused on API version 2026-10-21
  and by Sonnet / Opus 5.5), no ``tools`` / ``tool_choice`` (Sonnet / Opus 5.5 refuse a forced tool), no ``thinking``
  field (the model's default), no server-side ``fallbacks`` (it would switch the model unseen, ADR 0005 §4), no beta
  header. Structured output goes through ``output_config.format`` (``json_schema``); ``output_config.effort`` is set
  explicitly (``resolve_effort``) so the same model answers at the same effort on every way in.
- **Errors**: an ``AnthropicAPIError`` (a RuntimeError) whose ``llm_error_class`` is the AA-900 class
  (errors.py): 401 / 403 → ``access_denied`` (account level: the gateway raises ``LLMAccessDenied`` when every model
  was refused that way), 402 / a credit or spend-limit refusal (wording read only on 400 / 402 / 403) →
  ``quota_exhausted``, 429 / 529 → ``throttled`` (checked first, never from the text), anything else → ``other``.
  A 401 also drops the cached key and client, so a rotated key is read on the next call.
- **Usage**: ``input_tokens`` of the Messages API excludes the cache; ``MessagesResult`` keeps the parts and the
  client adds them up like the Converse path (``tokens_in`` = uncached + cache read + cache write). ``provider_model``
  is the ``model`` the API says answered.
"""
from __future__ import annotations

import copy
import hashlib
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import structlog

from .. import connections
from ..errors import ACCESS_DENIED, OTHER, QUOTA_EXHAUSTED, THROTTLED, classify_text

logger = structlog.get_logger()

DEFAULT_BASE_URL = "https://api.anthropic.com"
DEFAULT_TIMEOUT_S = 300            # the Bedrock client's read timeout (client.py)
DEFAULT_MAX_RETRIES = 1            # the Bedrock client's max_attempts=2
CONNECT_TIMEOUT_S = 10.0

EFFORTS = ("low", "medium", "high", "xhigh", "max")

# The default effort of each model family on the Messages API (what Bedrock Converse runs today, since the gateway
# sends no effort there). Sent explicitly, so the same model on another way in answers at the same effort.
# Matched against the wire id: the family, optionally with a date suffix ("claude-haiku-4-5-20251001").
_EFFORT_DEFAULTS: dict[str, Optional[str]] = {
    "claude-opus-5-5": "medium",
    "claude-haiku-5-5": "medium",
    "claude-sonnet-5-5": "high",
    "claude-sonnet-5": "high",
    "claude-opus-5": "high",
    "claude-fable-5-1": "high",
    "claude-fable-5": "high",
    "claude-opus-4-8": "high",
    "claude-opus-4-7": "high",
    "claude-opus-4-6": "high",
    "claude-sonnet-4-6": "high",
    # no effort parameter on these: sending one is a 400
    "claude-haiku-4-5": None,
    "claude-sonnet-4-5": None,
}

# JSON Schema keywords structured outputs do not take (numeric / string bounds, complex array bounds). They are
# removed from the schema SENT; the reply is still conformed to the caller's full schema (client._checked_json).
_UNSUPPORTED_KEYWORDS = frozenset({"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
                                   "minLength", "maxLength", "maxItems", "uniqueItems", "minProperties",
                                   "maxProperties"})

# Tests install a factory returning an ``anthropic.DefaultHttpxClient`` on a fake transport. None in production.
_http_client_factory: Optional[Callable[[], Any]] = None


class AnthropicAPIError(RuntimeError):
    """A failed Messages API request. ``llm_error_class`` is read by errors.classify (AA-900)."""

    def __init__(self, message: str, error_class: str = OTHER, status_code: Optional[int] = None,
                 error_type: Optional[str] = None, retry_after_s: Optional[float] = None,
                 request_id: Optional[str] = None):
        super().__init__(message, error_class, status_code, error_type, retry_after_s, request_id)
        self.llm_error_class = error_class
        self.status_code = status_code
        self.error_type = error_type
        self.retry_after_s = retry_after_s
        self.request_id = request_id

    def __str__(self) -> str:
        return str(self.args[0])


@dataclass
class MessagesResult:
    text: str
    provider_model: Optional[str]
    stop_reason: Optional[str]
    stop_details: Optional[dict]
    input_tokens: int                 # uncached part only (Messages API convention)
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    structured: bool                  # output_config.format was sent
    effort: Optional[str]
    request_id: Optional[str] = None
    params: dict = field(default_factory=dict, repr=False)


# ── request ──────────────────────────────────────────────────────────────────────────────────────────────────────

def _effort_facts(wire_id: str) -> tuple[bool, Optional[str]]:
    """(known family, default effort or None when the family takes no effort)."""
    for family, default in _EFFORT_DEFAULTS.items():
        if re.fullmatch(re.escape(family) + r"(-\d{8})?", wire_id or ""):
            return True, default
    return False, None


_EFFORT_WARNED: set[str] = set()


def resolve_effort(wire_id: str, stage: Optional[str], request_defaults: Optional[dict]) -> Optional[str]:
    """The effort to send: the deployment's ``request_defaults.effort_by_stage[stage]``, else its ``effort``, else
    the model family's default. None when the family takes no effort (Haiku 4.5, Sonnet 4.5) or is unknown and no
    effort is configured (warned once per wire id)."""
    defaults = request_defaults if isinstance(request_defaults, dict) else {}
    by_stage = defaults.get("effort_by_stage")
    configured = by_stage.get(stage) if isinstance(by_stage, dict) and stage else None
    configured = configured or defaults.get("effort")
    known, family_default = _effort_facts(wire_id)
    if known and family_default is None:
        if configured:
            logger.warning("llm_anthropic_effort_ignored", wire_id=wire_id, effort=configured,
                           reason="this model takes no effort parameter")
        return None
    if configured in EFFORTS:
        return configured
    if configured is not None:
        logger.warning("llm_anthropic_effort_invalid", wire_id=wire_id, effort=configured, used=family_default)
    if not known and wire_id not in _EFFORT_WARNED:
        _EFFORT_WARNED.add(wire_id)
        logger.warning("llm_anthropic_effort_unset", wire_id=wire_id,
                       hint="unknown model family: set request_defaults.effort on its deployment")
    return family_default


def api_schema(schema: dict) -> Optional[dict]:
    """The caller's JSON schema in the shape ``output_config.format`` takes: every object closed
    (``additionalProperties: false``) and the unsupported keywords removed. None when it cannot be expressed (an
    object with no ``properties`` or an open / typed ``additionalProperties``): the request then asks for the JSON in
    the system prompt instead."""
    def walk(node):
        if isinstance(node, list):
            out = [walk(n) for n in node]
            return None if any(o is _REFUSE for o in out) else out
        if not isinstance(node, dict):
            return node
        out = {}
        for k, v in node.items():
            if k in _UNSUPPORTED_KEYWORDS or (k == "minItems" and isinstance(v, int) and v > 1):
                continue
            if k in ("properties", "$defs", "definitions", "patternProperties"):
                if not isinstance(v, dict):
                    return _REFUSE
                sub = {name: walk(s) for name, s in v.items()}
                if any(s is _REFUSE for s in sub.values()):
                    return _REFUSE
                out[k] = sub
            elif k in ("items", "anyOf", "allOf", "oneOf", "not", "prefixItems"):
                sub = walk(v)
                if sub is _REFUSE or sub is None:
                    return _REFUSE
                out[k] = sub
            else:
                out[k] = copy.deepcopy(v)
        is_object = out.get("type") == "object" or (isinstance(out.get("type"), list) and "object" in out["type"])
        if is_object:
            if not isinstance(out.get("properties"), dict) or out.get("additionalProperties") not in (None, False):
                return _REFUSE
            out["additionalProperties"] = False
        return out

    result = walk(schema)
    return None if result is _REFUSE else result


_REFUSE = object()


def _schema_instruction(schema: dict) -> str:
    import json
    return (f"Answer with exactly one JSON object that matches the JSON schema {schema['name']!r} below, and nothing "
            "else: no prose before or after it, no markdown, no code fence.\n"
            f"JSON schema:\n{json.dumps(schema['schema'], ensure_ascii=False, sort_keys=True)}")


def build_params(*, wire_id: str, system: str, user: str, max_tokens: int, stage: Optional[str],
                 json_schema: Optional[dict] = None, request_defaults: Optional[dict] = None) -> dict:
    """The keyword arguments of ``messages.stream``. Only these keys can appear: model, max_tokens, system, messages,
    output_config (format / effort). Pinned by tests/unit/test_aa1002_anthropic_messages.py."""
    defaults = request_defaults if isinstance(request_defaults, dict) else {}
    output_config: dict = {}
    effort = resolve_effort(wire_id, stage, defaults)
    if effort:
        output_config["effort"] = effort
    if json_schema:
        sent = api_schema(json_schema["schema"])
        if sent is not None:
            output_config["format"] = {"type": "json_schema", "schema": sent}
        else:
            logger.warning("llm_anthropic_schema_not_expressible", stage=stage, schema=json_schema.get("name"),
                           action="schema asked in the system prompt; reply still conformed")
            system = f"{system}\n\n{_schema_instruction(json_schema)}"
    if defaults.get("cache_system_prompt") is True:
        system_param: Any = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
    else:
        system_param = system
    params = {
        "model": wire_id,
        "max_tokens": max_tokens,
        "system": system_param,
        "messages": [{"role": "user", "content": user}],
    }
    if output_config:
        params["output_config"] = output_config
    return params


# ── client ───────────────────────────────────────────────────────────────────────────────────────────────────────

_clients: dict[tuple, Any] = {}
_clients_lock = threading.Lock()


def _settings(conn: connections.ProviderConnection) -> tuple[int, int]:
    timeout_s = int(conn.timeout_s) if conn.timeout_s else DEFAULT_TIMEOUT_S
    retries = int(conn.max_retries) if conn.max_retries is not None else DEFAULT_MAX_RETRIES
    return timeout_s, retries


def _client_for(conn: connections.ProviderConnection, api_key: str, base_url: str):
    import anthropic

    timeout_s, retries = _settings(conn)
    fingerprint = hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]   # the cache key never holds the key
    cache_key = (conn.connection_key, base_url, timeout_s, retries, fingerprint)
    with _clients_lock:
        hit = _clients.get(cache_key)
    if hit is not None:
        return hit
    kwargs: dict = {
        "api_key": api_key,
        "base_url": base_url,
        "timeout": anthropic.Timeout(float(timeout_s), connect=min(CONNECT_TIMEOUT_S, float(timeout_s))),
        "max_retries": retries,
    }
    if _http_client_factory is not None:
        kwargs["http_client"] = _http_client_factory()
    client = anthropic.Anthropic(**kwargs)
    with _clients_lock:
        for stale in [k for k in _clients if k[0] == conn.connection_key]:
            _clients.pop(stale, None)
        _clients[cache_key] = client
    return client


def forget_connection(connection_key: str) -> None:
    """Drop the cached client and key of a connection (a 401, an admin toggle)."""
    with _clients_lock:
        for stale in [k for k in _clients if k[0] == connection_key]:
            _clients.pop(stale, None)
    connections.invalidate_credential(connection_key)


# ── errors ───────────────────────────────────────────────────────────────────────────────────────────────────────

# The statuses whose MESSAGE may say "no credit / spend limit reached" (Anthropic answers a workspace spend limit or
# an empty credit balance with a 400 invalid_request_error). Never a 429 / 529: a rate limit is retried with
# backoff, and reading its free text first could end a job as account-level on a word like "billing".
_QUOTA_TEXT_STATUSES = (400, 402, 403)


def status_error_class(status: Optional[int], error_type: Optional[str], text: str = "") -> str:
    """The AA-900 class of an Anthropic API status error. Order: rate limit / overload first (status or type, never
    the text), then billing (402 / billing_error), then the credit / spend-limit wording, only on 400 / 402 / 403 and
    with the same patterns ``errors.classify_text`` uses (so a stored job error classifies the same), then 401 / 403
    access denied."""
    if status in (429, 529) or error_type in ("rate_limit_error", "overloaded_error"):
        return THROTTLED
    if status == 402 or error_type == "billing_error":
        return QUOTA_EXHAUSTED
    if status in _QUOTA_TEXT_STATUSES and classify_text(text) == QUOTA_EXHAUSTED:
        return QUOTA_EXHAUSTED
    if status in (401, 403) or error_type in ("authentication_error", "permission_error"):
        return ACCESS_DENIED
    return OTHER


def _retry_after(headers) -> Optional[float]:
    if headers is None:
        return None
    for name, scale in (("retry-after-ms", 0.001), ("retry-after", 1.0)):
        raw = headers.get(name)
        if raw:
            try:
                return float(raw) * scale
            except ValueError:
                continue
    return None


def _wrap_status_error(conn_key: str, e) -> AnthropicAPIError:
    status = getattr(e, "status_code", None)
    error_type = getattr(e, "type", None)
    text = str(getattr(e, "message", "") or e)
    cls = status_error_class(status, error_type, text)
    response = getattr(e, "response", None)
    return AnthropicAPIError(
        f"Anthropic API {status} {error_type or 'error'} on connection {conn_key!r}: {text[:300]}",
        cls, status, error_type, _retry_after(getattr(response, "headers", None)), getattr(e, "request_id", None))


# ── send ─────────────────────────────────────────────────────────────────────────────────────────────────────────

def send(*, connection: connections.ProviderConnection, wire_id: str, system: str, user: str, max_tokens: int,
         stage: Optional[str], json_schema: Optional[dict] = None, request_defaults: Optional[dict] = None,
         on_delta: Optional[Callable[[str], None]] = None) -> MessagesResult:
    """One streamed Messages API request. Raises ConnectionCredentialError / ConnectionPolicyError before any
    network call when the connection may not be used, AnthropicAPIError when the API refuses or the request fails.
    A reply that arrived (any stop_reason, refusal included) is returned: the caller decides whether it is usable."""
    import anthropic

    if connection.kind != "anthropic":
        raise connections.ConnectionCredentialError(
            f"connection {connection.connection_key!r} is a {connection.kind!r} connection, not an Anthropic one")
    if not connection.secret_name or connection.credential_env:
        # the key comes from the aa-llm/ secret only, never from an env var of the task (policy_problems also refuses
        # a credential_env of another kind; this guard keeps the adapter closed whatever the policy lists say)
        raise connections.ConnectionCredentialError(
            f"connection {connection.connection_key!r}: an Anthropic connection takes its key from secret_name only")
    api_key = connections.resolve_api_key(connection)          # enabled + policy (secret prefix, base_url) first
    base_url = connection.base_url or DEFAULT_BASE_URL
    connections.check_base_url("anthropic", base_url)
    client = _client_for(connection, api_key, base_url)
    params = build_params(wire_id=wire_id, system=system, user=user, max_tokens=max_tokens, stage=stage,
                          json_schema=json_schema, request_defaults=request_defaults)
    try:
        with client.messages.stream(**params) as stream:
            for event in stream:
                if (on_delta is not None and getattr(event, "type", None) == "content_block_delta"
                        and getattr(event.delta, "type", None) == "text_delta"):
                    on_delta(event.delta.text)
            final = stream.get_final_message()
            request_id = getattr(stream, "request_id", None)
    except anthropic.APIStatusError as e:
        if getattr(e, "status_code", None) == 401:
            forget_connection(connection.connection_key)
        raise _wrap_status_error(connection.connection_key, e) from e
    except anthropic.APIConnectionError as e:          # APITimeoutError included
        raise AnthropicAPIError(f"Anthropic API connection failed on connection {connection.connection_key!r}: "
                                f"{type(e).__name__}: {str(e)[:200]}", OTHER) from e

    usage = final.usage
    stop_details = getattr(final, "stop_details", None)
    if stop_details is not None and hasattr(stop_details, "model_dump"):
        stop_details = stop_details.model_dump()
    return MessagesResult(
        text="".join(getattr(b, "text", "") for b in final.content if getattr(b, "type", None) == "text"),
        provider_model=getattr(final, "model", None),
        stop_reason=getattr(final, "stop_reason", None),
        stop_details=stop_details if isinstance(stop_details, dict) else None,
        input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
        output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
        cache_read_tokens=int(getattr(usage, "cache_read_input_tokens", 0) or 0),
        cache_write_tokens=int(getattr(usage, "cache_creation_input_tokens", 0) or 0),
        structured=bool(params.get("output_config", {}).get("format")),
        effort=params.get("output_config", {}).get("effort"),
        request_id=request_id,
        params=params,
    )
