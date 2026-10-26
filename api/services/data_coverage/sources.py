"""AA-992 — run the sources of the data coverage page. Every source returns a flat dict of named numbers or raises;
:func:`run_source` turns the outcome into ``{status, reason, timing_ms, values}`` and never raises.

Budget (design §4.3):
- a CIS source (``cis_app`` on ``app.state.pool``): its own connection and ``READ ONLY`` transaction with
  ``SET LOCAL statement_timeout = '4s'`` and ``lock_timeout = '1s'``, inside ``asyncio.wait_for(…, 5 s)``; at most
  :data:`CIS_CONCURRENCY` at once (the pool has 10 connections per task);
- a TripPlanner catalog source: one :func:`services.catalog_http.reader.catalog_connection` per statement (the tenant
  pool has 2 connections per task, shared with the Content studio and the CRM catalog sync, and its process-wide gate
  lets one catalog read in at a time and refuses a waiter after 5 s; critic B9). The five catalog sources of one build
  therefore run one after another: :attr:`Ctx.catalog_lock` (FIFO, registry order) admits one of them at a time and
  each releases the reader's gate before the next asks for it, so they never queue on the gate itself. ``wait_for``
  8 s from the moment the lock let the read in, ``SET LOCAL statement_timeout = '7s'``; slow tier only. ``tp_places``
  (``catalog.v_tour_place`` read whole: 6.1 s on Dev, S252) has its own :data:`TP_PLACES_STATEMENT_TIMEOUT` /
  :data:`TP_PLACES_WAIT_S` and runs last, so a slow place read turns only the ``tp.located`` row n/a;
- S3: ``ListObjectsV2`` in a thread, 5 s, at most :data:`S3_MAX_PAGES` pages (then the count is marked capped);
- CRM: ``crm.ops.v1`` 1.4 ``GET /data-coverage`` with the signed-in staff member's own token (``crm_console``).

Errors (fix round 2: only a final answer may be ``unavailable``, since a tier whose sources are all ok / unavailable
is cached for its full TTL and kept 24 h as the last good copy):
- ``unavailable`` — final until a deploy or a migration: a missing owner view or grant (``UndefinedTable`` /
  ``InsufficientPrivilege`` / ``InvalidSchemaName`` / ``UndefinedColumn``) is ``contract_view_missing`` (tp-020 /
  bk-025 not applied yet); ``cognito_session_required`` (a password session); ``catalog_unavailable: tenant pool not
  configured``;
- ``timeout`` — ``asyncio.TimeoutError``, ``QueryCanceled`` (statement_timeout), the catalog reader's own
  statement_timeout, a CRM 504;
- ``error`` — anything else, including the temporary :class:`Transient` answers: the catalog gate busy, a pool
  acquire error or a dropped tenant connection, and every other CRM non-200 (503 ``crm_unavailable`` on a network
  error or timeout, 502 ``crm_bad_response``, 401 / 403 / 404 / 429: the crm tier key is shared by every admin, so
  one caller's expired token or missing group must not blank the rows for the others for 10 minutes).
One ``data_coverage_source_failed`` log line per failure (key, class, ms), never SQL text or values.
"""
from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

import asyncpg
import structlog

from services.acp_shared.real_questions import TOPUP_MARKET

from . import queries as q
from .defs import MASTER_TENANT

logger = structlog.get_logger()

CIS_CONCURRENCY = 3
CIS_STATEMENT_TIMEOUT = "4s"
CIS_LOCK_TIMEOUT = "1s"
CIS_WAIT_S = 5.0
TENANT_WAIT_S = 8.0
TENANT_STATEMENT_TIMEOUT = "7s"
# tp_places only: 2.5 x the 6.1 s measured on Dev (S252). It holds the reader's process-wide gate that long (the
# Content studio / CRM sync reads wait up to 5 s, then get 503), at most once per slow-tier computation (600 s cache,
# single flight). build.TIER_DEADLINE_S["slow"] leaves it room after the quick catalog sources.
TP_PLACES_STATEMENT_TIMEOUT = "15s"
TP_PLACES_WAIT_S = 16.0
FINAL_CATALOG_REASONS = ("tenant pool not configured",)
S3_WAIT_S = 5.0
S3_MAX_PAGES = 10
CRM_PASS_LIMIT_MIN = 120

CONTRACT_ERRORS = (asyncpg.exceptions.UndefinedTableError, asyncpg.exceptions.InvalidSchemaNameError,
                   asyncpg.exceptions.InsufficientPrivilegeError, asyncpg.exceptions.UndefinedColumnError)


class Unavailable(Exception):  # noqa: N818 — reads as an outcome
    """A FINAL answer: the source cannot be read in this context until a deploy or a migration (reason code in
    ``args[0]``). Its tier may be cached for the full TTL and kept as the last good copy."""


class Transient(Exception):  # noqa: N818 — reads as an outcome
    """A temporary failure (catalog gate busy, dropped connection, CRM 5xx / 4xx): ``error`` with the reason code in
    ``args[0]``, so its tier is cached 60 s and never kept as the last good copy."""


class Ctx:
    """What the sources need for one build."""

    def __init__(self, pool: Any, tenant_pool: Any = None, authorization: Optional[str] = None,
                 s3: Any = None, bucket: Optional[str] = None) -> None:
        self.pool = pool
        self.tenant_pool = tenant_pool
        self.authorization = authorization
        self.s3 = s3
        self.bucket = bucket
        self.gate = asyncio.Semaphore(CIS_CONCURRENCY)
        self.catalog_lock = asyncio.Lock()   # the tenant-pool sources run one after another (FIFO)


def _jsonable(v: Any) -> Any:
    if isinstance(v, datetime):
        return v.isoformat()
    return v


async def _cis_fetch(ctx: Ctx, sql: str, args: tuple) -> list:
    """One statement in its own READ ONLY transaction with the statement / lock timeouts. The 5 s wait_for starts once
    the gate (CIS_CONCURRENCY) let the statement in, so queueing behind other sources never counts as its timeout."""
    async def run() -> list:
        async with ctx.pool.acquire() as conn:
            async with conn.transaction(readonly=True):
                await conn.execute(f"SET LOCAL statement_timeout = '{CIS_STATEMENT_TIMEOUT}'")
                await conn.execute(f"SET LOCAL lock_timeout = '{CIS_LOCK_TIMEOUT}'")
                return list(await conn.fetch(sql, *args))

    async with ctx.gate:
        return await asyncio.wait_for(run(), timeout=CIS_WAIT_S)


async def _cis_row(ctx: Ctx, sql: str, *args: Any) -> dict:
    rows = await _cis_fetch(ctx, sql, args)
    return {k: _jsonable(v) for k, v in dict(rows[0] if rows else {}).items()}


async def _cis_rows(ctx: Ctx, sql: str, *args: Any) -> list[asyncpg.Record]:
    return await _cis_fetch(ctx, sql, args)


async def _catalog(ctx: Ctx, sql: str, *, many: bool = False, statement_timeout: str = TENANT_STATEMENT_TIMEOUT,
                   wait_s: float = TENANT_WAIT_S) -> Any:
    from services.catalog_http.reader import CatalogUnavailable, catalog_connection

    async def run() -> Any:
        async with catalog_connection(ctx.tenant_pool) as conn:
            await conn.execute(f"SET LOCAL statement_timeout = '{statement_timeout}'")
            return list(await conn.fetch(sql)) if many else await conn.fetchrow(sql)

    async with ctx.catalog_lock:   # one catalog source at a time; the reader's gate is free again on release
        try:
            return await asyncio.wait_for(run(), timeout=wait_s)
        except CatalogUnavailable as e:
            reason = str(e)
            if reason in FINAL_CATALOG_REASONS:
                raise Unavailable(f"catalog_unavailable: {reason}") from e
            if reason == "QueryCanceledError":   # the statement_timeout above (or the pool's)
                raise asyncio.TimeoutError() from e
            raise Transient(f"catalog_unavailable: {reason}"[:80]) from e


def _metric_rows(rows: list) -> dict:
    return {r["metric"]: int(r["n"]) for r in rows}


# ── source functions (key -> async fn(ctx) -> dict) ──────────────────────────────────────────────────────────────────
def _simple(sql: str, with_master: bool = True) -> Callable[[Ctx], Awaitable[dict]]:
    async def fn(ctx: Ctx) -> dict:
        return await (_cis_row(ctx, sql, MASTER_TENANT) if with_master else _cis_row(ctx, sql))
    return fn


async def rank(ctx: Ctx) -> dict:
    """Ranking coverage; the market counts leave out the PAA top-up market (never a ranking market)."""
    return await _cis_row(ctx, q.RANK, MASTER_TENANT, TOPUP_MARKET)


async def in_drops(ctx: Ctx) -> dict:
    rows = await _cis_rows(ctx, q.IN_DROPS, MASTER_TENANT)
    by = {r["reason"]: int(r["n"] or 0) for r in rows}
    out = {reason: by.pop(reason, 0) for reason in q.DROP_REASONS}
    out["by_design"] = sum(out.values())
    out["insert_failed"] = sum(by.values())   # every other reason is an insert failure (its text stays here)
    return out


async def seg_classify(ctx: Ctx) -> dict:
    rows = await _cis_rows(ctx, q.SEG_NO_MEMBER_ATOMS, MASTER_TENANT)
    from services.acp_contract.segment_matching import SegmentAtom, forms_segment

    out = {"atoms_without_member": len(rows), "no_place": 0, "no_action": 0, "excluded_by_rule": 0, "unexplained": 0}
    unsegmented: dict[str, bool] = {}       # tour without any segment -> every atom of it excluded so far
    for r in rows:
        place, action = (r["place"] or "").strip(), (r["action"] or "").strip()
        if not place:
            k = "no_place"
        elif not action:
            k = "no_action"
        else:
            try:
                ok = forms_segment(SegmentAtom(r["atom_id"], r["tour_id"], r["itinerary_day"], place, action, "",
                                               r["activity_type"]))
            except Exception:  # noqa: BLE001 — an atom the rule cannot read counts as unexplained
                ok = True
            k = "unexplained" if ok else "excluded_by_rule"
        out[k] += 1
        if not r["tour_has_segment"]:
            unsegmented[r["tour_id"]] = unsegmented.get(r["tour_id"], True) and k != "unexplained"
    out["excluded_by_rule"] += out["no_place"] + out["no_action"]   # transit / unnamed: neither can form a segment
    out["all_excluded_tours"] = sum(1 for v in unsegmented.values() if v)
    return out


async def tp_totals(ctx: Ctx) -> dict:
    return _metric_rows(await _cis_rows(ctx, q.TP_TOTALS))


async def bk_totals(ctx: Ctx) -> dict:
    return _metric_rows(await _cis_rows(ctx, q.BK_TOTALS))


async def tp_pins(ctx: Ctx) -> dict:
    pins = await _cis_rows(ctx, q.TP_PINS)
    edges = await _cis_rows(ctx, q.TP_ID_MAP)
    return await _cis_row(ctx, q.TP_PIN_CHAIN, MASTER_TENANT,
                          [r["component_id"] for r in pins], [int(r["trips"]) for r in pins],
                          [r["old_id"] for r in edges], [r["new_id"] for r in edges])


async def tp_layers(ctx: Ctx) -> dict:
    return dict(await _catalog(ctx, q.TENANT_TP_LAYERS) or {})


async def tp_places(ctx: Ctx) -> dict:
    return dict(await _catalog(ctx, q.TENANT_TP_PLACES, statement_timeout=TP_PLACES_STATEMENT_TIMEOUT,
                               wait_s=TP_PLACES_WAIT_S) or {})


async def tp_components(ctx: Ctx) -> dict:
    return dict(await _catalog(ctx, q.TENANT_TP_COMPONENTS) or {})


async def tp_retired(ctx: Ctx) -> dict:
    return dict(await _catalog(ctx, q.TENANT_TP_RETIRED) or {})


async def tp_purge(ctx: Ctx) -> dict:
    keys = [r["idempotency_key"] for r in await _catalog(ctx, q.TENANT_TP_OUTBOX, many=True)]
    return await _cis_row(ctx, q.TP_PURGE, keys)


# ── S3 ───────────────────────────────────────────────────────────────────────────────────────────────────────────────
def _s3_client() -> Any:
    import boto3

    return boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-west-1"))


def _bucket(ctx: Ctx) -> str:
    if ctx.bucket:
        return ctx.bucket
    from shared.aws_client.bronze import bronze_bucket

    return bronze_bucket()


def _list_keys(client: Any, bucket: str, prefix: str) -> tuple[list[str], bool]:
    keys: list[str] = []
    token = None
    for _ in range(S3_MAX_PAGES):
        kw = {"Bucket": bucket, "Prefix": prefix}
        if token:
            kw["ContinuationToken"] = token
        page = client.list_objects_v2(**kw)
        keys.extend(o["Key"] for o in page.get("Contents", []))
        if not page.get("IsTruncated"):
            return keys, False
        token = page.get("NextContinuationToken")
    return keys, True


async def _s3_keys(ctx: Ctx, prefix: str) -> tuple[list[str], bool]:
    client = ctx.s3 or _s3_client()
    return await asyncio.wait_for(asyncio.to_thread(_list_keys, client, _bucket(ctx), prefix), timeout=S3_WAIT_S)


async def s3_raw_inbox(ctx: Ctx) -> dict:
    keys, capped = await _s3_keys(ctx, f"raw-inbox/{MASTER_TENANT}/")
    files = {k for k in keys if k.lower().endswith((".xlsx", ".xls"))}
    recorded = {r["s3_path"] for r in await _cis_rows(ctx, q.IN_S3_PATHS, MASTER_TENANT)}
    return {"objects": len(files), "not_ingested": len(files - recorded), "capped": capped}


async def s3_brand(ctx: Ctx) -> dict:
    keys, capped = await _s3_keys(ctx, "brand-identity/")
    tenants = {k.split("/")[1] for k in keys if k.count("/") >= 2 and k.split("/")[1]}
    files = [k for k in keys if not k.endswith("/")]
    return {"objects": len(files), "tenant_prefixes": len(tenants), "capped": capped}


# ── CRM (crm.ops.v1 1.4) ─────────────────────────────────────────────────────────────────────────────────────────────
async def crm(ctx: Ctx) -> dict:
    from api.services import crm_console
    from api.services.platform_apps import _cognito_bearer

    if _cognito_bearer(ctx.authorization) is None:
        raise Unavailable("cognito_session_required")
    status, body = await crm_console.crm_ops_call("GET", "/data-coverage", ctx.authorization)
    if status == 504:
        raise asyncio.TimeoutError()
    if status != 200:   # temporary or caller-specific (see the module docstring): never a final answer
        raise Transient(f"crm_{body.get('detail') or status}"[:80])
    ag, mem, cache = body.get("agencies") or {}, body.get("members") or {}, body.get("catalog_cache") or {}
    last_pass = cache.get("last_full_pass_at")
    pass_at = datetime.fromisoformat(last_pass) if last_pass else None
    age_min = None if pass_at is None else int((datetime.now(timezone.utc) - pass_at).total_seconds() // 60)
    # The CIS live set as of the CRM's last pass (critic B12-3): a tour published after it is the next pass's work.
    live = await _cis_row(ctx, q.LIVE_SINCE, MASTER_TENANT, pass_at or datetime.now(timezone.utc))
    fresh = age_min is not None and age_min <= CRM_PASS_LIMIT_MIN
    out = {f"agencies_{k}": int(ag.get(k) or 0) for k in ("active", "locked", "offboarding", "offboarded",
                                                         "synthetic")}
    out |= {f"members_{k}": int(mem.get(k) or 0) for k in ("active", "invited", "disabled")}
    out |= {"cache_served": int(cache.get("served") or 0), "cache_paused": int(cache.get("paused") or 0),
            "cache_retired": int(cache.get("retired") or 0), "cache_failures": int(cache.get("failures") or 0),
            "cache_age_min": age_min, "live_at_pass": live["live"],
            "published_after_pass": live["published_after"] if fresh else 0, "crm_as_of": body.get("as_of")}
    return out


FUNCTIONS: dict[str, Callable[[Ctx], Awaitable[dict]]] = {
    "in_files": _simple(q.IN_FILES),
    "in_batches": _simple(q.IN_BATCHES),
    "in_drops": in_drops,
    "in_staging": _simple(q.IN_STAGING),
    "s3_raw_inbox": s3_raw_inbox,
    "s3_brand": s3_brand,
    "raw": _simple(q.RAW),
    "s1": _simple(q.S1),
    "master": _simple(q.MASTER),
    "review": _simple(q.REVIEW, with_master=False),
    "seo": _simple(q.SEO),
    "atoms": _simple(q.ATOMS),
    "seg": _simple(q.SEG),
    "seg_classify": seg_classify,
    "gc": _simple(q.GC, with_master=False),
    "rank": rank,
    "route": _simple(q.ROUTE),
    "paa": _simple(q.PAA, with_master=False),
    "emb": _simple(q.EMB, with_master=False),
    "geo": _simple(q.GEO),
    "card": _simple(q.CARD),
    "photo": _simple(q.PHOTO),
    "ten": _simple(q.TEN),
    "t2": _simple(q.T2, with_master=False),
    "tact": _simple(q.TACT, with_master=False),
    "t9": _simple(q.T9, with_master=False),
    "aa_content": _simple(q.AA_CONTENT, with_master=False),
    "tp_totals": tp_totals,
    "tp_pins": tp_pins,
    "tp_layers": tp_layers,
    "tp_components": tp_components,
    "tp_retired": tp_retired,
    "tp_purge": tp_purge,
    "tp_places": tp_places,
    "bk_totals": bk_totals,
    "bk_keys": _simple(q.BK_PLAN_KEYS),
    "supply": _simple(q.SUPPLY, with_master=False),
    "bk_cases": _simple(q.BK_CASES, with_master=False),
    "bk_funnel": _simple(q.BK_FUNNEL, with_master=False),
    "crm": crm,
}


async def run_source(key: str, ctx: Ctx, tier: str) -> dict:
    """``{status: ok|error|timeout|unavailable, reason, timing_ms, tier, values}``; never raises."""
    started = time.monotonic()
    status, reason, values = "ok", None, {}
    try:
        values = await FUNCTIONS[key](ctx)
    except Unavailable as e:
        status, reason = "unavailable", str(e)[:80]
    except Transient as e:
        status, reason = "error", str(e)[:80]
    except CONTRACT_ERRORS:
        status, reason = "unavailable", "contract_view_missing"
    except (asyncio.TimeoutError, asyncpg.exceptions.QueryCanceledError):
        status, reason = "timeout", "timeout"
    except Exception as e:  # noqa: BLE001 — one source never fails the page
        status, reason = "error", type(e).__name__
    ms = round((time.monotonic() - started) * 1000)
    if status != "ok":
        logger.warning("data_coverage_source_failed", source=key, status=status, reason=reason, ms=ms)
    return {"status": status, "reason": reason, "timing_ms": ms, "tier": tier, "values": values}
