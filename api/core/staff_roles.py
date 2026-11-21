"""AA-1155 (spec tasks T8 / T9 / T10, root ADR 0006a A14) — custom staff roles as data.

Built-in roles stay in code (:data:`api.core.staff_authz.ROLE_PERMS`); CIS 462 holds only the CIS custom roles
(``shared.staff_role`` + ``shared.staff_role_capability``) and Booking bk-062 the Booking ones (read here through the
key-only view ``booking.v_staff_role_capability``, granted to ``cis_app``).

- A holder of the WORKFORCE group ``cis-custom`` has ``shared.admin_users.custom_role`` = the role's code. Its
  capabilities are the role's rows, kept only when the code knows them (:func:`custom_capabilities`): never an
  approve-class capability, never ``staff.write`` / ``staff.admin``, never a name ``staff_authz.CAPABILITIES`` does
  not hold. ``admin_users.role`` keeps the role's ``based_on`` (NOT NULL column, never admin / viewer / reviewer);
  authorisation never falls back to it.
- Fail closed for custom holders only (R5.1): no custom role on the row, an unknown or archived role, a role whose
  capabilities cannot be read or are empty -> the holder is refused (api/core/staff_identity.py). Built-in holders
  never read these tables, so an outage never locks out an administrator.
- A custom role code matches ``^custom_[a-z][a-z0-9_]{1,46}$`` in both apps (CIS 462 / bk-062 CHECKs), so it never
  equals a built-in role and a Booking custom code fits ``shared.staff_app_grant.role``.
- The seed of CIS 462, ``custom_content`` "Content" = ``content_lead`` minus ``content.settings.write`` plus
  ``tenant.read`` (tech lead Q7): :func:`content_seed_capabilities`, which a unit test compares with the migration.

The roles catalogue (:func:`roles_catalogue`) feeds the Staff page pickers and the read-only "Roles & permissions"
page: CIS built-in roles with their capabilities, CIS custom roles, Booking built-in role names (their capabilities
live in the Booking code), Booking custom roles from the bk-062 view, the three CRM staff groups as static rows and
TripPlanner as "no staff roles". No person, no E2E identity, no issue id.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Optional

import asyncpg
import structlog

logger = structlog.get_logger()

CUSTOM_CODE_RE = re.compile(r"^custom_[a-z][a-z0-9_]{1,46}$")
# The WORKFORCE groups of a custom holder (AA-CIS-Infra identity.tf, A14): they grant nothing without a role here.
CIS_CUSTOM_GROUP = "cis-custom"
BOOKING_CUSTOM_GROUP = "booking-custom"

# The approve class (CIS 462 staff_role_capability_not_approve_chk): reserved for built-in roles.
_APPROVE_RE = re.compile(r"\.approve(_[a-z0-9_]+)?$")
RESERVED_CAPABILITIES: frozenset[str] = frozenset({"staff.write", "staff.admin"})

# The seeded CIS custom role (CIS 462, Q7).
CONTENT_ROLE_CODE = "custom_content"
CONTENT_ROLE_LABEL = "Content"
CONTENT_ROLE_BASED_ON = "content_lead"

# Why a custom holder is refused (logged, never shown: the caller sees one 403 detail).
STATE_OK = "ok"
STATE_NO_ROLE = "no_custom_role"
STATE_UNKNOWN = "unknown_role"
STATE_ARCHIVED = "archived_role"
STATE_EMPTY = "no_capabilities"
STATE_UNREADABLE = "unreadable"

CUSTOM_ROLE_REFUSED = "Your custom staff role cannot be read; ask an owner to check it on the Staff page"

# English names of the CIS built-in roles (frontend/lib/rbac.ts ROLE_LABEL; the test of this module keeps them equal).
CIS_ROLE_LABELS = MappingProxyType({
    "admin": "Administrator",
    "content_lead": "Content Lead",
    "content": "Content Team",
    "tenant_qa": "Tenant Content QA",
    "customer_success": "Customer Success",
    "support": "Support",
    "finance": "Finance",
    "ai_ops": "AI Operations",
    "viewer": "Viewer (read-only)",
})
# The Booking built-in roles the Staff page assigns (api/services/staff_directory.BOOKING_ROLES) and their names (the
# Staff page's BOOKING_ROLE messages). Their capabilities are AA-Booking app/auth/staff.py PERMISSIONS.
BOOKING_ROLE_LABELS = MappingProxyType({
    "admin": "Administrator",
    "sales": "Sales",
    "contracting": "Product & operations",
    "finance": "Finance",
})
# The CRM staff groups (Q11: no CRM role editor; static rows). api/services/staff_directory.CRM_ROLE_GROUPS.
CRM_GROUP_LABELS = MappingProxyType({
    "platform_admin": "Agency admin",
    "ops": "Operations",
    "support": "Support",
})

SQL_CIS_ROLE = (
    "SELECT r.code, r.label, r.based_on::text AS based_on, r.archived, "
    "coalesce(array_agg(c.capability ORDER BY c.capability) FILTER (WHERE c.capability IS NOT NULL), "
    "ARRAY[]::text[]) AS capabilities "
    "FROM shared.staff_role r LEFT JOIN shared.staff_role_capability c ON c.role_code = r.code "
    "WHERE r.code = $1 GROUP BY r.code, r.label, r.based_on, r.archived"
)
SQL_CIS_ROLES = (
    "SELECT r.code, r.label, r.based_on::text AS based_on, r.archived, "
    "coalesce(array_agg(c.capability ORDER BY c.capability) FILTER (WHERE c.capability IS NOT NULL), "
    "ARRAY[]::text[]) AS capabilities "
    "FROM shared.staff_role r LEFT JOIN shared.staff_role_capability c ON c.role_code = r.code "
    "GROUP BY r.code, r.label, r.based_on, r.archived ORDER BY r.archived, r.label, r.code"
)
SQL_BOOKING_ROLES = (
    "SELECT role_code, label, based_on, archived, capability FROM booking.v_staff_role_capability "
    "ORDER BY role_code, capability"
)
SQL_BOOKING_ROLE = (
    "SELECT role_code, label, based_on, archived, capability FROM booking.v_staff_role_capability "
    "WHERE role_code = $1 ORDER BY capability"
)

# What a missing table / view / grant raises (CIS 462 or bk-062 not applied, a revoked grant): the reader answers
# "unavailable", never a 500.
_UNAVAILABLE = (asyncpg.UndefinedTableError, asyncpg.InsufficientPrivilegeError, asyncpg.UndefinedColumnError,
                asyncpg.InvalidSchemaNameError)


def is_custom_code(code: Any) -> bool:
    """True for a custom role code (``custom_<name>``), in either app."""
    return isinstance(code, str) and bool(CUSTOM_CODE_RE.match(code))


def is_approve_class(capability: str) -> bool:
    """The approve class a custom role may never hold: any ``*.approve`` / ``*.approve_<x>``, ``staff.write``,
    ``staff.admin`` (CIS 462 CHECK, the same names)."""
    return bool(_APPROVE_RE.search(capability)) or capability in RESERVED_CAPABILITIES


def custom_allowed() -> frozenset[str]:
    """Every capability a CIS custom role may hold: the code's capability set minus the owner-only and approve class."""
    from api.core.staff_authz import CAPABILITIES, OWNER_PERMS

    return frozenset(c for c in CAPABILITIES - OWNER_PERMS if not is_approve_class(c))


def custom_capabilities(raw: Any, *, role_code: Optional[str] = None) -> frozenset[str]:
    """The capabilities of a custom role's rows that the code knows and allows (unknown or approve-class names are
    dropped and logged: the table CHECK refuses the approve class already, an unknown name gates nothing)."""
    allowed = custom_allowed()
    names = {c for c in (raw or ()) if isinstance(c, str)}
    dropped = sorted(names - allowed)
    if dropped:
        logger.warning("staff_custom_role_capabilities_dropped", role=role_code, dropped=dropped)
    return frozenset(names & allowed)


def content_seed_capabilities() -> frozenset[str]:
    """CIS 462's ``custom_content`` (Q7): ``content_lead`` minus ``content.settings.write``, plus ``tenant.read``."""
    from api.core.staff_authz import CONTENT_SETTINGS_WRITE, ROLE_PERMS, TENANT_READ

    return (ROLE_PERMS[CONTENT_ROLE_BASED_ON] - {CONTENT_SETTINGS_WRITE}) | {TENANT_READ}


@dataclass(frozen=True)
class CustomRole:
    """One custom role (either app) as the readers return it."""
    code: str
    label: str
    based_on: str
    archived: bool
    capabilities: frozenset[str] = field(default_factory=frozenset)

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "label": self.label, "based_on": self.based_on, "archived": self.archived,
                "capabilities": sorted(self.capabilities)}


@dataclass(frozen=True)
class CustomGrant:
    """The custom role a holder resolves to (``state`` ok) or why it does not."""
    state: str
    role: Optional[CustomRole] = None

    @property
    def ok(self) -> bool:
        return self.state == STATE_OK and self.role is not None


def _cis_role(record: Any) -> CustomRole:
    code = record["code"]
    return CustomRole(code=code, label=record["label"], based_on=record["based_on"], archived=bool(record["archived"]),
                      capabilities=custom_capabilities(record["capabilities"], role_code=code))


async def load_cis_grant(conn: Any, custom_role: Optional[str]) -> CustomGrant:
    """The CIS custom role ``custom_role`` (a row's ``admin_users.custom_role``), fail closed: no role, an unknown or
    archived one, no usable capability, or a read error -> a refused grant (never raises)."""
    if not custom_role:
        return CustomGrant(STATE_NO_ROLE)
    try:
        record = await conn.fetchrow(SQL_CIS_ROLE, custom_role)
    except Exception as exc:  # noqa: BLE001 - any read failure refuses the custom holder, never anyone else
        logger.error("staff_custom_role_unreadable", role=custom_role, error=type(exc).__name__)
        return CustomGrant(STATE_UNREADABLE)
    if record is None:
        return CustomGrant(STATE_UNKNOWN)
    role = _cis_role(record)
    if role.archived:
        return CustomGrant(STATE_ARCHIVED, role)
    if not role.capabilities:
        return CustomGrant(STATE_EMPTY, role)
    return CustomGrant(STATE_OK, role)


async def list_cis_roles(conn: Any) -> Optional[list[CustomRole]]:
    """Every CIS custom role (archived last), or None when CIS 462 is not readable here."""
    try:
        rows = await conn.fetch(SQL_CIS_ROLES)
    except _UNAVAILABLE as exc:
        logger.warning("staff_custom_roles_unavailable", app="cis", error=type(exc).__name__)
        return None
    return [_cis_role(r) for r in rows]


def _booking_roles(rows: list[Any]) -> list[CustomRole]:
    grouped: dict[str, dict[str, Any]] = {}
    for r in rows:
        entry = grouped.setdefault(r["role_code"], {"label": r["label"], "based_on": r["based_on"],
                                                    "archived": bool(r["archived"]), "caps": set()})
        if r["capability"]:
            entry["caps"].add(r["capability"])
    return [CustomRole(code=code, label=e["label"], based_on=e["based_on"], archived=e["archived"],
                       capabilities=frozenset(e["caps"]))
            for code, e in sorted(grouped.items(), key=lambda kv: (kv[1]["archived"], kv[1]["label"], kv[0]))]


async def list_booking_roles(conn: Any) -> Optional[list[CustomRole]]:
    """Every Booking custom role (bk-062 view, ``cis_app`` reads it), or None when the view is not readable here."""
    try:
        rows = await conn.fetch(SQL_BOOKING_ROLES)
    except _UNAVAILABLE as exc:
        logger.warning("staff_custom_roles_unavailable", app="booking", error=type(exc).__name__)
        return None
    return _booking_roles(list(rows))


async def load_booking_role(conn: Any, code: str) -> CustomGrant:
    """One Booking custom role for the Staff page write (unknown, archived or unreadable -> refused)."""
    try:
        rows = await conn.fetch(SQL_BOOKING_ROLE, code)
    except _UNAVAILABLE as exc:
        logger.warning("staff_custom_roles_unavailable", app="booking", error=type(exc).__name__)
        return CustomGrant(STATE_UNREADABLE)
    roles = _booking_roles(list(rows))
    if not roles:
        return CustomGrant(STATE_UNKNOWN)
    if roles[0].archived:
        return CustomGrant(STATE_ARCHIVED, roles[0])
    return CustomGrant(STATE_OK, roles[0])


def cis_builtin_roles() -> list[dict[str, Any]]:
    """The CIS built-in roles with their capabilities (code order of ROLE_PERMS)."""
    from api.core.staff_authz import ROLE_PERMS

    return [{"code": code, "label": CIS_ROLE_LABELS.get(code, code), "capabilities": sorted(perms),
             "assignable": code != "viewer"}
            for code, perms in ROLE_PERMS.items()]


async def roles_catalogue(conn: Any) -> dict[str, Any]:
    """Every app's staff roles for the Staff page pickers and the read-only "Roles & permissions" page.

    Generated from code (built-in roles) and the tables (custom roles). ``available`` is False for a custom list that
    cannot be read here (the page says so; the built-in rows still show)."""
    cis_custom = await list_cis_roles(conn)
    booking_custom = await list_booking_roles(conn)
    return {
        "cis": {
            "builtin": cis_builtin_roles(),
            "custom": [r.as_dict() for r in cis_custom or []],
            "custom_available": cis_custom is not None,
        },
        "booking": {
            "builtin": [{"code": code, "label": label} for code, label in BOOKING_ROLE_LABELS.items()],
            "custom": [r.as_dict() for r in booking_custom or []],
            "custom_available": booking_custom is not None,
        },
        "crm": {"groups": [{"code": code, "label": label} for code, label in CRM_GROUP_LABELS.items()]},
        "tripplanner": {"staff_roles": False},
    }
