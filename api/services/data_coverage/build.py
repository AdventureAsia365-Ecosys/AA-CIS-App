"""AA-992 — build the data coverage response: run the sources by tier (cached), then evaluate every row.

Cache (design D8, ``ops_overview.cache.cached_section`` under ``admin:data-coverage``):
- ``fast`` (DB counts as ``cis_app``): 60 s; ``?refresh=1`` recomputes it only when the cached copy is older than
  :data:`REFRESH_MIN_AGE_S` (a second click within 15 s is served from the cache);
- ``slow`` (segment classification in Python, S3 listings, TripPlanner catalog views over the tenant pool, the pin
  chain): 600 s;
- ``crm`` (``crm.ops.v1`` over HTTP with the caller's own Cognito token): 600 s, its own key, filled only by a call
  that carries a Cognito token (a password session gets ``na: cognito_session_required`` and fills nothing; critic B12).
A tier whose sources all answered (ok, or a final ``unavailable``: see ``sources``) is cached for its TTL and kept 24 h
as the last good copy; one with an error or a timeout only 60 s. A tier has its :data:`TIER_DEADLINE_S`: the sources
that finished by then keep their answers, only the ones still running are cancelled and marked ``timeout: time_budget``
(design D7: a slow source turns only its own rows into n/a). The slow tier's deadline is longer (S252): its last
catalog source, ``tp_places``, may run :data:`sources.TP_PLACES_WAIT_S` after the quick ones; it stays under the API
Gateway's 29 s and :data:`LOCK_TTL_S`, and each other source still stops at its own ``wait_for``. One computation per
tier at a time (``SET NX`` lock, 30 s); a second caller meanwhile gets the last good copy (``stale``) or
``na: refreshing``.

No ids, names or samples leave this module: every value is a count, a percentage, a day / minute age or a timestamp.
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from typing import Any, Optional

import structlog

from api.services.ops_overview import cache

from . import registry as reg
from .sources import Ctx, run_source

logger = structlog.get_logger()

KEY_PREFIX = "admin:data-coverage"
TIER_TTL_S = {"fast": 60, "slow": 600, "crm": 600}
LOCK_TTL_S = 30
REFRESH_MIN_AGE_S = 15
TIER_DEADLINE_S = {"fast": 12.0, "slow": 20.0, "crm": 12.0}


class _Missing(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _settled(result: dict) -> bool:
    return result["status"] in ("ok", "unavailable")


async def _compute_tier(ctx: Ctx, tier: str) -> dict:
    keys = [s.key for s in reg.SOURCES if s.tier == tier]
    # Tasks start in registry order, so the catalog sources take Ctx.catalog_lock in that order.
    tasks = {k: asyncio.ensure_future(run_source(k, ctx, tier)) for k in keys}
    deadline = TIER_DEADLINE_S[tier]
    done: set = set()
    if tasks:
        done, pending = await asyncio.wait(tasks.values(), timeout=deadline)
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)   # let each release its connection / lock
    sources = {}
    for k, t in tasks.items():
        if t in done and not t.cancelled():
            sources[k] = t.result()    # run_source never raises
        else:
            logger.warning("data_coverage_source_failed", source=k, status="timeout", reason="time_budget",
                           ms=int(deadline * 1000))
            sources[k] = {"status": "timeout", "reason": "time_budget", "timing_ms": int(deadline * 1000),
                          "tier": tier, "values": {}}
    return {"status": "ok" if all(_settled(r) for r in sources.values()) else "partial",
            "computed_at": _now().isoformat(), "sources": sources}


async def _maybe_expire_fast(redis: Any) -> None:
    """``refresh=1``: drop the cached fast tier when it is older than REFRESH_MIN_AGE_S (refresh throttle)."""
    key = cache.section_key("fast", KEY_PREFIX)
    try:
        raw = await redis.get(key)
        if not raw:
            return
        data = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
        age = (_now() - datetime.fromisoformat(data["computed_at"])).total_seconds()
        if age >= REFRESH_MIN_AGE_S:
            await redis.delete(key)
    except Exception as e:  # noqa: BLE001 — a refresh that cannot read the cache just serves what is there
        logger.warning("data_coverage_refresh_failed", error=type(e).__name__)


async def _tier(ctx: Ctx, redis: Any, tier: str) -> dict:
    async def compute() -> dict:
        return await _compute_tier(ctx, tier)

    out = await cache.cached_section(redis, tier, TIER_TTL_S[tier], compute, needs_cache=False,
                                     key_prefix=KEY_PREFIX, single_flight=True, lock_ttl_s=LOCK_TTL_S)
    if "sources" not in out:   # cache answered "refreshing" / "error": every source of the tier is n/a
        reason = out.get("reason") or "error"
        out = {**out, "computed_at": None, "sources": {
            s.key: {"status": "unavailable", "reason": reason, "timing_ms": 0, "tier": tier, "values": {}}
            for s in reg.SOURCES if s.tier == tier}}
    return out


async def _crm_tier(ctx: Ctx, redis: Any) -> dict:
    from api.services.platform_apps import _cognito_bearer

    if _cognito_bearer(ctx.authorization) is None:
        return {"computed_at": None, "_cached": False, "sources": {"crm": {
            "status": "unavailable", "reason": "cognito_session_required", "timing_ms": 0, "tier": "crm",
            "values": {}}}}
    return await _tier(ctx, redis, "crm")


# ── rows ─────────────────────────────────────────────────────────────────────────────────────────────────────────────
def _getter(sources: dict) -> Any:
    def g(src: str, name: str) -> Any:
        s = sources.get(src)
        if s is None:
            raise _Missing("unknown_source")
        if s["status"] != "ok":
            raise _Missing(f"{s['status']}: {s.get('reason') or ''}".rstrip(": "))
        v = s["values"].get(name)
        if v is None:
            raise _Missing("no_data_yet")
        return v
    return g


def _missing(row: reg.Row, value: int, den: Optional[int]) -> Optional[int]:
    if row.polarity == "coverage":
        return None if den is None else max(0, den - value)
    if row.polarity == "equal":
        return None if den is None else abs(den - value)
    if row.polarity == "threshold":
        return max(0, value - (row.limit or 0))
    return value


def evaluate(row: reg.Row, sources: dict, tier_as_of: dict) -> dict:
    """One CoverageRow. Status: info rows never get a colour; ok when missing <= expected gap; na with the reason
    when a source the row reads failed, or ``expected gap unavailable`` when only the expected gap's source did."""
    g = _getter(sources)
    srcs = [reg.SOURCE_BY_KEY[k] for k in row.sources if k in reg.SOURCE_BY_KEY]
    out: dict[str, Any] = {
        "id": row.id, "label": row.label, "unit": row.unit, "kind": row.kind, "polarity": row.polarity,
        "value": None, "denominator": None, "denominator_label": row.denominator_label, "pct": None,
        "missing": None, "expected_gap": None, "expected_note": row.expected_note, "limit": row.limit,
        "status": "na", "reason": None, "issue": row.issue, "drill": row.drill,
        "source": "; ".join(s.reads for s in srcs),
        "timing_ms": max((int((sources.get(s.key) or {}).get("timing_ms") or 0) for s in srcs), default=0),
        "as_of": None,
    }
    tiers = [s.tier for s in srcs]
    out["as_of"] = min((tier_as_of[t] for t in tiers if tier_as_of.get(t)), default=None)
    if any(k == "gc" for k in row.sources):
        out["as_of"] = (sources.get("gc") or {}).get("values", {}).get("finished_at") or out["as_of"]
    if any((sources.get(k) or {}).get("values", {}).get("capped") for k in row.sources):
        out["expected_note"] = f"{row.expected_note + '; ' if row.expected_note else ''}listing capped: at least"
    try:
        value = int(row.value(g))
        den = int(row.denominator(g)) if row.denominator else None
    except _Missing as e:
        out["reason"] = e.reason
        return out
    out["value"], out["denominator"] = value, den
    if den:
        out["pct"] = round(100.0 * value / den, 1)
    missing = _missing(row, value, den)
    out["missing"] = missing
    expected = 0
    if row.expected is not None:
        try:
            expected = int(row.expected(g))
            out["expected_gap"] = expected
        except _Missing as e:
            if row.kind != "info":
                out["status"], out["reason"] = "na", f"expected gap unavailable ({e.reason})"
                return out
    if row.kind == "info":
        out["status"] = "info"
    elif missing is None:
        out["status"], out["reason"] = "na", "no denominator"
    else:
        out["status"] = "ok" if missing <= expected else "gap"
    return out


async def build(pool: Any, tenant_pool: Any, redis: Any, *, authorization: Optional[str] = None,
                refresh: bool = False, s3: Any = None, bucket: Optional[str] = None) -> dict:
    started = time.monotonic()
    ctx = Ctx(pool, tenant_pool, authorization, s3=s3, bucket=bucket)
    if refresh and redis is not None:
        await _maybe_expire_fast(redis)
    fast, slow, crm = await asyncio.gather(_tier(ctx, redis, "fast"), _tier(ctx, redis, "slow"),
                                           _crm_tier(ctx, redis))
    sources = {**fast["sources"], **slow["sources"], **crm["sources"]}
    tier_as_of = {"fast": fast.get("computed_at"), "slow": slow.get("computed_at"), "crm": crm.get("computed_at")}
    groups = []
    for gid, label in reg.GROUPS:
        rows = [evaluate(r, sources, tier_as_of) for r in reg.ROWS if r.group == gid]
        groups.append({"id": gid, "label": label, "note": reg.GROUP_NOTES.get(gid), "rows": rows})
    all_rows = [r for grp in groups for r in grp["rows"]]
    atoms = sources.get("atoms") or {}
    live = atoms.get("values", {}).get("live") if atoms.get("status") == "ok" else None
    ms = round((time.monotonic() - started) * 1000)
    cached = bool(fast.get("_cached") and slow.get("_cached"))
    stale = bool(fast.get("_stale") or slow.get("_stale") or crm.get("_stale"))
    logger.info("data_coverage_built", ms=ms, cached_fast=bool(fast.get("_cached")),
                cached_slow=bool(slow.get("_cached")), cached_crm=bool(crm.get("_cached")),
                na=sum(1 for r in all_rows if r["status"] == "na"))
    return {
        "as_of": tier_as_of["fast"] or _now().isoformat(),
        "generated_ms": ms,
        "cached": cached,
        "stale": stale,
        "live_tours": live,
        "summary": {
            "rows": len(all_rows),
            "gaps": sum(1 for r in all_rows if r["status"] == "gap" and r["kind"] == "must"),
            "backlog": sum(1 for r in all_rows if r["status"] == "gap" and r["kind"] == "backlog"),
            "na": sum(1 for r in all_rows if r["status"] == "na"),
        },
        "groups": groups,
        "sources": {k: {"status": v["status"], "reason": v.get("reason"), "timing_ms": int(v.get("timing_ms") or 0),
                        "tier": v.get("tier") or reg.SOURCE_BY_KEY[k].tier, "reads": reg.SOURCE_BY_KEY[k].reads}
                    for k, v in sources.items() if k in reg.SOURCE_BY_KEY},
    }
