"""AA-986 L3 (root ADR 0015 decision 5, Q7): the tenant's own notifications — the portal bell.

The tour lifecycle service (services/tour_lifecycle, L2) writes `tour.discontinued` / `tour.reinstated` rows into
shared.notifications for every tenant with a derivative of a tour whose served state changed. Before this router only
the staff bell read that table, so the warning never reached the tenant (audit S248 section 1 point 5).

  GET /v1/notifications              the tenant's tenant-facing rows, newest first, + the unread count
  POST /v1/notifications/{id}/read   mark one read (POST: the portal BFF proxy has no PUT)
  POST /v1/notifications/read-all    mark all read

Tenant pool (aa_app_user) behind TENANT_NOTIFICATIONS_FLAG (`aa986:tenant_notifications_pool`, fail closed; needs
CIS 274: RLS on shared.notifications, policy tenant_isolation + the restrictive notifications_tenant_audience, so
the tenant reads only its own rows written for tenants; SELECT and UPDATE (is_read) only). The SQL filters the same
way itself (`tenant_id = $1 AND target_roles @> {tenant}`), so the flag-off path (main pool, cis_app) answers the
same rows. The tenant's `is_read` is its own: the staff bell no longer reads or clears tenant-only rows
(api/routers/admin.py notification_scope).

The badge on My Tours / Content / Publish is the source of truth (served_status on each /v1 row); a notification is
the event that it changed.
"""
from __future__ import annotations

import json
from typing import Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request

from api.core.aa544_tenant_pool import TENANT_NOTIFICATIONS_FLAG, acquire_scoped_conn
from api.routers.v1_tours import get_tenant

logger = structlog.get_logger()
router = APIRouter(prefix="/v1/notifications", tags=["tenant-notifications"])

# The tenant-facing rows of one tenant (services/notifications.py _DEFAULT_ROLES: target_roles {tenant}).
_TENANT_COND = "tenant_id = $1::uuid AND target_roles @> ARRAY['tenant']::text[]"
_LIST_SQL = f"""
    SELECT id, event_type, entity_type, entity_id, payload, is_read, created_at
      FROM shared.notifications
     WHERE {_TENANT_COND} {{unread}}
     ORDER BY created_at DESC, id DESC
     LIMIT $2
"""
_UNREAD_SQL = f"SELECT count(*) FROM shared.notifications WHERE {_TENANT_COND} AND NOT is_read"
_READ_ONE_SQL = f"UPDATE shared.notifications SET is_read = true WHERE id = $2 AND {_TENANT_COND}"
_READ_ALL_SQL = f"UPDATE shared.notifications SET is_read = true WHERE {_TENANT_COND} AND NOT is_read"

_TITLES = {
    "tour.discontinued": "Tour no longer served",
    "tour.reinstated": "Tour served again",
    "tour.pages_to_put_back": "Pages to put back online",   # AA-1039 (option A): the restore fan-out writes it
    "tour.pages_to_review": "Pages about a tour no longer offered",   # AA-1039 (S276): the retire fan-out writes it
}
# Rows that carry a page list with a one-click action per page (put back online / move to draft).
_PAGE_ROWS = ("tour.pages_to_put_back", "tour.pages_to_review")


def _payload(raw) -> dict:
    if raw is None:
        return {}
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except ValueError:
            return {}
    return dict(raw)


def _message(event_type: str, p: dict) -> str:
    name = p.get("tour_name") or "A tour"
    if event_type == "tour.discontinued":
        msg = (f"Adventure Asia no longer serves {name}. Your copies and content stay in your account; "
               "new rewrites, posts and publishing for it are closed.")
        live = int(p.get("live_posts") or 0)
        if live:
            msg += (f" {live} published post{'s' if live != 1 else ''} about it "
                    f"{'are' if live != 1 else 'is'} still live and stay{'' if live != 1 else 's'} online: you can "
                    f"rewrite, move to draft or keep {'them' if live != 1 else 'it'} (Publish).")
        return msg
    if event_type == "tour.reinstated":
        return f"{name} is served by Adventure Asia again. You can work on it as before."
    if event_type == "tour.pages_to_put_back":
        n = len(_pages(p))
        return (f"{name} is back on sale. {n} of your WordPress page{'s' if n != 1 else ''} moved to draft while it "
                f"was off sale {'stay' if n != 1 else 'stays'} in draft until you put "
                f"{'them' if n != 1 else 'it'} back online.")
    if event_type == "tour.pages_to_review":
        pages = _pages(p, review=True)
        n = len(pages)
        dated = any(x.get("draft_on") for x in pages)
        msg = (f"Adventure Asia no longer offers {name}. {n} of your WordPress page{'s' if n != 1 else ''} about it "
               f"{'are' if n != 1 else 'is'} still online. You can rewrite {'them' if n != 1 else 'it'}, move "
               f"{'each one' if n != 1 else 'it'} to draft with one click, or keep {'them' if n != 1 else 'it'}.")
        if dated:
            msg += " Pages with a date go to draft on that date, as set in your Content studio Settings."
        return msg
    return p.get("message") or ""


def _pages(p: dict, review: bool = False) -> list[dict]:
    """AA-1039: the pages of a page row ({publish_id, url, draft_on}). tour.pages_to_put_back: the drafted pages, action
    POST /v1/publish-log/{publish_id}/put-back ("already online" for a page put back since). tour.pages_to_review
    (S276): the live pages about a tour no longer offered, action POST /v1/publish-log/{publish_id}/move-to-draft
    ("already in draft" for a page drafted since); draft_on is the automatic draft date of an opted-in agency, else
    None."""
    return [{"publish_id": str(x.get("publish_id")), "url": x.get("url"),
             **({"draft_on": x.get("draft_on")} if review else {})}
            for x in (p.get("pages") or []) if isinstance(x, dict) and x.get("publish_id")]


def _item(r) -> dict:
    p = _payload(r["payload"])
    return {
        "id": int(r["id"]),
        "event_type": r["event_type"],
        "title": _TITLES.get(r["event_type"], r["event_type"]),
        "message": _message(r["event_type"], p),
        "tour_id": r["entity_id"] if r["entity_type"] == "tour" else None,
        "tour_name": p.get("tour_name"),
        "reason": p.get("reason"),
        "live_posts": int(p.get("live_posts") or 0),
        "pages": _pages(p, review=r["event_type"] == "tour.pages_to_review") if r["event_type"] in _PAGE_ROWS else [],
        "is_read": bool(r["is_read"]),
        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
    }


@router.get("", summary="The tenant's own notifications (tour no longer served / served again / pages to put back / "
                        "pages about a tour no longer offered)")
async def list_notifications(
    request: Request,
    tenant=Depends(get_tenant),
    unread_only: bool = False,
    limit: int = Query(30, ge=1, le=100),
):
    tenant_id = tenant["sub"]
    async with acquire_scoped_conn(request, tenant_id, TENANT_NOTIFICATIONS_FLAG, fail_closed=True) as (conn, used):
        rows = await conn.fetch(_LIST_SQL.format(unread="AND NOT is_read" if unread_only else ""), tenant_id, limit)
        unread = await conn.fetchval(_UNREAD_SQL, tenant_id)
    logger.info("aa986_tenant_notifications_read", tenant_id=str(tenant_id), used_tenant_pool=used, count=len(rows))
    return {"items": [_item(r) for r in rows], "unread": int(unread or 0)}


@router.post("/read-all", summary="Mark all of the tenant's notifications read")
async def mark_all_read(request: Request, tenant=Depends(get_tenant)):
    tenant_id = tenant["sub"]
    async with acquire_scoped_conn(request, tenant_id, TENANT_NOTIFICATIONS_FLAG, fail_closed=True) as (conn, _u):
        result = await conn.execute(_READ_ALL_SQL, tenant_id)
    return {"cleared": _count(result)}


@router.post("/{notif_id}/read", summary="Mark one of the tenant's notifications read")
async def mark_read(notif_id: int, request: Request, tenant=Depends(get_tenant)):
    tenant_id = tenant["sub"]
    async with acquire_scoped_conn(request, tenant_id, TENANT_NOTIFICATIONS_FLAG, fail_closed=True) as (conn, _u):
        result = await conn.execute(_READ_ONE_SQL, tenant_id, notif_id)
    if _count(result) == 0:
        raise HTTPException(status_code=404, detail="Notification not found")   # not found or not the tenant's
    return {"id": notif_id, "is_read": True}


def _count(status: Optional[str]) -> int:
    try:
        return int(str(status).split()[-1])
    except (ValueError, IndexError):
        return 0
