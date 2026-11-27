"""AA-1156 — My work: the staff landing page (/admin/my-work), one tile per work queue. AA-1190 (slice 1): every
staff role lands here (lib/rbac.ts landingPage), approval and platform tiles join the content queues, and each tile
says which band it belongs to and whether it counts toward the page's total.

  GET /admin/my-work/summary
      The tiles the caller may act on, each with the number of items waiting, when the oldest of them entered the
      queue, its band (``group``), whether it counts toward the total (``counts_toward_total``) and the filter of the
      target page that lists exactly those items:

      key                               group     needs              target page (filter)
      settings_to_approve               approve   settings.approve   /admin/platform/settings?pending=1&app=<app>
      compliance_exceptions_to_approve  approve   compliance.write   /admin/platform/compliance?tab=exceptions
      erasure_to_approve                approve   tenant.write (*)   /admin/tenants/data-erasure?tab=offboarding
      review_waiting                    approve   review.act         /admin/review?policy=none
      supply_drafts                     work      content.write      /admin/supply-content?has_draft=1
      supply_no_description             work      content.write      /admin/supply-content?status=draft
      aa_content_drafts                 work      content.write      /admin/aa-content?status=draft
      compliance_holds                  work      quality.write      /admin/tenant-qa/compliance (default: open)
      ops_open_problems                 platform  ops.write (**)     /admin/overview
      review_held_by_policy             watch     review.act         /admin/review?policy=elephant_riding
      place_cards_to_review             watch     quality.write      /admin/places/cards?status=auto_checked

      Counted by (each next to its page's list query): settings_registry.open_proposals_summary,
      compliance_exceptions.proposed_summary, tenant_erasure.store.awaiting_approval_summary, review_queue_summary
      (both Review Queue tiles), supply_content.status_summary, aa_content.store.summary,
      admin_tenant_compliance.summary, ops_status.store.open_problems_summary, place_card_list.summary.

      (*) and the administrator role: approving an erasure needs it (admin_tenant_erasure.approve).
      (**) never for the business owner, who reads the status page and does not acknowledge (admin_overview).

Bands: ``approve`` (maker-checker: only what the caller may decide; their own proposals are left out exactly when the
approve route refuses them: for settings, a maker_checker key while two-person approval is on or the content publish
switch while it is on, ``settings_registry._own_refused``), ``work``, ``platform`` and ``watch``. A ``watch`` tile is
backlog or a decision of policy (place cards, the elephant-riding policy reviews): shown, never counted in the total
(``counts_toward_total`` false).

Shown by capability, never by person: the route needs one of the tile capabilities or ops.read (a custom role with
ops.read lands here too; POLICY, api/core/staff_authz.py) and each tile is in the answer only for a caller that may act
on it. Every count runs the page's own filter text (the counting helper sits next to the page's list query and shares
its WHERE), so a tile equals the total its target page shows for the same filter. The frontend keeps the page path
of each key (frontend/lib/myWork.ts) and adds the ``filter`` given here as the query string. A tile whose table is
missing (a migration not applied yet) answers ``available: false`` instead of failing the whole page. Read-only, no
LLM, no cache: the page loads it on open and on Refresh (no polling).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

import asyncpg
import structlog
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from api.core.staff_authz import (
    COMPLIANCE_WRITE, CONTENT_WRITE, OPS_READ, OPS_WRITE, QUALITY_WRITE, REVIEW_ACT, SETTINGS_APPROVE, TENANT_WRITE,
    StaffPrincipal, request_staff_identity, staff_policy,
)

logger = structlog.get_logger()

router = APIRouter(prefix="/admin/my-work", tags=["admin-my-work"], dependencies=[Depends(staff_policy)])

APPROVE, WORK, PLATFORM, WATCH = "approve", "work", "platform", "watch"
GROUPS: tuple[str, ...] = (APPROVE, WORK, PLATFORM, WATCH)


@dataclass(frozen=True)
class Viewer:
    """Who asks: the role, its capabilities, ``actor`` (``admin:<id>`` of a verified company sign-in, the identity the
    approve routes compare with the proposer; None otherwise) and the business-owner mark (read only when a tile needs
    it)."""
    role: Optional[str]
    perms: frozenset[str]
    actor: Optional[str] = None
    is_business_owner: bool = False


async def _settings_to_approve(conn, viewer: Viewer) -> dict:
    from api.services import settings_registry
    got = await settings_registry.open_proposals_summary(conn, viewer=viewer.actor)
    # the page shows one app tab at a time: open the tab of the oldest proposal (by_app says where the others are)
    return {**got, "filter": {"pending": "1", **({"app": got["app"]} if got["app"] else {})}}


async def _compliance_exceptions(conn, viewer: Viewer) -> dict:
    from api.services import compliance_exceptions
    return await compliance_exceptions.proposed_summary(conn, viewer=viewer.actor)


async def _erasure_to_approve(conn, _viewer: Viewer) -> dict:
    from services.tenant_erasure import store
    return await store.awaiting_approval_summary(conn)


async def _review_waiting(conn, _viewer: Viewer) -> dict:
    from api.routers.admin_pipeline.review import NO_POLICY_FILTER, review_queue_summary
    return await review_queue_summary(conn, status="pending", policy=NO_POLICY_FILTER)


async def _review_held(conn, _viewer: Viewer) -> dict:
    from api.routers.admin_pipeline.review import review_queue_summary
    from services.quality import policy_review
    return await review_queue_summary(conn, status="pending", policy=policy_review.FILTER_VALUE)


async def _supply_drafts(conn, _viewer: Viewer) -> dict:
    # by open draft version, not by entity status: a redraft of published or stale content (new facts from Booking,
    # or a human edit) keeps the entity's status, and only a 'draft' entity moves to 'in_review'
    from services.supply_content import review
    return await review.status_summary(conn, has_draft=True)


async def _supply_no_description(conn, _viewer: Viewer) -> dict:
    # status 'draft' never has an open draft (the first draft moves it to in_review). A 'stale' entity with no new draft
    # yet is left out: Booking shows no text for it either, but the automatic redraft job is already queued for it
    # (the supply feed board on the page shows that job's failures)
    from services.supply_content import review
    return await review.status_summary(conn, status="draft")


async def _aa_content_drafts(conn, _viewer: Viewer) -> dict:
    from services.aa_content import store
    return await store.summary(conn, status="draft")


async def _place_cards(conn, _viewer: Viewer) -> dict:
    from services.acp_shared import place_card_list
    return await place_card_list.summary(conn, status=place_card_list.TO_REVIEW)


async def _compliance(conn, _viewer: Viewer) -> dict:
    from api.routers import admin_tenant_compliance
    return await admin_tenant_compliance.summary(conn, state="open")


async def _ops_open_problems(conn, _viewer: Viewer) -> dict:
    from api.services.ops_status import store
    return await store.open_problems_summary(conn)


@dataclass(frozen=True)
class Tile:
    key: str
    capability: str
    # the target page's query string for exactly these items ({} = the page's default view); a counter may answer
    # its own ``filter`` (the settings tile opens the app tab of its oldest proposal)
    filter: dict
    count: Callable[[Any, Viewer], Awaitable[dict]]
    group: str = WORK
    counts_toward_total: bool = True
    # only these roles, on top of the capability (None: any holder of it)
    roles: Optional[tuple[str, ...]] = None
    # hidden for the business owner (who may not act on it)
    not_business_owner: bool = False


# Display order (bands first approve, work, platform, watch; the page groups them). frontend/lib/myWork.ts
# MY_WORK_TILES lists the same keys, capabilities and groups (test_aa1156_my_work keeps the two in step).
TILES: tuple[Tile, ...] = (
    Tile("settings_to_approve", SETTINGS_APPROVE, {"pending": "1"}, _settings_to_approve, APPROVE),
    Tile("compliance_exceptions_to_approve", COMPLIANCE_WRITE, {"tab": "exceptions"}, _compliance_exceptions,
         APPROVE),
    Tile("erasure_to_approve", TENANT_WRITE, {"tab": "offboarding"}, _erasure_to_approve, APPROVE,
         roles=("admin",)),
    Tile("review_waiting", REVIEW_ACT, {"policy": "none"}, _review_waiting, APPROVE),
    Tile("supply_drafts", CONTENT_WRITE, {"has_draft": "1"}, _supply_drafts),
    Tile("supply_no_description", CONTENT_WRITE, {"status": "draft"}, _supply_no_description),
    Tile("aa_content_drafts", CONTENT_WRITE, {"status": "draft"}, _aa_content_drafts),
    Tile("compliance_holds", QUALITY_WRITE, {}, _compliance),
    Tile("ops_open_problems", OPS_WRITE, {}, _ops_open_problems, PLATFORM, not_business_owner=True),
    Tile("review_held_by_policy", REVIEW_ACT, {"policy": "elephant_riding"}, _review_held, WATCH,
         counts_toward_total=False),
    Tile("place_cards_to_review", QUALITY_WRITE, {"status": "auto_checked"}, _place_cards, WATCH,
         counts_toward_total=False),
)
CAPABILITIES: tuple[str, ...] = tuple(dict.fromkeys(t.capability for t in TILES))
# The route also opens to ops.read alone: a custom role with it lands on My work (lib/rbac.ts customLanding).
ROUTE_CAPABILITIES: tuple[str, ...] = (*CAPABILITIES, OPS_READ)


class MyWorkTile(BaseModel):
    key: str
    capability: str
    group: str
    counts_toward_total: bool
    filter: dict[str, str]
    available: bool
    count: Optional[int] = None
    oldest_at: Optional[datetime] = None


class MyWorkSummary(BaseModel):
    role: Optional[str]
    tiles: list[MyWorkTile]
    generated_at: datetime


def tiles_for(perms: frozenset[str], role: Optional[str] = None, *, is_business_owner: bool = False) -> list[Tile]:
    """The tiles a caller holding ``perms`` (with ``role``) may act on, in display order."""
    return [t for t in TILES
            if t.capability in perms
            and (t.roles is None or role in t.roles)
            and not (t.not_business_owner and is_business_owner)]


async def _one(conn, tile: Tile, viewer: Viewer) -> MyWorkTile:
    base = {"key": tile.key, "capability": tile.capability, "group": tile.group,
            "counts_toward_total": tile.counts_toward_total, "filter": tile.filter}
    try:
        got = await tile.count(conn, viewer)
    except (asyncpg.UndefinedTableError, asyncpg.UndefinedColumnError) as exc:
        logger.warning("my_work_tile_unavailable", tile=tile.key, error=type(exc).__name__)
        return MyWorkTile(**base, available=False)
    if got.get("filter") is not None:
        base["filter"] = got["filter"]
    return MyWorkTile(**base, available=True, count=got["count"], oldest_at=got["oldest_at"])


def _actor(request: Request) -> Optional[str]:
    """``admin:<id>`` of a verified company sign-in (the approve routes' maker-checker identity), else None."""
    identity = request_staff_identity(request)
    if identity is None or getattr(identity, "auth", None) != "cognito" or not getattr(identity, "admin_id", None):
        return None
    return f"admin:{identity.admin_id}"


async def _is_business_owner(request: Request, conn) -> bool:
    """The caller carries the business-owner mark (shared.admin_users.is_business_owner). False when unknown: the
    platform tile is a count that links to the status page, which applies the same rule itself."""
    from api.core.tenant_scope import staff_id_of
    from api.services.ops_assist.evidence import BUSINESS_OWNER_SQL

    sid = staff_id_of(request)
    if not sid:
        return False
    try:
        return bool(await conn.fetchval(BUSINESS_OWNER_SQL, sid))
    except Exception as e:  # noqa: BLE001
        logger.warning("my_work_business_owner_read_failed", error=repr(e)[:200])
        return False


@router.get("/summary", response_model=MyWorkSummary, summary="My work: what is waiting for the caller")
async def my_work_summary(request: Request, principal: StaffPrincipal = Depends(staff_policy)) -> MyWorkSummary:
    async with request.app.state.pool.acquire() as conn:
        owner = False
        if any(t.not_business_owner for t in tiles_for(principal.perms, principal.role)):
            owner = await _is_business_owner(request, conn)
        viewer = Viewer(role=principal.role, perms=principal.perms, actor=_actor(request), is_business_owner=owner)
        tiles = tiles_for(principal.perms, principal.role, is_business_owner=owner)
        out = [await _one(conn, t, viewer) for t in tiles]
    return MyWorkSummary(role=principal.role, tiles=out, generated_at=datetime.now(timezone.utc))
