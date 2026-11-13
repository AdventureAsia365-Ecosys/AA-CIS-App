"""AA-1093 lane L2 — the pure rules of the tenant offboarding erasure saga (no I/O; unit-tested).

Phases of a request (shared.tenant_erasure_request.phase, CIS 408):
  scheduled          T0 (the offboard) recorded; every app has its earliest date (not_before). Nothing runs.
  dry_run            from the LATEST not_before (one combined approval needs every app's plan, decision D7): CIS runs
                     its own data_erasure DRY RUN, Booking and TripPlanner report a plan hash through their report
                     views, the CRM through POST /tenant/v1/erasure-requests/{id}/reports. A missing report view is
                     the blocker app_not_ready (never a skip).
  awaiting_approval  every app reported a plan hash: the combined fingerprint is set; a Cognito admin approves it
                     within the approval validity (24 h); the admin secret alone is refused.
  apply              Booking, TripPlanner and the CRM apply (they pull the request, recompute and must match their
                     approved plan hash: plan_changed otherwise); CIS applies LAST, after all three report done:
                     a tenant backup, a fresh CIS DRY RUN whose saga plan hash must equal the approved one, then the
                     data_erasure apply (which deletes shared.tenants; the request has no FK and outlives it).
  verify             every app reports the census of its personal-data columns for the tenant (pii_left); all four 0
                     = the receipt, phase done.
  cancelled          a restore of the tenant before the apply (the restore route refuses once the apply started).
  failed             a guard stopped the saga (the pilot tenant).

Guards: the pilot tenant is refused everywhere (it is never scheduled at the offboard and the job fails a request
that names it); on Dev only a synthetic tenant (is_canary, not the pilot) may be approved or applied; a drill (window
0) is for synthetic tenants only; the apply switch is off by default (policy.apply_enabled).
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping, Optional

from services.tenant_erasure import policy

APPS = policy.APPS
PEER_APPS = ("booking", "tripplanner", "crm")    # apply before CIS
VIEW_APPS = ("booking", "tripplanner")           # report through their own report view
PHASES = ("scheduled", "dry_run", "awaiting_approval", "apply", "verify", "done", "cancelled", "failed")
OPEN_PHASES = ("scheduled", "dry_run", "awaiting_approval", "apply", "verify")
TERMINAL_PHASES = ("done", "cancelled", "failed")
# A restore may cancel the request only before any app was asked to erase.
CANCELLABLE_PHASES = ("scheduled", "dry_run", "awaiting_approval")
# Phases in which the job has something to do (awaiting_approval waits for a person).
WORKING_PHASES = ("scheduled", "dry_run", "apply", "verify")
MODES = ("dry_run", "apply", "verify")
STEP_STATUSES = ("pending", "done", "failed", "plan_changed", "app_not_ready", "refused")
# A step that stops the phase until a person acts (a new round, or the app's lane fixed).
STOPPING = ("failed", "plan_changed", "refused")

# The CIS tables the saga itself writes for the tenant between the CIS DRY RUN and the CIS apply (its audit rows and
# the tenant backup job row): left out of the CIS saga plan hash, else the approval could never match. The
# data_erasure apply still binds the full fingerprint of its own fresh DRY RUN.
SAGA_VOLATILE_TABLES = frozenset({"acp_shared.audit_log", "shared.job"})

HASH_RE = re.compile(r"^[0-9a-f]{32,128}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")


class Refused(Exception):
    """A refused action: ``status`` is the HTTP status the route answers, ``code`` a short machine reason."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(status, code, message)
        self.status = status
        self.code = code
        self.message = message


# ── schedule ────────────────────────────────────────────────────────────────────────────────────────────────────────

def schedule(offboarded_at: datetime, mode: str, env: Optional[dict] = None) -> dict[str, datetime]:
    """not_before per app: T0 + the app's window (drill: T0 for every app)."""
    return {app: offboarded_at + w for app, w in policy.windows(mode, env).items()}


def not_before(request: Mapping[str, Any]) -> dict[str, datetime]:
    return {app: request[f"not_before_{app}"] for app in APPS}


def due_at(request: Mapping[str, Any]) -> datetime:
    """When the DRY RUN round may start: the latest not_before (one combined approval needs every app's plan)."""
    return max(not_before(request).values())


# ── hashes ──────────────────────────────────────────────────────────────────────────────────────────────────────────

def cis_plan_hash(tenant_id: str, rows: Iterable[Mapping[str, Any]]) -> str:
    """The CIS plan hash the saga approves: sha256 of the tenant id and the (table, action, rows) list of a
    data_erasure DRY RUN, without SAGA_VOLATILE_TABLES. Same counts for another tenant = another hash."""
    key = sorted((str(r["table"]), str(r["action"]), int(r["rows"])) for r in rows
                 if str(r["table"]) not in SAGA_VOLATILE_TABLES)
    return hashlib.sha256(json.dumps({"v": 1, "tenant": str(tenant_id), "rows": key}).encode()).hexdigest()


def combined_fingerprint(request_id: str, tenant_id: str, plans: Mapping[str, str]) -> str:
    """sha256(request_id, tenant_id, sorted "app:plan_hash"): what the tech lead approves once for every app."""
    missing = [app for app in APPS if not plans.get(app)]
    if missing:
        raise ValueError(f"combined fingerprint needs every app's plan hash (missing: {', '.join(missing)})")
    parts = [str(request_id), str(tenant_id)] + sorted(f"{app}:{plans[app]}" for app in APPS)
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


# ── steps ───────────────────────────────────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PhaseState:
    """Where a phase stands from its step rows: ready (every app done), stopped (a step needs a person), waiting."""
    ready: bool
    stopped: bool
    waiting: tuple[str, ...]
    blockers: tuple[dict, ...]


def phase_state(steps: Mapping[str, Optional[Mapping[str, Any]]], apps: Iterable[str], *,
                need_hash: bool = False, need_census: bool = False) -> PhaseState:
    """``steps``: app -> its step row of this phase (or None). app_not_ready is a blocker that waits (the app's lane may
    ship); failed / plan_changed / refused stop the phase; a verify census above 0 stops it too."""
    waiting, blockers, stopped = [], [], False
    for app in apps:
        s = steps.get(app)
        status = (s or {}).get("status")
        if status == "done":
            if need_hash and not HASH_RE.match(str((s or {}).get("plan_hash") or "")):
                blockers.append({"app": app, "kind": "no_plan_hash", "detail": "reported done without a plan hash"})
                stopped = True
            elif need_census and (s or {}).get("pii_left") is None:
                waiting.append(app)
            elif need_census and int(s["pii_left"]) > 0:
                blockers.append({"app": app, "kind": "pii_left", "detail": f"{int(s['pii_left'])} value(s) left"})
                stopped = True
            continue
        if status == "app_not_ready":
            blockers.append({"app": app, "kind": "app_not_ready", "detail": str((s or {}).get("detail") or "")[:200]})
            waiting.append(app)
        elif status in STOPPING:
            blockers.append({"app": app, "kind": status, "detail": str((s or {}).get("detail") or "")[:200]})
            stopped = True
        else:
            waiting.append(app)
    return PhaseState(ready=not waiting and not blockers and not stopped, stopped=stopped, waiting=tuple(waiting),
                      blockers=tuple(blockers))


def normalise_report(raw: Mapping[str, Any], *, current_round: int) -> Optional[dict]:
    """One app report (a Booking / TripPlanner report-view row as JSON, or the CRM POST body) in step shape, or None
    when it is not usable (unknown mode / status, another round, a bad hash). Ids, codes, hashes and counts only: any
    other key is dropped."""
    mode = str(raw.get("mode") or "")
    status = str(raw.get("status") or "")
    if mode not in MODES or status not in STEP_STATUSES or status in ("pending", "app_not_ready"):
        return None
    rnd = raw.get("round")
    if rnd is not None:
        try:
            if int(rnd) != int(current_round):
                return None
        except (TypeError, ValueError):
            return None
    plan_hash = raw.get("plan_hash")
    plan_hash = str(plan_hash).lower() if plan_hash else None
    if plan_hash is not None and not HASH_RE.match(plan_hash):
        return None
    counts = raw.get("counts")
    if isinstance(counts, str):
        try:
            counts = json.loads(counts)
        except ValueError:
            counts = None
    counts = {str(k)[:64]: int(v) for k, v in (counts or {}).items()
              if isinstance(v, (int, float)) and not isinstance(v, bool)} if isinstance(counts, dict) else {}
    pii = raw.get("pii_left")
    try:
        pii = int(pii) if pii is not None and not isinstance(pii, bool) else None
    except (TypeError, ValueError):
        pii = None
    if pii is not None and pii < 0:
        return None
    detail = raw.get("detail")
    return {"mode": mode, "status": status, "plan_hash": plan_hash, "counts": counts, "pii_left": pii,
            "detail": str(detail)[:500] if detail else None, "reported_at": raw.get("reported_at")}


def latest_reports(rows: Iterable[Mapping[str, Any]], *, current_round: int) -> dict[str, dict]:
    """mode -> the latest usable report of the current round (by reported_at, then the order given)."""
    out: dict[str, dict] = {}
    for raw in rows:
        rep = normalise_report(raw, current_round=current_round)
        if rep is None:
            continue
        prev = out.get(rep["mode"])
        if prev is None or str(rep.get("reported_at") or "") >= str(prev.get("reported_at") or ""):
            out[rep["mode"]] = rep
    return out


# ── people and guards ───────────────────────────────────────────────────────────────────────────────────────────────

def tenant_guard(tenant: Optional[Mapping[str, Any]], *, environment: str, synthetic: bool,
                 drill: bool = False) -> list[str]:
    """Why the saga may not approve or apply for this tenant (empty = it may)."""
    if tenant is None:
        return ["the tenant row does not exist"]
    out = []
    if tenant.get("is_pilot"):
        out.append("the pilot tenant is never erased by the saga (decision D8; handled by hand)")
    if tenant.get("cancelled_at") is None:
        out.append("the tenant is not offboarded")
    if (drill or environment == "dev") and not synthetic:
        out.append("only a synthetic tenant may be erased here (Dev, or a drill)")
    return out


def approval_refusal(request: Mapping[str, Any], *, fingerprint: str, now: datetime, cognito_admin: bool,
                     apply_on: bool, tenant_reasons: list[str], validity: timedelta) -> Optional[Refused]:
    """The first reason an approval is refused, or None. Order: who (403), the switch (403), the phase (409), the
    fingerprint (409 stale), its age (409), the tenant (409)."""
    if not cognito_admin:
        return Refused(403, "needs_cognito_admin",
                       "Approving a tenant erasure needs an administrator's company sign-in (Cognito); the admin "
                       "secret or a password session cannot")
    if not apply_on:
        return Refused(403, "apply_off", f"the erasure saga apply is off here ({policy.APPLY_SWITCH_ENV})")
    if request.get("phase") != "awaiting_approval":
        return Refused(409, "phase", f"the request is in phase {request.get('phase')}, not awaiting_approval")
    current = request.get("combined_fingerprint")
    if not current or str(fingerprint).lower() != current:
        return Refused(409, "stale_fingerprint",
                       "the fingerprint differs from the request's current combined fingerprint; reload and approve "
                       "the one shown")
    at = request.get("fingerprint_at")
    if at is None or now - at > validity:
        return Refused(409, "fingerprint_too_old",
                       f"the combined fingerprint is older than {validity}; start a new DRY RUN round")
    if tenant_reasons:
        return Refused(409, "tenant", "; ".join(tenant_reasons))
    return None


def report_refusal(request: Mapping[str, Any], *, mode: str, now: datetime,
                   crm_apply_done: bool) -> Optional[Refused]:
    """Whether the CRM may report ``mode`` for this request now (POST /tenant/v1/erasure-requests/{id}/reports)."""
    phase = request.get("phase")
    if phase in TERMINAL_PHASES:
        return Refused(409, "closed", f"the request is {phase}")
    if mode == "dry_run" and phase != "dry_run":
        return Refused(409, "phase", f"a dry_run report is taken in phase dry_run, not {phase}")
    if mode == "apply":
        if phase != "apply":
            return Refused(409, "phase", f"an apply report is taken in phase apply, not {phase}")
        if now < request["not_before_crm"]:
            return Refused(409, "too_early", "the CRM window has not ended (not_before)")
    if mode == "verify":
        if phase not in ("apply", "verify") or not crm_apply_done:
            return Refused(409, "phase", "a verify report follows the CRM's own apply report")
    return None


def receipt(request: Mapping[str, Any], steps: Mapping[tuple[str, str], Mapping[str, Any]], *, done_at: datetime,
            checklist: list[dict]) -> dict:
    """The erasure receipt: ids, hashes, counts and the census per app; the manual steps outside the database."""
    apps = {}
    for app in APPS:
        dry, app_apply, ver = (steps.get((app, m)) or {} for m in MODES)
        apps[app] = {"plan_hash": dry.get("plan_hash"), "applied": app_apply.get("counts") or {},
                     "pii_left": ver.get("pii_left"),
                     "reported_at": _iso(ver.get("reported_at") or app_apply.get("reported_at"))}
    return {
        "request_id": str(request["request_id"]), "tenant_id": str(request["tenant_id"]), "mode": request["mode"],
        "round": int(request["round"]), "combined_fingerprint": request.get("approved_fingerprint"),
        "approved_by": request.get("approved_by"), "approved_at": _iso(request.get("approved_at")),
        "snapshot_id": request.get("snapshot_id"), "offboarded_at": _iso(request.get("offboarded_at")),
        "done_at": _iso(done_at), "apps": apps,
        "outside_database": checklist,
        "note": "Live posts on the agency's own channels stay there (decision D5); the items under outside_database "
                "are done by hand (runbook section 4).",
    }


def _iso(v: Any) -> Optional[str]:
    if v is None:
        return None
    return v.isoformat() if hasattr(v, "isoformat") else str(v)
