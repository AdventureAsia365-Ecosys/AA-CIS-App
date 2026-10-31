"""AA-900 — "can the platform call its LLMs right now?", read from what real calls did. No probe call, $0.

Served by ``GET /admin/llm-availability`` (api/routers/admin_llm_ops.py), shown as the LLM availability card on
Settings › LLM Models.

Two sources, both real outcomes inside the window:

- ``shared.llm_call_log``: per provider / account (and embeddings apart), how many calls were answered and when the
  last answered one was. A row is written only for a billed call (AA-927), so a call the provider refused leaves no
  row here: this side can only say "the last success was at ...".
- ``shared.job``: the LLM errors of jobs that failed (or wait for a retry) in the window, each dated by when it
  happened (``JOBS_SQL``), kept when ``is_llm_failure`` (the ``llm_access_denied`` prefix, or older text that names
  an LLM call) and classified with
  ``shared.llm_client.errors.classify_text`` — ``access_denied`` (Bedrock "Error 002", AccessDenied, IAM),
  ``quota_exhausted`` (no OpenAI credit) or ``throttled``. Since AA-900 a job ended by an account-level refusal has an
  error that starts with ``llm_access_denied``; older rows are classified by their text (an older S1 job only says
  "run_tour failed after retries", so it does not count).

Status: the newest outcome wins. ``blocked`` when the newest account-level refusal is newer than the last answered
text call (embeddings run on another account and do not clear it), ``degraded`` for a throttle newer than it,
``ok`` when an answered call is the newest and at most STALE_OK_HOURS old, ``unknown`` otherwise.

AA-1002 (S4) — per connection: ``connections`` lists every row of ``shared.llm_provider_connection`` with the circuit
breaker state of each of its deployments that has a health row (``shared.llm_provider_health``, migration 309; the
state rule is ``shared.llm_client.breaker.state_of``). A connection's status: ``disabled`` (switched off), ``open``
(a deployment's circuit is open or half-open: calls skip it), ``degraded`` (failures in a row, circuit still closed),
``ok`` (no failure, an answered call within STALE_OK_HOURS), ``unknown`` otherwise. Before 304 / 309 are applied the
list is empty and ``health_ready`` is false. Still no probe call: the admin's "Test connection" is a separate action.

AA-1018 — each connection also carries ``circuit``: its connection-level circuit (``model_key "*"``, derived from
the deployment rows by ``shared.llm_client.breaker.connection_health``: tripped by a key or credit refusal, or access
denied on two or more models; ``state`` closed | open | half_open, ``refusing`` as the re-queue reads it, the class,
until when, the tripping models), and ``failure_domain`` (``shared.llm_client.invariant.failure_domain``).
``invariant`` is the report-only failure-domain rule (``invariant.read``): per stage and stage class, whether an
enabled, funded deployment in another failure domain exists.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

import asyncpg

from shared.llm_client import invariant
from shared.llm_client.breaker import (CLOSED, Health, connection_health, connection_members, connection_refusing,
                                       state_of)
from shared.llm_client.errors import ACCESS_DENIED, QUOTA_EXHAUSTED, THROTTLED, classify_text

FAILURE_CLASSES = (ACCESS_DENIED, QUOTA_EXHAUSTED, THROTTLED)
BLOCKING_CLASSES = (ACCESS_DENIED, QUOTA_EXHAUSTED)
ERROR_SAMPLE_CHARS = 300
JOB_ROW_LIMIT = 1000

# Jev (provider typesafe) is a decision service with its own credit card on Settings; not an LLM route here.
CALLS_SQL = """
    SELECT coalesce(provider, 'unknown')                                        AS provider,
           account,
           (role = 'embed')                                                     AS embed,
           count(*)::int                                                        AS calls,
           count(*) FILTER (
               WHERE quality_signal->>'billed_unusable' IS DISTINCT FROM 'true')::int AS ok_calls,
           max(created_at)                                                      AS last_call_at,
           max(created_at) FILTER (
               WHERE quality_signal->>'billed_unusable' IS DISTINCT FROM 'true') AS last_ok_at
      FROM shared.llm_call_log
     WHERE created_at >= $1
       AND coalesce(provider, '') <> 'typesafe'
     GROUP BY 1, 2, 3
     ORDER BY last_call_at DESC
"""

# When each LLM failure happened (S258 review of #1148): a failed row by its finished_at (set once, when the job ended;
# updated_at moves on any later touch), a row queued for a retry by its updated_at (queue.fail re-queues with the
# error and now(); nothing else writes a queued row before a worker claims it). A running row is left out: its error is
# the previous attempt's and its updated_at is the heartbeat, so it would date an old failure as new; the attempt it
# is running ends as a call-log row, a re-queue or a failure, and that outcome is counted then.
JOBS_SQL = """
    SELECT id::text AS id, kind, status, error, finished_at AS failed_at
      FROM shared.job
     WHERE status = 'failed' AND error IS NOT NULL AND finished_at >= $1
    UNION ALL
    SELECT id::text AS id, kind, status, error, updated_at AS failed_at
      FROM shared.job
     WHERE status = 'queued' AND error IS NOT NULL AND updated_at >= $1
     ORDER BY failed_at DESC
     LIMIT $2
"""


def _iso(v: Optional[datetime]) -> Optional[str]:
    return v.isoformat() if v else None


def _latest(values: Iterable[Optional[datetime]]) -> Optional[datetime]:
    present = [v for v in values if v is not None]
    return max(present) if present else None


# Only LLM failures count: S3 / Cognito AccessDenied, SES rate limits and Geoapify 429s also classify as
# access_denied / throttled, but they say nothing about the LLM route (S258 review of #1148).
LLM_ERROR_PREFIX = "llm_access_denied"
_LLM_MARKER_RE = re.compile(r"bedrock|invokemodel|converse|anthropic|openai|llm|error 002", re.I)


def is_llm_failure(error: Optional[str]) -> bool:
    """True for a job error written by the LLM gateway path: the AA-900 prefix, or older text naming an LLM call."""
    text = error or ""
    return text.startswith(LLM_ERROR_PREFIX) or bool(_LLM_MARKER_RE.search(text))


def summarize_failures(job_rows: Iterable[dict]) -> list[dict]:
    """One entry per LLM failure class seen (count, newest time, newest job), newest class first."""
    by_class: dict[str, dict] = {}
    for row in job_rows:
        if not is_llm_failure(row.get("error")):
            continue
        cls = classify_text(row.get("error"))
        if cls not in FAILURE_CLASSES:
            continue
        entry = by_class.setdefault(cls, {"error_class": cls, "jobs": 0, "last_at": None})
        entry["jobs"] += 1
        at = row.get("failed_at")
        if entry["last_at"] is None or (at is not None and at > entry["last_at"]):
            entry.update(last_at=at, last_kind=row.get("kind"), last_job_id=row.get("id"),
                         last_status=row.get("status"), last_error=(row.get("error") or "")[:ERROR_SAMPLE_CHARS])
    return sorted(by_class.values(), key=lambda e: e["last_at"] or datetime.min.replace(tzinfo=timezone.utc),
                  reverse=True)


# S258: no answered call for this long means "we cannot tell", not "ok" — after a provider refusal nothing may run
# (Dev on 29/10/2026: the newest outcome was a success from before the refusal, so the card said ok while every
# model was refused).
STALE_OK_HOURS = 6


def decide_status(last_ok_at: Optional[datetime], failures: list[dict],
                  now: Optional[datetime] = None) -> tuple[str, Optional[dict]]:
    """(status, the failure entry that set it). See the module docstring for the rule; an answered call older than
    STALE_OK_HOURS (with ``now`` given) gives ``unknown``."""
    blocking = [f for f in failures if f["error_class"] in BLOCKING_CLASSES and f["last_at"]]
    throttled = [f for f in failures if f["error_class"] == THROTTLED and f["last_at"]]
    newest_block = max(blocking, key=lambda f: f["last_at"], default=None)
    newest_throttle = max(throttled, key=lambda f: f["last_at"], default=None)
    if newest_block and (last_ok_at is None or newest_block["last_at"] > last_ok_at):
        return "blocked", newest_block
    if newest_throttle and (last_ok_at is None or newest_throttle["last_at"] > last_ok_at):
        return "degraded", newest_throttle
    if last_ok_at is not None and (now is None or now - last_ok_at <= timedelta(hours=STALE_OK_HOURS)):
        return "ok", None
    return "unknown", None


def build(call_rows: Iterable[dict], job_rows: Iterable[dict], *, hours: int, now: datetime) -> dict:
    """The endpoint's response from the two row sets (pure: unit-tested without a database)."""
    providers = []
    for r in call_rows:
        providers.append({
            "provider": r["provider"], "account": r.get("account"), "embed": bool(r.get("embed")),
            "calls": int(r.get("calls") or 0), "ok_calls": int(r.get("ok_calls") or 0),
            "last_call_at": _iso(r.get("last_call_at")), "last_ok_at": _iso(r.get("last_ok_at")),
        })
    text_rows = [r for r in call_rows if not r.get("embed")]
    last_ok_at = _latest(r.get("last_ok_at") for r in text_rows)
    failures = summarize_failures(job_rows)
    status, cause = decide_status(last_ok_at, failures, now)
    return {
        "status": status,
        "window_hours": hours,
        "generated_at": now.isoformat(),
        "last_ok_at": _iso(last_ok_at),
        "cause": ({"error_class": cause["error_class"], "at": _iso(cause["last_at"]), "kind": cause.get("last_kind"),
                   "job_id": cause.get("last_job_id"), "error": cause.get("last_error")} if cause else None),
        "providers": providers,
        "failures": [{**f, "last_at": _iso(f["last_at"])} for f in failures],
    }


# AA-1002 (S4): every connection with the breaker rows of its deployments (none = every circuit closed).
CONNECTIONS_SQL = """
    SELECT c.connection_key, c.kind, c.label, c.enabled, c.data_scope, c.account_id, c.account_id_env,
           h.model_key, h.last_ok_at, h.last_error_at, h.last_error_class, h.last_error, h.consecutive_failures,
           h.circuit_open_until, h.open_streak, h.opened_at, h.probe_until
      FROM shared.llm_provider_connection c
      LEFT JOIN shared.llm_provider_health h USING (connection_key)
     ORDER BY c.connection_key, h.model_key
"""

_HEALTH_FIELDS = ("last_ok_at", "last_error_at", "last_error_class", "last_error", "consecutive_failures",
                  "circuit_open_until", "open_streak", "opened_at", "probe_until")


def connection_status(enabled: bool, models: list[dict], now: datetime) -> str:
    """See the module docstring (AA-1002 S4)."""
    if not enabled:
        return "disabled"
    if any(m["state"] != CLOSED for m in models):
        return "open"
    if any(m["consecutive_failures"] for m in models):
        return "degraded"
    last_ok = _latest(m["_last_ok"] for m in models)
    if last_ok is not None and now - last_ok <= timedelta(hours=STALE_OK_HOURS):
        return "ok"
    return "unknown"


def build_connections(rows: Iterable[dict], *, now: datetime) -> list[dict]:
    """One entry per connection (pure: unit-tested without a database)."""
    by_key: dict[str, dict] = {}
    for r in rows:
        entry = by_key.setdefault(r["connection_key"], {
            "connection_key": r["connection_key"], "kind": r.get("kind"), "label": r.get("label"),
            "enabled": bool(r.get("enabled")), "models": [], "_health": []})
        if "failure_domain" not in entry and r.get("kind") and r.get("data_scope"):
            entry["failure_domain"] = invariant.failure_domain(r)
        if r.get("model_key") is None:
            continue
        h = Health(connection_key=r["connection_key"], model_key=r["model_key"],
                   **{f: r.get(f) for f in _HEALTH_FIELDS if f not in ("consecutive_failures", "open_streak")},
                   consecutive_failures=int(r.get("consecutive_failures") or 0),
                   open_streak=int(r.get("open_streak") or 0))
        entry["_health"].append(h)
        entry["models"].append({
            "model_key": h.model_key, "state": state_of(h, now), "consecutive_failures": h.consecutive_failures,
            "last_ok_at": _iso(h.last_ok_at), "last_error_at": _iso(h.last_error_at),
            "last_error_class": h.last_error_class, "last_error": (h.last_error or "")[:ERROR_SAMPLE_CHARS] or None,
            "circuit_open_until": _iso(h.circuit_open_until), "probe_until": _iso(h.probe_until),
            "open_streak": h.open_streak, "_last_ok": h.last_ok_at, "_last_error": h.last_error_at})
    out = []
    for entry in by_key.values():
        models = entry["models"]
        newest_error = max((m for m in models if m["_last_error"]), key=lambda m: m["_last_error"], default=None)
        entry.update(
            status=connection_status(entry["enabled"], models, now),
            open_models=sum(1 for m in models if m["state"] != CLOSED),
            last_ok_at=_iso(_latest(m["_last_ok"] for m in models)),
            last_error_at=newest_error["last_error_at"] if newest_error else None,
            last_error_class=newest_error["last_error_class"] if newest_error else None,
        )
        for m in models:
            m.pop("_last_ok")
            m.pop("_last_error")
        entry["circuit"] = connection_circuit(entry.pop("_health"), entry["connection_key"], now)
        entry.setdefault("failure_domain", None)
        out.append(entry)
    return out


def connection_circuit(health: list[Health], connection_key: str, now: datetime) -> dict:
    """AA-1018: the connection-level circuit of one connection, for display (pure)."""
    h = connection_health(health, connection_key, now)
    refusing = bool(h is not None and connection_refusing(h, now))
    return {"model_key": "*", "state": state_of(h, now), "refusing": refusing,
            "error_class": h.last_error_class if h else None,
            "open_until": _iso(h.circuit_open_until) if h else None, "opened_at": _iso(h.opened_at) if h else None,
            "models": connection_members(health, connection_key, now)}


def blocked_from(connections: list[dict]) -> dict[str, str]:
    """{connection_key: class} of the connections whose circuit still refuses (input of the invariant)."""
    return {c["connection_key"]: c["circuit"]["error_class"] for c in connections
            if c.get("circuit", {}).get("refusing")}


async def read_connections(conn, *, now: datetime) -> tuple[bool, list[dict]]:
    """(health_ready, connections). Before migration 304 / 309 the tables are missing: (False, [])."""
    try:
        rows = [dict(r) for r in await conn.fetch(CONNECTIONS_SQL)]
    except asyncpg.exceptions.UndefinedTableError:
        return False, []
    return True, build_connections(rows, now=now)


async def read(conn, *, hours: int = 24, now: Optional[datetime] = None) -> dict:
    """Read-only queries on one connection (the job side reads the finished_at and queued indexes); AA-1018: plus the
    four registry reads of the failure-domain report."""
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(hours=hours)
    call_rows = [dict(r) for r in await conn.fetch(CALLS_SQL, since)]
    job_rows = [dict(r) for r in await conn.fetch(JOBS_SQL, since, JOB_ROW_LIMIT)]
    out = build(call_rows, job_rows, hours=hours, now=now)
    out["health_ready"], out["connections"] = await read_connections(conn, now=now)
    out["invariant"] = await invariant.read(conn, blocked=blocked_from(out["connections"]), now=now)
    return out
