"""AA-1093 lane L2 — the pure rules of the tenant offboarding erasure saga (no I/O; unit-tested). Contract:
root docs/contracts/tenant.erasure.v1.openapi.yaml (byte copy in docs/contracts/ of this repo).

Stages and rounds. Each app has its own not_before (T0 + its exit window: the CRM 90 days, CIS / Booking /
TripPlanner 30 days). A round starts when at least one app that is not done has passed its not_before; the apps
active in the round (round_apps) are fixed then: stage 1 = CIS, Booking, TripPlanner, stage 2 = the CRM alone. Before
the first not_before the request sits in dry_run with no active app (the contract's step status "waiting").

Phases of a request (shared.tenant_erasure_request.phase, CIS 408; the contract's Phase):
  dry_run            every active app reports its DRY RUN plan hash: CIS runs its own data_erasure DRY RUN, Booking
                     and TripPlanner report through their report views, the CRM through POST
                     /tenant/v1/erasure-requests/{id}/reports. A report view that cannot be read is app_not_ready: a
                     blocker of the request, never a skip.
  awaiting_approval  every active app reported an ok plan: the combined fingerprint is set; a Cognito admin approves it
                     within the approval validity (24 h); the admin secret alone is refused.
  apply              the active non-CIS apps apply in parallel (they recompute and must match their approved plan hash:
                     refused / plan_changed otherwise); when CIS is active it applies LAST, after all of them report ok:
                     a tenant backup, a fresh CIS DRY RUN whose saga plan hash must equal the approved one, then the
                     data_erasure apply (which deletes shared.tenants; the request has no FK and outlives it).
  verify             every active app reports the census of its pii: columns for the tenant (pii_rows_found 0). Then
                     those apps are done; if an app is still waiting (the CRM), the request goes back to dry_run with
                     the next round, else done (with the receipt).
  cancelled          a restore of the tenant before any app applied.
  failed             a guard stopped the saga (the pilot tenant).

Guards: the pilot tenant is refused everywhere (it is never scheduled at the offboard and the job fails a request
whose tenant became the pilot); on Dev only a synthetic tenant (copied at T0) may be approved or applied; a drill
(window 0) is for synthetic tenants only; the apply switch is off by default (policy.apply_enabled).
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
# The contract's stages: stage 1 (T0 + 30 days) Booking and TripPlanner, then CIS last; stage 2 (T0 + 90 days) the CRM.
STAGES = (("cis", "booking", "tripplanner"), ("crm",))
PHASES = ("dry_run", "awaiting_approval", "apply", "verify", "done", "failed", "cancelled")
OPEN_PHASES = ("dry_run", "awaiting_approval", "apply", "verify")
TERMINAL_PHASES = ("done", "cancelled", "failed")
# A restore may cancel the request only before any app was asked to erase (store.on_restore also checks that no app
# reported an applied erasure and none is done).
CANCELLABLE_PHASES = ("dry_run", "awaiting_approval")
# Phases in which the job has something to do (awaiting_approval waits for a person).
WORKING_PHASES = ("dry_run", "apply", "verify")
MODES = ("dry_run", "apply", "verify")
REPORT_STATUSES = ("ok", "blocked", "refused", "failed")           # the contract's ErasureReport.status
STEP_STATUSES = ("pending", "app_not_ready") + REPORT_STATUSES     # + CIS's own job running / a report view missing
# A step that stops the phase until a person acts (a new round, or the app's lane fixed).
STOPPING = ("blocked", "refused", "failed")
CONTRACT = "tenant.erasure.v1"

# The CIS tables the saga itself writes for the tenant between the CIS DRY RUN and the CIS apply (its audit rows and
# the tenant backup job row): left out of the CIS saga plan hash, else the approval could never match. The
# data_erasure apply still binds the full fingerprint of its own fresh DRY RUN.
SAGA_VOLATILE_TABLES = frozenset({"acp_shared.audit_log", "shared.job"})

HASH_RE = re.compile(r"^[0-9a-f]{64}$")
MD5_RE = re.compile(r"^[0-9a-f]{32}$")
CODE_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
TABLE_RE = re.compile(r"^[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*$")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
TABLE_ACTIONS = ("delete", "cascade", "detach", "merge", "redact", "anonymise", "keep", "shred")


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


def _open_stage(request: Mapping[str, Any]) -> list[str]:
    """The apps not done yet of the first stage that still has one (STAGES order)."""
    done = set(request.get("done_apps") or ())
    for stage in STAGES:
        left = [app for app in stage if app not in done]
        if left:
            return left
    return []


def due_apps(request: Mapping[str, Any], now: datetime) -> list[str]:
    """The apps a new round would make active: those of the first unfinished stage that are past their not_before.
    A later stage never starts before the earlier one is done (the CRM after CIS, Booking and TripPlanner), so a drill
    (window 0 everywhere) still rehearses both stages."""
    nb = not_before(request)
    return [app for app in _open_stage(request) if now >= nb[app]]


def next_due_at(request: Mapping[str, Any]) -> Optional[datetime]:
    """When the next round may start: the earliest not_before in the first unfinished stage (None: every app done)."""
    nb = not_before(request)
    left = [nb[app] for app in _open_stage(request)]
    return min(left) if left else None


def step_status(request: Mapping[str, Any], app: str, failed_in_round: bool) -> str:
    """The contract's StepStatus of one app: done, failed (active and stopped this round), active, waiting."""
    if app in (request.get("done_apps") or ()):
        return "done"
    if app in (request.get("round_apps") or ()):
        return "failed" if failed_in_round else "active"
    return "waiting"


# ── hashes ──────────────────────────────────────────────────────────────────────────────────────────────────────────

def cis_plan_hash(tenant_id: str, round_: int, rows: Iterable[Mapping[str, Any]]) -> str:
    """The CIS plan hash the saga approves, in the contract's plan-hash form (sha256 of 'tenant.erasure.v1|<tenant>|
    <round>' and one sorted line '<table>|<action>|<rows>|<digest>' per entry; the CIS DRY RUN has no row digests, so
    the digest is empty), without SAGA_VOLATILE_TABLES. Same counts for another tenant or round = another hash."""
    lines = sorted(f"{r['table']}|{r['action']}|{int(r['rows'])}|" for r in rows
                   if str(r["table"]) not in SAGA_VOLATILE_TABLES)
    return hashlib.sha256("\n".join([f"{CONTRACT}|{tenant_id}|{int(round_)}"] + lines).encode()).hexdigest()


def combined_fingerprint(request_id: str, tenant_id: str, round_: int, plans: Mapping[str, str]) -> str:
    """sha256 over the request id, the tenant id, the round and every active app's "app:plan_hash" (sorted): what the
    tech lead approves once for the round. Never crosses the contract (each app sees only its approved plan hash)."""
    if not plans or any(not HASH_RE.match(str(h or "")) for h in plans.values()):
        raise ValueError("combined fingerprint needs a sha256 plan hash for every active app")
    parts = [str(request_id), str(tenant_id), str(int(round_))] + sorted(f"{app}:{h}" for app, h in plans.items())
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


# ── steps ───────────────────────────────────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PhaseState:
    """Where a phase stands from its step rows: ready (every app ok), stopped (a step needs a person), waiting."""
    ready: bool
    stopped: bool
    waiting: tuple[str, ...]
    blockers: tuple[dict, ...]


def census_left(step: Optional[Mapping[str, Any]]) -> Optional[int]:
    census = (step or {}).get("census")
    if isinstance(census, str):
        try:
            census = json.loads(census)
        except ValueError:
            return None
    if not isinstance(census, dict) or census.get("pii_rows_found") is None:
        return None
    return int(census["pii_rows_found"])


def phase_state(steps: Mapping[str, Optional[Mapping[str, Any]]], apps: Iterable[str], *,
                need_hash: bool = False, need_census: bool = False) -> PhaseState:
    """``steps``: app -> its step row of this phase (or None). app_not_ready is a blocker that waits (the app's lane may
    ship); blocked / refused / failed stop the phase; a verify census above 0 stops it too."""
    waiting, blockers, stopped = [], [], False
    for app in apps:
        s = steps.get(app) or {}
        status = s.get("status")
        if status == "ok":
            if need_hash and not HASH_RE.match(str(s.get("plan_hash") or "")):
                blockers.append({"app": app, "kind": "no_plan_hash", "detail": "an ok dry run without a plan hash"})
                stopped = True
            elif need_census and census_left(s) is None:
                waiting.append(app)
            elif need_census and census_left(s) > 0:
                blockers.append({"app": app, "kind": "census_not_zero", "detail": f"{census_left(s)} row(s) left"})
                stopped = True
            continue
        if status == "app_not_ready":
            blockers.append({"app": app, "kind": "app_not_ready", "detail": str(s.get("detail") or "")[:200]})
            waiting.append(app)
        elif status in STOPPING:
            blockers.append({"app": app, "kind": str(s.get("error_code") or status), "detail": status})
            stopped = True
        else:
            waiting.append(app)
    return PhaseState(ready=not waiting and not blockers and not stopped, stopped=stopped, waiting=tuple(waiting),
                      blockers=tuple(blockers))


def _ints(d: Any, keys: tuple[str, ...]) -> Optional[dict]:
    if isinstance(d, str):
        try:
            d = json.loads(d)
        except ValueError:
            return None
    if not isinstance(d, dict):
        return None
    out = {}
    for k in keys:
        v = d.get(k)
        if not isinstance(v, int) or isinstance(v, bool) or v < 0:
            return None
        out[k] = v
    return out


def _list(v: Any) -> list:
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            return []
    return v if isinstance(v, list) else []


def normalise_report(raw: Mapping[str, Any], *, current_round: int) -> Optional[dict]:
    """One app report (a Booking / TripPlanner report-view row as JSON, or the CRM POST body: the contract's
    ErasureReport) in step shape, or None when it is not usable (another round, unknown mode / status, a bad hash or
    code). Ids, codes, hashes and counts only: any other key is dropped."""
    mode, status = str(raw.get("mode") or ""), str(raw.get("status") or "")
    if mode not in MODES or status not in REPORT_STATUSES:
        return None
    try:
        if int(raw.get("round")) != int(current_round):
            return None
    except (TypeError, ValueError):
        return None
    plan_hash = raw.get("plan_hash")
    plan_hash = str(plan_hash).lower() if plan_hash else None
    code = raw.get("error_code")
    if (plan_hash is not None and not HASH_RE.match(plan_hash)) or (code is not None and not CODE_RE.match(str(code))):
        return None
    tables = []
    for t in _list(raw.get("tables"))[:500]:
        if not isinstance(t, dict) or not TABLE_RE.match(str(t.get("table") or "")) \
                or t.get("action") not in TABLE_ACTIONS or not isinstance(t.get("rows"), int) or t["rows"] < 0:
            continue
        digest = t.get("digest")
        tables.append({"table": t["table"], "action": t["action"], "rows": int(t["rows"]),
                       "digest": digest if isinstance(digest, str) and MD5_RE.match(digest) else None})
    blockers = [{"code": b["code"], "count": int(b["count"])} for b in _list(raw.get("blockers"))[:50]
                if isinstance(b, dict) and CODE_RE.match(str(b.get("code") or "")) and isinstance(b.get("count"), int)]
    census = _ints(raw.get("census"), ("pii_columns_checked", "pii_rows_found")) if raw.get("census") else None
    rid, job = str(raw.get("report_id") or "").lower(), str(raw.get("job_id") or "").lower()
    return {"mode": mode, "status": status, "error_code": code, "plan_hash": plan_hash, "tables": tables,
            "blockers": blockers, "census": census, "report_id": rid if UUID_RE.match(rid) else None,
            "job_id": job if UUID_RE.match(job) else None, "reported_at": raw.get("reported_at")}


def latest_reports(rows: Iterable[Mapping[str, Any]], *, current_round: int) -> dict[str, dict]:
    """mode -> the newest usable report of the current round (by reported_at, then the order given)."""
    out: dict[str, dict] = {}
    for raw in rows:
        rep = normalise_report(raw, current_round=current_round)
        if rep is None:
            continue
        prev = out.get(rep["mode"])
        if prev is None or str(rep.get("reported_at") or "") >= str(prev.get("reported_at") or ""):
            out[rep["mode"]] = rep
    return out


def totals(tables: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for t in tables:
        out[str(t["action"])] = out.get(str(t["action"]), 0) + int(t["rows"])
    return out


# ── people and guards ───────────────────────────────────────────────────────────────────────────────────────────────

def request_guard(request: Mapping[str, Any], *, environment: str) -> list[str]:
    """Why the saga may not approve or apply for this request (empty = it may). Reads is_pilot / is_synthetic as copied
    at T0, so it still holds after CIS erased the tenant row (stage 2)."""
    out = []
    if request.get("is_pilot"):
        out.append("the pilot tenant is never erased by the saga (decision D8; handled by hand)")
    if (request.get("mode") == "drill" or environment == "dev") and not request.get("is_synthetic"):
        out.append("only a synthetic tenant may be erased here (Dev, or a drill)")
    return out


def cis_tenant_guard(tenant: Optional[Mapping[str, Any]]) -> list[str]:
    """CIS applies only to an existing, offboarded, non-pilot tenant row (checked again by the data_erasure job)."""
    if tenant is None:
        return ["the tenant row does not exist"]
    out = []
    if tenant.get("is_pilot"):
        out.append("the tenant is the pilot now")
    if tenant.get("cancelled_at") is None:
        out.append("the tenant is not offboarded")
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
        return Refused(403, "apply_disabled", f"the erasure saga apply is off here ({policy.APPLY_SWITCH_ENV})")
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


def report_refusal(request: Mapping[str, Any], *, app: str, mode: str, report_round: int,
                   apply_ok: bool) -> Optional[Refused]:
    """Whether ``app`` may report ``mode`` for this request now (the contract's 409 codes)."""
    phase = request.get("phase")
    if report_round < int(request["round"]):
        return Refused(409, "round_superseded", f"round {report_round} is over; the request is in round "
                                                f"{request['round']}")
    if report_round > int(request["round"]):
        return Refused(422, "invalid", f"the request has no round {report_round} yet")
    if phase in TERMINAL_PHASES or app not in (request.get("round_apps") or ()):
        return Refused(409, "phase_mismatch", f"the {app} step is not active (phase {phase})")
    expected = {"dry_run": ("dry_run",), "apply": ("apply",), "verify": ("apply", "verify")}[mode]
    if phase not in expected:
        return Refused(409, "phase_mismatch", f"a {mode} report is not expected in phase {phase}")
    if mode == "verify" and not apply_ok:
        return Refused(409, "phase_mismatch", "a verify report follows the app's own ok apply report")
    return None


def receipt(request: Mapping[str, Any], steps: Iterable[Mapping[str, Any]], *, done_at: datetime,
            checklist: list[dict]) -> dict:
    """The erasure receipt: per app the round it was erased in, its plan hash, what it erased and its census; the
    manual steps outside the database. Ids, hashes and counts only."""
    by_app: dict[str, dict] = {}
    for st in sorted(steps, key=lambda r: (int(r["round"]), str(r["mode"]))):
        rec = by_app.setdefault(st["app"], {})
        if st["mode"] == "dry_run":
            rec.update(round=int(st["round"]), plan_hash=st.get("plan_hash"))
        elif st["mode"] == "apply" and st.get("status") == "ok":
            rec["applied"] = totals(_list(st.get("tables")))
        elif st["mode"] == "verify" and st.get("status") == "ok":
            rec.update(pii_rows_found=census_left(st), reported_at=_iso(st.get("reported_at")))
    return {
        "request_id": str(request["request_id"]), "tenant_id": str(request["tenant_id"]), "mode": request["mode"],
        "rounds": int(request["round"]), "opened_at": _iso(request.get("opened_at")), "done_at": _iso(done_at),
        "apps": {app: by_app.get(app, {}) for app in APPS},
        "outside_database": checklist,
        "note": "Live posts on the agency's own channels stay there (decision D5); the items under outside_database "
                "are done by hand (runbook section 4).",
    }


def _iso(v: Any) -> Optional[str]:
    if v is None:
        return None
    return v.isoformat() if hasattr(v, "isoformat") else str(v)
