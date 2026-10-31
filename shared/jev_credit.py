"""shared/jev_credit.py — AA-845: estimated Jev (TypeSafe) credit and the low-credit alert.

TypeSafe has no balance API. Admins record each manual top-up in AI providers › Providers › Jev credit
(shared.jev_credit_topup, AA-756); the estimate is

    estimated_left = sum(amount_usd of every top-up) − Jev spend (shared.llm_call_log, provider typesafe)
                     since the first top-up date

The exhausted alert (AA-720, shared/llm_client/decide.maybe_alert_jev_credit) fires only when a call already
failed with a 402. This module adds the warning before that point (AA-845 Q845-2, reversing the S224
"no running-low alert" rule): when the estimate drops below LOW_CREDIT_THRESHOLD_USD, one admin bell
(`platform.jev_credit.low`) and one operator e-mail (the AA-828 `ops_alert_email` job, the same recipients as
the spend budget e-mails).

- Who calls it: the api's ops sampler every JEV_CREDIT_CHECK_EVERY_S (api/core/ops_metrics_sampler.py), so the
  warning lands during a wave, not at the next daily probe; and POST /admin/decisions/jev-credit/check (admin,
  also the live verification path).
- Throttle (no paging on a repeat): one real low bell per 24 h, read or not; the e-mail is queued only when a
  new bell is written, and its job idempotency key allows one per UTC day. The estimate only falls between top-ups, so
  there is no flapping; a new top-up lifts it above the threshold and the next check stays quiet.
- No top-up recorded → no estimate → no alert (the Settings card shows "—").
- A test call (an explicit threshold_usd) writes a bell marked `"test": true` (admins only) and a "[TEST]"
  e-mail; test bells never count for the real throttle, and the test e-mail has its own idempotency key, so a
  verification run cannot suppress a real alert.
- Threshold: a code constant. There is no platform settings row to hold it (shared.spend_budget is per-run /
  per-day spend, not a balance floor), and no new env var (underscore env vars are set by hand only). Making it
  editable in Settings needs a migration; see docs/implementation-notes/AA-845.md.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Optional

import structlog

logger = structlog.get_logger()

JEV_CREDIT_LOW_EVENT = "platform.jev_credit.low"
LOW_CREDIT_THRESHOLD_USD = 3.0
# How often the api sampler re-checks (the sampler ticks every 300 s). The spend sum reads the
# provider-typesafe rows of llm_call_log since the first top-up, so it is not run every tick.
JEV_CREDIT_CHECK_EVERY_S = 1800.0
# Bounds for an explicit (test) threshold on the admin check route.
MAX_TEST_THRESHOLD_USD = 1000.0

# Same platform sentinel tenant as the AA-720 exhausted alert (shared.notifications.tenant_id is NOT NULL).
PLATFORM_TENANT_ID = "00000000-0000-0000-0000-000000000001"

TOPUP_TOTAL_SQL = """
    SELECT sum(amount_usd)::float AS total, min(topped_up_on) AS first_on
      FROM shared.jev_credit_topup
"""
# Jev spend since the earliest top-up date. $1 is that date (NULL → no top-ups → 0 spend). Shared with the
# Settings card (api/routers/admin_decisions.py GET /admin/decisions/jev-credit) so both show one number.
JEV_SPEND_SQL = """
    SELECT coalesce(sum(cost_usd), 0)::float AS spent
    FROM shared.llm_call_log
    WHERE provider = 'typesafe' AND $1::date IS NOT NULL AND created_at >= $1::date
"""
# A real (non-test) low bell in the last 24 h, read or not, blocks a new one. Not `is_read = FALSE` like the
# AA-720/AA-627 bells: is_read is one flag for every staff user, so after anyone opened the bell (or "mark all
# read") the 30-min sampler check would write a new low bell every 30 min while the estimate stays low (S246
# review). The estimate does not recover on its own, so one bell a day is the signal; a top-up ends it.
LOW_ALERT_RECENT_SQL = """
    SELECT 1 FROM shared.notifications
     WHERE event_type = $1 AND created_at >= now() - interval '24 hours'
       AND coalesce(payload->>'test', 'false') <> 'true'
     LIMIT 1
"""
# $4 = target_roles: admin + content for a real bell (as AA-720), admin only for a test bell (a content user,
# e.g. Ms. Thư, never sees a [TEST] bell).
LOW_ALERT_INSERT_SQL = """
    INSERT INTO shared.notifications
        (tenant_id, actor_type, event_type, entity_type, entity_id, payload, target_roles)
    VALUES ($1::uuid, 'system', $2, 'jev_account', 'typesafe', $3::jsonb, $4::text[])
"""
REAL_ALERT_ROLES = ["admin", "content"]
TEST_ALERT_ROLES = ["admin"]
LAST_LOW_ALERT_SQL = """
    SELECT max(created_at) AS last_at FROM shared.notifications
     WHERE event_type = $1 AND coalesce(payload->>'test', 'false') <> 'true'
"""
# Serialises the check-then-insert across api tasks and the admin route (the AA-756 race), distinct from the
# exhausted alert's key (756_020_720) and the job-claim lock.
_LOW_ALERT_LOCK_KEY = 845_020_003


def estimate_from(total: Optional[float], spent: Optional[float]) -> Optional[float]:
    """total − spent, or None when there is no top-up (pure, unit-tested)."""
    if total is None or spent is None:
        return None
    return round(float(total) - float(spent), 4)


def is_below(estimated_left: Optional[float], threshold_usd: float) -> bool:
    """True only for a known estimate strictly below the threshold."""
    return estimated_left is not None and estimated_left < threshold_usd


def low_message(estimated_left: float, threshold_usd: float, *, test: bool = False) -> str:
    text = (f"Jev (TypeSafe) credit is running low: an estimated ${estimated_left:.2f} is left, below the "
            f"${threshold_usd:.2f} alert threshold. Top up the TypeSafe account (thu@adventure.asia) and record "
            "the top-up in AI providers › Jev credit. The estimate is the recorded top-ups minus the Jev spend "
            "logged since the first top-up (TypeSafe has no balance API). When credit runs out, Jev calls fail "
            "with 402 and the stages fall back to their rules.")
    return f"[TEST] {text}" if test else text


async def estimate(conn) -> dict[str, Any]:
    """{total_topped_up_usd, first_topup_on, spent_since_first_topup_usd, estimated_left_usd}; nulls when
    no top-up is recorded. `conn` is an asyncpg connection or pool (fetchrow only)."""
    tot = await conn.fetchrow(TOPUP_TOTAL_SQL)
    total = tot["total"] if tot else None
    first_on = tot["first_on"] if tot else None
    spent = None
    if first_on is not None:
        row = await conn.fetchrow(JEV_SPEND_SQL, first_on)
        spent = float(row["spent"]) if row else 0.0
    return {
        "total_topped_up_usd": total,
        "first_topup_on": first_on.isoformat() if first_on else None,
        "spent_since_first_topup_usd": spent,
        "estimated_left_usd": estimate_from(total, spent),
    }


async def _maybe_insert_low_bell(conn, estimated_left: float, threshold_usd: float, *, test: bool,
                                 source: str) -> bool:
    """One locked check-then-insert. A test bell skips the throttle (an admin asked for it) but is marked."""
    payload = json.dumps({"message": low_message(estimated_left, threshold_usd, test=test),
                          "estimated_left_usd": round(estimated_left, 2), "threshold_usd": threshold_usd,
                          "source": source, "test": test})
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock($1)", _LOW_ALERT_LOCK_KEY)
        if not test and await conn.fetchval(LOW_ALERT_RECENT_SQL, JEV_CREDIT_LOW_EVENT):
            return False
        await conn.execute(LOW_ALERT_INSERT_SQL, PLATFORM_TENANT_ID, JEV_CREDIT_LOW_EVENT, payload,
                           TEST_ALERT_ROLES if test else REAL_ALERT_ROLES)
    return True


async def check_low_credit(pool, *, source: str, threshold_usd: Optional[float] = None,
                           now: Optional[datetime] = None) -> dict[str, Any]:
    """Compare the estimate with the threshold; below it, raise the throttled bell and queue the e-mail.

    `threshold_usd` None = the real check at LOW_CREDIT_THRESHOLD_USD. A value = a test check (bell and
    e-mail marked test). Returns the estimate plus {threshold_usd, below_threshold, alerted, email_job_id,
    test}. Raises only for a failed estimate read (the caller logs it); a failed bell or e-mail is logged and
    reported as alerted False / email_job_id None."""
    from shared import ops_alerts   # lazy: ops_alerts → ses_mailer, not needed by every importer

    test = threshold_usd is not None
    threshold = LOW_CREDIT_THRESHOLD_USD if threshold_usd is None else float(threshold_usd)
    async with pool.acquire() as conn:
        est = await estimate(conn)
        left = est["estimated_left_usd"]
        below = is_below(left, threshold)
        alerted, email_job_id = False, None
        if below:
            try:
                alerted = await _maybe_insert_low_bell(conn, left, threshold, test=test, source=source)
            except Exception as exc:  # noqa: BLE001 — the bell is best effort, the result still says below
                logger.warning("jev_credit_low_alert_insert_failed", error=f"{type(exc).__name__}: {exc}"[:200])
            if alerted:
                try:
                    email_job_id = await ops_alerts.enqueue_jev_credit_email(
                        conn, template=ops_alerts.TEMPLATE_JEV_CREDIT_LOW, test=test, now=now,
                        details={"estimated_left_usd": round(left, 2), "threshold_usd": threshold,
                                 "total_topped_up_usd": est["total_topped_up_usd"],
                                 "spent_since_first_topup_usd": est["spent_since_first_topup_usd"],
                                 "first_topup_on": est["first_topup_on"], "source": source,
                                 "message": low_message(left, threshold, test=test)})
                except Exception as exc:  # noqa: BLE001 — the bell is written; the mail is a second channel
                    logger.warning("jev_credit_low_email_enqueue_failed",
                                   error=f"{type(exc).__name__}: {exc}"[:200])
    out = {**est, "threshold_usd": threshold, "below_threshold": below, "alerted": alerted,
           "email_job_id": email_job_id, "test": test}
    if alerted:
        logger.warning("jev_credit_low_alert", source=source, estimated_left_usd=left, threshold_usd=threshold,
                       test=test, email_job_id=email_job_id)
    else:
        logger.info("jev_credit_low_checked", source=source, estimated_left_usd=left, threshold_usd=threshold,
                    below=below, test=test)
    return out
