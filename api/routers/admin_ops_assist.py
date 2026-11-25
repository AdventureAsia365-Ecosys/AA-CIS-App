"""AA-1154 (A0) — the CIS admin assistant (ops assist), read-only: evidence, action catalogue, rule-based cards.

  GET /admin/ops-assist/catalog                 the action catalogue: every action a card may propose, the API it
                                                calls, its base risk and who approves it
  GET /admin/ops-assist/signals                 what the assistant sees beyond the status page's Needs-attention list
                                                (an AI connection refusing at account level, throttled, a spend spike)
  GET /admin/ops-assist/items/{item_id}/cards   evidence + ranked action cards + plain-words Summary for one
                                                Needs-attention item (its id on the status page) or assistant signal;
                                                ``range`` = the status page's range, so the item ids match
  GET /admin/ops-assist/replays                 the recorded incidents the rules replay
  GET /admin/ops-assist/replays/{case}          one recorded incident replayed through today's rules
  A1 (the model ranks and explains; it has no tools and nothing is executed):
  POST /admin/ops-assist/items/{item_id}/analyse  the cards of one item ranked and explained by the model (one call of
                                                the AI gateway stage ops_assist, month cap 10 USD; depth quick = the
                                                stage route, deep = the model of cis.ops.assist_deep_model); cached
                                                for the card lifetime
  POST /admin/ops-assist/auto                   the status page's automatic analysis: one per incident of a severity
                                                in cis.ops.assist_auto_severities (default SEV1 / SEV2)
  GET  /admin/ops-assist/changes                "what changed" in one of the windows of
                                                cis.ops.assist_lookup_windows_min (minutes), from the records ($0, no
                                                model); the answer lists the offered windows
  POST /admin/ops-assist/changes/phrase         the same answer phrased by the model (one call; none when nothing
                                                changed)

Every route is ops.read (the status page's capability: admin, per the role matrix): the POST routes call a model
within the stage's hard-stop month cap and write nothing but the gateway ledger and a Redis cache. The business owner
(``shared.admin_users.is_business_owner``) gets the Summary only, no cards (tech lead AI10). See
``api/services/ops_assist/__init__.py``.
"""
from __future__ import annotations

from typing import Optional

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, Request

from api.core.staff_authz import staff_policy
from api.core.tenant_scope import staff_id_of
from api.routers.admin import verify_admin_secret
from api.services import ops_assist
from api.services.ops_assist import analysis, evidence, lookup, replays

logger = structlog.get_logger()

router = APIRouter(prefix="/admin/ops-assist", tags=["admin-ops-assist"], dependencies=[Depends(staff_policy)])

_ITEM_ID = Path(..., pattern=r"^[A-Za-z0-9_.:-]{1,200}$", description="A Needs-attention item id or a signal id")
_CASE = Path(..., pattern=r"^[a-z0-9-]{1,80}$")
_RANGE = Query("24h", alias="range", pattern="^(1h|6h|24h|7d)$",
               description="the status page's range (the item ids depend on it)")
_WINDOW = Query(None, description="how far back, in minutes: one of the windows of the setting "
                                  "cis.ops.assist_lookup_windows_min (default: the hour when offered)")


async def _is_business_owner(request: Request) -> bool:
    return await evidence.business_owner(request.app.state.pool, staff_id_of(request))


@router.get("/catalog", summary="The assistant's action catalogue (read-only)")
async def get_catalog(x_admin_secret: str = Header(None)) -> dict:
    verify_admin_secret(x_admin_secret)
    return ops_assist.catalogue()


@router.get("/signals", summary="What the assistant sees beyond the Needs-attention list")
async def get_signals(request: Request, x_admin_secret: str = Header(None)) -> dict:
    verify_admin_secret(x_admin_secret)
    return await ops_assist.list_signals(request.app.state.pool, getattr(request.app.state, "redis", None))


@router.get("/items/{item_id}/cards", summary="Evidence, ranked action cards and Summary for one item (no LLM)")
async def get_cards(
    request: Request,
    item_id: str = _ITEM_ID,
    range_: str = Query("24h", alias="range", pattern="^(1h|6h|24h|7d)$",
                        description="the status page's range (the item ids depend on it)"),
    x_admin_secret: str = Header(None),
) -> dict:
    verify_admin_secret(x_admin_secret)
    from api.services.platform_apps import relay_authorization

    out = await ops_assist.analyse(request.app.state.pool, getattr(request.app.state, "redis", None), item_id,
                                   range_key=range_, authorization=relay_authorization(request),
                                   business_owner=await _is_business_owner(request))
    if out.get("item") is None and "status" in (out.get("unavailable") or {}):
        raise HTTPException(status_code=503, detail={"code": "status_unavailable",
                                                     "message": "The status page could not be read; try again"})
    if out.get("item") is None:
        raise HTTPException(status_code=404, detail={"code": "no_such_item",
                                                     "message": "No open item or signal with this id now"})
    logger.info("ops_assist_cards", item_id=item_id, playbook=out.get("playbook"), cards=len(out.get("cards") or []),
                audience=out.get("audience"))
    return out


@router.get("/replays", summary="Recorded incidents the assistant's rules replay")
async def get_replays(x_admin_secret: str = Header(None)) -> dict:
    verify_admin_secret(x_admin_secret)
    return {"replays": replays.index()}


@router.get("/replays/{case}", summary="One recorded incident replayed through today's rules ($0)")
async def get_replay(request: Request, case: str = _CASE, x_admin_secret: str = Header(None)) -> dict:
    verify_admin_secret(x_admin_secret)
    try:
        return ops_assist.replay(case, business_owner=await _is_business_owner(request))
    except KeyError:
        raise HTTPException(status_code=404, detail={"code": "no_such_replay", "message": "No recorded incident "
                                                                                          "with this name"})


def _no_item(out: dict) -> None:
    if out.get("item") is None and "status" in (out.get("unavailable") or {}):
        raise HTTPException(status_code=503, detail={"code": "status_unavailable",
                                                     "message": "The status page could not be read; try again"})
    if out.get("item") is None:
        raise HTTPException(status_code=404, detail={"code": "no_such_item",
                                                     "message": "No open item or signal with this id now"})


@router.post("/items/{item_id}/analyse", summary="The item's cards ranked and explained by the model (one AI call)")
async def post_analyse(
    request: Request,
    item_id: str = _ITEM_ID,
    range_: str = _RANGE,
    depth: str = Query("quick", pattern="^(quick|deep)$",
                       description="quick = the stage route, deep = the model of cis.ops.assist_deep_model"),
    fresh: bool = Query(False, description="ask the model again instead of reusing the answer of the card lifetime"),
    x_admin_secret: str = Header(None),
) -> dict:
    verify_admin_secret(x_admin_secret)
    from api.services.platform_apps import relay_authorization

    out = await analysis.analyse(request.app.state.pool, getattr(request.app.state, "redis", None), item_id,
                                 range_key=range_, depth=depth, fresh=fresh,
                                 authorization=relay_authorization(request),
                                 business_owner=await _is_business_owner(request))
    _no_item(out)
    return out


@router.post("/auto", summary="Analyse each open incident of an automatic severity once (the status page calls it)")
async def post_auto(request: Request, range_: str = _RANGE, x_admin_secret: str = Header(None)) -> dict:
    verify_admin_secret(x_admin_secret)
    from api.services.platform_apps import relay_authorization

    return await analysis.auto(request.app.state.pool, getattr(request.app.state, "redis", None), range_key=range_,
                               authorization=relay_authorization(request))


def _window(window: Optional[int]) -> tuple[int, list[int]]:
    """(the window to read, the offered windows) from the setting; 422 for a window it does not offer."""
    offered = ops_assist.limits()["lookup_windows_min"]
    minutes = lookup.pick_window(window, offered)
    if minutes is None:
        raise HTTPException(status_code=422, detail={"code": "window_not_offered", "offered": offered,
                                                     "message": "Pick one of the offered windows"})
    return minutes, offered


@router.get("/changes", summary="What changed in the window, from the records (no AI call)")
async def get_changes(request: Request, window: Optional[int] = _WINDOW, x_admin_secret: str = Header(None)) -> dict:
    verify_admin_secret(x_admin_secret)
    minutes, offered = _window(window)
    return await lookup.what_changed(request.app.state.pool, minutes, windows=offered)


@router.post("/changes/phrase", summary="What changed in the window, phrased by the model (one AI call)")
async def post_changes_phrase(request: Request, window: Optional[int] = _WINDOW,
                              x_admin_secret: str = Header(None)) -> dict:
    verify_admin_secret(x_admin_secret)
    minutes, offered = _window(window)
    return await lookup.phrase(request.app.state.pool, minutes, windows=offered)
