"""AA-1002 (gateway slice S1) — read side of shared.llm_model_deployment.

A deployment is one catalog model on one connection (``connections.py``): the wire id that connection calls (Bedrock
inference profile, vendor model name), a priority (lower first), price overrides and request defaults. Migration 304
backfilled the paths today's code takes. From slice S3 (``LLM_DEPLOYMENT_ROUTING`` on, the default) the gateway sends
``wire_id`` on the wire; ``bedrock_profile_ids`` / ``wire_model`` in the catalog stay what TripPlanner calls (until
slice S8) and what the gateway calls only with routing off or for a model without deployment rows. Nothing in the DB
ties the two: a migration that changes one must change the other in the same file. Guarded in CI by
``tests/integration/test_aa1002_deployment_wire_drift.py`` (after every migration) and at run time by the
``llm_deployment_wire_drift`` warning (``vendor_route.deployment_plan``).

Slice S3 routes through it (``vendor_route.deployment_plan`` spreads a model over its enabled deployments) and adds the
alias read: ``shared.llm_model_alias`` (migration 307) maps a legacy key (``haiku`` / ``sonnet``) to the ordered catalog
models its chain tries, each called through its own deployments (``LLMClient`` with ``LLM_LEGACY_ALIASES`` on).
Read-only. Never raises on the sync read: a DB failure or a table that is not migrated yet means no deployments / no
aliases, and the two reads fail independently (the alias table may be missing while the deployments load).
"""
from __future__ import annotations

import json
import threading
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


@dataclass(frozen=True)
class ModelAlias:
    alias_key: str
    position: int
    model_key: str


_ALIAS_SQL = "SELECT alias_key, position, model_key FROM shared.llm_model_alias ORDER BY alias_key, position"


async def fetch_deployments(conn) -> list[ModelDeployment]:
    return [_row_to_deployment(r) for r in await conn.fetch(_SELECT_SQL)]


async def _fetch_all() -> list[ModelDeployment]:
    conn = await connections._connect()
    try:
        return await fetch_deployments(conn)
    finally:
        await conn.close()


async def _fetch_aliases() -> list[ModelAlias]:
    conn = await connections._connect()
    try:
        return [ModelAlias(r["alias_key"], int(r["position"]), r["model_key"]) for r in await conn.fetch(_ALIAS_SQL)]
    finally:
        await conn.close()


_by_model: dict[str, list[ModelDeployment]] = {}
_aliases: dict[str, tuple[str, ...]] = {}
_loaded_at = 0.0
_failed_at = 0.0


_load_lock = threading.Lock()      # one reload at a time (connections._ensure_loaded)


def _fresh(now: float) -> bool:
    return bool((_loaded_at and now - _loaded_at < _CACHE_TTL_SECONDS)
                or (_failed_at and now - _failed_at < _FAILURE_TTL_SECONDS))


def _ensure_loaded() -> None:
    if _fresh(time.monotonic()):
        return
    with _load_lock:
        now = time.monotonic()
        if _fresh(now):
            return              # another thread reloaded while this one waited
        _load(now)


def _load_aliases() -> None:
    global _aliases
    try:
        grouped: dict[str, list[ModelAlias]] = {}
        for a in connections.run_sync(_fetch_aliases):
            grouped.setdefault(a.alias_key, []).append(a)
        _aliases = {k: tuple(a.model_key for a in sorted(v, key=lambda a: a.position)) for k, v in grouped.items()}
    except Exception as e:  # noqa: BLE001 — never raises (table before 307, DB down): the last good read stays, and
        # none at all (a cold process) means no alias = the legacy chain
        logger.warning("llm_alias_registry_read_failed", error=str(e)[:300])


def _load(now: float) -> None:
    global _by_model, _loaded_at, _failed_at
    _load_aliases()
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


def has_rows_sync(model_key: str) -> bool:
    """Whether ``model_key`` has any deployment row, enabled or not (False when the table cannot be read)."""
    _ensure_loaded()
    return bool(_by_model.get(model_key))


def alias_targets_sync(alias_key: str) -> tuple[str, ...]:
    """The catalog models an alias key runs, in order (empty: no alias row, or the table cannot be read)."""
    _ensure_loaded()
    return _aliases.get(alias_key, ())


def invalidate() -> None:
    global _loaded_at, _failed_at
    _loaded_at = 0.0
    _failed_at = 0.0
