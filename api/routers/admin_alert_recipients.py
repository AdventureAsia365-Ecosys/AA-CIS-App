"""AA-913 — Platform › People & access › Alert recipients: who gets the platform alert e-mails.

  GET    /admin/platform/alert-recipients?refresh=false        list (reconciles with SNS first, throttled)
  POST   /admin/platform/alert-recipients                      add {email, note?}: AWS sends the confirmation e-mail
  POST   /admin/platform/alert-recipients/refresh              reconcile now, returns the GET body
  POST   /admin/platform/alert-recipients/test-email           queue a test ops e-mail (SES path)
  DELETE /admin/platform/alert-recipients/{recipient_id}       remove (unsubscribes from SNS)
  POST   /admin/platform/alert-recipients/{recipient_id}/resend  send the AWS confirmation again (pending only)

Everyone on the list gets every alert of the SNS alerts topic (CloudWatch alarms of all apps, security alerts, cost
anomalies, AWS Budgets) and the CIS spend e-mails. The Terraform bootstrap recipient (ALERT_BOOTSTRAP_EMAIL) is
shown locked and never changed here.

Who may call (api/core/staff_authz.py POLICY): the GET needs ``ops.read`` (admin); every POST / DELETE needs
``alerts.write`` (admin). Because the topic also carries security alerts, add / remove / resend / test additionally
need a verified Cognito session (the staff bearer middleware's identity): a legacy password session, a forged
x-admin-role or the machine gets 403. The status refresh only mirrors SNS, so an admin password session may run it.
The staff-audit middleware logs every non-GET call; the service adds ``alert_recipient.*`` audit rows with what the
HTTP row lacks (e-mail, SNS outcome).

Errors: ``{detail, code}`` with the HTTP status of api/services/alert_recipients.AlertRecipientError (422 invalid
address, 409 bootstrap / duplicate / full / not pending, 404, 429 + Retry-After, 502 SNS refused, 503 not
configured). A 429 also carries ``retry_after_s`` in the body: the admin BFF proxy forwards only Content-Type, so the
browser never sees the Retry-After header. Nothing returns the topic or subscription ARN; the page gets the topic name.

The router is included without an include prefix (staff_policy reads the route template from scope["route"].path).
"""
from __future__ import annotations

import uuid
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from api.core.staff_authz import request_staff_identity, staff_policy
from api.routers.admin import verify_admin_secret
from api.services import alert_recipients as svc
from shared import alert_recipients as ar

router = APIRouter(prefix="/admin/platform", tags=["admin-alert-recipients"],
                   dependencies=[Depends(verify_admin_secret), Depends(staff_policy)])

NEEDS_COGNITO = "Changing alert recipients needs a company sign-in (Cognito); password sessions cannot"


def get_sns() -> Optional[Any]:
    """The SNS client for the configured topic, or None when ALERTS_TOPIC_ARN is unset. Tests override it."""
    cfg = ar.topic_config()
    return ar.sns_client(cfg.region) if cfg is not None else None


class AddRecipientBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(..., max_length=320)
    note: Optional[str] = Field(None, max_length=200)


def _identity(request: Request) -> Any:
    identity = request_staff_identity(request)
    if identity is None or getattr(identity, "auth", None) != "cognito" or not getattr(identity, "admin_id", None):
        return None
    return identity


def _actor(request: Request) -> str:
    """'admin:<shared.admin_users.id>' of the verified Cognito caller; 403 otherwise (before any DB or AWS call)."""
    identity = _identity(request)
    if identity is None:
        raise HTTPException(status_code=403, detail=NEEDS_COGNITO)
    return f"admin:{identity.admin_id}"


def _actor_name(request: Request) -> Optional[str]:
    identity = _identity(request)
    if identity is None:
        return None
    for attr in ("display_name", "email", "username"):
        value = getattr(identity, attr, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _viewer(request: Request) -> dict:
    """For the UI only (the backend re-checks every change): may this session change the list?"""
    cognito = _identity(request) is not None
    return {"auth": "cognito" if cognito else "legacy", "can_write": cognito}


def _refused(exc: svc.AlertRecipientError) -> JSONResponse:
    body: dict[str, Any] = {"detail": exc.detail, "code": exc.code}
    retry_after = (exc.headers or {}).get("Retry-After")
    if retry_after is not None:
        body["retry_after_s"] = int(retry_after)
    return JSONResponse(body, status_code=exc.status, headers=exc.headers)


def _redis(request: Request) -> Any:
    return getattr(request.app.state, "redis", None)


def _uuid_or_404(recipient_id: str) -> str:
    try:
        return str(uuid.UUID(recipient_id))
    except ValueError:
        raise HTTPException(status_code=404, detail="Recipient not found") from None


async def _page(request: Request, sns: Any, *, refresh: bool, actor: str) -> dict:
    body = await svc.page(request.app.state.pool, sns, redis=_redis(request), refresh=refresh, actor=actor)
    body["viewer"] = _viewer(request)
    return body


# Literal paths (refresh, test-email) before the /{recipient_id} routes of the same method.

@router.get("/alert-recipients", summary="Platform alert recipients (reconciled with SNS)")
async def list_alert_recipients(request: Request, refresh: bool = False, sns: Any = Depends(get_sns)):
    identity = _identity(request)
    actor = f"admin:{identity.admin_id}" if identity is not None else ar.ACTOR_RECONCILE
    return await _page(request, sns, refresh=refresh, actor=actor)


@router.post("/alert-recipients/refresh", summary="Reconcile with SNS now")
async def refresh_alert_recipients(request: Request, sns: Any = Depends(get_sns)):
    identity = _identity(request)
    actor = f"admin:{identity.admin_id}" if identity is not None else ar.ACTOR_RECONCILE
    return await _page(request, sns, refresh=True, actor=actor)


@router.post("/alert-recipients/test-email", status_code=202, summary="Queue a test ops e-mail")
async def queue_test_alert_email(request: Request):
    actor = _actor(request)
    try:
        return await svc.send_test(request.app.state.pool, actor=actor, requested_by=_actor_name(request))
    except svc.AlertRecipientError as exc:
        return _refused(exc)


@router.post("/alert-recipients", status_code=201, summary="Add a recipient (AWS sends a confirmation)")
async def add_alert_recipient(body: AddRecipientBody, request: Request, sns: Any = Depends(get_sns)):
    actor = _actor(request)
    try:
        return await svc.add(request.app.state.pool, sns, actor=actor, email=body.email, note=body.note,
                             redis=_redis(request))
    except svc.AlertRecipientError as exc:
        return _refused(exc)


@router.delete("/alert-recipients/{recipient_id}", summary="Remove a recipient (unsubscribe)")
async def remove_alert_recipient(recipient_id: str, request: Request, sns: Any = Depends(get_sns)):
    actor = _actor(request)
    rid = _uuid_or_404(recipient_id)
    try:
        return await svc.remove(request.app.state.pool, sns, actor=actor, recipient_id=rid, redis=_redis(request))
    except svc.AlertRecipientError as exc:
        return _refused(exc)


@router.post("/alert-recipients/{recipient_id}/resend", summary="Send the AWS confirmation again")
async def resend_alert_confirmation(recipient_id: str, request: Request, sns: Any = Depends(get_sns)):
    actor = _actor(request)
    rid = _uuid_or_404(recipient_id)
    try:
        return await svc.resend(request.app.state.pool, sns, actor=actor, recipient_id=rid)
    except svc.AlertRecipientError as exc:
        return _refused(exc)
