"""AA-646 — admin-triggered search-demand research (the only path that buys DataForSEO demand data).

Before AA-646 `run_segment_research()` fired after every tenant T2 rewrite and, because AA-545 made
Segments platform-wide, swept every platform place for the tenant's markets (25/09/2026: 2,215
places x AU/US/UK, $49). Research is now an explicit admin action:

  - POST /admin/segment-research/preview — free: scope, stale count, selected count. No LLM, no DFS.
  - POST /admin/segment-research/run     — starts one run in the background (admin secret required).

Both take explicit markets and a mandatory `max_places` cap. Only one run at a time. The run stays an
in-process background task until the durable job runner (AA-650) replaces it; places that fail or are
skipped by an abort stay stale and are picked up by the next run (AA-647).
"""
from __future__ import annotations

import asyncio
from typing import Optional

import structlog
from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from api.routers.admin import verify_admin_secret
from services.seo_intelligence.seed_builder import DFS_LOCATION_MAP

logger = structlog.get_logger()

router = APIRouter(prefix="/admin/segment-research", tags=["admin-segment-research"])

# Hard ceiling until the cost guard (AA-649) exists: one run can never cover more places than this.
MAX_PLACES_PER_RUN = 200

_background_tasks: set = set()
_run_state: dict = {"running": False, "last_result": None}


class ResearchScope(BaseModel):
    markets: list[str] = Field(..., min_length=1, description="Buyer market codes, e.g. ['US','UK']")
    max_places: int = Field(..., ge=1, le=MAX_PLACES_PER_RUN)
    places: Optional[list[str]] = None
    tour_ids: Optional[list[str]] = None
    country: Optional[str] = None

    @field_validator("markets")
    @classmethod
    def _known_markets(cls, v: list[str]) -> list[str]:
        codes = [m.strip().upper() for m in v]
        unknown = [m for m in codes if m not in DFS_LOCATION_MAP]
        if unknown:
            raise ValueError(f"unknown market(s) {unknown}; supported: {sorted(DFS_LOCATION_MAP)}")
        return codes


def _kwargs(scope: ResearchScope) -> dict:
    return {
        "places": scope.places,
        "tour_ids": scope.tour_ids,
        "country": scope.country,
        "max_places": scope.max_places,
    }


@router.post("/preview", summary="AA-646 — scope + stale count for a research run (no LLM/DFS spend)")
async def preview_research(scope: ResearchScope, request: Request):
    from services.acp_contract.segment_research import run_segment_research

    result = await run_segment_research(
        {"countries": scope.markets}, request.app.state.pool, dry_run=True, **_kwargs(scope),
    )
    return {**result, "running": _run_state["running"]}


@router.post("/run", status_code=202, summary="AA-646 — start one admin-triggered research run")
async def run_research(scope: ResearchScope, request: Request, x_admin_secret: str = Header(None),
                       x_admin_user_id: str = Header(None)):
    verify_admin_secret(x_admin_secret)
    if _run_state["running"]:
        raise HTTPException(status_code=409, detail="A segment research run is already in progress")

    from services.acp_contract.segment_research import run_segment_research

    pool = request.app.state.pool
    preview = await run_segment_research({"countries": scope.markets}, pool, dry_run=True, **_kwargs(scope))
    if preview["places_selected"] == 0:
        return {**preview, "started": False, "reason": "nothing stale in scope"}

    async def _run() -> None:
        _run_state["running"] = True
        try:
            result = await run_segment_research({"countries": scope.markets}, pool, **_kwargs(scope))
            _run_state["last_result"] = result
            logger.info("admin_segment_research_done", admin_user=x_admin_user_id, **result)
        except Exception:
            logger.error("admin_segment_research_failed", admin_user=x_admin_user_id, exc_info=True)
        finally:
            _run_state["running"] = False

    task = asyncio.create_task(_run())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    logger.info("admin_segment_research_started", admin_user=x_admin_user_id,
                markets=scope.markets, places_selected=preview["places_selected"])
    return {**preview, "started": True}


@router.get("/status", summary="AA-646 — is a run in progress, and the last run's result")
async def research_status():
    return dict(_run_state)
