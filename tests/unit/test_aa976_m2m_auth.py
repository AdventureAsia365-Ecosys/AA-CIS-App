"""AA-976 — CIS verifies M2M client-credentials tokens of the aa-m2m pool (root ADR 0006b decision 7).

Runs the real shared verifier (api/core/identity_verifier.py) against a local issuer: the outcome classes 401 / 403 /
503 that the catalog routes map, the scope split, and the configuration rules (env override, the M2M issuer must not
be the WORKFORCE issuer, a malformed client id fails closed).
"""
import pytest

from api.core import identity_config, m2m_auth
from api.core.identity_verifier import AuthUnavailable, InvalidToken, MultiIssuerVerifier
from tests.unit.aa976_m2m import CRM, ISS, OTHER_CLIENT, READ, M2MIssuer
from tests.unit.test_identity_verifier import FakeFetcher


@pytest.fixture
def issuer():
    return M2MIssuer(ISS)


def _verifier(issuer, fetcher=None):
    return MultiIssuerVerifier([m2m_auth.m2m_issuer_config()], fetcher or FakeFetcher(issuer))


@pytest.mark.asyncio
async def test_client_credentials_token_gives_the_caller_and_its_scopes(issuer):
    caller = await m2m_auth.verify_caller(_verifier(issuer), issuer.token())
    assert caller.client_id == CRM
    assert caller.scopes == {"booking.b2b/read", READ}
    assert caller.has(READ)


@pytest.mark.asyncio
async def test_scope_as_a_list_is_split_too(issuer):
    caller = await m2m_auth.verify_caller(_verifier(issuer), issuer.token(scope=[READ, "a/b c/d"]))
    assert caller.scopes == {READ, "a/b", "c/d"}


@pytest.mark.asyncio
async def test_another_client_of_the_pool_is_refused_not_unauthorized(issuer):
    with pytest.raises(m2m_auth.CallerRefused):
        await m2m_auth.verify_caller(_verifier(issuer), issuer.token(client_id=OTHER_CLIENT))


@pytest.mark.asyncio
async def test_shared_verifier_message_for_a_foreign_client_is_unchanged(issuer):
    """verify_caller maps exactly this message to 403; if the shared module ever rewords it, this fails."""
    with pytest.raises(InvalidToken, match=f"^{m2m_auth.CLIENT_NOT_ALLOWED}$"):
        await _verifier(issuer).verify(issuer.token(client_id=OTHER_CLIENT))


@pytest.mark.asyncio
async def test_a_user_token_of_the_pool_is_refused(issuer):
    with pytest.raises(m2m_auth.CallerRefused, match="client-credentials"):
        await m2m_auth.verify_caller(_verifier(issuer), issuer.token(sub="a-person"))


@pytest.mark.asyncio
@pytest.mark.parametrize("claims", [{"exp": 1}, {"token_use": "id"}, {"iss": identity_config.DEV_WORKFORCE_ISSUER}])
async def test_expired_id_or_foreign_issuer_tokens_are_unauthorized(issuer, claims):
    with pytest.raises(InvalidToken):
        await m2m_auth.verify_caller(_verifier(issuer), issuer.token(**claims))


@pytest.mark.asyncio
async def test_a_token_signed_by_another_key_is_unauthorized(issuer):
    forger = M2MIssuer(ISS)
    with pytest.raises(InvalidToken):
        await m2m_auth.verify_caller(_verifier(issuer), forger.token())


@pytest.mark.asyncio
async def test_no_key_set_ever_loaded_is_unavailable(issuer):
    fetcher = FakeFetcher(issuer)
    fetcher.fail = True
    with pytest.raises(AuthUnavailable):
        await m2m_auth.verify_caller(_verifier(issuer, fetcher), issuer.token())


def test_dev_defaults_and_env_override(monkeypatch):
    monkeypatch.delenv(m2m_auth.ENV_M2M_ISSUER, raising=False)
    monkeypatch.delenv(m2m_auth.ENV_M2M_CRM_CLIENT_ID, raising=False)
    cfg = m2m_auth.m2m_issuer_config()
    assert (cfg.issuer, cfg.client_ids, cfg.groups_claim) == (ISS, frozenset({CRM}), "scope")
    monkeypatch.setenv(m2m_auth.ENV_M2M_ISSUER, "https://cognito-idp.us-west-1.amazonaws.com/us-west-1_Other")
    monkeypatch.setenv(m2m_auth.ENV_M2M_CRM_CLIENT_ID, "abc123")
    cfg = m2m_auth.m2m_issuer_config()
    assert (cfg.issuer, cfg.client_ids) == ("https://cognito-idp.us-west-1.amazonaws.com/us-west-1_Other",
                                           frozenset({"abc123"}))


def test_m2m_issuer_equal_to_workforce_fails_closed(monkeypatch):
    monkeypatch.setenv(m2m_auth.ENV_M2M_ISSUER, identity_config.workforce_issuer())
    with pytest.raises(ValueError, match="WORKFORCE"):
        m2m_auth.m2m_issuer_config()


def test_malformed_client_id_fails_closed(monkeypatch):
    monkeypatch.setenv(m2m_auth.ENV_M2M_CRM_CLIENT_ID, "not a client id")
    with pytest.raises(ValueError):
        m2m_auth.m2m_issuer_config()


def test_no_prefetch_under_test_environment(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "test")
    assert m2m_auth.start_m2m_jwks_prefetch() is None


@pytest.mark.asyncio
async def test_prefetch_never_raises(monkeypatch, issuer):
    fetcher = FakeFetcher(issuer)
    fetcher.fail = True
    m2m_auth.set_m2m_verifier(_verifier(issuer, fetcher))
    try:
        assert await m2m_auth.prefetch_m2m_jwks() == {ISS: 0}
        fetcher.fail = False
        assert await m2m_auth.prefetch_m2m_jwks() == {ISS: 1}
    finally:
        m2m_auth.set_m2m_verifier(None)
