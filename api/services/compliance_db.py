"""AA-1174 (S288) — evidence source ``cis_db``: what the CIS API can prove from its own database, for the privacy
obligations of Platform › Compliance (storage limitation / deletion when no longer needed).

    collect(pool, now) -> list of evidence rows (the shape compliance.ingest() takes), never raises

Checks (ids stay stable: AA-1173 moves the collection into a worker job without renaming them):

  retention_runs_recent    every retention job kind of the CIS job runner (services/retention/policy.py SPECS: llm
                           call log, tenant API usage, jobs, decision log, cost snapshots) finished a run; fact
                           ``last_date`` = the OLDEST of the newest succeeded runs (CIS grades its age against
                           cis.compliance.retention_run_max_age_days: a schedule that stopped is not evidence). A kind
                           with no succeeded run at all makes the check an error that names it.
  retention_live_enabled   fact ``never_live`` = the kinds with no succeeded LIVE run in the job table (scheduled runs
                           stay DRY RUN until a live run of the same windows was approved,
                           services/jobs/retention_job.py decide_mode, and a DRY RUN deletes nothing); CIS grades it
                           against cis.compliance.retention_never_live_max. The DRY RUN share of the last 30 days is in
                           detail.

detail holds kind names, counts and dates only (no job payload, no personal data); the link is the root retention and
erasure run log on GitHub. Read as cis_app (the role of the API; shared.job is its own table). A database error gives
``error`` rows, never an exception (the page still loads).
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

import structlog

from services.retention.policy import SPECS

logger = structlog.get_logger()

SOURCE = "cis_db"
COLLECTOR = "cis api (compliance_db)"
LINK = "https://github.com/AdventureAsia365-Ecosys/AA-Ecosys/blob/master/docs/legal/retention-log.md"
KINDS = tuple(SPECS)
DRY_RUN_WINDOW_DAYS = 30   # technical-constant: the DRY RUN share shown in detail (display only, never graded)
_SHA = re.compile(r"^[0-9a-f]{7,40}$")

SQL_RETENTION = """
SELECT kind,
       max(finished_at) FILTER (WHERE status = 'succeeded')                                AS last_run,
       max(finished_at) FILTER (WHERE status = 'succeeded' AND result ->> 'mode' = 'live')  AS last_live,
       count(*) FILTER (WHERE status = 'succeeded' AND finished_at > now() - make_interval(days => $2)
                          AND result ->> 'mode' = 'dry_run')                                AS dry_runs,
       count(*) FILTER (WHERE status = 'succeeded' AND finished_at > now() - make_interval(days => $2)
                          AND result ->> 'mode' IN ('live', 'held'))                        AS live_or_held
  FROM shared.job
 WHERE kind = ANY($1::text[])
 GROUP BY kind
"""


def _version() -> str | None:
    try:
        from api.core.readiness import git_sha   # noqa: PLC0415 - the running image's SHA (IPE: re-runnable code)

        sha = git_sha()
    except Exception:   # noqa: BLE001
        return None
    return sha if isinstance(sha, str) and _SHA.fullmatch(sha) else None


def _row(check_id: str, status: str, summary: str, detail: dict, now: datetime) -> dict:
    return {"source": SOURCE, "check_id": check_id, "status": status, "collected_at": now, "collector": COLLECTOR,
            "collector_version": _version(), "run_ref": None, "summary": summary[:300], "detail": detail, "link": LINK}


def evaluate(rows: list[dict], now: datetime) -> list[dict]:
    """The two evidence rows from the per-kind aggregate (pure; ``rows`` = SQL_RETENTION results)."""
    by_kind = {r["kind"]: r for r in rows}
    dates = {k: by_kind[k]["last_run"].date().isoformat() for k in KINDS
             if k in by_kind and by_kind[k]["last_run"] is not None}
    never_ran = sorted(set(KINDS) - set(dates))
    never_live = sorted(k for k in KINDS if k not in by_kind or by_kind[k]["last_live"] is None)
    dry = {k: int(by_kind[k]["dry_runs"] or 0) for k in KINDS if k in by_kind}
    live = {k: int(by_kind[k]["live_or_held"] or 0) for k in KINDS if k in by_kind}
    detail = {"kinds": len(KINDS), "last_run": dates, f"dry_runs_{DRY_RUN_WINDOW_DAYS}d": dry,
              f"live_or_held_{DRY_RUN_WINDOW_DAYS}d": live}
    if never_ran:
        runs = _row("retention_runs_recent", "error", f"{len(never_ran)} retention kind(s) never finished a run: "
                    + ", ".join(never_ran), dict(detail, never_ran=never_ran), now)
    else:
        oldest = min(dates.values())
        runs = _row("retention_runs_recent", "pass",
                    f"{len(dates)}/{len(KINDS)} retention kinds ran; oldest newest run {oldest}",
                    dict(detail, last_date=oldest), now)
    lives = _row("retention_live_enabled", "pass",
                 f"{len(KINDS) - len(never_live)}/{len(KINDS)} retention kinds have had a live run"
                 + (f"; DRY RUN only: {', '.join(never_live)}" if never_live else ""),
                 {"kinds": len(KINDS), "never_live": len(never_live), "never_live_kinds": never_live}, now)
    return [runs, lives]


async def collect(pool: Any, now: datetime) -> list[dict]:
    try:
        async with pool.acquire() as conn:
            rows = [dict(r) for r in await conn.fetch(SQL_RETENTION, list(KINDS), DRY_RUN_WINDOW_DAYS)]
    except Exception as err:   # noqa: BLE001 - an error result, never a page error
        logger.warning("compliance_db_collect_failed", error=type(err).__name__)
        summary = f"the CIS database could not be read ({type(err).__name__})"
        return [_row(cid, "error", summary, {}, now) for cid in ("retention_runs_recent", "retention_live_enabled")]
    return evaluate(rows, now)
