"""AA-1038 R5 slice 3 follow-up (S272) — the lead route of a tenant's TripPlanner site, edited on Tenant 360.

CIS 390 gave ``shared.tenant_tripplanner_site`` the lead route of a tenant WITHOUT the CRM add-on: ``lead_channel``
(none | webhook | email), ``lead_webhook_url`` and ``lead_email``. Its CHECK only asks for ``https://`` and a host of
``[A-Za-z0-9.-]``, so ``https://127.0.0.1/hook`` or ``https://10.0.3.7:8443/leads`` could be stored and TripPlanner
would then refuse every lead of that tenant at send time (``webhook_target_problem``: 503, nothing stored). The editor
here refuses such a target when it is written:

* :func:`webhook_url_problem` — not https, not the CHECK's shape, longer than 500 characters, an IP literal that is not
  a global unicast address (private, loopback, link-local, CGNAT, multicast, unspecified, reserved; IPv4 and IPv6,
  IPv4-mapped / NAT64 / 6to4 / IPv4-compatible IPv6 unwrapped, the legacy IPv4 spellings ``127.1``, ``2130706433``,
  ``0x7f.0.0.1`` included) or a host name that is plainly internal (one label, ``localhost``, ``*.local``,
  ``*.internal`` ...). No DNS lookup here: a name may resolve differently later, so TripPlanner checks the resolved
  addresses again before it stores a lead and before each POST (``public_address_check``).
* :func:`email_problem` — the CHECK's shape and length.
* :func:`lead_route` / :func:`set_lead_route` — read and write the three columns (the row is created with the table's
  defaults when the tenant has none yet), audited ``tenant.tripplanner_lead_route_changed`` (before / after; the URL
  and the address are the agency's business targets, never a traveller's).

Routes: ``api/routers/admin_tenant_subscription.py`` (GET / PUT ``/admin/tenants/{id}/tripplanner-site/lead-route``).
A refusal is :class:`LeadRouteError` (status 422 with the field it is about; 404 / 409 for the tenant).
"""
from __future__ import annotations

import ipaddress
import json
import re
import socket
import urllib.parse
from typing import Any, Optional

from services.acp_shared.audit_log import AdminTenantAuditAction, write_audit_log

LEAD_ROUTE_CHANGED = AdminTenantAuditAction.TRIPPLANNER_LEAD_ROUTE_CHANGED
CHANNELS = ("none", "webhook", "email")

WEBHOOK_URL_MAX = 500
EMAIL_MAX = 254
# The CIS 390 CHECKs (tenant_tripplanner_site_lead_webhook_chk / _lead_email_chk), matched in full (fullmatch: a
# trailing newline is not a match, as in Postgres).
_WEBHOOK_SHAPE = re.compile(r"https://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[^\s]*)?")
_EMAIL_SHAPE = re.compile(r"[^@\s]{1,64}@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,63}")

# Host names that only ever mean "inside some network": RFC 6761 / 6762 / 8375, the ICANN private-use .internal and
# the names corporate resolvers answer for. Matched as the whole name or its last labels.
INTERNAL_SUFFIXES = ("localhost", "local", "internal", "localdomain", "home.arpa", "lan", "intranet", "corp", "home")
# Every label numeric (decimal, octal or hex): a host the resolver library reads as an IPv4 address (inet_aton).
_LEGACY_IPV4 = re.compile(r"(0x[0-9a-f]*|[0-9]+)(\.(0x[0-9a-f]*|[0-9]+)){0,3}")
_NAT64 = ipaddress.IPv6Network("64:ff9b::/96")

# Error codes (the ``detail`` of a 422) and the field each is about.
URL_REQUIRED = "lead_webhook_url_required"
URL_TOO_LONG = "lead_webhook_url_too_long"
URL_NOT_HTTPS = "lead_webhook_url_not_https"
URL_INVALID = "lead_webhook_url_invalid"
URL_NOT_PUBLIC = "lead_webhook_url_not_public"
EMAIL_REQUIRED = "lead_email_required"
EMAIL_INVALID = "lead_email_invalid"
MESSAGES = {
    URL_REQUIRED: "A webhook route needs the agency's https URL.",
    URL_TOO_LONG: f"The webhook URL is longer than {WEBHOOK_URL_MAX} characters.",
    URL_NOT_HTTPS: "The webhook URL must start with https://.",
    URL_INVALID: "The webhook URL must be https://<host name>[:port][/path] with a public host name "
                 "(letters, digits, dots and hyphens).",
    URL_NOT_PUBLIC: "The webhook URL points to a private or internal address (a private, loopback, link-local or "
                    "reserved IP, or an internal host name). Use the agency's public https endpoint.",
    EMAIL_REQUIRED: "An e-mail route needs the agency's address.",
    EMAIL_INVALID: f"Enter one e-mail address of the agency (at most {EMAIL_MAX} characters).",
}


class LeadRouteError(ValueError):
    """A route the editor refuses; ``code`` is stable, ``field`` the body field it is about (None: the tenant)."""

    def __init__(self, code: str, field: Optional[str] = None, status: int = 422, message: Optional[str] = None):
        super().__init__(code, field, status, message)
        self.code = code
        self.field = field
        self.status = status
        self.message = message or MESSAGES.get(code, code)


# ── pure checks ─────────────────────────────────────────────────────────────────────────────────────────────────

def ip_is_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True only for a global unicast address. An IPv6 address that carries an IPv4 one (IPv4-mapped, NAT64
    64:ff9b::/96, 6to4, IPv4-compatible ::a.b.c.d) is judged by that IPv4 address, as TripPlanner's
    ``public_address_check`` does for the mapped form."""
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        elif ip in _NAT64:
            ip = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        elif ip.sixtofour is not None:
            ip = ip.sixtofour
        elif int(ip) >> 32 == 0 and int(ip) > 1:  # IPv4-compatible ::a.b.c.d (deprecated; :: and ::1 stay IPv6)
            ip = ipaddress.IPv4Address(int(ip))
    return bool(ip.is_global) and not ip.is_multicast


def host_ip(host: str) -> Optional[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """The address a host spells, without any lookup: an IPv4 / IPv6 literal (an IPv6 zone dropped), or a legacy
    IPv4 spelling the resolver library accepts (``127.1``, ``2130706433``, ``0x7f.0.0.1``, ``0177.0.0.1``). None for a
    host name. ValueError for an all-numeric host no resolver reads as an address (``999.1.1.1``)."""
    try:
        return ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        pass
    if _LEGACY_IPV4.fullmatch(host):
        try:
            return ipaddress.IPv4Address(socket.inet_aton(host))  # parses only; never a DNS query
        except OSError:
            raise ValueError("not an address") from None
    return None


def host_is_internal_name(host: str) -> bool:
    """A host name that cannot be a public internet name: one label (resolved through a search domain), or
    ``localhost`` / ``*.local`` / ``*.internal`` / the other :data:`INTERNAL_SUFFIXES`."""
    name = host.rstrip(".").lower()
    if "." not in name:
        return True
    return any(name == s or name.endswith("." + s) for s in INTERNAL_SUFFIXES)


def webhook_url_problem(url: str) -> Optional[str]:
    """None when ``url`` may be stored as a tenant's lead webhook, else the error code. Order: length, https, the
    host (an IP literal must be public, a name must not be internal), then the CIS 390 CHECK shape."""
    if len(url) > WEBHOOK_URL_MAX:
        return URL_TOO_LONG
    if not url.startswith("https://"):
        return URL_NOT_HTTPS
    try:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname or ""
        parts.port  # noqa: B018 — raises ValueError on a port out of range
    except ValueError:
        return URL_INVALID
    if not host:
        return URL_INVALID
    # A trailing root dot ("127.0.0.1.", "10.0.0.1..") is still the same address to getaddrinfo and to WHATWG URL
    # parsers, so every host check below reads the name without it.
    host = host.rstrip(".")
    if not host:
        return URL_INVALID
    try:
        ip = host_ip(host)
    except ValueError:
        return URL_INVALID
    if ip is not None:
        if not ip_is_public(ip):
            return URL_NOT_PUBLIC
    elif host_is_internal_name(host):
        return URL_NOT_PUBLIC
    elif host.rsplit(".", 1)[-1].isdigit():
        return URL_INVALID  # a name whose last label is a number is read as an address by URL parsers
    if not _WEBHOOK_SHAPE.fullmatch(url) or parts.username is not None or parts.password is not None:
        return URL_INVALID  # incl. a public IPv6 literal: the CHECK takes [A-Za-z0-9.-] hosts only
    return None


def email_problem(email: str) -> Optional[str]:
    if len(email) > EMAIL_MAX or not _EMAIL_SHAPE.fullmatch(email):
        return EMAIL_INVALID
    return None


def _clean(v: Any) -> Optional[str]:
    if v is None:
        return None
    if not isinstance(v, str):
        raise LeadRouteError("invalid_value")
    v = v.strip()
    return v or None


def validate_route(channel: Any, webhook_url: Any, email: Any) -> tuple[str, Optional[str], Optional[str]]:
    """The values to store, or :class:`LeadRouteError`. A target is checked whenever it is given, also when the
    channel does not use it (it is stored and becomes live the moment the channel is switched)."""
    if channel not in CHANNELS:
        raise LeadRouteError("lead_channel_invalid", "lead_channel", message="lead_channel is none, webhook or email.")
    try:
        url = _clean(webhook_url)
    except LeadRouteError:
        raise LeadRouteError(URL_INVALID, "lead_webhook_url") from None
    try:
        mail = _clean(email)
    except LeadRouteError:
        raise LeadRouteError(EMAIL_INVALID, "lead_email") from None
    if url is not None and (code := webhook_url_problem(url)):
        raise LeadRouteError(code, "lead_webhook_url")
    if mail is not None and (code := email_problem(mail)):
        raise LeadRouteError(code, "lead_email")
    if channel == "webhook" and url is None:
        raise LeadRouteError(URL_REQUIRED, "lead_webhook_url")
    if channel == "email" and mail is None:
        raise LeadRouteError(EMAIL_REQUIRED, "lead_email")
    return channel, url, mail


# ── database ────────────────────────────────────────────────────────────────────────────────────────────────────

TENANT_SQL = "SELECT account_status FROM shared.tenants WHERE tenant_id = $1::uuid"
TENANT_LOCK_SQL = TENANT_SQL + " FOR UPDATE"
HAS_TRIPPLANNER_SQL = """
    SELECT EXISTS (SELECT 1 FROM shared.tenant_subscription
                    WHERE tenant_id = $1::uuid AND product = 'tripplanner' AND status <> 'canceled') AS has_tripplanner
"""
ROUTE_SQL = """
    SELECT lead_channel, lead_webhook_url, lead_email, updated_at, updated_by
      FROM shared.tenant_tripplanner_site WHERE tenant_id = $1::uuid
"""
UPSERT_SQL = """
    INSERT INTO shared.tenant_tripplanner_site (tenant_id, lead_channel, lead_webhook_url, lead_email, updated_by)
    VALUES ($1::uuid, $2, $3, $4, $5)
    ON CONFLICT (tenant_id) DO UPDATE
       SET lead_channel = EXCLUDED.lead_channel, lead_webhook_url = EXCLUDED.lead_webhook_url,
           lead_email = EXCLUDED.lead_email, updated_by = EXCLUDED.updated_by, updated_at = now()
"""


def _route(row: Any) -> dict:
    if row is None:
        return {"site": False, "lead_channel": "none", "lead_webhook_url": None, "lead_email": None,
                "updated_at": None, "updated_by": None}
    at = row["updated_at"]
    return {"site": True, "lead_channel": row["lead_channel"], "lead_webhook_url": row["lead_webhook_url"],
            "lead_email": row["lead_email"], "updated_at": at.isoformat() if at is not None else None,
            "updated_by": row["updated_by"]}


async def lead_route(conn, tenant_id: str) -> Optional[dict]:
    """The tenant's stored lead route (``site`` false = no site row yet: channel none), or None for no tenant."""
    if await conn.fetchval(TENANT_SQL, tenant_id) is None:
        return None
    has_tp = bool(await conn.fetchval(HAS_TRIPPLANNER_SQL, tenant_id))
    return {"tenant_id": tenant_id, "has_tripplanner": has_tp, **_route(await conn.fetchrow(ROUTE_SQL, tenant_id))}


async def set_lead_route(conn, tenant_id: str, *, channel: Any, webhook_url: Any, email: Any, actor: str) -> dict:
    """Validate and store the route (creating the site row with its defaults when absent); audited."""
    channel, url, mail = validate_route(channel, webhook_url, email)
    async with conn.transaction():
        await conn.execute("SELECT set_config('aa.actor', $1, true)", actor)
        status = await conn.fetchval(TENANT_LOCK_SQL, tenant_id)
        if status is None:
            raise LeadRouteError("tenant_not_found", status=404, message="no tenant with this id")
        if status in ("offboarding", "closed"):
            raise LeadRouteError("tenant_offboarded", status=409, message="restore the tenant first")
        before = _route(await conn.fetchrow(ROUTE_SQL + " FOR UPDATE", tenant_id))
        await conn.execute(UPSERT_SQL, tenant_id, channel, url, mail, actor[:64])
        after = _route(await conn.fetchrow(ROUTE_SQL, tenant_id))
        keys = ("lead_channel", "lead_webhook_url", "lead_email")
        await write_audit_log(conn, tenant_id=tenant_id, actor=actor, action=LEAD_ROUTE_CHANGED,
                              resource_type="tenant", resource_id=tenant_id,
                              details=json.loads(json.dumps({"before": {k: before[k] for k in keys},
                                                             "after": {k: after[k] for k in keys}})))
    has_tp = bool(await conn.fetchval(HAS_TRIPPLANNER_SQL, tenant_id))
    return {"tenant_id": tenant_id, "has_tripplanner": has_tp, **after}
