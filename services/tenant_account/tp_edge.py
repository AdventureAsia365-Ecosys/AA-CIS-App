"""AA-1090 TripPlanner R8 — the AWS side of a tenant's custom host: ACM (us-east-1) and the TripPlanner AGENCY
CloudFront distribution. Used by the ``tripplanner_custom_domain`` job only
(``services/jobs/tripplanner_custom_domain_job.py``).

Why an agency distribution of its own. The public TripPlanner host (``tripplanner.<zone>``) stays on its Terraform
distribution with the Terraform wildcard certificate; nothing here can touch it. Agency hosts are aliases of a second
distribution (AA-CIS-Infra ``tripplanner_web_edge.tf``: same origin, behaviours and site header, no alias of its own),
so an agency's DNS can never break the renewal of the certificate our own host serves.

Why one edge certificate. A CloudFront distribution serves ONE viewer certificate, and that certificate must name every
alternate domain name of the distribution. So each tenant host gets its own certificate first (ownership: the agency's
validation record, shown on Tenant 360), and once it is issued the job requests the EDGE certificate naming every
attached host. ACM reuses the same validation record for a name within one account, so the edge certificate is issued
as soon as each of its names is validated, without a new record. The job then swaps the distribution's aliases and
certificate in one UpdateDistribution and deletes the edge certificate it replaced. With no host left, the distribution
goes back to no alias and the CloudFront default certificate: exactly the Terraform state (AA-CIS-Infra ignores
aliases and the viewer certificate on that distribution only).

The shared edge certificate still couples the agencies to each other: ACM can renew it (or issue a replacement) only
while every agency keeps its validation record. ``EdgeState.pending_names`` and :func:`edge_health` name the hosts
whose record ACM cannot find, so the job can detach exactly those (with the settings
``cis.tripplanner.custom_domain_edge_pending_hours`` and ``..._renewal_grace_days``) instead of failing an unrelated
host.

Least privilege (AA-CIS-Infra ``modules/ecs`` policy ``tripplanner_custom_domain``): RequestCertificate only with the
job's tag, its tag keys and DNS validation in us-east-1; AddTagsToCertificate only with that tag and those keys;
Describe / Delete only on certificates carrying that tag; an explicit Deny on every Terraform-managed certificate
(``ManagedBy = terraform``) for tagging and deletion; Get / UpdateDistribution only on the agency distribution.

Configuration (environment, set by AA-CIS-Infra on the CIS api and worker): ``TP_AGENCY_DISTRIBUTION_ID``. Missing =
custom domains are off (the editor answers 409, the job fails a host ``edge_not_configured``). No threshold here: the
job reads its limits from the settings registry.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Optional

ACM_REGION = "us-east-1"            # CloudFront reads viewer certificates from us-east-1 only (an AWS rule)
TAG_KEY = "aa-managed-by"
TAG_VALUE = "tripplanner-custom-domain"
DIST_ENV = "TP_AGENCY_DISTRIBUTION_ID"

# ACM certificate states (DescribeCertificate Certificate.Status)
ISSUED = "ISSUED"
PENDING = "PENDING_VALIDATION"
FAILED_STATES = ("FAILED", "VALIDATION_TIMED_OUT", "REVOKED", "EXPIRED", "INACTIVE")
# A name whose validation ACM cannot complete (DomainValidation.ValidationStatus / RenewalSummary statuses)
BLOCKING_VALIDATION = ("PENDING_VALIDATION", "FAILED")
# Error codes of a certificate this role can no longer reach: deleted already, or (with the tag-conditioned policy)
# IAM cannot read the tag of a deleted ARN and answers AccessDenied. Either way there is nothing left to delete.
GONE_CODES = ("ResourceNotFoundException", "AccessDeniedException")


class EdgeError(Exception):
    """A failure the host cannot get past by waiting: ``code`` is stored on the row (failure_code)."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(code, message)
        self.code = code
        self.message = message or code


class EdgeBusy(Exception):
    """Try again shortly (a concurrent distribution change: ETag mismatch, a certificate still in use)."""


def distribution_id() -> Optional[str]:
    return (os.environ.get(DIST_ENV) or "").strip() or None


def configured() -> bool:
    return bool(distribution_id())


def clients() -> tuple[Any, Any]:
    """(acm in us-east-1, cloudfront). Imported lazily: the API process never needs boto3 for the editor."""
    import boto3
    from botocore.config import Config

    cfg = Config(connect_timeout=3, read_timeout=20, retries={"max_attempts": 2, "mode": "standard"})
    return boto3.client("acm", region_name=ACM_REGION, config=cfg), boto3.client("cloudfront", config=cfg)


def error_code(exc: BaseException) -> str:
    resp = getattr(exc, "response", None)
    return str((resp or {}).get("Error", {}).get("Code", "")) if isinstance(resp, dict) else ""


# ── pure helpers ────────────────────────────────────────────────────────────────────────────────────────────────

def idempotency_token(names: Iterable[str]) -> str:
    """ACM IdempotencyToken (\\w{1,32}): the same name list asked again within an hour returns the same certificate."""
    return hashlib.sha256("\n".join(sorted(n.lower() for n in names)).encode()).hexdigest()[:32]


def domain_token(domain_id: str) -> str:
    return domain_id.replace("-", "")[:32]


def tags(**extra: str) -> list[dict]:
    return [{"Key": TAG_KEY, "Value": TAG_VALUE}, *({"Key": k, "Value": v} for k, v in extra.items())]


def certificate_names(cert: dict) -> list[str]:
    names = [cert["DomainName"], *cert.get("SubjectAlternativeNames", [])]
    out: list[str] = []
    for n in names:
        if n.lower() not in out:
            out.append(n.lower())
    return out


def validation_record(cert: dict, domain: str) -> Optional[tuple[str, str]]:
    """(name, value) of the CNAME ACM asks for ``domain``, once DescribeCertificate shows it (a few seconds after the
    request), else None."""
    for opt in cert.get("DomainValidationOptions", []):
        rec = opt.get("ResourceRecord")
        if opt.get("DomainName", "").lower() == domain and rec and rec.get("Type") == "CNAME":
            return rec["Name"], rec["Value"]
    return None


def blocking_names(options: Iterable[dict]) -> list[str]:
    """The names of a validation list (DomainValidationOptions, or RenewalSummary.DomainValidationOptions) whose
    validation ACM cannot complete: their record is missing from the agency's DNS."""
    return sorted({o["DomainName"].lower() for o in options
                   if o.get("DomainName") and o.get("ValidationStatus") in BLOCKING_VALIDATION})


# ── ACM ─────────────────────────────────────────────────────────────────────────────────────────────────────────

def request_host_certificate(acm, domain: str, domain_id: str) -> str:
    resp = acm.request_certificate(DomainName=domain, ValidationMethod="DNS",
                                   IdempotencyToken=domain_token(domain_id),
                                   Options={"CertificateTransparencyLoggingPreference": "ENABLED"},
                                   Tags=tags(**{"aa-domain-id": domain_id}))
    return str(resp["CertificateArn"])


def describe(acm, arn: str) -> dict:
    return acm.describe_certificate(CertificateArn=arn)["Certificate"]


def describe_or_none(acm, arn: Optional[str]) -> Optional[dict]:
    """The certificate, or None when it is gone (or no longer one this role may read)."""
    if not arn:
        return None
    try:
        return describe(acm, arn)
    except Exception as exc:
        if error_code(exc) in GONE_CODES:
            return None
        raise


def delete_certificate(acm, arn: Optional[str]) -> None:
    """Delete one of OUR certificates (the IAM tag condition refuses any other). Gone already (ResourceNotFound, or
    AccessDenied: IAM cannot read the tag of a deleted ARN) = done; still attached to the distribution (CloudFront is
    deploying the swap) = EdgeBusy, try again later."""
    if not arn:
        return
    try:
        acm.delete_certificate(CertificateArn=arn)
    except Exception as exc:
        code = error_code(exc)
        if code in GONE_CODES:
            return
        if code in ("ResourceInUseException", "ThrottlingException"):
            raise EdgeBusy(code) from exc
        raise


# ── the distribution ────────────────────────────────────────────────────────────────────────────────────────────

@dataclass
class EdgeState:
    """What sync_edge did: ``in_sync`` (nothing to change), ``updated`` (UpdateDistribution sent) or ``pending_cert``
    (the edge certificate is not issued yet; ``pending_arn`` is that certificate, which the caller passes back on its
    next run so no second one is requested; ``pending_names`` the hosts whose validation ACM has not completed and
    ``pending_since`` when the certificate was requested). ``deployed``: the distribution reports Deployed.
    ``retired``: the edge certificate the update replaced (the caller deletes it once CloudFront lets go of it).
    ``cname_target``: the distribution's own domain name (the CNAME value an agency points its host at)."""
    status: str
    deployed: bool
    cname_target: str
    retired: Optional[str] = None
    pending_arn: Optional[str] = None
    pending_names: list[str] = field(default_factory=list)
    pending_since: Optional[datetime] = None


def get_distribution(cf, dist_id: str) -> tuple[dict, str]:
    resp = cf.get_distribution(Id=dist_id)
    return resp["Distribution"], resp["ETag"]


def cname_target(cf, dist_id: str) -> str:
    dist, _ = get_distribution(cf, dist_id)
    return str(dist["DomainName"]).lower()


def _aliases(config: dict) -> list[str]:
    return [a.lower() for a in config.get("Aliases", {}).get("Items", []) or []]


def _served_certificate(config: dict) -> Optional[str]:
    viewer = config.get("ViewerCertificate", {})
    if viewer.get("CloudFrontDefaultCertificate"):
        return None
    return viewer.get("ACMCertificateArn") or None


def serves(cf, host: str, dist_id: Optional[str] = None) -> bool:
    """Whether ``host`` is an alias of the agency distribution now (a removal needs no edge change otherwise)."""
    dist_id = dist_id or distribution_id()
    if not dist_id:
        return False
    dist, _ = get_distribution(cf, dist_id)
    return host.lower() in _aliases(dist["DistributionConfig"])


def _matching(acm, arn: Optional[str], names: list[str]) -> Optional[dict]:
    """The certificate ``arn`` when it still exists and names exactly ``names`` (else None)."""
    cert = describe_or_none(acm, arn)
    return cert if cert is not None and set(certificate_names(cert)) == set(names) else None


def _edge_certificate(acm, names: list[str], current_cert: Optional[str],
                      pending_arn: Optional[str]) -> tuple[str, dict]:
    """(arn, certificate) naming exactly ``names``: the one the distribution serves, the one an earlier run requested,
    or a new request (ACM's idempotency token only holds for an hour, so the two first are reused explicitly: a sync
    never piles up certificates)."""
    for arn in (current_cert, pending_arn):
        cert = _matching(acm, arn, names)
        if cert is not None and cert.get("Status") in (ISSUED, PENDING):
            return str(arn), cert
    extra = {"SubjectAlternativeNames": names[1:]} if names[1:] else {}   # ACM refuses an empty SAN list (min 1)
    arn = acm.request_certificate(
        DomainName=names[0], ValidationMethod="DNS", IdempotencyToken=idempotency_token(names),
        Options={"CertificateTransparencyLoggingPreference": "ENABLED"},
        Tags=tags(**{"aa-edge": "tripplanner"}), **extra)["CertificateArn"]
    return str(arn), describe(acm, arn)


def _viewer_certificate(target_cert: Optional[str]) -> dict:
    if target_cert is None:   # no agency host: the CloudFront default certificate, as Terraform created it
        return {"CloudFrontDefaultCertificate": True, "MinimumProtocolVersion": "TLSv1"}
    return {"CloudFrontDefaultCertificate": False, "ACMCertificateArn": target_cert, "SSLSupportMethod": "sni-only",
            "MinimumProtocolVersion": "TLSv1.2_2021"}


def sync_edge(acm, cf, hosts: Iterable[str], *, dist_id: Optional[str] = None,
              pending_arn: Optional[str] = None) -> EdgeState:
    """Make the agency distribution serve exactly ``hosts`` (the attaching / active hosts), with a certificate that
    names all of them, or no alias and the default certificate when there is none. Converges from any state: an alias
    not in ``hosts`` (a removed host, or a row the tenant erasure deleted) is detached."""
    dist_id = dist_id or distribution_id()
    if not dist_id:
        raise EdgeError("edge_not_configured", f"{DIST_ENV} is not set")
    wanted = sorted({h.lower() for h in hosts})
    dist, etag = get_distribution(cf, dist_id)
    config = dist["DistributionConfig"]
    target = str(dist["DomainName"]).lower()
    deployed = dist.get("Status") == "Deployed"
    current_aliases = _aliases(config)
    current_cert = _served_certificate(config)

    target_cert: Optional[str] = None
    if wanted:
        edge_arn, cert = _edge_certificate(acm, wanted, current_cert, pending_arn)
        status = cert.get("Status")
        if status == PENDING:
            return EdgeState("pending_cert", deployed, target, pending_arn=edge_arn,
                             pending_names=blocking_names(cert.get("DomainValidationOptions", [])),
                             pending_since=cert.get("CreatedAt"))
        if status != ISSUED:
            raise EdgeError("edge_certificate_" + str(status).lower(),
                            f"the edge certificate is {status}: a host's validation record may be gone")
        target_cert = edge_arn

    if sorted(set(current_aliases)) == wanted and current_cert == target_cert:
        return EdgeState("in_sync", deployed, target)

    config["Aliases"] = {"Quantity": len(wanted), "Items": wanted} if wanted else {"Quantity": 0}
    config["ViewerCertificate"] = _viewer_certificate(target_cert)
    try:
        cf.update_distribution(Id=dist_id, IfMatch=etag, DistributionConfig=config)
    except Exception as exc:
        code = error_code(exc)
        if code in ("PreconditionFailed", "InvalidIfMatchVersion", "Throttling"):
            raise EdgeBusy(code) from exc
        if code == "CNAMEAlreadyExists":
            raise EdgeError("alias_in_use", "another CloudFront distribution (any AWS account) already serves one of "
                                            "these hosts; the agency must remove it there first") from exc
        if code in ("InvalidViewerCertificate", "InvalidArgument"):
            raise EdgeError("edge_update_refused", f"CloudFront refused the change ({code})") from exc
        raise
    retired = current_cert if current_cert not in (None, target_cert) else None
    return EdgeState("updated", False, target, retired)


# ── the daily edge check ────────────────────────────────────────────────────────────────────────────────────────

@dataclass
class EdgeHealth:
    """The served edge certificate as ACM sees it. ``blocking``: the hosts whose validation record ACM cannot find
    (they stop managed renewal for every host on the certificate); ``not_after``: its expiry; ``renewal_status``:
    RenewalSummary.RenewalStatus (None before ACM starts renewing, 60 days before expiry)."""
    cert_arn: Optional[str]
    status: Optional[str] = None
    not_after: Optional[datetime] = None
    renewal_status: Optional[str] = None
    blocking: list[str] = field(default_factory=list)


def edge_health(acm, cf, *, dist_id: Optional[str] = None) -> EdgeHealth:
    """Read the agency distribution's certificate: its status, expiry and the hosts blocking its renewal (from
    RenewalSummary while ACM renews it, else from its own validation list when it is not ISSUED)."""
    dist_id = dist_id or distribution_id()
    if not dist_id:
        raise EdgeError("edge_not_configured", f"{DIST_ENV} is not set")
    dist, _ = get_distribution(cf, dist_id)
    arn = _served_certificate(dist["DistributionConfig"])
    cert = describe_or_none(acm, arn)
    if cert is None:
        return EdgeHealth(arn)
    renewal = cert.get("RenewalSummary") or {}
    renewal_status = renewal.get("RenewalStatus")
    blocking: list[str] = []
    if renewal_status in BLOCKING_VALIDATION:
        blocking = blocking_names(renewal.get("DomainValidationOptions", []))
    elif cert.get("Status") != ISSUED:
        blocking = blocking_names(cert.get("DomainValidationOptions", []))
    return EdgeHealth(arn, cert.get("Status"), cert.get("NotAfter"), renewal_status, blocking)
