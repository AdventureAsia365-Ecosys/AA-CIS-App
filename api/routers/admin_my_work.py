"""AA-1156 — My work: the content band of the staff landing page (/admin/my-work), one tile per work queue.

  GET /admin/my-work/summary
      The tiles the caller's role may work, each with the number of items waiting, when the oldest of them entered
      the queue, and the filter of the target page that lists exactly those items:

      key                     needs           target page (filter)                       counted by
      review_waiting          review.act      /admin/review (its default: pending)       review_queue_summary
      supply_drafts           content.write   /admin/supply-content?has_draft=1          supply_content.status_summary
      supply_no_description   content.write   /admin/supply-content?status=draft         supply_content.status_summary
      aa_content_drafts       content.write   /admin/aa-content?status=draft             aa_content.store.summary
      place_cards_to_review   quality.write   /admin/places/cards?status=auto_checked    place_card_list.summary
      compliance_holds        quality.write   /admin/tenant-qa/compliance (default open) admin_tenant_compliance.summary

Shown by capability, never by person: the route needs one of the three capabilities (POLICY, api/core/staff_authz.py)
and each tile is in the answer only for a role that holds its capability. Every count runs the page's own filter text
(the counting helper sits next to the page's list query and shares its WHERE), so a tile equals the total its target
page shows for the same filter. The frontend keeps the page path of each key (frontend/lib/myWork.ts) and adds the
``filter`` given here as the query string. A tile whose table is missing (a migration not applied yet) answers
``available: false`` instead of failing the whole page. Read-only, no LLM, no cache: the page loads it on open and on
Refresh (no polling).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

import asyncpg
import structlog
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from api.core.staff_authz import CONTENT_WRITE, QUALITY_WRITE, REVIEW_ACT, StaffPrincipal, staff_policy

logger = structlog.get_logger()

router = APIRouter(prefix="/admin/my-work", tags=["admin-my-work"], dependencies=[Depends(staff_policy)])


async def _review_waiting(conn) -> dict:
    from api.routers.admin_pipeline.review import review_queue_summary
    return await review_queue_summary(conn, status="pending")


async def _supply_drafts(conn) -> dict:
    # by open draft version, not by entity status: a redraft of published or stale content (new facts from Booking,
    # or a human edit) keeps the entity's status, and only a 'draft' entity moves to 'in_review'
    from services.supply_content import review
    return await review.status_summary(conn, has_draft=True)


async def _supply_no_description(conn) -> dict:
    # status 'draft' never has an open draft (the first draft moves it to in_review). A 'stale' entity with no new draft
    # yet is left out: Booking shows no text for it either, but the automatic redraft job is already queued for it
    # (the supply feed board on the page shows that job's failures)
    from services.supply_content import review
    return await review.status_summary(conn, status="draft")


async def _aa_content_drafts(conn) -> dict:
    from services.aa_content import store
    return await store.summary(conn, status="draft")


async def _place_cards(conn) -> dict:
    from services.acp_shared import place_card_list
    return await place_card_list.summary(conn, status=place_card_list.TO_REVIEW)


async def _compliance(conn) -> dict:
    from api.routers import admin_tenant_compliance
    return await admin_tenant_compliance.summary(conn, state="open")


@dataclass(frozen=True)
class Tile:
    key: str
    capability: str
    # the target page's query string for exactly these items ({} = the page's default view)
    filter: dict
    count: Callable[[Any], Awaitable[dict]]


# Display order. frontend/lib/myWork.ts MY_WORK_TILES lists the same keys and capabilities (test_aa1156_my_work keeps
# the two in step).
TILES: tuple[Tile, ...] = (
    Tile("review_waiting", REVIEW_ACT, {}, _review_waiting),
    Tile("supply_drafts", CONTENT_WRITE, {"has_draft": "1"}, _supply_drafts),
    Tile("supply_no_description", CONTENT_WRITE, {"status": "draft"}, _supply_no_description),
    Tile("aa_content_drafts", CONTENT_WRITE, {"status": "draft"}, _aa_content_drafts),
    Tile("place_cards_to_review", QUALITY_WRITE, {"status": "auto_checked"}, _place_cards),
    Tile("compliance_holds", QUALITY_WRITE, {}, _compliance),
)
CAPABILITIES: tuple[str, ...] = tuple(dict.fromkeys(t.capability for t in TILES))


class MyWorkTile(BaseModel):
    key: str
    capability: str
    filter: dict[str, str]
    available: bool
    count: Optional[int] = None
    oldest_at: Optional[datetime] = None


class MyWorkSummary(BaseModel):
    role: Optional[str]
    tiles: list[MyWorkTile]
    generated_at: datetime


def tiles_for(perms: frozenset[str]) -> list[Tile]:
    """The tiles a caller holding ``perms`` may work, in display order."""
    return [t for t in TILES if t.capability in perms]


async def _one(conn, tile: Tile) -> MyWorkTile:
    base = {"key": tile.key, "capability": tile.capability, "filter": tile.filter}
    try:
        got = await tile.count(conn)
    except (asyncpg.UndefinedTableError, asyncpg.UndefinedColumnError) as exc:
        logger.warning("my_work_tile_unavailable", tile=tile.key, error=type(exc).__name__)
        return MyWorkTile(**base, available=False)
    return MyWorkTile(**base, available=True, count=got["count"], oldest_at=got["oldest_at"])


@router.get("/summary", response_model=MyWorkSummary, summary="My work: the caller's content queues")
async def my_work_summary(request: Request, principal: StaffPrincipal = Depends(staff_policy)) -> MyWorkSummary:
    tiles = tiles_for(principal.perms)
    async with request.app.state.pool.acquire() as conn:
        out = [await _one(conn, t) for t in tiles]
    return MyWorkSummary(role=principal.role, tiles=out, generated_at=datetime.now(timezone.utc))
