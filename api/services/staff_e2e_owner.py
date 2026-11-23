"""AA-1176 (S288, root ADR 0006a A10h) — the Staff page limits of the Dev-only E2E owner (decision D5).

The E2E owner (``e2e-owner@e2e-mail.lumiguides.it.com``, api/core/e2e_staff.py) is an administrator with the owner
marker, so it passes the directory's owner re-read (:func:`api.services.staff_directory._require_owner`). As the
ACTOR it may create staff and change the other staff, but:

- it never changes a row that is an owner, the business owner or its own (roles, disable, enable, sign-out, sync,
  resend invite): 403 ``e2e_owner_scope`` (:func:`refuse_target`, called by ``staff_directory._load_target``);
- it never sets or clears the business-owner marker (api/services/staff_custom_roles.set_business_owner);
- it never counts as "another active owner" for the last-owner guard (:data:`NOT_E2E_OWNER_SQL` in
  ``staff_directory.SQL_OTHER_ACTIVE_OWNERS``), so the last real owner stays protected;
- outside ``ENVIRONMENT`` exactly ``dev`` it is no owner actor at all (403 ``e2e_not_allowed_here``,
  :func:`check_actor`).

As a TARGET it holds exactly CIS admin and the Booking admin grant, no CRM role, Dev only (the directory's E2E scope and
environment rules); a real owner may disable it at any time. Removed with the A10 family (root ADR 0006a A10h).
"""
from __future__ import annotations

from typing import Any

import structlog

from api.core import e2e_staff

logger = structlog.get_logger()

# The SQL tail that leaves the E2E owner out of "another active owner" (a code constant, no user input).
NOT_E2E_OWNER_SQL = f"AND lower(email) IS DISTINCT FROM '{e2e_staff.E2E_OWNER_TEST_STAFF_EMAIL}'"
REFUSED_DETAIL = "The E2E owner account may not change an owner, the business owner or itself"


def is_actor(actor: Any) -> bool:
    """The actor row (``staff_directory.SQL_ACTOR``) is the E2E owner: its address after trimming and lower-casing (a
    look-alike cannot be stored: CIS 473 CHECK)."""
    email = actor.get("email") if actor is not None and hasattr(actor, "get") else None
    return e2e_staff.is_e2e_owner_like(email)


def check_actor(actor: Any) -> None:
    """403 ``e2e_not_allowed_here`` when the actor is the E2E owner and ``ENVIRONMENT`` is not exactly ``dev``."""
    if is_actor(actor):
        from api.services.staff_directory import require_dev_only_e2e_environment   # noqa: PLC0415 - circular

        require_dev_only_e2e_environment(actor["email"])


def refuse_target(actor: Any, target: Any) -> None:
    """403 ``e2e_owner_scope`` when the E2E owner acts on an owner, the business owner or its own row. No-op for every
    other actor (and when no actor is given)."""
    if not is_actor(actor):
        return
    if target["id"] == actor["id"] or target["is_owner"] or target["is_business_owner"]:
        from api.services.staff_directory import StaffDirectoryError   # noqa: PLC0415 - circular

        logger.warning("staff_directory_refused", reason=e2e_staff.E2E_OWNER_SCOPE, actor_id=actor["id"],
                       staff_id=target["id"])
        raise StaffDirectoryError(403, REFUSED_DETAIL, e2e_staff.E2E_OWNER_SCOPE)
