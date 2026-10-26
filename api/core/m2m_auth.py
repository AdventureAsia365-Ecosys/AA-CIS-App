"""Service-to-service tokens of the Cognito M2M pool ``aa-m2m-<env>`` on CIS (root ADR 0006b decision 7, AA-976).

CIS serves two contracts to the partner CRM (AA-Partner), which holds no grant on any CIS schema (root ADR 0008a
decision 1): ``catalog.http.v1`` (``/catalog/v1``, ``api/routers/catalog_http.py``, scope ``cis.catalog/read``) and
``tenant.membership.v1`` (``/tenant/membership/v1``, ``api/routers/tenant_membership.py``, scope
``cis.tenant/read``, AA-978). Both route classes accept exactly one kind of caller: the M2M pool issuer x the
AA-Partner (CRM) app client, OAuth client-credentials access tokens carrying the route class's scope. Each router
depends on :func:`require_m2m` with its scope (a token holding only the other scope gets 403).

Same pattern as AA-Booking ``app/auth/m2m.py`` (AA-972): the shared multi-issuer verifier
(``api/core/identity_verifier.py``, never edited here) with a Cognito access-token profile whose "groups" claim is
``scope`` (a client-credentials token carries ``scope`` as one space-separated string; the verifier returns it as a
one-element ``groups`` tuple and :func:`scopes_of` splits it). The token is decoded and checked once, by the shared
module.

Outcomes of :func:`verify_caller`:

- no key set ever loaded for the pool -> :class:`~api.core.identity_verifier.AuthUnavailable` (503);
- malformed, expired, badly signed, wrong issuer, ID token -> :class:`~api.core.identity_verifier.InvalidToken` (401);
- validly signed by the pool but for a client CIS does not accept, or a token whose ``sub`` is not its client id
  (not a client-credentials token) -> :class:`CallerRefused` (403). The verifier raises
  ``InvalidToken("client not allowed")`` only after the signature, issuer, expiry and ``token_use`` checks passed, so
  that message alone means "authenticated, not allowed" (``tests/unit/test_aa976_m2m_auth.py`` runs the real
  verifier and fails if the message changes).

Configuration follows ``api/core/identity_config.py``: Dev values are code constants with an env override, so the ECS
task definition needs no new variable. Pool and client ids are public values (they appear in every token); the
client secret lives only with the caller (AA-Partner, Secrets Manager) and never in CIS.

- ``COGNITO_M2M_ISSUER``          default :data:`DEV_M2M_ISSUER` (the pool issuer, never the prefix domain)
- ``COGNITO_M2M_CRM_CLIENT_ID``   default :data:`DEV_M2M_CRM_CLIENT_ID` (AA-CIS-Infra ``identity_m2m.tf`` m2m_crm)

The M2M issuer must differ from the WORKFORCE issuer (one verifier per route class: a staff token must never satisfy
the M2M check); :func:`m2m_issuer_config` raises ``ValueError`` otherwise and every catalog route answers 503 (fail
closed).
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Optional

import structlog

from fastapi import Request
from fastapi.responses import JSONResponse

from api.core.identity_config import workforce_issuer
from api.core.identity_verifier import (
    AuthUnavailable,
    InvalidToken,
    IssuerConfig,
    JwksFetcher,
    MultiIssuerVerifier,
    Principal,
)

log = structlog.get_logger(__name__)

M2M = "m2m"

DEV_M2M_ISSUER = "https://cognito-idp.us-west-1.amazonaws.com/us-west-1_QZ9xcik2F"
DEV_M2M_CRM_CLIENT_ID = "1lstqvrbqc4sgrbhf3143cqot7"

ENV_M2M_ISSUER = "COGNITO_M2M_ISSUER"
ENV_M2M_CRM_CLIENT_ID = "COGNITO_M2M_CRM_CLIENT_ID"

# Resource server "cis.catalog" of the M2M pool (AA-CIS-Infra identity_m2m.tf); scope string as in the contract
# catalog.http.v1 components.securitySchemes.m2m.
SCOPE_CATALOG_READ = "cis.catalog/read"
# Resource server "cis.tenant" (AA-978, AA-CIS-Infra identity_m2m.tf); contract tenant.membership.v1.
SCOPE_TENANT_READ = "cis.tenant/read"
# AA-979 P2b: the CRM binds an accepted studio invitation's partner-pool identity (tenant.membership.v1 1.1,
# POST /tenant/membership/v1/invitations/{id}/activate). Granted to the CRM client only (AA-CIS-Infra identity_m2m.tf).
SCOPE_TENANT_ACTIVATE = "cis.tenant/activate"

# The shared verifier's message for a validly signed token of a client this route class does not accept.
CLIENT_NOT_ALLOWED = "client not allowed"

_verifier: Optional[MultiIssuerVerifier] = None


class CallerRefused(Exception):  # noqa: N818 - reads as an outcome, like InvalidToken
    """A validly signed M2M token that CIS does not accept here (HTTP 403)."""


@dataclass(frozen=True)
class M2MCaller:
    client_id: str
    scopes: frozenset[str]

    def has(self, scope: str) -> bool:
        return scope in self.scopes


def _env(name: str, default: str) -> str:
    value = (os.environ.get(name) or "").strip()
    return value or default


def m2m_issuer() -> str:
    return _env(ENV_M2M_ISSUER, DEV_M2M_ISSUER)


def m2m_crm_client_id() -> str:
    return _env(ENV_M2M_CRM_CLIENT_ID, DEV_M2M_CRM_CLIENT_ID)


def m2m_issuer_config() -> IssuerConfig:
    """The M2M pool x {CRM client}, Cognito access-token profile with ``scope`` as the groups claim."""
    issuer = m2m_issuer()
    if issuer == workforce_issuer():
        raise ValueError(f"{ENV_M2M_ISSUER} must differ from the WORKFORCE issuer")
    client = m2m_crm_client_id()
    if not client.isalnum():
        raise ValueError(f"{ENV_M2M_CRM_CLIENT_ID} must be a Cognito app client id (letters and digits)")
    return IssuerConfig(issuer=issuer, client_ids=frozenset({client}), groups_claim="scope", name=M2M)


def build_m2m_verifier(fetcher: JwksFetcher | None = None) -> MultiIssuerVerifier:
    return MultiIssuerVerifier((m2m_issuer_config(),), fetcher)


def get_m2m_verifier() -> MultiIssuerVerifier:
    """Process-wide verifier for the M2M route class (built on first use from the env at that time).

    Raises ``ValueError`` when the configuration is invalid; the router turns that into 503 (fail closed)."""
    global _verifier
    if _verifier is None:
        _verifier = build_m2m_verifier()
    return _verifier


def set_m2m_verifier(verifier: Optional[MultiIssuerVerifier]) -> None:
    """Install (tests) or drop (None) the process-wide verifier."""
    global _verifier
    _verifier = verifier


def scopes_of(principal: Principal) -> frozenset[str]:
    """Every scope of the token (the ``scope`` claim is one space-separated string; a list is accepted too)."""
    return frozenset(s for group in principal.groups for s in group.split() if s)


async def verify_caller(verifier: MultiIssuerVerifier, token: str) -> M2MCaller:
    """Raises InvalidToken (401), CallerRefused (403) or AuthUnavailable (503)."""
    try:
        principal = await verifier.verify(token)
    except InvalidToken as exc:
        if str(exc) == CLIENT_NOT_ALLOWED:
            raise CallerRefused(CLIENT_NOT_ALLOWED) from exc
        raise
    # Client-credentials tokens: sub == client_id. A user token of the same pool (none exist: the pool has no users)
    # would have another sub; refuse it rather than treat a person as a service.
    if principal.sub != principal.client_id:
        raise CallerRefused("not a client-credentials token")
    return M2MCaller(client_id=principal.client_id, scopes=scopes_of(principal))


async def prefetch_m2m_jwks() -> dict[str, int]:
    """Start-up hook: load the M2M pool JWKS once. Never raises (the verifier retries on first use)."""
    try:
        counts = await get_m2m_verifier().prefetch()
    except Exception as exc:  # noqa: BLE001 - start-up must never fail on the identity prefetch
        log.warning("m2m_jwks_prefetch_failed", error=type(exc).__name__)
        return {}
    for iss, count in counts.items():
        if count:
            log.info("m2m_jwks_prefetched", issuer=iss, keys=count)
        else:
            log.warning("m2m_jwks_prefetch_empty", issuer=iss)
    return counts


def start_m2m_jwks_prefetch() -> Optional[asyncio.Task]:
    """Lifespan hook: run :func:`prefetch_m2m_jwks` in the background so a slow JWKS fetch through the NAT never
    delays start-up. None when ``ENVIRONMENT == "test"`` (no network call under TestClient)."""
    if os.environ.get("ENVIRONMENT", "").lower() == "test":
        return None
    return asyncio.create_task(prefetch_m2m_jwks(), name="aa976-m2m-jwks-prefetch")


async def stop_m2m_jwks_prefetch(task: Optional[asyncio.Task]) -> None:
    """Cancel the prefetch if it is still running at shutdown; swallows whatever it raised."""
    if task is None:
        return
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


# ── the route-class dependency (AA-978: shared by catalog.http.v1 and tenant.membership.v1) ─────────────────────────

class M2MProblem(Exception):
    """An error answered with the contracts' Problem body ``{error, message[, reason]}`` (handler registered in
    api/main.py). ``api.routers.catalog_http.CatalogProblem`` is this class."""

    def __init__(self, status: int, error: str, message: str, reason: Optional[str] = None,
                 headers: Optional[dict[str, str]] = None) -> None:
        super().__init__(status, error, message, reason, headers)
        self.status, self.error, self.message, self.reason = status, error, message, reason
        self.headers = headers or {}


def problem_response(exc: M2MProblem) -> JSONResponse:
    body: dict[str, object] = {"error": exc.error, "message": exc.message[:500]}
    if exc.reason is not None:
        body["reason"] = exc.reason
    return JSONResponse(status_code=exc.status, content=body, headers=exc.headers)


async def m2m_problem_handler(_request: Request, exc: M2MProblem) -> JSONResponse:
    return problem_response(exc)


def bearer_token(request: Request) -> Optional[str]:
    value = (request.headers.get("authorization") or "").strip()
    if value[:7].lower() != "bearer ":
        return None
    token = value[7:].strip()
    return token or None


def require_m2m(scope: str, *, realm: str, refused: str, log_prefix: str):
    """A FastAPI dependency for one M2M route class: the verified CRM caller holding ``scope``, or an M2MProblem.

    401 no / bad token (``WWW-Authenticate: Bearer realm="<realm>"``), 403 another client, a user token or a token
    without ``scope`` (``refused`` is the message for the first two), 503 key set or configuration unavailable.
    ``log_prefix`` keeps each route class's log events (``<prefix>_refused``, ``<prefix>_unauthorized``, ...)."""
    unauthorized_headers = {"WWW-Authenticate": f'Bearer realm="{realm}"'}

    async def dependency(request: Request) -> M2MCaller:
        token = bearer_token(request)
        if token is None:
            raise M2MProblem(401, "unauthorized", "An M2M bearer token is required.", headers=unauthorized_headers)
        try:
            verifier = get_m2m_verifier()
        except ValueError as exc:
            log.error(f"{log_prefix}_m2m_config_invalid", error=str(exc))
            raise M2MProblem(503, "unavailable", "Token verification is not configured.") from exc
        try:
            caller = await verify_caller(verifier, token)
        except CallerRefused as exc:
            log.info(f"{log_prefix}_refused", reason=str(exc))
            raise M2MProblem(403, "forbidden", refused) from exc
        except InvalidToken as exc:
            log.info(f"{log_prefix}_unauthorized", reason=str(exc))
            raise M2MProblem(401, "unauthorized", "The bearer token is not valid.",
                             headers=unauthorized_headers) from exc
        except AuthUnavailable as exc:
            log.error(f"{log_prefix}_jwks_unavailable")
            raise M2MProblem(503, "unavailable", "Token verification is unavailable; retry later.",
                             headers={"Retry-After": "5"}) from exc
        if not caller.has(scope):
            log.info(f"{log_prefix}_refused", reason="missing scope", client_id=caller.client_id)
            raise M2MProblem(403, "forbidden", f"The token lacks the scope {scope}.")
        return caller

    dependency.__name__ = f"require_m2m_{log_prefix}"
    return dependency
