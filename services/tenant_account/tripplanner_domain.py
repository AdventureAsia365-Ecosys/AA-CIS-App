"""AA-1090 TripPlanner R8 (root ADR 0019 b4) — the custom hosts of a tenant's TripPlanner site, edited on Tenant 360.

An agency's planner lives on our path /{slug} (ADR 0019 d10). R8 lets it serve the same site on its own host
(``trip.<agency domain>``). Staff add the host on Tenant 360 › Subscription (where the site's lead route is edited,
``tripplanner_site.py``); the page shows the two DNS records the agency creates; the CIS job
``tripplanner_custom_domain`` (``services/jobs/tripplanner_custom_domain_job.py``) does the AWS part (``tp_edge.py``):
an ACM certificate in us-east-1 with DNS validation, then the host as an alternate domain name of the TripPlanner
agency CloudFront distribution (a distribution of its own: the public TripPlanner host never shares a certificate with
an agency). TripPlanner serves an ``active`` host as the tenant through
``catalog.v_tenant_tripplanner_v1.custom_domains`` (CIS 429).

State (``shared.tenant_tripplanner_domain.status``, CIS 428) and the only moves :func:`transition` allows::

    pending ──requested──▶ validating ──issued──▶ attaching ──attached──▶ active
       │                      │                      │                      │
       └───────failed─────────┴────────failed────────┴───────failed─────────┘
    failed ──retry──▶ pending (requested_at = now, validated_at / active_at cleared: a full validation window)
    any but removing ──remove──▶ removing ──(job: alias detached, certificate deleted)──▶ row deleted
    removing ──remove again──▶ removing (a fresh job: a removal that could not finish, failure_code says why)

An active host fails only when its validation record is gone (``validation_record_gone``: the job's pending edge
certificate check or its daily renewal check detaches it so the other agencies keep a valid certificate).

Limits are settings (CIS 428, Platform › Settings): ``cis.tripplanner.custom_domain_max_per_tenant`` (per tenant,
hosts not being removed), ``cis.tripplanner.custom_domain_edge_max`` (all tenants, hosts that hold or will hold a
name on the edge certificate). The job reads the validation timeout, the poll interval, the edge pending window and
the renewal grace days.

Routes: ``api/routers/admin_tenant_subscription.py`` (GET / POST ``/admin/tenants/{id}/tripplanner-site/domains``,
DELETE ``.../domains/{domain_id}``, POST ``.../domains/{domain_id}/retry``). A refusal is :class:`DomainError`
(status 422 with the field, 404 / 409 otherwise). Every change is audited (``tenant.tripplanner_domain_*``).
"""
from __future__ import annotations

import ipaddress
import re
from typing import Any, Optional

from services.acp_shared.audit_log import AdminTenantAuditAction, write_audit_log
from services.tenant_account.tripplanner_site import host_is_internal_name
from shared import settings
from shared.jobs.registry import enqueue

KIND = "tripplanner_custom_domain"

STATUSES = ("pending", "validating", "attaching", "active", "removing", "failed")
# The hosts that hold, or are on their way to, a name on the edge certificate (the edge limit counts these).
ON_EDGE = ("pending", "validating", "attaching", "active")

# event -> {from status: to status}. "remove" is allowed from every status but removing (idempotent there).
_MOVES: dict[str, dict[str, str]] = {
    "requested": {"pending": "validating"},
    "issued": {"validating": "attaching"},
    "attached": {"attaching": "active"},
    "failed": {"pending": "failed", "validating": "failed", "attaching": "failed", "active": "failed"},
    "retry": {"failed": "pending"},
    "remove": {s: "removing" for s in STATUSES if s != "removing"},
}

MAX_PER_TENANT_KEY = "cis.tripplanner.custom_domain_max_per_tenant"
EDGE_MAX_KEY = "cis.tripplanner.custom_domain_edge_max"

DOMAIN_MAX = 253  # technical-constant: the DNS limit of a host name (RFC 1035), not a policy
# CIS 428 tenant_tripplanner_domain_domain_chk, matched in full.
_DOMAIN_SHAPE = re.compile(r"([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]([a-z0-9-]{0,61}[a-z0-9])?")
# Names that belong to the platform or to AWS: a tenant can never claim them (our own zone serves /{slug} already).
RESERVED_SUFFIXES = ("lumiguides.it.com", "adventure.asia", "cloudfront.net", "amazonaws.com", "amazon.com", "aws")

DOMAIN_INVALID = "domain_invalid"
DOMAIN_APEX = "domain_apex"
DOMAIN_RESERVED = "domain_reserved"
DOMAIN_TAKEN = "domain_taken"
MESSAGES = {
    DOMAIN_INVALID: "Enter one host name such as trip.agency-name.com (letters, digits and hyphens, dot-separated).",
    DOMAIN_APEX: "Use a sub-domain such as trip.agency-name.com: a CNAME record cannot sit on the bare domain.",
    DOMAIN_RESERVED: "This host belongs to the platform or to AWS and cannot be an agency's own domain.",
    DOMAIN_TAKEN: "This host is already added for an agency.",
    "domain_limit_reached": "This agency already has its maximum number of own domains. Remove one first.",
    "edge_full": "The trip planner serves its maximum number of own domains. Remove one, or raise the limit after "
                 "AWS raised the names-per-certificate quota.",
    "custom_domains_unavailable": "Own domains are not switched on in this environment yet.",
    "no_tripplanner": "This agency has no TripPlanner subscription.",
    "tenant_not_found": "No tenant with this id.",
    "tenant_offboarded": "Restore the tenant first.",
    "domain_not_found": "No such domain for this tenant.",
    "not_failed": "Only a failed domain can be retried.",
}


class DomainError(ValueError):
    """A change the editor refuses; ``code`` is stable, ``field`` the body field it is about (None: the request)."""

    def __init__(self, code: str, field: Optional[str] = None, status: int = 422, message: Optional[str] = None):
        super().__init__(code, field, status, message)
        self.code = code
        self.field = field
        self.status = status
        self.message = message or MESSAGES.get(code, code)


# ── pure parts ──────────────────────────────────────────────────────────────────────────────────────────────────

def transition(status: str, event: str) -> str:
    """The next status, or ValueError for a move the state machine does not allow."""
    moves = _MOVES.get(event)
    if moves is None:
        raise ValueError(f"unknown event {event!r}")
    if status not in moves:
        raise ValueError(f"{event!r} is not allowed from {status!r}")
    return moves[status]


def normalise(value: Any) -> str:
    """The host as stored: trimmed, lower-case, without a trailing root dot or a scheme / path a user pasted."""
    if not isinstance(value, str):
        raise DomainError(DOMAIN_INVALID, "domain")
    host = value.strip().lower()
    host = re.sub(r"^[a-z][a-z0-9+.-]*://", "", host)
    host = host.split("/", 1)[0].rstrip(".")
    return host


def domain_problem(host: str) -> Optional[str]:
    """None when ``host`` (already normalised) may be added, else the error code."""
    if not host or len(host) > DOMAIN_MAX or not _DOMAIN_SHAPE.fullmatch(host):
        return DOMAIN_INVALID
    try:
        ipaddress.ip_address(host)
        return DOMAIN_INVALID
    except ValueError:
        pass
    if host_is_internal_name(host):
        return DOMAIN_RESERVED
    if any(host == s or host.endswith("." + s) for s in RESERVED_SUFFIXES):
        return DOMAIN_RESERVED
    if host.count(".") < 2:
        return DOMAIN_APEX
    return None


def dns_records(row: dict) -> list[dict]:
    """The two records the agency creates, once the job knows them (the CNAME of the host, the ACM validation)."""
    out = []
    if row.get("cname_target"):
        out.append({"purpose": "route", "type": "CNAME", "name": row["domain"], "value": row["cname_target"]})
    if row.get("validation_name") and row.get("validation_value"):
        out.append({"purpose": "certificate", "type": "CNAME", "name": row["validation_name"],
                    "value": row["validation_value"]})
    return out


def _iso(v: Any) -> Optional[str]:
    return v.isoformat() if v is not None else None


def shape(row: Any) -> dict:
    d = dict(row)
    return {
        "domain_id": str(d["domain_id"]), "domain": d["domain"], "status": d["status"],
        "dns_records": dns_records(d), "failure_code": d.get("failure_code"),
        "failure_message": d.get("failure_message"), "requested_at": _iso(d.get("requested_at")),
        "validated_at": _iso(d.get("validated_at")), "active_at": _iso(d.get("active_at")),
        "updated_at": _iso(d.get("updated_at")), "updated_by": d.get("updated_by"),
    }


# ── database ────────────────────────────────────────────────────────────────────────────────────────────────────

COLUMNS = ("domain_id, tenant_id, domain, status, certificate_arn, validation_name, validation_value, cname_target, "
           "failure_code, failure_message, requested_at, validated_at, active_at, updated_at, updated_by")
TENANT_LOCK_SQL = "SELECT account_status FROM shared.tenants WHERE tenant_id = $1::uuid FOR UPDATE"
TENANT_SQL = "SELECT account_status FROM shared.tenants WHERE tenant_id = $1::uuid"
HAS_TRIPPLANNER_SQL = """
    SELECT EXISTS (SELECT 1 FROM shared.tenant_subscription
                    WHERE tenant_id = $1::uuid AND product = 'tripplanner' AND status <> 'canceled') AS has_tripplanner
"""
LIST_SQL = f"SELECT {COLUMNS} FROM shared.tenant_tripplanner_domain WHERE tenant_id = $1::uuid ORDER BY domain"
ROW_SQL = f"SELECT {COLUMNS} FROM shared.tenant_tripplanner_domain WHERE domain_id = $1::uuid AND tenant_id = $2::uuid"
TENANT_COUNT_SQL = """
    SELECT count(*) AS n FROM shared.tenant_tripplanner_domain WHERE tenant_id = $1::uuid AND status <> 'removing'
"""
EDGE_COUNT_SQL = "SELECT count(*) AS n FROM shared.tenant_tripplanner_domain WHERE status = ANY($1::text[])"
# The edge count is checked under one advisory lock so two tenants adding at once cannot both take the last place.
EDGE_LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtext('shared.tenant_tripplanner_domain:edge'))"
TAKEN_SQL = "SELECT EXISTS (SELECT 1 FROM shared.tenant_tripplanner_domain WHERE domain = $1) AS taken"
INSERT_SQL = f"""
    INSERT INTO shared.tenant_tripplanner_domain (tenant_id, domain, updated_by) VALUES ($1::uuid, $2, $3)
    RETURNING {COLUMNS}
"""
STATUS_SQL = f"""
    UPDATE shared.tenant_tripplanner_domain
       SET status = $3, failure_code = NULL, failure_message = NULL, updated_at = now(), updated_by = $4
     WHERE domain_id = $1::uuid AND tenant_id = $2::uuid
    RETURNING {COLUMNS}
"""
# A retry starts over: a full validation window from now (the job's timeout measures from requested_at) and no
# timestamps of the earlier attempt. The certificate stays: the job reuses it while ACM can still issue it.
RETRY_SQL = f"""
    UPDATE shared.tenant_tripplanner_domain
       SET status = 'pending', failure_code = NULL, failure_message = NULL, requested_at = now(), validated_at = NULL,
           active_at = NULL, updated_at = now(), updated_by = $3
     WHERE domain_id = $1::uuid AND tenant_id = $2::uuid AND status = 'failed'
    RETURNING {COLUMNS}
"""


def edge_configured() -> bool:
    """The agency distribution is known (AA-CIS-Infra sets TP_AGENCY_DISTRIBUTION_ID on api and worker)."""
    from services.tenant_account import tp_edge

    return tp_edge.configured()


async def list_domains(conn, tenant_id: str) -> Optional[dict]:
    """The tenant's hosts with their DNS records, or None for no tenant."""
    if await conn.fetchval(TENANT_SQL, tenant_id) is None:
        return None
    has_tp = bool(await conn.fetchval(HAS_TRIPPLANNER_SQL, tenant_id))
    rows = await conn.fetch(LIST_SQL, tenant_id)
    max_per_tenant = int(await settings.aget(MAX_PER_TENANT_KEY, tenant_id))
    return {"tenant_id": tenant_id, "has_tripplanner": has_tp, "available": edge_configured(),
            "max_domains": max_per_tenant, "domains": [shape(r) for r in rows]}


async def _locked_tenant(conn, tenant_id: str) -> None:
    status = await conn.fetchval(TENANT_LOCK_SQL, tenant_id)
    if status is None:
        raise DomainError("tenant_not_found", status=404)
    if status in ("offboarding", "closed"):
        raise DomainError("tenant_offboarded", status=409)


async def _audit(conn, tenant_id: str, actor: str, action: str, details: dict) -> None:
    await write_audit_log(conn, tenant_id=tenant_id, actor=actor, action=action, resource_type="tenant",
                          resource_id=tenant_id, details=details)


async def _enqueue(conn, domain_id: str, step: str, actor: str) -> str:
    job_id, _ = await enqueue(conn, KIND, {"domain_id": domain_id}, idempotency_key=f"{KIND}:{domain_id}:{step}",
                              created_by=actor[:64])
    return job_id


async def add_domain(conn, tenant_id: str, *, domain: Any, actor: str) -> dict:
    """Validate and add a host (status pending), audited, and queue its job. Limits are settings."""
    host = normalise(domain)
    if (code := domain_problem(host)) is not None:
        raise DomainError(code, "domain")
    if not edge_configured():
        raise DomainError("custom_domains_unavailable", status=409)
    async with conn.transaction():
        await conn.execute("SELECT set_config('aa.actor', $1, true)", actor)
        await _locked_tenant(conn, tenant_id)
        if not await conn.fetchval(HAS_TRIPPLANNER_SQL, tenant_id):
            raise DomainError("no_tripplanner", status=409)
        if await conn.fetchval(TAKEN_SQL, host):
            raise DomainError(DOMAIN_TAKEN, "domain", status=409)
        max_per_tenant = int(await settings.aget(MAX_PER_TENANT_KEY, tenant_id))
        if int(await conn.fetchval(TENANT_COUNT_SQL, tenant_id)) >= max_per_tenant:
            raise DomainError("domain_limit_reached", status=409)
        await conn.execute(EDGE_LOCK_SQL)
        if int(await conn.fetchval(EDGE_COUNT_SQL, list(ON_EDGE))) >= int(await settings.aget(EDGE_MAX_KEY)):
            raise DomainError("edge_full", status=409)
        row = await conn.fetchrow(INSERT_SQL, tenant_id, host, actor[:64])
        domain_id = str(row["domain_id"])
        job_id = await _enqueue(conn, domain_id, "add", actor)
        await _audit(conn, tenant_id, actor, AdminTenantAuditAction.TRIPPLANNER_DOMAIN_ADDED,
                     {"domain_id": domain_id, "domain": host, "job_id": job_id})
    return {**shape(row), "job_id": job_id}


def _step_key(step: str, row: Any) -> str:
    """A key per editor action on a row that already had one (retry, removal again): the row's new updated_at."""
    return f"{step}:{int(row['updated_at'].timestamp() * 1000)}"


async def remove_domain(conn, tenant_id: str, domain_id: str, *, actor: str) -> dict:
    """Mark the host removing and queue the job that detaches it. Removing it again (a removal that could not finish,
    or one whose job ended) queues a fresh job: staff can always drive a removal to its end."""
    async with conn.transaction():
        await conn.execute("SELECT set_config('aa.actor', $1, true)", actor)
        await _locked_tenant(conn, tenant_id)
        row = await conn.fetchrow(ROW_SQL + " FOR UPDATE", domain_id, tenant_id)
        if row is None:
            raise DomainError("domain_not_found", status=404)
        again = row["status"] == "removing"
        status = "removing" if again else transition(row["status"], "remove")
        new = await conn.fetchrow(STATUS_SQL, domain_id, tenant_id, status, actor[:64])
        job_id = await _enqueue(conn, domain_id, _step_key("remove", new) if again else "remove", actor)
        details = {"domain_id": domain_id, "domain": row["domain"], "before": row["status"], "job_id": job_id}
        if again:
            details["requeued"] = True
            details["failure_code"] = row["failure_code"]
        await _audit(conn, tenant_id, actor, AdminTenantAuditAction.TRIPPLANNER_DOMAIN_REMOVED, details)
    return {**shape(new), "job_id": job_id}


async def retry_domain(conn, tenant_id: str, domain_id: str, *, actor: str) -> dict:
    """A failed host goes back to pending (its failure cleared) and gets a new job."""
    if not edge_configured():
        raise DomainError("custom_domains_unavailable", status=409)
    async with conn.transaction():
        await conn.execute("SELECT set_config('aa.actor', $1, true)", actor)
        await _locked_tenant(conn, tenant_id)
        row = await conn.fetchrow(ROW_SQL + " FOR UPDATE", domain_id, tenant_id)
        if row is None:
            raise DomainError("domain_not_found", status=404)
        if row["status"] != "failed":
            raise DomainError("not_failed", status=409)
        await conn.execute(EDGE_LOCK_SQL)
        if int(await conn.fetchval(EDGE_COUNT_SQL, list(ON_EDGE))) >= int(await settings.aget(EDGE_MAX_KEY)):
            raise DomainError("edge_full", status=409)
        transition("failed", "retry")
        new = await conn.fetchrow(RETRY_SQL, domain_id, tenant_id, actor[:64])
        job_id = await _enqueue(conn, domain_id, _step_key("retry", new), actor)
        await _audit(conn, tenant_id, actor, AdminTenantAuditAction.TRIPPLANNER_DOMAIN_RETRIED,
                     {"domain_id": domain_id, "domain": row["domain"], "failure_code": row["failure_code"],
                      "job_id": job_id})
    return {**shape(new), "job_id": job_id}
