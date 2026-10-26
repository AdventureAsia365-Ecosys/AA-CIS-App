"""Partner-pool access tokens on CIS: the Content studio sign-in exchange (AA-979 P2b, root ADR 0010a).

After P2b the tenant Content studio (AA-Partner ``portal/``, host ``portal.``) signs people in through the partner pool
(``aa-partner-<env>``, MFA required) together with the CRM web. The portal BFF holds the person's partner-pool
ACCESS token (the CRM web session, read only) and swaps it at ``POST /auth/tenant-session`` for a short CIS tenant JWT
(``api/routers/tenant_session.py``); CIS stays the only owner of Content studio roles (``shared.tenant_members``).

One route class, one verifier (root ADR 0006 §8 rule 6): PARTNER x {partner-web}. This verifier is its OWN
:class:`MultiIssuerVerifier` and is never added to :func:`api.core.identity_config.cis_staff_issuers`: a partner token
must stay 401/403 on every staff path, and a staff, EXTERNAL or M2M token must be 403 here (card R: a ``cis-admin``
token gets no portal session).

Configuration follows ``api/core/identity_config.py``: Dev values are code constants with an env override, so the ECS
task definition needs no new variable (S250 lesson: a Terraform apply re-registers the CIS task definitions from the
template, so a runtime-only variable would silently fall away). Pool and client ids are public values (they appear in
every authorize URL and every token); the client secret lives only with the CRM web.

- ``COGNITO_PARTNER_ISSUER``          default :data:`DEV_PARTNER_ISSUER` (the pool issuer, never the custom domain)
- ``COGNITO_PARTNER_WEB_CLIENT_ID``   default :data:`DEV_PARTNER_WEB_CLIENT_ID` (AA-CIS-Infra ``identity_partner.tf``)

The partner issuer must differ from the WORKFORCE and the M2M issuers (:func:`partner_issuer_config` raises
``ValueError``; the exchange then answers 503, fail closed).

Outcomes of :func:`verify_partner_access_token`: no key set -> ``AuthUnavailable`` (503); malformed, expired, badly
signed, wrong issuer, an ID token -> ``InvalidToken`` (401); validly signed by the partner pool for another client ->
:class:`PartnerCallerRefused` (403). A token of another pool (WORKFORCE ``cis-admin`` / ``breakglass``, EXTERNAL
``cis-portal``, M2M) has an issuer this verifier does not know, so the shared verifier answers ``InvalidToken`` for it;
:func:`classify_foreign` turns a validly shaped foreign token into 403 (refused, not "bad token") so the card-R check
reads as a refusal.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
from typing import Optional

import structlog

from api.core.identity_config import workforce_issuer
from api.core.identity_verifier import (
    InvalidToken,
    IssuerConfig,
    JwksFetcher,
    MultiIssuerVerifier,
    Principal,
)

log = structlog.get_logger(__name__)

PARTNER = "partner"

DEV_PARTNER_ISSUER = "https://cognito-idp.us-west-1.amazonaws.com/us-west-1_pjsHzdgOc"
DEV_PARTNER_WEB_CLIENT_ID = "69d3d07oo51b1nij5n8tannrbr"

ENV_PARTNER_ISSUER = "COGNITO_PARTNER_ISSUER"
ENV_PARTNER_WEB_CLIENT_ID = "COGNITO_PARTNER_WEB_CLIENT_ID"

# The shared verifier's message for a validly signed token of a client this route class does not accept.
CLIENT_NOT_ALLOWED = "client not allowed"

_verifier: Optional[MultiIssuerVerifier] = None


class PartnerCallerRefused(Exception):  # noqa: N818 - reads as an outcome, like InvalidToken
    """An authenticated token that may not open a Content studio session (HTTP 403)."""


def _env(name: str, default: str) -> str:
    value = (os.environ.get(name) or "").strip()
    return value or default


def partner_issuer() -> str:
    return _env(ENV_PARTNER_ISSUER, DEV_PARTNER_ISSUER)


def partner_web_client_id() -> str:
    return _env(ENV_PARTNER_WEB_CLIENT_ID, DEV_PARTNER_WEB_CLIENT_ID)


def partner_issuer_config() -> IssuerConfig:
    """PARTNER pool x {partner-web}, the Cognito access-token profile (``token_use == "access"``)."""
    from api.core.m2m_auth import m2m_issuer  # local import: m2m_auth imports identity_config too

    issuer = partner_issuer()
    if issuer in {workforce_issuer(), m2m_issuer()}:
        raise ValueError(f"{ENV_PARTNER_ISSUER} must differ from the WORKFORCE and M2M issuers")
    client = partner_web_client_id()
    if not client.isalnum():
        raise ValueError(f"{ENV_PARTNER_WEB_CLIENT_ID} must be a Cognito app client id (letters and digits)")
    return IssuerConfig(issuer=issuer, client_ids=frozenset({client}), name=PARTNER)


def build_partner_verifier(fetcher: JwksFetcher | None = None) -> MultiIssuerVerifier:
    return MultiIssuerVerifier((partner_issuer_config(),), fetcher)


def get_partner_verifier() -> MultiIssuerVerifier:
    """Process-wide verifier for the exchange (built on first use from the env at that time). ``ValueError`` on an
    invalid configuration; the router answers 503 then."""
    global _verifier
    if _verifier is None:
        _verifier = build_partner_verifier()
    return _verifier


def set_partner_verifier(verifier: Optional[MultiIssuerVerifier]) -> None:
    """Install (tests) or drop (None) the process-wide verifier."""
    global _verifier
    _verifier = verifier


def _unverified_claims(token: str) -> dict:
    """The payload of a JWT WITHOUT checking anything (only to word a refusal; never to grant)."""
    try:
        part = token.split(".")[1]
        data = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except Exception:  # noqa: BLE001 - any malformed token is simply "not a known foreign token"
        return {}
    return data if isinstance(data, dict) else {}


def classify_foreign(token: str) -> bool:
    """True when the token names an issuer of ANOTHER known pool (WORKFORCE, M2M) or the EXTERNAL pool shape, so a
    refusal is 403 (authenticated somewhere, not allowed here) rather than 401. Not a verification: it only decides
    the status code of a token that is refused either way."""
    iss = _unverified_claims(token).get("iss")
    if not isinstance(iss, str):
        return False
    from api.core.m2m_auth import m2m_issuer

    known = {workforce_issuer(), m2m_issuer()}
    return iss in known or (iss.startswith("https://cognito-idp.") and iss != partner_issuer())


async def verify_partner_access_token(token: str) -> Principal:
    """The verified partner-web principal; raises InvalidToken (401), PartnerCallerRefused (403),
    AuthUnavailable (503) or ValueError (configuration, 503)."""
    verifier = get_partner_verifier()
    try:
        return await verifier.verify(token)
    except InvalidToken as exc:
        if str(exc) == CLIENT_NOT_ALLOWED:
            raise PartnerCallerRefused(CLIENT_NOT_ALLOWED) from exc
        if classify_foreign(token):
            raise PartnerCallerRefused("token of another pool") from exc
        raise


async def prefetch_partner_jwks() -> dict[str, int]:
    """Start-up hook: load the partner pool JWKS once. Never raises (the verifier retries on first use)."""
    try:
        counts = await get_partner_verifier().prefetch()
    except Exception as exc:  # noqa: BLE001 - start-up must never fail on the identity prefetch
        log.warning("partner_jwks_prefetch_failed", error=type(exc).__name__)
        return {}
    for iss, count in counts.items():
        if count:
            log.info("partner_jwks_prefetched", issuer=iss, keys=count)
        else:
            log.warning("partner_jwks_prefetch_empty", issuer=iss)
    return counts


def start_partner_jwks_prefetch() -> Optional[asyncio.Task]:
    """Lifespan hook (background, never delays start-up). None when ``ENVIRONMENT == "test"``."""
    if os.environ.get("ENVIRONMENT", "").lower() == "test":
        return None
    return asyncio.create_task(prefetch_partner_jwks(), name="aa979-partner-jwks-prefetch")


async def stop_partner_jwks_prefetch(task: Optional[asyncio.Task]) -> None:
    if task is None:
        return
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
