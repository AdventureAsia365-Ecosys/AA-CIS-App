"""AA-1090 TripPlanner R8 — the tripplanner_custom_domain job (services/jobs/tripplanner_custom_domain_job.py) on
in-memory rows with the fake ACM / CloudFront clients of test_aa1090_custom_domain.py: the add flow, a retry after a
validation timeout (a full window), removals that re-run cleanly after EdgeBusy, skip the edge for a host that was never
served or get stuck and are re-queued, one agency's missing validation record detached without blocking another's
change, the daily edge check and its schedule. No AWS, no database. Invented hosts only.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.jobs import tripplanner_custom_domain_job as job
from services.tenant_account import tp_edge
from shared.jobs.registry import NonRetryable, Reschedule, get_kind
from tests.unit.test_aa1090_custom_domain import (DID, DIST, HOST_CERT, L, NOW, Q, TARGET, FakeAcm, FakeCf, _attached,
                                                  _row)


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv(tp_edge.DIST_ENV, DIST)

# ── the job ─────────────────────────────────────────────────────────────────────────────────────────────────────

class FakePool:
    """Domain rows by id behind the job's SQL constants (the job's own row is ``row``)."""

    def __init__(self, row: dict | None, *others: dict):
        self.rows = {r["domain_id"]: r for r in (row, *others) if r is not None}
        self.calls: list[tuple] = []

    @property
    def row(self):
        return self.rows.get(DID)

    def by_domain(self, domain):
        return next(r for r in self.rows.values() if r["domain"] == domain)

    async def fetchrow(self, sql, *args):
        assert sql == job.ROW_SQL
        r = self.rows.get(args[0])
        return dict(r) if r is not None else None

    async def fetch(self, sql, *args):
        if sql == job.EDGE_HOSTS_SQL:
            return [{"domain": r["domain"]} for r in sorted(self.rows.values(), key=lambda r: r["domain"])
                    if r["status"] in ("attaching", "active")]
        assert sql == job.DETACH_SQL
        out = []
        for r in self.rows.values():
            if r["domain"] in args[0] and r["status"] in ("attaching", "active"):
                r.update(status="failed", failure_code=args[1], failure_message=args[2])
                out.append({"domain_id": r["domain_id"], "tenant_id": r["tenant_id"], "domain": r["domain"]})
        return out

    async def execute(self, sql, *args):
        self.calls.append((sql, args))
        r = self.rows.get(args[0])
        if r is None:
            return
        if sql == job.SET_CERT_SQL:
            r.update(certificate_arn=args[1], requested_at=datetime.now(timezone.utc))
        elif sql == job.CLEAR_CERT_SQL and r["certificate_arn"] == args[1]:
            r["certificate_arn"] = None
        elif sql == job.VALIDATING_SQL and r["status"] == "pending":
            r.update(status="validating", validation_name=args[1], validation_value=args[2], cname_target=args[3])
        elif sql == job.ATTACHING_SQL and r["status"] == "validating":
            r.update(status="attaching", validated_at=datetime.now(timezone.utc))
        elif sql == job.ACTIVE_SQL and r["status"] == "attaching":
            r.update(status="active", cname_target=args[1])
        elif sql == job.FAILED_SQL and r["status"] in ("pending", "validating", "attaching"):
            r.update(status="failed", failure_code=args[1], failure_message=args[2])
            if args[3]:
                r["certificate_arn"] = None
        elif sql == job.REMOVE_STUCK_SQL and r["status"] == "removing":
            r.update(failure_code=args[1], failure_message=args[2])
        elif sql == job.DELETE_SQL and r["status"] == "removing":
            del self.rows[args[0]]


OTHER = "1090a000-0000-4000-8000-0000000000d2"




def _other(status="active", **kw):
    return _row(status, domain_id=OTHER, domain=L, **kw)


def _ctx(pool, progress=None, payload=None):
    return SimpleNamespace(pool=pool, payload=payload or {"domain_id": DID}, job_id="job-1090",
                           job=SimpleNamespace(progress=dict(progress or {})), set_result=lambda r: None)


@pytest.fixture
def knobs(monkeypatch, env):
    values = {job.POLL_KEY: 120, job.TIMEOUT_KEY: 72, job.EDGE_PENDING_KEY: 6, job.RENEWAL_GRACE_KEY: 30}
    monkeypatch.setattr(job.settings, "aget", AsyncMock(side_effect=lambda k, *a: values[k]))
    alert = AsyncMock(return_value={"bell": True})
    monkeypatch.setattr(job.ops_checks, "raise_alert", alert)
    return SimpleNamespace(values=values, alert=alert)


def _run(ctx, acm, cf):
    return asyncio.run(job.run_once(ctx, acm, cf))


def _resume(ctx, exc: Reschedule):
    ctx.job.progress = {**ctx.job.progress, **exc.progress}


def _until_done(ctx, acm, cf, *, deploy=True, limit=8):
    """Run the job until it returns, resuming each reschedule (CloudFront deploys between two runs)."""
    for _ in range(limit):
        try:
            return _run(ctx, acm, cf)
        except Reschedule as r:
            _resume(ctx, r)
            if deploy:
                cf.status = "Deployed"
                acm.in_use.clear()
    raise AssertionError("the job did not finish")


def test_the_job_kind_is_registered_serialised_and_without_llm():
    from shared.llm_client.capacity import KIND_LINES

    kind = get_kind("tripplanner_custom_domain")
    assert kind is not None and kind.concurrency == 1 and kind.on_terminal is job.mark_terminal
    assert KIND_LINES["tripplanner_custom_domain"] is None
    assert job.ALERT_CHECK in __import__("shared.ops_checks", fromlist=["CHECKS"]).CHECKS


def test_from_added_to_active(knobs):
    acm, cf, pool = FakeAcm(), FakeCf(), FakePool(_row())
    ctx = _ctx(pool)
    with pytest.raises(Reschedule) as r:
        _run(ctx, acm, cf)
    assert pool.row["status"] == "validating" and pool.row["cname_target"] == TARGET
    assert pool.row["validation_name"] == f"_v.{Q}." and r.value.delay_seconds >= 120
    host_cert = pool.row["certificate_arn"]
    acm.certs[host_cert]["Status"] = "PENDING_VALIDATION"
    with pytest.raises(Reschedule):
        _run(ctx, acm, cf)                              # still waiting for the agency's record
    acm.certs[host_cert]["Status"] = "ISSUED"
    with pytest.raises(Reschedule) as r:
        _run(ctx, acm, cf)                              # issued -> attaching -> distribution updated, deploying
    _resume(ctx, r.value)
    assert pool.row["status"] == "attaching" and cf.aliases == [Q]
    cf.status = "Deployed"
    out = _run(ctx, acm, cf)
    assert out == {"domain_id": DID, "status": "active"} and pool.row["status"] == "active"
    assert len(cf.updates) == 1


def test_validation_timeout_fails_and_deletes_the_host_certificate(knobs):
    acm, cf = FakeAcm(), FakeCf()
    acm.add(HOST_CERT, Q, "PENDING_VALIDATION")
    pool = FakePool(_row("validating", certificate_arn=HOST_CERT,
                         requested_at=datetime.now(timezone.utc) - timedelta(hours=73)))
    out = _run(_ctx(pool), acm, cf)
    assert out["failure_code"] == "validation_timeout" and pool.row["status"] == "failed"
    assert HOST_CERT in acm.deleted and pool.row["certificate_arn"] is None


def test_a_retry_after_a_validation_timeout_gets_a_full_window(knobs):
    """The certificate was deleted at the timeout; requested_at still holds the original add (80 h ago). A retry
    requests a new certificate, which restarts the window: the job keeps waiting instead of failing at once."""
    acm, cf = FakeAcm(), FakeCf()
    old = datetime.now(timezone.utc) - timedelta(hours=80)
    pool = FakePool(_row("pending", requested_at=old, failure_code=None))
    ctx = _ctx(pool)
    with pytest.raises(Reschedule):
        _run(ctx, acm, cf)
    assert pool.row["status"] == "validating" and pool.row["requested_at"] > old
    acm.certs[pool.row["certificate_arn"]]["Status"] = "PENDING_VALIDATION"
    for _ in range(3):
        with pytest.raises(Reschedule) as r:
            _run(ctx, acm, cf)
        assert r.value.reason == "validation" and pool.row["status"] == "validating"
    pool.row["requested_at"] = datetime.now(timezone.utc) - timedelta(hours=73)
    assert _run(ctx, acm, cf)["failure_code"] == "validation_timeout"


def test_a_retry_reuses_a_certificate_acm_still_validates_and_replaces_a_dead_one(knobs):
    acm, cf = FakeAcm(), FakeCf()
    acm.add(HOST_CERT, Q, "PENDING_VALIDATION", DomainValidationOptions=[
        {"DomainName": Q, "ResourceRecord": {"Name": "_r.", "Type": "CNAME", "Value": "_s."}}])
    pool = FakePool(_row("pending", certificate_arn=HOST_CERT))
    with pytest.raises(Reschedule):
        _run(_ctx(pool), acm, cf)
    assert pool.row["certificate_arn"] == HOST_CERT and acm.requests == [] and pool.row["validation_name"] == "_r."
    acm.certs[HOST_CERT]["Status"] = "VALIDATION_TIMED_OUT"
    pool = FakePool(_row("pending", certificate_arn=HOST_CERT))
    with pytest.raises(Reschedule):
        _run(_ctx(pool), acm, cf)
    assert HOST_CERT in acm.deleted and pool.row["certificate_arn"] not in (None, HOST_CERT)


def test_a_failed_certificate_fails_the_host(knobs):
    acm, cf = FakeAcm(), FakeCf()
    acm.add(HOST_CERT, Q, "FAILED", FailureReason="CAA_ERROR")
    pool = FakePool(_row("validating", certificate_arn=HOST_CERT))
    out = _run(_ctx(pool), acm, cf)
    assert out["failure_code"] == "certificate_failed" and "CAA_ERROR" in pool.row["failure_message"]


def test_removal_detaches_deletes_the_certificate_once_and_the_row(knobs):
    acm, cf = FakeAcm(), FakeCf()
    edge = _attached(acm, cf, [Q])
    acm.add(HOST_CERT, Q)
    acm.in_use.add(edge)                                 # CloudFront still deploys the swap
    pool = FakePool(_row("removing", certificate_arn=HOST_CERT))
    ctx = _ctx(pool)
    with pytest.raises(Reschedule) as r:
        _run(ctx, acm, cf)
    _resume(ctx, r.value)
    assert cf.aliases == [] and cf.cert is None and r.value.reason == "edge_busy"
    assert acm.deleted == [HOST_CERT] and pool.row["certificate_arn"] is None   # cleared at once
    acm.in_use.clear()
    out = _run(ctx, acm, cf)
    assert out == {"domain_id": DID, "status": "removed"} and pool.row is None and edge in acm.deleted
    assert acm.delete_calls.count(HOST_CERT) == 1      # the re-run never deletes the host certificate again


def test_removal_keeps_the_other_hosts(knobs):
    acm, cf = FakeAcm(), FakeCf()
    both = _attached(acm, cf, [Q, L])
    pool = FakePool(_row("removing"), _other("active"))
    assert _until_done(_ctx(pool), acm, cf)["status"] == "removed"
    assert cf.aliases == [L] and both in acm.deleted and cf.cert not in (both, None)


def test_removing_a_host_that_was_never_served_skips_the_edge(knobs):
    """Another agency's record is missing (a new edge certificate would stay pending): the removal of a host that is
    not on the distribution does not wait for it."""
    acm, cf = FakeAcm(), FakeCf()
    _attached(acm, cf, [L])
    acm.missing = {L}
    acm.add(HOST_CERT, Q, "PENDING_VALIDATION")
    pool = FakePool(_row("removing", certificate_arn=HOST_CERT, validated_at=None), _other("active"))
    assert _run(_ctx(pool), acm, cf)["status"] == "removed"
    assert len(acm.requests) == 1 and len(cf.updates) == 1 and HOST_CERT in acm.deleted


def test_a_removal_with_the_edge_switched_off(monkeypatch, knobs):
    monkeypatch.delenv(tp_edge.DIST_ENV)
    acm = FakeAcm()
    acm.add(HOST_CERT, Q, "PENDING_VALIDATION")
    never = FakePool(_row("removing", certificate_arn=HOST_CERT, validated_at=None))
    assert _run(_ctx(never), acm, FakeCf())["status"] == "removed" and HOST_CERT in acm.deleted
    served = FakePool(_row("removing", validated_at=datetime.now(timezone.utc)))
    out = _run(_ctx(served), FakeAcm(), FakeCf())
    assert out == {"domain_id": DID, "status": "removing", "failure_code": "edge_not_configured"}
    assert served.row["status"] == "removing" and served.row["failure_code"] == "edge_not_configured"


def test_a_refused_removal_stays_removing_with_its_reason(knobs):
    acm, cf = FakeAcm(), FakeCf()
    _attached(acm, cf, [Q])
    cf.fail_with = "InvalidArgument"
    pool = FakePool(_row("removing"))
    out = _run(_ctx(pool), acm, cf)
    assert out["failure_code"] == "edge_update_refused" and pool.row["status"] == "removing"
    cf.fail_with = None                                 # staff remove it again: a fresh job finishes it
    assert _until_done(_ctx(pool), acm, cf)["status"] == "removed" and pool.row is None


def test_one_agencys_missing_record_is_detached_without_blocking_another(knobs):
    acm, cf = FakeAcm(), FakeCf()
    _attached(acm, cf, [L])
    acm.missing = {L}                                   # the agency of L deleted its validation record
    pool = FakePool(_row("attaching"), _other("active"))
    ctx = _ctx(pool)
    with pytest.raises(Reschedule) as r:
        _run(ctx, acm, cf)
    _resume(ctx, r.value)
    assert r.value.progress["pending_names"] == [L] and pool.by_domain(L)["status"] == "active"
    pending = ctx.job.progress["pending_edge_cert"]
    acm.certs[pending]["CreatedAt"] = datetime.now(timezone.utc) - timedelta(hours=7)
    out = _until_done(ctx, acm, cf)
    assert out == {"domain_id": DID, "status": "active"} and cf.aliases == [Q]
    gone = pool.by_domain(L)
    assert gone["status"] == "failed" and gone["failure_code"] == "validation_record_gone"
    assert pending in acm.deleted and knobs.alert.await_count == 1
    assert knobs.alert.await_args.args[1].details["hosts"] == [L]


def test_the_host_being_attached_fails_itself_when_its_own_record_is_gone(knobs):
    acm, cf = FakeAcm(), FakeCf()
    acm.missing = {Q}
    acm.created_at = datetime.now(timezone.utc) - timedelta(hours=7)
    pool = FakePool(_row("attaching"))
    out = _run(_ctx(pool), acm, cf)
    assert out == {"domain_id": DID, "status": "failed"} and pool.row["failure_code"] == "validation_record_gone"
    assert cf.aliases == []


def test_not_configured_fails_the_host(monkeypatch, knobs):
    monkeypatch.delenv(tp_edge.DIST_ENV)
    pool = FakePool(_row())
    out = _run(_ctx(pool), FakeAcm(), FakeCf())
    assert out["failure_code"] == "edge_not_configured" and pool.row["status"] == "failed"


def test_alias_in_use_fails_the_host(knobs):
    acm, cf = FakeAcm(), FakeCf()
    cf.fail_with = "CNAMEAlreadyExists"
    acm.add(HOST_CERT, Q)
    pool = FakePool(_row("validating", certificate_arn=HOST_CERT))
    out = _run(_ctx(pool), acm, cf)
    assert out["failure_code"] == "alias_in_use" and pool.row["status"] == "failed"


def test_a_busy_distribution_reschedules(knobs):
    acm, cf = FakeAcm(), FakeCf()
    cf.fail_with = "PreconditionFailed"
    pool = FakePool(_row("attaching"))
    with pytest.raises(Reschedule) as r:
        _run(_ctx(pool), acm, cf)
    assert r.value.reason == "edge_busy" and pool.row["status"] == "attaching"


def test_an_active_host_waits_until_a_replaced_certificate_is_deleted(knobs):
    acm, cf = FakeAcm(), FakeCf()
    _attached(acm, cf, [Q, L])
    pool = FakePool(_row("active"))                     # L was removed by an erasure: its alias is an orphan
    out = _until_done(_ctx(pool), acm, cf)
    assert out == {"domain_id": DID, "status": "active"} and cf.aliases == [Q] and len(acm.deleted) == 1


def test_failed_and_gone_rows_are_left_alone(knobs):
    assert _run(_ctx(FakePool(_row("failed"))), FakeAcm(), FakeCf())["skipped"] is True
    assert _run(_ctx(FakePool(None)), FakeAcm(), FakeCf())["status"] == "gone"


def test_a_bad_payload_is_not_retried(knobs):
    ctx = _ctx(FakePool(_row()))
    ctx.payload = {}
    with pytest.raises(NonRetryable):
        _run(ctx, FakeAcm(), FakeCf())


def test_waits_stay_within_the_reschedule_budget(knobs):
    from shared.jobs.registry import RESCHEDULE_MAX_COUNT, RESCHEDULE_MAX_DELAY_SECONDS

    for hours, delay in ((72, asyncio.run(job._validation_delay_s())), (6, asyncio.run(job._edge_pending_delay_s()))):
        assert delay <= RESCHEDULE_MAX_DELAY_SECONDS and hours * 3600 / delay <= RESCHEDULE_MAX_COUNT
    knobs.values[job.EDGE_PENDING_KEY] = 24
    assert 24 * 3600 / asyncio.run(job._edge_pending_delay_s()) <= RESCHEDULE_MAX_COUNT


def test_mark_terminal_fails_a_waiting_host_and_marks_a_stuck_removal():
    pool = FakePool(_row("validating"), _other("removing"))
    asyncio.run(job.mark_terminal(pool, {"id": "j1", "payload": {"domain_id": DID}, "error": "boom",
                                         "status": "failed"}))
    asyncio.run(job.mark_terminal(pool, {"id": "j2", "payload": json.dumps({"domain_id": OTHER}), "error": "x"}))
    assert pool.row["status"] == "failed" and pool.row["failure_code"] == "job_failed"
    assert pool.rows[OTHER]["status"] == "removing" and pool.rows[OTHER]["failure_code"] == "job_failed"
    calls = len(pool.calls)
    asyncio.run(job.mark_terminal(pool, {"id": "j3", "payload": {"check": "edge_health"}}))
    assert len(pool.calls) == calls                     # the daily check has no host


# ── the daily edge check ────────────────────────────────────────────────────────────────────────────────────────

def _health_ctx(pool):
    return _ctx(pool, payload={"check": job.HEALTH_CHECK})


def _blocked_renewal(acm, edge, host, days_left):
    acm.certs[edge]["NotAfter"] = datetime.now(timezone.utc) + timedelta(days=days_left)
    acm.certs[edge]["RenewalSummary"] = {"RenewalStatus": "PENDING_VALIDATION", "DomainValidationOptions": [
        {"DomainName": host, "ValidationStatus": "PENDING_VALIDATION"}]}


def test_health_on_a_healthy_edge_and_without_hosts(knobs):
    acm, cf = FakeAcm(), FakeCf()
    pool = FakePool(None)
    assert _run(_health_ctx(pool), acm, cf)["cert"] is False
    _attached(acm, cf, [Q])
    out = _run(_health_ctx(FakePool(_row("active"))), acm, cf)
    assert out["status"] == "ISSUED" and out["blocking"] == [] and not knobs.alert.await_count


def test_health_alerts_first_and_detaches_within_the_grace_days(knobs):
    acm, cf = FakeAcm(), FakeCf()
    edge = _attached(acm, cf, [Q, L])
    pool = FakePool(_row("active"), _other("active"))
    _blocked_renewal(acm, edge, L, days_left=50)
    out = _run(_health_ctx(pool), acm, cf)
    assert out["blocking"] == [L] and out["detached"] == [] and knobs.alert.await_count == 1
    assert pool.by_domain(L)["status"] == "active" and cf.aliases == sorted([L, Q])
    _blocked_renewal(acm, edge, L, days_left=29)
    acm.missing = {L}
    out = _until_done(_health_ctx(pool), acm, cf)
    assert cf.aliases == [Q] and pool.by_domain(L)["failure_code"] == "validation_record_gone"
    assert pool.row["status"] == "active" and edge in acm.deleted and out["edge"] in ("in_sync", "updated")


def test_health_is_skipped_while_the_edge_is_off(monkeypatch, knobs):
    monkeypatch.delenv(tp_edge.DIST_ENV)
    assert _run(_health_ctx(FakePool(None)), FakeAcm(), FakeCf())["skipped"] == "edge_not_configured"


def test_the_daily_check_is_queued_once_a_day_and_only_when_switched_on(monkeypatch, env):
    pool = MagicMock()
    pool.fetchval = AsyncMock(return_value=False)
    enq = AsyncMock(return_value=("job-h", True))
    monkeypatch.setattr(job, "enqueue", enq)
    job_id, created, key = asyncio.run(job.enqueue_daily_health(pool, now=NOW))
    assert (job_id, created, key) == ("job-h", True, "tripplanner_custom_domain:edge_health:2026-11-15")
    assert enq.await_args.args[2] == {"check": "edge_health"} and enq.await_args.kwargs["idempotency_key"] == key
    pool.fetchval = AsyncMock(return_value=True)
    assert asyncio.run(job.enqueue_daily_health(pool, now=NOW))[1] is False
    monkeypatch.delenv(tp_edge.DIST_ENV)
    assert asyncio.run(job.enqueue_daily_health(pool, now=NOW)) == (None, False, None)


def test_the_sampler_queues_the_daily_check(monkeypatch, env):
    from api.core import ops_metrics_sampler as s

    monkeypatch.setattr(job, "enqueue_daily_health", AsyncMock(return_value=("job-h", True, "k")))
    out: dict = {}
    asyncio.run(s._tripplanner_domain_health_part(object(), {job.KIND: 1}, NOW, out))
    assert out["tripplanner_custom_domain_health"] == {"job_id": "job-h", "key": "k"}
    out = {}
    asyncio.run(s._tripplanner_domain_health_part(object(), {}, NOW, out))
    assert out == {}
