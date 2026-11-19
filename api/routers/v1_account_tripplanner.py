"""
/v1/account/tripplanner — AA-1139: the Trip planner module in the Content studio, for an agency WITHOUT the CRM.

The agency app has the module twice: the CRM page (Settings > Add-ons > Trip planner; the CRM relays to
``tenant.addons.v1`` 1.1, ``api/routers/tenant_addons.py``) and this studio page. An agency without the CRM add-on has
no CRM agency (CIS creates one only for a live ``crm`` subscription, ``services/tenant_account/crm_onboarding.py``), so
its owner reaches the lead route (e-mail / webhook, the channel its leads really take: ``effective_channel`` is ``crm``
only while ``crm.core`` is live) and the planner's look here, with the studio's own CIS tenant token:

  GET  /v1/account/tripplanner                the module (``TripPlannerModule``: addresses, domains, lead route stored
                                              and in force, theme, locale, AI sessions quota)
  PUT  /v1/account/tripplanner/lead-route     {"lead_channel": none | webhook | email, "lead_webhook_url"?,
                                              "lead_email"?} -> the module after the change
  PUT  /v1/account/tripplanner/appearance     {"theme": {token: "#rrggbb"}, "locale": en | vi} -> the module

Tenant-JWT-gated (``get_tenant``; the tenant id comes ONLY from the verified token). The PUTs are ``/v1/account``
writes, so the tenant role middleware lets only the Content studio owner make them (editor and viewer: 403
``studio_role_forbidden``); every role reads. A "view as tenant" token reaches none of them (not in
``api/core/view_as.READ_ROUTES``). The same checks and the same audit rows as the CRM relay
(``services/tenant_account/tripplanner_site.py``: ``set_agency_lead_route`` / ``set_appearance``), the actor being the
person's studio membership (``membership:<id>``). A refusal is the editor's code: 422 (it names the field), 409
``tripplanner_not_active`` / ``tenant_offboarded``, 404 ``tenant_not_found``; ``X-Error-Code`` carries it. 503 while the
site table is missing. The TripPlanner half of the page (usage, leads, the webhook signing key) is served by the CRM
API to the same person after it checked this membership with CIS ``tenant.membership.v1`` (AA-Partner).
"""
from __future__ import annotations

from typing import Literal, Optional

import asyncpg
import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from api.routers.tenant_addons import TripPlannerModule
from api.routers.v1_account_addons import _actor, _tenant_id
from api.routers.v1_tours import get_tenant
from services.tenant_account import tripplanner_site as tp_site

logger = structlog.get_logger()
router = APIRouter(prefix="/v1/account", tags=["tenant-account"])

UNAVAILABLE = "The Trip planner settings are not available yet."
_DB_ERRORS = (asyncpg.UndefinedTableError, asyncpg.UndefinedColumnError)


class StudioLeadRouteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lead_channel: Literal["none", "webhook", "email"]
    lead_webhook_url: Optional[str] = Field(None, max_length=tp_site.WEBHOOK_URL_MAX + 100)
    lead_email: Optional[str] = Field(None, max_length=tp_site.EMAIL_MAX + 100)


class StudioAppearanceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    theme: dict[str, str] = Field(max_length=len(tp_site.THEME_TOKENS))
    locale: Literal["en", "vi"]


def _refused(e: tp_site.LeadRouteError) -> HTTPException:
    return HTTPException(status_code=e.status, detail={"detail": e.code, "message": e.message},
                         headers={"X-Error-Code": e.code})


async def _module(conn, tenant_id: str) -> dict:
    body = await tp_site.module_view(conn, tenant_id)
    if body is None:
        raise tp_site.LeadRouteError("tenant_not_found", status=404, message="no tenant with this id")
    return TripPlannerModule.model_validate(body).model_dump()


@router.get("/tripplanner", response_model=TripPlannerModule,
            summary="The agency's Trip planner: addresses, domains, lead route, look, quota")
async def get_account_tripplanner(request: Request, tenant=Depends(get_tenant)):
    tenant_id = _tenant_id(tenant)
    try:
        async with request.app.state.pool.acquire() as conn:
            return await _module(conn, tenant_id)
    except _DB_ERRORS as exc:
        logger.error("tenant_tripplanner_unavailable", reason=type(exc).__name__)
        raise HTTPException(status_code=503, detail=UNAVAILABLE) from exc
    except tp_site.LeadRouteError as e:
        raise _refused(e) from None


@router.put("/tripplanner/lead-route", response_model=TripPlannerModule,
            summary="Where the planner's leads go while the agency has no CRM (Content studio owner)")
async def put_account_tripplanner_lead_route(body: StudioLeadRouteIn, request: Request, tenant=Depends(get_tenant)):
    tenant_id = _tenant_id(tenant)
    try:
        async with request.app.state.pool.acquire() as conn:
            await tp_site.set_agency_lead_route(conn, tenant_id, channel=body.lead_channel,
                                                webhook_url=body.lead_webhook_url, email=body.lead_email,
                                                actor=_actor(tenant, tenant_id))
            out = await _module(conn, tenant_id)
    except _DB_ERRORS as exc:
        logger.error("tenant_tripplanner_unavailable", reason=type(exc).__name__)
        raise HTTPException(status_code=503, detail=UNAVAILABLE) from exc
    except tp_site.LeadRouteError as e:
        logger.info("tenant_tripplanner_route_refused", tenant_id=tenant_id, code=e.code, via="studio")
        raise _refused(e) from None
    logger.info("tenant_tripplanner_route_set", tenant_id=tenant_id, channel=body.lead_channel, via="studio")
    return out


@router.put("/tripplanner/appearance", response_model=TripPlannerModule,
            summary="The planner's brand colours and language (Content studio owner)")
async def put_account_tripplanner_appearance(body: StudioAppearanceIn, request: Request, tenant=Depends(get_tenant)):
    tenant_id = _tenant_id(tenant)
    try:
        async with request.app.state.pool.acquire() as conn:
            await tp_site.set_appearance(conn, tenant_id, theme=body.theme, locale=body.locale,
                                         actor=_actor(tenant, tenant_id))
            out = await _module(conn, tenant_id)
    except _DB_ERRORS as exc:
        logger.error("tenant_tripplanner_unavailable", reason=type(exc).__name__)
        raise HTTPException(status_code=503, detail=UNAVAILABLE) from exc
    except tp_site.LeadRouteError as e:
        logger.info("tenant_tripplanner_appearance_refused", tenant_id=tenant_id, code=e.code, via="studio")
        raise _refused(e) from None
    logger.info("tenant_tripplanner_appearance_set", tenant_id=tenant_id, locale=body.locale, via="studio")
    return out
