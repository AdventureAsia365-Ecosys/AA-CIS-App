"""AA-1140 — reads of the sampler tables (CIS 475) for the Ecosystem status page: the runtimes grid with its uptime
strip, the queue and database series, the event timeline. Small reads on indexed time ranges; the pure helpers
(cells, uptime, series) are unit-tested.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from api.services.ops_status.ranges import TimeRange
from shared import ops_status_thresholds as th

# Uptime cell lengths in minutes: the first that gives at most MAX_CELLS cells over the range (mockup v2).
CELL_MINUTES = (5, 10, 15, 30, 60, 180, 360, 720, 1440)
MAX_CELLS = 48  # technical-constant: the strip's drawing width (cells), not a policy value
SAMPLE_MINUTES = 5
UP, DOWN = ("ok", "warn"), ("degraded", "down")
_RANK = {"unknown": -1, "ok": 0, "warn": 1, "degraded": 2, "down": 3}

SAMPLES_SQL = """
    SELECT sampled_at, kind, subject, app, state, value
      FROM acp_shared.ops_sample
     WHERE kind = ANY($1::text[]) AND sampled_at >= $2 AND sampled_at < $3
     ORDER BY sampled_at
"""
LATEST_SQL = """
    SELECT DISTINCT ON (kind, subject) sampled_at, kind, subject, app, state, value
      FROM acp_shared.ops_sample
     WHERE kind = ANY($1::text[]) AND sampled_at >= $2
     ORDER BY kind, subject, sampled_at DESC
"""
EVENTS_SQL = """
    SELECT event_id, occurred_at, ended_at, kind, severity, app, subject, title, detail, details
      FROM acp_shared.ops_event
     WHERE occurred_at < $2 AND coalesce(ended_at, now()) >= $1
     ORDER BY occurred_at DESC
     LIMIT 500
"""


def _json(v: Any) -> dict:
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            return {}
    return v if isinstance(v, dict) else {}


def cell_minutes(rng: TimeRange) -> int:
    span_min = rng.span.total_seconds() / 60.0
    return next((c for c in CELL_MINUTES if span_min / c <= MAX_CELLS), CELL_MINUTES[-1])


def strip(samples: Iterable[dict], rng: TimeRange, slo_pct: float) -> dict:
    """Uptime cells (worst state of each cell, ``none`` without a sample) and uptime against the objective for one
    runtime's samples [{sampled_at, state}]. ok / warn = up, degraded / down = down, unknown = not measured. Pure."""
    cm = cell_minutes(rng)
    n = max(1, int(-(-rng.span.total_seconds() // (cm * 60))))
    cells = ["none"] * n
    good = bad = 0
    for s in samples:
        t = s["sampled_at"]
        i = int((t - rng.start).total_seconds() // (cm * 60))
        if not 0 <= i < n:
            continue
        st = s["state"]
        if st in UP:
            good += 1
        elif st in DOWN:
            bad += 1
        else:
            continue
        if cells[i] == "none" or _RANK[st] > _RANK[cells[i]]:
            cells[i] = st
    measured = good + bad
    up = round(100.0 * good / measured, 3) if measured else None
    allowed = measured * SAMPLE_MINUTES * (100.0 - slo_pct) / 100.0
    budget_left = (round(100.0 * (allowed - bad * SAMPLE_MINUTES) / allowed, 1) if allowed > 0 else
                   (100.0 if measured and not bad else None))
    return {"cell_minutes": cm, "cells": cells, "uptime_pct": up, "measured_min": measured * SAMPLE_MINUTES,
            "down_min": bad * SAMPLE_MINUTES, "budget_left_pct": budget_left,
            "below_slo": up is not None and up < slo_pct}


def step_series(samples: Iterable[dict], rng: TimeRange, field: str, *, agg: str = "max",
                scale: float = 1.0) -> list[Optional[float]]:
    """One numeric field of the samples laid on the range's chart steps (max / min / last per step). Pure."""
    n = len(rng.buckets())
    out: list[Optional[float]] = [None] * n
    for s in samples:
        v = _json(s["value"]).get(field)
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            continue
        i = int((s["sampled_at"] - rng.start).total_seconds() // rng.step_s)
        if not 0 <= i < n:
            continue
        v = float(v) * scale
        prev = out[i]
        out[i] = v if prev is None or agg == "last" else (max(prev, v) if agg == "max" else min(prev, v))
    return [None if v is None else round(v, 3) for v in out]


async def samples(pool: Any, kinds: list[str], rng: TimeRange) -> list[dict]:
    rows = await pool.fetch(SAMPLES_SQL, kinds, rng.start, rng.end)
    return [{**dict(r), "value": _json(r["value"])} for r in rows]


async def latest(pool: Any, kinds: list[str], since: datetime) -> list[dict]:
    rows = await pool.fetch(LATEST_SQL, kinds, since)
    return [{**dict(r), "value": _json(r["value"])} for r in rows]


async def events(pool: Any, rng: TimeRange) -> list[dict]:
    rows = await pool.fetch(EVENTS_SQL, rng.start, rng.end)
    return [{**dict(r), "details": _json(r["details"])} for r in rows]


def fresh_since(now: datetime) -> datetime:
    """Samples older than this are not "now": ``cis.ops.status_stale_factor`` x the sampler's 5-minute tick (the
    page's one freshness setting, used by every section that reads the latest samples)."""
    return now.astimezone(timezone.utc) - timedelta(minutes=SAMPLE_MINUTES * th.stale_factor())
