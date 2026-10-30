"""AA-1002 (gateway slice S9) — the OpenAI-format Chat Completions client of one connection (catalog ``api_style``
``openai_chat``): the OpenAI API (kind ``openai``, including an OpenAI-compatible endpoint once its host is on the
kind's allowlist) and Azure OpenAI (kind ``azure_openai``).

``LLMClient._call_openai`` builds the request, checks and prices the reply; this module only answers "which SDK client
talks to this connection", per connection and at call time (before S9 one ``openai.OpenAI`` was built once in
``LLMClient.__init__`` from the task's ``OPENAI_API_KEY``, with the SDK's 600 s timeout and 2 retries):

- **Credential**: ``connections.resolve_api_key`` — the connection's ``aa-llm/<env>/<name>`` secret when it names
  one (read at call time, cached 15 minutes, dropped on a 401), else its allowlisted ``credential_env`` (today's
  ``OPENAI_API_KEY`` of openai-direct: only a fallback until the secret is set on the Connections tab). Fails closed
  (``ConnectionCredentialError``: disabled, policy problem, nothing readable), before any network call. Never logged.
- **Endpoint**: ``base_url`` must pass ``connections.check_base_url`` (the kind's host allowlist, a code constant)
  and is always passed to the SDK explicitly, so ``OPENAI_BASE_URL`` in the environment cannot redirect a key. kind
  ``openai`` defaults to ``https://api.openai.com/v1``; kind ``azure_openai`` must name its resource endpoint
  (``https://<resource>.openai.azure.com``) and calls ``openai.AzureOpenAI`` with ``api_version`` (the deployment's
  ``request_defaults.api_version``, else ``AZURE_API_VERSION``); the deployment's ``wire_id`` is the Azure deployment
  name.
- **Timeout / retries**: per connection (``timeout_s``, ``max_retries``); NULL means ``DEFAULT_TIMEOUT_S`` /
  ``DEFAULT_MAX_RETRIES`` (the Bedrock client's 300 s / 2 attempts, as the Anthropic adapter), never the SDK's 600 s
  and 2 retries, so one attempt can no longer hold a route for ~30 minutes.
- One client per connection settings, rebuilt when the key changes (the key is kept beside the client and compared
  directly, never hashed: CodeQL py/weak-sensitive-data-hashing). ``forget_connection`` drops it (a 401, an admin
  edit).
"""
from __future__ import annotations

import hmac
import re
import threading
from typing import Any, Callable, Optional

from .. import connections

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_TIMEOUT_S = 300
DEFAULT_MAX_RETRIES = 1
CONNECT_TIMEOUT_S = 10.0
# Azure OpenAI data-plane API version (GA). A deployment may pin another one in request_defaults.api_version.
AZURE_API_VERSION = "2024-10-21"
_API_VERSION_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(-preview)?$")
KINDS = ("openai", "azure_openai")

# Tests install a factory returning an httpx client on a fake transport. None in production.
_http_client_factory: Optional[Callable[[], Any]] = None

_clients: dict[tuple, Any] = {}
_clients_lock = threading.Lock()


def _settings(conn: connections.ProviderConnection) -> tuple[int, int]:
    timeout_s = int(conn.timeout_s) if conn.timeout_s else DEFAULT_TIMEOUT_S
    retries = int(conn.max_retries) if conn.max_retries is not None else DEFAULT_MAX_RETRIES
    return timeout_s, retries


def api_version_of(request_defaults: Optional[dict]) -> str:
    """The Azure API version: the deployment's ``request_defaults.api_version`` when it is a dated version string,
    else ``AZURE_API_VERSION``."""
    v = (request_defaults or {}).get("api_version") if isinstance(request_defaults, dict) else None
    return v if isinstance(v, str) and _API_VERSION_RE.fullmatch(v) else AZURE_API_VERSION


def endpoint_of(conn: connections.ProviderConnection) -> str:
    """The checked endpoint of an OpenAI-format connection. Raises ConnectionPolicyError / ConnectionCredentialError."""
    if conn.kind == "openai":
        url = conn.base_url or DEFAULT_BASE_URL
    elif conn.kind == "azure_openai":
        if not conn.base_url:
            raise connections.ConnectionCredentialError(
                f"connection {conn.connection_key!r}: an Azure OpenAI connection needs its resource endpoint "
                "(base_url https://<resource>.openai.azure.com)")
        url = conn.base_url
    else:
        raise connections.ConnectionCredentialError(
            f"connection {conn.connection_key!r} is a {conn.kind!r} connection, not an OpenAI-format one")
    connections.check_base_url(conn.kind, url)
    return url


def client_for(conn: connections.ProviderConnection, request_defaults: Optional[dict] = None):
    """The SDK client of ``conn`` (``openai.OpenAI`` / ``openai.AzureOpenAI``), built on the connection's key,
    endpoint, timeout and retries. Raises ConnectionCredentialError / ConnectionPolicyError before any network call."""
    import openai

    api_key = connections.resolve_api_key(conn)              # enabled + policy (secret prefix, base_url) first
    url = endpoint_of(conn)
    timeout_s, retries = _settings(conn)
    version = api_version_of(request_defaults) if conn.kind == "azure_openai" else None
    cache_key = (conn.connection_key, conn.kind, url, timeout_s, retries, version)
    with _clients_lock:
        hit = _clients.get(cache_key)
    if hit is not None and hmac.compare_digest(hit[0], api_key):
        return hit[1]
    kwargs: dict = {
        "api_key": api_key,
        "timeout": openai.Timeout(float(timeout_s), connect=min(CONNECT_TIMEOUT_S, float(timeout_s))),
        "max_retries": retries,
    }
    if _http_client_factory is not None:
        kwargs["http_client"] = _http_client_factory()
    if conn.kind == "azure_openai":
        client = openai.AzureOpenAI(azure_endpoint=url, api_version=version, **kwargs)
    else:
        client = openai.OpenAI(base_url=url, **kwargs)
    with _clients_lock:
        for stale in [k for k in _clients if k[0] == conn.connection_key]:
            _clients.pop(stale, None)
        _clients[cache_key] = (api_key, client)
    return client


def forget_connection(connection_key: str) -> None:
    """Drop the cached client and key of a connection (a 401, an admin edit)."""
    with _clients_lock:
        for stale in [k for k in _clients if k[0] == connection_key]:
            _clients.pop(stale, None)
    connections.invalidate_credential(connection_key)


def is_auth_error(e: BaseException) -> bool:
    """A 401 from the provider (the key is wrong or rotated): the caller drops the cached key and client."""
    return getattr(e, "status_code", None) == 401
