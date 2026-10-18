"""AA-816 / AA-854 C3 — POST /auth/verify-admin accepts a WORKFORCE access token.

The path is chosen by the unverified `iss`: Cognito tokens go through the C2 verifier and
api/core/staff_identity.py, legacy HS256 tokens (no `iss`) keep the old path behind
LEGACY_ADMIN_LOGIN_ENABLED (default true). Covers: role from the highest cis-* group, booking-only groups
403, inactive / unknown 401, first sign-in binds by email exactly once, role drift logged (not denied),
last_login_at, the (iss, sub) cache, refused clients / token kinds, 503 when the verifier, the JWKS or the
directory is unavailable, and the legacy flag.
"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient
from structlog.testing import capture_logs

import api.main as main_mod
from api.core import identity_config, staff_identity
from api.main import app, get_pool
from api.routers.auth import _create_admin_jwt
from tests.unit.aa854_cognito_staff import (
    BOOKING_STAFF, BREAKGLASS, DROP, EMAIL, ISS, SUB, Directory, install_issuer,
)


@pytest.fixture
def directory():
    d = Directory()
    app.dependency_overrides[get_pool] = lambda: d
    yield d
    app.dependency_overrides.pop(get_pool, None)


@pytest.fixture
def issuer(monkeypatch):
    monkeypatch.delenv("LEGACY_ADMIN_LOGIN_ENABLED", raising=False)
    iss, _ = install_issuer()
    main_mod._staff_state_cache.clear()
    yield iss
    identity_config.reset_cis_staff_verifier()
    staff_identity.clear_cache()
    main_mod._staff_state_cache.clear()


@pytest.fixture
def client(directory, issuer):
    return TestClient(app)


def _verify(client, token):
    return client.post("/auth/verify-admin", json={"token": token})


def test_linked_admin_gets_200_with_directory_fields(client, directory, issuer):
    uid = directory.add(role="admin", iss=ISS, sub=SUB)
    res = _verify(client, issuer.token())
    assert res.status_code == 200, res.text
    body = res.json()
    assert body == {"admin_id": uid, "username": "staff-1", "role": "admin", "valid": True,
                    "email": EMAIL, "display_name": "Staff One", "auth": "cognito"}
    assert directory.touches == [uid]          # last_login_at
    assert directory.links == 0


def test_first_sign_in_binds_by_email_exactly_once(client, directory, issuer):
    uid = directory.add(role="reviewer", email=EMAIL)
    with capture_logs() as logs:
        res = _verify(client, issuer.token(**{"cognito:groups": ["cis-reviewer"], "email": "Staff@Adventure.Asia"}))
    assert res.status_code == 200, res.text
    assert res.json()["admin_id"] == uid
    row = directory.row(uid)
    assert (row["cognito_iss"], row["cognito_sub"]) == (ISS, SUB)
    assert directory.links == 1
    assert any(e["event"] == "staff_identity_linked" and e["admin_id"] == uid for e in logs)
    # Later sign-ins find the row by (iss, sub); nothing is bound again.
    staff_identity.clear_cache()
    assert _verify(client, issuer.token(**{"cognito:groups": ["cis-reviewer"]})).status_code == 200
    assert directory.links == 1


def test_a_row_bound_to_another_identity_is_never_rebound(client, directory, issuer):
    uid = directory.add(email=EMAIL, iss=ISS, sub="another-sub-from-a-rebuilt-user")
    with capture_logs() as logs:
        res = _verify(client, issuer.token())
    assert res.status_code == 401
    assert res.json()["detail"] == "Staff user is inactive or unknown"
    assert directory.row(uid)["cognito_sub"] == "another-sub-from-a-rebuilt-user"
    assert directory.links == 0
    assert any(e["event"] == "staff_identity_refused" and e["reason"] == "bound_to_other_identity" for e in logs)


def test_booking_only_groups_are_refused_403_before_the_directory(client, directory, issuer):
    directory.add(iss=ISS, sub=SUB)
    res = _verify(client, issuer.token(**{"cognito:groups": ["booking-admin", "booking-sales"]}))
    assert res.status_code == 403
    assert res.json()["detail"] == "No CIS staff group"
    assert directory.sql == []


def test_no_groups_claim_is_refused_403(client, directory, issuer):
    res = _verify(client, issuer.token(**{"cognito:groups": DROP}))
    assert res.status_code == 403


def test_inactive_row_is_refused_401(client, directory, issuer):
    uid = directory.add(active=False, iss=ISS, sub=SUB)
    res = _verify(client, issuer.token())
    assert res.status_code == 401
    assert directory.touches == []
    # An inactive row is not bound on a first sign-in either.
    directory.row(uid).update(cognito_iss=None, cognito_sub=None)
    staff_identity.clear_cache()
    assert _verify(client, issuer.token()).status_code == 401
    assert directory.links == 0


def test_unknown_identity_and_email_is_refused_401(client, directory, issuer):
    directory.add(email="someone.else@adventure.asia")
    with capture_logs() as logs:
        assert _verify(client, issuer.token()).status_code == 401
    assert any(e.get("reason") == "unknown_email" for e in logs)


def test_role_comes_from_the_highest_group_and_drift_is_logged_not_denied(client, directory, issuer):
    uid = directory.add(role="admin", iss=ISS, sub=SUB)
    with capture_logs() as logs:
        res = _verify(client, issuer.token(**{"cognito:groups": ["cis-content", "cis-reviewer", "booking-admin"]}))
    assert res.status_code == 200, res.text
    assert res.json()["role"] == "reviewer"
    drift = [e for e in logs if e["event"] == "staff_role_drift"]
    assert drift == [{"event": "staff_role_drift", "log_level": "warning", "admin_id": uid,
                      "group_role": "reviewer", "db_role": "admin", "sub": SUB}]


@pytest.mark.parametrize("groups, role", [
    (["cis-content", "cis-admin"], "admin"),
    (["cis-content"], "content"),
    (["booking-admin", "cis-content", "cis-reviewer"], "reviewer"),
])
def test_role_order_admin_reviewer_content(client, directory, issuer, groups, role):
    directory.add(role=role, iss=ISS, sub=SUB)
    res = _verify(client, issuer.token(**{"cognito:groups": groups}))
    assert res.json()["role"] == role


def test_breakglass_client_is_accepted(client, directory, issuer):
    directory.add(iss=ISS, sub=SUB)
    assert _verify(client, issuer.token(client_id=BREAKGLASS)).status_code == 200


@pytest.mark.parametrize("claims", [
    {"client_id": BOOKING_STAFF},                   # not a CIS staff client
    {"token_use": "id"},                            # ID token
    {"exp": int(time.time()) - 3600},               # expired
    {"iss": "https://cognito-idp.us-west-1.amazonaws.com/us-west-1_OTHERPOOL"},
])
def test_unacceptable_tokens_are_refused_401_before_the_directory(client, directory, issuer, claims):
    directory.add(iss=ISS, sub=SUB)
    res = _verify(client, issuer.token(**claims))
    assert res.status_code == 401
    assert res.json()["detail"] == "Invalid staff token"
    assert directory.sql == []


def test_token_signed_by_another_key_is_refused_401(client, directory, issuer):
    from tests.unit.test_identity_verifier import _rsa_key

    directory.add(iss=ISS, sub=SUB)
    assert _verify(client, issuer.token(key=_rsa_key())).status_code == 401


def test_the_identity_row_is_cached_and_last_login_written_once(client, directory, issuer):
    uid = directory.add(iss=ISS, sub=SUB)
    for _ in range(3):
        assert _verify(client, issuer.token()).status_code == 200
    assert directory.sql.count(staff_identity.SQL_BY_IDENTITY) == 1
    assert directory.touches == [uid]


def test_a_deactivation_takes_effect_after_the_ttl(client, directory, issuer, monkeypatch):
    uid = directory.add(iss=ISS, sub=SUB)
    assert _verify(client, issuer.token()).status_code == 200
    directory.row(uid)["is_active"] = False
    assert _verify(client, issuer.token()).status_code == 200   # within the TTL
    later = time.monotonic() + staff_identity.STAFF_STATE_TTL_S + 1
    monkeypatch.setattr(staff_identity.time, "monotonic", lambda: later)
    assert _verify(client, issuer.token()).status_code == 401


def test_verifier_build_failure_is_503_not_500(client, directory, monkeypatch):
    identity_config.reset_cis_staff_verifier()
    monkeypatch.setenv(identity_config.ENV_WORKFORCE_ISSUER, "http://not-https.example")
    from tests.unit.aa854_cognito_staff import StaffIssuer

    res = _verify(client, StaffIssuer(ISS).token())
    assert res.status_code == 503
    assert res.json()["detail"] == "Staff sign-in is unavailable"


def test_jwks_never_loaded_is_503(client, directory, monkeypatch):
    issuer, fetcher = install_issuer()
    fetcher.fail = True
    assert _verify(client, issuer.token()).status_code == 503


def test_directory_outage_is_503(client, directory, issuer):
    directory.fail = True
    assert _verify(client, issuer.token()).status_code == 503


# ── Legacy HS256 path (D3: LEGACY_ADMIN_LOGIN_ENABLED) ─────────────────────────────────────────────

class _LegacyPool(Directory):
    async def fetchrow(self, sql, *args):
        uid = str(args[0])
        r = next((r for r in self.rows if r["id"] == uid and r["is_active"]), None)
        return None if r is None else {k: r[k] for k in ("id", "username", "role", "email", "display_name")}


@pytest.fixture
def legacy(issuer):
    pool = _LegacyPool()
    app.dependency_overrides[get_pool] = lambda: pool
    yield pool
    app.dependency_overrides.pop(get_pool, None)


def test_legacy_token_unchanged_by_default_and_reports_auth_legacy(legacy):
    uid = legacy.add(role="content", email=None, display_name=None, username="u-content")
    res = _verify(TestClient(app), _create_admin_jwt(uid, "u-content", "content"))
    assert res.status_code == 200, res.text
    assert res.json() == {"admin_id": uid, "username": "u-content", "role": "content", "valid": True,
                          "email": None, "display_name": None, "auth": "legacy"}


@pytest.mark.parametrize("value", ["false", "0", "no", "off"])
def test_legacy_flag_off_refuses_hs256_but_not_cognito(legacy, issuer, monkeypatch, value):
    monkeypatch.setenv("LEGACY_ADMIN_LOGIN_ENABLED", value)
    uid = legacy.add(role="admin")
    client = TestClient(app)
    res = _verify(client, _create_admin_jwt(uid, "staff-1", "admin"))
    assert res.status_code == 401
    assert res.json()["detail"] == "Password sessions are switched off; sign in again"

    directory = Directory()
    directory.add(iss=ISS, sub=SUB)
    app.dependency_overrides[get_pool] = lambda: directory
    assert _verify(client, issuer.token()).status_code == 200


@pytest.mark.parametrize("value", ["true", "1", "yes", "TRUE", "", "  ", "on", "ture", "enabled"])
def test_legacy_flag_stays_on_unless_explicitly_off(legacy, monkeypatch, value):
    """Only 0/false/no/off switch the rollback path off; empty or a typo keeps it (review finding 4)."""
    monkeypatch.setenv("LEGACY_ADMIN_LOGIN_ENABLED", value)
    uid = legacy.add(role="admin")
    assert _verify(TestClient(app), _create_admin_jwt(uid, "staff-1", "admin")).status_code == 200


def test_token_kind_picks_the_path_by_iss():
    issuer = install_issuer()[0]
    try:
        assert staff_identity.token_kind(issuer.token()) == "cognito"
        assert staff_identity.token_kind(_create_admin_jwt(SUB, "x", "admin")) == "legacy"
        assert staff_identity.token_kind("not.a.jwt") == "malformed"
        assert staff_identity.token_kind("") == "malformed"
        assert staff_identity.token_kind(None) == "malformed"
        assert staff_identity.token_kind("a" * 9000) == "malformed"
        # Another issuer (EXTERNAL portal, CIS-minted Booking RS256) is not a CIS staff token.
        assert staff_identity.token_kind(issuer.token(iss="https://cognito-idp.us-west-1.amazonaws.com/x")) == "other"
    finally:
        identity_config.reset_cis_staff_verifier()


def test_token_kind_fails_closed_when_the_verifier_can_not_be_built(monkeypatch):
    """A bad issuer override must not turn WORKFORCE tokens into pass-through ones: they stay "cognito" so
    the caller answers 503."""
    from tests.unit.aa854_cognito_staff import StaffIssuer

    identity_config.reset_cis_staff_verifier()
    monkeypatch.setenv(identity_config.ENV_WORKFORCE_ISSUER, "http://not-https.example")
    try:
        assert staff_identity.token_kind(StaffIssuer(ISS).token()) == "cognito"
    finally:
        identity_config.reset_cis_staff_verifier()


def test_foreign_issuer_is_refused_401_without_the_legacy_path(client, directory, issuer, monkeypatch):
    """A JWT from another issuer never reaches the HS256 path or the directory, legacy flag on or off."""
    directory.add(iss=ISS, sub=SUB)
    for flag in ("true", "false"):
        monkeypatch.setenv("LEGACY_ADMIN_LOGIN_ENABLED", flag)
        with capture_logs() as logs:
            res = _verify(client, issuer.token(iss="https://cognito-idp.us-west-1.amazonaws.com/us-west-1_EXT"))
        assert res.status_code == 401
        assert res.json()["detail"] == "Invalid staff token"
        assert any(e.get("reason") == "foreign_issuer" for e in logs)
    assert directory.sql == []


def test_first_link_refused_when_email_verified_is_false(client, directory, issuer):
    """ADR 0006 §6: link by email only on a verified address (review finding 5)."""
    uid = directory.add()
    with capture_logs() as logs:
        res = _verify(client, issuer.token(email_verified="false"))
    assert res.status_code == 401
    assert directory.links == 0 and directory.row(uid)["cognito_sub"] is None
    assert any(e.get("reason") == "email_not_verified" for e in logs)


def test_first_link_allowed_when_email_verified_is_true(client, directory, issuer):
    uid = directory.add()
    assert _verify(client, issuer.token(email_verified=True)).status_code == 200
    assert directory.row(uid)["cognito_sub"] == SUB


def test_last_login_write_failure_does_not_fail_the_sign_in(client, directory, issuer):
    """last_login_at is bookkeeping (review finding 3): first load and cache hit both still answer 200."""
    uid = directory.add()                           # unbound: the first call links, then the touch fails
    directory.fail_touch = True
    with capture_logs() as logs:
        first = _verify(client, issuer.token())
        second = _verify(client, issuer.token())    # cache hit, touch retried, fails again
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert directory.row(uid)["cognito_sub"] == SUB
    assert [e["event"] for e in logs].count("staff_identity_touch_failed") == 2
    directory.fail_touch = False
    assert _verify(client, issuer.token()).status_code == 200   # retried once it works
    assert directory.touches == [uid]
    assert _verify(client, issuer.token()).status_code == 200   # then at most once per cache entry
    assert directory.touches == [uid]
