"""CIS staff-surface issuer configuration for the multi-issuer verifier (root ADR 0006 §8, AA-816 / AA-854).

CIS staff routes accept exactly WORKFORCE x {cis-admin, breakglass} (ADR 0006 §8 rule 6). The Dev values are
code constants with an env override, following Booking's settings defaults, so the ECS task definitions need
no new variables (S242 plan, stage B / C2):

- ``COGNITO_WORKFORCE_ISSUER``       default :data:`DEV_WORKFORCE_ISSUER`
- ``COGNITO_CIS_ADMIN_CLIENT_ID``    default :data:`DEV_CIS_ADMIN_CLIENT_ID`
- ``COGNITO_BREAKGLASS_CLIENT_ID``   default :data:`DEV_BREAKGLASS_CLIENT_ID`

The issuer is the pool issuer as published by Terraform (``identity.tf`` outputs) and the pool's
``/.well-known/openid-configuration``, never the custom/hosted domain. Client ids are public values (they appear
in every authorize URL); client secrets never live here.

Nothing here is wired into ``api/main.py`` yet: :func:`prefetch_cis_staff_jwks` is the start-up hook that a later
step (the bearer middleware / verify-admin change) will call from the lifespan.
"""

from __future__ import annotations

import os

import structlog

from api.core.identity_verifier import IssuerConfig, JwksFetcher, MultiIssuerVerifier

log = structlog.get_logger(__name__)

DEV_WORKFORCE_ISSUER = "https://cognito-idp.us-west-1.amazonaws.com/us-west-1_bCcFvbSOS"
DEV_CIS_ADMIN_CLIENT_ID = "3oqkumijh04cp81k3nlnub6qtv"
DEV_BREAKGLASS_CLIENT_ID = "2g3f3ncfuqdiuu1o5b70o4h0lq"

ENV_WORKFORCE_ISSUER = "COGNITO_WORKFORCE_ISSUER"
ENV_CIS_ADMIN_CLIENT_ID = "COGNITO_CIS_ADMIN_CLIENT_ID"
ENV_BREAKGLASS_CLIENT_ID = "COGNITO_BREAKGLASS_CLIENT_ID"

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


def cis_staff_issuers() -> tuple[IssuerConfig, ...]:
    """The (issuer, client_id) pairs the CIS staff surface accepts: WORKFORCE x {cis-admin, breakglass}."""
    return (
        IssuerConfig(
            issuer=workforce_issuer(),
            client_ids=frozenset({cis_admin_client_id(), breakglass_client_id()}),
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

    Never raises: a failure is logged and the verifier retries on first use. Not called from main.py yet.
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
