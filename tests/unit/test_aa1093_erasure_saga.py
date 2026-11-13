"""AA-1093 lane L2 — the pure rules of the tenant offboarding erasure saga (services/tenant_erasure/saga.py, policy.py;
contract tenant.erasure.v1) and the saga job's payload and registration. No DB (the phases on real Postgres:
tests/integration/test_aa1093_erasure_saga_db.py).

  * schedule: the CRM 90 days after T0, CIS / Booking / TripPlanner the retention window (30 days), a drill 0; a round
    makes active the apps past their not_before that are not done (stage 1, then the CRM alone);
  * the contract's step status (waiting, active, failed, done);
  * the combined fingerprint (order-free, bound to request, tenant and round) and the CIS saga plan hash (bound to
    tenant and round, without the tables the saga itself writes);
  * phase state: app_not_ready is a blocker that waits (never a skip), blocked / refused / failed stop, a census above
    0 stops;
  * report normalisation (the contract's ErasureReport: round, sha256, codes, no row values);
  * the approval: refused for a non-Cognito caller (the admin secret alone), the switch off, a wrong phase, a stale
    fingerprint, a fingerprint older than 24 h, the pilot / a non-synthetic tenant on Dev;
  * the CRM report rules (round_superseded, phase_mismatch, verify after its own apply).
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


def _request(**over) -> dict:
    nb = saga.schedule(T0, "live", {})
    req = {"request_id": RID, "tenant_id": TID, "mode": "live", "phase": "awaiting_approval", "round": 1,
           "opened_at": T0, "is_pilot": False, "is_synthetic": True, **{f"not_before_{a}": nb[a] for a in saga.APPS},
           "round_apps": ["cis", "booking", "tripplanner"], "done_apps": [],
           "combined_fingerprint": "e" * 64, "fingerprint_at": T0 + timedelta(days=30)}
    req.update(over)
    return req


# ── schedule and stages ─────────────────────────────────────────────────────────────────────────────────────────────

def test_crm_gets_90_days_and_the_others_the_retention_window():
    nb = saga.schedule(T0, "live", {})
    assert nb["crm"] == T0 + timedelta(days=90)
    assert nb["cis"] == nb["booking"] == nb["tripplanner"] == T0 + timedelta(days=30)


def test_the_others_follow_the_cis_retention_window_setting():
    nb = saga.schedule(T0, "live", {"ERASURE_TENANT_RETENTION_DAYS": "45"})
    assert nb["booking"] == T0 + timedelta(days=45) and nb["crm"] == T0 + timedelta(days=90)


def test_a_drill_has_window_zero_for_every_app():
    assert set(saga.schedule(T0, "drill", {}).values()) == {T0}


def test_stage_one_then_the_crm_alone():
    req = _request(round_apps=[], phase="dry_run")
    assert saga.due_apps(req, T0 + timedelta(days=29)) == []
    assert saga.next_due_at(req) == T0 + timedelta(days=30)
    assert saga.due_apps(req, T0 + timedelta(days=30)) == ["cis", "booking", "tripplanner"]
    after = _request(round_apps=[], done_apps=["cis", "booking", "tripplanner"], phase="dry_run", round=2)
    assert saga.due_apps(after, T0 + timedelta(days=60)) == []
    assert saga.next_due_at(after) == T0 + timedelta(days=90)
    assert saga.due_apps(after, T0 + timedelta(days=90)) == ["crm"]
    assert saga.next_due_at(_request(done_apps=list(saga.APPS), round_apps=[])) is None


def test_a_drill_still_runs_the_two_stages():
    drill = _request(round_apps=[], phase="dry_run", mode="drill", **{f"not_before_{a}": T0 for a in saga.APPS})
    assert saga.due_apps(drill, T0) == ["cis", "booking", "tripplanner"]
    assert saga.due_apps({**drill, "done_apps": ["cis", "booking", "tripplanner"]}, T0) == ["crm"]


def test_step_status_follows_the_contract():
    req = _request(round_apps=["crm"], done_apps=["cis", "booking", "tripplanner"])
    assert saga.step_status(req, "cis", False) == "done"
    assert saga.step_status(req, "crm", False) == "active"
    assert saga.step_status(req, "crm", True) == "failed"
    assert saga.step_status(_request(), "crm", False) == "waiting"


# ── hashes ──────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_combined_fingerprint_is_order_free_and_bound_to_request_tenant_and_round():
    plans = {a: H[a] for a in ("cis", "booking", "tripplanner")}
    fp = saga.combined_fingerprint(RID, TID, 1, plans)
    assert len(fp) == 64 and fp == saga.combined_fingerprint(RID, TID, 1, dict(reversed(list(plans.items()))))
    assert fp != saga.combined_fingerprint(RID, RID, 1, plans)
    assert fp != saga.combined_fingerprint(TID, TID, 1, plans)
    assert fp != saga.combined_fingerprint(RID, TID, 2, plans)
    assert fp != saga.combined_fingerprint(RID, TID, 1, {**plans, "booking": "f" * 64})


def test_combined_fingerprint_needs_a_hash_for_every_active_app():
    with pytest.raises(ValueError):
        saga.combined_fingerprint(RID, TID, 1, {"cis": H["cis"], "booking": None})
    with pytest.raises(ValueError):
        saga.combined_fingerprint(RID, TID, 1, {})


def test_cis_plan_hash_ignores_the_tables_the_saga_writes_and_binds_the_round():
    rows = [{"table": "shared.tenants", "action": "delete", "rows": 1},
            {"table": "acp_shared.audit_log", "action": "keep", "rows": 7},
            {"table": "shared.job", "action": "delete", "rows": 3}]
    more = [dict(r, rows=r["rows"] + 2) if r["table"] in saga.SAGA_VOLATILE_TABLES else r for r in rows]
    assert saga.cis_plan_hash(TID, 1, rows) == saga.cis_plan_hash(TID, 1, more)
    assert saga.cis_plan_hash(TID, 1, rows) != saga.cis_plan_hash(RID, 1, rows)
    assert saga.cis_plan_hash(TID, 1, rows) != saga.cis_plan_hash(TID, 2, rows)
    changed = [dict(rows[0], rows=2)] + rows[1:]
    assert saga.cis_plan_hash(TID, 1, rows) != saga.cis_plan_hash(TID, 1, changed)


# ── phase state ─────────────────────────────────────────────────────────────────────────────────────────────────────

ACTIVE = ("cis", "booking", "tripplanner")


def _ok(app, **kw):
    return {"app": app, "status": "ok", "plan_hash": H[app], **kw}


def test_every_active_app_ok_with_a_hash_is_ready():
    st = saga.phase_state({a: _ok(a) for a in ACTIVE}, ACTIVE, need_hash=True)
    assert st.ready and not st.blockers and not st.waiting


def test_a_missing_report_view_is_a_blocker_never_a_skip():
    steps = {a: _ok(a) for a in ACTIVE}
    steps["booking"] = {"status": "app_not_ready", "detail": "report view not readable"}
    st = saga.phase_state(steps, ACTIVE, need_hash=True)
    assert not st.ready and not st.stopped and st.waiting == ("booking",)
    assert st.blockers[0]["kind"] == "app_not_ready" and st.blockers[0]["app"] == "booking"


def test_no_report_yet_waits_without_a_blocker():
    steps = {a: _ok(a) for a in ACTIVE}
    steps["tripplanner"] = None
    st = saga.phase_state(steps, ACTIVE, need_hash=True)
    assert not st.ready and st.waiting == ("tripplanner",) and not st.blockers


@pytest.mark.parametrize("status", ["blocked", "refused", "failed"])
def test_a_stopped_step_stops_the_phase(status):
    steps = {a: _ok(a) for a in ACTIVE}
    steps["tripplanner"] = {"status": status, "error_code": "plan_changed"}
    st = saga.phase_state(steps, ACTIVE)
    assert st.stopped and not st.ready and st.blockers[0]["kind"] == "plan_changed"


def test_ok_without_a_plan_hash_stops_the_dry_run():
    steps = {a: _ok(a) for a in ACTIVE}
    steps["booking"] = {"status": "ok", "plan_hash": None}
    assert saga.phase_state(steps, ACTIVE, need_hash=True).stopped


def test_the_census_must_be_zero_everywhere():
    def census(n):
        return {"pii_columns_checked": 4, "pii_rows_found": n}
    steps = {a: _ok(a, census=census(0)) for a in ACTIVE}
    assert saga.phase_state(steps, ACTIVE, need_census=True).ready
    steps["booking"] = _ok("booking", census=census(3))
    st = saga.phase_state(steps, ACTIVE, need_census=True)
    assert st.stopped and st.blockers[0]["kind"] == "census_not_zero"
    steps["booking"] = _ok("booking", census=None)
    assert saga.phase_state(steps, ACTIVE, need_census=True).waiting == ("booking",)


# ── reports ─────────────────────────────────────────────────────────────────────────────────────────────────────────

REPORT = {"report_id": "10930000-0000-4000-8000-0000000000c1", "request_id": RID, "tenant_id": TID, "app": "booking",
          "round": 1, "mode": "dry_run", "status": "ok", "error_code": None, "plan_hash": "A" * 64,
          "tables": [{"table": "booking.trip_cases", "action": "anonymise", "rows": 4, "digest": "1" * 32},
                     {"table": "Not A Table", "action": "delete", "rows": 1, "digest": None},
                     {"table": "booking.notes", "action": "drop", "rows": 1, "digest": None}],
          "blockers": [], "census": None, "job_id": None, "reported_at": "2026-12-01T00:00:00Z",
          "agency_name": "Zed Trails"}


def test_a_report_keeps_ids_codes_hashes_and_counts_only():
    rep = saga.normalise_report(REPORT, current_round=1)
    assert rep["plan_hash"] == "a" * 64 and rep["report_id"] == REPORT["report_id"]
    assert rep["tables"] == [{"table": "booking.trip_cases", "action": "anonymise", "rows": 4, "digest": "1" * 32}]
    assert "agency_name" not in rep


@pytest.mark.parametrize("over", [
    {"round": 2},                     # another round
    {"round": None},                  # a report always names its round
    {"mode": "erase"},                # unknown mode
    {"status": "done"},               # not a contract status
    {"plan_hash": "a" * 32},          # not a sha256
    {"error_code": "Plan Changed"},   # not a code
])
def test_unusable_reports_are_dropped(over):
    assert saga.normalise_report({**REPORT, **over}, current_round=1) is None


def test_report_rows_outside_the_contract_shape_are_flagged():
    older_round = {**REPORT, "round": 2}
    assert not saga.report_shape_unknown([])                       # no row yet: wait, no blocker
    assert not saga.report_shape_unknown([older_round])            # well formed, only another round
    off_contract = {"request_id": REPORT["request_id"], "mode": "apply", "status": "applied", "reason": None,
                    "counts": {"deleted": 2}}                       # no round, own status words
    assert saga.report_shape_unknown([off_contract])
    assert not saga.report_shape_unknown([off_contract, REPORT])   # one contract row is enough


def test_an_app_not_ready_blocker_names_its_code():
    steps = {a: _ok(a) for a in ACTIVE}
    steps["tripplanner"] = {"status": "app_not_ready", "error_code": "report_shape_unknown"}
    st = saga.phase_state(steps, ACTIVE, need_hash=True)
    assert not st.ready and st.blockers[0]["detail"] == "report_shape_unknown"


def test_latest_report_per_mode_wins():
    rows = [{**REPORT, "status": "failed", "plan_hash": None, "reported_at": "2026-12-01T00:00:00Z"},
            {**REPORT, "reported_at": "2026-12-02T00:00:00Z"},
            {**REPORT, "mode": "apply", "reported_at": "2026-12-03T00:00:00Z"}]
    out = saga.latest_reports(rows, current_round=1)
    assert out["dry_run"]["status"] == "ok" and out["apply"]["status"] == "ok"


# ── guards and the approval ─────────────────────────────────────────────────────────────────────────────────────────

def test_request_guard_refuses_the_pilot_and_non_synthetic_on_dev_and_in_a_drill():
    assert saga.request_guard(_request(), environment="dev") == []
    assert saga.request_guard(_request(is_synthetic=False), environment="prod") == []
    assert any("synthetic" in r for r in saga.request_guard(_request(is_synthetic=False), environment="dev"))
    assert any("synthetic" in r for r in saga.request_guard(_request(is_synthetic=False, mode="drill"),
                                                             environment="prod"))
    assert any("pilot" in r for r in saga.request_guard(_request(is_pilot=True), environment="prod"))


def test_cis_tenant_guard_needs_an_offboarded_non_pilot_row():
    assert saga.cis_tenant_guard({"is_pilot": False, "cancelled_at": T0}) == []
    assert saga.cis_tenant_guard(None)
    assert saga.cis_tenant_guard({"is_pilot": True, "cancelled_at": T0})
    assert saga.cis_tenant_guard({"is_pilot": False, "cancelled_at": None})


def _approve(req=None, **kw):
    args = dict(fingerprint="e" * 64, now=T0 + timedelta(days=30, hours=1), cognito_admin=True, apply_on=True,
                tenant_reasons=[], validity=timedelta(hours=24))
    args.update(kw)
    return saga.approval_refusal(req or _request(), **args)


def test_a_cognito_admin_approves_the_current_fingerprint():
    assert _approve() is None


def test_the_admin_secret_alone_is_refused():
    r = _approve(cognito_admin=False)
    assert r.status == 403 and r.code == "needs_cognito_admin"


def test_approval_refused_while_the_switch_is_off():
    assert _approve(apply_on=False).code == "apply_disabled"


def test_approval_refused_for_a_stale_fingerprint():
    r = _approve(fingerprint="f" * 64)
    assert r.status == 409 and r.code == "stale_fingerprint"


def test_approval_refused_when_the_fingerprint_is_older_than_24_hours():
    r = _approve(now=T0 + timedelta(days=31, minutes=1))
    assert r.status == 409 and r.code == "fingerprint_too_old"


def test_approval_refused_outside_awaiting_approval_and_for_a_refused_tenant():
    assert _approve(_request(phase="dry_run")).code == "phase"
    assert _approve(tenant_reasons=["the pilot tenant is never erased"]).code == "tenant"


def test_crm_report_rules():
    stage2 = _request(phase="dry_run", round=2, round_apps=["crm"], done_apps=["cis", "booking", "tripplanner"])
    assert saga.report_refusal(stage2, app="crm", mode="dry_run", report_round=2, apply_ok=False) is None
    assert saga.report_refusal(stage2, app="crm", mode="dry_run", report_round=1,
                               apply_ok=False).code == "round_superseded"
    assert saga.report_refusal(stage2, app="crm", mode="apply", report_round=2, apply_ok=False).code == \
        "phase_mismatch"
    applying = {**stage2, "phase": "apply"}
    assert saga.report_refusal(applying, app="crm", mode="apply", report_round=2, apply_ok=False) is None
    assert saga.report_refusal(applying, app="crm", mode="verify", report_round=2, apply_ok=False).code == \
        "phase_mismatch"
    assert saga.report_refusal(applying, app="crm", mode="verify", report_round=2, apply_ok=True) is None
    # stage 1: the CRM is waiting, so it has nothing to report
    assert saga.report_refusal(_request(phase="dry_run"), app="crm", mode="dry_run", report_round=1,
                               apply_ok=False).code == "phase_mismatch"


# ── policy and the job ──────────────────────────────────────────────────────────────────────────────────────────────

def test_the_knobs_are_registry_settings_with_the_decided_defaults(monkeypatch):
    assert policy.apply_enabled() is False                                       # default off
    assert policy.approval_validity() == timedelta(hours=24)                    # D7
    assert policy.windows("live", {})["crm"] == timedelta(days=90)              # D2
    seen = []

    def fake(key):
        seen.append(key)
        return {policy.APPLY_SETTING: True, policy.APPROVAL_VALID_SETTING: 6, policy.CRM_WINDOW_SETTING: 120}[key]
    monkeypatch.setattr(policy._settings, "get", fake)
    assert policy.apply_enabled() is True and policy.approval_validity() == timedelta(hours=6)
    assert policy.windows("live", {})["crm"] == timedelta(days=120)
    assert set(seen) == {policy.APPLY_SETTING, policy.APPROVAL_VALID_SETTING, policy.CRM_WINDOW_SETTING}


def test_a_build_without_the_compiled_key_keeps_the_decided_values(monkeypatch):
    def missing(key):
        raise KeyError(key)
    monkeypatch.setattr(policy._settings, "get", missing)
    assert policy.apply_enabled() is False and policy.approval_validity() == timedelta(hours=24)
    assert policy.windows("live", {})["crm"] == timedelta(days=90)


def test_job_payload_is_the_request_id_only():
    assert sj.validate_payload({"request_id": RID.upper()}) == {"request_id": RID}
    for bad in ({}, {"request_id": "x"}, {"request_id": RID, "tenant_id": TID}):
        with pytest.raises(NonRetryable):
            sj.validate_payload(bad)


def test_kind_is_registered_background():
    k = get_kind(sj.KIND)
    assert k is not None and k.background and k.concurrency == 1 and not k.deploy_blocking


def test_blocker_kinds_become_contract_codes():
    assert sj._code("live_job") == "live_job" and sj._code("S3 refs!") == "s3_refs"
    assert sj._code(None) == "blocked" and sj._code("9x") == "blocked"


def test_receipt_holds_hashes_counts_census_and_the_manual_checklist():
    steps = []
    for app, rnd in (("cis", 1), ("booking", 1), ("tripplanner", 1), ("crm", 2)):
        steps += [{"app": app, "round": rnd, "mode": "dry_run", "status": "ok", "plan_hash": H[app]},
                  {"app": app, "round": rnd, "mode": "apply", "status": "ok",
                   "tables": [{"table": "x.y", "action": "delete", "rows": 3, "digest": None}]},
                  {"app": app, "round": rnd, "mode": "verify", "status": "ok", "reported_at": T0,
                   "census": {"pii_columns_checked": 2, "pii_rows_found": 0}}]
    doc = saga.receipt(_request(round=2), steps, done_at=T0, checklist=[{"owner": "AWS", "item": "x", "how": "y"}])
    assert doc["apps"]["crm"] == {"round": 2, "plan_hash": H["crm"], "applied": {"delete": 3}, "pii_rows_found": 0,
                                  "reported_at": T0.isoformat()}
    assert doc["apps"]["cis"]["round"] == 1 and doc["outside_database"]
