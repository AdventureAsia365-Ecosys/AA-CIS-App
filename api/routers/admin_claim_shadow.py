"""AA-1050 W4 (root ADR 0020 D7) — Writing Lab › claim shadow: the read side of the pinned claim instrument's shadow
verdicts (shared.claim_shadow_verdict, CIS 391; services/quality/claim_shadow.py).

  GET /admin/writing-memory/claim-shadow/summary?days=30&run_kind=live&surface=&sample=30&seed=aa1050
      the switch, the pinned instrument, its calibrated / enforce classes (none until W2 measures one; every class
      warns), the shadow stage's spend over its window vs the cap, agreement counts per surface (agree / disagree /
      unverified / no_baseline), the baseline x verdict cells with their cost, the typed reasons, the cached and
      pinned shares, how many verdicts the enforce map would hold or review (and how many of those came from a
      non-pinned answer: must be 0), and a seeded, stable sample of disagreement ids (piece / version ids, no text,
      no tenant id) for the tech lead to label.

Who may call (api/core/staff_authz.py POLICY): Writing Lab read (lab.read or tenant_content.read): counts and ids.
Errors: 503 `unavailable` before CIS 391.
The router is included without an include prefix (staff_policy reads the route template from scope["route"].path).
"""
from __future__ import annotations

from typing import Literal, Optional

import asyncpg
import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request

from api.core.staff_authz import staff_policy
from api.routers.admin import verify_admin_secret
from services.quality import claim_shadow as cs

logger = structlog.get_logger()

router = APIRouter(prefix="/admin/writing-memory/claim-shadow", tags=["admin-claim-shadow"],
                   dependencies=[Depends(verify_admin_secret), Depends(staff_policy)])


def _pool(request: Request):
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="DB not ready")
    return pool


@router.get("/summary")
async def summary(request: Request, days: int = Query(30, ge=1, le=180),
                  run_kind: Literal["live", "replay", "all"] = "live",
                  surface: Optional[Literal["t9_blog", "t9_social", "t2"]] = None,
                  sample: int = Query(30, ge=0, le=200),
                  seed: str = Query("aa1050", pattern=r"^[A-Za-z0-9_-]{1,32}$")) -> dict:
    try:
        async with _pool(request).acquire() as conn:
            return await cs.summary(conn, days=days, run_kind=run_kind, surface=surface, sample=sample, seed=seed)
    except asyncpg.UndefinedTableError as e:
        logger.warning("aa1050_claim_shadow_unavailable", error=f"{type(e).__name__}: {str(e)[:200]}")
        raise HTTPException(status_code=503, detail={"detail": "The claim shadow table is not applied yet (CIS 391)",
                                                     "code": "unavailable"})
