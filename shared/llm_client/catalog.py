"""AA-658 / ADR 0005 — read side of shared.llm_model_catalog.

The whole table is small, so it is loaded in one query and cached in-process. Never raises: on a
DB failure every lookup returns None and callers fall back (pricing.py, the legacy LLMClient
chain). A failed load is remembered for a short while so a DB outage — or a unit-test run with
no database — costs one connection attempt, not one per LLM call.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import asyncpg
import structlog

from shared.secrets import get_database_url

logger = structlog.get_logger()

_CACHE_TTL_SECONDS = 300.0
_FAILURE_TTL_SECONDS = 60.0
_CONNECT_TIMEOUT_SECONDS = 3.0

LEGACY_KEYS = frozenset({"haiku", "sonnet", "gpt-4.1"})


class CatalogPriceRequired(Exception):
    """AA-686 — a model cannot be enabled without an in/out price. Raised by set_catalog_price so
    the router can map it to 422 instead of leaking the DB CHECK violation as a 500. The DB
    CHECK (llm_model_catalog_enabled_needs_price, migration 169) is the real enforcement; this is
    the readable, mapped-to-422 form of the same rule."""


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


# ── AA-686 — admin catalog read/write (full rows incl. prices + provenance) ────────────────────

_FULL_SELECT_SQL = (
    "SELECT model_key, label, vendor, provider, api_style, bedrock_profile_ids, wire_model, "
    "callable_via, supports_temperature, max_output_tokens, price_in_per_mtok, price_out_per_mtok, "
    "price_cache_read_per_mtok, price_cache_write_per_mtok, price_source, enabled, blocked_reason, "
    "notes, updated_at, updated_by FROM shared.llm_model_catalog ORDER BY model_key"
)

# Columns an admin may edit via PATCH /admin/llm-catalog/{model_key} (ADR 0005: the dropdown
# options and prices come from here; vendor/provider/api_style/profile ids are a code+migration
# change, not a config change).
_CATALOG_EDITABLE_FIELDS = ("price_in_per_mtok", "price_out_per_mtok", "price_source", "enabled")


def _row_to_dict(row) -> dict:
    d = dict(row)
    profiles = d.get("bedrock_profile_ids")
    if isinstance(profiles, str):
        profiles = json.loads(profiles)
    d["bedrock_profile_ids"] = dict(profiles or {})
    d["callable_via"] = list(d.get("callable_via") or [])
    for k in ("price_in_per_mtok", "price_out_per_mtok", "price_cache_read_per_mtok",
              "price_cache_write_per_mtok"):
        d[k] = _num(d.get(k))
    d["updated_at"] = d["updated_at"].isoformat() if d.get("updated_at") else None
    return d


async def list_catalog_rows() -> list[dict]:
    """AA-686 admin read — every catalog row with full price + provenance columns, as plain dicts
    (JSON-ready). Fresh each call (admin surface), unlike the cached CatalogModel read path."""
    conn = await asyncpg.connect(get_database_url(), ssl="require", timeout=_CONNECT_TIMEOUT_SECONDS)
    try:
        rows = await conn.fetch(_FULL_SELECT_SQL)
        return [_row_to_dict(r) for r in rows]
    finally:
        await conn.close()


async def set_catalog_price(
    model_key: str, *, fields: dict[str, Any], updated_by: str, audit_actor: str,
) -> dict:
    """AA-686 — update a catalog row's price/enabled fields and audit it (action
    `llm_catalog_changed`) in one transaction. Returns the updated row dict.

    Raises ValueError when `model_key` does not exist (router -> 404) and CatalogPriceRequired when
    the DB rejects enabling a model with no in/out price (router -> 422, never 500). The in-process
    cache is invalidated after a committed write so the next dropdown read is fresh.
    """
    edits = {k: v for k, v in fields.items() if k in _CATALOG_EDITABLE_FIELDS}
    if not edits:
        raise ValueError("no editable catalog fields supplied")
    set_frags = [f"{col} = ${i + 2}" for i, col in enumerate(edits)]
    values = list(edits.values())
    conn = await asyncpg.connect(get_database_url(), ssl="require", timeout=_CONNECT_TIMEOUT_SECONDS)
    try:
        async with conn.transaction():
            before = await conn.fetchrow(
                "SELECT price_in_per_mtok, price_out_per_mtok, price_source, enabled "
                "FROM shared.llm_model_catalog WHERE model_key = $1",
                model_key,
            )
            if before is None:
                raise ValueError(f"unknown model_key: {model_key!r}")
            try:
                row = await conn.fetchrow(
                    f"UPDATE shared.llm_model_catalog SET {', '.join(set_frags)}, "
                    f"updated_at = now(), updated_by = ${len(values) + 2} "
                    "WHERE model_key = $1 RETURNING "
                    "model_key, label, vendor, provider, api_style, bedrock_profile_ids, "
                    "wire_model, callable_via, supports_temperature, max_output_tokens, "
                    "price_in_per_mtok, price_out_per_mtok, price_cache_read_per_mtok, "
                    "price_cache_write_per_mtok, price_source, enabled, blocked_reason, notes, "
                    "updated_at, updated_by",
                    model_key, *values, updated_by,
                )
            except asyncpg.exceptions.CheckViolationError as e:
                if "enabled_needs_price" in str(e):
                    raise CatalogPriceRequired(
                        "cannot enable a model without both an input and an output price"
                    ) from e
                raise
            _price_cols = ("price_in_per_mtok", "price_out_per_mtok")
            before_d = {k: (_num(before[k]) if k in _price_cols else before[k])
                        for k in ("price_in_per_mtok", "price_out_per_mtok", "price_source",
                                  "enabled")}
            after_d = {k: (_num(v) if k in _price_cols else v) for k, v in edits.items()}
            await conn.execute(
                """
                INSERT INTO acp_shared.audit_log
                    (actor, action, resource_type, resource_id, details)
                VALUES ($1, $2, $3, $4, $5::jsonb)
                """,
                audit_actor, "llm_catalog_changed", "llm_model_catalog", model_key,
                json.dumps({"before": before_d, "after": after_d}, default=str),
            )
        return _row_to_dict(row)
    finally:
        await conn.close()
        invalidate()
