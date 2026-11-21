"""AA-1155 (spec tasks T13 / T14) — Platform › People & access › Staff: the CIS role editor and the preset editor.

  GET   /admin/platform/roles/cis               the editor: built-in CIS roles (read-only), custom roles with their
                                                active holders, every capability (assignable or not), can_edit
  POST  /admin/platform/roles/cis               clone a built-in role into a custom role (owners only, audited)
  PATCH /admin/platform/roles/cis/{code}        label, capabilities (the full set), archived (owners only, audited
                                                before and after)
  GET   /admin/platform/presets                 the Staff page's people-role presets (shared.staff_role_preset, CIS
                                                465) + can_edit
  POST  /admin/platform/presets                 a new preset (owners only, audited)
  PATCH /admin/platform/presets/{code}          the fields sent of one preset; ``archived`` retires it (owners only,
                                                audited before and after)

Reads: ``staff.read`` (the route policy). Writes: ``staff.write`` (an owner on a Cognito session), re-checked in the
transaction (api/services/staff_directory._require_owner). The rules live in api/services/staff_role_editor.py and
api/services/staff_presets.py; a refusal answers its status with the service's detail. No body takes an owner flag:
every body forbids extra fields (a preset never sets is_owner, root spec R5.7).

Included without an include prefix (staff_policy reads the route template from scope["route"].path).
"""
from __future__ import annotations

from typing import Any, Literal, Optional, Union

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from typing_extensions import Annotated

from api.core import staff_roles
from api.core.staff_authz import request_staff_identity, staff_policy
from api.routers.admin import verify_admin_secret
from api.services import staff_presets, staff_role_editor
from api.services.staff_directory import StaffDirectoryError

router = APIRouter(prefix="/admin/platform", tags=["admin-platform"],
                   dependencies=[Depends(verify_admin_secret), Depends(staff_policy)])

CustomRoleCode = Annotated[str, StringConstraints(pattern=staff_roles.CUSTOM_CODE_RE.pattern)]
PresetCode = Annotated[str, StringConstraints(pattern=staff_presets.PRESET_CODE_RE.pattern)]
CisRole = Union[Literal["admin", "content_lead", "content", "tenant_qa", "customer_success", "support", "finance",
                        "ai_ops"], CustomRoleCode]
BookingRole = Union[Literal["admin", "sales", "contracting", "finance"], CustomRoleCode]
CrmRole = Literal["platform_admin", "ops", "support"]
_CAPS_MAX = 200   # technical-constant: bounds a request body list (the capability catalogue is far smaller)


def _viewer_is_owner(request: Request) -> bool:
    identity = request_staff_identity(request)
    return bool(identity is not None and getattr(identity, "auth", None) == "cognito"
                and getattr(identity, "is_owner", False))


def _actor_id(request: Request) -> str:
    admin_id = getattr(request_staff_identity(request), "admin_id", None)
    if not admin_id:   # staff_policy already refused non-Cognito callers; never write without a verified actor
        raise HTTPException(status_code=403, detail="Only a platform owner can change staff roles")
    return admin_id


def _refused(exc: StaffDirectoryError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=exc.detail, headers=exc.headers)


# ── CIS role editor (T13, R5.6) ────────────────────────────────────────────────────────────────────────────────

class CreateRoleBody(BaseModel):
    """Clone ``based_on`` (a built-in CIS role, never admin or viewer) into ``code``; ``capabilities`` replaces the
    cloned set when given."""
    model_config = ConfigDict(extra="forbid")
    code: CustomRoleCode
    label: str = Field(..., max_length=staff_role_editor.LABEL_MAX)
    based_on: str = Field(..., max_length=32)
    capabilities: Optional[list[str]] = Field(default=None, max_length=_CAPS_MAX)


class UpdateRoleBody(BaseModel):
    """Only the fields sent change; ``capabilities`` is the full new set; ``archived`` retires or restores the role."""
    model_config = ConfigDict(extra="forbid")
    label: Optional[str] = Field(default=None, max_length=staff_role_editor.LABEL_MAX)
    capabilities: Optional[list[str]] = Field(default=None, max_length=_CAPS_MAX)
    archived: Optional[bool] = None


@router.get("/roles/cis", summary="The CIS role editor: built-in and custom roles, capabilities")
async def cis_role_editor(request: Request):
    async with request.app.state.pool.acquire() as conn:
        return await staff_role_editor.editor_view(conn, can_edit=_viewer_is_owner(request))


@router.post("/roles/cis", status_code=201, summary="Create a CIS custom role from a built-in one (owners, audited)")
async def create_cis_role(body: CreateRoleBody, request: Request):
    try:
        return await staff_role_editor.create_role(
            request.app.state.pool, actor_id=_actor_id(request), code=body.code, label=body.label,
            based_on=body.based_on, capabilities=body.capabilities)
    except StaffDirectoryError as exc:
        raise _refused(exc) from None


@router.patch("/roles/cis/{code}", summary="Relabel, set the capabilities of or archive a CIS custom role")
async def update_cis_role(code: str, body: UpdateRoleBody, request: Request):
    sent = body.model_fields_set
    try:
        return await staff_role_editor.update_role(
            request.app.state.pool, actor_id=_actor_id(request), code=code,
            label=body.label if "label" in sent else None,
            capabilities=body.capabilities if "capabilities" in sent else None,
            archived=body.archived if "archived" in sent else None)
    except StaffDirectoryError as exc:
        raise _refused(exc) from None


# ── People-role presets (T14, R5.7) ────────────────────────────────────────────────────────────────────────────

class PresetFields(BaseModel):
    """The editable fields of a preset (never an owner flag)."""
    model_config = ConfigDict(extra="forbid")
    label: Optional[str] = Field(default=None, max_length=staff_presets.LABEL_MAX)
    description: Optional[str] = Field(default=None, max_length=staff_presets.DESCRIPTION_MAX)
    cis_role: Optional[CisRole] = None
    booking_mode: Optional[Literal["keep", "set", "clear"]] = None
    booking_role: Optional[BookingRole] = None
    crm_mode: Optional[Literal["keep", "replace", "add"]] = None
    crm_roles: Optional[list[CrmRole]] = Field(default=None, max_length=3)
    additive_only: Optional[bool] = None
    cis_keep_roles: Optional[list[CisRole]] = Field(default=None, max_length=staff_presets.KEEP_MAX)
    booking_keep_roles: Optional[list[BookingRole]] = Field(default=None, max_length=staff_presets.KEEP_MAX)
    sort_order: Optional[int] = Field(default=None, ge=0, le=staff_presets.SORT_MAX)
    archived: Optional[bool] = None


class CreatePresetBody(PresetFields):
    code: PresetCode
    label: str = Field(..., max_length=staff_presets.LABEL_MAX)


def _sent(body: PresetFields, *, keep_nulls: tuple[str, ...]) -> dict[str, Any]:
    """The fields sent; a null is kept only where it means something (cis_role / booking_role: none)."""
    out: dict[str, Any] = {}
    for key in body.model_fields_set:
        value = getattr(body, key)
        if value is None and key not in keep_nulls:
            continue
        out[key] = value
    return out


@router.get("/presets", summary="The Staff page's people-role presets")
async def list_presets(request: Request):
    async with request.app.state.pool.acquire() as conn:
        presets = await staff_presets.list_presets(conn)
    return {"presets": [p.as_dict() for p in presets or []], "available": presets is not None,
            "can_edit": _viewer_is_owner(request)}


@router.post("/presets", status_code=201, summary="Create a people-role preset (owners, audited)")
async def create_preset(body: CreatePresetBody, request: Request):
    try:
        preset = await staff_presets.create_preset(
            request.app.state.pool, actor_id=_actor_id(request),
            fields=_sent(body, keep_nulls=("cis_role", "booking_role")))
    except StaffDirectoryError as exc:
        raise _refused(exc) from None
    return preset.as_dict()


@router.patch("/presets/{code}", summary="Change a people-role preset (owners, audited before and after)")
async def update_preset(code: str, body: PresetFields, request: Request):
    try:
        preset = await staff_presets.update_preset(
            request.app.state.pool, actor_id=_actor_id(request), code=code,
            fields=_sent(body, keep_nulls=("cis_role", "booking_role")))
    except StaffDirectoryError as exc:
        raise _refused(exc) from None
    return preset.as_dict()
