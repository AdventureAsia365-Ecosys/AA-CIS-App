"""AA-1127 C3 — the staff session's own access hint for the admin shell.

  GET /admin/session   {role, read_only, reason}: may this session change data at all? (UI hint, $0, no DB read)

Before this route only the AI gateway page knew that a session is read-only (its own GET /admin/ai-gateway/access),
so every other page showed run and paid buttons to the Dev E2E admin, whose role cookie says "admin" but whose every
write the server refuses (403 ``e2e_read_only``, api/core/staff_identity.py). The admin shell reads this once per
session (react-query, 5 min) and hands the answer to the kit (``ReadOnlyProvider`` in app/_kit/core/ReadOnly.tsx);
pages hide their write controls with ``useReadOnly()`` / ``WriteOnly``. It is a hint: the server still refuses every
write it must (staff_authz.denial_reason), whatever the page shows.

``read_only`` is true for a read-only role (staff_authz.READ_ONLY_ROLES, the viewer) and for a Dev-only E2E
identity (``e2e_read_only``), the A10b identity included: its few allow-listed writes (e2e_staff.E2E_ALLOWED_WRITES)
are page-specific and the pages that offer them do not read this hint. ``reason`` says which ("role" | "e2e" |
"view_as" | null); AA-1157: "view_as" for an owner's read-only "view as a lower role" session.

Every staff role may call it (capability notifications.read, the shell-level read every role holds). The router is
included without an include prefix (staff_policy reads the route template from scope["route"].path).
"""
from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from api.core.staff_authz import READ_ONLY_ROLES, StaffPrincipal, staff_policy
from api.routers.admin import verify_admin_secret

router = APIRouter(prefix="/admin/session", tags=["admin-session"],
                   dependencies=[Depends(verify_admin_secret), Depends(staff_policy)])


class SessionHint(BaseModel):
    role: Optional[str] = None
    read_only: bool
    reason: Optional[Literal["role", "e2e", "view_as"]] = None


def session_hint(principal: StaffPrincipal) -> SessionHint:
    """The hint for one caller (pure; tests/unit/test_aa1127_session_hint.py)."""
    if principal.view_as_session:
        # AA-1157: an owner's "view as a lower role" session (the role is the view role): every write is refused.
        return SessionHint(role=principal.role, read_only=True, reason="view_as")
    if principal.role in READ_ONLY_ROLES:
        return SessionHint(role=principal.role, read_only=True, reason="role")
    if principal.e2e_read_only:
        return SessionHint(role=principal.role, read_only=True, reason="e2e")
    return SessionHint(role=principal.role, read_only=False)


@router.get("", response_model=SessionHint,
            summary="This staff session: may it change data at all? (admin shell hint, $0)")
async def get_session(principal: StaffPrincipal = Depends(staff_policy)) -> SessionHint:
    return session_hint(principal)
