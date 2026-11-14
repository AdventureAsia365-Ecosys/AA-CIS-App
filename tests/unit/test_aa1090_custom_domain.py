"""AA-1090 TripPlanner R8 — a tenant's custom host: the editor's rules (services/tenant_account/tripplanner_domain.py),
the state machine, the edge sync against fake ACM / CloudFront clients (tp_edge.py: attach, in step, pending
certificate, removal back to the base certificate, an orphan alias detached, refusals) and the job
(services/jobs/tripplanner_custom_domain_job.py) on an in-memory row, plus the admin routes. No AWS, no database: the
database
path is in tests/integration/test_aa1090_custom_domain_pg.py. Invented hosts only (example.invalid, example.test).
"""
from __future__ import annotations

import asyncio
import copy
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
BASE = f"{ACM}/00000000-0000-4000-8000-0000000000b0"
HOST_CERT = f"{ACM}/00000000-0000-4000-8000-0000000000c1"
BASE_NAMES = ["*.planner-zone.example.test", "planner-zone.example.test"]
BASE_ALIAS = "tripplanner.planner-zone.example.test"
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
    ("attaching", "failed"): "failed", ("failed", "retry"): "pending",
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

@pytest.mark.parametrize("host,ok", [
    (BASE_ALIAS, True), ("planner-zone.example.test", True), ("a.b.planner-zone.example.test", False),
    ("trip.quillmere.example.invalid", False),
])
def test_covered(host, ok):
    assert tp_edge.covered(host, BASE_NAMES) is ok


def test_idempotency_token_is_order_free_and_short():
    a = tp_edge.idempotency_token(["b.example.test", "a.example.test"])
    assert a == tp_edge.idempotency_token(["A.example.test", "b.example.test"]) and len(a) == 32 and a.isalnum()


def test_validation_record():
    cert = {"DomainValidationOptions": [{"DomainName": "trip.q.example.invalid",
                                         "ResourceRecord": {"Name": "_a.", "Type": "CNAME", "Value": "_b."}}]}
    assert tp_edge.validation_record(cert, "trip.q.example.invalid") == ("_a.", "_b.")
    assert tp_edge.validation_record({"DomainValidationOptions": [{"DomainName": "x"}]}, "x") is None


# ── fakes ───────────────────────────────────────────────────────────────────────────────────────────────────────

class ClientError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeAcm:
    """Certificates by ARN with their names, status and tags; a request with the same names returns the same ARN."""

    def __init__(self):
        self.certs = {BASE: {"DomainName": BASE_NAMES[0], "SubjectAlternativeNames": BASE_NAMES, "Status": "ISSUED",
                             "tags": []}}
        self.requests: list[dict] = []
        self.deleted: list[str] = []
        self.next_status = "ISSUED"
        self.in_use: set[str] = set()

    def request_certificate(self, **kw):
        self.requests.append(kw)
        assert kw["ValidationMethod"] == "DNS"
        assert {"Key": tp_edge.TAG_KEY, "Value": tp_edge.TAG_VALUE} in kw["Tags"]
        arn = f"{ACM}/00000000-0000-4000-8000-{len(self.certs):012d}"
        names = [kw["DomainName"], *kw.get("SubjectAlternativeNames", [])]
        self.certs[arn] = {"DomainName": kw["DomainName"], "SubjectAlternativeNames": names,
                           "Status": self.next_status, "tags": kw["Tags"],
                           "DomainValidationOptions": [{"DomainName": n, "ResourceRecord": {
                               "Name": f"_v.{n}.", "Type": "CNAME", "Value": "_x.acm-validations.aws."}}
                               for n in names]}
        return {"CertificateArn": arn}

    def describe_certificate(self, CertificateArn):
        if CertificateArn not in self.certs:
            raise ClientError("ResourceNotFoundException")
        return {"Certificate": {k: v for k, v in self.certs[CertificateArn].items() if k != "tags"}}

    def delete_certificate(self, CertificateArn):
        assert CertificateArn != BASE, "the base certificate must never be deleted"
        if CertificateArn in self.in_use:
            raise ClientError("ResourceInUseException")
        if CertificateArn not in self.certs:
            raise ClientError("ResourceNotFoundException")
        del self.certs[CertificateArn]
        self.deleted.append(CertificateArn)


class FakeCf:
    def __init__(self, aliases=(BASE_ALIAS,), cert=BASE, status="Deployed"):
        self.config = {"Aliases": {"Quantity": len(aliases), "Items": list(aliases)}, "Comment": "kept",
                       "ViewerCertificate": {"ACMCertificateArn": cert, "SSLSupportMethod": "sni-only",
                                             "MinimumProtocolVersion": "TLSv1.2_2021", "Certificate": cert,
                                             "CertificateSource": "acm"}}
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


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv(tp_edge.DIST_ENV, DIST)
    monkeypatch.setenv(tp_edge.BASE_CERT_ENV, BASE)


# ── sync_edge ───────────────────────────────────────────────────────────────────────────────────────────────────

def test_attach_requests_one_edge_certificate_and_swaps_aliases_and_certificate(env):
    acm, cf = FakeAcm(), FakeCf()
    state = tp_edge.sync_edge(acm, cf, ["trip.quillmere.example.invalid"])
    assert state.status == "updated" and state.cname_target == TARGET and state.retired is None
    req = acm.requests[0]
    assert [req["DomainName"], *req["SubjectAlternativeNames"]] == BASE_NAMES + ["trip.quillmere.example.invalid"]
    cfg = cf.updates[0]
    assert cfg["Aliases"] == {"Quantity": 2, "Items": sorted([BASE_ALIAS, "trip.quillmere.example.invalid"])}
    edge = cfg["ViewerCertificate"]["ACMCertificateArn"]
    assert edge != BASE and cfg["ViewerCertificate"]["SSLSupportMethod"] == "sni-only"
    assert "Certificate" not in cfg["ViewerCertificate"] and cfg["Comment"] == "kept"


def test_in_step_reuses_the_served_certificate_and_sends_nothing(env):
    acm, cf = FakeAcm(), FakeCf()
    tp_edge.sync_edge(acm, cf, ["trip.quillmere.example.invalid"])
    cf.status = "Deployed"
    state = tp_edge.sync_edge(acm, cf, ["trip.quillmere.example.invalid"])
    assert state.status == "in_sync" and state.deployed and len(cf.updates) == 1 and len(acm.requests) == 1


def test_a_pending_edge_certificate_waits_and_is_reused(env):
    acm, cf = FakeAcm(), FakeCf()
    acm.next_status = "PENDING_VALIDATION"
    state = tp_edge.sync_edge(acm, cf, ["trip.quillmere.example.invalid"])
    assert state.status == "pending_cert" and state.pending_arn and cf.updates == []
    acm.certs[state.pending_arn]["Status"] = "ISSUED"
    again = tp_edge.sync_edge(acm, cf, ["trip.quillmere.example.invalid"], pending_arn=state.pending_arn)
    assert again.status == "updated" and len(acm.requests) == 1
    assert cf.config["ViewerCertificate"]["ACMCertificateArn"] == state.pending_arn


def test_a_second_host_replaces_the_edge_certificate_and_retires_the_old_one(env):
    acm, cf = FakeAcm(), FakeCf()
    tp_edge.sync_edge(acm, cf, ["trip.quillmere.example.invalid"])
    first = cf.config["ViewerCertificate"]["ACMCertificateArn"]
    state = tp_edge.sync_edge(acm, cf, ["trip.quillmere.example.invalid", "go.larchfield.example.invalid"])
    assert state.status == "updated" and state.retired == first
    assert cf.config["Aliases"]["Items"] == sorted([BASE_ALIAS, "go.larchfield.example.invalid",
                                                    "trip.quillmere.example.invalid"])


def test_removing_the_last_host_goes_back_to_the_base_state(env):
    acm, cf = FakeAcm(), FakeCf()
    tp_edge.sync_edge(acm, cf, ["trip.quillmere.example.invalid"])
    edge = cf.config["ViewerCertificate"]["ACMCertificateArn"]
    state = tp_edge.sync_edge(acm, cf, [])
    assert state.status == "updated" and state.retired == edge and len(acm.requests) == 1
    assert cf.config["Aliases"] == {"Quantity": 1, "Items": [BASE_ALIAS]}
    assert cf.config["ViewerCertificate"]["ACMCertificateArn"] == BASE


def test_an_orphan_alias_is_detached(env):
    acm, cf = FakeAcm(), FakeCf(aliases=(BASE_ALIAS, "gone.erased-agency.example.invalid"))
    state = tp_edge.sync_edge(acm, cf, [])
    assert state.status == "updated" and cf.config["Aliases"]["Items"] == [BASE_ALIAS]


def test_alias_served_elsewhere_is_a_failure_and_etag_race_is_busy(env):
    acm, cf = FakeAcm(), FakeCf()
    cf.fail_with = "CNAMEAlreadyExists"
    with pytest.raises(tp_edge.EdgeError) as e:
        tp_edge.sync_edge(acm, cf, ["trip.quillmere.example.invalid"])
    assert e.value.code == "alias_in_use"
    cf.fail_with = "PreconditionFailed"
    with pytest.raises(tp_edge.EdgeBusy):
        tp_edge.sync_edge(acm, cf, ["trip.quillmere.example.invalid"])


def test_a_failed_edge_certificate_is_a_failure(env):
    acm, cf = FakeAcm(), FakeCf()
    acm.next_status = "FAILED"
    with pytest.raises(tp_edge.EdgeError) as e:
        tp_edge.sync_edge(acm, cf, ["trip.quillmere.example.invalid"])
    assert e.value.code == "edge_certificate_failed"


def test_not_configured(monkeypatch):
    monkeypatch.delenv(tp_edge.DIST_ENV, raising=False)
    assert tp_edge.configured() is False
    with pytest.raises(tp_edge.EdgeError) as e:
        tp_edge.sync_edge(FakeAcm(), FakeCf(), [])
    assert e.value.code == "edge_not_configured"


def test_delete_certificate_tolerates_gone_and_waits_while_in_use():
    acm = FakeAcm()
    tp_edge.delete_certificate(acm, HOST_CERT)          # not there: done
    acm.certs[HOST_CERT] = {"DomainName": "x", "Status": "ISSUED", "tags": []}
    acm.in_use.add(HOST_CERT)
    with pytest.raises(tp_edge.EdgeBusy):
        tp_edge.delete_certificate(acm, HOST_CERT)


# ── the job ─────────────────────────────────────────────────────────────────────────────────────────────────────

class FakePool:
    """One domain row (plus other edge hosts) behind the job's SQL constants."""

    def __init__(self, row: dict, others=()):
        self.row = row
        self.others = list(others)
        self.calls: list[tuple] = []

    async def fetchrow(self, sql, *args):
        assert sql == job.ROW_SQL
        return dict(self.row) if self.row is not None else None

    async def fetch(self, sql, *args):
        assert sql == job.EDGE_HOSTS_SQL
        rows = list(self.others)
        if self.row is not None and self.row["status"] in ("attaching", "active"):
            rows.append(self.row["domain"])
        return [{"domain": d} for d in sorted(rows)]

    async def execute(self, sql, *args):
        self.calls.append((sql, args))
        r = self.row
        if sql == job.SET_CERT_SQL:
            r["certificate_arn"] = args[1]
        elif sql == job.VALIDATING_SQL and r["status"] == "pending":
            r.update(status="validating", validation_name=args[1], validation_value=args[2], cname_target=args[3])
        elif sql == job.ATTACHING_SQL and r["status"] == "validating":
            r["status"] = "attaching"
        elif sql == job.ACTIVE_SQL and r["status"] == "attaching":
            r.update(status="active", cname_target=args[1])
        elif sql == job.FAILED_SQL and r["status"] in ("pending", "validating", "attaching"):
            r.update(status="failed", failure_code=args[1], failure_message=args[2])
            if args[3]:
                r["certificate_arn"] = None
        elif sql == job.DELETE_SQL and r["status"] == "removing":
            self.row = None


def _row(status="pending", **kw):
    row = {"domain_id": DID, "tenant_id": TID, "domain": "trip.quillmere.example.invalid", "status": status,
           "certificate_arn": None, "validation_name": None, "validation_value": None, "cname_target": None,
           "failure_code": None, "failure_message": None, "requested_at": datetime.now(timezone.utc)}
    row.update(kw)
    return row


def _ctx(pool, progress=None):
    return SimpleNamespace(pool=pool, payload={"domain_id": DID}, job_id="job-1090",
                           job=SimpleNamespace(progress=dict(progress or {})), set_result=lambda r: None)


@pytest.fixture
def knobs(monkeypatch, env):
    values = {job.POLL_KEY: 120, job.TIMEOUT_KEY: 72}
    monkeypatch.setattr(job.settings, "aget", AsyncMock(side_effect=lambda k, *a: values[k]))
    return values


def _run(ctx, acm, cf):
    return asyncio.run(job.run_once(ctx, acm, cf))


def _resume(ctx, exc: Reschedule):
    ctx.job.progress = {**ctx.job.progress, **exc.progress}


def test_the_job_kind_is_registered_serialised_and_without_llm():
    from shared.llm_client.capacity import KIND_LINES

    kind = get_kind("tripplanner_custom_domain")
    assert kind is not None and kind.concurrency == 1 and kind.on_terminal is job.mark_terminal
    assert KIND_LINES["tripplanner_custom_domain"] is None


def test_from_added_to_active(knobs):
    acm, cf, pool = FakeAcm(), FakeCf(), FakePool(_row())
    ctx = _ctx(pool)
    with pytest.raises(Reschedule) as r:
        _run(ctx, acm, cf)
    assert pool.row["status"] == "validating" and pool.row["cname_target"] == TARGET
    assert pool.row["validation_name"] == "_v.trip.quillmere.example.invalid." and r.value.delay_seconds >= 120
    host_cert = pool.row["certificate_arn"]
    acm.certs[host_cert]["Status"] = "PENDING_VALIDATION"
    with pytest.raises(Reschedule):
        _run(ctx, acm, cf)                              # still waiting for the agency's record
    acm.certs[host_cert]["Status"] = "ISSUED"
    with pytest.raises(Reschedule) as r:
        _run(ctx, acm, cf)                              # issued -> attaching -> distribution updated, deploying
    _resume(ctx, r.value)
    assert pool.row["status"] == "attaching" and "trip.quillmere.example.invalid" in cf.config["Aliases"]["Items"]
    cf.status = "Deployed"
    out = _run(ctx, acm, cf)
    assert out == {"domain_id": DID, "status": "active"} and pool.row["status"] == "active"
    assert len(cf.updates) == 1


def test_validation_timeout_fails_and_deletes_the_host_certificate(knobs):
    acm, cf = FakeAcm(), FakeCf()
    acm.certs[HOST_CERT] = {"DomainName": "trip.quillmere.example.invalid", "Status": "PENDING_VALIDATION",
                            "tags": []}
    pool = FakePool(_row("validating", certificate_arn=HOST_CERT,
                         requested_at=datetime.now(timezone.utc) - timedelta(hours=73)))
    out = _run(_ctx(pool), acm, cf)
    assert out["failure_code"] == "validation_timeout" and pool.row["status"] == "failed"
    assert HOST_CERT in acm.deleted and pool.row["certificate_arn"] is None


def test_a_failed_certificate_fails_the_host(knobs):
    acm, cf = FakeAcm(), FakeCf()
    acm.certs[HOST_CERT] = {"DomainName": "trip.quillmere.example.invalid", "Status": "FAILED",
                            "FailureReason": "CAA_ERROR", "tags": []}
    pool = FakePool(_row("validating", certificate_arn=HOST_CERT))
    out = _run(_ctx(pool), acm, cf)
    assert out["failure_code"] == "certificate_failed" and "CAA_ERROR" in pool.row["failure_message"]


def test_removal_detaches_deletes_the_certificate_and_the_row(knobs):
    acm, cf = FakeAcm(), FakeCf()
    tp_edge.sync_edge(acm, cf, ["trip.quillmere.example.invalid"])
    edge = cf.config["ViewerCertificate"]["ACMCertificateArn"]
    acm.certs[HOST_CERT] = {"DomainName": "trip.quillmere.example.invalid", "Status": "ISSUED", "tags": []}
    acm.in_use.add(edge)                                 # CloudFront still deploys the swap
    pool = FakePool(_row("removing", certificate_arn=HOST_CERT))
    ctx = _ctx(pool)
    with pytest.raises(Reschedule) as r:
        _run(ctx, acm, cf)
    _resume(ctx, r.value)
    assert cf.config["Aliases"]["Items"] == [BASE_ALIAS] and cf.config["ViewerCertificate"]["ACMCertificateArn"] == BASE
    assert HOST_CERT in acm.deleted and pool.row is not None   # the old edge certificate is still held
    acm.in_use.clear()
    out = _run(ctx, acm, cf)
    assert out == {"domain_id": DID, "status": "removed"} and pool.row is None and edge in acm.deleted


def test_removal_keeps_the_other_hosts(knobs):
    acm, cf = FakeAcm(), FakeCf()
    tp_edge.sync_edge(acm, cf, ["trip.quillmere.example.invalid", "go.larchfield.example.invalid"])
    both = cf.config["ViewerCertificate"]["ACMCertificateArn"]
    pool = FakePool(_row("removing"), others=["go.larchfield.example.invalid"])
    assert _run(_ctx(pool), acm, cf)["status"] == "removed"
    assert cf.config["Aliases"]["Items"] == sorted([BASE_ALIAS, "go.larchfield.example.invalid"])
    assert both in acm.deleted and cf.config["ViewerCertificate"]["ACMCertificateArn"] not in (both, BASE)


def test_not_configured_fails_the_host(monkeypatch, knobs):
    monkeypatch.delenv(tp_edge.BASE_CERT_ENV)
    pool = FakePool(_row())
    out = _run(_ctx(pool), FakeAcm(), FakeCf())
    assert out["failure_code"] == "edge_not_configured" and pool.row["status"] == "failed"


def test_alias_in_use_fails_the_host(knobs):
    acm, cf = FakeAcm(), FakeCf()
    cf.fail_with = "CNAMEAlreadyExists"
    acm.certs[HOST_CERT] = {"DomainName": "trip.quillmere.example.invalid", "Status": "ISSUED", "tags": []}
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


def test_failed_and_gone_rows_are_left_alone(knobs):
    assert _run(_ctx(FakePool(_row("failed"))), FakeAcm(), FakeCf())["skipped"] is True
    assert _run(_ctx(FakePool(None)), FakeAcm(), FakeCf())["status"] == "gone"


def test_a_bad_payload_is_not_retried(knobs):
    ctx = _ctx(FakePool(_row()))
    ctx.payload = {}
    with pytest.raises(NonRetryable):
        _run(ctx, FakeAcm(), FakeCf())


def test_validation_waits_stay_within_the_reschedule_budget(knobs):
    from shared.jobs.registry import RESCHEDULE_MAX_COUNT, RESCHEDULE_MAX_DELAY_SECONDS

    delay = asyncio.run(job._validation_delay_s())
    assert delay <= RESCHEDULE_MAX_DELAY_SECONDS
    assert 72 * 3600 / delay <= RESCHEDULE_MAX_COUNT


def test_mark_terminal_fails_a_waiting_host():
    pool = MagicMock()
    pool.execute = AsyncMock()
    asyncio.run(job.mark_terminal(pool, {"id": "j1", "payload": {"domain_id": DID}, "error": "boom",
                                         "status": "failed"}))
    sql, *args = pool.execute.await_args.args
    assert sql == job.FAILED_SQL and args[1] == "job_failed"


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

    def transaction(self):
        return _Tx()

    async def execute(self, sql, *args):
        return None

    async def fetchval(self, sql, *args):
        if sql.startswith("\n        INSERT INTO shared.job") or "shared.job" in sql:
            return "job-1"
        return self.v[sql]

    async def fetchrow(self, sql, *args):
        if sql == tpd.INSERT_SQL:
            self.inserted.append(args)
            return {**_row(), "domain": args[1], "updated_at": datetime.now(timezone.utc), "updated_by": args[2]}
        if sql.startswith(tpd.ROW_SQL):
            return self.row
        if sql == tpd.STATUS_SQL:
            return {**self.row, "status": args[2], "updated_at": datetime.now(timezone.utc)}
        if "INSERT INTO shared.job" in sql:
            return {"id": "job-1"}
        raise AssertionError(sql)


@pytest.fixture
def editor(monkeypatch, env):
    audit = AsyncMock()
    monkeypatch.setattr(tpd, "write_audit_log", audit)
    monkeypatch.setattr(tpd, "enqueue", AsyncMock(return_value=("job-1", True)))
    monkeypatch.setattr(tpd.settings, "aget", AsyncMock(side_effect=lambda k, *a: {
        tpd.MAX_PER_TENANT_KEY: 1, tpd.EDGE_MAX_KEY: 8}[k]))
    return audit


def test_add_stores_audits_and_queues(editor):
    conn = EditorConn()
    out = asyncio.run(tpd.add_domain(conn, TID, domain="Trip.Quillmere.Example.Invalid", actor="admin:u1"))
    assert out["domain"] == "trip.quillmere.example.invalid" and out["status"] == "pending" and out["job_id"] == "job-1"
    assert conn.inserted == [(TID, "trip.quillmere.example.invalid", "admin:u1")]
    assert editor.await_args.kwargs["action"] == "tenant.tripplanner_domain_added"
    assert tpd.enqueue.await_args.kwargs["idempotency_key"].startswith("tripplanner_custom_domain:")


@pytest.mark.parametrize("kw,code,status", [
    ({"status": None}, "tenant_not_found", 404),
    ({"status": "offboarding"}, "tenant_offboarded", 409),
    ({"has_tp": False}, "no_tripplanner", 409),
    ({"taken": True}, tpd.DOMAIN_TAKEN, 409),
    ({"tenant_count": 1}, "domain_limit_reached", 409),
    ({"edge_count": 8}, "edge_full", 409),
])
def test_add_refusals(editor, kw, code, status):
    conn = EditorConn(**kw)
    with pytest.raises(tpd.DomainError) as e:
        asyncio.run(tpd.add_domain(conn, TID, domain="trip.quillmere.example.invalid", actor="admin:u1"))
    assert (e.value.code, e.value.status) == (code, status) and conn.inserted == [] and not editor.await_count


def test_add_refuses_a_bad_host_before_the_database(editor):
    with pytest.raises(tpd.DomainError) as e:
        asyncio.run(tpd.add_domain(EditorConn(), TID, domain="quillmere.example", actor="admin:u1"))
    assert e.value.code == tpd.DOMAIN_APEX and e.value.field == "domain"


def test_add_is_refused_when_the_edge_is_not_configured(editor, monkeypatch):
    monkeypatch.delenv(tp_edge.DIST_ENV)
    with pytest.raises(tpd.DomainError) as e:
        asyncio.run(tpd.add_domain(EditorConn(), TID, domain="trip.quillmere.example.invalid", actor="admin:u1"))
    assert e.value.code == "custom_domains_unavailable"


def test_remove_marks_removing_and_is_idempotent(editor):
    conn = EditorConn(row=_row("active", updated_at=datetime.now(timezone.utc)))
    out = asyncio.run(tpd.remove_domain(conn, TID, DID, actor="admin:u1"))
    assert out["status"] == "removing" and editor.await_args.kwargs["details"]["before"] == "active"
    again = asyncio.run(tpd.remove_domain(EditorConn(row=_row("removing", updated_at=None)), TID, DID,
                                          actor="admin:u1"))
    assert again["status"] == "removing" and editor.await_count == 1


def test_retry_only_from_failed(editor):
    with pytest.raises(tpd.DomainError) as e:
        asyncio.run(tpd.retry_domain(EditorConn(row=_row("active")), TID, DID, actor="admin:u1"))
    assert e.value.code == "not_failed"
    out = asyncio.run(tpd.retry_domain(EditorConn(row=_row("failed", failure_code="validation_timeout")), TID, DID,
                                       actor="admin:u1"))
    assert out["status"] == "pending" and editor.await_args.kwargs["action"] == "tenant.tripplanner_domain_retried"


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
