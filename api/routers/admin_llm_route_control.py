"""AA-1042 (S268, route control center) — the Route control tab of the admin AI gateway page (/admin/ai-gateway).

  GET  /admin/ai-gateway/route-control                       the live route config: order mode, version in force, each
                                                             stage's chain with its selectable models, each model's
                                                             provider order, the presets.                spend.read
  POST /admin/ai-gateway/route-control/preview               what a proposed config would do before it is saved: the
                                                             first model@connection per stage now and after, the rule
                                                             errors / warnings, the evidence need, the diff ($0, no
                                                             model call).                                ai.admin
  POST /admin/ai-gateway/route-control/save                  save a proposal as a new version (audited
                                                             llm_route_config_saved).                    ai.admin
  POST /admin/ai-gateway/route-control/presets/{preset}      apply a preset as a new version (audited
                                                             llm_route_config_preset_applied).           ai.admin
  GET  /admin/ai-gateway/route-control/versions              the newest versions with who / when / diff. spend.read
  GET  /admin/ai-gateway/route-control/versions/{version}    one version with its snapshot.              spend.read
  POST /admin/ai-gateway/route-control/versions/{version}/revert
                                                             restore a version as a new version (audited
                                                             llm_route_config_reverted).                 ai.admin

A body names only what changes: ``order_mode``, ``routes`` (stage -> chain, primary first), ``provider_order`` (model
-> connection keys in order). Writes carry ``expected_version`` (409 when another change was saved since) and, when the
preview says ``needs_evidence`` (a writer / judge stage changes its chain or the model@connection that serves it
first), ``eval_run_id`` or ``break_glass_reason``. 503 until CIS migration 370 is applied. The logic:
api/services/llm_route_control.py; the versions: shared/llm_client/route_versions.py. Every write keeps its own POLICY
capability (api/core/staff_authz.py).
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

from api.core.staff_authz import staff_policy
from api.routers.admin import verify_admin_secret
from api.services import llm_route_control as rc
from shared.llm_client import route_evidence, route_versions

router = APIRouter(prefix="/admin/ai-gateway/route-control", tags=["admin-ai-gateway"],
                   dependencies=[Depends(verify_admin_secret), Depends(staff_policy)])

class Proposal(BaseModel):
    """The changes to preview or save; an omitted part keeps the config in force."""
    model_config = ConfigDict(extra="forbid")
    order_mode: Optional[str] = Field(None, pattern=r"^(model_first|aws_first)$")
    routes: Optional[dict[str, list[str]]] = None
    provider_order: Optional[dict[str, list[str]]] = None

    @model_validator(mode="after")
    def _bounded(self):
        for part in (self.routes or {}), (self.provider_order or {}):
            if len(part) > 200:
                raise ValueError("at most 200 entries")
            for key, items in part.items():
                if len(key) > 100 or len(items) > 20 or any(len(i) > 100 for i in items):
                    raise ValueError("a stage or model key is at most 100 characters and a list at most 20 items")
        return self


class PreviewBody(Proposal):
    preset: Optional[str] = Field(None, max_length=40)
    revert_to: Optional[int] = Field(None, ge=1)

    @model_validator(mode="after")
    def _one_kind(self):
        named = [x for x in (self.preset is not None, self.revert_to is not None,
                             any(v is not None for v in (self.order_mode, self.routes, self.provider_order))) if x]
        if len(named) > 1:
            raise ValueError("preview a preset, a version or your own changes, not several at once")
        return self


class _WriteMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(..., ge=1)    # the version the caller looked at (409 when another was saved)
    note: Optional[str] = Field(None, max_length=route_versions.NOTE_MAX)
    eval_run_id: Optional[str] = Field(None, max_length=64)
    break_glass_reason: Optional[str] = Field(None, max_length=route_evidence.BREAK_GLASS_MAX_CHARS)


class SaveBody(Proposal, _WriteMeta):
    pass


def _pool(request: Request):
    return request.app.state.pool


def _actor(x_admin_user_id: Optional[str]) -> str:
    return f"admin:{x_admin_user_id}" if x_admin_user_id else "admin"


async def _guarded(coro):
    try:
        return await coro
    except rc.RouteControlUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
    except rc.RouteControlConflict as e:
        raise HTTPException(status_code=409, detail=str(e))
    except rc.RouteControlRefused as e:
        raise HTTPException(status_code=422, detail=str(e))
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))


async def _wm_reverify(request: Request, admin_user_id: Optional[str]) -> None:
    """AA-900 W0: a saved route config (save / preset / revert) changes route rows, so the active writing-memory
    releases are re-checked against their config fingerprint (flag only; never fails the save)."""
    from services.writing_memory.releases import reverify_scan_safe
    pool = getattr(getattr(getattr(request, "app", None), "state", None), "pool", None)
    if pool is not None:
        await reverify_scan_safe(pool, actor=_actor(admin_user_id)[:128])


@router.get("", summary="The live route config: order mode, version, chains, provider order, presets ($0)")
async def get_route_control(request: Request) -> dict:
    return await _guarded(rc.read_state(_pool(request)))


@router.post("/preview", summary="What a proposed route config would do, before saving ($0, no model call)")
async def post_preview(body: PreviewBody, request: Request) -> dict:
    return await _guarded(rc.preview(_pool(request), order_mode=body.order_mode, routes=body.routes,
                                     provider_order=body.provider_order, preset=body.preset,
                                     revert_to=body.revert_to))


@router.post("/save", summary="Save a route config change as a new version (validated, audited)")
async def post_save(body: SaveBody, request: Request, x_admin_user_id: Optional[str] = Header(None)) -> dict:
    if body.order_mode is None and not body.routes and not body.provider_order:
        raise HTTPException(status_code=422, detail="nothing to change")
    out = await _guarded(rc.apply(
        _pool(request), actor=_actor(x_admin_user_id), action="save", expected_version=body.expected_version,
        note=body.note, eval_run_id=body.eval_run_id, break_glass_reason=body.break_glass_reason,
        order_mode=body.order_mode, routes=body.routes, provider_order=body.provider_order))
    await _wm_reverify(request, x_admin_user_id)
    return out


@router.post("/presets/{preset}", summary="Apply a route preset as a new version (validated, audited)")
async def post_preset(body: _WriteMeta, request: Request,
                      preset: str = Path(..., pattern=r"^[a-z0-9_]{1,40}$"),
                      x_admin_user_id: Optional[str] = Header(None)) -> dict:
    if preset not in rc.PRESETS:
        raise HTTPException(status_code=404, detail=f"unknown preset {preset!r}")
    out = await _guarded(rc.apply(
        _pool(request), actor=_actor(x_admin_user_id), action="preset", expected_version=body.expected_version,
        note=body.note, eval_run_id=body.eval_run_id, break_glass_reason=body.break_glass_reason, preset=preset))
    await _wm_reverify(request, x_admin_user_id)
    return out


@router.get("/versions", summary="The newest route config versions with who, when and the diff ($0)")
async def get_versions(request: Request, limit: int = Query(50, ge=1, le=200)) -> dict:
    return await _guarded(rc.list_versions(_pool(request), limit))


@router.get("/versions/{version}", summary="One route config version with its snapshot ($0)")
async def get_version(request: Request, version: int = Path(..., ge=1)) -> dict:
    out = await _guarded(rc.read_version(_pool(request), version))
    if out is None:
        raise HTTPException(status_code=404, detail=f"no route config version {version}")
    return out


@router.post("/versions/{version}/revert", summary="Restore a route config version as a new version (audited)")
async def post_revert(body: _WriteMeta, request: Request, version: int = Path(..., ge=1),
                      x_admin_user_id: Optional[str] = Header(None)) -> dict:
    out = await _guarded(rc.apply(
        _pool(request), actor=_actor(x_admin_user_id), action="revert", expected_version=body.expected_version,
        note=body.note, eval_run_id=body.eval_run_id, break_glass_reason=body.break_glass_reason,
        revert_to=version))
    await _wm_reverify(request, x_admin_user_id)
    return out
