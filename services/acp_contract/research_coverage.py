"""services/acp_contract/research_coverage.py — AA-1055 (stale guard): how much of the research scope is stale, per
target market, and how many places wait for research after an atomize. Read only, $0 (no LLM, Jev or DataForSEO call).

Lesson S273: 119 of the audit tenant's 160 places had never been researched and 91 % of its ranking rows had no demand,
and nothing said so. This module measures it with the research run's own definitions (segment_research), so the guard,
the data coverage page and a preview never disagree on what "stale" means:

- **in scope**: the places of the Segments research covers (`segment_research.scoped_rows`: an atom of an active
  platform Master, minus transit / unnamed places by the recompute's rule and the stored Jev picks, never a new one);
- **stale for market M**: `segment_research.stale_tiers` for that market alone: its volume tier has no log row within
  FRESH_FOR (365 days), or, for the SERP market (US) only, its one SERP has none within SERP_FRESH_FOR. The SERP is
  bought once per place in the US, so it is counted against the US share only. A place the keyword rules would rebuild
  (`needs_requalify`) is not stale by this definition (it was researched): the research preview still selects it;
- **markets**: every market of `seed_builder.DFS_LOCATION_MAP` that at least one active tenant targets, resolved the
  way the Slate resolves a tenant's markets (`resolve_buyer_markets`: no usable country = US). An unmapped country is
  NOT recorded here (the Slate read does that; a guard must not write);
- **waiting after an atomize**: places atomized since their newest research row (or never researched) and stale in at
  least one targeted market (`segment_research.reatomized_places`). There is no queue table: a research run with scope
  `reatomized` (POST /admin/segment-research/preview, then /run) is that batch, priced before an admin approves it.

Callers: the api ops sampler once a day (ops check `research_stale_share`, shared/ops_checks.py) and the data coverage
page (`research` source, slow tier). Both bound every statement (`ReadOnlyPool`: READ ONLY transaction, statement and
lock timeouts) and the whole read (asyncio.wait_for at the caller).
"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any, Iterable, Optional

STALE_SHARE_LIMIT = 0.20      # above this share of the scope stale in a market, the guard alerts and the row is a gap
CHECK_EVERY_S = 86400.0       # the api ops sampler measures once a day (its own monotonic schedule)
CHECK_TIMEOUT_S = 30.0        # the sampler part's budget for the whole read
CHECK_STATEMENT_TIMEOUT = "10s"

# Active tenants (the data coverage `ten` rule: is_active, not cancelled, not the AA master) and their SEO markets.
# LEFT JOIN: a tenant with no SEO config row targets the default market, like the Slate (TenantConfigService).
TARGET_MARKETS_SQL = """
    SELECT c.target_market
      FROM shared.tenants t
      LEFT JOIN shared.tenant_seo_config c ON c.tenant_id = t.tenant_id
     WHERE t.is_active AND t.cancelled_at IS NULL AND t.tenant_id <> $1::uuid
"""
MASTER_TENANT = "00000000-0000-0000-0000-000000000001"


class ReadOnlyPool:
    """A pool stand-in for the research reads: every `acquire()` is its own connection in a READ ONLY transaction with
    a statement and a lock timeout, so a guard read can never write nor hold a lock or a connection for long. `gate`
    (optional asyncio.Semaphore) bounds how many such connections are open at once (the data coverage page's)."""

    def __init__(self, pool: Any, statement_timeout: str = "7s", lock_timeout: str = "1s", gate: Any = None) -> None:
        self._pool = pool
        self._statement_timeout = statement_timeout
        self._lock_timeout = lock_timeout
        self._gate = gate

    @asynccontextmanager
    async def _open(self):
        async with self._pool.acquire() as conn:
            async with conn.transaction(readonly=True):
                await conn.execute(f"SET LOCAL statement_timeout = '{self._statement_timeout}'")
                await conn.execute(f"SET LOCAL lock_timeout = '{self._lock_timeout}'")
                yield conn

    @asynccontextmanager
    async def acquire(self):
        if self._gate is None:
            async with self._open() as conn:
                yield conn
        else:
            async with self._gate:
                async with self._open() as conn:
                    yield conn

    async def fetch(self, sql: str, *args: Any) -> list:
        async with self.acquire() as conn:
            return list(await conn.fetch(sql, *args))


def _parse(target_market: Any) -> dict:
    if isinstance(target_market, str):
        try:
            target_market = json.loads(target_market)
        except ValueError:
            return {}
    return target_market if isinstance(target_market, dict) else {}


def targeted_markets(target_markets: Iterable[Any]) -> list[str]:
    """The DFS markets at least one tenant targets, in MARKET_RANK order, from their `target_market` values (dict,
    JSON text or None). Each resolves like the Slate (`resolve_buyer_markets`: unknown or empty = US). Pure."""
    from services.seo_intelligence.seed_builder import LOCATION_CODE_TO_MARKET, MARKET_RANK, resolve_buyer_markets

    codes: set[str] = set()
    for tm in target_markets:
        codes.update(LOCATION_CODE_TO_MARKET[loc] for loc, _n, _l in resolve_buyer_markets(_parse(tm)))
    return sorted(codes, key=lambda c: (MARKET_RANK.get(c, 99), c))


def market_tiers(market: str) -> tuple[str, ...]:
    """The tiers counted against one market: volume everywhere, plus the one SERP in the SERP market."""
    from services.acp_contract.segment_research_batch import SERP_MARKET

    return ("volume", "serp") if market == SERP_MARKET else ("volume",)


def is_stale(got: set[tuple[str, str]], market: str) -> bool:
    """A place (fresh log rows `got`) is stale for `market` (segment_research.stale_tiers, that market alone)."""
    from services.acp_contract.segment_research import stale_tiers

    markets, serp = stale_tiers(got, [market], market_tiers(market))
    return bool(markets or serp)


def summarize(places: Iterable[str], fresh: dict[str, set[tuple[str, str]]], markets: list[str],
              reatomized: Optional[set[str]] = None, limit: float = STALE_SHARE_LIMIT) -> dict:
    """The stale share per market and the places waiting after an atomize. Pure.

    `places`: the places in scope; `fresh`: place -> fresh (market, tier) rows (segment_research._fresh_tiers);
    `reatomized`: the places atomized since their last research (segment_research.reatomized_places)."""
    names = sorted(set(places))
    n = len(names)
    by_market: dict[str, dict] = {}
    stale_any: set[str] = set()
    for m in markets:
        stale = [p for p in names if is_stale(fresh.get(p, set()), m)]
        stale_any.update(stale)
        share = (len(stale) / n) if n else 0.0
        by_market[m] = {"places_in_scope": n, "places_stale": len(stale), "stale_share": round(share, 4),
                        "above_limit": share > limit}
    waiting = sorted(stale_any & (reatomized or set()))
    return {"markets": list(markets), "places_in_scope": n, "limit": limit, "by_market": by_market,
            "waiting_after_reatomize": len(waiting)}


async def target_markets(pool: Any) -> list[str]:
    rows = await pool.fetch(TARGET_MARKETS_SQL, MASTER_TENANT)
    return targeted_markets(r["target_market"] for r in rows)


async def measure(pool: Any) -> dict:
    """Read the scope, the research log and the tenants' markets (5 bounded reads, stored Jev picks only) and
    summarize. `pool` is a ReadOnlyPool (or anything with acquire() / fetch()). Raises on a database error; the
    caller bounds the whole call."""
    from services.acp_contract import segment_research as sr
    from services.acp_contract.segment_research_batch import SERP_FRESH_FOR

    markets = await target_markets(pool)
    rows = await sr.scoped_rows(pool, cached_only=True)
    names = sorted({r["canonical_place"] for r in rows})
    fresh = await sr._fresh_tiers(pool, names) if markets else {}
    newest = await sr.newest_research(pool, names) if markets else {}
    out = summarize(names, fresh, markets, sr.reatomized_places(rows, newest))
    out["fresh_for_days"] = sr.FRESH_FOR.days
    out["serp_fresh_for_days"] = SERP_FRESH_FOR.days
    return out


def coverage_values(summary: dict) -> dict:
    """The flat numbers of the data coverage `research` source: `in_scope`, `allowed` (the most stale places a market
    may have before its row is a gap: floor(limit x scope), the same test as the ops check's share > limit),
    `waiting_after_reatomize`, and per market of DFS_LOCATION_MAP `targeted_<MK>` (1 / 0) and, when targeted,
    `stale_<MK>`. Pure."""
    from services.seo_intelligence.seed_builder import DFS_LOCATION_MAP

    n = int(summary.get("places_in_scope") or 0)
    limit = float(summary.get("limit", STALE_SHARE_LIMIT))
    out: dict[str, Any] = {"in_scope": n, "allowed": int(limit * n + 1e-9),
                           "waiting_after_reatomize": int(summary.get("waiting_after_reatomize") or 0)}
    by_market = summary.get("by_market") or {}
    for m in DFS_LOCATION_MAP:
        row = by_market.get(m)
        out[f"targeted_{m}"] = 1 if row is not None else 0
        if row is not None:
            out[f"stale_{m}"] = int(row["places_stale"])
    return out
