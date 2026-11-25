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
  A2 (predicted against measured; no model, $0):
  POST /admin/ops-assist/runs                   mark one card as done (the person ran its call or its runbook): the
                                                card is rebuilt here, its measures read as the baseline; the status
                                                sampler checks them at the minutes of cis.ops.assist_check_after_min
  GET  /admin/ops-assist/runs                   the newest runs (of one item) with their checks and verdicts, and
                                                the card lifetime the page matches them to a card with
  GET  /admin/ops-assist/track                  the hit rate of the last cis.ops.assist_digest_days days, per action

Every route is ops.read (the status page's capability: admin, per the role matrix): the A1 POST routes call a model
within the stage's hard-stop month cap and write nothing but the gateway ledger and a Redis cache; POST /runs writes
the run record only (acp_shared.ops_assist_run) and needs the card's own capability (the capability of the API the
card calls, or ops.write for a step with no API). A read-only session gets 403 on every POST (staff_policy). Each
evidence read is bounded by cis.ops.assist_build_timeout_s: past it the answer is 503 ``assistant_timeout`` with a
"try again" message (fail soft, never a hang). The business owner
(``shared.admin_users.is_business_owner``) gets the Summary only, no cards (tech lead AI10). See
``api/services/ops_assist/__init__.py``.
"""
from __future__ import annotations

from typing import Optional

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from api.core.staff_authz import staff_policy
from api.core.tenant_scope import principal_of, staff_id_of
from api.routers.admin import verify_admin_secret
from api.services import ops_assist
from api.services.ops_assist import analysis, evidence, lookup, outcome, replays

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


TIMEOUT_MESSAGE = ("The assistant could not read the evidence in time (a deploy may be replacing the API). "
                   "Try again in a minute.")
NOT_SET_UP_MESSAGE = "The assistant's run record is not set up yet on this environment"


async def _soft(coro):
    """The answer, or 503 with a clear "try again" message when the evidence read passed
    cis.ops.assist_build_timeout_s or failed outright (S292: one 503 and one hang during a deploy)."""
    try:
        return await coro
    except ops_assist.EvidenceTimeout:
        logger.warning("ops_assist_evidence_timeout")
        raise HTTPException(status_code=503, detail={"code": "assistant_timeout", "message": TIMEOUT_MESSAGE,
                                                     "retry": True})


@router.get("/catalog", summary="The assistant's action catalogue (read-only)")
async def get_catalog(x_admin_secret: str = Header(None)) -> dict:
    verify_admin_secret(x_admin_secret)
    return ops_assist.catalogue()


@router.get("/signals", summary="What the assistant sees beyond the Needs-attention list")
async def get_signals(request: Request, x_admin_secret: str = Header(None)) -> dict:
    verify_admin_secret(x_admin_secret)
    return await _soft(ops_assist.list_signals(request.app.state.pool, getattr(request.app.state, "redis", None)))


@router.get("/items/{item_id}/cards", summary="Evidence, ranked action cards and Summary for one item (no new LLM "
                                              "call; the model's part saved within the card lifetime, if any)")
async def get_cards(
    request: Request,
    item_id: str = _ITEM_ID,
    range_: str = Query("24h", alias="range", pattern="^(1h|6h|24h|7d)$",
                        description="the status page's range (the item ids depend on it)"),
    x_admin_secret: str = Header(None),
) -> dict:
    verify_admin_secret(x_admin_secret)
    from api.services.platform_apps import relay_authorization

    out = await _soft(analysis.cached(request.app.state.pool, getattr(request.app.state, "redis", None), item_id,
                                      range_key=range_, authorization=relay_authorization(request),
                                      business_owner=await _is_business_owner(request)))
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

    out = await _soft(analysis.analyse(request.app.state.pool, getattr(request.app.state, "redis", None), item_id,
                                       range_key=range_, depth=depth, fresh=fresh,
                                       authorization=relay_authorization(request),
                                       business_owner=await _is_business_owner(request)))
    _no_item(out)
    return out


@router.post("/auto", summary="Analyse each open incident of an automatic severity once (the status page calls it)")
async def post_auto(request: Request, range_: str = _RANGE, x_admin_secret: str = Header(None)) -> dict:
    verify_admin_secret(x_admin_secret)
    from api.services.platform_apps import relay_authorization

    return await _soft(analysis.auto(request.app.state.pool, getattr(request.app.state, "redis", None),
                                     range_key=range_, authorization=relay_authorization(request)))


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


# ── A2: predicted against measured ────────────────────────────────────────────────────────────────────────────────

class RunBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_id: str = Field(..., pattern=r"^[A-Za-z0-9_.:-]{1,200}$")
    card_id: str = Field(..., min_length=1, max_length=300)
    range: str = Field("24h", pattern="^(1h|6h|24h|7d)$")


def may_record(perms: frozenset, card: dict) -> bool:
    """Only someone who could have run the card marks it as done: one of the capabilities of the API it calls, or
    ops.write for a step with no API (watch, a copy-only runbook). Pure."""
    caps = (card.get("approval") or {}).get("capability") or []
    return any(c in perms for c in caps) if caps else "ops.write" in perms


@router.post("/runs", summary="Mark one action card as done; its outcome is checked later (no model, $0)")
async def post_run(body: RunBody, request: Request, x_admin_secret: str = Header(None)) -> dict:
    verify_admin_secret(x_admin_secret)
    from api.routers.admin_overview import _actor
    from api.services.platform_apps import relay_authorization

    if await _is_business_owner(request):
        raise HTTPException(status_code=403, detail={"code": "business_owner_reads_only",
                                                     "message": "The business owner reads the Summary only"})
    pool = request.app.state.pool
    answer = await _soft(ops_assist.analyse(pool, getattr(request.app.state, "redis", None), body.item_id,
                                            range_key=body.range, authorization=relay_authorization(request)))
    _no_item(answer)
    card = next((c for c in answer.get("cards") or [] if c["id"] == body.card_id), None)
    if card is None:
        raise HTTPException(status_code=409, detail={"code": "card_gone", "message": "This card is no longer "
                                                     "proposed for the item; analyse it again"})
    if not may_record(principal_of(request).perms, card):
        raise HTTPException(status_code=403, detail={"code": "not_your_action", "message": "Only someone allowed to "
                                                     "run this card's action can mark it as done"})
    try:
        return await outcome.record(pool, answer, card, actor=_actor(request), limits=answer["limits"])
    except outcome.AlreadyRecorded:
        raise HTTPException(status_code=409, detail={"code": "already_recorded", "message": "This card was already "
                                                     "marked as done"})
    except outcome.NoCheckpoint:
        raise HTTPException(status_code=409, detail={"code": "no_checkpoint", "message": "No checkpoint is set; set "
                                                     "cis.ops.assist_check_after_min on Platform › Settings"})
    except outcome.NotSetUp:
        raise HTTPException(status_code=503, detail={"code": "not_set_up", "message": NOT_SET_UP_MESSAGE})


@router.get("/runs", summary="The newest action cards marked as done, with their checks (no model, $0)")
async def get_runs(request: Request, item_id: Optional[str] = Query(None, pattern=r"^[A-Za-z0-9_.:-]{1,200}$"),
                   x_admin_secret: str = Header(None)) -> dict:
    verify_admin_secret(x_admin_secret)
    try:
        rows = await outcome.runs(request.app.state.pool, item_id)
    except outcome.NotSetUp:
        raise HTTPException(status_code=503, detail={"code": "not_set_up", "message": NOT_SET_UP_MESSAGE})
    lim = ops_assist.limits()
    # card_ttl_min: the page shows a run under a card only when it was recorded within that card's lifetime (the same
    # card id comes back when the item recurs; an older run is history and the card can be marked as done again).
    return {"runs": rows, "check_after_min": lim["check_after_min"], "card_ttl_min": lim["card_ttl_min"]}


@router.get("/track", summary="The assistant's hit rate: right n of m checked cards (no model, $0)")
async def get_track(request: Request, x_admin_secret: str = Header(None)) -> dict:
    verify_admin_secret(x_admin_secret)
    lim = ops_assist.limits()
    try:
        return await outcome.track(request.app.state.pool, days=lim["digest_days"])
    except outcome.NotSetUp:
        raise HTTPException(status_code=503, detail={"code": "not_set_up", "message": NOT_SET_UP_MESSAGE})
