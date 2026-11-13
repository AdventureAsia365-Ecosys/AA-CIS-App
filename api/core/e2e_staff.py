"""AA-816 A10b (S252, root ADR 0006a A10b) — the Dev-only E2E CIS admin: constants and rules shared by the staff
directory (api/services/staff_directory.py), the identity resolver (api/core/staff_identity.py), the bearer
middleware (api/middleware/staff_bearer.py) and the route policy (api/core/staff_authz.py).

A tiny module of its own so staff_identity can read the address without importing staff_directory (which imports
staff_identity).

TEMPORARY, removed at the end of project code with A10 / A10a (AA-816):

- :data:`E2E_CIS_ADMIN_TEST_STAFF_EMAIL` is the one address. Its directory row is an ``admin``, not an owner, without
  a password (migration 283 CHECK) and without a Booking grant; the WORKFORCE pre token trigger issues its tokens
  only with exactly ``cis-admin`` and only where ``E2E_DEV_ONLY_STAFF_ENABLED`` is ``"true"`` (Terraform: dev only).
- :func:`dev_only_e2e_environment`: the API's ``ENVIRONMENT`` must be exactly ``"dev"`` (no default, no strip, no
  lower-casing: unset, ``"Dev "``, ``"prod"`` all refuse) to create, re-enable or resolve this account.
- Its session is **read-only** (``e2e_read_only`` on the identity): every non-GET/HEAD/OPTIONS on a staff path is
  refused with 403 ``e2e_read_only`` before the route runs, EXCEPT the explicit allow-list
  :data:`E2E_ALLOWED_WRITES` — the two content-quality sentence-apply routes behind ``/admin/content-quality`` Cut and
  manual Edit with a reason chip (S252 orchestrator decision: the AA-916 WM-07 sample of admin sentence actions on
  synthetic Dev tenants). On those routes only ``mode`` ``cut`` / ``manual`` is accepted (:data:`E2E_SENTENCE_MODES`):
  ``rewrite`` records a model suggestion, and ``.../sentences/suggest`` (the paid model call) stays refused.
- AA-898 (S255): the one READ beyond its admin role, ``docs.owner_read`` (api/core/staff_authz.docs_owner_read_via):
  on Dev only (``ENVIRONMENT`` exactly "dev", re-read per call) and only on a verified Cognito session, it may list and
  read the owner-only pages of ``/admin/docs`` so the agent's admin-evidence run can screenshot one. ``is_owner`` stays
  False, so ``staff.write`` and every owner-only write stay refused. Removed with A10 / A10b like the rest of this list.
- AA-979 (S252, root ADR 0006a A10b amendment "A10b-1"): the allow-list also holds the two Content studio invitation
  routes (:data:`E2E_STUDIO_INVITATION_WRITES`: invite, send again), so the AA-979 phase 2 re-enrolment E2E runs without
  a person. The middleware and the route policy let them through; the routes themselves then refuse the A10b session
  (403 ``e2e_read_only``, before any write or relay) unless the tenant is synthetic (``shared.tenants.is_canary IS
  TRUE``, :data:`SYNTHETIC_TENANT_SQL`), the invitee is an address of the E2E mailbox domain
  (:data:`E2E_MAIL_DOMAIN`, :func:`is_e2e_mailbox_address`) and the Content studio role is ``viewer`` or ``editor``
  (:data:`E2E_INVITE_ROLES`: the body's role on a new invitation, the membership's stored role on a send-again).

- AA-1087 (S278, root ADR 0006a A10b-3): one more allow-listed write, the Compliance release of a held piece
  (:data:`E2E_COMPLIANCE_RELEASE_WRITES`), the CIS staff write of the synthetic pilot rehearsal. The route refuses the
  A10b session (403 ``e2e_read_only``, before the flag is resolved or audited) unless the flag's tenant is synthetic
  (:func:`is_synthetic_tenant`: ``is_canary IS TRUE`` and not the pilot).

- AA-1094 (S278, root ADR 0006a A10b-4): two more allow-listed writes, the TripPlanner add-on of a synthetic tenant
  (:data:`E2E_TRIPPLANNER_ADDON_WRITES`: the ``tenant_provision`` attach and cancel routes, the same staff path as
  Tenant 360 > Subscription "Add an add-on" / "Cancel"), so the agent can serve a synthetic agency on TripPlanner and
  show its own tours live. The routes refuse the A10b session (403 ``e2e_read_only``, before a job is enqueued) unless
  every add-on of the request is a :data:`E2E_ADDON_PRODUCTS` product (``tripplanner``: no CRM seats, no content plan)
  and the tenant is synthetic (:func:`is_synthetic_tenant`). The create route (``POST /admin/tenants/provision``) stays
  refused: a new tenant is never an E2E write.

- AA-1093 (S279, root ADR 0006a A10b-5): five more allow-listed writes, the live DRY RUN drill of the tenant offboarding
  erasure saga (:data:`E2E_ERASURE_DRILL_WRITES`): offboard and restore the tenant, start the drill, run the saga job
  now (advance), stop the request. Each route refuses the A10b session (403 ``e2e_read_only``, before any write, job or
  audit row) unless the tenant is synthetic (:func:`is_synthetic_tenant`; for advance / stop the request's tenant):
  :mod:`api.core.e2e_tenant_writes`. Approve and new-round stay off the list (only a person approves an erasure) and
  refuse every Dev-only E2E identity inside the route as well.

A10d (AA-1035, S270, root ADR 0006a A10d): a second Dev-only CIS E2E identity, :data:`E2E_CIS_CS_TEST_STAFF_EMAIL`,
role ``customer_success``, so the agent can prove the AA-1035 tenant-book scoping with a real Cognito session. Same
directory rules as A10b (not an owner, no password, no Booking grant, no CRM role; CIS migration 378), same Dev-only
environment guard, same read-only cap, but **no write allow-list at all**: every non-GET/HEAD/OPTIONS is refused 403
``e2e_read_only``, including the A10b allow-listed routes (sentence apply, studio invitations, view-as), and it gets no
``docs.owner_read``. Its tenant book holds synthetic tenants only (api/routers/admin_tenant360.put_tenant_assignment).

Two identity flags (api/core/staff_identity.StaffIdentity) carry this:

- ``e2e_read_only``: any Dev-only E2E identity (:data:`DEV_ONLY_E2E_STAFF_ROLES`, :func:`is_dev_only_e2e_staff`).
  Caps the session at reads.
- ``e2e_allowlisted_writes``: the A10b identity only (:func:`is_e2e_cis_admin`). Every A10b exception (the write
  allow-list in the middleware and the route policy, the route-level checks of A10b-1 / A10b-2, the sentence modes,
  ``docs.owner_read``) keys on this flag, never on ``e2e_read_only``.
"""
from __future__ import annotations

import os
import re
import uuid
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

E2E_CIS_ADMIN_TEST_STAFF_EMAIL = "e2e-cis-admin@e2e-mail.lumiguides.it.com"
E2E_CIS_ADMIN_TEST_STAFF_ROLE = "admin"
# A10d (AA-1035, S270): the Dev-only read-only customer_success E2E account (same address as AA-CIS-Infra workforce.py
# E2E_CIS_CS_TEST_STAFF_EMAIL and AA-Partner e2e_staff.E2E_CIS_CS_EMAIL). A module-level constant on purpose: the live
# precheck finds the deployed address by scanning this module's upper-case str constants.
E2E_CIS_CS_TEST_STAFF_EMAIL = "e2e-cis-cs@e2e-mail.lumiguides.it.com"
E2E_CIS_CS_TEST_STAFF_ROLE = "customer_success"

# Every Dev-only CIS E2E identity and its one directory role (read-only map). A10b admin, A10d customer_success.
DEV_ONLY_E2E_STAFF_ROLES: Mapping[str, str] = MappingProxyType({
    E2E_CIS_ADMIN_TEST_STAFF_EMAIL: E2E_CIS_ADMIN_TEST_STAFF_ROLE,
    E2E_CIS_CS_TEST_STAFF_EMAIL: E2E_CIS_CS_TEST_STAFF_ROLE,
})

# The two other WORKFORCE E2E identities of root ADR 0006a, named here for the staff directory reconcile's "known E2E
# exceptions" block (services/staff_reconcile.py, AA-816 follow-up S253). A10: the content E2E account (the staff
# directory's own constant is api/services/staff_directory.E2E_TEST_STAFF_EMAIL; a test keeps the two equal).
# A10a: the Booking staff admin E2E account, never in the CIS directory.
E2E_CONTENT_TEST_STAFF_EMAIL = "e2e@e2e-mail.lumiguides.it.com"
E2E_BOOKING_TEST_STAFF_EMAIL = "e2e-booking@e2e-mail.lumiguides.it.com"
# A10c (AA-1019, S262; named here for the reconcile by AA-1031): the Dev-only AA-Partner staff E2E account, groups
# exactly crm-ops + crm-support, never in the CIS directory (same address as AA-CIS-Infra workforce.py
# E2E_CRM_STAFF_TEST_STAFF_EMAIL and AA-Partner e2e_staff.E2E_CRM_STAFF_EMAIL).
E2E_CRM_STAFF_TEST_STAFF_EMAIL = "e2e-crm-staff@e2e-mail.lumiguides.it.com"
# A10e (AA-1087, S278; named here for the reconcile): the three Dev-only write-capable AA-Booking E2E accounts, each in
# exactly one Booking group, never in the CIS directory (same addresses as AA-CIS-Infra workforce.py
# E2E_BOOKING_{STAFF,MAKER,CHECKER}_TEST_STAFF_EMAIL and AA-Booking app/auth/e2e_staff.py). AA-Booking holds each one to
# its own writes on synthetic agencies only; the CIS API never sees them.
E2E_BOOKING_STAFF_TEST_STAFF_EMAIL = "e2e-booking-staff@e2e-mail.lumiguides.it.com"
E2E_BOOKING_MAKER_TEST_STAFF_EMAIL = "e2e-booking-maker@e2e-mail.lumiguides.it.com"
E2E_BOOKING_CHECKER_TEST_STAFF_EMAIL = "e2e-booking-checker@e2e-mail.lumiguides.it.com"
# A10e-2 (AA-1091, S279, root ADR 0006a A10e-2): the AA-1091 live check with SYNTHETIC prices. The maker is the A10d
# address above once an owner gave it the Staff page "Pricing team" preset: its one Booking grant may then be
# ``contracting`` (Dev only; staff_directory.require_e2e_test_scope), so its groups are exactly cis-customer_success +
# booking-contracting. Its CIS session stays read-only (A10d: no write allow-list); AA-Booking holds its writes to the
# pricing steps on synthetic trip plans. The checker is a Dev-only booking-contracting address, never in the CIS
# directory (same address as AA-CIS-Infra workforce.py E2E_BOOKING_PRICING_CHECKER_TEST_STAFF_EMAIL and AA-Booking
# app/auth/e2e_staff.E2E_PRICING_CHECKER_EMAIL).
E2E_CIS_CS_PRICING_BOOKING_ROLE = "contracting"
E2E_BOOKING_PRICING_CHECKER_TEST_STAFF_EMAIL = "e2e-booking-pricing-checker@e2e-mail.lumiguides.it.com"
# The one Booking grant a CIS directory E2E address may hold (Dev only; every other E2E address holds none). Read-only.
DEV_ONLY_E2E_BOOKING_ROLES: Mapping[str, str] = MappingProxyType({
    E2E_CIS_CS_TEST_STAFF_EMAIL: E2E_CIS_CS_PRICING_BOOKING_ROLE,
})

# The one ENVIRONMENT value under which the account may exist (compared exactly).
DEV_ONLY_ENVIRONMENT = "dev"

# The 403 details (the BFF and the agent's helpers match on them).
E2E_READ_ONLY = "e2e_read_only"
E2E_NOT_ALLOWED_HERE = "e2e_not_allowed_here"

READ_METHODS: frozenset[str] = frozenset({"GET", "HEAD", "OPTIONS"})

# The writes an A10b session may make: (method, route template). The route policy (staff_authz) matches on the
# template; the middleware, which runs before routing, matches the concrete path with E2E_ALLOWED_WRITE_PATHS.
# AA-979 (ADR 0006a A10b-1): the two Content studio invitation routes, synthetic tenants only (checked by the routes).
E2E_STUDIO_INVITATION_WRITES: frozenset[tuple[str, str]] = frozenset({
    ("POST", "/admin/tenants/{tenant_id}/members/invitations"),
    ("POST", "/admin/tenants/{tenant_id}/members/{membership_id}/invitation/send"),
})
# AA-1035 R2 slice 2 (root ADR 0006a A10b-2): start and end a read-only view-as session, synthetic tenants only
# (checked by api/routers/admin_view_as.py), so the agent's admin-evidence run can show the banner and the audit row.
# The session itself is read-only whoever opens it (api/core/view_as.py).
E2E_VIEW_AS_WRITES: frozenset[tuple[str, str]] = frozenset({
    ("POST", "/admin/tenants/{tenant_id}/view-as"),
    ("POST", "/admin/view-as/sessions/{session_id}/end"),
})
# AA-1087 (S278, root ADR 0006a A10b-3): release a held piece (Tenant QA > Compliance: the staff review "approve" of an
# agency's content that the subject gate held), synthetic tenants only (checked by
# api/routers/admin_tenant_compliance.py before any write): the pilot rehearsal proves a CIS staff write in a browser.
E2E_COMPLIANCE_RELEASE_WRITES: frozenset[tuple[str, str]] = frozenset({
    ("POST", "/admin/tenant-qa/compliance/{flag_id}/release"),
})
# AA-1094 (S278, root ADR 0006a A10b-4): attach / cancel the TripPlanner add-on of a synthetic tenant (the
# tenant_provision saga's attach and cancel modes), checked by api/routers/admin_tenant_provision.py before a job is
# enqueued: every product of the request in E2E_ADDON_PRODUCTS and the tenant synthetic.
E2E_TRIPPLANNER_ADDON_WRITES: frozenset[tuple[str, str]] = frozenset({
    ("POST", "/admin/tenants/{tenant_id}/provision"),
    ("POST", "/admin/tenants/{tenant_id}/provision/cancel"),
})
# The add-on products an A10b session may attach or cancel (never crm: seats are a CRM licence count).
E2E_ADDON_PRODUCTS: frozenset[str] = frozenset({"tripplanner"})
# AA-1093 (S279, root ADR 0006a A10b-5): the DRY RUN drill of the tenant offboarding erasure saga, synthetic tenants
# only (checked by api/core/e2e_tenant_writes.py inside each route). Approve and new-round are never E2E writes.
_SAGA = "/admin/job-runner/erasure/requests"
E2E_ERASURE_DRILL_WRITES: frozenset[tuple[str, str]] = frozenset({
    ("POST", "/admin/tenants/{tenant_id}/offboard"),
    ("POST", "/admin/tenants/{tenant_id}/restore"),
    ("POST", f"{_SAGA}/drill"),
    ("POST", f"{_SAGA}/{{request_id}}/advance"),
    ("POST", f"{_SAGA}/{{request_id}}/stop"),
})
E2E_ALLOWED_WRITES: frozenset[tuple[str, str]] = frozenset({
    ("POST", "/admin/content-quality/pieces/{piece_id}/sentences/apply"),
    ("POST", "/admin/content-quality/tour-versions/{version_id}/sentences/apply"),
}) | E2E_STUDIO_INVITATION_WRITES | E2E_VIEW_AS_WRITES | E2E_COMPLIANCE_RELEASE_WRITES | E2E_TRIPPLANNER_ADDON_WRITES \
    | E2E_ERASURE_DRILL_WRITES
_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
E2E_ALLOWED_WRITE_PATHS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("POST", re.compile(rf"/admin/content-quality/pieces/{_UUID}/sentences/apply")),
    ("POST", re.compile(rf"/admin/content-quality/tour-versions/{_UUID}/sentences/apply")),
    ("POST", re.compile(rf"/admin/tenants/{_UUID}/members/invitations")),
    ("POST", re.compile(rf"/admin/tenants/{_UUID}/members/{_UUID}/invitation/send")),
    ("POST", re.compile(rf"/admin/tenants/{_UUID}/view-as")),
    ("POST", re.compile(rf"/admin/view-as/sessions/{_UUID}/end")),
    ("POST", re.compile(rf"/admin/tenant-qa/compliance/{_UUID}/release")),
    ("POST", re.compile(rf"/admin/tenants/{_UUID}/provision")),
    ("POST", re.compile(rf"/admin/tenants/{_UUID}/provision/cancel")),
    ("POST", re.compile(rf"/admin/tenants/{_UUID}/offboard")),
    ("POST", re.compile(rf"/admin/tenants/{_UUID}/restore")),
    ("POST", re.compile(rf"{_SAGA}/drill")),
    ("POST", re.compile(rf"{_SAGA}/{_UUID}/advance")),
    ("POST", re.compile(rf"{_SAGA}/{_UUID}/stop")),
)
# The sentence-apply modes an A10b session may use (Cut, manual Edit). Not "rewrite".
E2E_SENTENCE_MODES: frozenset[str] = frozenset({"cut", "manual"})

# AA-979 (A10b-1): what makes a tenant synthetic. ``is_canary`` (CIS migration 058: "TRUE for synthetic canary tenant")
# is nullable on Dev since migration 281, so only an explicit TRUE counts; a missing tenant reads as not synthetic.
# AA-969 (root ADR 0019 d16): the pilot tenant (``is_pilot``, CIS 354) is never synthetic, whatever its canary flag, so
# the E2E identity never writes to it.
SYNTHETIC_TENANT_SQL = "SELECT is_canary IS TRUE AND is_pilot IS NOT TRUE FROM shared.tenants WHERE tenant_id = $1"
# The invitee must be a recipient of the E2E mailbox (AA-CIS-Infra e2e_mailbox.tf), the domain of the A10b address.
E2E_MAIL_DOMAIN = E2E_CIS_ADMIN_TEST_STAFF_EMAIL.split("@", 1)[1]
# The Content studio roles an A10b session may invite (the AA-979 §6a role check): never ``owner``.
E2E_INVITE_ROLES: frozenset[str] = frozenset({"viewer", "editor"})


def normalise(email: Any) -> str:
    return email.strip().lower() if isinstance(email, str) else ""


def is_e2e_cis_admin(email: Any) -> bool:
    """True for the A10b address only (after trimming and lower-casing): the one identity with allow-listed writes."""
    return normalise(email) == E2E_CIS_ADMIN_TEST_STAFF_EMAIL


def is_e2e_cis_cs(email: Any) -> bool:
    """True for the A10d address (after trimming and lower-casing)."""
    return normalise(email) == E2E_CIS_CS_TEST_STAFF_EMAIL


def e2e_booking_role_allowed(email: Any, booking_role: Any) -> bool:
    """A10e-2: True for no Booking grant, or for the one grant :data:`DEV_ONLY_E2E_BOOKING_ROLES` allows the address,
    only where ``ENVIRONMENT`` is exactly ``"dev"`` (the staff directory's E2E scope rule)."""
    allowed = DEV_ONLY_E2E_BOOKING_ROLES.get(normalise(email))
    return booking_role is None or (allowed is not None and booking_role == allowed and dev_only_e2e_environment())


def e2e_booking_rule(email: Any) -> str:
    """How the directory's 400 ``e2e_test_scope`` words the Booking part of an E2E address's scope."""
    allowed = DEV_ONLY_E2E_BOOKING_ROLES.get(normalise(email))
    return f"no Booking role other than {allowed} (Dev only)" if allowed else "no Booking role"


def is_dev_only_e2e_staff(email: Any) -> bool:
    """True for every Dev-only CIS E2E address (:data:`DEV_ONLY_E2E_STAFF_ROLES`: A10b, A10d)."""
    return normalise(email) in DEV_ONLY_E2E_STAFF_ROLES


def has_allowlisted_writes(identity: Any) -> bool:
    """True only for a verified identity that is the A10b session (``e2e_read_only`` AND ``e2e_allowlisted_writes``).
    Every A10b exception asks this, never ``e2e_read_only`` alone (AA-1035 A10d)."""
    return bool(getattr(identity, "e2e_read_only", False) and getattr(identity, "e2e_allowlisted_writes", False))


def dev_only_e2e_environment() -> bool:
    """True only when ``ENVIRONMENT`` is exactly ``"dev"`` (read per call; unset refuses, unlike the
    ``os.getenv("ENVIRONMENT", "dev")`` default used elsewhere)."""
    return os.environ.get("ENVIRONMENT") == DEV_ONLY_ENVIRONMENT


def is_allowed_e2e_write_path(method: str, path: str) -> bool:
    """The middleware's check: the concrete (method, path) is one of :data:`E2E_ALLOWED_WRITE_PATHS`, matched in full
    (no prefix, no trailing slash, no extra segment)."""
    return any(method == m and pattern.fullmatch(path or "") for m, pattern in E2E_ALLOWED_WRITE_PATHS)


def is_allowed_e2e_write_route(method: str, route: str) -> bool:
    """The route policy's check, on the matched route template."""
    return (method.upper(), route) in E2E_ALLOWED_WRITES


def is_e2e_mailbox_address(email: Any) -> bool:
    """True when ``email`` is one address of :data:`E2E_MAIL_DOMAIN`: ASCII only (``str.lower()`` folds some non-ASCII
    letters to ASCII, the A10a look-alike rule), one ``@``, a non-empty local part, the domain matched exactly after
    trimming and lower-casing (no sub-domain, no trailing dot)."""
    if not isinstance(email, str) or not email.isascii():
        return False
    local, sep, domain = normalise(email).rpartition("@")
    return bool(sep) and bool(local) and "@" not in local and domain == E2E_MAIL_DOMAIN


async def is_synthetic_tenant(conn: Any, tenant_id: Any) -> bool:
    """True only when ``shared.tenants.is_canary`` is TRUE for ``tenant_id`` and it is not the pilot tenant (NULL,
    FALSE, the pilot or no row: False)."""
    return (await conn.fetchval(SYNTHETIC_TENANT_SQL, tenant_id)) is True


async def synthetic_write_refusal(identity: Any, conn: Any, tenant_id: Any) -> str | None:
    """A10b-5 (AA-1093): why a write keyed on ``tenant_id`` is refused to this identity, or None when it may go on.

    None, without a DB read, for every identity that is not a Dev-only E2E session. ``not_allowlisted`` for any
    Dev-only E2E identity without the A10b allow-list (A10d). ``tenant_not_synthetic`` when ``tenant_id`` is not a
    uuid, has no row, is not ``is_canary`` or is the pilot (:func:`is_synthetic_tenant`). ``conn`` may be a pool."""
    if not getattr(identity, "e2e_read_only", False):
        return None
    if not has_allowlisted_writes(identity):
        return "not_allowlisted"
    try:
        tid = uuid.UUID(str(tenant_id))
    except ValueError:
        return "tenant_not_synthetic"
    return None if await is_synthetic_tenant(conn, tid) else "tenant_not_synthetic"
