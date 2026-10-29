"""AA-1002 (gateway slice S1) — read side of shared.llm_model_deployment.

A deployment is one catalog model on one connection (``connections.py``): the wire id that connection calls (Bedrock
inference profile, vendor model name), a priority (lower first), price overrides and request defaults. Migration 304
backfilled the paths today's code takes; ``bedrock_profile_ids`` in the catalog is still what the gateway and
TripPlanner call.

Read-only and off the routing path in S1 (slice S3 spreads a route over a model's enabled deployments). Never raises
on the sync read: a DB failure or a table that is not migrated yet means no deployments.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Optional

import structlog

from . import connections

logger = structlog.get_logger()

_CACHE_TTL_SECONDS = 300.0
_FAILURE_TTL_SECONDS = 60.0


@dataclass(frozen=True)
class ModelDeployment:
    model_key: str
    connection_key: str
    wire_id: str
    priority: int
    enabled: bool
    origin: str
    price_in_override: Optional[float] = None
    price_out_override: Optional[float] = None
    price_cache_read_override: Optional[float] = None
    price_cache_write_override: Optional[float] = None
    price_cache_write_1h_override: Optional[float] = None
    batch_capable: bool = False
    request_defaults: dict = field(default_factory=dict, compare=False)
    notes: Optional[str] = None


_PRICE_COLUMNS = ("price_in_override", "price_out_override", "price_cache_read_override",
                  "price_cache_write_override", "price_cache_write_1h_override")
_COLUMNS = ("model_key", "connection_key", "wire_id", "priority", "enabled", "origin", *_PRICE_COLUMNS,
            "batch_capable", "request_defaults", "notes")
_SELECT_SQL = (f"SELECT {', '.join(_COLUMNS)} FROM shared.llm_model_deployment "
               "ORDER BY model_key, priority, connection_key")


def _row_to_deployment(row) -> ModelDeployment:
    d = {k: row[k] for k in _COLUMNS}
    for k in _PRICE_COLUMNS:
        d[k] = float(d[k]) if d[k] is not None else None
    defaults = d["request_defaults"]
    if isinstance(defaults, str):
        defaults = json.loads(defaults)
    d["request_defaults"] = dict(defaults or {})
    return ModelDeployment(**d)


async def fetch_deployments(conn) -> list[ModelDeployment]:
    return [_row_to_deployment(r) for r in await conn.fetch(_SELECT_SQL)]


async def _fetch_all() -> list[ModelDeployment]:
    conn = await connections._connect()
    try:
        return await fetch_deployments(conn)
    finally:
        await conn.close()


_by_model: dict[str, list[ModelDeployment]] = {}
_loaded_at = 0.0
_failed_at = 0.0


def _ensure_loaded() -> None:
    global _by_model, _loaded_at, _failed_at
    now = time.monotonic()
    if _loaded_at and now - _loaded_at < _CACHE_TTL_SECONDS:
        return
    if _failed_at and now - _failed_at < _FAILURE_TTL_SECONDS:
        return
    try:
        rows = connections.run_sync(_fetch_all)
        grouped: dict[str, list[ModelDeployment]] = {}
        for d in rows:
            grouped.setdefault(d.model_key, []).append(d)
        _by_model = {k: sorted(v, key=lambda d: (d.priority, d.connection_key)) for k, v in grouped.items()}
        _loaded_at, _failed_at = now, 0.0
    except Exception as e:  # noqa: BLE001 — never raises (missing table before 304, DB down)
        _failed_at = now
        logger.warning("llm_deployment_registry_read_failed", error=str(e)[:300])


def deployments_for_sync(model_key: str, *, enabled_only: bool = True) -> list[ModelDeployment]:
    """A model's deployments, lowest priority first (connection_key breaks ties)."""
    _ensure_loaded()
    rows = _by_model.get(model_key, [])
    return [d for d in rows if d.enabled] if enabled_only else list(rows)


def invalidate() -> None:
    global _loaded_at, _failed_at
    _loaded_at = 0.0
    _failed_at = 0.0
