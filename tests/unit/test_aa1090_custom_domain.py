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


def _row(status="pending", **kw):
    row = {"domain_id": DID, "tenant_id": TID, "domain": Q, "status": status, "certificate_arn": None,
           "validation_name": None, "validation_value": None, "cname_target": None, "failure_code": None,
           "failure_message": None, "requested_at": datetime.now(timezone.utc),
           "validated_at": datetime.now(timezone.utc) if status in ("attaching", "active") else None}
    row.update(kw)
    return row


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
