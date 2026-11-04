"""AA-1045 — the AI & data usage explorer (admin AI & spend › Usage explorer): any window, one or two dimensions, the
usage metrics, filters on every dimension, from the hourly rollup ``shared.ai_usage_hourly`` (CIS 372,
api/services/usage_rollup.py) plus the CRM's daily totals. $0: database reads and one crm.ops.v1 read.

Query (``parse_query``; every value is checked, the router answers 422 on the first bad one):
  range        today | 24h | 7d | 30d | this_month | last_month | custom (``from`` / ``to``: YYYY-MM-DD, inclusive days)
               — days and months are Asia/Ho_Chi_Minh (ICT, UTC+7, whole hours, so every bucket is whole UTC hours of
               the rollup); data stays UTC and every time in the answer is an ISO string with its offset.
  granularity  hour | day | week (ISO, Monday) | month — the chart's buckets, and the ``time`` dimension.
  group        one or two of ``DIMENSIONS`` + ``time``.
  f_<dim>      comma-separated values to keep (multi-select); ``(none)`` keeps rows without a value.
  sort, dir    any metric or grouped dimension; asc | desc.
  per_unit     1: also cost per tour and per content piece (from the raw LLM ledger, only inside its raw window: a
               distinct count of tours does not add up across hours, so the rollup cannot hold it).

Answer: ``rows`` (one per group, metrics below, at most ``MAX_GROUPS`` after sorting; ``truncated`` says so),
``series`` (per time bucket: requests, tokens, cost), ``totals``, ``labels`` (tenant names), ``crm`` (whether the CRM's
daily totals were merged, or why not), ``rollup`` (the last rollup run) and ``notes``. A time bucket (``rows[].bucket``,
``series[]``) carries ``start`` / ``end`` clipped to the range and ``partial`` (bucket_span): the drill-down to the
Calls list uses them, so the list holds the rows the cell counted.

Metrics per group: requests, ledger_rows, input / output / cache read / cache write / reasoning tokens, cost_usd,
avg_latency_ms, p95_latency_ms (approximate: the latency histogram, usage_dims.percentile_ms), error_rate (requests with
a status other than ok / requests), fallback_rate (fallback answers / answered LLM requests, usage_dims.FALLBACK_SQL),
and with ``per_unit`` cost_per_tour / cost_per_piece (LLM rows with a tour / piece, cost / distinct count).

The CRM (AA-Partner) keeps its own ledger, which CIS cannot read; its gateway's daily totals per UTC day, stage and
model (crm.ops.v1 GET /llm/usage, with the staff member's own token: no agency, no personal data) are merged as source
``crm`` when the granularity is a day or longer and no filter needs a value the CRM does not send (provider, account,
role, tenant, error class, traffic); grouped by one of those, the CRM's rows show no value there. A CRM UTC day is put
in the ICT day of the same date.
"""
from __future__ import annotations

import asyncio
import re
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Awaitable, Callable, Optional
from zoneinfo import ZoneInfo

import structlog

from api.services import usage_dims as ud
from api.services import usage_rollup as ur

logger = structlog.get_logger()

TZ_NAME = "Asia/Ho_Chi_Minh"
TZ = ZoneInfo(TZ_NAME)
RANGES = ("today", "24h", "7d", "30d", "this_month", "last_month", "custom")
GRANULARITIES = ("hour", "day", "week", "month")
DIMENSIONS = ud.DIMENSIONS
GROUPABLE = DIMENSIONS + ("time",)
METRICS = ("requests", "ledger_rows", "tokens_in", "tokens_out", "cache_read_tokens", "cache_write_tokens",
           "reasoning_tokens", "cost_usd", "avg_latency_ms", "p95_latency_ms", "error_rate", "fallback_rate",
           "cost_per_tour", "cost_per_piece")
NONE_VALUE = "(none)"
MAX_GROUP = 2
MAX_GROUPS = 2000
MAX_VALUES = 50
MAX_CUSTOM_DAYS = 400
MAX_HOUR_BUCKETS_DAYS = 31
CRM_MAX_DAYS = 92                 # crm.ops.v1 GET /llm/usage refuses a longer range
CRM_DIMS = frozenset({"source", "app", "product", "model", "family", "stage", "status", "time"})
_VALUE_RE = re.compile(r"^[\w .:/@()+-]{1,120}$")


@dataclass(frozen=True)
class ExplorerQuery:
    range: str = "7d"
    since_day: Optional[date] = None
    until_day: Optional[date] = None
    granularity: str = "day"
    group: tuple[str, ...] = ("provider",)
    filters: dict[str, tuple[str, ...]] = field(default_factory=dict)
    sort: str = "cost_usd"
    dir: str = "desc"
    per_unit: bool = False


def _blank(v: Optional[str]) -> Optional[str]:
    return None if v is None or str(v).strip() == "" else str(v).strip()


def parse_query(params: dict[str, Any]) -> ExplorerQuery:
    """The checked query from the request's parameters (``f_<dim>`` keys are filters). ValueError names the first bad
    value. Pure."""
    rng = _blank(params.get("range")) or "7d"
    if rng not in RANGES:
        raise ValueError(f"range must be one of {', '.join(RANGES)}")
    since_day = until_day = None
    if rng == "custom":
        try:
            since_day = date.fromisoformat(_blank(params.get("from")) or "")
            until_day = date.fromisoformat(_blank(params.get("to")) or "")
        except ValueError:
            raise ValueError("a custom range needs from and to as YYYY-MM-DD") from None
        if until_day < since_day:
            raise ValueError("to must not be before from")
        if (until_day - since_day).days + 1 > MAX_CUSTOM_DAYS:
            raise ValueError(f"a custom range is at most {MAX_CUSTOM_DAYS} days")
    gran = _blank(params.get("granularity")) or "day"
    if gran not in GRANULARITIES:
        raise ValueError(f"granularity must be one of {', '.join(GRANULARITIES)}")
    group_raw = _blank(params.get("group")) or "provider"
    group = tuple(dict.fromkeys(g.strip() for g in group_raw.split(",") if g.strip()))
    if not 1 <= len(group) <= MAX_GROUP:
        raise ValueError(f"group by one or {MAX_GROUP} dimensions")
    for g in group:
        if g not in GROUPABLE:
            raise ValueError(f"group must name dimensions of {', '.join(GROUPABLE)}")
    filters: dict[str, tuple[str, ...]] = {}
    for d in DIMENSIONS:
        raw = _blank(params.get(f"f_{d}"))
        if raw is None:
            continue
        values = tuple(dict.fromkeys(v.strip() for v in raw.split(",") if v.strip()))
        if not values:
            continue
        if len(values) > MAX_VALUES:
            raise ValueError(f"f_{d} takes at most {MAX_VALUES} values")
        for v in values:
            if not _VALUE_RE.match(v):
                raise ValueError(f"f_{d} has a value that is not a dimension value")
            if d == "tenant" and v != NONE_VALUE:
                try:
                    uuid.UUID(v)
                except ValueError:
                    raise ValueError("f_tenant takes tenant UUIDs") from None
        filters[d] = tuple(v.lower() if d == "tenant" else v for v in values)
    sort = _blank(params.get("sort")) or "cost_usd"
    if sort not in METRICS and sort not in group:
        raise ValueError("sort must be a metric or a grouped dimension")
    direction = (_blank(params.get("dir")) or "desc").lower()
    if direction not in ("asc", "desc"):
        raise ValueError("dir must be asc or desc")
    per_unit = str(params.get("per_unit") or "").strip().lower() in ("1", "true", "yes")
    return ExplorerQuery(range=rng, since_day=since_day, until_day=until_day, granularity=gran, group=group,
                         filters=filters, sort=sort, dir=direction, per_unit=per_unit)


def _local_midnight(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=TZ)


def resolve_range(q: ExplorerQuery, now: datetime) -> tuple[datetime, datetime]:
    """[since, until) in UTC, whole hours (ICT days are whole UTC hours). Pure."""
    local = now.astimezone(TZ)
    end_now = ur.floor_hour(now) + timedelta(hours=1)
    if q.range == "today":
        since, until = _local_midnight(local.date()), end_now
    elif q.range == "24h":
        since, until = end_now - timedelta(hours=24), end_now
    elif q.range == "7d":
        since, until = end_now - timedelta(days=7), end_now
    elif q.range == "30d":
        since, until = end_now - timedelta(days=30), end_now
    elif q.range == "this_month":
        since, until = _local_midnight(local.date().replace(day=1)), end_now
    elif q.range == "last_month":
        first = local.date().replace(day=1)
        prev = (first - timedelta(days=1)).replace(day=1)
        since, until = _local_midnight(prev), _local_midnight(first)
    else:
        since = _local_midnight(q.since_day)  # type: ignore[arg-type]
        until = _local_midnight(q.until_day + timedelta(days=1))  # type: ignore[operator]
    return since.astimezone(timezone.utc), until.astimezone(timezone.utc)


def check_shape(q: ExplorerQuery, since: datetime, until: datetime) -> None:
    if q.granularity == "hour" and (until - since) > timedelta(days=MAX_HOUR_BUCKETS_DAYS):
        raise ValueError(f"hour granularity covers at most {MAX_HOUR_BUCKETS_DAYS} days")


# ── SQL ─────────────────────────────────────────────────────────────────────────────────────────────────────────────

FAMILY_CTE = """fam AS (
    SELECT DISTINCT ON (fam_key) fam_key, fam_family FROM (
        SELECT model_key AS fam_key, family AS fam_family, 0 AS fam_pri FROM shared.llm_model_catalog
        UNION ALL
        SELECT wire_model, family, 1 FROM shared.llm_model_catalog WHERE wire_model IS NOT NULL
    ) x ORDER BY fam_key, fam_pri)"""


def time_sql(time_col: str, granularity: str) -> str:
    """The bucket start (timestamptz) of a UTC time column: the UTC hour, or the ICT day / ISO week / month."""
    if granularity == "hour":
        return f"date_trunc('hour', {time_col}, 'UTC')"
    return f"date_trunc('{granularity}', {time_col}, '{TZ_NAME}')"


def _dim_rollup(d: str, granularity: str) -> str:
    if d == "time":
        return time_sql("hour", granularity)
    if d == "family":
        return "fam_family"
    if d == "product":
        return ud.product_sql("stage")
    col = ud.ROLLUP_COLUMN[d]
    return f"{col}::text" if d == "tenant" else col


def _dim_raw_llm(d: str, granularity: str) -> str:
    if d == "time":
        return time_sql("created_at", granularity)
    if d == "family":
        return "fam_family"
    if d == "product":
        return ud.product_sql("stage")
    return ud.dim_sql("llm", d)  # type: ignore[return-value]


def _filter_clauses(q: ExplorerQuery, expr: Callable[[str], Optional[str]], arg: Callable[[Any], str]) -> list[str]:
    out = []
    for d, values in sorted(q.filters.items()):
        e = expr(d)
        if e is None:
            continue
        vals = ["" if v == NONE_VALUE else v for v in values]
        out.append(f"coalesce(({e})::text, '') = ANY({arg(vals)}::text[])")
    return out


def _needs_family(q: ExplorerQuery) -> bool:
    return "family" in q.group or "family" in q.filters


def aggregate_sql(q: ExplorerQuery, since: datetime, until: datetime, *, by: tuple[str, ...],
                  hist: bool = False) -> tuple[str, list[Any]]:
    """(SQL, args) of the rollup grouped by ``by`` (dimension keys incl. ``time``): the summed metrics, or with
    ``hist`` the summed latency histogram cells (one row per group and bucket). Pure."""
    args: list[Any] = []

    def arg(v: Any) -> str:
        args.append(v)
        return f"${len(args)}"

    where = [f"hour >= {arg(since)}", f"hour < {arg(until)}"]
    where += _filter_clauses(q, lambda d: _dim_rollup(d, q.granularity), arg)
    dims = [f"{_dim_rollup(d, q.granularity)} AS d_{d}" for d in by]
    cte = f"WITH {FAMILY_CTE}\n" if (_needs_family(q) or "family" in by) else ""
    src = "shared.ai_usage_hourly"
    if cte:
        src += " LEFT JOIN fam ON fam_key = model"
    group = ", ".join(str(i) for i in range(1, len(by) + 1))
    if hist:
        sel = dims + ["h.i::int AS bucket", "sum(h.c)::bigint AS n"]
        src += " CROSS JOIN LATERAL unnest(latency_hist) WITH ORDINALITY AS h(c, i)"
        group = ", ".join(str(i) for i in range(1, len(by) + 2))
    else:
        sel = dims + [
            "sum(requests)::bigint AS requests", "sum(ledger_rows)::bigint AS ledger_rows",
            "coalesce(sum(requests) FILTER (WHERE status <> 'ok'), 0)::bigint AS failed_requests",
            "coalesce(sum(requests) FILTER (WHERE status = 'ok' AND source = 'llm'), 0)::bigint AS answered_llm",
            "sum(fallback_requests)::bigint AS fallback_requests",
            "sum(tokens_in)::bigint AS tokens_in", "sum(tokens_out)::bigint AS tokens_out",
            "sum(cache_read_tokens)::bigint AS cache_read_tokens",
            "sum(cache_write_tokens)::bigint AS cache_write_tokens",
            "sum(reasoning_tokens)::bigint AS reasoning_tokens", "sum(cost_usd) AS cost_usd",
            "sum(latency_ms_sum)::bigint AS latency_ms_sum", "sum(latency_ms_count)::bigint AS latency_ms_count",
        ]
    sql = (f"{cte}SELECT {', '.join(sel)}\n  FROM {src}\n WHERE " + "\n   AND ".join(where)
           + (f"\n GROUP BY {group}" if group else ""))
    return sql, args


def per_unit_sql(q: ExplorerQuery, since: datetime, until: datetime) -> tuple[str, list[Any]]:
    """(SQL, args) of cost per tour and per piece from the raw LLM ledger, grouped like the table. Pure."""
    args: list[Any] = []

    def arg(v: Any) -> str:
        args.append(v)
        return f"${len(args)}"

    where = [f"created_at >= {arg(since)}", f"created_at < {arg(until)}", ud.SOURCE_WHERE["llm"]]
    where += _filter_clauses(q, lambda d: _dim_raw_llm(d, q.granularity), arg)
    dims = [f"{_dim_raw_llm(d, q.granularity)} AS d_{d}" for d in q.group]
    cte = f"WITH {FAMILY_CTE}\n" if _needs_family(q) else ""
    src = "shared.llm_call_log" + (" LEFT JOIN fam ON fam_key = model" if cte else "")
    sel = dims + [
        "coalesce(sum(cost_usd) FILTER (WHERE tour_id IS NOT NULL), 0) AS tour_cost",
        "count(DISTINCT tour_id)::bigint AS tours",
        "coalesce(sum(cost_usd) FILTER (WHERE content_piece_id IS NOT NULL), 0) AS piece_cost",
        "count(DISTINCT content_piece_id)::bigint AS pieces",
    ]
    group = ", ".join(str(i) for i in range(1, len(q.group) + 1))
    return (f"{cte}SELECT {', '.join(sel)}\n  FROM {src}\n WHERE " + "\n   AND ".join(where)
            + f"\n GROUP BY {group}"), args


# ── rows ────────────────────────────────────────────────────────────────────────────────────────────────────────────

_SUMS = ("requests", "ledger_rows", "failed_requests", "answered_llm", "fallback_requests", "tokens_in", "tokens_out",
         "cache_read_tokens", "cache_write_tokens", "reasoning_tokens", "cost_usd", "latency_ms_sum",
         "latency_ms_count")


def _key_value(d: str, v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.astimezone(timezone.utc).isoformat()
    return str(v)


def bucket_label(start_utc_iso: str, granularity: str) -> str:
    """The ICT label of a bucket start: 2026-11-04T09:00+07:00 (hour), 2026-11-04 (day / week start), 2026-11 (month).
    Pure."""
    t = datetime.fromisoformat(start_utc_iso).astimezone(TZ)
    if granularity == "hour":
        return t.isoformat(timespec="minutes")
    if granularity == "month":
        return t.strftime("%Y-%m")
    return t.date().isoformat()


def bucket_end(start_utc_iso: str, granularity: str) -> str:
    """The end (exclusive, ISO with offset) of the bucket starting at ``start_utc_iso``. Pure."""
    t = datetime.fromisoformat(start_utc_iso).astimezone(TZ)
    if granularity == "hour":
        e = t + timedelta(hours=1)
    elif granularity == "day":
        e = t + timedelta(days=1)
    elif granularity == "week":
        e = t + timedelta(days=7)
    else:
        e = (t.replace(day=1) + timedelta(days=32)).replace(day=1)
    return e.isoformat()


def _span_text(t: datetime) -> str:
    """An ICT instant as a short label: the date at midnight, else the date and the time."""
    return t.date().isoformat() if (t.hour, t.minute) == (0, 0) else t.strftime("%Y-%m-%d %H:%M")


def bucket_span(start_utc_iso: str, granularity: str, since: datetime, until: datetime) -> dict:
    """The bucket of ``start_utc_iso`` clipped to the query range [since, until): ``start`` / ``end`` (ISO, ICT) are
    what the explorer counted, so the Calls list a drill-down opens holds the same rows. A day, week or month bucket
    the range cuts (the first and last of a 7d range, say) is ``partial`` and labelled with its clipped span
    (2026-10-28 14:00–2026-10-29); a whole bucket keeps bucket_label. Pure."""
    b_start = datetime.fromisoformat(start_utc_iso)
    b_end = datetime.fromisoformat(bucket_end(start_utc_iso, granularity))
    start, end = max(b_start, since), min(b_end, until)
    partial = start > b_start or end < b_end
    label = (f"{_span_text(start.astimezone(TZ))}–{_span_text(end.astimezone(TZ))}" if partial
             else bucket_label(start_utc_iso, granularity))
    return {"start": start.astimezone(TZ).isoformat(), "end": end.astimezone(TZ).isoformat(), "label": label,
            "partial": partial}


def new_acc() -> dict:
    return {**{k: 0 for k in _SUMS}, "cost_usd": Decimal(0), "hist": [0] * ud.HIST_BUCKETS}


def add_into(acc: dict, row: dict) -> None:
    for k in _SUMS:
        v = row.get(k)
        if v is None:
            continue
        acc[k] = acc[k] + (Decimal(str(v)) if k == "cost_usd" else int(v))


def finish(acc: dict, unit: Optional[dict] = None) -> dict:
    """The metrics of one group from its sums. Pure."""
    req = int(acc["requests"])
    out: dict[str, Any] = {
        "requests": req, "ledger_rows": int(acc["ledger_rows"]), "tokens_in": int(acc["tokens_in"]),
        "tokens_out": int(acc["tokens_out"]), "cache_read_tokens": int(acc["cache_read_tokens"]),
        "cache_write_tokens": int(acc["cache_write_tokens"]), "reasoning_tokens": int(acc["reasoning_tokens"]),
        "cost_usd": round(float(acc["cost_usd"]), 6),
        "avg_latency_ms": (round(acc["latency_ms_sum"] / acc["latency_ms_count"]) if acc["latency_ms_count"]
                           else None),
        "p95_latency_ms": ud.percentile_ms(acc["hist"]),
        "error_rate": round(acc["failed_requests"] / req, 4) if req else None,
        "fallback_rate": (round(acc["fallback_requests"] / acc["answered_llm"], 4) if acc["answered_llm"]
                          else None),
        "cost_per_tour": None, "cost_per_piece": None,
    }
    if unit is not None:
        tours, pieces = int(unit.get("tours") or 0), int(unit.get("pieces") or 0)
        out["tours"], out["pieces"] = tours, pieces
        out["cost_per_tour"] = round(float(unit.get("tour_cost") or 0) / tours, 6) if tours else None
        out["cost_per_piece"] = round(float(unit.get("piece_cost") or 0) / pieces, 6) if pieces else None
    return out


def sort_rows(rows: list[dict], key: str, direction: str) -> list[dict]:
    """Sort by a metric or a dimension; rows without a value go last in both directions. Pure."""
    def k(r: dict):
        v = r.get(key) if key in METRICS else (r.get("dims") or {}).get(key)
        return v
    present = [r for r in rows if k(r) not in (None, "")]
    missing = [r for r in rows if k(r) in (None, "")]
    present.sort(key=lambda r: (k(r), _tie(r)), reverse=(direction == "desc"))
    return present + missing


def _tie(r: dict) -> str:
    return "|".join(str(v) for v in (r.get("dims") or {}).values())


# ── CRM ─────────────────────────────────────────────────────────────────────────────────────────────────────────────

CrmUsageRows = Callable[[date, date], Awaitable[tuple[Optional[dict], dict]]]


def crm_eligible(q: ExplorerQuery) -> Optional[str]:
    """None when the CRM's daily totals can be merged into this query, else the reason. Pure."""
    if q.granularity == "hour":
        return "hour_granularity"
    for d in q.filters:
        if d not in CRM_DIMS:
            return f"filter_{d}"
    return None


def crm_windows(since: datetime, until: datetime) -> list[tuple[date, date]]:
    """The crm.ops.v1 /llm/usage calls ([from, to] inclusive UTC days, <= CRM_MAX_DAYS each) covering the ICT dates of
    [since, until). Pure."""
    first = since.astimezone(TZ).date()
    last = (until - timedelta(seconds=1)).astimezone(TZ).date()
    out = []
    a = first
    while a <= last:
        b = min(a + timedelta(days=CRM_MAX_DAYS - 1), last)
        out.append((a, b))
        a = b + timedelta(days=1)
    return out


def crm_rows(body_rows: list, families: dict[str, str]) -> list[dict]:
    """The CRM's usage rows as explorer partial rows: per (day, stage, model) an ok part and a failed part. Pure."""
    out = []
    for r in body_rows:
        if not isinstance(r, dict):
            continue
        try:
            day = date.fromisoformat(str(r["day"]))
            calls, failed = int(r.get("calls") or 0), int(r.get("failed_calls") or 0)
            cost = Decimal(str(r.get("cost_usd") or 0))
            tin, tout = int(r.get("input_tokens") or 0), int(r.get("output_tokens") or 0)
        except (KeyError, TypeError, ValueError, ArithmeticError):
            continue
        stage = str(r.get("stage") or "")
        model = str(r.get("model") or "")
        base = {"source": "crm", "app": "crm", "product": "crm", "provider": "", "account": "", "model": model,
                "family": families.get(model, ""), "stage": stage, "role": "", "tenant": "", "error_class": "",
                "traffic": "live", "day": day}
        ok = max(calls - failed, 0)
        if ok:
            out.append({**base, "status": "ok", "requests": ok, "ledger_rows": ok, "answered_llm": 0,
                        "tokens_in": tin, "tokens_out": tout, "cost_usd": cost})
        if failed:
            out.append({**base, "status": "error", "requests": failed, "ledger_rows": failed,
                        "failed_requests": failed, "tokens_in": 0 if ok else tin, "tokens_out": 0 if ok else tout,
                        "cost_usd": Decimal(0) if ok else cost})
    return out


def crm_keep(row: dict, q: ExplorerQuery) -> bool:
    for d, values in q.filters.items():
        v = row.get(d) or ""
        if (NONE_VALUE if v == "" else v) not in values and v not in values:
            return False
    return True


def crm_bucket(day: date, granularity: str) -> str:
    """The UTC ISO bucket start of a CRM day (its ICT date) at a day / week / month granularity. Pure."""
    if granularity == "week":
        day = day - timedelta(days=day.weekday())
    elif granularity == "month":
        day = day.replace(day=1)
    return _local_midnight(day).astimezone(timezone.utc).isoformat()


# ── read ────────────────────────────────────────────────────────────────────────────────────────────────────────────

async def _fetch(pool, sql: str, args: list) -> list[dict]:
    async with pool.acquire() as conn:
        return [dict(r) for r in await conn.fetch(sql, *args)]


async def _families(pool) -> dict[str, str]:
    try:
        rows = await _fetch(pool, f"WITH {FAMILY_CTE} SELECT fam_key, fam_family FROM fam", [])
    except Exception:  # noqa: BLE001 — the CRM's family column is a courtesy
        return {}
    return {r["fam_key"]: r["fam_family"] or "" for r in rows}


async def _tenant_names(pool, ids: list[str]) -> dict[str, str]:
    ids = sorted({i for i in ids if i})
    if not ids:
        return {}
    try:
        rows = await _fetch(pool, "SELECT tenant_id::text AS id, name FROM shared.tenants "
                                  "WHERE tenant_id = ANY($1::uuid[])", [ids])
    except Exception:  # noqa: BLE001 — labels only
        return {}
    return {r["id"]: r["name"] for r in rows}


async def explore(pool, q: ExplorerQuery, *, crm_usage_rows: Optional[CrmUsageRows] = None,
                  now: Optional[datetime] = None) -> dict:
    """GET /admin/usage-explorer/query (see the module docstring)."""
    import asyncpg

    now = now or datetime.now(timezone.utc)
    since, until = resolve_range(q, now)
    check_shape(q, since, until)
    notes: list[str] = []

    main_sql, main_args = aggregate_sql(q, since, until, by=q.group)
    hist_sql, hist_args = aggregate_sql(q, since, until, by=q.group, hist=True)
    series_sql, series_args = aggregate_sql(q, since, until, by=("time",))
    try:
        main, hist, series = await asyncio.gather(_fetch(pool, main_sql, main_args),
                                                  _fetch(pool, hist_sql, hist_args),
                                                  _fetch(pool, series_sql, series_args))
    except asyncpg.UndefinedTableError as e:
        raise ur.RollupUnavailable("shared.ai_usage_hourly is missing (apply CIS 372)") from e

    groups: dict[tuple, dict] = {}
    dims_of: dict[tuple, dict] = {}

    def key_of(row: dict, by: tuple[str, ...]) -> tuple:
        return tuple(_key_value(d, row.get(f"d_{d}")) for d in by)

    for r in main:
        k = key_of(r, q.group)
        acc = groups.setdefault(k, new_acc())
        add_into(acc, r)
        dims_of[k] = {d: v for d, v in zip(q.group, k)}
    for r in hist:
        k = key_of(r, q.group)
        b = int(r["bucket"]) - 1
        if k in groups and 0 <= b < ud.HIST_BUCKETS:
            groups[k]["hist"][b] += int(r["n"] or 0)

    series_acc: dict[str, dict] = {}
    for r in series:
        t = _key_value("time", r.get("d_time"))
        add_into(series_acc.setdefault(t, new_acc()), r)

    # CRM daily totals
    crm: dict[str, Any]
    reason = crm_eligible(q)
    if reason is not None:
        crm = {"status": "not_asked", "reason": reason}
    elif crm_usage_rows is None:
        crm = {"status": "not_asked", "reason": "no_reader"}
    else:
        crm = await _merge_crm(pool, q, since, until, crm_usage_rows, groups, dims_of, series_acc)

    # cost per tour / piece (raw LLM ledger)
    units: dict[tuple, dict] = {}
    if q.per_unit:
        if since < now - ur.RAW_WINDOW:
            notes.append("per_unit_outside_raw_window")
        elif "source" in q.filters and "llm" not in q.filters["source"]:
            notes.append("per_unit_llm_only")
        else:
            sql, args = per_unit_sql(q, since, until)
            for r in await _fetch(pool, sql, args):
                units[key_of(r, q.group)] = r

    rows = []
    total = new_acc()
    for k, acc in groups.items():
        for s in _SUMS:
            total[s] = total[s] + acc[s]
        total["hist"] = ud.merge_hist(total["hist"], acc["hist"])
        rows.append({"key": "|".join(k), "dims": dims_of[k], **finish(acc, units.get(k) if q.per_unit else None)})
    for r in rows:
        if "time" in r["dims"] and r["dims"]["time"]:
            r["bucket"] = bucket_span(r["dims"]["time"], q.granularity, since, until)
            r["dims"]["time"] = r["bucket"]["label"]
    rows = sort_rows(rows, q.sort, q.dir)
    truncated = len(rows) > MAX_GROUPS
    rows = rows[:MAX_GROUPS]

    series_out = []
    for t in sorted(series_acc):
        acc = series_acc[t]
        series_out.append({**bucket_span(t, q.granularity, since, until),
                           "requests": int(acc["requests"]), "tokens": int(acc["tokens_in"]) + int(acc["tokens_out"]),
                           "tokens_in": int(acc["tokens_in"]), "tokens_out": int(acc["tokens_out"]),
                           "cost_usd": round(float(acc["cost_usd"]), 6)})

    tenant_ids = [r["dims"].get("tenant") for r in rows if r["dims"].get("tenant")]
    tenant_ids += [t for t in q.filters.get("tenant", ()) if t != NONE_VALUE]
    last = None
    try:
        last = await ur.last_run(pool)
    except Exception as e:  # noqa: BLE001 — the freshness line is a courtesy
        logger.warning("usage_explorer_last_run_failed", error=repr(e)[:200])
    if last is None:
        notes.append("rollup_never_ran")
    return {
        "generated_at": now.isoformat(),
        "timezone": TZ_NAME,
        "range": {"key": q.range, "from": since.astimezone(TZ).isoformat(), "to": until.astimezone(TZ).isoformat(),
                  "from_utc": since.isoformat(), "to_utc": until.isoformat()},
        "granularity": q.granularity,
        "group": list(q.group),
        "filters": {d: list(v) for d, v in q.filters.items()},
        "sort": {"key": q.sort, "dir": q.dir},
        "rows": rows,
        "truncated": truncated,
        "groups": len(groups),
        "series": series_out,
        "totals": finish(total),
        "labels": {"tenant": await _tenant_names(pool, tenant_ids)},
        "crm": crm,
        "rollup": last,
        "notes": notes,
    }


async def _merge_crm(pool, q: ExplorerQuery, since: datetime, until: datetime, reader: CrmUsageRows,
                     groups: dict, dims_of: dict, series_acc: dict) -> dict:
    families = await _families(pool) if ("family" in q.group or "family" in q.filters) else {}
    first = since.astimezone(TZ).date()
    last = (until - timedelta(seconds=1)).astimezone(TZ).date()
    merged = 0
    for a, b in crm_windows(since, until):
        try:
            body, problem = await reader(a, b)
        except Exception as e:  # noqa: BLE001 — the CRM is optional here
            return {"status": "error", "reason": type(e).__name__}
        if body is None:
            return {"status": problem.get("status") or "error", "reason": problem.get("reason"),
                    "http_status": problem.get("http_status")}
        for r in crm_rows(body.get("rows") if isinstance(body.get("rows"), list) else [], families):
            if not first <= r["day"] <= last or not crm_keep(r, q):
                continue
            bucket = crm_bucket(r["day"], q.granularity)
            r = {**r, "time": bucket}
            k = tuple(_key_value(d, r.get(d)) for d in q.group)
            add_into(groups.setdefault(k, new_acc()), r)
            dims_of.setdefault(k, {d: v for d, v in zip(q.group, k)})
            add_into(series_acc.setdefault(bucket, new_acc()), r)
            merged += 1
    return {"status": "ok", "rows": merged, "days": "utc"}


# ── options, invoices, saved views ──────────────────────────────────────────────────────────────────────────────────

async def options(pool, q: ExplorerQuery, now: Optional[datetime] = None) -> dict:
    """The values each dimension takes in the query's window (for the filter lists), with tenant names."""
    import asyncpg

    now = now or datetime.now(timezone.utc)
    since, until = resolve_range(q, now)
    out: dict[str, list[str]] = {}
    try:
        async with pool.acquire() as conn:
            for d, col in ud.ROLLUP_COLUMN.items():
                expr = f"{col}::text" if d == "tenant" else col
                rows = await conn.fetch(f"SELECT DISTINCT coalesce({expr}, '') AS v FROM shared.ai_usage_hourly "
                                        "WHERE hour >= $1 AND hour < $2 ORDER BY 1 LIMIT 500", since, until)
                out[d] = [r["v"] or NONE_VALUE for r in rows]
            fams = await conn.fetch("SELECT DISTINCT family FROM shared.llm_model_catalog WHERE family IS NOT NULL "
                                    "ORDER BY 1")
    except asyncpg.UndefinedTableError as e:
        raise ur.RollupUnavailable("shared.ai_usage_hourly is missing (apply CIS 372)") from e
    out["family"] = [r["family"] for r in fams] + [NONE_VALUE]
    out["product"] = sorted({ud.product_of(s) for s in out.get("stage", []) if s != NONE_VALUE} | {"crm", "other"})
    for d, extra in (("source", "crm"), ("app", "crm")):
        if extra not in out.setdefault(d, []):
            out[d].append(extra)
    names = await _tenant_names(pool, [t for t in out.get("tenant", []) if t != NONE_VALUE])
    return {"range": {"from": since.astimezone(TZ).isoformat(), "to": until.astimezone(TZ).isoformat()},
            "values": out, "labels": {"tenant": names}}


INVOICE_LEDGER_SQL = """
    SELECT provider, coalesce(account, '') AS account, sum(requests)::bigint AS requests, sum(cost_usd) AS cost_usd
      FROM shared.ai_usage_hourly
     WHERE hour >= $1 AND hour < $2
     GROUP BY 1, 2
"""
INVOICE_RECONCILE_SQL = """
    SELECT connection_key, sum(provider_usd) AS provider_usd, count(provider_usd)::int AS days_with_figure,
           array_agg(DISTINCT source) AS sources
      FROM shared.llm_cost_reconcile
     WHERE day >= $1 AND day < $2
     GROUP BY 1
"""


def month_range(month: Optional[str], now: datetime) -> tuple[date, date]:
    """[first day, first day of next month) of a YYYY-MM (UTC months: provider invoices are UTC); default last month."""
    if month:
        try:
            first = date.fromisoformat(f"{month}-01")
        except ValueError:
            raise ValueError("month must be YYYY-MM") from None
    else:
        this = now.astimezone(timezone.utc).date().replace(day=1)
        first = (this - timedelta(days=1)).replace(day=1)
    nxt = (first + timedelta(days=32)).replace(day=1)
    return first, nxt


async def invoices(pool, month: Optional[str], now: Optional[datetime] = None) -> dict:
    """Per provider and account: the ledger's month total next to the provider's own figure where one is stored
    (shared.llm_cost_reconcile, the nightly reconciliation: Cost Explorer for Bedrock, the vendor usage APIs once a
    usage key is set) and the invoice amount, which nothing records yet."""
    import asyncpg

    now = now or datetime.now(timezone.utc)
    first, nxt = month_range(month, now)
    a = datetime(first.year, first.month, 1, tzinfo=timezone.utc)
    b = datetime(nxt.year, nxt.month, 1, tzinfo=timezone.utc)
    try:
        ledger = await _fetch(pool, INVOICE_LEDGER_SQL, [a, b])
    except asyncpg.UndefinedTableError as e:
        raise ur.RollupUnavailable("shared.ai_usage_hourly is missing (apply CIS 372)") from e
    try:
        rec = {r["connection_key"]: r for r in await _fetch(pool, INVOICE_RECONCILE_SQL, [first, nxt])}
    except Exception:  # noqa: BLE001 — the reconciliation table is optional here
        rec = {}
    rows = []
    for r in sorted(ledger, key=lambda r: -float(r["cost_usd"] or 0)):
        x = rec.get(r["account"])
        rows.append({"provider": r["provider"], "account": r["account"] or None, "requests": int(r["requests"]),
                     "ledger_usd": round(float(r["cost_usd"] or 0), 6),
                     "provider_usd": round(float(x["provider_usd"]), 6) if x and x["provider_usd"] is not None
                     else None,
                     "provider_days": int(x["days_with_figure"]) if x else 0,
                     "provider_source": [s for s in (x["sources"] or []) if s != "none"] if x else [],
                     "invoice_usd": None})
    return {"month": first.strftime("%Y-%m"), "from": first.isoformat(), "to": nxt.isoformat(), "rows": rows,
            "invoice_source": None,
            "missing": "No provider invoice is recorded anywhere yet: the invoice column stays empty until the "
                       "invoice reconciliation stores each provider's monthly invoice amount."}


VIEW_LIMIT = 50
VIEW_KEYS = frozenset({"range", "from", "to", "granularity", "group", "sort", "dir", "per_unit", "metrics"}
                      | {f"f_{d}" for d in DIMENSIONS})
_VIEW_COLS = "id::text AS id, name, query, created_at, updated_at"


class ViewsUnavailable(RuntimeError):
    """shared.ai_usage_saved_view is missing (CIS 372 not applied)."""


def check_view(name: Any, query: Any) -> tuple[str, dict]:
    """(name, query) of a saved view, or ValueError. The query must parse as an explorer query. Pure."""
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
        raise ValueError("name must be 1 to 80 characters")
    if not isinstance(query, dict):
        raise ValueError("query must be an object of explorer parameters")
    clean: dict[str, str] = {}
    for k, v in query.items():
        if k not in VIEW_KEYS:
            raise ValueError(f"query has an unknown parameter {k!r}")
        if not isinstance(v, str) or len(v) > 1200:
            raise ValueError(f"query parameter {k!r} must be a short string")
        if v.strip():
            clean[k] = v.strip()
    parse_query(clean)
    return name.strip(), clean


def _view(r: dict) -> dict:
    q = r["query"]
    if isinstance(q, str):
        import json
        q = json.loads(q)
    return {"id": r["id"], "name": r["name"], "query": q, "created_at": r["created_at"].isoformat(),
            "updated_at": r["updated_at"].isoformat()}


async def list_views(pool, staff_id: str) -> list[dict]:
    import asyncpg
    try:
        rows = await _fetch(pool, f"SELECT {_VIEW_COLS} FROM shared.ai_usage_saved_view WHERE staff_id = $1 "
                                  "ORDER BY lower(name)", [staff_id])
    except asyncpg.UndefinedTableError as e:
        raise ViewsUnavailable("shared.ai_usage_saved_view is missing (apply CIS 372)") from e
    return [_view(r) for r in rows]


async def save_view(pool, staff_id: str, name: str, query: dict) -> dict:
    """Create the view, or replace the one with the same name (case-insensitive). LookupError past VIEW_LIMIT."""
    import json

    import asyncpg
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SELECT pg_advisory_xact_lock(hashtext('ai_usage_saved_view:' || $1))", staff_id)
                row = await conn.fetchrow(
                    f"UPDATE shared.ai_usage_saved_view SET name = $3, query = $4::jsonb, updated_at = now() "
                    f"WHERE staff_id = $1 AND lower(btrim(name)) = lower(btrim($2)) RETURNING {_VIEW_COLS}",
                    staff_id, name, name, json.dumps(query))
                if row is None:
                    n = await conn.fetchval("SELECT count(*) FROM shared.ai_usage_saved_view WHERE staff_id = $1",
                                            staff_id)
                    if n >= VIEW_LIMIT:
                        raise LookupError(f"at most {VIEW_LIMIT} saved views; delete one first")
                    row = await conn.fetchrow(
                        f"INSERT INTO shared.ai_usage_saved_view (staff_id, name, query) VALUES ($1, $2, $3::jsonb) "
                        f"RETURNING {_VIEW_COLS}", staff_id, name, json.dumps(query))
    except asyncpg.UndefinedTableError as e:
        raise ViewsUnavailable("shared.ai_usage_saved_view is missing (apply CIS 372)") from e
    return _view(dict(row))


async def delete_view(pool, staff_id: str, view_id: str) -> bool:
    import asyncpg
    try:
        status = await pool.execute("DELETE FROM shared.ai_usage_saved_view WHERE id = $1::uuid AND staff_id = $2",
                                    view_id, staff_id)
    except asyncpg.UndefinedTableError as e:
        raise ViewsUnavailable("shared.ai_usage_saved_view is missing (apply CIS 372)") from e
    return str(status).endswith(" 1")
