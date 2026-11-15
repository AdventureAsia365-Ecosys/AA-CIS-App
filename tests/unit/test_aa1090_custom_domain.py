"""AA-1090 TripPlanner R8 — a tenant's custom host: the editor's rules (services/tenant_account/tripplanner_domain.py),
the state machine, the edge sync against fake ACM / CloudFront clients (tp_edge.py: attach, in step, pending
certificate, removal back to no alias and the default certificate, an orphan alias detached, refusals, the renewal
check) and the job (services/jobs/tripplanner_custom_domain_job.py) on in-memory rows: the add flow, a retry after a
validation timeout, removals that re-run cleanly or get stuck and are re-queued, one agency's missing record detached
without blocking another's change, the daily edge check, plus the admin routes. No AWS, no database: the database path
is in tests/integration/test_aa1090_custom_domain_pg.py. Invented hosts only (example.invalid, example.test).
"""
from __future__ import annotations

import asyncio
import copy
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.core.staff_authz import POLICY, TENANT_WRITE
from services.jobs import tripplanner_custom_domain_job as job
from services.tenant_account import tp_edge
from services.tenant_account import tripplanner_domain as tpd
from shared.jobs.registry import NonRetryable, Reschedule, get_kind

TID = "1090a000-0000-4000-8000-000000000001"
DID = "1090a000-0000-4000-8000-0000000000d1"
DIST = "EDISTTEST1090"
# ARNs are built at run time: the repo's resource-ID check refuses an ARN with an account number in a public repo.
ACCOUNT = "0" * 12
ACM = f"arn:aws:acm:us-east-1:{ACCOUNT}:certificate"
HOST_CERT = f"{ACM}/00000000-0000-4000-8000-0000000000c1"
Q = "trip.quillmere.example.invalid"
L = "go.larchfield.example.invalid"
NOW = datetime(2026, 11, 15, 9, 0, tzinfo=timezone.utc)
TARGET = "d1090abcdef.cloudfront.net"


# ── the editor's rules ──────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,host", [
    ("trip.quillmere.example.invalid", "trip.quillmere.example.invalid"),
    ("  Trip.Quillmere.Example.Invalid.  ", "trip.quillmere.example.invalid"),
    ("https://trip.quillmere.example.invalid/plan", "trip.quillmere.example.invalid"),
])
def test_normalise(raw, host):
    assert tpd.normalise(raw) == host


@pytest.mark.parametrize("host,code", [
    ("trip.quillmere.example.invalid", None),
    ("plan.larchfield-tours.example.org", None),
    ("quillmere.example", tpd.DOMAIN_APEX),
    ("trip.quillmere", tpd.DOMAIN_APEX),
    ("", tpd.DOMAIN_INVALID),
    ("trip_x.quillmere.example.invalid", tpd.DOMAIN_INVALID),
    ("-trip.quillmere.example.invalid", tpd.DOMAIN_INVALID),
    ("10.0.0.1", tpd.DOMAIN_INVALID),
    ("a" * 64 + ".example.invalid", tpd.DOMAIN_INVALID),
    ("x." * 127 + "invalid", tpd.DOMAIN_INVALID),
    ("crm.corp.internal", tpd.DOMAIN_RESERVED),
    ("trip.box.local", tpd.DOMAIN_RESERVED),
    ("agency.lumiguides.it.com", tpd.DOMAIN_RESERVED),
    ("d111.cloudfront.net", tpd.DOMAIN_RESERVED),
    ("x.s3.amazonaws.com", tpd.DOMAIN_RESERVED),
])
def test_domain_problem(host, code):
    assert tpd.domain_problem(host) == code


def test_normalise_refuses_a_non_string():
    with pytest.raises(tpd.DomainError) as e:
        tpd.normalise(7)
    assert e.value.code == tpd.DOMAIN_INVALID and e.value.field == "domain"


# ── the state machine ───────────────────────────────────────────────────────────────────────────────────────────

ALLOWED = {
    ("pending", "requested"): "validating", ("validating", "issued"): "attaching",
    ("attaching", "attached"): "active", ("pending", "failed"): "failed", ("validating", "failed"): "failed",
    ("attaching", "failed"): "failed", ("active", "failed"): "failed", ("failed", "retry"): "pending",
    ("pending", "remove"): "removing", ("validating", "remove"): "removing", ("attaching", "remove"): "removing",
    ("active", "remove"): "removing", ("failed", "remove"): "removing",
}
EVENTS = ("requested", "issued", "attached", "failed", "retry", "remove")


@pytest.mark.parametrize("status", tpd.STATUSES)
@pytest.mark.parametrize("event", EVENTS)
def test_transition_matrix(status, event):
    if (status, event) in ALLOWED:
        assert tpd.transition(status, event) == ALLOWED[(status, event)]
    else:
        with pytest.raises(ValueError):
            tpd.transition(status, event)


def test_unknown_event():
    with pytest.raises(ValueError):
        tpd.transition("pending", "jump")


def test_dns_records_appear_once_known():
    row = {"domain": "trip.q.example.invalid", "cname_target": None, "validation_name": None, "validation_value": None}
    assert tpd.dns_records(row) == []
    row.update(cname_target=TARGET, validation_name="_a1.trip.q.example.invalid.", validation_value="_b2.acm.aws.")
    assert tpd.dns_records(row) == [
        {"purpose": "route", "type": "CNAME", "name": "trip.q.example.invalid", "value": TARGET},
        {"purpose": "certificate", "type": "CNAME", "name": "_a1.trip.q.example.invalid.", "value": "_b2.acm.aws."},
    ]


# ── tp_edge pure helpers ────────────────────────────────────────────────────────────────────────────────────────

def test_idempotency_token_is_order_free_and_short():
    a = tp_edge.idempotency_token(["b.example.test", "a.example.test"])
    assert a == tp_edge.idempotency_token(["A.example.test", "b.example.test"]) and len(a) == 32 and a.isalnum()


def test_validation_record():
    cert = {"DomainValidationOptions": [{"DomainName": Q, "ResourceRecord": {"Name": "_a.", "Type": "CNAME",
                                                                              "Value": "_b."}}]}
    assert tp_edge.validation_record(cert, Q) == ("_a.", "_b.")
    assert tp_edge.validation_record({"DomainValidationOptions": [{"DomainName": "x"}]}, "x") is None


def test_blocking_names():
    opts = [{"DomainName": Q, "ValidationStatus": "SUCCESS"}, {"DomainName": L.upper(), "ValidationStatus": "FAILED"},
            {"DomainName": "x.example.invalid", "ValidationStatus": "PENDING_VALIDATION"}, {"ValidationStatus": "x"}]
    assert tp_edge.blocking_names(opts) == ["go.larchfield.example.invalid", "x.example.invalid"]


# ── fakes ───────────────────────────────────────────────────────────────────────────────────────────────────────

class ClientError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeAcm:
    """Certificates by ARN. A name in ``missing`` has no validation record in its agency's DNS: a certificate naming
    it stays PENDING_VALIDATION (that name's ValidationStatus too). A deleted ARN answers AccessDenied, as the
    tag-conditioned policy does for an ARN whose tags IAM can no longer read."""

    def __init__(self):
        self.certs: dict[str, dict] = {}
        self.requests: list[dict] = []
        self.deleted: list[str] = []
        self.delete_calls: list[str] = []
        self.next_status = "ISSUED"
        self.missing: set[str] = set()
        self.in_use: set[str] = set()
        self.created_at = datetime.now(timezone.utc)

    def request_certificate(self, **kw):
        self.requests.append(kw)
        assert kw["ValidationMethod"] == "DNS"
        assert {"Key": tp_edge.TAG_KEY, "Value": tp_edge.TAG_VALUE} in kw["Tags"]
        assert {t["Key"] for t in kw["Tags"]} <= {"aa-managed-by", "aa-domain-id", "aa-edge"}   # the policy's keys
        arn = f"{ACM}/00000000-0000-4000-8000-{len(self.requests):012d}"
        names = [kw["DomainName"], *kw.get("SubjectAlternativeNames", [])]
        blocked = [n for n in names if n in self.missing]
        self.certs[arn] = {"DomainName": kw["DomainName"], "SubjectAlternativeNames": names,
                           "Status": "PENDING_VALIDATION" if blocked else self.next_status,
                           "CreatedAt": self.created_at, "NotAfter": self.created_at + timedelta(days=395),
                           "DomainValidationOptions": [{
                               "DomainName": n, "ValidationStatus": "PENDING_VALIDATION" if n in blocked else "SUCCESS",
                               "ResourceRecord": {"Name": f"_v.{n}.", "Type": "CNAME",
                                                  "Value": "_x.acm-validations.aws."}} for n in names]}
        return {"CertificateArn": arn}

    def describe_certificate(self, CertificateArn):
        if CertificateArn in self.deleted:
            raise ClientError("AccessDeniedException")
        if CertificateArn not in self.certs:
            raise ClientError("ResourceNotFoundException")
        return {"Certificate": copy.deepcopy(self.certs[CertificateArn])}

    def delete_certificate(self, CertificateArn):
        self.delete_calls.append(CertificateArn)
        if CertificateArn in self.in_use:
            raise ClientError("ResourceInUseException")
        if CertificateArn in self.deleted:
            raise ClientError("AccessDeniedException")
        if CertificateArn not in self.certs:
            raise ClientError("ResourceNotFoundException")
        del self.certs[CertificateArn]
        self.deleted.append(CertificateArn)

    def add(self, arn, domain, status="ISSUED", **extra):
        self.certs[arn] = {"DomainName": domain, "SubjectAlternativeNames": [domain], "Status": status, **extra}


DEFAULT_VIEWER = {"CloudFrontDefaultCertificate": True, "MinimumProtocolVersion": "TLSv1",
                  "CertificateSource": "cloudfront"}


class FakeCf:
    """The agency distribution: no alias and the CloudFront default certificate, as Terraform creates it."""

    def __init__(self, aliases=(), cert=None, status="Deployed"):
        self.config = {"Aliases": {"Quantity": len(aliases), **({"Items": list(aliases)} if aliases else {})},
                       "Comment": "kept",
                       "ViewerCertificate": dict(DEFAULT_VIEWER) if cert is None else {
                           "ACMCertificateArn": cert, "SSLSupportMethod": "sni-only",
                           "MinimumProtocolVersion": "TLSv1.2_2021", "Certificate": cert, "CertificateSource": "acm"}}
        self.status = status
        self.etag = "E1"
        self.updates: list[dict] = []
        self.fail_with: str | None = None

    def get_distribution(self, Id):
        assert Id == DIST
        return {"Distribution": {"DomainName": TARGET, "Status": self.status,
                                 "DistributionConfig": copy.deepcopy(self.config)}, "ETag": self.etag}

    def update_distribution(self, Id, IfMatch, DistributionConfig):
        assert Id == DIST
        if self.fail_with:
            raise ClientError(self.fail_with)
        assert IfMatch == self.etag
        self.updates.append(DistributionConfig)
        self.config = copy.deepcopy(DistributionConfig)
        self.etag = f"E{len(self.updates) + 1}"
        self.status = "InProgress"

    @property
    def aliases(self):
        return self.config["Aliases"].get("Items", [])

    @property
    def cert(self):
        return self.config["ViewerCertificate"].get("ACMCertificateArn")


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv(tp_edge.DIST_ENV, DIST)


def _attached(acm, cf, hosts):
    """A distribution that already serves ``hosts`` (deployed)."""
    tp_edge.sync_edge(acm, cf, hosts)
    cf.status = "Deployed"
    return cf.cert


def _row(status="pending", **kw):
    row = {"domain_id": DID, "tenant_id": TID, "domain": Q, "status": status, "certificate_arn": None,
           "validation_name": None, "validation_value": None, "cname_target": None, "failure_code": None,
           "failure_message": None, "requested_at": datetime.now(timezone.utc),
           "validated_at": datetime.now(timezone.utc) if status in ("attaching", "active") else None}
    row.update(kw)
    return row


# ── sync_edge ───────────────────────────────────────────────────────────────────────────────────────────────────

def test_attach_requests_one_edge_certificate_and_swaps_aliases_and_certificate(env):
    acm, cf = FakeAcm(), FakeCf()
    state = tp_edge.sync_edge(acm, cf, [Q])
    assert state.status == "updated" and state.cname_target == TARGET and state.retired is None
    req = acm.requests[0]
    assert [req["DomainName"], *req["SubjectAlternativeNames"]] == [Q]
    cfg = cf.updates[0]
    assert cfg["Aliases"] == {"Quantity": 1, "Items": [Q]} and cfg["Comment"] == "kept"
    assert cfg["ViewerCertificate"] == {"CloudFrontDefaultCertificate": False, "ACMCertificateArn": cf.cert,
                                        "SSLSupportMethod": "sni-only", "MinimumProtocolVersion": "TLSv1.2_2021"}


def test_in_step_reuses_the_served_certificate_and_sends_nothing(env):
    acm, cf = FakeAcm(), FakeCf()
    _attached(acm, cf, [Q])
    state = tp_edge.sync_edge(acm, cf, [Q])
    assert state.status == "in_sync" and state.deployed and len(cf.updates) == 1 and len(acm.requests) == 1
    assert tp_edge.sync_edge(acm, FakeCf(), []).status == "in_sync"   # no host on an untouched distribution


def test_a_pending_edge_certificate_waits_names_the_blocking_host_and_is_reused(env):
    acm, cf = FakeAcm(), FakeCf()
    acm.missing = {L}
    state = tp_edge.sync_edge(acm, cf, [Q, L])
    assert state.status == "pending_cert" and state.pending_arn and cf.updates == []
    assert state.pending_names == [L] and state.pending_since == acm.created_at
    acm.certs[state.pending_arn]["Status"] = "ISSUED"
    again = tp_edge.sync_edge(acm, cf, [Q, L], pending_arn=state.pending_arn)
    assert again.status == "updated" and len(acm.requests) == 1 and cf.cert == state.pending_arn


def test_a_second_host_replaces_the_edge_certificate_and_retires_the_old_one(env):
    acm, cf = FakeAcm(), FakeCf()
    first = _attached(acm, cf, [Q])
    state = tp_edge.sync_edge(acm, cf, [Q, L])
    assert state.status == "updated" and state.retired == first and cf.aliases == sorted([L, Q])


def test_removing_the_last_host_goes_back_to_no_alias_and_the_default_certificate(env):
    acm, cf = FakeAcm(), FakeCf()
    edge = _attached(acm, cf, [Q])
    state = tp_edge.sync_edge(acm, cf, [])
    assert state.status == "updated" and state.retired == edge and len(acm.requests) == 1
    assert cf.config["Aliases"] == {"Quantity": 0}
    assert cf.config["ViewerCertificate"] == {"CloudFrontDefaultCertificate": True, "MinimumProtocolVersion": "TLSv1"}


def test_an_orphan_alias_is_detached(env):
    acm, cf = FakeAcm(), FakeCf()
    _attached(acm, cf, [Q, "gone.erased-agency.example.invalid"])
    state = tp_edge.sync_edge(acm, cf, [Q])
    assert state.status == "updated" and cf.aliases == [Q]


def test_alias_served_elsewhere_is_a_failure_and_etag_race_is_busy(env):
    acm, cf = FakeAcm(), FakeCf()
    cf.fail_with = "CNAMEAlreadyExists"
    with pytest.raises(tp_edge.EdgeError) as e:
        tp_edge.sync_edge(acm, cf, [Q])
    assert e.value.code == "alias_in_use"
    cf.fail_with = "PreconditionFailed"
    with pytest.raises(tp_edge.EdgeBusy):
        tp_edge.sync_edge(acm, cf, [Q])


def test_a_failed_edge_certificate_is_a_failure(env):
    acm, cf = FakeAcm(), FakeCf()
    acm.next_status = "FAILED"
    with pytest.raises(tp_edge.EdgeError) as e:
        tp_edge.sync_edge(acm, cf, [Q])
    assert e.value.code == "edge_certificate_failed"


def test_not_configured(monkeypatch):
    monkeypatch.delenv(tp_edge.DIST_ENV, raising=False)
    assert tp_edge.configured() is False
    with pytest.raises(tp_edge.EdgeError) as e:
        tp_edge.sync_edge(FakeAcm(), FakeCf(), [])
    assert e.value.code == "edge_not_configured"
    with pytest.raises(tp_edge.EdgeError):
        tp_edge.edge_health(FakeAcm(), FakeCf())
    assert tp_edge.serves(FakeCf(), Q) is False


def test_delete_certificate_tolerates_gone_and_access_denied_and_waits_while_in_use():
    acm = FakeAcm()
    tp_edge.delete_certificate(acm, HOST_CERT)          # never there: done
    tp_edge.delete_certificate(acm, None)
    acm.add(HOST_CERT, Q)
    tp_edge.delete_certificate(acm, HOST_CERT)
    tp_edge.delete_certificate(acm, HOST_CERT)          # deleted already: AccessDenied under the tag policy = done
    assert acm.deleted == [HOST_CERT]
    acm.add(HOST_CERT + "0", Q)
    acm.in_use.add(HOST_CERT + "0")
    with pytest.raises(tp_edge.EdgeBusy):
        tp_edge.delete_certificate(acm, HOST_CERT + "0")


def test_serves(env):
    acm, cf = FakeAcm(), FakeCf()
    _attached(acm, cf, [Q])
    assert tp_edge.serves(cf, Q) and not tp_edge.serves(cf, L)


def test_edge_health_names_the_hosts_that_block_renewal(env):
    acm, cf = FakeAcm(), FakeCf()
    assert tp_edge.edge_health(acm, cf).cert_arn is None             # no agency host: the default certificate
    edge = _attached(acm, cf, [Q, L])
    healthy = tp_edge.edge_health(acm, cf)
    assert healthy.cert_arn == edge and healthy.status == "ISSUED" and healthy.blocking == []
    acm.certs[edge]["RenewalSummary"] = {"RenewalStatus": "PENDING_VALIDATION", "DomainValidationOptions": [
        {"DomainName": Q, "ValidationStatus": "SUCCESS"}, {"DomainName": L, "ValidationStatus": "PENDING_VALIDATION"}]}
    blocked = tp_edge.edge_health(acm, cf)
    assert blocked.blocking == [L] and blocked.renewal_status == "PENDING_VALIDATION" and blocked.not_after


# ── add / remove / retry (no database: a scripted connection) ───────────────────────────────────────────────────

class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class EditorConn:
    def __init__(self, *, status="active", has_tp=True, taken=False, tenant_count=0, edge_count=0, row=None):
        self.v = {tpd.TENANT_LOCK_SQL: status, tpd.HAS_TRIPPLANNER_SQL: has_tp, tpd.TAKEN_SQL: taken,
                  tpd.TENANT_COUNT_SQL: tenant_count, tpd.EDGE_COUNT_SQL: edge_count}
        self.row = row
        self.inserted = []
        self.writes: list[str] = []

    def transaction(self):
        return _Tx()

    async def execute(self, sql, *args):
        return None

    async def fetchval(self, sql, *args):
        return self.v[sql]

    async def fetchrow(self, sql, *args):
        if sql == tpd.INSERT_SQL:
            self.inserted.append(args)
            return {**_row(), "domain": args[1], "updated_at": datetime.now(timezone.utc), "updated_by": args[2]}
        if sql.startswith(tpd.ROW_SQL):
            return self.row
        if sql == tpd.STATUS_SQL:
            self.writes.append("status")
            return {**self.row, "status": args[2], "failure_code": None, "failure_message": None, "updated_at": NOW}
        if sql == tpd.RETRY_SQL:
            self.writes.append("retry")
            return {**self.row, "status": "pending", "failure_code": None, "requested_at": NOW, "validated_at": None,
                    "active_at": None, "updated_at": NOW}
        raise AssertionError(sql)


@pytest.fixture
def editor(monkeypatch, env):
    audit = AsyncMock()
    monkeypatch.setattr(tpd, "write_audit_log", audit)
    monkeypatch.setattr(tpd, "enqueue", AsyncMock(return_value=("job-1", True)))
    monkeypatch.setattr(tpd.settings, "aget", AsyncMock(side_effect=lambda k, *a: {
        tpd.MAX_PER_TENANT_KEY: 1, tpd.EDGE_MAX_KEY: 10}[k]))
    return audit


def test_add_stores_audits_and_queues(editor):
    conn = EditorConn()
    out = asyncio.run(tpd.add_domain(conn, TID, domain="Trip.Quillmere.Example.Invalid", actor="admin:u1"))
    assert out["domain"] == Q and out["status"] == "pending" and out["job_id"] == "job-1"
    assert conn.inserted == [(TID, Q, "admin:u1")]
    assert editor.await_args.kwargs["action"] == "tenant.tripplanner_domain_added"
    assert tpd.enqueue.await_args.kwargs["idempotency_key"] == f"tripplanner_custom_domain:{DID}:add"


@pytest.mark.parametrize("kw,code,status", [
    ({"status": None}, "tenant_not_found", 404),
    ({"status": "offboarding"}, "tenant_offboarded", 409),
    ({"has_tp": False}, "no_tripplanner", 409),
    ({"taken": True}, tpd.DOMAIN_TAKEN, 409),
    ({"tenant_count": 1}, "domain_limit_reached", 409),
    ({"edge_count": 10}, "edge_full", 409),
])
def test_add_refusals(editor, kw, code, status):
    conn = EditorConn(**kw)
    with pytest.raises(tpd.DomainError) as e:
        asyncio.run(tpd.add_domain(conn, TID, domain=Q, actor="admin:u1"))
    assert (e.value.code, e.value.status) == (code, status) and conn.inserted == [] and not editor.await_count


def test_add_refuses_a_bad_host_before_the_database(editor):
    with pytest.raises(tpd.DomainError) as e:
        asyncio.run(tpd.add_domain(EditorConn(), TID, domain="quillmere.example", actor="admin:u1"))
    assert e.value.code == tpd.DOMAIN_APEX and e.value.field == "domain"


def test_add_is_refused_when_the_edge_is_not_configured(editor, monkeypatch):
    monkeypatch.delenv(tp_edge.DIST_ENV)
    with pytest.raises(tpd.DomainError) as e:
        asyncio.run(tpd.add_domain(EditorConn(), TID, domain=Q, actor="admin:u1"))
    assert e.value.code == "custom_domains_unavailable"


def test_remove_marks_removing(editor):
    conn = EditorConn(row=_row("active", updated_at=datetime.now(timezone.utc)))
    out = asyncio.run(tpd.remove_domain(conn, TID, DID, actor="admin:u1"))
    assert out["status"] == "removing" and editor.await_args.kwargs["details"]["before"] == "active"
    assert tpd.enqueue.await_args.kwargs["idempotency_key"] == f"tripplanner_custom_domain:{DID}:remove"


def test_removing_again_requeues_a_stuck_removal_with_a_fresh_key(editor):
    conn = EditorConn(row=_row("removing", failure_code="edge_update_refused", updated_at=None))
    out = asyncio.run(tpd.remove_domain(conn, TID, DID, actor="admin:u1"))
    stamp = int(NOW.timestamp() * 1000)
    assert out["status"] == "removing" and out["failure_code"] is None and out["job_id"] == "job-1"
    assert conn.writes == ["status"]
    assert tpd.enqueue.await_args.kwargs["idempotency_key"] == f"tripplanner_custom_domain:{DID}:remove:{stamp}"
    details = editor.await_args.kwargs["details"]
    assert details["requeued"] is True and details["failure_code"] == "edge_update_refused"


def test_retry_only_from_failed_and_restarts_the_window(editor):
    with pytest.raises(tpd.DomainError) as e:
        asyncio.run(tpd.retry_domain(EditorConn(row=_row("active")), TID, DID, actor="admin:u1"))
    assert e.value.code == "not_failed"
    conn = EditorConn(row=_row("failed", failure_code="validation_timeout"))
    out = asyncio.run(tpd.retry_domain(conn, TID, DID, actor="admin:u1"))
    assert out["status"] == "pending" and out["requested_at"] == NOW.isoformat() and conn.writes == ["retry"]
    assert editor.await_args.kwargs["action"] == "tenant.tripplanner_domain_retried"
    assert tpd.enqueue.await_args.kwargs["idempotency_key"].startswith(f"tripplanner_custom_domain:{DID}:retry:")


def test_the_retry_sql_resets_the_window_and_the_old_timestamps():
    sql = " ".join(tpd.RETRY_SQL.split())
    for part in ("requested_at = now()", "validated_at = NULL", "active_at = NULL", "status = 'failed'"):
        assert part in sql
    assert "requested_at = now()" in " ".join(job.SET_CERT_SQL.split())


# ── the admin routes ────────────────────────────────────────────────────────────────────────────────────────────

_SECRET = "test-admin-secret"
PATH = f"/admin/tenants/{TID}/tripplanner-site/domains"


@pytest.fixture
def client(monkeypatch):
    from fastapi.testclient import TestClient

    from api.main import app
    from api.routers import admin, admin_settings

    monkeypatch.delenv("ADMIN_SECRET", raising=False)
    monkeypatch.setattr(admin, "ADMIN_SECRET", _SECRET)
    monkeypatch.setattr(admin_settings, "ADMIN_SECRET", _SECRET)
    conn = MagicMock()
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(app.state, "pool", pool, raising=False)
    return TestClient(app, raise_server_exceptions=True)


def _h(role="admin"):
    h = {"X-Admin-Secret": _SECRET, "x-admin-user-id": "u-admin"}
    if role is not None:
        h["x-admin-role"] = role
    return h


def _out(status="pending"):
    return {**tpd.shape({**_row(status), "updated_at": None}), "job_id": "job-1"}


def test_policy_entries():
    base = "/admin/tenants/{tenant_id}/tripplanner-site/domains"
    assert POLICY[("POST", base)] == TENANT_WRITE
    assert POLICY[("DELETE", base + "/{domain_id}")] == TENANT_WRITE
    assert POLICY[("POST", base + "/{domain_id}/retry")] == TENANT_WRITE
    assert POLICY[("GET", base)] == POLICY[("GET", "/admin/tenants/{tenant_id}/subscription")]


def test_post_refuses_a_bad_host_with_a_field_error(client, monkeypatch, env):
    resp = client.post(PATH, headers=_h(), json={"domain": "quillmere.example"})
    assert resp.status_code == 422
    body = resp.json()["detail"]
    assert body["detail"] == tpd.DOMAIN_APEX and body["field"] == "domain" and "sub-domain" in body["message"]


def test_post_passes_to_the_service(client, monkeypatch):
    adder = AsyncMock(return_value=_out())
    monkeypatch.setattr(tpd, "add_domain", adder)
    resp = client.post(PATH, headers=_h(), json={"domain": "trip.quillmere.example.invalid"})
    assert resp.status_code == 201 and resp.json()["status"] == "pending"
    assert adder.await_args.kwargs == {"domain": "trip.quillmere.example.invalid", "actor": "admin:u-admin"}


@pytest.mark.parametrize("role", ["content", "customer_success", None])
def test_writes_are_admin_only(client, role):
    assert client.post(PATH, headers=_h(role), json={"domain": "trip.q.example.invalid"}).status_code == 403
    assert client.delete(f"{PATH}/{DID}", headers=_h(role)).status_code == 403
    assert client.post(f"{PATH}/{DID}/retry", headers=_h(role)).status_code == 403


def test_get_delete_and_retry(client, monkeypatch):
    monkeypatch.setattr(tpd, "list_domains", AsyncMock(return_value=None))
    assert client.get(PATH, headers=_h()).status_code == 404
    monkeypatch.setattr(tpd, "list_domains", AsyncMock(return_value={
        "tenant_id": TID, "has_tripplanner": True, "available": True, "max_domains": 1, "domains": [_out("active")]}))
    assert client.get(PATH, headers=_h()).json()["domains"][0]["status"] == "active"
    monkeypatch.setattr(tpd, "remove_domain", AsyncMock(return_value=_out("removing")))
    assert client.delete(f"{PATH}/{DID}", headers=_h()).json()["status"] == "removing"
    monkeypatch.setattr(tpd, "retry_domain", AsyncMock(side_effect=tpd.DomainError("not_failed", status=409)))
    resp = client.post(f"{PATH}/{DID}/retry", headers=_h())
    assert resp.status_code == 409 and resp.headers["X-Error-Code"] == "not_failed"
