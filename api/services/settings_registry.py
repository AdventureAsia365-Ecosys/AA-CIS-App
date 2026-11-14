"""S279 thresholds L1 — the settings registry service behind Platform › Settings (CIS 407; router
api/routers/admin_settings_registry.py; reader shared/settings).

- ``list_settings``: every definition of an app (CIS: the compiled defaults, equal to the migration seed) with the value
  in force and where it comes from (env / tenant / platform / default), its bounds, the last decided change, the open
  proposal, the "overridden by env" and "not enforced yet" flags, and (a tenant-scoped key) ``tenant_overrides``: the
  approved tenant value in force of each tenant (AA-1096, S280).
- ``history``: every value row of a key, newest first, each with the approved value it replaced (the page's diff).
- ``propose``: a new value (validated against the definition first: 422 with the reason), optimistic lock on the
  approved version the person saw (409 when it moved), one open proposal per key and scope (409). ``apply=True`` also
  approves it in the same transaction when the caller may.
- ``approve`` / ``reject``: decide an open proposal. maker_checker: the approver is not the proposer, unless the
  setting ``cis.settings.two_person_approval`` is off (dev phase, tech lead S279 Q2): then the administrator may approve
  their own change (``self_approved``), and a high-impact one queues an e-mail to the tech lead (ops recipients).
  An approval whose ``effective_from`` has come supersedes the older approved rows of the same scope. An approval
  re-checks the value against its definition and, for a quality key, the rules between keys against the approved rows
  read in its transaction (409 ``cross_check``; S279 L2 review).

Every write leaves one acp_shared.audit_log row in its transaction (settings.value_proposed / _approved / _rejected,
tenant_id = the row's tenant or NULL, details: before / after / reason / evidence). The reader of this process is
invalidated after an approval; other processes see it within one cache window (30 s).
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import asyncpg
import structlog

from services.acp_shared.audit_log import PlatformAuditAction, write_audit_log
from shared import settings
from shared.settings.reader import Definition

logger = structlog.get_logger()

RESOURCE = "setting"
TWO_PERSON_KEY = "cis.settings.two_person_approval"
APPS = ("cis", "tripplanner", "booking", "crm")
NOT_MIGRATED = "The settings registry is not available yet: the database update (CIS 407) is not applied"
REASON_MIN = 20  # technical-constant: mirrors the CHECK of CIS 407 (one break-glass minimum for the platform)


class SettingsError(Exception):
    def __init__(self, status: int, detail: str, code: str = "refused") -> None:
        super().__init__(status, detail, code)
        self.status, self.detail, self.code = status, detail, code

    def __str__(self) -> str:
        return self.detail


def _iso(v: Any) -> Optional[str]:
    return v.astimezone(timezone.utc).isoformat() if isinstance(v, datetime) else v


def _json(text: Optional[str]) -> Any:
    return None if text is None else json.loads(text)


def _definition_out(d: Definition) -> dict:
    return {"key": d.key, "app": d.app, "area": d.area, "label": d.label, "description": d.description,
            "value_type": d.value_type, "unit": d.unit, "min_value": d.min_value, "max_value": d.max_value,
            "allowed_values": list(d.allowed_values) if d.allowed_values is not None else None,
            "default_value": d.default_value, "scope_kind": d.scope_kind, "impact": d.impact, "approval": d.approval,
            "evidence_required": d.evidence_required, "env_override": d.env_override, "cache_s": d.cache_s,
            "enforced": d.enforced, "guide": settings.guide(d.key)}


def _row_out(r: Any) -> dict:
    return {"id": str(r["id"]), "key": r["key"], "tenant_id": r["tenant_id"], "value": _json(r["value_text"]),
            "version": r["version"], "status": r["status"], "effective_from": _iso(r["effective_from"]),
            "proposed_by": r["proposed_by"], "proposed_at": _iso(r["proposed_at"]), "reason": r["reason"],
            "evidence_ref": r["evidence_ref"], "self_approved": r["self_approved"], "decided_by": r["decided_by"],
            "decided_at": _iso(r["decided_at"]), "decision_note": r["decision_note"]}


_ROW_COLS = """v.id, v.key, v.tenant_id::text AS tenant_id, v.value::text AS value_text, v.version, v.status,
               v.effective_from, v.proposed_by, v.proposed_at, v.reason, v.evidence_ref, v.self_approved,
               v.decided_by, v.decided_at, v.decision_note"""

_LATEST_SQL = f"""
    SELECT DISTINCT ON (v.key) {_ROW_COLS}
      FROM shared.setting_value v
     WHERE v.key LIKE $1 AND v.tenant_id IS NULL AND v.status IN ('approved', 'rejected', 'superseded')
     ORDER BY v.key, v.decided_at DESC
"""
_OPEN_SQL = f"SELECT {_ROW_COLS} FROM shared.setting_value v WHERE v.key LIKE $1 AND v.status = 'proposed' " \
            "ORDER BY v.proposed_at"
# AA-1096 (S280): the approved tenant values in force of every tenant-scoped key, one row per (key, tenant): the page
# counts them on the row and names each tenant's value (the reader's own resolution order: latest in force wins).
_TENANT_SQL = """
    SELECT DISTINCT ON (v.key, v.tenant_id) v.key, v.tenant_id::text AS tenant_id, v.value::text AS value_text,
           v.version, v.effective_from
      FROM shared.setting_value v
     WHERE v.tenant_id IS NOT NULL AND v.status = 'approved' AND v.effective_from <= now()
     ORDER BY v.key, v.tenant_id, v.effective_from DESC, v.version DESC
"""
_HISTORY_SQL = f"""
    SELECT {_ROW_COLS} FROM shared.setting_value v
     WHERE v.key = $1 AND v.tenant_id IS NOT DISTINCT FROM $2::uuid
     ORDER BY v.version DESC
     LIMIT 200
"""
_CURRENT_VERSION_SQL = """
    SELECT version, value::text AS value_text FROM shared.setting_value
     WHERE key = $1 AND tenant_id IS NOT DISTINCT FROM $2::uuid AND status = 'approved' AND effective_from <= now()
     ORDER BY effective_from DESC, version DESC LIMIT 1
"""
_MAX_VERSION_SQL = "SELECT coalesce(max(version), 0) FROM shared.setting_value " \
                   "WHERE key = $1 AND tenant_id IS NOT DISTINCT FROM $2::uuid"
_LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtextextended($1, 407))"
# The two-person flag in force, read inside the decision transaction (never the 30 s process cache): the latest
# approved platform row in force, else the definition's default.
_TWO_PERSON_SQL = """
    SELECT value::text AS value_text FROM shared.setting_value
     WHERE key = $1 AND tenant_id IS NULL AND status = 'approved' AND effective_from <= now()
     ORDER BY effective_from DESC, version DESC LIMIT 1
"""


# AA-1096 l23: the page shows who proposed / decided a value by name (the rows keep admin:<uuid>, CIS 407). One read of
# the staff rows the page shows; an actor that is not an admin uuid (a migration, a script) keeps its id.
ADMIN_ACTOR = re.compile(r"^admin:([0-9a-fA-F-]{36})$")
_NAMES_SQL = """
    SELECT id::text AS id, coalesce(nullif(btrim(display_name), ''), email, username) AS name
      FROM shared.admin_users WHERE id = ANY($1::uuid[])
"""


async def _actor_names(conn: Any, rows: list[dict]) -> None:
    """Add ``proposed_by_name`` / ``decided_by_name`` (None = no staff row) to each row, in place."""
    ids = {m.group(1).lower() for r in rows for a in (r.get("proposed_by"), r.get("decided_by"))
           if a and (m := ADMIN_ACTOR.match(a))}
    names = {r["id"]: r["name"] for r in await conn.fetch(_NAMES_SQL, sorted(ids))} if ids else {}
    for r in rows:
        for field in ("proposed_by", "decided_by"):
            m = ADMIN_ACTOR.match(r.get(field) or "")
            r[f"{field}_name"] = names.get(m.group(1).lower()) if m else None


async def _tenant_values(conn: Any, defs: list[Definition]) -> dict[str, list[dict]]:
    """key -> the approved tenant values in force (tenant_id, value, version, effective_from); read only when the app
    has a tenant-scoped key."""
    out: dict[str, list[dict]] = {}
    if not any(d.tenant_scoped for d in defs):
        return out
    for r in await conn.fetch(_TENANT_SQL):
        out.setdefault(r["key"], []).append({"tenant_id": r["tenant_id"], "value": _json(r["value_text"]),
                                             "version": r["version"], "effective_from": _iso(r["effective_from"])})
    return out


def _app_definitions(app: Optional[str], area: Optional[str]) -> list[Definition]:
    out = []
    for key in settings.READER.keys():
        d = settings.definition(key)
        if (app is None or d.app == app) and (area is None or d.area == area):
            out.append(d)
    return out


async def list_settings(pool: Any, *, app: Optional[str] = "cis", area: Optional[str] = None) -> dict:
    if app is not None and app not in APPS:
        raise SettingsError(422, f"app must be one of {list(APPS)}", "bad_app")
    defs = _app_definitions(app, area)
    try:
        await settings.READER.refresh(force=True, pool=pool)
        async with pool.acquire() as conn:
            latest = {r["key"]: _row_out(r) for r in await conn.fetch(_LATEST_SQL, "%")}
            open_rows: dict[str, list] = {}
            for r in await conn.fetch(_OPEN_SQL, "%"):
                open_rows.setdefault(r["key"], []).append(_row_out(r))
            tenant_rows = await _tenant_values(conn, defs)
            await _actor_names(conn, [*latest.values(), *(r for rs in open_rows.values() for r in rs)])
    except asyncpg.exceptions.UndefinedTableError:
        raise SettingsError(503, NOT_MIGRATED, "not_migrated") from None
    rows = []
    for d in defs:
        res = settings.resolve(d.key)
        env_value, env_ignored, env_set = settings.READER.env_value(d)
        rows.append({
            **_definition_out(d),
            "value": res.value, "source": res.source, "version": res.version,
            "overridden_by_env": res.source == "env", "env_ignored": env_ignored if env_set else None,
            "refused": res.refused, "last_change": latest.get(d.key), "proposals": open_rows.get(d.key, []),
            "tenant_overrides": tenant_rows.get(d.key, []) if d.tenant_scoped else [],
        })
    areas = sorted({d.area for d in defs})
    return {"app": app, "areas": areas, "settings": rows, "two_person_approval": bool(settings.get(TWO_PERSON_KEY)),
            "reader_error": settings.READER.last_error}


async def history(pool: Any, key: str, tenant_id: Optional[str] = None) -> dict:
    d = _definition(key)
    try:
        async with pool.acquire() as conn:
            rows = [_row_out(r) for r in await conn.fetch(_HISTORY_SQL, key, tenant_id)]
            await _actor_names(conn, rows)
    except asyncpg.exceptions.UndefinedTableError:
        raise SettingsError(503, NOT_MIGRATED, "not_migrated") from None
    # the page's diff: each row against the approved value in force before it (oldest first walk)
    previous: Any = d.default_value
    for row in reversed(rows):
        row["previous_value"] = previous
        if row["status"] in ("approved", "superseded"):
            previous = row["value"]
    return {"key": key, "tenant_id": tenant_id, "definition": _definition_out(d), "history": rows}


def _definition(key: str) -> Definition:
    try:
        return settings.definition(key)
    except KeyError:
        raise SettingsError(404, f"Unknown setting {key}", "unknown_setting") from None


def _check_value(d: Definition, value: Any) -> Any:
    problem = settings.validate(d, value)
    if problem:
        raise SettingsError(422, f"{d.label}: {problem}", "out_of_bounds")
    # S279 L2: the rules between quality keys (a cap under the pass line, a band's minimum under its maximum, an aim
    # inside the meta band, a Channel bar's fields), checked against the other values in force
    from services.quality import quality_settings

    problem = quality_settings.cross_problem(d.key, value)
    if problem:
        raise SettingsError(422, f"{d.label}: {problem}", "cross_check")
    return value


# S279 L2 review: the rules between quality keys are re-checked when a proposal is approved, against the approved
# platform rows read in the decision transaction (never the 30 s process cache): the values in force now
# ($3 = false) and the values once every future-dated approval is in force ($3 = true). One advisory lock serializes
# the approvals of quality keys, so two approvals of related keys cannot each miss the other.
_CROSS_LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtextextended('cis.quality.cross', 407))"
_APPROVED_SQL = """
    SELECT DISTINCT ON (key) key, value::text AS value_text FROM shared.setting_value
     WHERE key = ANY($1::text[]) AND tenant_id IS NULL AND status = 'approved' AND id <> $2::uuid
       AND ($3 OR effective_from <= now())
     ORDER BY key, effective_from DESC, version DESC
"""


async def _recheck_on_approval(conn: Any, d: Definition, proposal: dict) -> None:
    """Refuse (409) an approval whose value no longer fits its definition or that breaks a rule between quality keys
    with the other approved values, now or once a future-dated approval takes effect."""
    value = proposal["value"]
    problem = settings.validate(d, value)
    if problem:
        raise SettingsError(409, f"{d.label}: {problem}", "out_of_bounds")
    from services.quality import quality_settings, settings_keys

    if not quality_settings.governs(d.key):
        return
    await conn.execute(_CROSS_LOCK_SQL)
    keys = list(settings_keys.ALL_KEYS)
    for eventual in (False, True):
        rows = {r["key"]: _json(r["value_text"]) for r in await conn.fetch(_APPROVED_SQL, keys, proposal["id"],
                                                                           eventual)}
        problem = quality_settings.cross_problem(
            d.key, value, get=lambda k, rows=rows: rows[k] if k in rows else settings.definition(k).default_value)
        if problem:
            when = "once the approved future-dated changes take effect" if eventual else "with the values in force"
            raise SettingsError(409, f"{d.label}: {problem} ({when}); propose a value that fits", "cross_check")


def _resource_id(key: str, tenant_id: Optional[str]) -> str:
    return key if tenant_id is None else f"{key}@{tenant_id}"


async def propose(pool: Any, key: str, *, value: Any, reason: str, actor: str, base_version: Optional[int],
                  evidence_ref: Optional[str] = None, effective_from: Optional[datetime] = None,
                  tenant_id: Optional[str] = None, apply: bool = False, can_approve: bool = False) -> dict:
    d = _definition(key)
    _check_value(d, value)
    reason = (reason or "").strip()
    if len(reason) < REASON_MIN:
        raise SettingsError(422, f"Give a reason of at least {REASON_MIN} characters", "reason_too_short")
    evidence_ref = (evidence_ref or "").strip() or None
    if d.evidence_required and not evidence_ref:
        raise SettingsError(422, f"{d.label} needs an evidence link (an eval or replay run)", "evidence_required")
    if tenant_id is not None and not d.tenant_scoped:
        raise SettingsError(422, f"{d.label} has no per-tenant value", "not_tenant_scoped")
    if apply and not can_approve:
        raise SettingsError(403, "You may propose this change, not approve it", "approve_not_allowed")
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(_LOCK_SQL, _resource_id(key, tenant_id))
                current = await conn.fetchrow(_CURRENT_VERSION_SQL, key, tenant_id)
                current_version = current["version"] if current else 0
                if (base_version or 0) != current_version:
                    raise SettingsError(409, f"{d.label} changed since you opened it (version {current_version}); "
                                             "reload and try again", "stale_version")
                version = int(await conn.fetchval(_MAX_VERSION_SQL, key, tenant_id)) + 1
                try:
                    row = await conn.fetchrow(
                        f"""INSERT INTO shared.setting_value AS v
                                (key, tenant_id, value, version, effective_from, proposed_by, reason, evidence_ref)
                            VALUES ($1, $2::uuid, $3::jsonb, $4, coalesce($5, now()), $6, $7, $8)
                            RETURNING {_ROW_COLS}""",
                        key, tenant_id, json.dumps(value), version, effective_from, actor, reason, evidence_ref)
                except asyncpg.exceptions.UniqueViolationError:
                    raise SettingsError(409, f"{d.label} already has an open proposal: approve or reject it first",
                                        "open_proposal") from None
                out = _row_out(row)
                before = _json(current["value_text"]) if current else d.default_value
                audits = [(PlatformAuditAction.SETTING_PROPOSED,
                           {"value_id": out["id"], "version": version, "before": before, "after": value,
                            "reason": reason, "evidence_ref": evidence_ref, "effective_from": out["effective_from"]})]
                if apply:
                    out, decided = await _decide(conn, d, out, actor=actor, approve=True, note=None)
                    audits.append(decided)
                # QRY-4: the audit rows go last (the chain trigger holds a transaction-scoped lock)
                for action, details in audits:
                    await write_audit_log(conn, tenant_id=tenant_id, actor=actor, action=action,
                                          resource_type=RESOURCE, resource_id=_resource_id(key, tenant_id),
                                          details=details)
    except asyncpg.exceptions.UndefinedTableError:
        raise SettingsError(503, NOT_MIGRATED, "not_migrated") from None
    if out["status"] == "approved":
        settings.READER.invalidate()
    return out


async def decide(pool: Any, value_id: str, *, actor: str, approve: bool, note: Optional[str] = None) -> dict:
    try:
        value_id = str(uuid.UUID(str(value_id)))
    except ValueError:
        raise SettingsError(404, "Unknown proposal", "unknown_proposal") from None
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(f"SELECT {_ROW_COLS} FROM shared.setting_value v WHERE v.id = $1::uuid "
                                          "FOR UPDATE", value_id)
                if row is None:
                    raise SettingsError(404, "Unknown proposal", "unknown_proposal")
                proposal = _row_out(row)
                if proposal["status"] != "proposed":
                    raise SettingsError(409, f"This proposal is already {proposal['status']}", "already_decided")
                d = _definition(proposal["key"])
                out, (action, details) = await _decide(conn, d, proposal, actor=actor, approve=approve, note=note)
                await write_audit_log(conn, tenant_id=out["tenant_id"], actor=actor, action=action,
                                      resource_type=RESOURCE, resource_id=_resource_id(out["key"], out["tenant_id"]),
                                      details=details)
    except asyncpg.exceptions.UndefinedTableError:
        raise SettingsError(503, NOT_MIGRATED, "not_migrated") from None
    if out["status"] == "approved":
        settings.READER.invalidate()
    return out


async def two_person_on(conn: Any) -> bool:
    """Is two-person approval on, read in the caller's transaction (S279 review: the process cache could be empty or
    up to one cache window old, which failed open). Fails closed: an unreadable row or a value that is not a boolean
    counts as on."""
    try:
        async with conn.transaction():          # a savepoint: a failed read leaves the decision transaction usable
            row = await conn.fetchrow(_TWO_PERSON_SQL, TWO_PERSON_KEY)
    except asyncpg.exceptions.UndefinedTableError:
        raise
    except Exception as exc:  # fail closed
        logger.warning("settings_two_person_read_failed", error=repr(exc)[:200])
        return True
    if row is None:
        return settings.definition(TWO_PERSON_KEY).default_value is not False
    try:
        value = json.loads(row["value_text"])
    except (TypeError, ValueError):
        return True
    return value is not False


async def _decide(conn: Any, d: Definition, proposal: dict, *, actor: str, approve: bool,
                  note: Optional[str]) -> tuple[dict, tuple[str, dict]]:
    """Decide ``proposal`` inside the caller's transaction; returns the row and the audit row (action, details) the
    caller writes last."""
    note = (note or "").strip() or None
    if not approve and (note is None or len(note) < 3):
        raise SettingsError(422, "Say why the proposal is rejected", "note_required")
    if approve:
        await _recheck_on_approval(conn, d, proposal)
    self_approved = False
    if approve and actor == proposal["proposed_by"] and d.approval == "maker_checker":
        # The flag itself is maker_checker (CIS 407): while it is on, the one person it checks cannot turn it off.
        if await two_person_on(conn):
            raise SettingsError(409, f"{d.label} needs a second person to approve (two-person approval is on)",
                                "second_person_required")
        self_approved = True
    status = "approved" if approve else "rejected"
    row = await conn.fetchrow(
        f"""UPDATE shared.setting_value AS v SET status = $2, decided_by = $3, decided_at = now(), decision_note = $4,
                   self_approved = $5
             WHERE v.id = $1::uuid AND v.status = 'proposed'
            RETURNING {_ROW_COLS}""",
        proposal["id"], status, actor, note, self_approved)
    if row is None:
        raise SettingsError(409, "This proposal was decided meanwhile", "already_decided")
    out = _row_out(row)
    before = None
    if approve:
        prior = await conn.fetchrow(
            """SELECT value::text AS value_text FROM shared.setting_value
                WHERE key = $1 AND tenant_id IS NOT DISTINCT FROM $2::uuid AND status = 'approved' AND id <> $3::uuid
                  AND effective_from <= now() ORDER BY effective_from DESC, version DESC LIMIT 1""",
            out["key"], out["tenant_id"], out["id"])
        before = _json(prior["value_text"]) if prior else d.default_value
        # the new value supersedes the older approved rows once it is in force (a future-dated one waits)
        await conn.execute(
            """UPDATE shared.setting_value SET status = 'superseded'
                WHERE key = $1 AND tenant_id IS NOT DISTINCT FROM $2::uuid AND status = 'approved' AND id <> $3::uuid
                  AND $4::timestamptz <= now() AND effective_from <= $4::timestamptz""",
            out["key"], out["tenant_id"], out["id"], row["effective_from"])
        if self_approved and d.impact == "high":
            from shared import ops_alerts

            await ops_alerts.enqueue_setting_self_approved_email(conn, details={
                "key": d.key, "label": d.label, "value_id": out["id"], "before": before, "after": out["value"],
                "reason": out["reason"], "actor": actor, "effective_from": out["effective_from"]})
    logger.info("setting_value_decided", key=out["key"], status=status, version=out["version"],
                self_approved=self_approved)
    action = PlatformAuditAction.SETTING_APPROVED if approve else PlatformAuditAction.SETTING_REJECTED
    return out, (action, {"value_id": out["id"], "version": out["version"], "before": before, "after": out["value"],
                          "self_approved": self_approved, "note": note, "proposed_by": out["proposed_by"]})


# ── tighten-only (tech lead S279 Q1) ─────────────────────────────────────────────────────────────────────────────
# The delegated pages keep their roles (ai_ops: AI gateway budgets; customer_success: a tenant's RPM and publish limit),
# but a role without settings.write may only tighten a cap there: lower it, add one, or turn a hard stop on; never
# raise, remove or soften it. The administrator (settings.write) may do both.

TIGHTEN_ONLY = ("Your role may only tighten this cap (lower it or turn the hard stop on); ask an administrator to "
                "raise it")


def tighten_only(role: Optional[str]) -> bool:
    """True for a staff role that may only tighten caps (not the administrator; the machine credential is not a role
    and keeps its own route checks)."""
    from api.core.staff_authz import SETTINGS_WRITE, has_perm

    return isinstance(role, str) and bool(role) and not has_perm(role, SETTINGS_WRITE)


def loosened(before: Optional[dict], after: dict, *, caps: tuple = (), stops: tuple = (),
             up_is_looser: tuple = (), down_is_looser: tuple = ()) -> list[str]:
    """The fields ``after`` loosens against ``before`` (a row or None = no row yet). ``caps``: a money / count cap
    where None = no cap (None after a value, or a higher value, loosens); ``stops``: booleans where True is the strict
    side (True -> False loosens); ``up_is_looser``: thresholds where a higher number acts later (alert / approval
    percentages, a retry count); ``down_is_looser``: alert floors where a lower number (or None = no alert) acts later
    (a low-balance alert). No row before: only a missing stop flag counts (a new cap can only tighten). Pure."""
    if before is None:
        return [f for f in stops if after.get(f) is False]
    out = []
    for f in caps:
        old, new = before.get(f), after.get(f)
        if old is not None and (new is None or float(new) > float(old)):
            out.append(f)
    for f in stops:
        if bool(before.get(f)) and after.get(f) is False:
            out.append(f)
    for f in up_is_looser:
        old, new = before.get(f), after.get(f)
        if old is not None and new is not None and float(new) > float(old):
            out.append(f)
    for f in down_is_looser:
        old, new = before.get(f), after.get(f)
        if old is not None and (new is None or float(new) < float(old)):
            out.append(f)
    return out


def refuse_loosening(role: Optional[str], before: Optional[dict], after: dict, **kinds: tuple) -> None:
    """Raise PermissionError (the routes answer 403) when ``role`` is tighten-only and ``after`` loosens ``before``
    (``kinds``: the ``loosened`` field groups). The administrator and the machine pass."""
    if not tighten_only(role):
        return
    worse = loosened(before, after, **kinds)
    if worse:
        raise PermissionError(f"{TIGHTEN_ONLY}: {', '.join(worse)}")


__all__ = ["APPS", "NOT_MIGRATED", "RESOURCE", "SettingsError", "TIGHTEN_ONLY", "TWO_PERSON_KEY", "decide",
           "history", "list_settings", "loosened", "propose", "refuse_loosening", "tighten_only", "two_person_on"]
