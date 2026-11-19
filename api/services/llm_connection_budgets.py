"""AA-1143 (tech lead decision G3, S285; CIS 455 / 456) — one budget per LLM connection: its total (the provider budget)
and one fixed-USD share per app, CIS taking the remainder, read and edited on ONE screen (AI gateway › Budgets).

Where each value lives:

- ``shared.llm_connection_budget`` (CIS 455): append-only versions; app NULL = the total, else an app of
  ``shared.llm_app_profile`` other than cis. A save is ONE INSERT statement (the table's statement trigger then refuses
  shares above the total and writes the CIS remainder to ``shared.llm_provider_connection.monthly_budget_usd``, the
  number the CIS gateway enforces: no new read on the call path). Reason 20..500 characters, audited
  (``acp_shared.audit_log`` action ``llm_connection_budget_changed``), versioned like Settings.
- TripPlanner reads its row through ``catalog.v_llm_connection_cap_v2`` (CIS 456, the control plane it already reads,
  root ADR 0009 d13). An app with no share of its own on a connection spends inside the CIS remainder (TripPlanner
  today).
- The CRM has its own ledger and reads no CIS table (root ADR 0008a). CIS sends it its copy over crm.ops.v1
  (``PUT /llm/connection-shares``, the staff member's own WORKFORCE token, the relay CIS already uses for the CRM app
  profile and settings): every connection that has a total, with the CRM's share or 0 (no share = the CRM may not
  spend there: its spend is counted nowhere in CIS). Order (no moment above the total): a lower CRM share is sent
  BEFORE the CIS rows are written, a higher one after. A copy that could not be sent is shown ("not sent") and sent
  again with ``sync_crm``. For one release the CRM falls back to its old setting ``crm.ai.connection_monthly_cap_usd``
  for a connection its copy does not name; the page shows that setting next to the row.

Before CIS 455 the table is missing (SQLSTATE 42P01): the read says ``ready: false`` and a save answers 503.
"""
from __future__ import annotations

import json
from decimal import Decimal
from typing import Any, Optional

import asyncpg
import structlog

from api.services import crm_console
from api.services import llm_connection_split as rules
from shared import cost_guard

logger = structlog.get_logger()

NOT_MIGRATED = "the connection budget rows need database migrations 455 and 456"
AUDIT_ACTION = "llm_connection_budget_changed"
CRM_APP = "crm"
CRM_SHARES_PATH = "/llm/connection-shares"
CRM_NOT_SENT, CRM_IN_SYNC, CRM_DIFFERS, CRM_UNKNOWN = "not_sent", "in_sync", "differs", "unknown"

_ROWS_SQL = """
    SELECT connection_key, app, budget_usd, version, reason, actor, valid_from
      FROM shared.llm_connection_budget_current
"""
_HISTORY_SQL = """
    SELECT id, connection_key, app, budget_usd, version, reason, actor, valid_from
      FROM shared.llm_connection_budget
     WHERE connection_key = $1
     ORDER BY id DESC
     LIMIT $2
"""
_VERSIONS_SQL = """
    SELECT app, max(version) AS version FROM shared.llm_connection_budget WHERE connection_key = $1 GROUP BY app
"""
_APPS_SQL = "SELECT app FROM shared.llm_app_profile ORDER BY app"
# One INSERT per save: the statement trigger checks the rows in force after all of them (CIS 455).
_INSERT_SQL = """
    INSERT INTO shared.llm_connection_budget (connection_key, app, budget_usd, version, reason, actor)
    SELECT $1, c.app, c.usd, c.version, $5, $6
      FROM unnest($2::text[], $3::numeric[], $4::int[]) WITH ORDINALITY AS c(app, usd, version, ord)
     ORDER BY c.ord
"""
# This UTC month's CIS-ledger spend per connection and app (a NULL app counts as cis), the sum the v2 view makes.
_SPEND_SQL = """
    SELECT account, coalesce(app, 'cis') AS app, coalesce(sum(cost_usd), 0) AS usd, count(*)::int AS calls
      FROM shared.llm_call_log
     WHERE account = ANY($1::text[])
       AND created_at >= date_trunc('month', now() AT TIME ZONE 'utc') AT TIME ZONE 'utc'
     GROUP BY account, coalesce(app, 'cis')
"""


class BudgetsUnavailable(RuntimeError):
    """The table is not migrated (503)."""


def _dec(v: Any) -> Optional[Decimal]:
    return None if v is None else Decimal(str(v))


def _f(v: Any) -> Optional[float]:
    return None if v is None else float(v)


def _row_json(r: Any) -> dict:
    return {"usd": _f(r["budget_usd"]), "version": r["version"], "reason": r["reason"], "actor": r["actor"],
            "valid_from": r["valid_from"].isoformat() if hasattr(r["valid_from"], "isoformat") else r["valid_from"]}


async def share_apps(conn) -> list[str]:
    """The apps that may hold a share: every app of shared.llm_app_profile but cis (CIS takes the remainder)."""
    try:
        apps = [r["app"] for r in await conn.fetch(_APPS_SQL)]
    except asyncpg.exceptions.UndefinedTableError:
        from shared.llm_client import app_profile
        apps = list(app_profile.APPS)
    return [a for a in apps if a != rules.REMAINDER_APP]


async def current_rows(conn) -> dict[str, dict[Optional[str], Any]]:
    """{connection_key: {app or None: row in force}}. BudgetsUnavailable before CIS 455."""
    try:
        rows = await conn.fetch(_ROWS_SQL)
    except asyncpg.exceptions.UndefinedTableError as e:
        raise BudgetsUnavailable(NOT_MIGRATED) from e
    out: dict[str, dict[Optional[str], Any]] = {}
    for r in rows:
        out.setdefault(r["connection_key"], {})[r["app"]] = r
    return out


def crm_copy(rows: dict[str, dict[Optional[str], Any]]) -> dict[str, str]:
    """The CRM's copy: every connection with a total -> the CRM's share, else "0.00" (no share = no spend). Pure."""
    out = {}
    for key, by_app in sorted(rows.items()):
        if None not in by_app:
            continue
        share = by_app.get(CRM_APP)
        out[key] = f"{Decimal(str(share['budget_usd'])) if share is not None else Decimal(0):.2f}"
    return out


async def _crm_get(authorization: Optional[str], path: str, params: Optional[dict] = None) -> tuple[Any, Any]:
    status, body = await crm_console.crm_ops_call("GET", path, authorization, params=params)
    if status != 200:
        code = body.get("detail") if isinstance(body.get("detail"), str) else None
        return None, code or f"crm_status_{status}"
    return body, None


async def crm_state(authorization: Optional[str]) -> dict:
    """What the CRM holds: its copy (crm.ops.v1 GET /llm/connection-shares) and its old setting (GET /settings, area
    ai), each read on its own. Never raises."""
    copy_body, copy_error = await _crm_get(authorization, CRM_SHARES_PATH)
    copy = None
    if copy_body is not None and isinstance(copy_body.get("shares"), dict):
        try:
            copy = {k: float(v) for k, v in rules.budget_map(copy_body["shares"]).items()}
        except ValueError:
            copy_error = "crm_copy_unreadable"
    settings_body, settings_error = await _crm_get(authorization, "/settings", {"area": rules.CRM_CAP_AREA})
    setting = None
    if settings_body is not None:
        item = next((s for s in settings_body.get("settings") or []
                     if isinstance(s, dict) and s.get("key") == rules.CRM_CAP_KEY), None)
        try:
            setting = None if item is None else {k: float(v) for k, v in rules.budget_map(item.get("value")).items()}
        except ValueError:
            settings_error = "crm_setting_unreadable"
    return {"copy": copy, "copy_error": copy_error, "sent_at": (copy_body or {}).get("updated_at"),
            "setting": setting, "setting_error": settings_error}


def crm_row_state(key: str, has_total: bool, expected: Optional[str], crm: dict) -> str:
    """not_sent (the CRM's copy does not name the connection), in_sync, differs, unknown (the CRM did not answer)."""
    if not has_total:
        return CRM_NOT_SENT
    if crm.get("copy_error") and crm.get("copy") is None:
        return CRM_UNKNOWN
    copy = crm.get("copy") or {}
    if key not in copy:
        return CRM_NOT_SENT
    return CRM_IN_SYNC if Decimal(str(copy[key])).quantize(rules.CENT) == Decimal(expected) else CRM_DIFFERS


async def read(pool: Any, authorization: Optional[str]) -> dict:
    """GET /admin/ai-gateway/connection-budgets: every LLM connection with its total, each app's share, the CIS
    remainder (and the enforced CIS cap, which the trigger keeps equal to it), this month's CIS-ledger spend per app,
    the CIS remainder's spend (the CIS gateway's rule) and the CRM's copy. ``ready: false`` before CIS 455."""
    from shared.llm_client import connections as conn_registry

    errors: list[dict] = []
    async with pool.acquire() as conn:
        registry = await conn_registry.fetch_connections(conn)
        apps = await share_apps(conn)
        try:
            rows = await current_rows(conn)
        except BudgetsUnavailable:
            return {"ready": False, "apps": apps, "rows": [], "crm": None, "errors": [], "message": NOT_MIGRATED}
        try:
            policies = await cost_guard.read_cost_policies(conn)
        except Exception as e:  # noqa: BLE001 — the page still shows the budgets
            policies = {}
            errors.append({"source": "cost_policy", "error": type(e).__name__})
        spend: dict[str, dict[str, tuple[float, int]]] = {}
        try:
            for r in await conn.fetch(_SPEND_SQL, sorted(registry)):
                spend.setdefault(r["account"], {})[r["app"]] = (float(r["usd"] or 0), int(r["calls"] or 0))
        except Exception as e:  # noqa: BLE001
            errors.append({"source": "spend", "error": type(e).__name__})
    crm = await crm_state(authorization)
    expected = crm_copy(rows)
    out = []
    for key in sorted(registry):
        c = registry[key]
        by_app = rows.get(key, {})
        total = by_app.get(None)
        shares = {a: (by_app[a] if a in by_app else None) for a in apps}
        share_usd = {a: (_dec(r["budget_usd"]) if r is not None else None) for a, r in shares.items()}
        left = rules.remainder(_dec(total["budget_usd"]) if total else None, share_usd)
        used = spend.get(key, {})
        pool_usd = sum((usd if share_usd.get(app) is None or app == rules.REMAINDER_APP
                        else max(usd - float(share_usd[app]), 0.0)) for app, (usd, _n) in used.items())
        policy = cost_guard.policy_of(policies, key)
        out.append({
            "connection_key": key, "label": c.label, "kind": c.kind, "enabled": c.enabled,
            "total": _row_json(total) if total else None,
            "shares": {a: (_row_json(r) if r is not None else None) for a, r in shares.items()},
            "cis_remainder_usd": _f(left),
            "cis_cap_usd": _f(c.monthly_budget_usd),
            "cis_cap_in_sync": total is None or (c.monthly_budget_usd is not None
                                                 and Decimal(str(c.monthly_budget_usd)) == left),
            "spend": {app: round(usd, 6) for app, (usd, _n) in used.items()},
            "cis_pool_spent_usd": round(pool_usd, 6),
            "alert_pct": policy.alert_pct, "hard_stop": policy.hard_stop,
            "crm_expected_usd": _f(_dec(expected[key])) if key in expected else None,
            "crm_copy_usd": (crm.get("copy") or {}).get(key),
            "crm_setting_usd": (crm.get("setting") or {}).get(key),
            "crm_state": crm_row_state(key, total is not None, expected.get(key), crm),
        })
    return {"ready": True, "apps": apps, "rows": out, "crm": {k: crm[k] for k in ("copy_error", "setting_error",
                                                                                  "sent_at")},
            "errors": errors}


async def history(pool: Any, connection_key: str, limit: int = 100) -> dict:
    async with pool.acquire() as conn:
        try:
            rows = await conn.fetch(_HISTORY_SQL, connection_key, limit)
        except asyncpg.exceptions.UndefinedTableError as e:
            raise BudgetsUnavailable(NOT_MIGRATED) from e
    return {"connection_key": connection_key,
            "versions": [{"app": r["app"], **_row_json(r)} for r in rows]}


async def send_crm_copy(authorization: Optional[str], copy: dict[str, str], *, reason: str) -> Optional[str]:
    """PUT the CRM's whole copy. None when the CRM took it, else the reason it did not. Never raises."""
    status, body = await crm_console.crm_ops_call("PUT", CRM_SHARES_PATH, authorization,
                                                  json={"shares": copy, "reason": reason[:rules.REASON_MAX]})
    if status == 200:
        return None
    code = body.get("detail") if isinstance(body.get("detail"), str) else None
    logger.warning("llm_connection_budget_crm_send_failed", status=status, code=code)
    return code or f"crm_status_{status}"


def _loosened(role: Optional[str], before: dict, after: dict) -> None:
    """S279 thresholds L1: a tighten-only role may lower a cap, never raise one: no app's cap (CIS: the remainder) and
    not the total may go up. PermissionError otherwise."""
    from api.services import settings_registry

    settings_registry.refuse_loosening(role, before, after, caps=tuple(sorted(set(before) | set(after))))


async def save(pool: Any, authorization: Optional[str], connection_key: str, *, total: Any = None,
               shares: Optional[dict] = None, reason: Any, actor: str, role: Optional[str] = None) -> dict:
    """PUT /admin/ai-gateway/connection-budgets/{key}: change the total and / or some apps' shares in one audited save.
    Raises LookupError (no such LLM connection, 404), rules.BudgetEditRefused (422), PermissionError (403),
    BudgetsUnavailable (503) and CrmRefused (the CRM did not take a lower share: nothing written, 502 / its status)."""
    from shared.llm_client import connection_budget, connections as conn_registry

    text = rules.check_reason(reason)
    if total is None and not shares:
        raise rules.BudgetEditRefused("nothing_to_change", "nothing to change")
    async with pool.acquire() as conn:
        registry = await conn_registry.fetch_connections(conn)
        if connection_key not in registry:
            raise LookupError(connection_key)
        apps = await share_apps(conn)
        rows = await current_rows(conn)
        by_app = rows.get(connection_key, {})
        before_total = _dec(by_app[None]["budget_usd"]) if None in by_app else None
        before_shares = {a: (_dec(by_app[a]["budget_usd"]) if a in by_app else None) for a in apps}
        plan = rules.plan_edit(apps, before_total, before_shares, total, shares)
        if not plan["changes"]:
            return {"connection_key": connection_key, "changed": False, "crm_sync": None}
        _loosened(role, rules.effective_caps(before_total, before_shares),
                  rules.effective_caps(plan["total"], plan["shares"]))
        crm_change = next(((app, new) for app, new in plan["changes"] if app == CRM_APP), None)
        new_rows = {k: dict(v) for k, v in rows.items()}
        target = new_rows.setdefault(connection_key, {})
        for app, new in plan["changes"]:
            if new is None:
                target.pop(app, None)
            else:
                target[app] = {"budget_usd": new}
        copy_after = crm_copy(new_rows)
        crm_first = (crm_change is not None and rules.crm_tightens(before_shares.get(CRM_APP), crm_change[1])) or (
            before_total is not None and plan["total"] < before_total and CRM_APP in apps)
        if crm_first:
            err = await send_crm_copy(authorization, copy_after, reason=text)
            if err is not None:
                raise CrmRefused(err)
        async with conn.transaction():
            # One save at a time per connection: the version numbers below come from this read.
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext('llm_connection_budget:' || $1))",
                               connection_key)
            versions = {r["app"]: r["version"] for r in await conn.fetch(_VERSIONS_SQL, connection_key)}
            apps_col = [app for app, _ in plan["changes"]]
            usd_col = [new for _, new in plan["changes"]]
            ver_col = [versions.get(app, 0) + 1 for app in apps_col]
            try:
                await conn.execute(_INSERT_SQL, connection_key, apps_col, usd_col, ver_col, text, actor[:128])
            except asyncpg.exceptions.CheckViolationError as e:
                raise rules.BudgetEditRefused("over_allocated", str(e).splitlines()[0]) from e
            await conn.execute(
                "INSERT INTO acp_shared.audit_log (actor, action, resource_type, resource_id, details) "
                "VALUES ($1, $2, $3, $4, $5::jsonb)",
                actor[:64], AUDIT_ACTION, "llm_connection_budget", connection_key,
                json.dumps({"reason": text,
                            "before": {"total": _f(before_total), **{a: _f(v) for a, v in before_shares.items()},
                                       "cis_remainder": _f(rules.remainder(before_total, before_shares))},
                            "after": {"total": _f(plan["total"]), **{a: _f(v) for a, v in plan["shares"].items()},
                                      "cis_remainder": _f(plan["remainder"])}}))
    conn_registry.invalidate()               # this process enforces the new remainder at once; others within the TTL
    connection_budget.invalidate()
    crm_sync = None
    if CRM_APP in apps and not crm_first:
        err = await send_crm_copy(authorization, copy_after, reason=text)
        crm_sync = {"sent": err is None, "error": err}
    elif crm_first:
        crm_sync = {"sent": True, "error": None}
    return {"connection_key": connection_key, "changed": True, "total_usd": _f(plan["total"]),
            "shares": {a: _f(v) for a, v in plan["shares"].items()}, "cis_remainder_usd": _f(plan["remainder"]),
            "crm_sync": crm_sync}


class CrmRefused(RuntimeError):
    """The CRM did not take a lower share, so nothing was written (the CIS remainder stays as it was)."""


async def sync_crm(pool: Any, authorization: Optional[str], *, actor: str) -> dict:
    """POST /admin/ai-gateway/connection-budgets/crm-sync: send the CRM its whole copy again (after a failed send, or
    the first time after CIS 456 seeded the rows). Audited when the CRM took it."""
    async with pool.acquire() as conn:
        rows = await current_rows(conn)
    copy = crm_copy(rows)
    err = await send_crm_copy(authorization, copy, reason="AI gateway Budgets: the CRM copy of its shares sent again")
    if err is None:
        await pool.execute(
            "INSERT INTO acp_shared.audit_log (actor, action, resource_type, resource_id, details) "
            "VALUES ($1, $2, $3, $4, $5::jsonb)",
            actor[:64], "llm_connection_budget_crm_sent", "llm_connection_budget", CRM_APP,
            json.dumps({"shares": copy}))
    return {"sent": err is None, "error": err, "shares": copy}
