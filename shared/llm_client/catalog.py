"""AA-658 / ADR 0005 — read side of shared.llm_model_catalog.

The whole table is small, so it is loaded in one query and cached in-process. Never raises: on a
DB failure every lookup returns None and callers fall back (pricing.py, the legacy LLMClient
chain). A failed load is remembered for a short while so a DB outage — or a unit-test run with
no database — costs one connection attempt, not one per LLM call.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

import asyncpg
import structlog

from shared.secrets import get_database_url

logger = structlog.get_logger()

_CACHE_TTL_SECONDS = 300.0
_FAILURE_TTL_SECONDS = 60.0
_CONNECT_TIMEOUT_SECONDS = 3.0

LEGACY_KEYS = frozenset({"haiku", "sonnet", "gpt-4.1"})


@dataclass(frozen=True)
class CatalogModel:
    model_key: str
    label: str
    vendor: str
    provider: str
    api_style: str
    bedrock_profile_ids: dict = field(default_factory=dict)
    wire_model: Optional[str] = None
    callable_via: tuple = ()
    supports_temperature: bool = True
    max_output_tokens: Optional[int] = None
    price_in_per_mtok: Optional[float] = None
    price_out_per_mtok: Optional[float] = None
    price_cache_read_per_mtok: Optional[float] = None
    price_cache_write_per_mtok: Optional[float] = None
    enabled: bool = False
    blocked_reason: Optional[str] = None

    def account_for(self, preferred: Optional[str]) -> Optional[str]:
        """The preferred satellite account if this model is served there, else the first account
        that serves it (acc3 before acc1 before acc2)."""
        if preferred and preferred in self.bedrock_profile_ids:
            return preferred
        for acct in ("acc3", "acc1", "acc2"):
            if acct in self.bedrock_profile_ids:
                return acct
        return None


_models: dict[str, CatalogModel] = {}
_loaded_at: float = 0.0
_failed_at: float = 0.0


def _num(v) -> Optional[float]:
    return float(v) if v is not None else None


def _row_to_model(row) -> CatalogModel:
    profiles = row["bedrock_profile_ids"]
    if isinstance(profiles, str):
        import json
        profiles = json.loads(profiles)
    return CatalogModel(
        model_key=row["model_key"], label=row["label"], vendor=row["vendor"],
        provider=row["provider"], api_style=row["api_style"],
        bedrock_profile_ids=dict(profiles or {}), wire_model=row["wire_model"],
        callable_via=tuple(row["callable_via"] or ()),
        supports_temperature=row["supports_temperature"],
        max_output_tokens=row["max_output_tokens"],
        price_in_per_mtok=_num(row["price_in_per_mtok"]),
        price_out_per_mtok=_num(row["price_out_per_mtok"]),
        price_cache_read_per_mtok=_num(row["price_cache_read_per_mtok"]),
        price_cache_write_per_mtok=_num(row["price_cache_write_per_mtok"]),
        enabled=row["enabled"], blocked_reason=row["blocked_reason"],
    )


_SELECT_SQL = (
    "SELECT model_key, label, vendor, provider, api_style, bedrock_profile_ids, wire_model, "
    "callable_via, supports_temperature, max_output_tokens, price_in_per_mtok, price_out_per_mtok, "
    "price_cache_read_per_mtok, price_cache_write_per_mtok, enabled, blocked_reason "
    "FROM shared.llm_model_catalog ORDER BY model_key"
)


async def _fetch_all() -> dict[str, CatalogModel]:
    conn = await asyncpg.connect(get_database_url(), ssl="require", timeout=_CONNECT_TIMEOUT_SECONDS)
    try:
        rows = await conn.fetch(_SELECT_SQL)
        return {r["model_key"]: _row_to_model(r) for r in rows}
    finally:
        await conn.close()


def _run_fetch_sync() -> dict[str, CatalogModel]:
    import asyncio
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_fetch_all())
    # Called from inside a running loop (a plain `def` used directly by async code) — same
    # workaround as role_config.get_stage_config_sync().
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(lambda: asyncio.run(_fetch_all())).result(timeout=10)


def _ensure_loaded() -> None:
    global _models, _loaded_at, _failed_at
    now = time.monotonic()
    if _loaded_at and now - _loaded_at < _CACHE_TTL_SECONDS:
        return
    if _failed_at and now - _failed_at < _FAILURE_TTL_SECONDS:
        return
    try:
        _models = _run_fetch_sync()
        _loaded_at = now
        _failed_at = 0.0
    except Exception as e:
        _failed_at = now
        logger.warning("llm_model_catalog_read_failed", error=str(e),
                       hint="using stale catalog if any, else pricing.py / legacy chain")


def get_model_sync(model_key: str) -> Optional[CatalogModel]:
    """Accepts a Model Key, optionally with the "satellite-" label prefix LLMResponse uses."""
    if not model_key:
        return None
    if model_key.startswith("satellite-"):
        model_key = model_key[len("satellite-"):]
    _ensure_loaded()
    return _models.get(model_key)


async def list_models() -> list[CatalogModel]:
    """Admin read — always fresh, and refreshes the in-process cache as a side effect."""
    global _models, _loaded_at, _failed_at
    models = await _fetch_all()
    _models, _loaded_at, _failed_at = models, time.monotonic(), 0.0
    return list(models.values())


def invalidate() -> None:
    global _loaded_at, _failed_at
    _loaded_at = 0.0
    _failed_at = 0.0
