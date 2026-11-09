"""AA-1045 — the hourly usage rollup ``shared.ai_usage_hourly`` (CIS 372): build it from the ledgers and prove it equals
them. $0: database reads and writes only.

``roll_hours(pool, since, until)``: for whole UTC hours [since, until), in chunks of ``CHUNK`` (one transaction each),
DELETE the rollup rows of the chunk's hours and INSERT ... SELECT them again from the four ledgers (api/services/
usage_dims.py: one dimension and metric definition per ledger). Running it twice gives the same rows (idempotent per
hour); running it again after a late row lands counts that row. Hours older than ``RAW_WINDOW`` are never re-rolled:
once the retention job has deleted a ledger's raw rows, a re-roll would replace the hour's history by nothing.

``check_totals(pool, since, until)``: per source, the ledger's own totals (rows, requests, tokens, cost) of [since,
until) next to the rollup's; ``ok`` when every figure is equal. Cost is compared as numeric, exactly.

``last_run(pool)``: the newest succeeded ``ai_usage_rollup`` job (when, which hours), what the explorer shows as
"rolled up to".
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional

import structlog

from api.services import usage_dims as ud

logger = structlog.get_logger()

KIND = "ai_usage_rollup"
CHUNK = timedelta(hours=24)
# shared.llm_call_log keeps 180 days of raw rows (DM-7.1); stay clear of the retention edge.
RAW_WINDOW = timedelta(days=175)
MAX_RANGE = RAW_WINDOW

_KEY = ("hour", "source") + tuple(ud.ROLLUP_COLUMN[d] for d in ud.STORED_DIMS)
_METRICS = ("requests", "ledger_rows", "fallback_requests", "tokens_in", "tokens_out", "cache_read_tokens",
            "cache_write_tokens", "reasoning_tokens", "cost_usd", "latency_ms_sum", "latency_ms_count", "latency_hist")


class RollupUnavailable(RuntimeError):
    """shared.ai_usage_hourly is missing (CIS 372 not applied)."""


def floor_hour(t: datetime) -> datetime:
    t = t.astimezone(timezone.utc)
    return t.replace(minute=0, second=0, microsecond=0)


def ceil_hour(t: datetime) -> datetime:
    f = floor_hour(t)
    return f if f == t.astimezone(timezone.utc) else f + timedelta(hours=1)


def _source_select(source: str) -> str:
    """One ledger's rows of [$1, $2) as (at, source, stored dims..., per-row metrics)."""
    t = ud.TIME[source]
    dims = []
    for d in ud.STORED_DIMS:
        expr = ud.dim_sql(source, d)
        cast = "::uuid" if d == "tenant" else "::text"
        dims.append(f"({expr}){cast} AS {ud.ROLLUP_COLUMN[d]}" if expr is not None
                    else f"NULL{cast} AS {ud.ROLLUP_COLUMN[d]}")
    m = ud.metric_sql(source)
    return (f"SELECT {t} AS at, '{source}'::text AS source, " + ", ".join(dims) + ",\n"
            f"       ({m['requests']})::bigint AS requests, ({m['fallback']})::bigint AS fallback,\n"
            f"       ({m['tokens_in']})::bigint AS tokens_in, ({m['tokens_out']})::bigint AS tokens_out,\n"
            f"       ({m['cache_read_tokens']})::bigint AS cache_read_tokens,\n"
            f"       ({m['cache_write_tokens']})::bigint AS cache_write_tokens,\n"
            f"       ({m['reasoning_tokens']})::bigint AS reasoning_tokens,\n"
            f"       ({m['cost_usd']})::numeric AS cost_usd, ({m['latency_ms']})::int AS latency_ms\n"
            f"  FROM {ud.TABLE[source]}\n"
            f" WHERE {t} >= $1 AND {t} < $2 AND {ud.SOURCE_WHERE[source]}")


def insert_sql() -> str:
    """INSERT ... SELECT of the rollup rows of [$1, $2) from every ledger. Pure (unit-tested; runs in integration)."""
    union = "\n UNION ALL\n".join(_source_select(s) for s in ud.SOURCES)
    key = ", ".join(_KEY)
    group = ", ".join(str(i) for i in range(1, len(_KEY) + 1))
    return (f"INSERT INTO shared.ai_usage_hourly ({key}, {', '.join(_METRICS)}, refreshed_at)\n"
            f"SELECT date_trunc('hour', u.at, 'UTC') AS hour, "
            + ", ".join(f"u.{c}" for c in _KEY[1:]) + ",\n"
            "       sum(u.requests), count(*), sum(u.fallback), sum(u.tokens_in), sum(u.tokens_out),\n"
            "       sum(u.cache_read_tokens), sum(u.cache_write_tokens), sum(u.reasoning_tokens), sum(u.cost_usd),\n"
            "       coalesce(sum(u.latency_ms), 0), count(u.latency_ms), "
            + ud.hist_sql("u.latency_ms") + ", now()\n"
            f"  FROM (\n{union}\n) u\n"
            f" GROUP BY {group}")


DELETE_SQL = "DELETE FROM shared.ai_usage_hourly WHERE hour >= $1 AND hour < $2"


def chunks(since: datetime, until: datetime, step: timedelta = CHUNK) -> list[tuple[datetime, datetime]]:
    out = []
    a = since
    while a < until:
        b = min(a + step, until)
        out.append((a, b))
        a = b
    return out


def clamp_range(since: datetime, until: datetime, *, now: Optional[datetime] = None) -> tuple[datetime, datetime]:
    """Whole hours, not before the raw window, not after the current hour's end. ValueError for an empty range."""
    now = now or datetime.now(timezone.utc)
    lo = floor_hour(now - RAW_WINDOW) + timedelta(hours=1)
    hi = floor_hour(now) + timedelta(hours=1)
    a = max(floor_hour(since), lo)
    b = min(ceil_hour(until), hi)
    if a >= b:
        raise ValueError("the range has no whole hour inside the ledgers' raw window")
    return a, b


async def roll_hours(pool, since: datetime, until: datetime, *, progress=None) -> dict:
    """Re-roll [since, until) (whole UTC hours, already clamped) chunk by chunk. Returns totals of what was written."""
    import asyncpg

    sql = insert_sql()
    rows = hours = 0
    for a, b in chunks(since, until):
        try:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute(DELETE_SQL, a, b)
                    status = await conn.execute(sql, a, b)
        except asyncpg.UndefinedTableError as e:
            raise RollupUnavailable("shared.ai_usage_hourly is missing (apply CIS 372)") from e
        rows += int(str(status).rsplit(" ", 1)[-1] or 0)
        hours += int((b - a).total_seconds() // 3600)
        if progress is not None:
            await progress({"rolled_to": b.isoformat(), "hours": hours, "rows": rows})
    return {"from": since.isoformat(), "to": until.isoformat(), "hours": hours, "rows_written": rows}


# ── check ──────────────────────────────────────────────────────────────────────────────────────────────────────────

def raw_totals_sql(source: str) -> str:
    t = ud.TIME[source]
    m = ud.metric_sql(source)
    return (f"SELECT count(*)::bigint AS ledger_rows, coalesce(sum({m['requests']}), 0)::bigint AS requests,\n"
            f"       coalesce(sum({m['tokens_in']}), 0)::bigint AS tokens_in,\n"
            f"       coalesce(sum({m['tokens_out']}), 0)::bigint AS tokens_out,\n"
            f"       coalesce(sum({m['cost_usd']}), 0)::numeric AS cost_usd\n"
            f"  FROM {ud.TABLE[source]}\n"
            f" WHERE {t} >= $1 AND {t} < $2 AND {ud.SOURCE_WHERE[source]}")


ROLLUP_TOTALS_SQL = """
    SELECT source, sum(ledger_rows)::bigint AS ledger_rows, sum(requests)::bigint AS requests,
           sum(tokens_in)::bigint AS tokens_in, sum(tokens_out)::bigint AS tokens_out, sum(cost_usd) AS cost_usd
      FROM shared.ai_usage_hourly
     WHERE hour >= $1 AND hour < $2
     GROUP BY source
"""
_CHECKED = ("ledger_rows", "requests", "tokens_in", "tokens_out", "cost_usd")


def compare(raw: dict[str, dict], rolled: dict[str, dict]) -> dict:
    """Per source: raw vs rollup figures and their differences; ``ok`` when all are equal. Pure."""
    out = []
    ok = True
    for s in ud.SOURCES:
        r = raw.get(s) or {}
        u = rolled.get(s) or {}
        diff = {}
        for k in _CHECKED:
            a = Decimal(str(r.get(k) or 0))
            b = Decimal(str(u.get(k) or 0))
            if a != b:
                diff[k] = str(b - a)
        ok = ok and not diff
        out.append({"source": s, "ledger": {k: _num(r.get(k)) for k in _CHECKED},
                    "rollup": {k: _num(u.get(k)) for k in _CHECKED}, "diff": diff, "equal": not diff})
    return {"ok": ok, "sources": out}


def _num(v: Any) -> Any:
    if v is None:
        return 0
    if isinstance(v, Decimal):
        return str(v)
    return v


async def check_totals(pool, since: datetime, until: datetime) -> dict:
    import asyncpg

    raw: dict[str, dict] = {}
    async with pool.acquire() as conn:
        for s in ud.SOURCES:
            row = await conn.fetchrow(raw_totals_sql(s), since, until)
            raw[s] = dict(row) if row else {}
        try:
            rolled = {r["source"]: dict(r) for r in await conn.fetch(ROLLUP_TOTALS_SQL, since, until)}
        except asyncpg.UndefinedTableError as e:
            raise RollupUnavailable("shared.ai_usage_hourly is missing (apply CIS 372)") from e
    return {"from": since.isoformat(), "to": until.isoformat(), **compare(raw, rolled)}


LAST_RUN_SQL = """
    SELECT id::text AS job_id, started_at, finished_at, result
      FROM shared.job
     WHERE kind = $1 AND status = 'succeeded'
     ORDER BY finished_at DESC NULLS LAST
     LIMIT 1
"""


async def last_run(pool) -> Optional[dict]:
    row = await pool.fetchrow(LAST_RUN_SQL, KIND)
    if row is None:
        return None
    result = row["result"]
    if isinstance(result, str):
        import json
        try:
            result = json.loads(result)
        except ValueError:
            result = None
    result = result if isinstance(result, dict) else {}
    return {"job_id": row["job_id"], "started_at": _iso(row["started_at"]), "finished_at": _iso(row["finished_at"]),
            "from": result.get("from"), "to": result.get("to"), "mode": result.get("mode")}


def _iso(v: Any) -> Any:
    return v.isoformat() if hasattr(v, "isoformat") else v
