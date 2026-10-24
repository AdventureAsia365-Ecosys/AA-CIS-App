"""AA-976 test helpers: a local M2M pool issuer wired into the CIS M2M verifier. No network, no AWS."""
from __future__ import annotations

from typing import Any

from api.core import m2m_auth
from api.core.identity_verifier import MultiIssuerVerifier
from tests.unit.test_identity_verifier import _DROP, FakeFetcher, LocalIssuer

ISS = m2m_auth.DEV_M2M_ISSUER
CRM = m2m_auth.DEV_M2M_CRM_CLIENT_ID
OTHER_CLIENT = "7otherclient0000000000000a"
READ = m2m_auth.SCOPE_CATALOG_READ

__all__ = ["ISS", "CRM", "OTHER_CLIENT", "READ", "M2MIssuer", "install_m2m"]


class M2MIssuer(LocalIssuer):
    """The aa-m2m pool: client-credentials access tokens (sub == client_id, scope string, no user claims)."""

    def token(self, **claims: Any) -> str:  # type: ignore[override]
        claims.setdefault("client_id", CRM)
        claims.setdefault("sub", claims["client_id"])
        claims.setdefault("scope", f"booking.b2b/read {READ}")
        for user_claim in ("username", "email", "cognito:groups"):
            claims.setdefault(user_claim, _DROP)
        return super().token(**claims)


def install_m2m() -> tuple[M2MIssuer, FakeFetcher]:
    """Replace the process-wide M2M verifier with one that trusts a local M2M pool issuer."""
    issuer = M2MIssuer(ISS)
    fetcher = FakeFetcher(issuer)
    m2m_auth.set_m2m_verifier(MultiIssuerVerifier([m2m_auth.m2m_issuer_config()], fetcher))
    return issuer, fetcher
