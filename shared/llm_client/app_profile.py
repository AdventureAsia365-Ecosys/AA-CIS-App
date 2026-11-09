"""AA-1025 H1a (root ADR 0009 decision 4) — the app profile: one row per app (``shared.llm_app_profile``, CIS 345)
with its AI switch, bulk pause, default daily AI budget and degrade mode.

The CIS gateway reads the profile of its own app (``call_log.APP`` = ``cis``) before anything else on the call path:

- **AI off** (``ai_enabled = false``): every AI call of the app is refused before any provider attempt, whatever the
  stage, model or connection: ``LLMClient.generate`` (``_generate_with``), the admin's connection test
  (``LLMClient.probe``), ``embed()`` and Jev ``decide()``. The gateway writes one 0 USD ledger row (status
  ``refused``, error_class ``disabled``, as the stage kill switch) and raises ``LLMAppDisabled``, an
  ``LLMStageDisabled``: the job fails without a retry, the T2 / T9 hooks keep the version / piece to do and the bulk
  re-queue holds the job while the app's AI is off (``shared/jobs/queue.disabled_stages``). That is the app's
  declared degrade mode ``queue_retry`` (tech lead T-21). Jev fails open as on its own switch: the stage keeps its
  rule and the ask gets its 0 USD refused row.
- ``bulk_paused``, ``default_daily_budget_usd`` and ``degrade_mode`` are stored, edited and shown here; the gateway
  does not act on them in this slice (H2 pauses the bulk work; CIS enforces its central cap ``llm/app:cis``, A7;
  TripPlanner and the CRM read their own row in H1b / H1c).

Reads: the four rows in one query, cached ``CACHE_TTL_SECONDS`` per process (the stage switch's 20 s), so a switch
reaches every task within that window; the writer invalidates its own process at once. **Fail open**: no table
(before 345), no row or an unreadable table = AI on (logged; the same rule as the Jev and connection switches, no
row = on): a database blip must not stop every AI call. ``LLM_APP_PROFILE=off`` (ECS env, read per call) skips the
check.

Admin writes: ``update`` (``PATCH /admin/ai-gateway/app-profiles/{app}``, audited in ``acp_shared.audit_log``).
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Optional

import structlog

from . import connections
from .errors import LLMStageDisabled

logger = structlog.get_logger()

APPS = ("cis", "tripplanner", "crm", "booking")
# llm_role_config's degrade_mode_check (326) plus 'none' (an app with no AI); migration 345's CHECK.
DEGRADE_MODES = ("queue_retry", "basic_narration", "last_saved_text", "manual_reply_needed", "none")
SWITCH_ENV = "LLM_APP_PROFILE"
CACHE_TTL_SECONDS = 20.0
FAILURE_TTL_SECONDS = 60.0
REASON_MAX = 500
BUDGET_MAX_USD = 100_000.0
EDITABLE = ("ai_enabled", "bulk_paused", "default_daily_budget_usd", "degrade_mode", "disabled_reason")

SELECT_SQL = "SELECT to_jsonb(p) AS row FROM shared.llm_app_profile p ORDER BY p.app"
_UPDATE_SQL = """
    UPDATE shared.llm_app_profile
       SET ai_enabled = $2, bulk_paused = $3, default_daily_budget_usd = $4, degrade_mode = $5,
           disabled_reason = $6, updated_at = now(), updated_by = $7
     WHERE app = $1
 RETURNING to_jsonb(llm_app_profile) AS row
"""
_LOCK_SQL = "SELECT to_jsonb(p) AS row FROM shared.llm_app_profile p WHERE p.app = $1 FOR UPDATE"
_AUDIT_SQL = ("INSERT INTO acp_shared.audit_log (actor, action, resource_type, resource_id, details) "
              "VALUES ($1, $2, 'llm_app_profile', $3, $4::jsonb)")


class AppProfileUnavailable(RuntimeError):
    """The profile table is not there (migration 345 not applied): the admin routes answer 503."""


@dataclass(frozen=True)
class AppProfile:
    app: str
    ai_enabled: bool = True
    bulk_paused: bool = False
    default_daily_budget_usd: Optional[float] = None
    degrade_mode: str = "queue_retry"
    disabled_reason: Optional[str] = None
    updated_at: Optional[str] = None
    updated_by: Optional[str] = None

    def as_dict(self) -> dict:
        return asdict(self)


def profile_of(raw: Any) -> Optional[AppProfile]:
    """An AppProfile from a ``to_jsonb`` row (dict or JSON text), or None for a row that does not parse. Pure."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    if not isinstance(raw, dict) or raw.get("app") not in APPS:
        return None
    budget = raw.get("default_daily_budget_usd")
    try:
        budget = None if budget is None else round(float(budget), 4)
    except (TypeError, ValueError):
        budget = None
    updated = raw.get("updated_at")
    return AppProfile(
        app=raw["app"], ai_enabled=raw.get("ai_enabled") is not False, bulk_paused=raw.get("bulk_paused") is True,
        default_daily_budget_usd=budget,
        degrade_mode=raw.get("degrade_mode") if raw.get("degrade_mode") in DEGRADE_MODES else "queue_retry",
        disabled_reason=raw.get("disabled_reason") or None,
        updated_at=updated.isoformat() if isinstance(updated, datetime) else (str(updated) if updated else None),
        updated_by=raw.get("updated_by") or None)


class LLMAppDisabled(LLMStageDisabled):
    """AA-1025 H1a: the app's AI is switched off in its app profile, so the gateway refused the call before any
    provider attempt (ledger: status refused, error_class ``disabled``, 0 USD). An ``LLMStageDisabled`` on purpose:
    its message names the stage the way the kill switch's does (``stage '<stage>' is switched off``), so the job runner,
    the T2 / T9 hooks and the bulk re-queue treat it as the stage switch (``queue_retry``)."""

    def __init__(self, app: str, stage: Optional[str], reason: Optional[str] = None):
        why = f" ({reason.strip()[:300]})" if isinstance(reason, str) and reason.strip() else ""
        super().__init__(stage or "unstaged", f"AI is off for app {app!r} in its app profile{why}")
        self.app = app


def refusal_text(app: str) -> str:
    """The text of a Jev ask the app switch refused (decision_log error; ends like every Jev refusal)."""
    return f"AI is off for app {app!r} (AI providers, App profiles): nothing was sent to the provider"


def enabled() -> bool:
    return (os.environ.get(SWITCH_ENV) or "on").strip().lower() not in ("off", "false", "0", "no")


# ── reads ────────────────────────────────────────────────────────────────────────────────────────────────────────────

async def read_all(conn) -> dict[str, AppProfile]:
    """Every profile by app. Raises ``AppProfileUnavailable`` when the table is missing. ``conn``: asyncpg."""
    import asyncpg
    try:
        rows = await conn.fetch(SELECT_SQL)
    except asyncpg.exceptions.UndefinedTableError as e:
        raise AppProfileUnavailable("the app profile needs migration 345") from e
    out = {}
    for r in rows:
        p = profile_of(r["row"])
        if p is not None:
            out[p.app] = p
    return out


async def _load() -> dict[str, AppProfile]:
    conn = await connections._connect()
    try:
        return await read_all(conn)
    finally:
        await conn.close()


def loader() -> dict[str, AppProfile]:
    """The sync loader of the cache (tests replace it; conftest: no database in unit tests)."""
    return connections.run_sync(_load)


async def loader_async(db) -> dict[str, AppProfile]:
    """The async loader of the cache through ``db`` (a pool or a ``_SingleConn``; tests replace it)."""
    async with db.acquire() as conn:
        return await read_all(conn)


_profiles: dict[str, AppProfile] = {}
_loaded_at = 0.0
_failed_at = 0.0
_lock = threading.Lock()


def _fresh(now: float) -> bool:
    return bool((_loaded_at and now - _loaded_at < CACHE_TTL_SECONDS)
                or (_failed_at and now - _failed_at < FAILURE_TTL_SECONDS))


def _store(table: Optional[dict[str, AppProfile]], error: Optional[BaseException] = None) -> None:
    global _profiles, _loaded_at, _failed_at
    if table is None:
        _profiles, _loaded_at, _failed_at = {}, 0.0, time.monotonic()
        logger.warning("llm_app_profile_read_failed", error=str(error)[:300] if error else None,
                       hint="every app counts as AI on until the profile can be read")
    else:
        _profiles, _loaded_at, _failed_at = table, time.monotonic(), 0.0


def profiles() -> dict[str, AppProfile]:
    """The cached profiles by app ({} = none, or unreadable: AI on). Never raises."""
    if _fresh(time.monotonic()):
        return _profiles
    with _lock:
        if _fresh(time.monotonic()):
            return _profiles
        try:
            _store(loader())
        except Exception as e:  # noqa: BLE001 — fail open, see the module docstring
            _store(None, e)
    return _profiles


async def profiles_async(db) -> dict[str, AppProfile]:
    """``profiles()`` for async code that holds a pool (or a ``_SingleConn``): the same cache, refreshed through
    ``db``. Never raises."""
    if _fresh(time.monotonic()):
        return _profiles
    try:
        table = await loader_async(db)
    except Exception as e:  # noqa: BLE001 — fail open
        with _lock:
            _store(None, e)
        return _profiles
    with _lock:
        _store(table)
    return _profiles


def profile(app: str, table: Optional[dict[str, AppProfile]] = None) -> AppProfile:
    """The profile of ``app`` (the default, AI on, when it has no row or the table cannot be read)."""
    found = (profiles() if table is None else table).get(app)
    return found if found is not None else AppProfile(app=app)


def ai_off(app: Optional[str] = None, table: Optional[dict[str, AppProfile]] = None) -> Optional[AppProfile]:
    """The profile of ``app`` (default: this gateway's app) when its AI is switched off, else None. Never raises."""
    if not enabled():
        return None
    from .call_log import APP
    try:
        p = profile(app or APP, table)
    except Exception as e:  # noqa: BLE001 — never on the call path
        logger.warning("llm_app_profile_check_failed", error=str(e)[:200])
        return None
    return p if not p.ai_enabled else None


def refusal(stage: Optional[str], app: Optional[str] = None) -> Optional[LLMAppDisabled]:
    """The gateway's check before any attempt: an ``LLMAppDisabled`` to log and raise when the app's AI is off, else
    None. Never raises."""
    off = ai_off(app)
    if off is None:
        return None
    logger.warning("llm_app_ai_off_refused", app=off.app, stage=stage)
    return LLMAppDisabled(off.app, stage, off.disabled_reason)


async def ai_off_async(db, app: Optional[str] = None) -> Optional[AppProfile]:
    """``ai_off`` for async code holding a pool. Never raises."""
    if not enabled():
        return None
    return ai_off(app, await profiles_async(db))


def invalidate() -> None:
    global _profiles, _loaded_at, _failed_at
    with _lock:
        _profiles, _loaded_at, _failed_at = {}, 0.0, 0.0


# ── admin writes ─────────────────────────────────────────────────────────────────────────────────────────────────────

def merged(before: AppProfile, fields: dict) -> AppProfile:
    """``before`` with the edited ``fields`` (keys of ``EDITABLE``); switching AI on clears the reason, switching off
    keeps the given one. Raises ValueError on a value the table would refuse or an off switch without a reason. Pure."""
    unknown = set(fields) - set(EDITABLE)
    if unknown:
        raise ValueError(f"not editable: {', '.join(sorted(unknown))}")
    d = before.as_dict()
    d.update(fields)
    if d["degrade_mode"] not in DEGRADE_MODES:
        raise ValueError(f"degrade_mode must be one of {', '.join(DEGRADE_MODES)}")
    budget = d["default_daily_budget_usd"]
    if budget is not None:
        budget = round(float(budget), 4)
        if not 0 <= budget <= BUDGET_MAX_USD:
            raise ValueError(f"default_daily_budget_usd must be between 0 and {BUDGET_MAX_USD:.0f}")
    reason = (d["disabled_reason"] or "").strip() or None
    if d["ai_enabled"]:
        reason = None
    elif reason is None or len(reason) < 3:
        raise ValueError("say why AI is switched off (reason, at least 3 characters)")
    elif len(reason) > REASON_MAX:
        raise ValueError(f"reason must be at most {REASON_MAX} characters")
    return AppProfile(app=before.app, ai_enabled=bool(d["ai_enabled"]), bulk_paused=bool(d["bulk_paused"]),
                      default_daily_budget_usd=budget, degrade_mode=d["degrade_mode"], disabled_reason=reason,
                      updated_at=before.updated_at, updated_by=before.updated_by)


def audit_action(before: AppProfile, after: AppProfile) -> str:
    """The audit action of one edit: the AI switch when it moved, else a plain update. Pure."""
    if before.ai_enabled != after.ai_enabled:
        return "llm_app_ai_switched_on" if after.ai_enabled else "llm_app_ai_switched_off"
    return "llm_app_profile_updated"


async def update(pool, app: str, fields: dict, *, actor: str) -> dict:
    """Edit one app's profile (the fields given; the rest kept) and write its audit row in the same transaction.
    Returns the saved profile. Raises LookupError (unknown app / no row), ValueError (a value the table would refuse),
    AppProfileUnavailable (no table). This process's cache is invalidated at once; the other tasks see the change
    within ``CACHE_TTL_SECONDS``."""
    import asyncpg
    if app not in APPS:
        raise LookupError(f"unknown app: {app!r}")
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(_LOCK_SQL, app)
                before = profile_of(row["row"]) if row is not None else None
                if before is None:
                    raise LookupError(f"no profile row for app {app!r}")
                after = merged(before, fields)
                saved = await conn.fetchrow(_UPDATE_SQL, app, after.ai_enabled, after.bulk_paused,
                                            after.default_daily_budget_usd, after.degrade_mode,
                                            after.disabled_reason, actor[:128])
                out = profile_of(saved["row"])
                changed = {k: [getattr(before, k), getattr(after, k)] for k in EDITABLE
                           if getattr(before, k) != getattr(after, k)}
                await conn.execute(_AUDIT_SQL, actor[:64], audit_action(before, after), app,
                                   json.dumps({"changed": changed}, default=str))
    except asyncpg.exceptions.UndefinedTableError as e:
        raise AppProfileUnavailable("the app profile needs migration 345") from e
    finally:
        invalidate()
    logger.info("llm_app_profile_updated", app=app, actor=actor[:64], ai_enabled=after.ai_enabled)
    return out.as_dict() if out is not None else after.as_dict()
