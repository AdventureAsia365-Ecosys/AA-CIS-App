"""AA-1002 (gateway slice S2) — when a catalog model is served by a vendor API connection instead of today's path.

The only reader of the connection registry (``connections.py``) and the deployments (``deployments.py``) on the
call path. ``LLMClient`` asks it once per catalog-key attempt (both tables are cached in process, 5 minutes):

- ``vendor_deployment_for(model)``: a model's enabled deployments whose connection is enabled, lowest ``priority``
  first (``connection_key`` breaks ties). When the FIRST of them is on an ``anthropic`` connection, the model is
  called through the Anthropic Messages adapter; in every other case (no row, the table not migrated, the DB down,
  the Anthropic row disabled or its connection disabled, a Bedrock deployment ahead of it) the answer is None and
  the gateway takes today's path, byte for byte (tests/unit/test_aa1002_s2_vendor_route.py). Slice S2 calls only
  Anthropic connections through the registry: a Bedrock or OpenAI deployment ahead means "today's code", not a
  registry-driven call (slice S3 spreads a route over every deployment).
- ``pinned_deployment(key)``: an explicit ``model_tier`` of the form ``<model_key>@<connection_key>`` (a probe or a
  connection test, never a stage route: a stored route names catalog keys only) calls that one deployment, provided
  the deployment and its connection are enabled.

Only Anthropic-vendor catalog rows may run on an Anthropic connection; legacy keys (``haiku`` / ``sonnet`` /
``gpt-4.1``) never do in S2 (their aliasing is slice S3).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import structlog

from . import connections, deployments
from .catalog import LEGACY_KEYS, CatalogModel

logger = structlog.get_logger()

VENDOR_KINDS = ("anthropic",)
PIN_SEPARATOR = "@"


@dataclass(frozen=True)
class VendorDeployment:
    deployment: deployments.ModelDeployment
    connection: connections.ProviderConnection


def _first_enabled(model_key: str) -> Optional[VendorDeployment]:
    for d in deployments.deployments_for_sync(model_key):          # enabled only, priority order
        conn = connections.get_connection_sync(d.connection_key)
        if conn is None or not conn.enabled:
            continue
        return VendorDeployment(d, conn)
    return None


def _usable(model: CatalogModel, found: Optional[VendorDeployment]) -> Optional[VendorDeployment]:
    if found is None or found.connection.kind not in VENDOR_KINDS:
        return None
    if model.model_key in LEGACY_KEYS or model.vendor != "anthropic":
        logger.warning("llm_vendor_deployment_ignored", model=model.model_key, vendor=model.vendor,
                       connection=found.connection.connection_key,
                       reason="legacy key" if model.model_key in LEGACY_KEYS else "not an Anthropic model")
        return None
    return found


def vendor_deployment_for(model: CatalogModel) -> Optional[VendorDeployment]:
    """The Anthropic deployment to call for ``model``, or None for today's path. Never raises."""
    try:
        return _usable(model, _first_enabled(model.model_key))
    except Exception as e:  # noqa: BLE001 — a registry problem must never break today's path
        logger.warning("llm_vendor_route_failed", model=model.model_key, error=str(e)[:200])
        return None


def split_pin(key: str) -> Optional[tuple[str, str]]:
    """``(model_key, connection_key)`` for a ``model@connection`` key, else None."""
    if not key or PIN_SEPARATOR not in key:
        return None
    model_key, _, connection_key = key.partition(PIN_SEPARATOR)
    return (model_key, connection_key) if model_key and connection_key else None


def pinned_deployment(model: CatalogModel, connection_key: str) -> VendorDeployment:
    """The enabled deployment of ``model`` on ``connection_key`` (an Anthropic connection, enabled). Raises
    RuntimeError naming what is missing."""
    rows = [d for d in deployments.deployments_for_sync(model.model_key, enabled_only=False)
            if d.connection_key == connection_key]
    if not rows:
        raise RuntimeError(f"{model.model_key!r} has no deployment on connection {connection_key!r}")
    if not rows[0].enabled:
        raise RuntimeError(f"the deployment of {model.model_key!r} on {connection_key!r} is disabled")
    conn = connections.get_connection_sync(connection_key)
    if conn is None or not conn.enabled:
        raise RuntimeError(f"connection {connection_key!r} is missing or disabled")
    found = _usable(model, VendorDeployment(rows[0], conn))
    if found is None:
        raise RuntimeError(f"connection {connection_key!r} ({conn.kind}) cannot serve {model.model_key!r} in S2")
    return found
