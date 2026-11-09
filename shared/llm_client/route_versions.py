"""AA-1042 (S268, route control center) — route config versions (CIS migration 370), the write side.

A route config is what the admin route control edits and one click restores:

    {"order_mode": "model_first" | "aws_first",
     "routes": {stage: {"model_id": key, "fallback_model_ids": [key, ...]}},     # shared.llm_role_config
     "priorities": {model_key: {connection_key: priority}}}                      # shared.llm_model_deployment

Every change of it is a row of ``shared.llm_route_config_version`` (who, when, action, note, the snapshot after the
change and the diff against the version before), and ``shared.llm_route_settings.current_version`` names the one in
force (the ledger trigger stamps it on every CIS row of ``shared.llm_call_log``).

Writers call, inside THEIR transaction (a nested ``conn.transaction()`` is a savepoint; outside a transaction the
settings lock would be released at once):

    state = await begin(conn)          # lock the settings row; record an 'external' version first when the live rows
                                       # differ from the version in force (a migration changed them); None before 370
    ... UPDATE the routes / priorities / order mode ...
    await finish(conn, state, actor=..., action="save", note=...)   # snapshot, diff, version + 1; None when nothing
                                                                     # changed

Used by the route control service (``api/services/llm_route_control.py``) and by the Routing tab's PATCH writers
(``role_config.set_stage_config`` / ``set_stage_route``), so every route change is a version whatever made it.
``diff`` and ``normalize`` are pure.
"""
from __future__ import annotations

import json
from typing import Any, Optional

import asyncpg
import structlog

logger = structlog.get_logger()

ACTIONS = ("baseline", "save", "preset", "revert", "external")
EXTERNAL_ACTOR = "external"
NOTE_MAX = 500

_LOCK_SQL = "SELECT order_mode, current_version FROM shared.llm_route_settings WHERE singleton FOR UPDATE"
_ROUTES_SQL = "SELECT stage, model_id, fallback_model_ids FROM shared.llm_role_config ORDER BY stage"
_PRIORITIES_SQL = "SELECT model_key, connection_key, priority FROM shared.llm_model_deployment"
_SNAPSHOT_SQL = "SELECT snapshot FROM shared.llm_route_config_version WHERE version = $1"
_INSERT_SQL = """
    INSERT INTO shared.llm_route_config_version
        (version, created_by, action, preset, reverted_from, note, snapshot, diff)
    VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8::jsonb)
    RETURNING version, created_at, created_by, action, preset, reverted_from, note
"""
_SETTINGS_SQL = ("UPDATE shared.llm_route_settings SET current_version = $1, updated_at = now(), updated_by = $2 "
                 "WHERE singleton")


def _loads(value: Any) -> Any:
    return json.loads(value) if isinstance(value, (str, bytes)) else value


def normalize(snapshot: Optional[dict]) -> dict:
    """The canonical form of a snapshot (pure): lists as lists, priorities as ints, missing parts empty."""
    snap = _loads(snapshot) or {}
    routes = {}
    for stage, r in (snap.get("routes") or {}).items():
        routes[stage] = {"model_id": r.get("model_id"), "fallback_model_ids": list(r.get("fallback_model_ids") or [])}
    priorities = {m: {c: int(p) for c, p in (conns or {}).items()}
                  for m, conns in (snap.get("priorities") or {}).items()}
    return {"order_mode": snap.get("order_mode"), "routes": routes, "priorities": priorities}


def chain_of(route: Optional[dict]) -> Optional[list]:
    """[primary, *fallbacks] of a snapshot route (None for a stage the snapshot does not have)."""
    if not route:
        return None
    return [k for k in [route.get("model_id"), *(route.get("fallback_model_ids") or [])] if k]


def provider_order(conns: Optional[dict]) -> list:
    """The connections of one model in the order the gateway tries them (priority, then connection key)."""
    return [c for c, _ in sorted((conns or {}).items(), key=lambda kv: (int(kv[1]), kv[0]))]


def diff(before: Optional[dict], after: Optional[dict]) -> list[dict]:
    """The changes from ``before`` to ``after`` (pure), in a stable order: the order mode, then each stage whose chain
    differs, then each model whose provider ORDER differs (renumbered priorities with the same order are no change)."""
    b, a = normalize(before), normalize(after)
    out: list[dict] = []
    if b["order_mode"] != a["order_mode"]:
        out.append({"kind": "order_mode", "before": b["order_mode"], "after": a["order_mode"]})
    for stage in sorted(set(b["routes"]) | set(a["routes"])):
        cb, ca = chain_of(b["routes"].get(stage)), chain_of(a["routes"].get(stage))
        if cb != ca:
            out.append({"kind": "route", "stage": stage, "before": cb, "after": ca})
    for model in sorted(set(b["priorities"]) | set(a["priorities"])):
        pb, pa = b["priorities"].get(model), a["priorities"].get(model)
        ob = provider_order(pb) if pb is not None else None
        oa = provider_order(pa) if pa is not None else None
        if ob != oa:
            out.append({"kind": "provider_order", "model": model, "before": ob, "after": oa})
    return out


async def read_live(conn, order_mode: Optional[str] = None) -> dict:
    """The live route config (the rows as they are now, in this transaction)."""
    if order_mode is None:
        row = await conn.fetchrow("SELECT order_mode FROM shared.llm_route_settings WHERE singleton")
        order_mode = row["order_mode"] if row is not None else None
    routes = {r["stage"]: {"model_id": r["model_id"], "fallback_model_ids": list(r["fallback_model_ids"] or [])}
              for r in await conn.fetch(_ROUTES_SQL)}
    priorities: dict[str, dict[str, int]] = {}
    for r in await conn.fetch(_PRIORITIES_SQL):
        priorities.setdefault(r["model_key"], {})[r["connection_key"]] = int(r["priority"])
    return {"order_mode": order_mode, "routes": routes, "priorities": priorities}


async def read_snapshot(conn, version: int) -> Optional[dict]:
    row = await conn.fetchrow(_SNAPSHOT_SQL, version)
    return normalize(row["snapshot"]) if row is not None else None


async def _insert(conn, *, version: int, actor: str, action: str, snapshot: dict, changes: list,
                  note: Optional[str] = None, preset: Optional[str] = None,
                  reverted_from: Optional[int] = None) -> dict:
    note = ((note or "").strip()[:NOTE_MAX]) or None
    row = await conn.fetchrow(_INSERT_SQL, version, actor[:128], action, preset, reverted_from, note,
                              json.dumps(snapshot), json.dumps(changes))
    await conn.execute(_SETTINGS_SQL, version, actor[:128])
    return dict(row)


async def begin(conn) -> Optional[dict]:
    """Lock the settings row (inside the caller's transaction) and bring the history up to date: when the live rows
    differ from the version in force, an ``external`` version records them first (a change the app did not make).
    Returns ``{"order_mode", "current_version", "before_external"}`` (``before_external``: the version the caller's
    preview saw), or None when migration 370 is not applied (the caller writes no version)."""
    try:
        async with conn.transaction():          # savepoint: a missing table must not abort the caller's transaction
            row = await conn.fetchrow(_LOCK_SQL)
    except (asyncpg.exceptions.UndefinedTableError, asyncpg.exceptions.UndefinedColumnError):
        return None
    if row is None:
        return None
    state = {"order_mode": row["order_mode"], "current_version": int(row["current_version"]),
             "before_external": int(row["current_version"])}
    previous = await read_snapshot(conn, state["current_version"])
    live = await read_live(conn, state["order_mode"])
    changes = diff(previous, live)
    if changes:
        version = state["current_version"] + 1
        await _insert(conn, version=version, actor=EXTERNAL_ACTOR, action="external", snapshot=live, changes=changes,
                      note="Changed outside the admin (e.g. a migration); recorded before the next admin change.")
        state["current_version"] = version
        logger.info("llm_route_config_external_change", version=version, changes=len(changes))
    return state


async def finish(conn, state: Optional[dict], *, actor: str, action: str = "save", note: Optional[str] = None,
                 preset: Optional[str] = None, reverted_from: Optional[int] = None) -> Optional[dict]:
    """Record the caller's change as the next version (snapshot after it, diff against the version in force). None
    when ``state`` is None (no 370) or nothing changed."""
    if state is None:
        return None
    assert action in ACTIONS and action not in ("baseline", "external"), action
    previous = await read_snapshot(conn, state["current_version"])
    live = await read_live(conn)
    changes = diff(previous, live)
    if not changes:
        return None
    version = state["current_version"] + 1
    out = await _insert(conn, version=version, actor=actor, action=action, snapshot=live, changes=changes, note=note,
                        preset=preset, reverted_from=reverted_from)
    state["current_version"] = version
    out["diff"] = changes
    return out


async def record_safe(conn, state: Optional[dict], **kwargs) -> Optional[dict]:
    """``finish`` for the Routing tab's PATCH writers: a failure is logged and never fails their change (savepoint)."""
    if state is None:
        return None
    try:
        async with conn.transaction():
            return await finish(conn, state, **kwargs)
    except Exception as e:  # noqa: BLE001 — the route change itself is audited in acp_shared.audit_log either way
        logger.warning("llm_route_version_write_failed", error=str(e)[:300])
        return None


async def begin_safe(conn) -> Optional[dict]:
    """``begin`` that never fails the caller's change (savepoint; None on any failure)."""
    try:
        async with conn.transaction():
            return await begin(conn)
    except Exception as e:  # noqa: BLE001
        logger.warning("llm_route_version_begin_failed", error=str(e)[:300])
        return None


__all__ = ["ACTIONS", "begin", "begin_safe", "chain_of", "diff", "finish", "normalize", "provider_order",
           "read_live", "read_snapshot", "record_safe"]
