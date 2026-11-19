# api/routers/admin_pipeline/_publish.py — AA-1146 (S285): who is calling a content write, and the publish gate every
# route that makes a tour version live runs (Review Queue approve, promote, a draft's publish).
from __future__ import annotations

from typing import Any, Iterable, Optional
from uuid import UUID

from fastapi import HTTPException, Request

from api.core.staff_authz import principal_from_role, request_staff_identity
from api.services import content_publish_gate as gate


def request_actor(request: Request) -> str:
    """The audit actor of the caller: admin:<admin_users.id> from the verified identity, else the x-admin-user-id the
    BFF forwards (a well-formed UUID only), else "admin" (a secret session without a person)."""
    identity = request_staff_identity(request)
    admin_id = getattr(identity, "admin_id", None) if identity is not None else None
    if not admin_id:
        raw = (getattr(request, "headers", None) or {}).get("x-admin-user-id")
        try:
            admin_id = str(UUID(raw)) if raw else None
        except (ValueError, AttributeError):
            admin_id = None
    return f"admin:{admin_id}" if admin_id else "admin"


def request_perms(request: Request) -> frozenset:
    """The caller's role capabilities (empty for the machine)."""
    principal = principal_from_role((getattr(request, "headers", None) or {}).get("x-admin-role"),
                                    request_staff_identity(request))
    return frozenset(principal.perms)


async def enforce_publish_gate(conn: Any, request: Request, *, editors: Iterable[Optional[str]] = ()) -> None:
    """403 when cis.content.publish_two_person is on and the caller may not publish this version: no content.publish
    (``two_person_required``), or the caller wrote it (``same_person``; ``editors`` = everyone who wrote it). Reads
    the switch in ``conn`` (fails closed)."""
    if not await gate.switch_on(conn):
        return
    actor = request_actor(request)
    wrote = actor if actor != "admin" and actor in {e for e in editors if e} else None
    code = gate.refusal(switch=True, perms=request_perms(request), actor=actor, author=wrote)
    if code is not None:
        raise HTTPException(status_code=403, detail={"code": code, "message": gate.MESSAGES[code]},
                            headers={"X-Error-Code": code})
