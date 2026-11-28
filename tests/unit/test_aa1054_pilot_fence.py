"""AA-1054 (S297; tech lead decision S295 sec-Q11; root ADR 0006a) — the pilot fence of the CIS staff surface.

While ``cis.platform.pilot_e2e_fence`` is on, every staff session whose directory row or token address is of the E2E
mailbox domain is refused 403 ``e2e_pilot_fence`` before any other E2E rule, and every E2E exception that asks
``e2e_staff.dev_only_e2e_environment()`` closes too. Off, nothing changes. Real staff never read the switch. A process
that has a settings pool but could not read the switch (no snapshot yet / since start-up, or the stored value refused)
fails closed: 403 ``e2e_pilot_fence_unknown``. The academy (/learn) is fenced too. No network, no AWS, no DB (the
settings snapshot and the reader's database read are faked).
"""
from __future__ import annotations

import importlib.util

import pytest
import structlog.testing

from api.core import e2e_staff, pilot_fence, staff_identity
from api.core.identity_verifier import Principal
from api.routers import learn
from services.learn import academy as ac
from shared import settings
from shared.settings.reader import Definition, Resolution
from tests.unit import aa1105_learn_fake as lf
from tests.unit.aa854_cognito_staff import ISS, SUB, Directory, StaffIssuer
from tests.unit.test_aa816_e2e_cis_admin import ADMIN_E2E, _set_env, directory, issuer  # noqa: F401 — fixtures

OWNER_E2E = "e2e-owner@e2e-mail.lumiguides.it.com"
CS_E2E = "e2e-cis-cs@e2e-mail.lumiguides.it.com"
CONTENT_E2E = "e2e@e2e-mail.lumiguides.it.com"
STAFF = "lan.tran@adventure.asia"


@pytest.fixture
def fence(monkeypatch):
    """The settings snapshot with a confirmed fence value (``source`` platform), and the reads it saw."""
    state = {"value": False, "reads": 0}
    real_resolve = settings.resolve

    def fake_resolve(key, tenant_id=None):
        if key == pilot_fence.PILOT_E2E_FENCE:
            state["reads"] += 1
            if isinstance(state["value"], BaseException):
                raise state["value"]
            return Resolution(key, state["value"], "platform", version=1)
        return real_resolve(key, tenant_id)

    monkeypatch.setattr(settings, "resolve", fake_resolve)
    pilot_fence.reset()
    yield state
    pilot_fence.reset()


FENCE_DEF = Definition(key=pilot_fence.PILOT_E2E_FENCE, app="cis", area="platform", label="Pilot fence",
                       description="Sample definition of the fence switch for the reader tests.", value_type="bool",
                       default_value=False, impact="high")


class _Pool:
    """Stands for the pool the API lifespan installs; the reader's database read is faked on ``READER._fetch``."""


@pytest.fixture
def reader(monkeypatch):
    """The real settings reader as a fresh API task sees it: a pool installed, no snapshot yet, the fence key defined.
    ``rows`` is what the next database read returns (an exception: the read fails); ``fetches`` counts the reads."""
    r = settings.READER
    monkeypatch.setitem(r._defs, FENCE_DEF.key, FENCE_DEF)
    for name, value in (("_pool", _Pool()), ("_snapshot", None), ("_refused", {}), ("_loaded_at", -1e18),
                        ("_lock", None), ("last_error", None), ("_alerted", set())):
        monkeypatch.setattr(r, name, value)
    state = {"rows": [], "fetches": 0}

    async def fake_fetch(pool):
        state["fetches"] += 1
        if isinstance(state["rows"], BaseException):
            raise state["rows"]
        return list(state["rows"])

    monkeypatch.setattr(r, "_fetch", fake_fetch)
    monkeypatch.setenv("ENVIRONMENT", "dev")
    pilot_fence.reset()
    yield state
    pilot_fence.reset()


def _value_row(value_text: str) -> dict:
    return {"key": FENCE_DEF.key, "tenant_id": None, "value_text": value_text, "version": 3, "effective_from": None}


# ── the helpers ──────────────────────────────────────────────────────────────────────────────────

def test_constants_and_the_compiled_key_once_the_migration_pr_is_on_main():
    assert pilot_fence.PILOT_E2E_FENCE == "cis.platform.pilot_e2e_fence"
    assert pilot_fence.E2E_PILOT_FENCE == "e2e_pilot_fence"
    if importlib.util.find_spec("api.core.pilot_fence_settings") is not None:   # CIS 495's PR (MIG-4 chain)
        from api.core import pilot_fence_settings

        assert pilot_fence_settings.PILOT_E2E_FENCE == pilot_fence.PILOT_E2E_FENCE
        from shared import settings

        d = settings.definition(pilot_fence.PILOT_E2E_FENCE)
        assert d.default_value is False and d.value_type == "bool"


@pytest.mark.parametrize("email,e2e", [
    (CONTENT_E2E, True), (ADMIN_E2E, True), (OWNER_E2E, True), (CS_E2E, True),
    ("e2e-booking-pricing-checker@e2e-mail.lumiguides.it.com", True),
    ("  E2E-Owner@E2E-Mail.Lumiguides.IT.com ", True),
    ("someone-new@e2e-mail.lumiguides.it.com", True),          # a future E2E address is fenced too
    ("K2e@e2e-mail.lumiguides.it.com", True),              # a look-alike local part is fenced, not let through
    (STAFF, False), ("e2e@adventure.asia", False), ("e2e@x.e2e-mail.lumiguides.it.com", False),
    ("e2e@e2e-mail.lumiguides.it.com.evil", False), ("e2e-mail.lumiguides.it.com", False), ("", False), (None, False),
])
def test_is_e2e_identity(email, e2e):
    assert pilot_fence.is_e2e_identity(email) is e2e


def test_every_named_e2e_address_is_an_e2e_identity():
    named = {v for k, v in vars(e2e_staff).items() if k.isupper() and isinstance(v, str) and k.endswith("_EMAIL")}
    assert len(named) >= 12
    assert all(pilot_fence.is_e2e_identity(a) for a in named)


@pytest.mark.parametrize("value,on", [(False, False), (True, True), (None, True), ("false", True), (0, True)])
def test_on_reads_the_switch_and_fails_closed_on_a_strange_value(fence, value, on):
    fence["value"] = value
    assert pilot_fence.on() is on


def test_an_unknown_key_reads_off_and_logs_once(fence):
    pilot_fence.reset()
    fence["value"] = KeyError(pilot_fence.PILOT_E2E_FENCE)
    with structlog.testing.capture_logs() as logs:
        assert pilot_fence.on() is False
        assert pilot_fence.on() is False
    assert [e["event"] for e in logs] == ["pilot_fence_switch_unknown"]


def test_real_staff_never_read_the_switch(fence):
    fence["value"] = True
    assert pilot_fence.refused(STAFF, STAFF) is False
    assert fence["reads"] == 0
    assert pilot_fence.refused(STAFF, ADMIN_E2E) is True and fence["reads"] == 1


# ── the switch cannot be read in this process: fail closed ──────────────────────────────────────

def test_no_snapshot_yet_reads_unknown_and_closes_the_dev_only_exceptions(reader):
    with structlog.testing.capture_logs() as logs:
        assert pilot_fence.state() == pilot_fence.UNKNOWN
        assert pilot_fence.on() is True
        assert e2e_staff.dev_only_e2e_environment() is False
    assert [e["event"] for e in logs] == ["pilot_fence_switch_unconfirmed"]
    assert logs[0]["reason"] == "no_snapshot"
    assert reader["fetches"] == 0                    # the sync read never touches the database


def test_a_snapshot_without_a_stored_value_confirms_the_default_off(reader):
    settings.READER._snapshot = {}
    assert pilot_fence.state() == pilot_fence.OFF
    assert e2e_staff.dev_only_e2e_environment() is True


@pytest.mark.asyncio
async def test_a_refused_stored_value_reads_unknown(reader):
    reader["rows"] = [_value_row('"yes"')]                # not a bool: the reader refuses it and serves the default
    assert await settings.ensure_loaded() is True
    assert settings.resolve(FENCE_DEF.key).refused
    with structlog.testing.capture_logs() as logs:
        assert pilot_fence.state() == pilot_fence.UNKNOWN
    assert logs[0]["event"] == "pilot_fence_switch_unconfirmed" and logs[0]["reason"] == "stored_value_refused"


def test_without_a_pool_or_under_test_the_default_is_the_value(reader, monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "test")
    assert pilot_fence.state() == pilot_fence.OFF
    monkeypatch.setenv("ENVIRONMENT", "dev")
    settings.READER._pool = None
    assert pilot_fence.state() == pilot_fence.OFF


@pytest.mark.asyncio
async def test_ensure_loaded_reads_once_and_not_on_every_call_after_a_failure(reader):
    reader["rows"] = OSError("database unreachable")
    assert await settings.ensure_loaded() is False
    assert await settings.ensure_loaded() is False       # retried after one cache window, not on every call
    assert reader["fetches"] == 1 and settings.READER.last_error


def test_dev_only_environment_closes_under_the_fence(fence, monkeypatch):
    _set_env(monkeypatch, "dev")
    assert e2e_staff.dev_only_e2e_environment() is True
    fence["value"] = True
    assert e2e_staff.dev_only_e2e_environment() is False
    assert not e2e_staff.e2e_booking_role_allowed(CS_E2E, "contracting")
    _set_env(monkeypatch, "prod")
    fence["value"] = False
    assert e2e_staff.dev_only_e2e_environment() is False


# ── resolve_staff: both ways ─────────────────────────────────────────────────────────────────────

def _row(directory: Directory, email: str, role: str = "admin", owner: bool = False) -> str:
    uid = directory.add(role=role, email=email, iss=ISS, sub=SUB, username=email.split("@")[0])
    directory.row(uid)["is_owner"] = owner
    return uid


def _token(issuer: StaffIssuer, email: str, groups) -> str:
    return issuer.token(email=email, **{"cognito:groups": list(groups)})


CASES = [
    pytest.param(ADMIN_E2E, "admin", ("cis-admin",), False, id="a10b-admin"),
    pytest.param(CS_E2E, "customer_success", ("cis-customer_success",), False, id="a10d-cs"),
    pytest.param(OWNER_E2E, "admin", ("cis-admin", "booking-admin"), True, id="a10h-owner"),
    pytest.param(CONTENT_E2E, "content", ("cis-content",), False, id="a10-content"),
]


@pytest.mark.parametrize("email,role,groups,owner", CASES)
@pytest.mark.asyncio
async def test_fence_off_the_e2e_identity_resolves_as_before(issuer, directory, monkeypatch, fence, email, role,
                                                           groups, owner):
    _set_env(monkeypatch, "dev")
    uid = _row(directory, email, role, owner)
    identity = await staff_identity.resolve_staff(directory, await staff_identity.verify_staff_token(
        _token(issuer, email, groups)))
    assert identity.admin_id == uid


@pytest.mark.parametrize("email,role,groups,owner", CASES)
@pytest.mark.asyncio
async def test_fence_on_the_e2e_identity_is_refused_first(issuer, directory, monkeypatch, fence, email, role,
                                                        groups, owner):
    _set_env(monkeypatch, "dev")
    fence["value"] = True
    _row(directory, email, role, owner)
    with structlog.testing.capture_logs() as logs:
        with pytest.raises(staff_identity.StaffAuthError) as exc:
            await staff_identity.authenticate(directory, _token(issuer, email, groups))
    assert (exc.value.status, exc.value.detail, exc.value.reason) == (403, "e2e_pilot_fence", "e2e_pilot_fence")
    assert any(e.get("event") == "staff_identity_refused" and e.get("reason") == "e2e_pilot_fence" for e in logs)


@pytest.mark.asyncio
async def test_fence_on_outside_dev_still_says_pilot_fence(issuer, directory, monkeypatch, fence):
    _set_env(monkeypatch, "prod")
    fence["value"] = True
    _row(directory, ADMIN_E2E)
    with pytest.raises(staff_identity.StaffAuthError) as exc:
        await staff_identity.authenticate(directory, _token(issuer, ADMIN_E2E, ("cis-admin",)))
    assert exc.value.reason == "e2e_pilot_fence"


@pytest.mark.asyncio
async def test_fence_on_a_company_row_with_an_e2e_token_email_is_refused(issuer, directory, monkeypatch, fence):
    _set_env(monkeypatch, "dev")
    fence["value"] = True
    _row(directory, STAFF)
    with pytest.raises(staff_identity.StaffAuthError) as exc:
        await staff_identity.authenticate(directory, _token(issuer, ADMIN_E2E, ("cis-admin",)))
    assert exc.value.reason == "e2e_pilot_fence"


@pytest.mark.parametrize("email,role,groups,owner", CASES)
@pytest.mark.asyncio
async def test_switch_unreadable_the_e2e_identity_is_refused_unknown(issuer, directory, monkeypatch, reader, email,
                                                                    role, groups, owner):
    reader["rows"] = OSError("database unreachable")   # a new task whose first settings read fails
    _row(directory, email, role, owner)
    with structlog.testing.capture_logs() as logs:
        with pytest.raises(staff_identity.StaffAuthError) as exc:
            await staff_identity.authenticate(directory, _token(issuer, email, groups))
    assert (exc.value.status, exc.value.reason) == (403, "e2e_pilot_fence_unknown")
    assert reader["fetches"] == 1
    assert any(e.get("event") == "staff_identity_refused" and e.get("reason") == "e2e_pilot_fence_unknown"
               for e in logs)


@pytest.mark.asyncio
async def test_no_snapshot_yet_the_first_e2e_request_reads_the_switch_then_decides(issuer, directory, reader):
    reader["rows"] = [_value_row("true")]
    _row(directory, ADMIN_E2E)
    with pytest.raises(staff_identity.StaffAuthError) as exc:
        await staff_identity.authenticate(directory, _token(issuer, ADMIN_E2E, ("cis-admin",)))
    assert exc.value.reason == "e2e_pilot_fence" and reader["fetches"] == 1


@pytest.mark.asyncio
async def test_no_snapshot_yet_and_the_switch_reads_off_the_e2e_identity_resolves(issuer, directory, reader):
    reader["rows"] = [_value_row("false")]
    uid = _row(directory, ADMIN_E2E)
    identity = await staff_identity.resolve_staff(directory, await staff_identity.verify_staff_token(
        _token(issuer, ADMIN_E2E, ("cis-admin",))))
    assert identity.admin_id == uid and reader["fetches"] == 1


@pytest.mark.asyncio
async def test_switch_unreadable_real_staff_sign_in_and_nothing_is_read(issuer, directory, reader):
    reader["rows"] = OSError("database unreachable")
    uid = _row(directory, STAFF, "content")
    identity = await staff_identity.authenticate(directory, _token(issuer, STAFF, ("cis-content",)))
    assert identity.admin_id == uid and reader["fetches"] == 0


@pytest.mark.asyncio
async def test_fence_on_real_staff_sign_in_as_usual(issuer, directory, monkeypatch, fence):
    _set_env(monkeypatch, "dev")
    fence["value"] = True
    uid = _row(directory, STAFF, "content")
    identity = await staff_identity.authenticate(directory, _token(issuer, STAFF, ("cis-content",)))
    assert identity.admin_id == uid and identity.role == "content"
    assert fence["reads"] == 0


# ── the academy (/learn) ─────────────────────────────────────────────────────────────────────────

def _learn(email: str) -> tuple:
    store = lf.FakeStore()
    store.staff[(lf.WORKFORCE, lf.SUB)]["email"] = email
    principal = Principal(iss=lf.WORKFORCE, sub=lf.SUB, client_id="academy-web", groups=("cis-content",), email=email,
                          email_verified=None, username=None)
    return store, principal


@pytest.mark.parametrize("email", [CONTENT_E2E, ADMIN_E2E])
@pytest.mark.asyncio
async def test_learn_fence_on_refuses_every_e2e_learner_before_a_learner_row(monkeypatch, fence, email):
    _set_env(monkeypatch, "dev")
    fence["value"] = True
    store, principal = _learn(email)
    with pytest.raises(ac.Problem) as exc:
        await learn.learner_ctx(store, principal)
    assert (exc.value.status, exc.value.error) == (403, "forbidden")
    assert store.learners == {}


@pytest.mark.asyncio
async def test_learn_switch_unreadable_refuses_the_e2e_learner(reader):
    reader["rows"] = OSError("database unreachable")
    store, principal = _learn(CONTENT_E2E)
    with pytest.raises(ac.Problem) as exc:
        await learn.learner_ctx(store, principal)
    assert exc.value.error == "forbidden" and store.learners == {}


@pytest.mark.asyncio
async def test_learn_fence_off_keeps_today_s_rules(monkeypatch, fence):
    _set_env(monkeypatch, "dev")
    store, principal = _learn(CONTENT_E2E)                 # A10: not a Dev-only test learner, served as today
    ctx = await learn.learner_ctx(store, principal)
    assert ctx.learner["is_test"] is False
    fence["value"] = True
    store, principal = _learn(STAFF)
    ctx = await learn.learner_ctx(store, principal)       # real staff never read the switch
    assert ctx.learner["is_test"] is False
