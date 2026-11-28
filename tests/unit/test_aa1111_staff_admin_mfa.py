"""AA-1111 slice S2 — api/core/staff_mfa.py: an administrator session (cis-admin, or the owner flag) without an
authenticator code (token amr lacks "otp") is refused 403 mfa_required only while cis.platform.admin_mfa_required is
on; off it is logged (would_refuse) and served. Other roles, the Preview viewer and the Dev-only read-only E2E
identities are never checked. Wired into staff_identity.authenticate, so the bearer middleware and
/auth/verify-admin both answer it. Local issuer and fake directory: no network, no AWS, no DB."""
from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from structlog.testing import capture_logs

from api.core import identity_config, pilot_fence, staff_identity, staff_mfa, staff_mfa_settings
from api.middleware.staff_bearer import StaffBearerMiddleware
from tests.unit.aa854_cognito_staff import ISS, SUB, Directory, install_issuer

TOTP_AMR = ["pwd", "otp", "mfa"]
EMAIL_CODE_AMR = ["email_otp"]


@pytest.fixture
def issuer():
    iss, _ = install_issuer()
    staff_mfa.clear_log_throttle()
    yield iss
    identity_config.reset_cis_staff_verifier()
    staff_identity.clear_cache()
    staff_mfa.clear_log_throttle()


@pytest.fixture
def directory():
    return Directory()


@pytest.fixture
def switch(monkeypatch):
    state = {"on": False}

    def fake_get(key, tenant_id=None):
        if key == pilot_fence.PILOT_E2E_FENCE:      # AA-1054: the pilot fence stays off here
            return False
        assert key == staff_mfa_settings.ADMIN_MFA_REQUIRED
        return state["on"]

    from shared import settings

    monkeypatch.setattr(settings, "get", fake_get)
    return state


def _token(issuer, *, groups=("cis-admin",), amr=None):
    claims = {"cognito:groups": list(groups)}
    if amr is not None:
        claims["amr"] = amr
    return issuer.token(**claims)


# ── token_amr ──────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("value, expected", [
    (TOTP_AMR, ("pwd", "otp", "mfa")),
    ("otp", ("otp",)),
    ([1, "otp", None], ("otp",)),
    ({"otp": True}, ()),
])
def test_token_amr_reads_the_claim_shapes(issuer, value, expected):
    assert staff_mfa.token_amr(issuer.token(amr=value)) == expected


def test_token_amr_absent_or_malformed_is_empty(issuer):
    assert staff_mfa.token_amr(issuer.token()) == ()
    assert staff_mfa.token_amr("not-a-jwt") == ()
    assert staff_mfa.token_amr(None) == ()


def test_only_otp_counts_as_mfa():
    assert staff_mfa.mfa_used(("pwd", "otp", "mfa"))
    assert not staff_mfa.mfa_used(("pwd", "email_otp", "mfa"))
    assert not staff_mfa.mfa_used(("email_otp",))
    assert not staff_mfa.mfa_used(())


# ── authenticate (the one path of the middleware and /auth/verify-admin) ───────────────────────────

@pytest.mark.asyncio
async def test_switch_on_refuses_an_admin_without_the_code(issuer, directory, switch):
    directory.add(role="admin", iss=ISS, sub=SUB)
    switch["on"] = True
    with capture_logs() as logs:
        with pytest.raises(staff_identity.StaffAuthError) as err:
            await staff_identity.authenticate(directory, _token(issuer, amr=EMAIL_CODE_AMR))
    assert (err.value.status, err.value.detail, err.value.reason) == (403, "mfa_required", "mfa_required")
    line = next(e for e in logs if e["event"] == "staff_admin_mfa_check")
    assert line["outcome"] == "refused" and line["amr"] == EMAIL_CODE_AMR and line["role"] == "admin"


@pytest.mark.asyncio
async def test_switch_on_refuses_an_admin_token_with_no_amr(issuer, directory, switch):
    directory.add(role="admin", iss=ISS, sub=SUB)
    switch["on"] = True
    with pytest.raises(staff_identity.StaffAuthError) as err:
        await staff_identity.authenticate(directory, _token(issuer))
    assert err.value.status == 403 and err.value.detail == "mfa_required"


@pytest.mark.asyncio
async def test_switch_on_accepts_an_admin_who_used_the_code(issuer, directory, switch):
    uid = directory.add(role="admin", iss=ISS, sub=SUB)
    switch["on"] = True
    with capture_logs() as logs:
        identity = await staff_identity.authenticate(directory, _token(issuer, amr=TOTP_AMR))
    assert identity.admin_id == uid and identity.role == "admin"
    assert [e["outcome"] for e in logs if e["event"] == "staff_admin_mfa_check"] == ["satisfied"]


@pytest.mark.asyncio
async def test_switch_off_serves_the_admin_and_logs_would_refuse_once(issuer, directory, switch):
    uid = directory.add(role="admin", iss=ISS, sub=SUB)
    token = _token(issuer, amr=EMAIL_CODE_AMR)
    with capture_logs() as logs:
        first = await staff_identity.authenticate(directory, token)
        second = await staff_identity.authenticate(directory, token)
    assert first.admin_id == second.admin_id == uid
    checks = [e for e in logs if e["event"] == "staff_admin_mfa_check"]
    assert [e["outcome"] for e in checks] == ["would_refuse"]  # throttled: one line per identity and outcome


@pytest.mark.asyncio
async def test_switch_on_refuses_an_owner_with_a_lower_role(issuer, directory, switch):
    uid = directory.add(role="content_lead", iss=ISS, sub=SUB)
    directory.row(uid)["is_owner"] = True
    switch["on"] = True
    with pytest.raises(staff_identity.StaffAuthError) as err:
        await staff_identity.authenticate(directory, _token(issuer, groups=("cis-content_lead",),
                                                            amr=EMAIL_CODE_AMR))
    assert err.value.detail == "mfa_required"


@pytest.mark.asyncio
@pytest.mark.parametrize("group, role", [
    ("cis-content_lead", "content_lead"),
    ("cis-ai_ops", "ai_ops"),
    ("cis-customer_success", "customer_success"),
    ("cis-finance", "finance"),
    ("cis-support", "support"),
    ("cis-content", "content"),
])
async def test_switch_on_leaves_every_other_role_alone(issuer, directory, switch, group, role):
    directory.add(role=role, iss=ISS, sub=SUB)
    switch["on"] = True
    with capture_logs() as logs:
        identity = await staff_identity.authenticate(directory, _token(issuer, groups=(group,), amr=EMAIL_CODE_AMR))
    assert identity.role == role
    assert not [e for e in logs if e["event"] == "staff_admin_mfa_check"]


def test_preview_and_e2e_read_only_sessions_are_never_checked(switch):
    switch["on"] = True
    base = staff_identity.StaffIdentity(
        admin_id="a-1", username="u", role="admin", db_role="admin", email=None, display_name=None,
        iss=ISS, sub=SUB, client_id="c", is_owner=True)
    assert staff_mfa.is_admin_session(base)
    for capped in (replace(base, preview=True, role="viewer", is_owner=False),
                   replace(base, e2e_read_only=True, is_owner=False)):
        assert not staff_mfa.is_admin_session(capped)
        staff_mfa.check(capped, "not-a-jwt")  # no raise


def test_unknown_key_reads_off(monkeypatch):
    from shared import settings

    def missing(key, tenant_id=None):
        raise KeyError(key)

    monkeypatch.setattr(settings, "get", missing)
    staff_mfa.clear_log_throttle()
    with capture_logs() as logs:
        assert staff_mfa.required() is False
    assert any(e.get("outcome") == "switch_unknown" for e in logs)


# ── the bearer middleware answers it as JSON 403 ───────────────────────────────────────────────────

def test_bearer_middleware_answers_403_mfa_required(issuer, directory, switch):
    directory.add(role="admin", iss=ISS, sub=SUB)
    switch["on"] = True
    app = FastAPI()

    @app.get("/admin/echo")
    async def _echo():
        return {"ok": True}

    app.add_middleware(StaffBearerMiddleware)
    app.state.pool = directory
    client = TestClient(app)
    refused = client.get("/admin/echo", headers={"Authorization": f"Bearer {_token(issuer, amr=EMAIL_CODE_AMR)}"})
    assert refused.status_code == 403 and refused.json() == {"detail": "mfa_required"}
    ok = client.get("/admin/echo", headers={"Authorization": f"Bearer {_token(issuer, amr=TOTP_AMR)}"})
    assert ok.status_code == 200
