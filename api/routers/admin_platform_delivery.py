"""AA-1108 — Platform › Delivery: CI/CD and DORA per app for the admin page /admin/platform/delivery.

  GET /admin/platform/delivery[?refresh=1][&section=dora|queue|git]
                                               dora (default): the delivery summary per app (five DORA keys with n,
                                               lead-time segments, CI health per workflow, main state, merge-queue
                                               wait, flaky jobs, deploys and failed deploys with recovery time,
                                               12-week trend), each metric's colour and the thresholds in force;
                                               queue (AA-1125): the merge-queue entries of the apps with a merge queue,
                                               coloured with the CIS 445 settings; git (AA-1125): commit activity,
                                               churn hotspots, author kinds and the recent commit graph per repo
                                                                                                    ops.read (admin)
  AA-1135: [&range=7d|28d|90d|custom][&from=YYYY-MM-DD&to=YYYY-MM-DD] — the window of every section: a preset the
           collector publishes, or a custom range of whole UTC days recomputed from the collector's day files
           (api/services/delivery_range.py). 422 for a range that cannot be (reversed, too long for the setting
           cis.delivery.custom_range_max_days, outside the stored history), 409 while custom ranges are paused (the
           CIS engine copy differs from the collector's, the preset is not published yet, or the day files do not
           rebuild the summary's event store: out_of_step), 503 when the
           computation fails. The answer's `range` says the window served and what the date picker may offer.

Read only. The summary comes from the root repo workflow "Delivery metrics" through S3
(api/services/delivery_metrics.py), cached in this process; nothing here touches the database except the settings
reader. `refresh` re-reads the published file (the workflow itself runs hourly). Admin only: POLICY = ops.read, and
the BFF refuses every other role first (frontend/lib/rbac.ts API_READ_RULES "platform").

Included in main.py WITHOUT an include prefix: staff_policy reads the route template from scope["route"].path.
"""
from __future__ import annotations

from datetime import date
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse

from api.core.staff_authz import staff_policy
from api.services import delivery_metrics, delivery_range

router = APIRouter(prefix="/admin/platform/delivery", tags=["admin-platform-delivery"],
                   dependencies=[Depends(staff_policy)])

_NO_STORE = {"Cache-Control": "private, no-store"}


@router.get("", summary="CI/CD and DORA per app: deploys, lead time, failures, recovery, CI health (Dev numbers)")
async def get_delivery(
    refresh: bool = Query(False, description="re-read the published summary (at most every 30 s)"),
    section: Literal["dora", "queue", "git"] = Query(
        "dora", description="dora: DORA and CI per app; queue: merge-queue entries; git: commit activity and churn"),
    range_: Literal["7d", "28d", "90d", "custom"] = Query(
        "28d", alias="range", description="the window: a preset of the collector, or custom with from and to"),
    from_: Optional[date] = Query(None, alias="from", description="custom range: first day (UTC), included"),
    to: Optional[date] = Query(None, description="custom range: last day (UTC), included"),
):
    try:
        body = await delivery_metrics.view(refresh=refresh, section=section, range_=range_,
                                           start=from_.isoformat() if from_ else None,
                                           end=to.isoformat() if to else None)
    except delivery_metrics.DeliveryMissing as e:
        raise HTTPException(status_code=404, detail=str(e))
    except delivery_metrics.DeliveryUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
    except delivery_range.RangeRefused as e:
        raise HTTPException(status_code=e.status, detail=str(e))
    except delivery_range.RangeFailed as e:
        raise HTTPException(status_code=503, detail=str(e))
    return JSONResponse(body, headers=_NO_STORE)
