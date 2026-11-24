"""AA-1174 (S288) — evidence source ``cis_db``: what the CIS API can prove from its own database, for the privacy
obligations of Platform › Compliance (storage limitation / deletion when no longer needed).

    collect(pool, now) -> list of evidence rows (the shape compliance.ingest() takes), never raises

Checks (ids stay stable: AA-1173 moves the collection into a worker job without renaming them):

  retention_runs_recent    every retention job kind of the CIS job runner (services/retention/policy.py SPECS: llm
                           call log, tenant API usage, jobs, decision log, cost snapshots) finished a run; fact
                           ``last_date`` = the OLDEST of the newest succeeded runs (CIS grades its age against
                           cis.compliance.retention_run_max_age_days: a schedule that stopped is not evidence). A kind
                           with no succeeded run at all makes the check an error that names it.
  retention_live_enabled   fact ``live_last_date`` = the OLDEST of the newest succeeded LIVE runs, one per kind (CIS
                           grades its age against cis.compliance.retention_live_max_age_days). Recency, not existence:
                           scheduled runs stay DRY RUN until a live run of the same windows was approved and fall back
                           to DRY RUN for good after any window change (services/jobs/retention_job.py decide_mode); a
                           DRY RUN or a held run deletes nothing, so only a recent live run proves old data goes. A kind
                           with no live run at all makes the row ``fail`` and names it. A manual_override run does not
                           count, as in the job runner's own live baseline. The DRY RUN and live/held counts of the
                           last 30 days are in detail.

detail holds kind names, counts and dates only (no job payload, no personal data); the link is the root retention and
erasure run log on GitHub. Read as cis_app (the role of the API; shared.job is its own table). A database error gives
``error`` rows, never an exception (the page still loads).

AA-1173 (S290, phase 2): the job compliance_db_checks (services/jobs/compliance_db_checks_job.py) collects this source
once a day on the worker (the Refresh action still may), and three more checks join it:

  db_rls_policies          the X2 RLS catalogue rules (shared/compliance/rls_catalogue_rules.py, a byte copy of the CIS
                           CI guard tests/integration/rls_catalogue_rules.py, also run by AA-CIS-Infra on the nightly
                           schema dump) over the LIVE catalog of every app schema (CIS, Booking, TripPlanner, CRM):
                           rules a b c d t (a tenant_id table without RLS + an app.tenant_id policy, a policy TO
                           PUBLIC, the old GUC, a GUC outside NULLIF, an unconditional policy). pass = no finding off
                           the shared allowlist (the allowlist is the reviewed exception path; a security invariant, no
                           threshold). detail: counts per rule and up to 50 object keys (schema.table[:policy]).
  db_grants                the same rules p g e (a grant to PUBLIC, a grant to another owner's role, a foreign key
                           across owners). pass = none off the allowlist.
  monthly_review_recent    fact ``last_date`` = the day of the newest Security posture monthly review
                           (compliance.review, CIS 481); CIS grades its age against
                           cis.compliance.review_max_age_days. No review yet = ``fail`` (no date to grade).

The catalog reads only pg_catalog views every role may read (pg_class, pg_attribute, pg_policies, aclexplode of the
ACLs), so cis_app sees the RLS state of booking.*, tripplanner.* and crm.* without any grant on their tables (root
advisory section 4 C.4). Policy expressions are read to evaluate the rules, never stored: detail keeps object names.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Optional

import structlog

from services.retention.policy import SPECS
from shared.compliance import rls_catalogue_rules as rls

logger = structlog.get_logger()

SOURCE = "cis_db"
COLLECTOR = "cis api (compliance_db)"
LINK = "https://github.com/AdventureAsia365-Ecosys/AA-Ecosys/blob/master/docs/legal/retention-log.md"
LINK_SCHEMA = ("https://github.com/AdventureAsia365-Ecosys/AA-Ecosys/blob/master/docs/architecture/"
               "db-schema-reference.md")
KINDS = tuple(SPECS)
DRY_RUN_WINDOW_DAYS = 30   # technical-constant: the DRY RUN share shown in detail (display only, never graded)
_SHA = re.compile(r"^[0-9a-f]{7,40}$")

# A manual_override run (an admin proof run with an explicit cutoff, Dev only) is not a live baseline for the job runner
# (services/jobs/retention_job.py _LAST_LIVE_SQL leaves it out, so scheduled runs stay DRY RUN after it): the same
# filter here, so the page never counts as "live" what the runner itself does not accept.
NOT_OVERRIDE = "coalesce(result ->> 'trigger', 'manual') <> 'manual_override'"
SQL_RETENTION = f"""
SELECT kind,
       max(finished_at) FILTER (WHERE status = 'succeeded')                                AS last_run,
       max(finished_at) FILTER (WHERE status = 'succeeded' AND result ->> 'mode' = 'live'
                                  AND {NOT_OVERRIDE})                                      AS last_live,
       count(*) FILTER (WHERE status = 'succeeded' AND finished_at > now() - make_interval(days => $2)
                          AND result ->> 'mode' = 'dry_run')                                AS dry_runs,
       count(*) FILTER (WHERE status = 'succeeded' AND finished_at > now() - make_interval(days => $2)
                          AND result ->> 'mode' IN ('live', 'held') AND {NOT_OVERRIDE})     AS live_or_held
  FROM shared.job
 WHERE kind = ANY($1::text[])
 GROUP BY kind
"""

RLS_RULES = ("a", "b", "c", "d", "t")
GRANT_RULES = ("p", "g", "e")
MAX_KEYS = 50   # technical-constant: object keys listed in detail (the 8 KiB detail cap)
SQL_LAST_REVIEW = "SELECT max(reviewed_at) AS last FROM compliance.review WHERE kind = 'monthly_posture'"


def _version() -> str | None:
    try:
        from api.core.readiness import git_sha   # noqa: PLC0415 - the running image's SHA (IPE: re-runnable code)

        sha = git_sha()
    except Exception:   # noqa: BLE001
        return None
    return sha if isinstance(sha, str) and _SHA.fullmatch(sha) else None


def _row(check_id: str, status: str, summary: str, detail: dict, now: datetime,
         link: Optional[str] = LINK) -> dict:
    return {"source": SOURCE, "check_id": check_id, "status": status, "collected_at": now, "collector": COLLECTOR,
            "collector_version": _version(), "run_ref": None, "summary": summary[:300], "detail": detail, "link": link}


def evaluate(rows: list[dict], now: datetime) -> list[dict]:
    """The two evidence rows from the per-kind aggregate (pure; ``rows`` = SQL_RETENTION results)."""
    by_kind = {r["kind"]: r for r in rows}
    dates = {k: by_kind[k]["last_run"].date().isoformat() for k in KINDS
             if k in by_kind and by_kind[k]["last_run"] is not None}
    never_ran = sorted(set(KINDS) - set(dates))
    live_dates = {k: by_kind[k]["last_live"].date().isoformat() for k in KINDS
                  if k in by_kind and by_kind[k]["last_live"] is not None}
    never_live = sorted(set(KINDS) - set(live_dates))
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
    live_detail = {"kinds": len(KINDS), "last_live": live_dates, "never_live_kinds": never_live,
                   f"dry_runs_{DRY_RUN_WINDOW_DAYS}d": dry, f"live_or_held_{DRY_RUN_WINDOW_DAYS}d": live}
    if never_live:
        # a kind that never deleted anything for real: no date to grade, so the row fails on its own
        lives = _row("retention_live_enabled", "fail",
                     f"{len(never_live)} retention kind(s) never had a live run (DRY RUN only): "
                     + ", ".join(never_live), live_detail, now)
    else:
        oldest_live = min(live_dates.values())
        lives = _row("retention_live_enabled", "pass",
                     f"{len(live_dates)}/{len(KINDS)} retention kinds had a live run; oldest newest live run "
                     f"{oldest_live}", dict(live_detail, live_last_date=oldest_live), now)
    return [runs, lives]


def _asyncpg_sql(sql: str) -> str:
    return sql.replace("%(schemas)s", "$1")


async def read_catalog(conn: Any) -> dict:
    """The rules' input (a format-2 dump subset) from the live catalog (pg_catalog reads only)."""
    rows = {"schemas": [dict(r) for r in await conn.fetch(rls.CATALOG_SQL["schemas"])]}
    names = [s["schema"] for s in rows["schemas"]]
    for key, sql in rls.CATALOG_SQL.items():
        if key != "schemas":
            rows[key] = [dict(r) for r in await conn.fetch(_asyncpg_sql(sql), names)]
    return rls.dump_from_rows(rows)


def evaluate_catalog(dump: dict, now: datetime, allowlist: Optional[dict] = None) -> list[dict]:
    """The two catalogue rows (pure): RLS policies and grants, each pass when no finding is off the allowlist."""
    result = rls.check(dump, allowlist)
    out = []
    for check_id, rules, what in (("db_rls_policies", RLS_RULES, "RLS policy"),
                                  ("db_grants", GRANT_RULES, "grant or ownership")):
        bad = [f for f in result.violations if f.rule in rules]
        accepted = [f for f, _ in result.allowlisted if f.rule in rules]
        by_rule = {r: sum(1 for f in bad if f.rule == r) for r in rules}
        detail = {"schemas": len(dump.get("schemas", [])),
                  "tables": sum(len(v) for v in dump.get("relations", {}).values()),
                  "violations": len(bad), "by_rule": by_rule, "allowlisted": len(accepted),
                  "keys": sorted(f"{f.rule}:{f.key}" for f in bad)[:MAX_KEYS]}
        if bad:
            out.append(_row(check_id, "fail", f"{len(bad)} {what} finding(s) off the allowlist "
                            f"({', '.join(f'{r} {n}' for r, n in by_rule.items() if n)})", detail, now, LINK_SCHEMA))
        else:
            out.append(_row(check_id, "pass", f"no {what} finding off the allowlist ({len(accepted)} allowlisted, "
                            f"{detail['tables']} relations read)", detail, now, LINK_SCHEMA))
    return out


def evaluate_review(last: Optional[datetime], now: datetime) -> dict:
    if last is None:
        return _row("monthly_review_recent", "fail", "no Security posture monthly review recorded yet", {}, now, None)
    day = last.date().isoformat()
    return _row("monthly_review_recent", "pass", f"newest monthly review {day}", {"last_date": day}, now, None)


CATALOG_CHECKS = ("db_rls_policies", "db_grants")


async def collect(pool: Any, now: datetime) -> list[dict]:
    try:
        async with pool.acquire() as conn:
            rows = [dict(r) for r in await conn.fetch(SQL_RETENTION, list(KINDS), DRY_RUN_WINDOW_DAYS)]
        out = evaluate(rows, now)
    except Exception as err:   # noqa: BLE001 - an error result, never a page error
        logger.warning("compliance_db_collect_failed", error=type(err).__name__)
        summary = f"the CIS database could not be read ({type(err).__name__})"
        out = [_row(cid, "error", summary, {}, now) for cid in ("retention_runs_recent", "retention_live_enabled")]
    try:
        async with pool.acquire() as conn:
            dump = await read_catalog(conn)
        out += evaluate_catalog(dump, now)
    except Exception as err:   # noqa: BLE001
        logger.warning("compliance_db_catalog_failed", error=type(err).__name__)
        out += [_row(cid, "error", f"the catalog could not be read ({type(err).__name__})", {}, now)
                for cid in CATALOG_CHECKS]
    try:
        async with pool.acquire() as conn:
            last = await conn.fetchval(SQL_LAST_REVIEW)
        out.append(evaluate_review(last, now))
    except Exception as err:   # noqa: BLE001 - CIS 481 not applied yet, or the table unreadable
        logger.warning("compliance_db_review_failed", error=type(err).__name__)
        out.append(_row("monthly_review_recent", "error",
                        f"the review record could not be read ({type(err).__name__})", {}, now))
    return out
