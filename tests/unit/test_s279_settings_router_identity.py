"""S279 review — Platform › Settings writes take the proposer / approver from the verified Cognito staff row, never
from the x-admin-* headers (any holder of the admin secret can set those, so a forged X-Admin-User-Id would make a
self-approval look like a second person and skip the tech-lead e-mail). No DB: the service is replaced."""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from api.routers import admin_settings_registry as router
from api.services import settings_registry as reg


def _request(identity=None):
    state = {"staff_identity": identity} if identity is not None else {}
    return SimpleNamespace(scope={"state": state}, app=SimpleNamespace(state=SimpleNamespace(pool=object())))


def _ident(role="admin", admin_id="zz-admin-1", auth="cognito", **kw):
    return SimpleNamespace(role=role, admin_id=admin_id, auth=auth, e2e_read_only=False, **kw)


@pytest.fixture
def calls(monkeypatch):
    seen: list = []

    async def propose(pool, key, **kw):
        seen.append(("propose", key, kw))
        return {"status": "proposed"}

    async def decide(pool, value_id, **kw):
        seen.append(("decide", value_id, kw))
        return {"status": "approved" if kw["approve"] else "rejected"}
    monkeypatch.setattr(reg, "propose", propose)
    monkeypatch.setattr(reg, "decide", decide)
    # no contextvar identity left over from another test
    from api.core.staff_identity import current_staff
    token = current_staff.set(None)
    yield seen
    current_staff.reset(token)


BODY = router.ProposalBody(value=12, reason="zz test: a reason of at least twenty characters", base_version=0)
VID = "00000000-0000-4000-8000-00000000a279"


@pytest.mark.asyncio
@pytest.mark.parametrize("identity", [None, "legacy", "no_admin_id", "e2e"])
async def test_writes_need_a_verified_company_sign_in(calls, identity):
    ident = {None: None, "legacy": _ident(auth="legacy"), "no_admin_id": _ident(admin_id=None),
             "e2e": SimpleNamespace(role="admin", admin_id="zz-e2e", auth="cognito", e2e_read_only=True)}[identity]
    for call in (lambda: router.propose(_request(ident), "cis.publish.daily_limit_default", BODY),
                 lambda: router.approve(_request(ident), VID, router.DecisionBody()),
                 lambda: router.reject(_request(ident), VID, router.DecisionBody(note="zz no"))):
        with pytest.raises(HTTPException) as e:
            await call()
        assert e.value.status_code == 403
    assert calls == []


@pytest.mark.asyncio
async def test_the_actor_and_role_come_from_the_verified_identity(calls):
    await router.propose(_request(_ident()), "cis.publish.daily_limit_default", BODY)
    await router.approve(_request(_ident(admin_id="zz-admin-2")), VID, router.DecisionBody())
    (_, key, p), (_, vid, d) = calls
    assert p["actor"] == "admin:zz-admin-1" and p["can_approve"] is True
    assert d["actor"] == "admin:zz-admin-2" and d["approve"] is True and vid == VID


@pytest.mark.asyncio
async def test_a_verified_role_without_the_capability_is_refused(calls):
    with pytest.raises(HTTPException) as e:
        await router.propose(_request(_ident(role="ai_ops")), "cis.publish.daily_limit_default", BODY)
    assert e.value.status_code == 403 and "settings.write" in e.value.detail
    with pytest.raises(HTTPException) as e:
        await router.approve(_request(_ident(role="content_lead")), VID, router.DecisionBody())
    assert e.value.status_code == 403 and calls == []
