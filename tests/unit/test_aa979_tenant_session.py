"""AA-979 P2b — POST /auth/tenant-session and GET /auth/tenant-memberships (api/routers/tenant_session.py).

A local partner-pool issuer (no network) wired into the partner verifier; a scripted fake pool answers the membership
read. Covers card R (a staff / EXTERNAL / M2M token gets no studio session), the claims of the minted token
(client=portal, amr=partner, mid, csr, exp <= 15 min and <= the access token's exp), the chooser (409 / chosen
tenant), no membership, the per-identity throttle and that the minted token passes the existing tenant checks.
"""
from __future__ import annotations

import time
import uuid
from typing import Any
from unittest.mock import AsyncMock

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.core import identity_config, m2m_auth, partner_identity
from api.core.identity_verifier import MultiIssuerVerifier
from api.routers import tenant_session
from api.routers.auth import JWT_ALG, JWT_SECRET, verify_tenant_jwt
from services.tenant_membership import session as studio
from tests.unit.test_identity_verifier import _DROP, FakeFetcher, LocalIssuer

PARTNER_ISS = partner_identity.DEV_PARTNER_ISSUER
PARTNER_WEB = partner_identity.DEV_PARTNER_WEB_CLIENT_ID
SUB = "6c1e0f4a-0000-4000-8000-0000000000aa"
T1, T2 = str(uuid.uuid4()), str(uuid.uuid4())
M1, M2 = str(uuid.uuid4()), str(uuid.uuid4())


class PartnerIssuer(LocalIssuer):
    def token(self, **claims: Any) -> str:  # type: ignore[override]
        claims.setdefault("client_id", PARTNER_WEB)
        claims.setdefault("sub", SUB)
        claims.setdefault("username", SUB)
        claims.setdefault("cognito:groups", _DROP)
        claims.setdefault("email", _DROP)
        return super().token(**claims)


def _row(tenant_id: str, membership_id: str, role: str = "editor", name: str = "Agency") -> dict:
    return {"tenant_id": uuid.UUID(tenant_id), "membership_id": uuid.UUID(membership_id),
            "content_studio_role": role, "name": name, "plan_tier": "growth"}


class FakePool:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.calls: list[tuple] = []
        self.execute = AsyncMock()  # the audit row (write_audit_log on the pool)

    def acquire(self, timeout: float | None = None):
        pool = self

        class _CM:
            async def __aenter__(self):
                conn = AsyncMock()

                async def fetch(sql, *args):
                    pool.calls.append((sql, args))
                    assert sql == studio.ACTIVE_FOR_IDENTITY_SQL
                    return pool.rows
                conn.fetch = AsyncMock(side_effect=fetch)
                return conn

            async def __aexit__(self, *exc):
                return False
        return _CM()


class FakeRedis:
    def __init__(self) -> None:
        self.counts: dict[str, int] = {}

    async def incr(self, key: str) -> int:
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]

    async def expire(self, key: str, ttl: int) -> None:
        return None


@pytest.fixture
def issuer():
    iss = PartnerIssuer(PARTNER_ISS)
    partner_identity.set_partner_verifier(
        MultiIssuerVerifier([partner_identity.partner_issuer_config()], FakeFetcher(iss)))
    yield iss
    partner_identity.set_partner_verifier(None)


def _client(rows: list[dict], redis: Any = None) -> tuple[TestClient, FakePool]:
    app = FastAPI()
    app.include_router(tenant_session.router)
    pool = FakePool(rows)
    app.state.pool = pool
    app.state.redis = redis if redis is not None else FakeRedis()
    return TestClient(app), pool


def _post(client: TestClient, token: str | None, body: Any = None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return client.post("/auth/tenant-session", headers=headers, json=body)


def test_partner_token_with_one_membership_gets_a_short_portal_session(issuer):
    client, pool = _client([_row(T1, M1, role="editor", name="Saigon Trails")])
    access_exp = int(time.time()) + 1800
    r = _post(client, issuer.token(exp=access_exp))
    assert r.status_code == 200, r.text
    body = r.json()
    assert r.headers["cache-control"] == "no-store"
    assert (body["tenant_id"], body["tenant_name"], body["content_studio_role"], body["membership_id"]) == (
        T1, "Saigon Trails", "editor", M1)
    claims = jwt.decode(body["token"], JWT_SECRET, algorithms=[JWT_ALG])
    assert claims["sub"] == T1 and claims["role"] == "tenant" and claims["client"] == "portal"
    assert claims["amr"] == "partner" and claims["mid"] == M1 and claims["csr"] == "editor"
    assert claims["exp"] - claims["iat"] <= 15 * 60
    assert verify_tenant_jwt(body["token"])["sub"] == T1  # every /v1 route and /auth/verify-tenant accept it
    sql, args = pool.calls[0]
    assert args == (PARTNER_ISS, SUB)  # keyed by iss AND sub
    pool.execute.assert_awaited()  # the tenant.login audit row (login_method=partner)
    assert "partner" in pool.execute.await_args.args[-1]


def test_session_never_outlives_the_access_token(issuer):
    client, _ = _client([_row(T1, M1)])
    access_exp = int(time.time()) + 120
    body = _post(client, issuer.token(exp=access_exp)).json()
    claims = jwt.decode(body["token"], JWT_SECRET, algorithms=[JWT_ALG])
    assert claims["exp"] <= access_exp


@pytest.mark.parametrize("kind", ["cis-admin", "breakglass", "m2m", "external", "partner-other-client"])
def test_card_r_foreign_tokens_are_refused_403(issuer, kind):
    client, pool = _client([_row(T1, M1)])
    if kind in ("cis-admin", "breakglass"):
        staff = LocalIssuer(identity_config.DEV_WORKFORCE_ISSUER)
        client_id = (identity_config.DEV_CIS_ADMIN_CLIENT_ID if kind == "cis-admin"
                     else identity_config.DEV_BREAKGLASS_CLIENT_ID)
        token = staff.token(client_id=client_id)
    elif kind == "m2m":
        token = LocalIssuer(m2m_auth.DEV_M2M_ISSUER).token(client_id=m2m_auth.DEV_M2M_CRM_CLIENT_ID,
                                                           sub=m2m_auth.DEV_M2M_CRM_CLIENT_ID)
    elif kind == "external":
        token = LocalIssuer("https://cognito-idp.us-west-1.amazonaws.com/us-west-1_EXTERNALxx").token(
            client_id="cisportalclient00000000001")
    else:
        token = issuer.token(client_id="otherpartnerclient00000001")
    r = _post(client, token)
    assert r.status_code == 403 and r.json()["detail"] == "forbidden"
    assert pool.calls == []  # refused before any DB read


@pytest.mark.parametrize("case", ["none", "garbage", "id_token", "expired", "wrong_key"])
def test_bad_or_missing_partner_tokens_are_401(issuer, case):
    client, pool = _client([_row(T1, M1)])
    token = {
        "none": None,
        "garbage": "not-a-jwt",
        "id_token": issuer.token(token_use="id"),
        "expired": issuer.token(exp=int(time.time()) - 3600),
        "wrong_key": PartnerIssuer(PARTNER_ISS).token(),  # same iss, unknown signing key
    }[case]
    r = _post(client, token)
    assert r.status_code == 401 and r.json()["detail"] == "unauthorized"
    assert pool.calls == []


def test_no_membership_is_403(issuer):
    client, _ = _client([])
    r = _post(client, issuer.token())
    assert r.status_code == 403 and r.json()["detail"] == "no_membership"


def test_two_memberships_need_a_choice_then_the_chosen_one_opens(issuer):
    client, _ = _client([_row(T1, M1, role="owner"), _row(T2, M2, role="viewer")])
    token = issuer.token()
    r = _post(client, token)
    assert r.status_code == 409 and r.json()["detail"] == "choose_tenant"
    r = _post(client, token, {"tenant_id": T2})
    assert r.status_code == 200
    assert r.json()["tenant_id"] == T2 and r.json()["content_studio_role"] == "viewer"


def test_a_tenant_the_identity_is_not_a_member_of_is_403(issuer):
    client, _ = _client([_row(T1, M1)])
    r = _post(client, issuer.token(), {"tenant_id": str(uuid.uuid4())})
    assert r.status_code == 403 and r.json()["detail"] == "tenant_not_member"


def test_body_with_unknown_fields_is_400(issuer):
    client, _ = _client([_row(T1, M1)])
    r = _post(client, issuer.token(), {"tenant_id": T1, "role": "owner"})
    assert r.status_code == 400


def test_throttle_is_per_identity_not_per_ip(issuer):
    redis = FakeRedis()
    client, _ = _client([_row(T1, M1)], redis=redis)
    token = issuer.token()
    for _ in range(tenant_session.EXCHANGE_LIMIT_PER_MIN):
        assert _post(client, token).status_code == 200
    assert _post(client, token).status_code == 429
    other = issuer.token(sub="6c1e0f4a-0000-4000-8000-0000000000bb", username="x")
    assert _post(client, other).status_code != 429  # another person behind the same NAT address is not blocked


def test_throttle_fails_open_without_redis(issuer):
    class Broken:
        async def incr(self, key):
            raise ConnectionError("down")
    client, _ = _client([_row(T1, M1)], redis=Broken())
    assert _post(client, issuer.token()).status_code == 200


def test_memberships_list_has_no_identity(issuer):
    client, _ = _client([_row(T1, M1, role="owner", name="A"), _row(T2, M2, role="viewer", name="B")])
    r = client.get("/auth/tenant-memberships", headers={"Authorization": f"Bearer {issuer.token()}"})
    assert r.status_code == 200
    items = r.json()["memberships"]
    assert [i["tenant_id"] for i in items] == [T1, T2]
    assert all(set(i) == {"tenant_id", "tenant_name", "content_studio_role", "membership_id"} for i in items)


def test_memberships_list_refuses_staff_tokens(issuer):
    client, _ = _client([_row(T1, M1)])
    staff = LocalIssuer(identity_config.DEV_WORKFORCE_ISSUER).token(client_id=identity_config.DEV_CIS_ADMIN_CLIENT_ID)
    r = client.get("/auth/tenant-memberships", headers={"Authorization": f"Bearer {staff}"})
    assert r.status_code == 403


def test_partner_issuer_must_differ_from_staff_and_m2m(monkeypatch):
    monkeypatch.setenv(partner_identity.ENV_PARTNER_ISSUER, identity_config.DEV_WORKFORCE_ISSUER)
    with pytest.raises(ValueError):
        partner_identity.partner_issuer_config()
    monkeypatch.setenv(partner_identity.ENV_PARTNER_ISSUER, m2m_auth.DEV_M2M_ISSUER)
    with pytest.raises(ValueError):
        partner_identity.partner_issuer_config()


def test_partner_pool_is_never_a_staff_issuer():
    staff_issuers = {c.issuer for c in identity_config.cis_staff_issuers()}
    assert partner_identity.partner_issuer() not in staff_issuers


def test_misconfiguration_answers_503(monkeypatch):
    partner_identity.set_partner_verifier(None)
    monkeypatch.setenv(partner_identity.ENV_PARTNER_WEB_CLIENT_ID, "not-a-client-id!")
    client, _ = _client([_row(T1, M1)])
    r = _post(client, "a.b.c")
    assert r.status_code == 503
    partner_identity.set_partner_verifier(None)
