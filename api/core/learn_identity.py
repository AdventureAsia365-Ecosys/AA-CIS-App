"""WORKFORCE ``academy-web`` access tokens on the CIS learning API (AA-1105, root ADR 0022 decisions 6 and 7).

The AA Academy host (``academy.<env domain>``) is served by the AA-Partner CRM web; its server side (BFF) holds the
learner's WORKFORCE access token of the app client ``academy-web`` and forwards it as a Bearer token to ``/learn/v1``.

One route class, one verifier (root ADR 0006 §8 rule 6): ``/learn/v1/*`` accepts only WORKFORCE x {academy-web}. This
verifier is its OWN :class:`MultiIssuerVerifier`; the ``academy-web`` client is never added to
:func:`api.core.identity_config.cis_staff_issuers` (a test keeps it out), so an academy token is 401 on every staff
path, and a ``cis-admin``, ``breakglass`` or ``crm-staff`` token is 403 here.

Configuration follows ``api/core/identity_config.py`` and ``api/core/partner_identity.py``: the Dev value is a code
constant with an env override, so the CIS task definition needs no new variable (AA-CIS-Infra AA-1105 notes). The
client id is a public value (it is in every authorize URL and every token).

- ``COGNITO_ACADEMY_WEB_CLIENT_ID``   default :data:`DEV_ACADEMY_WEB_CLIENT_ID` (AA-CIS-Infra ``identity.tf``)

It must differ from the CIS staff clients (``cis-admin``, ``breakglass``, the preview client):
:func:`academy_issuer_config` raises ``ValueError`` and the routes answer 503 (fail closed).

Outcomes of :func:`verify_academy_token`: no key set -> ``AuthUnavailable`` (503); malformed, expired, badly signed,
an unknown issuer, an ID token -> ``InvalidToken`` (401); a validly signed WORKFORCE token of another client ->
:class:`AcademyCallerRefused` (403 ``forbidden``); a token of the PARTNER pool -> :class:`PartnerNotOpen` (403
``partner_not_open``, until the agency academy, P7, decision D6); a token of another known pool (M2M, EXTERNAL) ->
:class:`AcademyCallerRefused` (403). The refusals read only the unverified issuer to choose the status code; they
never grant anything.
"""

from __future__ import annotations

import asyncio
import os
from typing import Optional

import structlog

from api.core import identity_config
from api.core.identity_verifier import (
    InvalidToken,
    IssuerConfig,
    JwksFetcher,
    MultiIssuerVerifier,
    Principal,
)
from api.core.partner_identity import CLIENT_NOT_ALLOWED, _unverified_claims, partner_issuer

log = structlog.get_logger(__name__)

ACADEMY = "academy"

DEV_ACADEMY_WEB_CLIENT_ID = "4q6g6de1pkk4i7f29clh77gb5o"
ENV_ACADEMY_WEB_CLIENT_ID = "COGNITO_ACADEMY_WEB_CLIENT_ID"

_verifier: Optional[MultiIssuerVerifier] = None


class AcademyCallerRefused(Exception):  # noqa: N818 - reads as an outcome, like InvalidToken
    """An authenticated token of another client or pool: 403 ``forbidden`` on the learning API."""


class PartnerNotOpen(Exception):  # noqa: N818 - reads as an outcome
    """A partner-pool token: 403 ``partner_not_open`` until the agency academy (P7, decision D6)."""


def academy_web_client_id() -> str:
    value = (os.environ.get(ENV_ACADEMY_WEB_CLIENT_ID) or "").strip()
    return value or DEV_ACADEMY_WEB_CLIENT_ID


def academy_issuer_config() -> IssuerConfig:
    """WORKFORCE x {academy-web}, the Cognito access-token profile. ``ValueError`` when the client id is not a Cognito
    client id or equals a CIS staff client (the academy client is accepted nowhere else, and no staff client here)."""
    client = academy_web_client_id()
    if not client.isalnum():
        raise ValueError(f"{ENV_ACADEMY_WEB_CLIENT_ID} must be a Cognito app client id (letters and digits)")
    staff_clients = {identity_config.cis_admin_client_id(), identity_config.breakglass_client_id(),
                     identity_config.cis_admin_preview_client_id()}
    if client in staff_clients:
        raise ValueError(f"{ENV_ACADEMY_WEB_CLIENT_ID} must differ from the CIS staff client ids")
    return IssuerConfig(issuer=identity_config.workforce_issuer(), client_ids=frozenset({client}), name=ACADEMY)


def build_academy_verifier(fetcher: JwksFetcher | None = None) -> MultiIssuerVerifier:
    return MultiIssuerVerifier((academy_issuer_config(),), fetcher)


def get_academy_verifier() -> MultiIssuerVerifier:
    """Process-wide verifier of the learning API (built on first use). ``ValueError`` on a bad configuration."""
    global _verifier
    if _verifier is None:
        _verifier = build_academy_verifier()
    return _verifier


def set_academy_verifier(verifier: Optional[MultiIssuerVerifier]) -> None:
    """Install (tests) or drop (None) the process-wide verifier."""
    global _verifier
    _verifier = verifier


def _foreign_refusal(token: str) -> Optional[Exception]:
    """The refusal for a validly shaped token of another known pool, or None (then it stays a 401)."""
    iss = _unverified_claims(token).get("iss")
    if not isinstance(iss, str):
        return None
    if iss == partner_issuer():
        return PartnerNotOpen("partner pool")
    from api.core.m2m_auth import m2m_issuer

    if iss == m2m_issuer() or iss.startswith("https://cognito-idp."):
        return AcademyCallerRefused("token of another pool")
    return None


async def verify_academy_token(token: str) -> Principal:
    """The verified academy-web principal; raises InvalidToken (401), AcademyCallerRefused / PartnerNotOpen (403),
    AuthUnavailable (503) or ValueError (configuration, 503)."""
    verifier = get_academy_verifier()
    try:
        return await verifier.verify(token)
    except InvalidToken as exc:
        if str(exc) == CLIENT_NOT_ALLOWED:
            raise AcademyCallerRefused(CLIENT_NOT_ALLOWED) from exc
        refusal = _foreign_refusal(token) if str(exc) == "unknown issuer" else None
        if refusal is not None:
            raise refusal from exc
        raise


async def prefetch_academy_jwks() -> dict[str, int]:
    """Start-up hook: load the WORKFORCE JWKS for this verifier once. Never raises."""
    try:
        counts = await get_academy_verifier().prefetch()
    except Exception as exc:  # noqa: BLE001 - start-up must never fail on the identity prefetch
        log.warning("academy_jwks_prefetch_failed", error=type(exc).__name__)
        return {}
    for iss, count in counts.items():
        (log.info if count else log.warning)("academy_jwks_prefetched", issuer=iss, keys=count)
    return counts


def start_academy_jwks_prefetch() -> Optional[asyncio.Task]:
    """Lifespan hook (background, never delays start-up). None when ``ENVIRONMENT == "test"``."""
    if os.environ.get("ENVIRONMENT", "").lower() == "test":
        return None
    return asyncio.create_task(prefetch_academy_jwks(), name="aa1105-academy-jwks-prefetch")


async def stop_academy_jwks_prefetch(task: Optional[asyncio.Task]) -> None:
    if task is None:
        return
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
