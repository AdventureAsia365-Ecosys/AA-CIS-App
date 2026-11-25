"""AA-1154 (A2) — predicted against measured: the action cards a person ran, checked again later, and the hit rate.

Tech lead decision S285 (root docs/audits/2026-11-19-S285-session/advisory-ai-assistants.md sections 3.7, 6 and 8,
phase A2): each executed card records what it predicted against what happened at +10 and +30 minutes, read again by
the worker with no model ($0), and the digest shows the hit rate. Nothing here runs an action (AI5): the person runs
the card's API call (or the runbook of a copy-only step) and then marks the card as done in the Analyse drawer.

- **Measures** (:func:`m`, built by the playbooks with each card): what the check reads again and the direction the
  card predicts: ``zero`` (the value is 0: the wave's job left the queue, the alert closed), ``nonzero`` (it happened:
  a probe finished, the fallback answered), ``down`` (lower than when the card was marked done), ``up``. A card
  without a measure (an AWS Support case: AWS answers when it answers) is recorded and ends ``unclear``.
- **Record** (:func:`record`): the card is rebuilt on the server (never taken from the browser); each measure is read
  once as the baseline; the checkpoints are copied from ``cis.ops.assist_check_after_min`` (a later edit of the
  setting never changes an open run); the open incident of the same item (``acp_shared.ops_incident``, CIS 485) is
  linked. One record per card within the card lifetime (``cis.ops.assist_card_ttl_min``): a second click is refused.
- **Checks** (:func:`check_due`): the 5-minute status sampler (job kind ``ops_status_sample``, the monitoring lane of
  the job runner, AA-1178) reads every due checkpoint of the open runs. A checkpoint taken later than the shortest
  checkpoint after it was due (a worker that was down) is ``unclear`` (the state may have changed for other reasons).
  Each measure: ``hit`` / ``miss`` / ``unclear`` (:func:`judge`); a checkpoint is a miss when any measure missed, a hit
  when at least one hit and none missed, else unclear (:func:`combine`); the run's verdict is its last checkpoint's.
- **Hit rate** (:func:`track`): over ``cis.ops.assist_digest_days`` days, right ``n`` of ``m`` checked runs (unclear
  ones are not counted), per action too; the status page and the OPS-12 digest show it. **Track record**
  (:func:`history`): per action, all time, the cards' "right k of n" confidence (shown from
  ``cis.ops.assist_history_min_n``).

Every read is a DB read of existing sources ($0): ``shared.llm_call_log`` of the evidence window, ``shared.job``,
``shared.rerun_wave``, ``acp_shared.ops_event`` and the newest ``acp_shared.ops_sample`` rows the sampler writes, and
the assistant's own signals (no status page build: its CloudWatch part is not free). The only writes of the assistant
package are here, into ``acp_shared.ops_assist_run`` (``tests/unit/test_aa1154_ops_assist.py``). While CIS 490 is not
applied the table is missing: :class:`NotSetUp` (the API answers 503 "not set up yet"), and the sampler skips it.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Optional

import asyncpg
import structlog

logger = structlog.get_logger()

EXPECTS = ("zero", "nonzero", "down", "up")
VERDICTS = ("hit", "miss", "unclear")
OPEN_ROWS = 50  # technical-constant: open runs read per sampler tick (a few a month on Dev)
RUN_ROWS = 20  # technical-constant: runs listed per answer
MAX_CHECKPOINTS = 10  # technical-constant: the CIS 490 CHECK ops_assist_run_check_after_chk (1..10 checkpoints)
ACCESS_CLASSES = ("access_denied", "credential", "quota_exhausted")   # evidence.ACCESS_CLASSES (kept in step by test)


class NotSetUp(RuntimeError):
    """acp_shared.ops_assist_run is missing (CIS 490 not applied yet)."""


class AlreadyRecorded(RuntimeError):
    """The same card was marked as done within the card lifetime."""


class NoCheckpoint(RuntimeError):
    """``check_after_min`` of the limits is empty (``ops_assist_settings.snapshot`` reads an empty setting as its
    compiled default, so only limits built elsewhere get here): a run needs 1..10 checkpoints (CIS 490)."""


# ── measures (pure) ──────────────────────────────────────────────────────────────────────────────────────────────

def m(metric: str, expect: str, label: str, **params: Any) -> dict:
    """One measure of a card: the metric read again, its parameters, the predicted direction and plain words. Pure."""
    assert metric in METRIC_NAMES, metric
    assert expect in EXPECTS, expect
    return {"metric": metric, "params": params, "expect": expect, "label": label}


def judge(expect: str, baseline: Optional[float], value: Optional[float]) -> str:
    """hit / miss / unclear of one measure: ``value`` read at the checkpoint, ``baseline`` when the card was marked
    done. Nothing read = unclear; ``down`` from 0 = unclear (nothing could fall). Pure."""
    if value is None:
        return "unclear"
    if expect == "zero":
        return "hit" if value == 0 else "miss"
    if expect == "nonzero":
        return "hit" if value > 0 else "miss"
    if baseline is None:
        return "unclear"
    if expect == "down":
        if baseline <= 0:
            return "unclear"
        return "hit" if value < baseline else "miss"
    return "hit" if value > baseline else "miss"


def combine(verdicts: list[str]) -> str:
    """A checkpoint's verdict from its measures': any miss = miss, else any hit = hit, else unclear. Pure."""
    if "miss" in verdicts:
        return "miss"
    return "hit" if "hit" in verdicts else "unclear"


def due_checkpoints(run: dict, now: datetime) -> list[int]:
    """The checkpoints (minutes) of an open run that are due and not taken yet, ascending. Pure."""
    taken = {int(c.get("after_min")) for c in run.get("checks") or [] if c.get("after_min") is not None}
    start = run["recorded_at"]
    return sorted(int(x) for x in run.get("check_after_min") or []
                  if int(x) not in taken and start + timedelta(minutes=int(x)) <= now)


def late(run: dict, after_min: int, now: datetime) -> bool:
    """Is a checkpoint taken later than the shortest checkpoint after it was due (a worker that was down)? Pure."""
    grace = min(int(x) for x in run.get("check_after_min") or [after_min])
    return now > run["recorded_at"] + timedelta(minutes=after_min + grace)


def checkpoint(run: dict, after_min: int, values: dict[int, Optional[float]], now: datetime) -> dict:
    """One checkpoint entry: the values read (by measure index), each measure's verdict and the checkpoint's.
    Pure."""
    measures = run.get("measures") or []
    is_late = late(run, after_min, now)
    per = []
    for i, ms in enumerate(measures):
        v = values.get(i)
        per.append({"value": v, "verdict": "unclear" if is_late else judge(ms["expect"], ms.get("baseline"), v)})
    verdict = "unclear" if is_late or not measures else combine([p["verdict"] for p in per])
    return {"after_min": after_min, "at": now.isoformat(), "measures": per, "verdict": verdict, "late": is_late}


def final_verdict(run: dict, checks: list[dict]) -> Optional[str]:
    """The run's verdict once every checkpoint is taken (the last checkpoint's), else None. Pure."""
    wanted = {int(x) for x in run.get("check_after_min") or []}
    by_min = {int(c["after_min"]): c for c in checks}
    if not wanted or not wanted <= set(by_min):
        return None
    return by_min[max(wanted)]["verdict"]


def summarize(rows: list[dict], titles: Optional[dict[str, str]] = None) -> dict:
    """{recorded, open, checked (hits + misses), hits, unclear, by_action}: the hit rate of some runs. Pure."""
    titles = titles or {}
    out = {"recorded": len(rows), "open": 0, "checked": 0, "hits": 0, "unclear": 0}
    by: dict[str, dict] = {}
    for r in rows:
        v = r.get("verdict")
        a = by.setdefault(r["action"], {"action": r["action"], "title": titles.get(r["action"], r["action"]),
                                        "recorded": 0, "checked": 0, "hits": 0})
        a["recorded"] += 1
        if v is None:
            out["open"] += 1
        elif v == "unclear":
            out["unclear"] += 1
        else:
            out["checked"] += 1
            a["checked"] += 1
            if v == "hit":
                out["hits"] += 1
                a["hits"] += 1
    out["by_action"] = sorted(by.values(), key=lambda a: (-a["recorded"], a["action"]))
    return out


def hit_rate_text(t: dict) -> str:
    """The plain-words line of the status page and the digest. Pure."""
    days = t.get("days")
    if not t.get("recorded"):
        return f"No action card was marked as done in the last {days} days."
    head = f"In the last {days} days {t['recorded']} action card(s) were marked as done"
    if not t.get("checked"):
        return head + "; none has a clear checked outcome yet."
    rest = f"; the assistant's prediction was right {t['hits']} of {t['checked']} checked"
    extra = []
    if t.get("unclear"):
        extra.append(f"{t['unclear']} unclear")
    if t.get("open"):
        extra.append(f"{t['open']} still being checked")
    return head + rest + (f" ({', '.join(extra)})" if extra else "") + "."


# ── metric reads ($0, DB only) ───────────────────────────────────────────────────────────────────────────────────

_CALLS_SQL = """
    SELECT count(*) FILTER (WHERE status = 'refused' AND error_class = ANY($4::text[]))::float8 AS refused,
           count(*) FILTER (WHERE error_class = 'throttled')::float8 AS throttled,
           count(*) FILTER (WHERE coalesce(status, 'ok') = 'ok')::float8 AS answered,
           coalesce(sum(cost_usd), 0)::float8 AS usd
      FROM shared.llm_call_log
     WHERE created_at >= $1 AND created_at < $2 AND connection_key = ANY($3::text[]) AND is_shadow IS NOT TRUE
"""
_APP_SPEND_SQL = """
    SELECT coalesce(sum(cost_usd), 0)::float8 AS usd
      FROM shared.llm_call_log
     WHERE created_at >= $1 AND created_at < $2 AND app = $3 AND is_shadow IS NOT TRUE
"""
_DENIED_SQL = """
    SELECT count(*)::float8 AS n FROM shared.job
     WHERE status = 'failed' AND finished_at >= $1 AND error LIKE 'llm\\_access\\_denied%'
"""
_WAVE_SQL = ("SELECT (paused_at IS NOT NULL OR status <> 'open')::int::float8 AS paused FROM shared.rerun_wave "
             "WHERE id = $1::uuid")
_JOB_SQL = "SELECT (status = 'queued')::int::float8 AS queued FROM shared.job WHERE id = $1::uuid"
_OLDEST_SQL = ("SELECT coalesce(extract(epoch FROM ($1 - min(run_after))) / 60.0, 0)::float8 AS wait_min "
               "FROM shared.job WHERE status = 'queued' AND run_after <= $1")
_PROBE_SQL = ("SELECT count(*)::float8 AS n FROM shared.job WHERE kind = 'llm_connection_probe' "
              "AND status = 'succeeded' AND created_at >= $1")
_EVENT_OPEN_SQL = ("SELECT (ended_at IS NULL)::int::float8 AS open FROM acp_shared.ops_event "
                   "WHERE event_id = $1")
_SAMPLE_SQL = ("SELECT state, value FROM acp_shared.ops_sample WHERE kind = $1 AND subject = $2 "
               "ORDER BY sampled_at DESC LIMIT 1")


async def _val(pool: Any, sql: str, *args: Any) -> Optional[float]:
    v = await pool.fetchval(sql, *args)
    return float(v) if v is not None else None


def _window(ctx: dict) -> tuple[datetime, datetime, int]:
    minutes = int(ctx["limits"]["evidence_window_min"])
    return ctx["now"] - timedelta(minutes=minutes), ctx["now"], minutes


async def _calls(pool: Any, ctx: dict, keys: list[str], field: str) -> Optional[float]:
    if not keys:
        return None
    start, end, minutes = _window(ctx)
    row = await pool.fetchrow(_CALLS_SQL, start, end, list(keys), list(ACCESS_CLASSES))
    if row is None:
        return None
    if field == "usd":
        return round(float(row["usd"]) * 60.0 / max(minutes, 1), 4)
    return float(row[field])


async def _refused(pool: Any, p: dict, ctx: dict) -> Optional[float]:
    return await _calls(pool, ctx, [p.get("connection_key")] if p.get("connection_key") else [], "refused")


async def _throttled(pool: Any, p: dict, ctx: dict) -> Optional[float]:
    return await _calls(pool, ctx, [p.get("connection_key")] if p.get("connection_key") else [], "throttled")


async def _answered(pool: Any, p: dict, ctx: dict) -> Optional[float]:
    return await _calls(pool, ctx, list(p.get("connections") or []), "answered")


async def _spend(pool: Any, p: dict, ctx: dict) -> Optional[float]:
    return await _calls(pool, ctx, list(p.get("connections") or []), "usd")


async def _app_spend(pool: Any, p: dict, ctx: dict) -> Optional[float]:
    start, end, minutes = _window(ctx)
    usd = await _val(pool, _APP_SPEND_SQL, start, end, p.get("app") or "cis")
    return None if usd is None else round(usd * 60.0 / max(minutes, 1), 4)


async def _denied(pool: Any, p: dict, ctx: dict) -> Optional[float]:
    return await _val(pool, _DENIED_SQL, _window(ctx)[0])


async def _wave_paused(pool: Any, p: dict, ctx: dict) -> Optional[float]:
    return await _val(pool, _WAVE_SQL, p["wave_id"]) if p.get("wave_id") else None


async def _job_queued(pool: Any, p: dict, ctx: dict) -> Optional[float]:
    return await _val(pool, _JOB_SQL, p["job_id"]) if p.get("job_id") else None


async def _oldest_wait(pool: Any, p: dict, ctx: dict) -> Optional[float]:
    v = await _val(pool, _OLDEST_SQL, ctx["now"])
    return None if v is None else round(v, 1)


async def _probe_finished(pool: Any, p: dict, ctx: dict) -> Optional[float]:
    return await _val(pool, _PROBE_SQL, ctx["since"])


async def _sample(pool: Any, kind: str, subject: str) -> Optional[dict]:
    row = await pool.fetchrow(_SAMPLE_SQL, kind, subject)
    if row is None:
        return None
    value = row["value"]
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            value = {}
    return {"state": row["state"], "value": value if isinstance(value, dict) else {}}


async def _live_workers(pool: Any, p: dict, ctx: dict) -> Optional[float]:
    s = await _sample(pool, "queue", "shared.job")
    v = (s or {}).get("value", {}).get("live_workers")
    return float(v) if isinstance(v, (int, float)) else None


async def _signal_open(pool: Any, item_id: str, ctx: dict) -> Optional[float]:
    from api.services.ops_assist import evidence, playbooks

    memo = ctx.setdefault("memo", {})
    if "signals" not in memo:
        snap = await evidence.collect(pool, None, item_id="", limits=ctx["limits"], now=ctx["now"],
                                      include_status=False)
        failed = {"connections", "calls", "spend_baseline"} & set(snap.get("unavailable") or {})
        memo["signals"] = None if failed else {s["id"] for s in playbooks.signals(snap, ctx["limits"])}
    ids = memo["signals"]
    return None if ids is None else float(item_id in ids)


def item_source(item_id: str) -> Optional[tuple[str, str]]:
    """(source, key) the openness of a Needs-attention item or signal can be read from without the status page build
    (no CloudWatch call), else None. Pure."""
    for prefix in ("alarm-e", "rollout-e"):
        if item_id.startswith(prefix) and item_id[len(prefix):].isdigit():
            return "event", item_id[len(prefix):]
    for prefix, kind in (("svc-", "ecs_service"), ("health-", "app_health")):
        if item_id.startswith(prefix):
            return kind, item_id[len(prefix):]
    if item_id == "queue-shared":
        return "queue", "shared.job"
    if item_id.startswith(("llm-conn-", "llm-throttle-", "llm-spend-")):
        return "signal", item_id
    return None


async def _item_open(pool: Any, p: dict, ctx: dict) -> Optional[float]:
    src = item_source(str(p.get("item_id") or ""))
    if src is None:
        return None
    kind, key = src
    if kind == "event":
        return await _val(pool, _EVENT_OPEN_SQL, int(key))
    if kind == "signal":
        return await _signal_open(pool, key, ctx)
    s = await _sample(pool, kind, key)
    if s is None or s["state"] == "unknown":
        return None
    if kind == "queue":
        return float(s["state"] != "ok")
    return float(s["state"] in ("down", "degraded"))


Reader = Callable[[Any, dict, dict], Awaitable[Optional[float]]]
METRICS: dict[str, Reader] = {
    "item_open": _item_open, "refused_calls": _refused, "throttled_calls": _throttled,
    "answered_calls": _answered, "spend_per_hour": _spend, "app_spend_per_hour": _app_spend,
    "denied_jobs": _denied, "wave_paused": _wave_paused, "job_queued": _job_queued,
    "oldest_wait_min": _oldest_wait, "probe_finished": _probe_finished, "live_workers": _live_workers,
}
METRIC_NAMES = frozenset(METRICS)


async def read(pool: Any, measure: dict, ctx: dict) -> Optional[float]:
    """One measure's value now; None when it cannot be read (the failure is logged, the check says unclear)."""
    try:
        return await METRICS[measure["metric"]](pool, measure.get("params") or {}, ctx)
    except Exception as e:  # noqa: BLE001 — an unreadable source is "unclear", never a guess
        logger.warning("ops_assist_measure_failed", metric=measure.get("metric"), error=repr(e)[:200])
        return None


# ── the record ───────────────────────────────────────────────────────────────────────────────────────────────────

_RECENT_SQL = ("SELECT run_id FROM acp_shared.ops_assist_run WHERE card_id = $1 AND recorded_at > $2 "
               "ORDER BY recorded_at DESC LIMIT 1")
_INCIDENT_SQL = ("SELECT incident_id FROM acp_shared.ops_incident WHERE item_id = $1 AND status <> 'resolved' "
                 "ORDER BY declared_at DESC LIMIT 1")
_INSERT_SQL = """
    INSERT INTO acp_shared.ops_assist_run (item_id, card_id, action, playbook, risk_level, range_key, incident_id,
                                           predicted, measures, check_after_min, recorded_by, recorded_at)
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9::jsonb, $10::int[], $11, $12)
    RETURNING run_id
"""
_RUN_COLS = ("run_id, item_id, card_id, action, playbook, risk_level, range_key, incident_id, predicted, measures, "
             "check_after_min, checks, verdict, checked_at, recorded_by, recorded_at")
_OPEN_SQL = (f"SELECT {_RUN_COLS} FROM acp_shared.ops_assist_run WHERE verdict IS NULL "
             f"ORDER BY recorded_at LIMIT {OPEN_ROWS}")
_UPDATE_SQL = """
    UPDATE acp_shared.ops_assist_run SET checks = $2::jsonb, verdict = $3, checked_at = $4
     WHERE run_id = $1 AND verdict IS NULL
    RETURNING run_id
"""
_LIST_SQL = (f"SELECT {_RUN_COLS} FROM acp_shared.ops_assist_run WHERE ($1::text IS NULL OR item_id = $1) "
             f"ORDER BY recorded_at DESC LIMIT {RUN_ROWS}")
_TRACK_SQL = "SELECT action, verdict FROM acp_shared.ops_assist_run WHERE recorded_at >= $1"
_HISTORY_SQL = ("SELECT action, count(*) FILTER (WHERE verdict IN ('hit', 'miss')) AS n, "
                "count(*) FILTER (WHERE verdict = 'hit') AS hits FROM acp_shared.ops_assist_run GROUP BY action")


def _jsonb(v: Any) -> Any:
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return None
    return v


def run_view(row: dict) -> dict:
    """A run as the API serves it (times ISO, jsonb parsed). Pure."""
    out = dict(row)
    for k in ("predicted", "measures", "checks"):
        out[k] = _jsonb(out.get(k)) or []
    for k in ("recorded_at", "checked_at"):
        out[k] = out[k].isoformat() if isinstance(out.get(k), datetime) else out.get(k)
    out["check_after_min"] = [int(x) for x in out.get("check_after_min") or []]
    return out


def _run_row(row: Any) -> dict:
    r = dict(row)
    for k in ("predicted", "measures", "checks"):
        r[k] = _jsonb(r.get(k)) or []
    return r


async def record(pool: Any, answer: dict, card: dict, *, actor: str, limits: dict,
                 now: Optional[datetime] = None) -> dict:
    """Mark ``card`` of ``answer`` (both rebuilt on the server) as done: its baselines read now, the checkpoints of the
    setting, the open incident of the item. Raises :class:`AlreadyRecorded` within the card lifetime, :class:`NotSetUp`
    while the table is missing, :class:`NoCheckpoint` when the limits name no checkpoint."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    checkpoints = sorted({int(x) for x in limits.get("check_after_min") or []})
    if not 1 <= len(checkpoints) <= MAX_CHECKPOINTS:
        raise NoCheckpoint(card["id"])
    ctx = {"now": now, "since": now, "limits": limits}
    measures = [{**ms, "baseline": await read(pool, ms, ctx)} for ms in card.get("measures") or []]
    predicted = [{k: p.get(k) for k in ("text", "confidence", "basis")} for p in card.get("predictions") or []]
    try:
        if await pool.fetchval(_RECENT_SQL, card["id"], now - timedelta(minutes=int(limits["card_ttl_min"]))):
            raise AlreadyRecorded(card["id"])
        incident = await pool.fetchval(_INCIDENT_SQL, answer.get("item_id"))
        run_id = await pool.fetchval(
            _INSERT_SQL, answer.get("item_id"), card["id"], card["action"], answer.get("playbook"),
            (card.get("risk") or {}).get("level"), answer.get("range") or "24h", incident,
            json.dumps(predicted, default=str), json.dumps(measures, default=str),
            checkpoints, actor, now)
    except asyncpg.UndefinedTableError as e:
        raise NotSetUp("acp_shared.ops_assist_run") from e
    logger.info("ops_assist_run_recorded", run_id=run_id, card_id=card["id"], action=card["action"],
                measures=len(measures))
    return {"run_id": run_id, "item_id": answer.get("item_id"), "card_id": card["id"], "action": card["action"],
            "incident_id": incident, "measures": measures, "check_after_min": checkpoints,
            "recorded_at": now.isoformat(), "recorded_by": actor, "verdict": None, "checks": []}


async def check_due(pool: Any, *, limits: Optional[dict] = None, now: Optional[datetime] = None) -> dict:
    """Take every due checkpoint of the open runs (the status sampler's part). {checked, finished}; never raises for
    one run (its failure is logged, the next tick retries)."""
    from api.services import ops_assist

    lim = limits or ops_assist.limits()
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    try:
        rows = [_run_row(r) for r in await pool.fetch(_OPEN_SQL)]
    except asyncpg.UndefinedTableError:
        return {"checked": 0, "finished": 0, "not_set_up": True}
    ctx: dict = {"now": now, "limits": lim, "memo": {}}
    checked = finished = 0
    for run in rows:
        due = due_checkpoints(run, now)
        if not due:
            continue
        try:
            checks = list(run.get("checks") or [])
            values = {i: await read(pool, ms, {**ctx, "since": run["recorded_at"]})
                      for i, ms in enumerate(run.get("measures") or [])}
            for after in due:
                checks.append(checkpoint(run, after, values, now))
            verdict = final_verdict(run, checks)
            await pool.fetchval(_UPDATE_SQL, run["run_id"], json.dumps(checks, default=str), verdict,
                                now if verdict else None)
            checked += len(due)
            finished += 1 if verdict else 0
        except Exception as e:  # noqa: BLE001 — one run never stops the others
            logger.warning("ops_assist_check_failed", run_id=run.get("run_id"), error=repr(e)[:200])
    if checked:
        logger.info("ops_assist_checked", checkpoints=checked, finished=finished)
    return {"checked": checked, "finished": finished}


async def runs(pool: Any, item_id: Optional[str] = None) -> list[dict]:
    """The newest runs (of one item, or all). Raises :class:`NotSetUp` while the table is missing."""
    try:
        return [run_view(_run_row(r)) for r in await pool.fetch(_LIST_SQL, item_id)]
    except asyncpg.UndefinedTableError as e:
        raise NotSetUp("acp_shared.ops_assist_run") from e


async def track(pool: Any, *, days: int, now: Optional[datetime] = None) -> dict:
    """The hit rate of the last ``days`` days (see :func:`summarize`) with its line. Raises :class:`NotSetUp`."""
    from api.services.ops_assist import catalog

    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    since = now - timedelta(days=int(days))
    try:
        rows = [dict(r) for r in await pool.fetch(_TRACK_SQL, since)]
    except asyncpg.UndefinedTableError as e:
        raise NotSetUp("acp_shared.ops_assist_run") from e
    out = {"days": int(days), "since": since.isoformat(),
           **summarize(rows, {a.id: a.title for a in catalog.ACTIONS})}
    out["text"] = hit_rate_text(out)
    return out


async def history(pool: Any) -> dict[str, dict]:
    """{action: {n, hits}} of every checked run (all time): each card's track record. Never raises: {} while the table
    is missing or unreadable (the cards then say "not enough data", the safe side)."""
    try:
        return {r["action"]: {"n": int(r["n"]), "hits": int(r["hits"])} for r in await pool.fetch(_HISTORY_SQL)}
    except asyncpg.UndefinedTableError:
        return {}
    except Exception as e:  # noqa: BLE001 — a track record is never a reason to fail the cards
        logger.warning("ops_assist_history_failed", error=repr(e)[:200])
        return {}


__all__ = ["AlreadyRecorded", "METRICS", "MAX_CHECKPOINTS", "NoCheckpoint", "NotSetUp", "check_due", "checkpoint", "combine", "final_verdict",
           "hit_rate_text", "history", "item_source", "judge", "m", "record", "run_view", "runs", "summarize",
           "track"]
