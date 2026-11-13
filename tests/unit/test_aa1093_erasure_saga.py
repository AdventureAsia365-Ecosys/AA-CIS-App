"""AA-1093 lane L2 — the pure rules of the tenant offboarding erasure saga (services/tenant_erasure/saga.py, policy.py)
and the saga job's payload and registration. No DB (the phases on real Postgres: tests/integration/
test_aa1093_erasure_saga_db.py).

  * schedule: the CRM 90 days after T0, CIS / Booking / TripPlanner the retention window (30 days), a drill 0; the DRY
    RUN round starts at the latest not_before;
  * the combined fingerprint (order-free, bound to request and tenant, every app required) and the CIS saga plan hash
    (without the tables the saga itself writes);
  * phase state: app_not_ready is a blocker that waits (never a skip), failed / plan_changed stop, a census above 0
    stops;
  * the approval: refused for a non-Cognito caller (the admin secret alone), the switch off, a wrong phase, a stale
    fingerprint, a fingerprint older than 24 h, the pilot / a non-synthetic tenant on Dev;
  * the CRM report rules (its 90-day window, verify only after its own apply).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from services.jobs import tenant_erasure_saga_job as sj
from services.tenant_erasure import policy, saga
from shared.jobs.registry import NonRetryable, get_kind

T0 = datetime(2026, 11, 13, 9, 0, tzinfo=timezone.utc)
RID = "10930000-0000-4000-8000-0000000000a1"
TID = "10930000-0000-4000-8000-0000000000b1"
H = {app: (c * 64) for app, c in zip(saga.APPS, "abcd")}
ENV = {"ENVIRONMENT": "dev"}


def _request(**over) -> dict:
    nb = saga.schedule(T0, "live", {})
    req = {"request_id": RID, "tenant_id": TID, "mode": "live", "phase": "awaiting_approval", "round": 1,
           "offboarded_at": T0, **{f"not_before_{a}": nb[a] for a in saga.APPS},
           "combined_fingerprint": "e" * 64, "fingerprint_at": T0 + timedelta(days=90)}
    req.update(over)
    return req


# ── schedule ────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_crm_gets_90_days_and_the_others_the_retention_window():
    nb = saga.schedule(T0, "live", {})
    assert nb["crm"] == T0 + timedelta(days=90)
    assert nb["cis"] == nb["booking"] == nb["tripplanner"] == T0 + timedelta(days=30)


def test_the_others_follow_the_cis_retention_window_setting():
    nb = saga.schedule(T0, "live", {"ERASURE_TENANT_RETENTION_DAYS": "45"})
    assert nb["booking"] == T0 + timedelta(days=45) and nb["crm"] == T0 + timedelta(days=90)


def test_a_drill_has_window_zero_for_every_app():
    assert set(saga.schedule(T0, "drill", {}).values()) == {T0}


def test_the_round_starts_at_the_latest_not_before():
    assert saga.due_at(_request()) == T0 + timedelta(days=90)
    assert saga.due_at(_request(**{f"not_before_{a}": T0 for a in saga.APPS})) == T0


# ── hashes ──────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_combined_fingerprint_is_order_free_and_bound_to_request_and_tenant():
    fp = saga.combined_fingerprint(RID, TID, H)
    assert len(fp) == 64 and fp == saga.combined_fingerprint(RID, TID, dict(reversed(list(H.items()))))
    assert fp != saga.combined_fingerprint(RID, RID, H)
    assert fp != saga.combined_fingerprint(TID, TID, H)
    assert fp != saga.combined_fingerprint(RID, TID, {**H, "crm": "f" * 64})


def test_combined_fingerprint_needs_every_app():
    with pytest.raises(ValueError, match="crm"):
        saga.combined_fingerprint(RID, TID, {k: v for k, v in H.items() if k != "crm"})


def test_cis_plan_hash_ignores_the_tables_the_saga_writes():
    rows = [{"table": "shared.tenants", "action": "delete", "rows": 1},
            {"table": "acp_shared.audit_log", "action": "keep", "rows": 7},
            {"table": "shared.job", "action": "delete", "rows": 3}]
    more = [dict(r, rows=r["rows"] + 2) if r["table"] in saga.SAGA_VOLATILE_TABLES else r for r in rows]
    assert saga.cis_plan_hash(TID, rows) == saga.cis_plan_hash(TID, more)
    assert saga.cis_plan_hash(TID, rows) != saga.cis_plan_hash(RID, rows)
    changed = [dict(rows[0], rows=2)] + rows[1:]
    assert saga.cis_plan_hash(TID, rows) != saga.cis_plan_hash(TID, changed)


# ── phase state ─────────────────────────────────────────────────────────────────────────────────────────────────────

def _done(app, **kw):
    return {"app": app, "status": "done", "plan_hash": H[app], **kw}


def test_every_app_done_with_a_hash_is_ready():
    st = saga.phase_state({a: _done(a) for a in saga.APPS}, saga.APPS, need_hash=True)
    assert st.ready and not st.blockers and not st.waiting


def test_a_missing_report_view_is_a_blocker_never_a_skip():
    steps = {a: _done(a) for a in saga.APPS}
    steps["booking"] = {"status": "app_not_ready", "detail": "report view not readable"}
    st = saga.phase_state(steps, saga.APPS, need_hash=True)
    assert not st.ready and not st.stopped and st.waiting == ("booking",)
    assert st.blockers[0]["kind"] == "app_not_ready" and st.blockers[0]["app"] == "booking"


def test_no_report_yet_waits_without_a_blocker():
    steps = {a: _done(a) for a in saga.APPS}
    steps["crm"] = None
    st = saga.phase_state(steps, saga.APPS, need_hash=True)
    assert not st.ready and st.waiting == ("crm",) and not st.blockers


@pytest.mark.parametrize("status", ["failed", "plan_changed", "refused"])
def test_a_failed_step_stops_the_phase(status):
    steps = {a: _done(a) for a in saga.APPS}
    steps["tripplanner"] = {"status": status, "detail": "x"}
    st = saga.phase_state(steps, saga.APPS)
    assert st.stopped and not st.ready and st.blockers[0]["kind"] == status


def test_done_without_a_plan_hash_stops_the_dry_run():
    steps = {a: _done(a) for a in saga.APPS}
    steps["crm"] = {"status": "done", "plan_hash": None}
    assert saga.phase_state(steps, saga.APPS, need_hash=True).stopped


def test_the_census_must_be_zero_everywhere():
    steps = {a: _done(a, pii_left=0) for a in saga.APPS}
    assert saga.phase_state(steps, saga.APPS, need_census=True).ready
    steps["crm"] = _done("crm", pii_left=3)
    st = saga.phase_state(steps, saga.APPS, need_census=True)
    assert st.stopped and st.blockers[0]["kind"] == "pii_left"
    steps["crm"] = _done("crm", pii_left=None)
    assert saga.phase_state(steps, saga.APPS, need_census=True).waiting == ("crm",)


# ── reports ─────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_a_report_keeps_ids_codes_hashes_and_counts_only():
    rep = saga.normalise_report({"request_id": RID, "round": 1, "mode": "dry_run", "status": "done",
                                 "plan_hash": "A" * 64, "counts": {"delete": 4, "label": "x"}, "agency_name": "Zed",
                                 "reported_at": "2026-12-01T00:00:00Z"}, current_round=1)
    assert rep["plan_hash"] == "a" * 64 and rep["counts"] == {"delete": 4} and "agency_name" not in rep


@pytest.mark.parametrize("raw", [
    {"mode": "dry_run", "status": "done", "round": 2},          # another round
    {"mode": "erase", "status": "done"},                         # unknown mode
    {"mode": "dry_run", "status": "pending"},                    # not a report
    {"mode": "dry_run", "status": "done", "plan_hash": "zz"},    # not a hash
    {"mode": "verify", "status": "done", "pii_left": -1},        # negative census
])
def test_unusable_reports_are_dropped(raw):
    assert saga.normalise_report(raw, current_round=1) is None


def test_latest_report_per_mode_wins():
    rows = [{"mode": "dry_run", "status": "failed", "reported_at": "2026-12-01T00:00:00Z"},
            {"mode": "dry_run", "status": "done", "plan_hash": "b" * 64, "reported_at": "2026-12-02T00:00:00Z"},
            {"mode": "apply", "status": "done", "reported_at": "2026-12-03T00:00:00Z"}]
    out = saga.latest_reports(rows, current_round=1)
    assert out["dry_run"]["status"] == "done" and out["apply"]["status"] == "done"


# ── guards and the approval ─────────────────────────────────────────────────────────────────────────────────────────

def test_tenant_guard_refuses_the_pilot_and_non_synthetic_on_dev_and_in_a_drill():
    off = {"is_pilot": False, "cancelled_at": T0}
    assert saga.tenant_guard(off, environment="dev", synthetic=True) == []
    assert saga.tenant_guard(off, environment="prod", synthetic=False) == []
    assert any("synthetic" in r for r in saga.tenant_guard(off, environment="dev", synthetic=False))
    assert any("synthetic" in r for r in saga.tenant_guard(off, environment="prod", synthetic=False, drill=True))
    assert any("pilot" in r for r in saga.tenant_guard({**off, "is_pilot": True}, environment="prod",
                                                        synthetic=False))
    assert any("not offboarded" in r for r in saga.tenant_guard({**off, "cancelled_at": None}, environment="prod",
                                                                 synthetic=True))
    assert saga.tenant_guard(None, environment="prod", synthetic=False)


def _approve(req=None, **kw):
    args = dict(fingerprint="e" * 64, now=T0 + timedelta(days=90, hours=1), cognito_admin=True, apply_on=True,
                tenant_reasons=[], validity=timedelta(hours=24))
    args.update(kw)
    return saga.approval_refusal(req or _request(), **args)


def test_a_cognito_admin_approves_the_current_fingerprint():
    assert _approve() is None


def test_the_admin_secret_alone_is_refused():
    r = _approve(cognito_admin=False)
    assert r.status == 403 and r.code == "needs_cognito_admin"


def test_approval_refused_while_the_switch_is_off():
    assert _approve(apply_on=False).code == "apply_off"


def test_approval_refused_for_a_stale_fingerprint():
    r = _approve(fingerprint="f" * 64)
    assert r.status == 409 and r.code == "stale_fingerprint"


def test_approval_refused_when_the_fingerprint_is_older_than_24_hours():
    r = _approve(now=T0 + timedelta(days=91, minutes=1))
    assert r.status == 409 and r.code == "fingerprint_too_old"


def test_approval_refused_outside_awaiting_approval_and_for_a_refused_tenant():
    assert _approve(_request(phase="dry_run")).code == "phase"
    assert _approve(tenant_reasons=["the pilot tenant is never erased"]).code == "tenant"


def test_crm_reports_follow_its_window_and_its_own_apply():
    req = _request(phase="apply")
    early = T0 + timedelta(days=60)
    assert saga.report_refusal(req, mode="apply", now=early, crm_apply_done=False).code == "too_early"
    late = T0 + timedelta(days=91)
    assert saga.report_refusal(req, mode="apply", now=late, crm_apply_done=False) is None
    assert saga.report_refusal(req, mode="verify", now=late, crm_apply_done=False).code == "phase"
    assert saga.report_refusal(req, mode="verify", now=late, crm_apply_done=True) is None
    assert saga.report_refusal(req, mode="dry_run", now=late, crm_apply_done=False).code == "phase"
    assert saga.report_refusal(_request(phase="cancelled"), mode="dry_run", now=late,
                               crm_apply_done=False).code == "closed"


# ── policy and the job ──────────────────────────────────────────────────────────────────────────────────────────────

def test_apply_switch_is_off_by_default():
    assert policy.apply_enabled({}) is False
    assert policy.apply_enabled({policy.APPLY_SWITCH_ENV: "on"}) is True
    assert policy.apply_enabled({policy.APPLY_SWITCH_ENV: "yes please"}) is False


def test_approval_validity_follows_the_dry_run_validity():
    assert policy.approval_validity({}) == timedelta(hours=24)


def test_job_payload_is_the_request_id_only():
    assert sj.validate_payload({"request_id": RID.upper()}) == {"request_id": RID}
    for bad in ({}, {"request_id": "x"}, {"request_id": RID, "tenant_id": TID}):
        with pytest.raises(NonRetryable):
            sj.validate_payload(bad)


def test_kind_is_registered_background():
    k = get_kind(sj.KIND)
    assert k is not None and k.background and k.concurrency == 1 and not k.deploy_blocking


def test_receipt_holds_hashes_counts_and_the_manual_checklist():
    req = _request(phase="verify", approved_fingerprint="e" * 64, approved_by="admin:x", approved_at=T0,
                   snapshot_id="snap-1")
    steps = {(a, "dry_run"): {"plan_hash": H[a]} for a in saga.APPS}
    steps.update({(a, "verify"): {"pii_left": 0, "reported_at": T0} for a in saga.APPS})
    doc = saga.receipt(req, steps, done_at=T0, checklist=[{"owner": "AWS", "item": "x", "how": "y"}])
    assert doc["apps"]["crm"]["plan_hash"] == H["crm"] and doc["apps"]["cis"]["pii_left"] == 0
    assert doc["combined_fingerprint"] == "e" * 64 and doc["outside_database"]
