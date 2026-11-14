"""AA-1090 TripPlanner R8 — the AWS side of a tenant's custom host: ACM (us-east-1) and the TripPlanner CloudFront
distribution. Used by the ``tripplanner_custom_domain`` job (``services/jobs/tripplanner_custom_domain_job.py``) only.

Why one edge certificate. A CloudFront distribution serves ONE viewer certificate, and that certificate must name
every alternate domain name of the distribution. So each tenant host gets its own certificate first (ownership: the
agency's validation record, shown on Tenant 360), and once it is issued the job requests the EDGE certificate: the
base names (those of the Terraform base certificate, the wildcard of our zone) plus every attached host. ACM reuses
the same validation record for a name within one account, so the edge certificate is issued as soon as each of its
names is validated, without a new record. The job then swaps the distribution's aliases and certificate in one
UpdateDistribution and deletes the edge certificate it replaced. With no host left, the distribution goes back to the
base certificate and its base aliases: exactly the Terraform state (AA-CIS-Infra ignores aliases and the viewer
certificate on that distribution, ``tripplanner_web_edge.tf``).

Least privilege (AA-CIS-Infra ``modules/ecs`` policy ``tripplanner_custom_domain``): RequestCertificate only with the
tag
``aa-managed-by = tripplanner-custom-domain`` and DNS validation in us-east-1; Describe / Delete only on certificates
with that tag (plus Describe on the base certificate); Get / UpdateDistribution only on the TripPlanner distribution.
The base certificate can never be deleted by this role.

Configuration (environment, set by AA-CIS-Infra on the CIS api and worker): ``TP_WEB_DISTRIBUTION_ID`` and
``TP_WEB_BASE_CERT_ARN``. Either missing = custom domains are off (the editor answers 409, the job fails the host
``edge_not_configured``). No threshold here: the job reads its limits from the settings registry.
"""
from __future__ import annotations

import fnmatch
import hashlib
import os
from dataclasses import dataclass
from typing import Any, Iterable, Optional

ACM_REGION = "us-east-1"            # CloudFront reads viewer certificates from us-east-1 only (an AWS rule)
TAG_KEY = "aa-managed-by"
TAG_VALUE = "tripplanner-custom-domain"
DIST_ENV = "TP_WEB_DISTRIBUTION_ID"
BASE_CERT_ENV = "TP_WEB_BASE_CERT_ARN"

# ACM certificate states (DescribeCertificate Certificate.Status)
ISSUED = "ISSUED"
PENDING = "PENDING_VALIDATION"
FAILED_STATES = ("FAILED", "VALIDATION_TIMED_OUT", "REVOKED", "EXPIRED", "INACTIVE")


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


def base_certificate_arn() -> Optional[str]:
    return (os.environ.get(BASE_CERT_ENV) or "").strip() or None


def configured() -> bool:
    return bool(distribution_id() and base_certificate_arn())


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

def covered(host: str, names: Iterable[str]) -> bool:
    """Whether a certificate naming ``names`` serves ``host``: an exact name, or a wildcard ``*.zone`` for exactly
    one label left of the zone (the TLS rule; ``*.zone`` does not cover ``zone`` nor ``a.b.zone``)."""
    host = host.lower()
    for name in names:
        name = name.lower()
        if name == host:
            return True
        if name.startswith("*.") and fnmatch.fnmatchcase(host, name) and host.count(".") == name.count("."):
            return True
    return False


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


# ── ACM ─────────────────────────────────────────────────────────────────────────────────────────────────────────

def request_host_certificate(acm, domain: str, domain_id: str) -> str:
    resp = acm.request_certificate(DomainName=domain, ValidationMethod="DNS",
                                   IdempotencyToken=domain_token(domain_id),
                                   Options={"CertificateTransparencyLoggingPreference": "ENABLED"},
                                   Tags=tags(**{"aa-domain-id": domain_id}))
    return str(resp["CertificateArn"])


def describe(acm, arn: str) -> dict:
    return acm.describe_certificate(CertificateArn=arn)["Certificate"]


def delete_certificate(acm, arn: Optional[str]) -> None:
    """Delete one of OUR certificates (the IAM tag condition refuses any other). Gone already = done; still attached
    to the distribution (CloudFront is deploying the swap) = EdgeBusy, try again later."""
    if not arn:
        return
    try:
        acm.delete_certificate(CertificateArn=arn)
    except Exception as exc:
        code = error_code(exc)
        if code == "ResourceNotFoundException":
            return
        if code in ("ResourceInUseException", "ThrottlingException"):
            raise EdgeBusy(code) from exc
        raise


# ── the distribution ────────────────────────────────────────────────────────────────────────────────────────────

@dataclass
class EdgeState:
    """What sync_edge did: ``in_sync`` (nothing to change), ``updated`` (UpdateDistribution sent) or ``pending_cert``
    (the edge certificate is not issued yet; ``pending_arn`` is that certificate, which the caller passes back on its
    next run so no second one is requested). ``deployed``: the distribution reports Deployed. ``retired``: the edge
    certificate the update replaced (the caller deletes it once CloudFront lets go of it). ``cname_target``: the
    distribution's own domain name (the CNAME value an agency points its host at)."""
    status: str
    deployed: bool
    cname_target: str
    retired: Optional[str] = None
    pending_arn: Optional[str] = None


def get_distribution(cf, dist_id: str) -> tuple[dict, str]:
    resp = cf.get_distribution(Id=dist_id)
    return resp["Distribution"], resp["ETag"]


def cname_target(cf, dist_id: str) -> str:
    dist, _ = get_distribution(cf, dist_id)
    return str(dist["DomainName"]).lower()


def _matching(acm, arn: Optional[str], names: list[str], base_arn: str) -> Optional[dict]:
    """The certificate ``arn`` when it is one of ours and names exactly ``names`` (else None)."""
    if not arn or arn == base_arn:
        return None
    try:
        cert = describe(acm, arn)
    except Exception as exc:
        if error_code(exc) in ("ResourceNotFoundException", "AccessDeniedException"):
            return None
        raise
    return cert if set(certificate_names(cert)) == set(names) else None


def _edge_certificate(acm, names: list[str], current_cert: Optional[str], pending_arn: Optional[str],
                      base_arn: str) -> tuple[str, str]:
    """(arn, status) of a certificate naming exactly ``names``: the one the distribution serves, the one an earlier
    run requested, or a new request (ACM's idempotency token only holds for an hour, so the two first are reused
    explicitly: a sync never piles up certificates)."""
    for arn in (current_cert, pending_arn):
        cert = _matching(acm, arn, names, base_arn)
        if cert is not None and cert.get("Status") in (ISSUED, PENDING):
            return str(arn), str(cert["Status"])
    arn = acm.request_certificate(
        DomainName=names[0], SubjectAlternativeNames=names[1:], ValidationMethod="DNS",
        IdempotencyToken=idempotency_token(names),
        Options={"CertificateTransparencyLoggingPreference": "ENABLED"},
        Tags=tags(**{"aa-edge": "tripplanner"}))["CertificateArn"]
    return str(arn), str(describe(acm, arn).get("Status"))


def sync_edge(acm, cf, hosts: Iterable[str], *, dist_id: Optional[str] = None, base_arn: Optional[str] = None,
              pending_arn: Optional[str] = None) -> EdgeState:
    """Make the distribution serve exactly its base aliases plus ``hosts`` (the attaching / active hosts), with a
    certificate that names all of them. Converges from any state: an alias that the base certificate does not cover
    and that is not in ``hosts`` (a removed host, or a row the tenant erasure deleted) is detached."""
    dist_id = dist_id or distribution_id()
    base_arn = base_arn or base_certificate_arn()
    if not dist_id or not base_arn:
        raise EdgeError("edge_not_configured", "TP_WEB_DISTRIBUTION_ID / TP_WEB_BASE_CERT_ARN are not set")
    wanted = sorted({h.lower() for h in hosts})
    dist, etag = get_distribution(cf, dist_id)
    config = dist["DistributionConfig"]
    target = str(dist["DomainName"]).lower()
    deployed = dist.get("Status") == "Deployed"
    current_aliases = [a.lower() for a in config.get("Aliases", {}).get("Items", []) or []]
    viewer = config.get("ViewerCertificate", {})
    current_cert = viewer.get("ACMCertificateArn")
    base_names = certificate_names(describe(acm, base_arn))
    base_aliases = [a for a in current_aliases if covered(a, base_names)]
    desired_aliases = sorted(set(base_aliases) | set(wanted))

    if wanted:
        names = base_names + [h for h in wanted if not covered(h, base_names)]
        edge_arn, status = _edge_certificate(acm, names, current_cert, pending_arn, base_arn)
        if status == PENDING:
            return EdgeState("pending_cert", deployed, target, pending_arn=edge_arn)
        if status != ISSUED:
            raise EdgeError("edge_certificate_" + str(status).lower(),
                            f"the edge certificate is {status}: a host's validation record may be gone")
        target_cert = edge_arn
    else:
        target_cert = base_arn

    if sorted(set(current_aliases)) == desired_aliases and current_cert == target_cert:
        return EdgeState("in_sync", deployed, target)

    config["Aliases"] = {"Quantity": len(desired_aliases), "Items": desired_aliases}
    config["ViewerCertificate"] = {
        "CloudFrontDefaultCertificate": False,
        "ACMCertificateArn": target_cert,
        "SSLSupportMethod": viewer.get("SSLSupportMethod", "sni-only"),
        "MinimumProtocolVersion": viewer.get("MinimumProtocolVersion", "TLSv1.2_2021"),
    }
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
    retired = current_cert if current_cert not in (None, base_arn, target_cert) else None
    return EdgeState("updated", False, target, retired)
