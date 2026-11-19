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


def may_edit_price(request: Request) -> bool:
    """AA-1146 D5: may the caller change a source tour's price text (reference_price.write: admin, customer_success)?
    A secret session without a role (the machine) may; a staff role without the capability may not."""
    from api.core.staff_authz import REFERENCE_PRICE_WRITE
    if (getattr(request, "headers", None) or {}).get("x-admin-role") is None:
        return True
    return REFERENCE_PRICE_WRITE in request_perms(request)


def price_read_only() -> HTTPException:
    """403 price_read_only: the D5 refusal of a price change (PATCH /raw, a merge, a fact draft's publish)."""
    return HTTPException(status_code=403, headers={"X-Error-Code": "price_read_only"},
                         detail="Prices are entered by the pricing team (Reference prices), not on the tour")


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


async def review_publish_gate(conn: Any, request: Request, review_id: str, tenant_id: str) -> None:
    """The publish gate of a Review Queue approve (api/routers/v1_pipeline.py approve_review): the editors are the
    version's draft editors and its last hand editor (reviewed_by). No extra read while the switch is off."""
    from services.content_generation import tour_draft as td
    if not await gate.switch_on(conn):
        return
    row = await conn.fetchrow("""
        SELECT gc.reviewed_by::text AS reviewed_by, gc.edit_diff
          FROM silver_aa_internal.review_queue rq
          JOIN silver_aa_internal.generated_content gc ON gc.id = rq.generated_content_id
         WHERE rq.id = $1::uuid AND rq.tenant_id = $2::uuid
    """, review_id, tenant_id)
    editors = td.editors_of(row["edit_diff"], None) if row is not None else []
    if row is not None and row["reviewed_by"]:
        editors.append(f"admin:{row['reviewed_by']}")
    await enforce_publish_gate(conn, request, editors=editors)


def lock_kw(request: Request) -> dict:
    """Who published, for the hand-edit lock process_export sets on a hand-edited version."""
    return {"lock_by": request_actor(request)}
