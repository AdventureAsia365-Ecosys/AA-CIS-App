"""AA-1154 (A1) — "Analyse": the rule-made answer of one item plus the model's ranking and explanation, cached for the
card lifetime; and the automatic analysis of a SEV1 / SEV2 item, once per incident (tech lead AI6).

- **Analyse** (:func:`analyse`): the A0 answer (``ops_assist.analyse``: evidence, cards, Summary), then
  ``explain.explain`` (one gateway call of the ``ops_assist`` stage) unless the same item, range and depth were
  analysed within the card lifetime (``cis.ops.assist_card_ttl_min``): that model part is reused (Redis), applied to
  the cards just rebuilt (cards keep their ids, so an explanation follows its card; a card that is gone loses its
  explanation). ``fresh`` asks again. The business owner gets the Summary and the plain-words paragraph only (AI10).
- **Auto** (:func:`auto`): the status page calls it while it is open. For each open Needs-attention item and each
  assistant signal whose severity is in ``cis.ops.assist_auto_severities`` (AI6: SEV1, SEV2; empty = none), one quick
  analysis per incident: a Redis marker per item id is set with NX, so the first caller wins and nobody pays twice.
  The marker is keyed on the item id alone, never on ``since``: for the health and ECS items ``since`` is the newest
  sample's time and moves on every sampler tick, so a marker on it would be new on every call (one paid call per
  item every card lifetime). The marker lives ``cis.ops.assist_auto_incident_gap_min`` (never shorter than the card
  lifetime) and is renewed every time the item is still open, so only an item that stayed away (or unwatched) that
  long counts as a new incident. Without Redis nothing runs automatically (fail closed: no marker, no guarantee of
  "once"). Everything else waits for a click.

Nothing here executes an action (AI5): the answer names the API a person would call; the buttons are the clicker's.
"""
from __future__ import annotations

import json
from typing import Any, Optional

import structlog

from api.services import ops_assist
from api.services.ops_assist import evidence, explain, playbooks

logger = structlog.get_logger()

CACHE_PREFIX = "ops_assist:llm:"
AUTO_PREFIX = "ops_assist:auto:"


def cache_key(item_id: str, range_key: str, depth: str) -> str:
    return f"{CACHE_PREFIX}{range_key}:{depth}:{item_id}"


def auto_key(item: dict) -> str:
    """One marker per item id (see the module docstring: never the moving ``since``). Pure."""
    return f"{AUTO_PREFIX}{item['id']}"


def auto_severities(limits: dict) -> frozenset:
    """The severities analysed automatically (``cis.ops.assist_auto_severities``; empty = none). Pure."""
    return frozenset(str(x) for x in limits.get("auto_severities") or [])


def marker_ttl_s(limits: dict) -> int:
    """How long the "already analysed" marker lives without renewal: the incident gap, never below the card lifetime.
    Pure."""
    return max(int(limits["auto_incident_gap_min"]), int(limits["card_ttl_min"])) * 60


def auto_targets(snapshot: dict, limits: dict) -> list[dict]:
    """The open Needs-attention items and assistant signals of a snapshot whose severity is analysed automatically
    (acked or snoozed items wait for a click). Pure."""
    sev = auto_severities(limits)
    if not sev:
        return []
    items = [i for i in ((snapshot.get("status") or {}).get("attention") or [])
             if i.get("severity") in sev and (i.get("status") or "open") == "open"]
    seen = {i["id"] for i in items}
    sigs = [{"id": s["id"], "severity": s["severity"], "since": None} for s in playbooks.signals(snapshot, limits)
            if s.get("severity") in sev and s["id"] not in seen]
    return [{"id": i["id"], "severity": i["severity"], "since": i.get("since")} for i in items] + sigs


async def _get(redis: Any, key: str) -> Optional[dict]:
    if redis is None:
        return None
    try:
        raw = await redis.get(key)
        return json.loads(raw) if raw else None
    except Exception as e:  # noqa: BLE001 — a cache miss, never an error
        logger.warning("ops_assist_cache_read_failed", error=repr(e)[:200])
        return None


async def _put(redis: Any, key: str, value: dict, ttl_s: int) -> None:
    if redis is None:
        return
    try:
        await redis.set(key, json.dumps(value, default=str), ex=ttl_s)
    except Exception as e:  # noqa: BLE001 — the answer is still returned
        logger.warning("ops_assist_cache_write_failed", error=repr(e)[:200])


async def analyse(pool: Any, redis: Any, item_id: str, *, range_key: str = "24h", depth: str = "quick",
                  fresh: bool = False, authorization: Optional[str] = None, business_owner: bool = False,
                  llm: Any = None) -> dict:
    """The rule-made answer with the model's part (new, cached or none, see the module docstring)."""
    base = await ops_assist.analyse(pool, redis, item_id, range_key=range_key, authorization=authorization)
    if base.get("item") is None:
        return base
    ttl_s = int(base["limits"]["card_ttl_min"]) * 60
    key = cache_key(item_id, range_key, depth)
    hit = None if fresh else await _get(redis, key)
    if hit and hit.get("part") is not None:
        out = explain.merge(base, hit["part"], {**hit["meta"], "cached": True})
    else:
        part, meta = await explain.explain(pool, base, depth=depth, llm=llm,
                                           deep_model=(base.get("limits") or {}).get("deep_model"))
        if part is not None:
            await _put(redis, key, {"part": part, "meta": meta}, ttl_s)
        out = explain.merge(base, part, meta)
    logger.info("ops_assist_analysed", item_id=item_id, depth=depth, used=out["llm"].get("used"),
                cached=out["llm"].get("cached"), error=out["llm"].get("error"), cost_usd=out["llm"].get("cost_usd"))
    return explain.owner_view(out) if business_owner else out


async def auto(pool: Any, redis: Any, *, range_key: str = "24h", authorization: Optional[str] = None,
               llm: Any = None) -> dict:
    """One quick analysis per incident of an automatic severity not analysed yet; {analysed, already, failed,
    enabled, severities}."""
    lim = ops_assist.limits()
    sev = sorted(auto_severities(lim))
    if redis is None:
        return {"enabled": False, "reason": "no_redis", "severities": sev, "analysed": [], "already": [], "failed": []}
    if not sev:
        return {"enabled": False, "reason": "no_severities", "severities": sev, "analysed": [], "already": [],
                "failed": []}
    ttl_s = marker_ttl_s(lim)
    snap = await evidence.collect(pool, redis, item_id="", limits=lim, range_key=range_key,
                                  authorization=authorization)
    out: dict = {"enabled": True, "severities": sev, "analysed": [], "already": [], "failed": []}
    for t in auto_targets(snap, lim):
        k = auto_key(t)
        try:
            first = await redis.set(k, "1", ex=ttl_s, nx=True)
            if not first:
                await redis.expire(k, ttl_s)        # still open: the same incident
                out["already"].append(t["id"])
                continue
        except Exception as e:  # noqa: BLE001 — no marker, no automatic call
            logger.warning("ops_assist_auto_marker_failed", error=repr(e)[:200])
            out["failed"].append(t["id"])
            continue
        res = await analyse(pool, redis, t["id"], range_key=range_key, authorization=authorization, llm=llm)
        (out["analysed"] if (res.get("llm") or {}).get("used") else out["failed"]).append(t["id"])
    return out


__all__ = ["analyse", "auto", "auto_key", "auto_severities", "auto_targets", "cache_key", "marker_ttl_s"]
