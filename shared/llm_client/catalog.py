"""AA-658 / ADR 0005 — read side of shared.llm_model_catalog.

The whole table is small, so it is loaded in one query and cached in-process. Never raises: on a
DB failure every lookup returns None and callers fall back (pricing.py, the legacy LLMClient
chain). A failed load is remembered for a short while so a DB outage — or a unit-test run with
no database — costs one connection attempt, not one per LLM call.

AA-1017 (registry v2, migration 320; root ADR 0009 §3): each row also carries its ``family`` (the maker, a row of
shared.llm_model_family; NULL = its vendor) and its ``capabilities`` (jsonb, read as data). The gateway reads a
capability first and falls back to today's code rules when the row does not say (``capability``):

- ``forced_tool_choice`` (bool): False = the model answers a forced tool choice with a 400 (client.py asks with
  ``auto`` + an instruction). Unset = the profile-id markers (``client._NO_FORCED_TOOL_CHOICE_MARKERS``).
- ``prompt_cache`` (bool): True / False = a Converse cachePoint may / may not be sent. Unset = only to an
  ``anthropic.`` inference profile.
- ``cache_min_tokens`` (int): the minimum cacheable prompt prefix. Unset = the per-family table in client.py.
- ``effort_default`` (string or null): the Anthropic API effort sent when the deployment sets none; null = the model
  takes no effort parameter. Unset = ``adapters/anthropic_messages._EFFORT_DEFAULTS``.

Every row has ``{}`` at migration 320, so behaviour is today's. Prices change by migration only: the admin PATCH may
switch a row on or off but ``set_catalog_price`` refuses a price edit (AA-1017, D9).
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import asyncpg
import structlog

from shared.secrets import get_database_url

from .models import ANTHROPIC_API_LABEL_PREFIX

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


# S235 (Nghiệp, 14/10/2026): retired Model Keys — disabled by migration 224 and never re-enabled from
# the admin UI (bringing one back is a migration + decision, not a toggle).
RETIRED_KEYS = frozenset({"gpt-4.1"})


def serving_accounts() -> tuple[str, ...]:
    """AA-1002 (S3) — the accounts a catalog profile may run on, in preference order: the Bedrock connections of the
    registry and its built-in mirror, assume-role (satellite) ones first (connections.bedrock_connection_keys;
    ("acc3", "acc2") for the 304 seed and when the registry cannot be read). Replaces the hard-coded
    SERVING_ACCOUNTS of AA-998: an account no connection names (a retired account) is never chosen."""
    from . import connections
    return connections.bedrock_connection_keys()


class CatalogPolicyBlocked(Exception):
    """S235 — an admin tried to enable a retired catalog row (RETIRED_KEYS). Router -> 422."""


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
    # AA-1017 (migration 320): the family key (None = before 320 or not set: read vendor) and the capabilities row.
    family: Optional[str] = None
    capabilities: dict = field(default_factory=dict)

    @property
    def family_key(self) -> str:
        """The model's family: ``family``, else its vendor (every row before migration 320)."""
        return self.family or self.vendor

    def account_for(self, preferred: Optional[str]) -> Optional[str]:
        """The preferred account if this model is served there, else the first account that serves
        it (acc3 before acc2). AA-998 / AA-1002 (S3): only accounts a Bedrock connection names count
        (serving_accounts) — a profile under any other key (a stale row from before migration
        295) is never chosen. The deployment route (vendor_route.deployment_plan) replaces this on the
        call path whenever the model has deployment rows; it remains the no-registry fallback."""
        serving = serving_accounts()
        if preferred in serving and preferred in self.bedrock_profile_ids:
            return preferred
        for acct in serving:
            if acct in self.bedrock_profile_ids:
                return acct
        return None


_models: dict[str, CatalogModel] = {}
_loaded_at: float = 0.0
_failed_at: float = 0.0


def _num(v) -> Optional[float]:
    return float(v) if v is not None else None


_MISSING = object()


def capability(model, name: str, default=None):
    """AA-1017: the value of capability ``name`` in ``model.capabilities`` (the catalog row), else ``default`` (the
    caller then applies today's code rule). Works on any object with a ``capabilities`` dict (test doubles without one
    answer ``default``). Never raises."""
    caps = getattr(model, "capabilities", None)
    if not isinstance(caps, dict):
        return default
    value = caps.get(name, _MISSING)
    return default if value is _MISSING else value


def has_capability(model, name: str) -> bool:
    """True when the catalog row states capability ``name`` (null included: "takes no effort" is a statement)."""
    caps = getattr(model, "capabilities", None)
    return isinstance(caps, dict) and name in caps


def _field(row, key: str):
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


def _capabilities(raw) -> dict:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return {}
    return dict(raw) if isinstance(raw, dict) else {}


def _row_to_model(row) -> CatalogModel:
    profiles = row["bedrock_profile_ids"]
    if isinstance(profiles, str):
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
        family=_field(row, "family"), capabilities=_capabilities(_field(row, "capabilities")),
    )


_SELECT_SQL = (
    "SELECT model_key, label, vendor, provider, api_style, bedrock_profile_ids, wire_model, "
    "callable_via, supports_temperature, max_output_tokens, price_in_per_mtok, price_out_per_mtok, "
    "price_cache_read_per_mtok, price_cache_write_per_mtok, enabled, blocked_reason "
    "FROM shared.llm_model_catalog ORDER BY model_key"
)
# AA-1017: the same read with the row's capabilities (migration 304) and family (migration 320).
_SELECT_V2_SQL = _SELECT_SQL.replace(", blocked_reason ", ", blocked_reason, capabilities, family ")


async def _fetch_rows(conn, v2_sql: str, v1_sql: str):
    """The v2 read, or the pre-320 one when the table has no family column yet."""
    try:
        return await conn.fetch(v2_sql)
    except asyncpg.exceptions.UndefinedColumnError:
        return await conn.fetch(v1_sql)


async def _fetch_all() -> dict[str, CatalogModel]:
    conn = await asyncpg.connect(get_database_url(), ssl="require", timeout=_CONNECT_TIMEOUT_SECONDS)
    try:
        rows = await _fetch_rows(conn, _SELECT_V2_SQL, _SELECT_SQL)
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
    """Accepts a Model Key, optionally with the "satellite-" / "anthropic-api-" label prefix LLMResponse uses."""
    if not model_key:
        return None
    if model_key.startswith("satellite-"):
        model_key = model_key[len("satellite-"):]
    elif model_key.startswith(ANTHROPIC_API_LABEL_PREFIX):        # AA-1002 S2 label of the Anthropic API path
        model_key = model_key[len(ANTHROPIC_API_LABEL_PREFIX):]
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
# AA-1017: the admin read with the family (migration 320) and the capabilities (migration 304).
_FULL_SELECT_V2_SQL = _FULL_SELECT_SQL.replace(", updated_by FROM", ", updated_by, family, capabilities FROM")

# Columns an admin may edit via PATCH /admin/llm-catalog/{model_key} (ADR 0005: the dropdown
# options come from here; vendor/provider/api_style/profile ids are a code+migration change, not a
# config change). AA-1017 (design D9): prices too — a price record changes by migration only
# (ADR 0009 §3), so the admin keeps the on/off switch and a price edit is refused (CatalogPriceReadOnly).
_CATALOG_EDITABLE_FIELDS = ("enabled",)
CATALOG_PRICE_FIELDS = ("price_in_per_mtok", "price_out_per_mtok", "price_source")


class CatalogPriceReadOnly(ValueError):
    """AA-1017 (D9): a catalog price edit through the admin. Prices change by migration only. Router -> 422."""


def _row_to_dict(row) -> dict:
    d = dict(row)
    profiles = d.get("bedrock_profile_ids")
    if isinstance(profiles, str):
        profiles = json.loads(profiles)
    d["bedrock_profile_ids"] = dict(profiles or {})
    d["callable_via"] = list(d.get("callable_via") or [])
    if "capabilities" in d:
        d["capabilities"] = _capabilities(d.get("capabilities"))
    if "family" in d or "vendor" in d:
        d["family"] = d.get("family") or d.get("vendor")
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
        rows = await _fetch_rows(conn, _FULL_SELECT_V2_SQL, _FULL_SELECT_SQL)
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

    AA-1017 (D9): a price field (CATALOG_PRICE_FIELDS) raises CatalogPriceReadOnly before anything
    is read or written (router -> 422): prices change by migration only. ``enabled`` stays editable.
    """
    prices = sorted(k for k in fields if k in CATALOG_PRICE_FIELDS)
    if prices:
        raise CatalogPriceReadOnly(
            f"{', '.join(prices)}: catalog prices change by migration only (a reviewed price record); "
            "only the on/off switch is editable here")
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
            if edits.get("enabled") is True and model_key in RETIRED_KEYS:
                raise CatalogPolicyBlocked(f"{model_key} is retired (S235) and cannot be re-enabled")
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
