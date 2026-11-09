"""AA-1056 — why did a tour on/off event wait for its recompute? READ ONLY.

Runs in the CIS api container (python3 + asyncpg, DATABASE_URL = cis_app, ssl=require), through
tools/ecs/ecsx.sh in the root repo:

    tools/ecs/ecsx.sh apps/AA-CIS-App/scripts/aa1056/recompute_lag_diag.py SINCE=2026-11-05T00:00:00+00:00 \
        UNTIL=2026-11-06T00:00:00+00:00

For every `retired` / `paused` / `restored` event in [SINCE, UNTIL) it prints:
  * whether the after-commit effects ran: the `tripplanner_purge` and `tour_retirement_fanout` jobs keyed on the event
    and their `payload.trigger` (`after_commit` = run_after_commit ran in the request; `sweep` = the ops sampler made
    it later, so the request's effects never ran);
  * the recompute rows created from 2 minutes before to 30 minutes after the event (created_by, reason kind, status,
    run_after offset, coalesced count), the recompute rows queued or running at the event (a fold target), and the
    first succeeded recompute that STARTED after the event (the S273 lag measure);
  * whether a job worker was alive at the event;
  * one verdict line.
Only counts, offsets, statuses, fixed labels and 8-character id prefixes are printed: no names, texts or errors.
Every query is isolated: a failure prints `ERR <section> <ExceptionName>` and the script goes on.

Tables / columns: AA-Ecosys docs/architecture/db-schema-reference.md (verified 2026-11-08, CIS 390).
"""
import asyncio
import os
from datetime import datetime, timedelta, timezone

import asyncpg

EVENTS = ["retired", "paused", "restored"]
PURGE_PREFIX = "tripplanner_purge:"            # services/jobs/tripplanner_purge_job.KEY_PREFIX
FANOUT_PREFIX = "tour_retirement_fanout:"      # services/jobs/tour_retirement_jobs.enqueue_for_events
NEAR_BEFORE = timedelta(minutes=2)
NEAR_AFTER = timedelta(minutes=30)


def out(metric, value):
    print(f"LAGDIAG {metric}={value}")


def short(v):
    return "-" if v is None else str(v)[:8]


def secs(a, b):
    if a is None or b is None:
        return "null"
    return f"{(a - b).total_seconds():.1f}"


def reason_kind(reason):
    """The fixed part of a recompute reason (`master_status=inactive:<tour>` -> `master_status=inactive`)."""
    if not reason:
        return "-"
    return str(reason).split(":", 1)[0][:40]


def actor_kind(actor):
    if not actor:
        return "-"
    a = str(actor)
    if a in ("pipeline", "admin", "system"):
        return a
    return "staff" if "@" in a else a.split(":", 1)[0][:20]


def who(created_by):
    """A job's created_by without personal data (a staff e-mail becomes `staff`)."""
    if not created_by:
        return "-"
    return "staff" if "@" in str(created_by) else str(created_by)[:40]


def parse_ts(name, default):
    raw = os.environ.get(name)
    if not raw:
        return default
    ts = datetime.fromisoformat(raw)
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


async def rows(conn, section, sql, *args):
    try:
        return await conn.fetch(sql, *args)
    except Exception as e:  # noqa: BLE001 — print the class only, never the message
        print(f"ERR {section} {type(e).__name__}")
        return None


Q_EVENTS = """
SELECT e.id, e.tour_id::text AS tour_id, e.event, e.reason, e.action, e.actor, e.occurred_at, e.idempotency_key
  FROM gold_aa_internal.tour_lifecycle_event e
 WHERE e.event = ANY($1::text[]) AND e.occurred_at >= $2 AND e.occurred_at < $3
 ORDER BY e.occurred_at, e.id
"""

Q_KEYED_JOB = """
SELECT kind, status, created_by, payload->>'trigger' AS trig, created_at, started_at, finished_at
  FROM shared.job WHERE idempotency_key = $1
"""

Q_RECOMPUTE_NEAR = """
SELECT id::text AS id, status, created_by, payload->>'scope' AS scope, payload->>'reason' AS reason,
       coalesce((payload->>'coalesced')::int, 0) AS coalesced, attempt, run_after, created_at, started_at, finished_at
  FROM shared.job
 WHERE kind = 'recompute' AND created_at >= $1 AND created_at < $2
 ORDER BY created_at
"""

# Recompute rows alive at the event: created before it and not finished before it (queued -> a fold target; running ->
# the event's change may have come after the run read the state).
Q_RECOMPUTE_AT = """
SELECT id::text AS id, status, payload->>'scope' AS scope, payload->>'reason' AS reason, run_after, created_at,
       started_at, finished_at
  FROM shared.job
 WHERE kind = 'recompute' AND created_at <= $1 AND (finished_at IS NULL OR finished_at > $1)
 ORDER BY created_at
"""

Q_FIRST_AFTER = """
SELECT id::text AS id, created_by, payload->>'scope' AS scope, payload->>'reason' AS reason,
       coalesce((payload->>'coalesced')::int, 0) AS coalesced, created_at, started_at, finished_at
  FROM shared.job
 WHERE kind = 'recompute' AND status = 'succeeded' AND started_at > $1
 ORDER BY started_at LIMIT 1
"""

Q_OTHER_RECOMPUTE_BETWEEN = """
SELECT status, count(*)::int AS n
  FROM shared.job
 WHERE kind = 'recompute' AND created_at > $1 AND created_at < $2
 GROUP BY 1 ORDER BY 1
"""

Q_WORKERS_AT = """
SELECT count(*)::int AS alive
  FROM shared.job_worker
 WHERE started_at <= $1 AND (stopped_at IS NULL OR stopped_at > $1) AND last_seen_at >= $1 - interval '5 minutes'
"""

Q_CLAIMS_BETWEEN = """
SELECT kind, count(*)::int AS n
  FROM shared.job
 WHERE started_at > $1 AND started_at < $2
 GROUP BY 1 ORDER BY 2 DESC LIMIT 8
"""


def verdict(purge, recompute_near, first, ev_at):
    if purge is None:
        effects = "unknown (no purge job keyed on the event)"
    elif purge["trig"] == "after_commit":
        effects = "ran"
    elif purge["trig"] == "sweep":
        effects = "did not run in the request (purge came from the sampler sweep)"
    else:
        effects = f"purge trigger {purge['trig']}"
    status_rows = [r for r in recompute_near or [] if reason_kind(r["reason"]).startswith("master_status=")]
    if status_rows:
        enq = "status-change recompute created"
    elif recompute_near:
        enq = "only other recompute rows near the event (a fold into one of them is possible)"
    else:
        enq = "no recompute row created near the event"
    lag = "never" if first is None else f"{(first['finished_at'] - ev_at).total_seconds():.0f}s"
    if effects == "ran" and not recompute_near:
        cause = ("after-commit ran but the recompute enqueue failed "
                 "(look for aa713_recompute_enqueue_failed in the logs)")
    elif effects.startswith("did not run") and not recompute_near:
        cause = "the event's request never ran run_after_commit (a path or a script that skips it, or the api died " \
                "between commit and effects); nothing swept recompute until an unrelated run"
    elif status_rows and first is not None and first["created_at"] > status_rows[-1]["created_at"] + NEAR_AFTER:
        cause = "a status-change recompute was created but did not finish: see its status / run_after above"
    else:
        cause = "see the rows above"
    return f"effects={effects}; enqueue={enq}; lag_to_finished={lag}; likely_cause={cause}"


async def main():
    since = parse_ts("SINCE", datetime(2026, 11, 5, tzinfo=timezone.utc))
    until = parse_ts("UNTIL", since + timedelta(days=1))
    out("window", f"{since.isoformat()}..{until.isoformat()}")
    try:
        conn = await asyncpg.connect(os.environ["DATABASE_URL"], ssl="require",
                                     server_settings={"application_name": "aa1056-recompute-lag-diag",
                                                      "default_transaction_read_only": "on"})
    except Exception as e:  # noqa: BLE001
        print(f"ERR connect {type(e).__name__}")
        return
    try:
        events = await rows(conn, "events", Q_EVENTS, EVENTS, since, until)
        out("events", "null" if events is None else len(events))
        for i, e in enumerate(events or [], start=1):
            at = e["occurred_at"]
            p = f"e{i}"
            out(f"{p}.event", f"{e['event']} reason={e['reason']} action={e['action']} actor={actor_kind(e['actor'])} "
                              f"tour={short(e['tour_id'])} at={at.isoformat()}")
            purge = None
            for label, prefix in (("purge", PURGE_PREFIX), ("fanout", FANOUT_PREFIX)):
                r = await rows(conn, f"{p}.{label}", Q_KEYED_JOB, prefix + e["idempotency_key"])
                if r is None:
                    continue
                if not r:
                    out(f"{p}.{label}", "none")
                    continue
                j = r[0]
                if label == "purge":
                    purge = j
                out(f"{p}.{label}", f"trigger={j['trig']} status={j['status']} created_by={who(j['created_by'])} "
                                    f"created_after_s={secs(j['created_at'], at)}")
            near = await rows(conn, f"{p}.recompute_near", Q_RECOMPUTE_NEAR, at - NEAR_BEFORE, at + NEAR_AFTER)
            for k, j in enumerate(near or [], start=1):
                out(f"{p}.recompute_near.{k}", f"job={short(j['id'])} status={j['status']} "
                                               f"created_by={who(j['created_by'])} "
                                               f"scope={j['scope']} reason={reason_kind(j['reason'])} "
                                               f"coalesced={j['coalesced']} attempt={j['attempt']} "
                                               f"created_after_s={secs(j['created_at'], at)} "
                                               f"run_after_after_s={secs(j['run_after'], at)} "
                                               f"started_after_s={secs(j['started_at'], at)} "
                                               f"finished_after_s={secs(j['finished_at'], at)}")
            if near is not None and not near:
                out(f"{p}.recompute_near", "none")
            alive = await rows(conn, f"{p}.recompute_at_event", Q_RECOMPUTE_AT, at)
            for k, j in enumerate(alive or [], start=1):
                out(f"{p}.recompute_at_event.{k}", f"job={short(j['id'])} status={j['status']} scope={j['scope']} "
                                                   f"reason={reason_kind(j['reason'])} "
                                                   f"created_before_s={secs(at, j['created_at'])} "
                                                   f"run_after_after_s={secs(j['run_after'], at)} "
                                                   f"started_after_s={secs(j['started_at'], at)} "
                                                   f"finished_after_s={secs(j['finished_at'], at)}")
            if alive is not None and not alive:
                out(f"{p}.recompute_at_event", "none")
            first_rows = await rows(conn, f"{p}.first_after", Q_FIRST_AFTER, at)
            first = first_rows[0] if first_rows else None
            if first is None:
                out(f"{p}.first_finished_after", "none")
            else:
                out(f"{p}.first_finished_after", f"job={short(first['id'])} created_by={who(first['created_by'])} "
                                                 f"scope={first['scope']} reason={reason_kind(first['reason'])} "
                                                 f"coalesced={first['coalesced']} "
                                                 f"created_after_s={secs(first['created_at'], at)} "
                                                 f"started_after_s={secs(first['started_at'], at)} "
                                                 f"finished_after_s={secs(first['finished_at'], at)}")
                between = await rows(conn, f"{p}.recompute_between", Q_OTHER_RECOMPUTE_BETWEEN, at, first["created_at"])
                out(f"{p}.recompute_rows_between", ",".join(f"{r['status']}:{r['n']}" for r in between or []) or "0")
            w = await rows(conn, f"{p}.workers", Q_WORKERS_AT, at)
            out(f"{p}.workers_alive_at_event", w[0]["alive"] if w else "null")
            horizon = first["started_at"] if first is not None else at + timedelta(hours=6)
            claims = await rows(conn, f"{p}.claims", Q_CLAIMS_BETWEEN, at, horizon)
            out(f"{p}.claims_until_first_recompute", ",".join(f"{r['kind']}:{r['n']}" for r in claims or []) or "0")
            out(f"{p}.verdict", verdict(purge, near, first, at))
    finally:
        await conn.close()
    out("done", 1)


if __name__ == "__main__":
    asyncio.run(main())
