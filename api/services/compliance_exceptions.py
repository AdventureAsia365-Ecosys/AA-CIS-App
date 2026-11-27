"""AA-1173 (S290, compliance readiness phase 3, CIS 479) — exceptions (accepted risks) and the monthly posture review of
Platform › Compliance (/admin/platform/compliance, tabs Exceptions and Reviews).

Exceptions (compliance.exception). One person proposes (subject = a control id, a check id or a vulnerability id; a
title, the reason, an optional compensating control, the risk level, the expiry date), a second person approves or
rejects (maker-checker). While the platform setting ``cis.settings.two_person_approval`` is off (tech lead S287 Q13: one
tech lead for now) the proposer may approve their own proposal; the row then says ``self_approved`` (the same rule as
Platform › Settings, api/services/settings_registry.py). The expiry may be at most ``cis.compliance.exception_max_days``
after today, checked at proposal and again at approval (a setting lowered meanwhile refuses an old, longer proposal).
An approved exception can be revoked (with a note); "expired" is derived (approved, expires_on before today). The table
keeps every row (trigger: no delete, no edit of the proposal, only valid moves). An exception never raises a score:
the page shows the control as "accepted risk" next to its state.

Reviews (compliance.review). One "Security posture monthly review" per month: the numbers the page shows at that
moment (``summary``: scores per framework, controls by state, checks failing / stale, exceptions by state; counts and
ids only) plus the reviewer's notes and actions. Append-only, one per (kind, month): a second record of the same month
is a 409. The newest review date is published as the check ``monthly_review_recent`` (api/services/compliance_db.py),
graded against ``cis.compliance.review_max_age_days``.

Every write leaves one acp_shared.audit_log row in its transaction (compliance.exception_proposed / _approved /
_rejected / _revoked, compliance.review_recorded; tenant_id NULL, details: the row's ids and the decision). Actors are
staff ids ("admin:<id>") from a verified company sign-in (the router refuses password, secret-only and read-only E2E
sessions).

ComplianceRefused(status, detail, code) -> the HTTP answer (404 unknown, 409 conflict, 422 invalid).
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from api.services import compliance_settings as keys
from services.acp_shared.audit_log import write_audit_log
from shared import settings

SUBJECT_KINDS = ("control", "check", "vulnerability")
RISK_LEVELS = ("low", "medium", "high")
STATUSES = ("proposed", "approved", "rejected", "revoked")
REVIEW_KIND = "monthly_posture"
_SUBJECT_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$")
# technical-constant: these bounds mirror the CHECKs of CIS 479 (a 422 before the database refuses it)
TITLE_RANGE, REASON_RANGE, TEXT_MAX, NOTE_MAX = (3, 160), (20, 2000), 2000, 1000
REVIEW_NOTES_RANGE, REVIEW_ACTIONS_MAX = (20, 4000), 4000
AUDIT_RESOURCE_EXCEPTION, AUDIT_RESOURCE_REVIEW = "compliance_exception", "compliance_review"


class ComplianceRefused(Exception):
    def __init__(self, status: int, detail: str, code: str = "refused") -> None:
        super().__init__(status, detail, code)
        self.status, self.detail, self.code = status, detail, code

    def __str__(self) -> str:
        return self.detail


def _today(now: Optional[datetime] = None) -> date:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).date()


def _text(value: Any, name: str, lo: int, hi: int, required: bool = True) -> Optional[str]:
    text = value.strip() if isinstance(value, str) else ""
    if not text:
        if required:
            raise ComplianceRefused(422, f"{name} is required", f"{name}_required")
        return None
    if not lo <= len(text) <= hi:
        raise ComplianceRefused(422, f"{name} must be {lo} to {hi} characters", f"{name}_length")
    return text


def effective_status(row: dict, today: date) -> str:
    """proposed | approved | rejected | revoked | expired (approved and past its expiry)."""
    if row["status"] == "approved" and row["expires_on"] < today:
        return "expired"
    return row["status"]


def _out(row: Any, today: date) -> dict:
    r = dict(row)
    out = {k: (v.isoformat() if isinstance(v, (date, datetime)) else v) for k, v in r.items()}
    out["state"] = effective_status(r, today)
    out["days_left"] = (r["expires_on"] - today).days
    return out


SQL_COLS = ("exception_id, subject_kind, subject_ref, title, reason, compensating, risk_level, status, proposed_by, "
            "proposed_at, expires_on, decided_by, decided_at, decision_note, self_approved, revoked_by, revoked_at, "
            "revoke_note")
SQL_LIST = f"SELECT {SQL_COLS} FROM compliance.exception ORDER BY proposed_at DESC, exception_id DESC LIMIT $1"
SQL_ONE = f"SELECT {SQL_COLS} FROM compliance.exception WHERE exception_id = $1"
SQL_ACTIVE = (f"SELECT {SQL_COLS} FROM compliance.exception WHERE status = 'approved' AND expires_on >= $1 "
              "ORDER BY expires_on")
LIST_LIMIT = 500   # technical-constant: the page lists every exception; a few dozen a year
# AA-1190 My work: the open proposals (the Exceptions tab's "proposed" count) a checker may decide; $1 = the viewer's
# own proposals to leave out (two-person approval on), NULL = none left out.
PROPOSED_SUMMARY_SQL = ("SELECT count(*) AS n, min(proposed_at) AS oldest FROM compliance.exception "
                        "WHERE status = 'proposed' AND ($1::text IS NULL OR proposed_by <> $1)")


async def max_days() -> int:
    return int(await settings.aget(keys.EXCEPTION_MAX_DAYS))


async def list_exceptions(pool: Any, now: Optional[datetime] = None) -> dict:
    today = _today(now)
    async with pool.acquire() as conn:
        rows = [_out(r, today) for r in await conn.fetch(SQL_LIST, LIST_LIMIT)]
    counts = {s: sum(1 for r in rows if r["state"] == s) for s in (*STATUSES, "expired")}
    return {"exceptions": rows, "counts": counts, "max_days": await max_days(),
            "two_person_approval": await _two_person(pool)}


async def proposed_summary(conn: Any, *, viewer: Optional[str] = None) -> dict:
    """AA-1190 My work: the proposed exceptions ``viewer`` may approve. Their own count only while two-person approval
    is off (a recorded self-approval, as :func:`decide`); ``viewer`` None (not a verified sign-in) leaves none out."""
    own = viewer if viewer is not None and await _two_person(conn) else None
    row = await conn.fetchrow(PROPOSED_SUMMARY_SQL, own)
    return {"count": int(row["n"] or 0), "oldest_at": row["oldest"]}


async def active_by_subject(conn: Any, now: Optional[datetime] = None) -> dict[tuple[str, str], dict]:
    """(subject_kind, subject_ref) -> the approved, unexpired exception that ends last (for the control rows)."""
    today = _today(now)
    out: dict[tuple[str, str], dict] = {}
    for r in await conn.fetch(SQL_ACTIVE, today):
        out[(r["subject_kind"], r["subject_ref"])] = {"exception_id": r["exception_id"], "title": r["title"],
                                                      "expires_on": r["expires_on"].isoformat(),
                                                      "risk_level": r["risk_level"]}
    return out


async def _two_person(pool_or_conn: Any) -> bool:
    from api.services.settings_registry import two_person_on   # noqa: PLC0415 - one rule with Platform › Settings

    if hasattr(pool_or_conn, "acquire"):
        async with pool_or_conn.acquire() as conn:
            return await two_person_on(conn)
    return await two_person_on(pool_or_conn)


def _validate_expiry(expires_on: Any, today: date, limit: int) -> date:
    try:
        day = expires_on if isinstance(expires_on, date) else date.fromisoformat(str(expires_on)[:10])
    except ValueError as e:
        raise ComplianceRefused(422, "expires_on must be a date (YYYY-MM-DD)", "expires_on_invalid") from e
    if day <= today:
        raise ComplianceRefused(422, "The expiry must be after today", "expires_on_past")
    if day > today + timedelta(days=limit):
        raise ComplianceRefused(422, f"The expiry may be at most {limit} days ahead (setting "
                                f"{keys.EXCEPTION_MAX_DAYS})", "expires_on_too_far")
    return day


async def propose(pool: Any, body: dict, actor: str, now: Optional[datetime] = None) -> dict:
    today = _today(now)
    kind = body.get("subject_kind")
    if kind not in SUBJECT_KINDS:
        raise ComplianceRefused(422, f"subject_kind must be one of {', '.join(SUBJECT_KINDS)}", "subject_kind")
    ref = body.get("subject_ref")
    if not isinstance(ref, str) or not _SUBJECT_REF.fullmatch(ref.strip()):
        raise ComplianceRefused(422, "subject_ref must be a control, check or vulnerability id", "subject_ref")
    ref = ref.strip()
    risk = body.get("risk_level")
    if risk not in RISK_LEVELS:
        raise ComplianceRefused(422, f"risk_level must be one of {', '.join(RISK_LEVELS)}", "risk_level")
    title = _text(body.get("title"), "title", *TITLE_RANGE)
    reason = _text(body.get("reason"), "reason", *REASON_RANGE)
    compensating = _text(body.get("compensating"), "compensating", 1, TEXT_MAX, required=False)
    expires = _validate_expiry(body.get("expires_on"), today, await max_days())
    async with pool.acquire() as conn:
        async with conn.transaction():
            if kind == "control" and not await conn.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM compliance.control WHERE control_id = $1) AS ok", ref):
                raise ComplianceRefused(404, f"No control {ref}", "unknown_control")
            open_dup = await conn.fetchval(
                "SELECT exception_id FROM compliance.exception WHERE subject_kind = $1 AND subject_ref = $2 "
                "AND status = 'proposed' LIMIT 1", kind, ref)
            if open_dup:
                raise ComplianceRefused(409, f"An open proposal for {ref} exists already (#{open_dup})",
                                        "open_proposal")
            row = await conn.fetchrow(
                f"INSERT INTO compliance.exception (subject_kind, subject_ref, title, reason, compensating, "
                f"risk_level, proposed_by, expires_on) VALUES ($1, $2, $3, $4, $5, $6, $7, $8) RETURNING {SQL_COLS}",
                kind, ref, title, reason, compensating, risk, actor, expires)
            await write_audit_log(conn, tenant_id=None, actor=actor, action="compliance.exception_proposed",
                                  resource_type=AUDIT_RESOURCE_EXCEPTION, resource_id=str(row["exception_id"]),
                                  details={"subject_kind": kind, "subject_ref": ref, "risk_level": risk,
                                           "expires_on": expires.isoformat()})
    return _out(row, today)


async def _load(conn: Any, exception_id: int) -> dict:
    row = await conn.fetchrow(SQL_ONE + " FOR UPDATE", exception_id)
    if row is None:
        raise ComplianceRefused(404, f"No exception #{exception_id}", "unknown_exception")
    return dict(row)


async def decide(pool: Any, exception_id: int, actor: str, approve: bool, note: Optional[str] = None,
                 now: Optional[datetime] = None) -> dict:
    """Approve or reject an open proposal (maker-checker)."""
    today = _today(now)
    note = _text(note, "note", 3, NOTE_MAX, required=not approve)
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await _load(conn, exception_id)
            if row["status"] != "proposed":
                raise ComplianceRefused(409, f"Exception #{exception_id} is {row['status']}, not open",
                                        "already_decided")
            self_approved = False
            if actor == row["proposed_by"]:
                if not approve:
                    # a rejection is the checker's decision (CIS 479 exception_checker_check); the proposer withdraws
                    raise ComplianceRefused(409, "Withdraw your own proposal instead of rejecting it",
                                            "withdraw_instead")
                if await _two_person(conn):
                    raise ComplianceRefused(409, "A second person must approve this exception (two-person approval "
                                            "is on)", "second_person_required")
                self_approved = True
            if approve:
                _validate_expiry(row["expires_on"], today, await max_days())
            status = "approved" if approve else "rejected"
            out = await conn.fetchrow(
                f"UPDATE compliance.exception SET status = $2, decided_by = $3, decided_at = now(), "
                f"decision_note = $4, self_approved = $5 WHERE exception_id = $1 RETURNING {SQL_COLS}",
                exception_id, status, actor, note, self_approved)
            await write_audit_log(conn, tenant_id=None, actor=actor, action=f"compliance.exception_{status}",
                                  resource_type=AUDIT_RESOURCE_EXCEPTION, resource_id=str(exception_id),
                                  details={"before": "proposed", "after": status, "self_approved": self_approved,
                                           "subject_ref": row["subject_ref"], "note": note})
    return _out(out, today)


async def revoke(pool: Any, exception_id: int, actor: str, note: Any, now: Optional[datetime] = None) -> dict:
    """End an approved (or withdraw an open) exception before its expiry; the note says why."""
    today = _today(now)
    note = _text(note, "note", 3, NOTE_MAX)
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await _load(conn, exception_id)
            if row["status"] not in ("proposed", "approved"):
                raise ComplianceRefused(409, f"Exception #{exception_id} is {row['status']}", "not_revocable")
            out = await conn.fetchrow(
                f"UPDATE compliance.exception SET status = 'revoked', revoked_by = $2, revoked_at = now(), "
                f"revoke_note = $3 WHERE exception_id = $1 RETURNING {SQL_COLS}", exception_id, actor, note)
            await write_audit_log(conn, tenant_id=None, actor=actor, action="compliance.exception_revoked",
                                  resource_type=AUDIT_RESOURCE_EXCEPTION, resource_id=str(exception_id),
                                  details={"before": row["status"], "after": "revoked",
                                           "subject_ref": row["subject_ref"], "note": note})
    return _out(out, today)


# ── monthly review ──────────────────────────────────────────────────────────────────────────────────────────────────

SQL_REVIEWS = ("SELECT review_id, kind, period, summary, notes, actions, reviewed_by, reviewed_at "
               "FROM compliance.review ORDER BY period DESC, review_id DESC LIMIT $1")
SQL_LAST_REVIEW = "SELECT max(reviewed_at) AS last FROM compliance.review WHERE kind = $1"
REVIEW_LIST_LIMIT = 36   # technical-constant: three years of monthly records on one page


def _review_out(row: Any) -> dict:
    r = dict(row)
    r["summary"] = json.loads(r["summary"]) if isinstance(r["summary"], str) else r["summary"]
    return {k: (v.isoformat() if isinstance(v, (date, datetime)) else v) for k, v in r.items()}


def review_summary(overview: dict, exceptions: dict) -> dict:
    """The numbers a review records (pure): from the overview answer and the exception list. Counts and ids only."""
    return {
        "generated_at": overview.get("generated_at"),
        "frameworks": [{"framework": f["framework"], "strict_pct": f.get("strict_pct"),
                        "weighted_pct": f.get("weighted_pct"), "n_met": f.get("n_met"), "n_gap": f.get("n_gap"),
                        "n_stale": f.get("n_stale")} for f in overview.get("frameworks", [])],
        "controls": overview.get("controls", {}),
        "checks": overview.get("checks", {}),
        "attention": sorted({a["check_id"] for a in overview.get("attention", [])})[:100],
        "collectors_down": sorted(h["source"] for h in overview.get("heartbeat", [])
                                  if h.get("scheduled") and not h.get("ok")),
        "exceptions": exceptions.get("counts", {}),
        "exceptions_active": sorted(e["subject_ref"] for e in exceptions.get("exceptions", [])
                                    if e.get("state") == "approved")[:100],
    }


async def list_reviews(pool: Any, now: Optional[datetime] = None) -> dict:
    today = _today(now)
    async with pool.acquire() as conn:
        rows = [_review_out(r) for r in await conn.fetch(SQL_REVIEWS, REVIEW_LIST_LIMIT)]
    period = today.replace(day=1)
    return {"reviews": rows, "current_period": period.isoformat(),
            "current_recorded": any(r["period"] == period.isoformat() for r in rows),
            "max_age_days": int(await settings.aget(keys.REVIEW_MAX_AGE_DAYS))}


async def record_review(pool: Any, body: dict, actor: str, summary: dict, now: Optional[datetime] = None) -> dict:
    """Record this month's review (or the month named in ``period``, the current or the previous month only)."""
    today = _today(now)
    notes = _text(body.get("notes"), "notes", *REVIEW_NOTES_RANGE)
    actions = _text(body.get("actions"), "actions", 1, REVIEW_ACTIONS_MAX, required=False)
    current = today.replace(day=1)
    previous = (current - timedelta(days=1)).replace(day=1)
    raw = body.get("period")
    try:
        period = current if raw in (None, "") else date.fromisoformat(str(raw)[:10]).replace(day=1)
    except ValueError as e:
        raise ComplianceRefused(422, "period must be a date (YYYY-MM-DD)", "period_invalid") from e
    if period not in (current, previous):
        raise ComplianceRefused(422, "A review records the current or the previous month", "period_out_of_range")
    data = json.dumps(summary, sort_keys=True, default=str)
    if len(data.encode()) > 16384:   # technical-constant: CIS 479 review_summary_check
        raise ComplianceRefused(422, "The review summary is too large", "summary_too_large")
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "INSERT INTO compliance.review (kind, period, summary, notes, actions, reviewed_by) "
                "VALUES ($1, $2, $3::jsonb, $4, $5, $6) ON CONFLICT (kind, period) DO NOTHING "
                "RETURNING review_id, kind, period, summary, notes, actions, reviewed_by, reviewed_at",
                REVIEW_KIND, period, data, notes, actions, actor)
            if row is None:
                raise ComplianceRefused(409, f"The review of {period:%Y-%m} is recorded already", "review_exists")
            await write_audit_log(conn, tenant_id=None, actor=actor, action="compliance.review_recorded",
                                  resource_type=AUDIT_RESOURCE_REVIEW, resource_id=str(row["review_id"]),
                                  details={"period": period.isoformat(), "kind": REVIEW_KIND})
    return _review_out(row)
