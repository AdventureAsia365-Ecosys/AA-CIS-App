"""CIS staff-surface issuer configuration for the multi-issuer verifier (root ADR 0006 §8, AA-816 / AA-854).

CIS staff routes accept exactly WORKFORCE x {cis-admin, breakglass, cis-admin-preview} (ADR 0006 §8 rule 6;
cis-admin-preview: AA-911 decision D-1, root ADR 0006a A11). The Dev values are code constants with an env
override, following Booking's settings defaults, so the ECS task definitions need no new variables (S242 plan,
stage B / C2):

- ``COGNITO_WORKFORCE_ISSUER``       default :data:`DEV_WORKFORCE_ISSUER`
- ``COGNITO_CIS_ADMIN_CLIENT_ID``    default :data:`DEV_CIS_ADMIN_CLIENT_ID`
- ``COGNITO_BREAKGLASS_CLIENT_ID``   default :data:`DEV_BREAKGLASS_CLIENT_ID`
- ``COGNITO_CIS_ADMIN_PREVIEW_CLIENT_ID`` default :data:`DEV_CIS_ADMIN_PREVIEW_CLIENT_ID` (empty until the
  AA-CIS-Infra apply creates the client; empty = no preview client accepted). The client of the fixed Preview
  host ``aa-cis-preview.lumiguides.it.com`` only: ``api/core/staff_identity.resolve_staff`` caps every token of it
  at the viewer role (:func:`is_preview_client`), whatever ``cis-*`` group the user holds. It must differ from the
  two clients above (the verifier build fails otherwise, so every staff token answers 503: fail closed).
- ``COGNITO_WORKFORCE_POOL_ID``      default: the pool id at the end of the WORKFORCE issuer
  (:data:`DEV_WORKFORCE_POOL_ID` on Dev). Used by the staff directory writer (C6,
  ``api/services/staff_directory.py``); the region is the pool id's prefix.

The issuer is the pool issuer as published by Terraform (``identity.tf`` outputs) and the pool's
``/.well-known/openid-configuration``, never the custom/hosted domain. Client ids are public values (they appear
in every authorize URL); client secrets never live here.

No route or middleware uses the verifier yet (C3 wires it into the bearer middleware / verify-admin). The only
thing ``api/main.py`` calls is :func:`start_cis_staff_jwks_prefetch`: a background task that loads the WORKFORCE
JWKS once at start-up and logs ``identity_jwks_prefetched`` (off when ``ENVIRONMENT == "test"``, never raises,
never delays start-up).
"""

from __future__ import annotations

import asyncio
import os
from typing import Optional

import structlog

from api.core.identity_verifier import IssuerConfig, JwksFetcher, MultiIssuerVerifier

log = structlog.get_logger(__name__)

DEV_WORKFORCE_ISSUER = "https://cognito-idp.us-west-1.amazonaws.com/us-west-1_bCcFvbSOS"
DEV_CIS_ADMIN_CLIENT_ID = "3oqkumijh04cp81k3nlnub6qtv"
DEV_BREAKGLASS_CLIENT_ID = "2g3f3ncfuqdiuu1o5b70o4h0lq"
# AA-911 D-1: the Preview-only cis-admin-preview client (AA-CIS-Infra identity.tf). Empty until the Infra apply creates
# it; then the id is filled in here.
DEV_CIS_ADMIN_PREVIEW_CLIENT_ID = ""
DEV_WORKFORCE_POOL_ID = "us-west-1_bCcFvbSOS"

ENV_WORKFORCE_ISSUER = "COGNITO_WORKFORCE_ISSUER"
ENV_CIS_ADMIN_CLIENT_ID = "COGNITO_CIS_ADMIN_CLIENT_ID"
ENV_BREAKGLASS_CLIENT_ID = "COGNITO_BREAKGLASS_CLIENT_ID"
ENV_CIS_ADMIN_PREVIEW_CLIENT_ID = "COGNITO_CIS_ADMIN_PREVIEW_CLIENT_ID"
ENV_WORKFORCE_POOL_ID = "COGNITO_WORKFORCE_POOL_ID"

_verifier: MultiIssuerVerifier | None = None


def _env(name: str, default: str) -> str:
    value = (os.environ.get(name) or "").strip()
    return value or default


def workforce_issuer() -> str:
    return _env(ENV_WORKFORCE_ISSUER, DEV_WORKFORCE_ISSUER)


def cis_admin_client_id() -> str:
    return _env(ENV_CIS_ADMIN_CLIENT_ID, DEV_CIS_ADMIN_CLIENT_ID)


def breakglass_client_id() -> str:
    return _env(ENV_BREAKGLASS_CLIENT_ID, DEV_BREAKGLASS_CLIENT_ID)


def cis_admin_preview_client_id() -> str:
    """The Preview-only client id, or "" when none is configured (then no preview token is accepted)."""
    return _env(ENV_CIS_ADMIN_PREVIEW_CLIENT_ID, DEV_CIS_ADMIN_PREVIEW_CLIENT_ID)


def is_preview_client(client_id: Optional[str]) -> bool:
    """True when ``client_id`` is the Preview-only client (AA-911 D-1): its sessions are always the viewer."""
    preview = cis_admin_preview_client_id()
    return bool(preview) and client_id == preview


def workforce_pool_id() -> str:
    """The WORKFORCE user pool id: the env override, else the last path segment of the issuer (so an issuer
    override for another pool moves the directory writer with it)."""
    return _env(ENV_WORKFORCE_POOL_ID, workforce_issuer().rstrip("/").rsplit("/", 1)[-1])


def workforce_region() -> str:
    """The pool's region: the pool id prefix (``us-west-1_xxxx`` -> ``us-west-1``)."""
    return workforce_pool_id().split("_", 1)[0]


def cis_staff_issuers() -> tuple[IssuerConfig, ...]:
    """The (issuer, client_id) pairs the CIS staff surface accepts: WORKFORCE x {cis-admin, breakglass} plus
    cis-admin-preview when configured (capped at viewer by staff_identity). A preview id equal to a production
    client id is refused (ValueError): it would either cap Production or un-cap Preview."""
    production = {cis_admin_client_id(), breakglass_client_id()}
    preview = cis_admin_preview_client_id()
    if preview and preview in production:
        raise ValueError(f"{ENV_CIS_ADMIN_PREVIEW_CLIENT_ID} must differ from the production client ids")
    return (
        IssuerConfig(
            issuer=workforce_issuer(),
            client_ids=frozenset(production | ({preview} if preview else set())),
            name="workforce",
        ),
    )


def build_cis_staff_verifier(fetcher: JwksFetcher | None = None) -> MultiIssuerVerifier:
    return MultiIssuerVerifier(cis_staff_issuers(), fetcher)


def get_cis_staff_verifier() -> MultiIssuerVerifier:
    """Process-wide verifier for the CIS staff surface (built on first use from the env at that time)."""
    global _verifier
    if _verifier is None:
        _verifier = build_cis_staff_verifier()
    return _verifier


def reset_cis_staff_verifier() -> None:
    """Drop the cached verifier (tests, or after an env change)."""
    global _verifier
    _verifier = None


async def prefetch_cis_staff_jwks() -> dict[str, int]:
    """Start-up hook: load the WORKFORCE JWKS once so a NAT blip later never becomes a sign-in outage.

    Never raises: a failure is logged and the verifier retries on first use.
    """
    try:
        counts = await get_cis_staff_verifier().prefetch()
    except Exception as exc:  # noqa: BLE001 - start-up must never fail on the identity prefetch
        log.warning("identity_jwks_prefetch_failed", error=type(exc).__name__)
        return {}
    for iss, count in counts.items():
        if count:
            log.info("identity_jwks_prefetched", issuer=iss, keys=count)
        else:
            log.warning("identity_jwks_prefetch_empty", issuer=iss)
    return counts


def start_cis_staff_jwks_prefetch() -> Optional[asyncio.Task]:
    """Lifespan hook: run :func:`prefetch_cis_staff_jwks` in the background so a slow JWKS fetch through the NAT
    never delays start-up. None (nothing started) when ``ENVIRONMENT == "test"``, so TestClient runs make no
    network call."""
    if os.environ.get("ENVIRONMENT", "").lower() == "test":
        return None
    return asyncio.create_task(prefetch_cis_staff_jwks(), name="aa854-identity-jwks-prefetch")


async def stop_cis_staff_jwks_prefetch(task: Optional[asyncio.Task]) -> None:
    """Cancel the prefetch if it is still running at shutdown; swallows whatever it raised."""
    if task is None:
        return
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
