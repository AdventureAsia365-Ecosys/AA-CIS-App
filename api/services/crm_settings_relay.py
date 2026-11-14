"""AA-1096 lane L4 — the CRM tab of Platform › Settings: the CIS relay to the partner CRM's settings registry
(crm.ops.v1 1.13, AA-Partner crm-064; S279 thresholds audit section 5.3: "each tab calls the owning app's settings
API; CIS stays the single admin console and the owner keeps its table").

The CRM keeps its settings in schema ``crm`` (root ADR 0008a: no shared table), so CIS never reads them: every call goes
through ``crm_console.crm_ops_call`` with the signed-in staff member's own WORKFORCE token (the L1 console pattern; the
CRM's route class checks the group itself: cis-admin or crm-platform-admin read, crm-platform-admin writes).

- ``list_settings`` / ``history`` answer the page's shapes (``RegistryResp`` / ``HistoryResp`` of
  frontend/lib/settingsRegistry.ts): the CRM calls the open changes ``pending`` (crm.ops.v1 forbids the word
  "proposal" in a key); they are renamed ``proposals`` here.
- ``propose`` / ``decide`` relay a write (``/settings/{key}/changes``, ``/settings/changes/{id}/approve|reject``) and,
  when the CRM took it, write the CIS audit row of the change (``acp_shared.audit_log``, the same actions as a CIS key,
  ``details.app = crm``): the CRM's ``crm.setting_value`` row keeps the CRM staff actor, CIS records the CIS admin.
- A CRM refusal is passed on with its status and code (``{code, message}``, the page shows the message); a network
  failure is 503 ``crm_unavailable`` (crm_console).
- S280 UX fix round: the list carries ``can_write`` (the CRM's ``/console`` answer for this staff member: only
  ``crm-platform-admin`` writes; None = the CRM did not say, the page then leaves the buttons and the CRM decides), the
  rows name their CRM staff actor (``platform:<Cognito sub>``, the same workforce pool as CIS staff) by the CIS staff
  name (``proposed_by_name`` / ``decided_by_name``, the CIS rows' fields), and a change dated in the past is refused
  before it is relayed (``settings_registry.effective_start``, the CIS rule).
- S280 fix round, second pass: the agencies' own values and open changes (crm.ops.v1 1.14 ``agency_overrides`` /
  ``agency_pending``) are relayed as ``tenant_overrides`` / ``proposals`` with ``agency_name`` (the page lists and
  decides them on the CRM tab), and an approval's ``superseded_versions`` / ``cancelled_scheduled_versions`` (1.15) go
  into the CIS audit row. ``approved_version`` and ``scheduled`` (1.15) pass through as the CRM sends them.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Optional

import structlog

from api.services import crm_console
from api.services.settings_registry import RESOURCE, SettingsError, effective_start
from services.acp_shared.audit_log import PlatformAuditAction, write_audit_log

logger = structlog.get_logger()

CRM_PREFIX = "crm."
PASSED_ON = (400, 401, 403, 404, 409, 422, 503)


def is_crm_key(key: str) -> bool:
    return key.startswith(CRM_PREFIX)


def _refused(status: int, body: dict) -> SettingsError:
    code = body.get("detail") if isinstance(body.get("detail"), str) else "crm_refused"
    message = body.get("message") if isinstance(body.get("message"), str) else f"The partner CRM refused: {code}"
    return SettingsError(status if status in PASSED_ON else 502, message, code)


async def _call(authorization: Optional[str], method: str, path: str, *, json: Any = None,
                params: Optional[dict] = None, ok: tuple = (200,)) -> dict:
    status, body = await crm_console.crm_ops_call(method, path, authorization, json=json, params=params)
    if status not in ok:
        raise _refused(status, body)
    return body


def _page_row(setting: dict) -> dict:
    """One CRM setting in the page's shape: ``pending`` -> ``proposals``; the agencies' own values in force
    (``agency_overrides``, crm.ops.v1 1.14) -> ``tenant_overrides`` and their open changes (``agency_pending``) join
    ``proposals``, each with its ``agency_name`` (the CRM's agencies are not CIS tenants; Approve / Reject send the
    row's ``tenant_id``)."""
    out = dict(setting)
    agency_pending = out.pop("agency_pending", None) or []
    out["proposals"] = (out.pop("pending", None) or []) + agency_pending
    overrides = out.pop("agency_overrides", None)
    if overrides is not None:
        out["tenant_overrides"] = [
            {"tenant_id": o.get("tenant_id"), "value": o.get("value"), "version": o.get("version"),
             "effective_from": o.get("effective_from"), "agency_name": o.get("agency_name")}
            for o in overrides if isinstance(o, dict) and o.get("tenant_id")]
    return out


CRM_ACTOR = re.compile(r"^platform:([A-Za-z0-9-]{1,128})$")
_NAMES_SQL = """
    SELECT cognito_sub, coalesce(nullif(btrim(display_name), ''), email, username) AS name
      FROM shared.admin_users WHERE cognito_sub = ANY($1::text[])
"""


async def _actor_names(pool: Any, rows: list[dict]) -> None:
    """Add ``proposed_by_name`` / ``decided_by_name`` to each CRM row, in place: the CIS staff row of the Cognito sub
    in ``platform:<sub>`` (None = no CIS staff row, or the names could not be read: the page shows the id)."""
    subs = {m.group(1) for r in rows for a in (r.get("proposed_by"), r.get("decided_by"))
            if isinstance(a, str) and (m := CRM_ACTOR.match(a))}
    names: dict[str, str] = {}
    if subs and pool is not None:
        try:
            async with pool.acquire() as conn:
                names = {r["cognito_sub"]: r["name"] for r in await conn.fetch(_NAMES_SQL, sorted(subs))}
        except Exception as exc:  # a name is a convenience: never fail the page on it
            logger.warning("crm_setting_names_failed", error=type(exc).__name__)
    for r in rows:
        for field in ("proposed_by", "decided_by"):
            m = CRM_ACTOR.match(r.get(field) or "")
            r[f"{field}_name"] = names.get(m.group(1)) if m else None


def _rows_of(settings: list[dict]) -> list[dict]:
    return [r for s in settings for r in ([s["last_change"]] if s.get("last_change") else []) + s["proposals"]]


async def _can_write(authorization: Optional[str]) -> Optional[bool]:
    """The CRM's own answer: may this staff member change CRM settings (crm-platform-admin)? None = no answer."""
    status, body = await crm_console.crm_ops_call("GET", "/console", authorization)
    value = body.get("can_write") if status == 200 else None
    return value if isinstance(value, bool) else None


async def list_settings(authorization: Optional[str], *, area: Optional[str] = None, pool: Any = None) -> dict:
    body = await _call(authorization, "GET", "/settings", params={"area": area})
    rows = [_page_row(s) for s in body.get("settings") or []]
    await _actor_names(pool, _rows_of(rows))
    return {**body, "settings": rows, "can_write": await _can_write(authorization)}


async def history(authorization: Optional[str], key: str, tenant_id: Optional[str] = None, *,
                  pool: Any = None) -> dict:
    body = await _call(authorization, "GET", f"/settings/{key}/history", params={"tenant_id": tenant_id})
    await _actor_names(pool, [r for r in body.get("history") or [] if isinstance(r, dict)])
    return body


def _resource_id(key: str, tenant_id: Optional[str]) -> str:
    return key if tenant_id is None else f"{key}@{tenant_id}"


async def _audit(pool: Any, *, actor: str, action: str, row: dict, extra: dict) -> None:
    """The CIS audit row of a change the CRM took (after the CRM answered: the CRM row is the change itself, so a
    failed audit write is logged, never reported as a failed change). The agency of an agency's own value is in
    ``resource_id`` / ``details`` (a CRM agency id, which need not be a CIS tenant row)."""
    agency = row.get("tenant_id")
    try:
        await write_audit_log(pool, tenant_id=None, actor=actor, action=action, resource_type=RESOURCE,
                              resource_id=_resource_id(row["key"], agency),
                              details={"app": "crm", "agency_id": agency, "value_id": row.get("id"),
                                       "version": row.get("version"), "after": row.get("value"),
                                       "self_approved": row.get("self_approved"), **extra})
    except Exception as exc:  # the CRM already holds the change
        logger.error("crm_setting_audit_failed", key=row.get("key"), action=action, error=repr(exc)[:200])


def _superseded(row: dict) -> dict:
    """What a CRM approval superseded (crm.ops.v1 1.15: the newest decision wins, as on CIS), for the CIS audit row;
    the same fields as a CIS approval's audit (none superseded, or an older CRM: no field)."""
    return {k: row[k] for k in ("superseded_versions", "cancelled_scheduled_versions") if isinstance(row.get(k), list)}


async def propose(pool: Any, authorization: Optional[str], key: str, *, actor: str, value: Any, reason: str,
                  base_version: Optional[int], evidence_ref: Optional[str] = None,
                  effective_from: Optional[datetime] = None, tenant_id: Optional[str] = None,
                  apply: bool = False) -> dict:
    effective_from = effective_start(effective_from)
    body = {"value": value, "reason": reason, "evidence_ref": evidence_ref,
            "effective_from": effective_from.isoformat() if effective_from else None, "tenant_id": tenant_id,
            "base_version": base_version, "apply": apply}
    row = await _call(authorization, "POST", f"/settings/{key}/changes", json=body, ok=(200, 201))
    await _audit(pool, actor=actor, action=PlatformAuditAction.SETTING_PROPOSED, row=row,
                 extra={"reason": reason, "evidence_ref": evidence_ref})
    if row.get("status") == "approved":
        await _audit(pool, actor=actor, action=PlatformAuditAction.SETTING_APPROVED, row=row,
                     extra=_superseded(row))
    logger.info("crm_setting_relayed", key=key, status=row.get("status"), agency=tenant_id is not None)
    return row


async def decide(pool: Any, authorization: Optional[str], value_id: str, *, actor: str, approve: bool,
                 note: Optional[str] = None, tenant_id: Optional[str] = None) -> dict:
    decision = "approve" if approve else "reject"
    row = await _call(authorization, "POST", f"/settings/changes/{value_id}/{decision}",
                      json={"note": note, "tenant_id": tenant_id})
    action = PlatformAuditAction.SETTING_APPROVED if approve else PlatformAuditAction.SETTING_REJECTED
    await _audit(pool, actor=actor, action=action, row=row, extra={"note": note, **_superseded(row)})
    logger.info("crm_setting_decision_relayed", key=row.get("key"), status=row.get("status"))
    return row


__all__ = ["CRM_PREFIX", "decide", "history", "is_crm_key", "list_settings", "propose"]
