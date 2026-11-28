"""AA Academy learning API (AA-1105, root ADR 0022, contract learn.http.v1; a byte copy in
docs/contracts/learn.http.v1.openapi.yaml).

  GET  /learn/v1/me                                  the learner (created on first call)
  GET  /learn/v1/paths                               paths with enrollment, progress and certificate state
  POST /learn/v1/paths/{path_id}/enroll              201 enrolled / 200 already
  GET  /learn/v1/modules/{module_id}                 module, lessons, quiz and lab state
  GET  /learn/v1/lessons/{lesson_id}?lang=en|vi      lesson body and its live cited sections
  GET  /learn/v1/lessons/{lesson_id}/video           presigned video + caption URLs (cis.learn.video_url_ttl_seconds)
  POST /learn/v1/progress                            learning statement (idempotent on statement_id)
  POST /learn/v1/modules/{module_id}/quiz/attempts   module quiz attempt (items without answers)
  POST /learn/v1/paths/{path_id}/final-quiz/attempts the path's final mixed quiz
  POST /learn/v1/quiz/attempts/{attempt_id}/submit   server grading, review cards, certificate
  GET  /learn/v1/review/due                          due review cards (cis.learn.review_max_cards_per_day)
  POST /learn/v1/review/{item_id}/result             graded review, ts-fsrs card stored after checks
  POST /learn/v1/modules/{module_id}/lab/check       409 lab_closed until the lab provisioner (P4b)
  GET  /learn/v1/certificates                        the learner's certificates
  POST /learn/v1/certificates/{id}/code              a new verification code (the old one stops working)
  GET  /learn/public/v1/certificates/{code}          PUBLIC verify view, rate limited per client address

Who may (decisions 6 and 7): ``/learn/v1/*`` takes only a WORKFORCE access token of the ``academy-web`` client
(``api/core/learn_identity.py``, its own verifier): none / bad -> 401 ``unauthorized``; another client or pool -> 403
``forbidden``; a partner-pool token -> 403 ``partner_not_open`` (P7). The learner is the ACTIVE ``shared.admin_users``
row bound to the verified ``(iss, sub)``, any staff role; no row -> 403 ``not_a_learner``. A Dev-only E2E identity is a
test learner (``is_test``) and refused outside Dev (403 ``forbidden``); while the pilot fence is on or unread
(AA-1054, ``api/core/pilot_fence.py``) every address of the E2E mailbox domain is refused 403 ``forbidden``. The
certificate name is the directory's display name, refreshed on every call, never a name the client sends (S280
review). Draft modules need
``learn.preview`` (``staff_authz.ROLE_PERMS`` of the token's ``cis-*`` group role).

Public verify rate limit (S280 review): the key is the caller's address as ``login_throttle.client_ip`` reads it, the
RIGHTMOST globally routable X-Forwarded-For entry. The edge is API Gateway (REST, appends the real source address) ->
VPC link NLB (TCP, adds nothing) -> ALB (appends the NLB's private address) -> ECS, so the rightmost global entry is
the address API Gateway saw; anything a caller writes lands to its left. The academy web calls this route from its
server, so behind it every visitor shares the web's NAT address: the web limits its own visitors, and this limit caps
the whole host. The limit is the setting ``cis.learn.verify_requests_per_minute`` (one fixed window per minute in
Redis; an in-process window when Redis is unavailable). Every verify answer (200, 404, 429, 503) carries
``X-Learn-Visitor-Limit`` (contract 1.1): the per-visitor limit such a caller must apply, the setting
``cis.learn.verify_visitor_requests_per_minute`` clamped to the per-address limit minus one, so one visitor of the web
can never use up the address limit the web's visitors share.

Errors: the contract's Problem body ``{error, message, retry_at}``; FastAPI's own 422 shape is never used (bodies and
path values are checked here). The course is read from the learn bundle (503 ``unavailable`` when none is published).
"""
from __future__ import annotations

import re
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Awaitable, Callable, Optional

import asyncpg
import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError

from api.core import e2e_staff, learn_identity, login_throttle, pilot_fence
from api.core.identity_verifier import AuthUnavailable, InvalidToken, Principal
from api.core.m2m_auth import bearer_token
from api.core.staff_authz import LEARN_PREVIEW, ROLE_PERMS
from api.core.staff_identity import StaffAuthError, role_from_groups
from api.schemas import learn as m
from services.learn import academy as ac
from services.learn import assessment as qa
from services.learn import bundle as lb
from services.learn import grading as g
from services.learn.store import PgStore
from shared import settings

logger = structlog.get_logger()

ACQUIRE_TIMEOUT_S = 5.0  # technical-constant: pool acquire bound, as the other HTTP contracts
NO_STORE = {"Cache-Control": "private, no-store"}
UNAUTHORIZED = {"WWW-Authenticate": 'Bearer realm="learn"'}
PATH_ID = re.compile(r"^[a-z][a-z0-9-]{1,40}$")
MODULE_ID = re.compile(r"^[A-Z][0-9]{1,2}$")
LESSON_ID = re.compile(r"^[A-Z][0-9]{1,2}-[0-9]{2}$")
ITEM_ID = re.compile(r"^[A-Z][0-9]{1,2}-Q[0-9]{1,3}$")
UUID_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
CODE = re.compile(r"^[A-Za-z0-9_-]{22}$")
_DB_DOWN = (asyncpg.UndefinedTableError, asyncpg.InvalidSchemaNameError, asyncpg.PostgresConnectionError,
            asyncpg.InterfaceError, asyncpg.TooManyConnectionsError, OSError, TimeoutError)
LOCAL_WINDOW_MAX = 10_000  # technical-constant: keys kept by the in-process verify window before it is reset
_window: dict[str, int] = {}   # in-process verify window when Redis is unavailable
VISITOR_LIMIT_HEADER = "X-Learn-Visitor-Limit"  # contract learn.http.v1 1.1 components/headers/VisitorLimit

Handler = Callable[[ac.Ctx], Awaitable[tuple[int, Any]]]


def problem(status: int, error: str, message: str, retry_at: Any = None,
            headers: Optional[dict] = None) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": error, "message": message, "retry_at": g.iso(retry_at)},
                        headers={**NO_STORE, **(headers or {})})


def _check(pattern: re.Pattern, value: str, what: str) -> None:
    if not pattern.match(value or ""):
        raise ac.not_found(what)


async def _principal(request: Request) -> Principal:
    token = bearer_token(request)
    if token is None:
        raise ac.Problem(401, "unauthorized", "An academy sign-in token is required.")
    try:
        return await learn_identity.verify_academy_token(token)
    except learn_identity.PartnerNotOpen:
        raise ac.Problem(403, "partner_not_open", "The agency academy is not open yet.") from None
    except learn_identity.AcademyCallerRefused:
        raise ac.Problem(403, "forbidden", "This sign-in cannot open the academy.") from None
    except InvalidToken:
        raise ac.Problem(401, "unauthorized", "The sign-in token is not valid.") from None
    except (AuthUnavailable, ValueError) as exc:
        logger.error("learn_identity_unavailable", error=type(exc).__name__)
        raise ac.Problem(503, "unavailable", "Sign-in verification is unavailable; retry later.") from None


async def require_academy_token(request: Request) -> Principal:
    """Router dependency of /learn/v1 (the route-auth scan, tests/unit/test_s241_admin_routes_require_auth.py, sees
    it): the verified academy-web principal, kept on request.state; a refusal raises :class:`ac.Problem`, which
    :func:`problem_handler` answers with the contract's Problem body."""
    principal = await _principal(request)
    request.state.learn_principal = principal
    return principal


async def problem_handler(_request: Request, exc: ac.Problem) -> JSONResponse:
    """App exception handler (api/main.py) for a refusal raised by :func:`require_academy_token`."""
    return problem(exc.status, exc.error, exc.message, exc.retry_at, UNAUTHORIZED if exc.status == 401 else None)


router = APIRouter(prefix="/learn", tags=["learn"], dependencies=[Depends(require_academy_token)])
public_router = APIRouter(prefix="/learn", tags=["learn"])


async def learner_ctx(store: Any, principal: Principal) -> ac.Ctx:
    """The staff learner behind a verified academy token (decision 7), its row refreshed from the directory."""
    staff = await store.staff_row(principal.iss, principal.sub)
    if staff is None:
        logger.info("learn_refused", reason="not_a_learner", client_id=principal.client_id)
        raise ac.Problem(403, "not_a_learner", "This account is not an active staff member.")
    # AA-1054: the pilot fence holds here too (this path does not go through staff_identity.resolve_staff): while it is
    # on or cannot be read, every E2E identity (A10 included, which is_dev_only_e2e_address below does not mark) is
    # refused before a learner row is written. The contract's error code stays "forbidden".
    try:
        await pilot_fence.check(row_email=staff.get("email"), token_email=principal.email,
                                admin_id=staff.get("admin_id"), sub=principal.sub, client_id=principal.client_id)
    except StaffAuthError as exc:
        logger.info("learn_refused", reason=exc.reason, client_id=principal.client_id)
        raise ac.Problem(403, "forbidden", "This test account is closed while the pilot runs.") from None
    # AA-1176: the A10h E2E owner is a test learner too (Dev only), though its CIS session is not read-only.
    is_test = (e2e_staff.is_dev_only_e2e_address(staff.get("email"))
               or e2e_staff.is_dev_only_e2e_address(principal.email))
    if is_test and not e2e_staff.dev_only_e2e_environment():
        raise ac.Problem(403, "forbidden", "This test account is not allowed here.")
    learner = await store.upsert_learner(principal.iss, principal.sub, staff["admin_id"], staff.get("display_name"),
                                         is_test)
    role = role_from_groups(principal.groups) or staff.get("db_role")
    return ac.Ctx(learner=learner, preview=LEARN_PREVIEW in ROLE_PERMS.get(role or "", frozenset()), store=store)


@asynccontextmanager
async def _pg_store(request: Request) -> AsyncIterator[Any]:
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        raise ac.Problem(503, "unavailable", "The academy is unavailable; retry later.")
    async with pool.acquire(timeout=ACQUIRE_TIMEOUT_S) as conn:
        async with conn.transaction():
            yield PgStore(conn)


# Tests replace this with an in-memory store factory (tests/unit/aa1105_learn_fake.py).
open_store: Callable[[Request], Any] = _pg_store


async def _answer(request: Request, handler: Handler, *, bundle: bool = True) -> JSONResponse:
    principal = request.state.learn_principal
    try:
        loaded = await lb.load() if bundle else None
        async with open_store(request) as store:
            ctx = await learner_ctx(store, principal)
            ctx.bundle = loaded
            status, body = await handler(ctx)
    except ac.Problem as exc:
        return await problem_handler(request, exc)
    except lb.LearnUnavailable as exc:
        return problem(503, "unavailable", str(exc), headers={"Retry-After": "30"})
    except _DB_DOWN as exc:
        logger.error("learn_db_unavailable", error=type(exc).__name__)
        return problem(503, "unavailable", "The academy is unavailable; retry later.", headers={"Retry-After": "5"})
    return JSONResponse(status_code=status, content=body, headers=NO_STORE)


async def _body(request: Request, model: type[BaseModel]) -> dict:
    try:
        return model.model_validate_json(await request.body()).model_dump(mode="json")
    except (ValidationError, ValueError):
        raise ac.Problem(422, "invalid", "The request body does not match the contract.") from None


def _ok(model: type[BaseModel], status: int = 200) -> dict:
    return {status: {"model": model}}


# ── learner routes ────────────────────────────────────────────────────────────────────────────────────────────────

@router.get("/v1/me", responses=_ok(m.Me), summary="The signed-in learner")
async def get_me(request: Request) -> JSONResponse:
    async def run(ctx: ac.Ctx):
        return 200, ac.me(ctx)
    return await _answer(request, run, bundle=False)


@router.get("/v1/paths", responses=_ok(m.PathList), summary="Learning paths with progress")
async def list_paths(request: Request) -> JSONResponse:
    async def run(ctx: ac.Ctx):
        return 200, await ac.list_paths(ctx)
    return await _answer(request, run)


@router.post("/v1/paths/{path_id}/enroll", responses={**_ok(m.Enrollment), **_ok(m.Enrollment, 201)},
             summary="Enroll in a path (idempotent)")
async def enroll_path(request: Request, path_id: str) -> JSONResponse:
    async def run(ctx: ac.Ctx):
        _check(PATH_ID, path_id, "The path")
        body, created = await ac.enroll(ctx, path_id)
        return (201 if created else 200), body
    return await _answer(request, run)


@router.get("/v1/modules/{module_id}", responses=_ok(m.Module), summary="One module with the learner's state")
async def get_module(request: Request, module_id: str) -> JSONResponse:
    async def run(ctx: ac.Ctx):
        _check(MODULE_ID, module_id, "The module")
        return 200, await ac.get_module_view(ctx, module_id)
    return await _answer(request, run)


@router.get("/v1/lessons/{lesson_id}", responses=_ok(m.Lesson), summary="Lesson body and its cited sections")
async def get_lesson(request: Request, lesson_id: str, lang: str = "en") -> JSONResponse:
    async def run(ctx: ac.Ctx):
        _check(LESSON_ID, lesson_id, "The lesson")
        if lang not in ("en", "vi"):
            raise ac.Problem(422, "invalid", "lang must be en or vi.")
        return 200, await ac.get_lesson_view(ctx, lesson_id, lang)
    return await _answer(request, run)


@router.get("/v1/lessons/{lesson_id}/video", responses=_ok(m.VideoUrls), summary="Presigned lesson video URLs")
async def get_lesson_video(request: Request, lesson_id: str) -> JSONResponse:
    async def run(ctx: ac.Ctx):
        _check(LESSON_ID, lesson_id, "The lesson")
        return 200, await ac.get_video(ctx, lesson_id)
    return await _answer(request, run)


@router.post("/v1/progress", responses={**_ok(m.ProgressAccepted), **_ok(m.ProgressAccepted, 201)},
             summary="Record a learning event (idempotent on statement_id)")
async def post_progress(request: Request) -> JSONResponse:
    async def run(ctx: ac.Ctx):
        body, created = await ac.record_progress(ctx, await _body(request, m.ProgressStatementIn))
        return (201 if created else 200), body
    return await _answer(request, run)


@router.post("/v1/modules/{module_id}/quiz/attempts", status_code=201, responses=_ok(m.QuizAttempt, 201),
             summary="Start a module quiz attempt")
async def start_module_quiz(request: Request, module_id: str) -> JSONResponse:
    async def run(ctx: ac.Ctx):
        _check(MODULE_ID, module_id, "The module")
        return 201, await qa.start_module_quiz(ctx, module_id)
    return await _answer(request, run)


@router.post("/v1/paths/{path_id}/final-quiz/attempts", status_code=201, responses=_ok(m.QuizAttempt, 201),
             summary="Start the path's final quiz")
async def start_final_quiz(request: Request, path_id: str) -> JSONResponse:
    async def run(ctx: ac.Ctx):
        _check(PATH_ID, path_id, "The path")
        return 201, await qa.start_final_quiz(ctx, path_id)
    return await _answer(request, run)


@router.post("/v1/quiz/attempts/{attempt_id}/submit", responses=_ok(m.QuizResult), summary="Submit and grade")
async def submit_quiz(request: Request, attempt_id: str) -> JSONResponse:
    async def run(ctx: ac.Ctx):
        _check(UUID_ID, attempt_id, "The attempt")
        body = await _body(request, m.QuizSubmissionIn)
        return 200, await qa.submit(ctx, attempt_id, body["responses"])
    return await _answer(request, run)


@router.get("/v1/review/due", responses=_ok(m.ReviewDue), summary="Due spaced-review cards")
async def review_due(request: Request) -> JSONResponse:
    async def run(ctx: ac.Ctx):
        return 200, await qa.due_reviews(ctx)
    return await _answer(request, run)


@router.post("/v1/review/{item_id}/result", responses=_ok(m.ReviewResult), summary="Grade one review answer")
async def review_result(request: Request, item_id: str) -> JSONResponse:
    async def run(ctx: ac.Ctx):
        _check(ITEM_ID, item_id, "The review card")
        return 200, await qa.review_result(ctx, item_id, await _body(request, m.ReviewSubmissionIn))
    return await _answer(request, run)


@router.post("/v1/modules/{module_id}/lab/check", responses=_ok(m.LabResult), summary="Run the module's lab check")
async def lab_check(request: Request, module_id: str) -> JSONResponse:
    async def run(ctx: ac.Ctx):
        _check(MODULE_ID, module_id, "The module")
        return 200, await qa.lab_check(ctx, module_id)
    return await _answer(request, run)


@router.get("/v1/certificates", responses=_ok(m.CertificateList), summary="The learner's certificates")
async def list_certificates(request: Request) -> JSONResponse:
    async def run(ctx: ac.Ctx):
        return 200, await qa.list_certificates(ctx)
    return await _answer(request, run)


@router.post("/v1/certificates/{certificate_id}/code", responses=_ok(m.CertificateCode),
             summary="A new verification code for the learner's own valid certificate")
async def rotate_certificate_code(request: Request, certificate_id: str) -> JSONResponse:
    async def run(ctx: ac.Ctx):
        _check(UUID_ID, certificate_id, "The certificate")
        return 200, await qa.rotate_code(ctx, certificate_id)
    return await _answer(request, run)


# ── public verify ─────────────────────────────────────────────────────────────────────────────────────────────────

def _local_count(key: str) -> int:
    if len(_window) > LOCAL_WINDOW_MAX:
        _window.clear()
    _window[key] = _window.get(key, 0) + 1
    return _window[key]


async def verify_count(request: Request) -> int:
    """This minute's verify requests from the caller's address (Redis, else in process)."""
    minute = int(time.time() // 60)
    key = f"learn_verify:{login_throttle.client_ip(request)}:{minute}"
    redis = getattr(request.app.state, "redis", None)
    if redis is not None:
        try:
            count = await redis.incr(key)
            if count == 1:
                await redis.expire(key, 120)
            if isinstance(count, int) and not isinstance(count, bool):
                return count
        except Exception as exc:  # noqa: BLE001 - fall back to the in-process window
            logger.warning("learn_verify_redis_error", error=type(exc).__name__)
    return _local_count(key)


async def visitor_limit(per_address: int) -> int:
    """The per-visitor, per-minute limit a caller fronting many visitors from one address must apply: the setting,
    never more than the per-address limit minus one (and at least 1)."""
    visitor = int(await settings.aget("cis.learn.verify_visitor_requests_per_minute"))
    return max(1, min(visitor, per_address - 1))


@public_router.get("/public/v1/certificates/{code}", responses=_ok(m.CertificateVerify),
            summary="Public verify view of a certificate (rate limited, no sign-in)")
async def verify_certificate(request: Request, code: str) -> JSONResponse:
    limit = int(await settings.aget("cis.learn.verify_requests_per_minute"))
    visitor = {VISITOR_LIMIT_HEADER: str(await visitor_limit(limit))}
    if await verify_count(request) > limit:
        retry = 60 - int(time.time()) % 60
        logger.info("learn_verify_throttled", ip=login_throttle.client_ip(request))
        return problem(429, "rate_limited", "Too many certificate checks. Try again in a minute.",
                       headers={"Retry-After": str(retry), **visitor})
    if not CODE.match(code or ""):
        return problem(404, "not_found", "The certificate was not found.", headers=visitor)
    try:
        loaded = await lb.load()
    except lb.LearnUnavailable:
        loaded = None      # the stored row is enough; the status then ignores newer module versions
    try:
        async with open_store(request) as store:
            body = await qa.verify(store, code, loaded)
    except ac.Problem as exc:
        return problem(exc.status, exc.error, exc.message, headers=visitor)
    except _DB_DOWN as exc:
        logger.error("learn_db_unavailable", error=type(exc).__name__)
        return problem(503, "unavailable", "The academy is unavailable; retry later.",
                       headers={"Retry-After": "5", **visitor})
    return JSONResponse(content=body, headers={"Cache-Control": "no-store", **visitor})
