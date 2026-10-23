"""AA-716 / AA-986 L2 — trashing a source tour cascades to its Master, atoms, review queue and tenants.

AA-716 wrote that cascade inside the trash / restore routes. AA-986 L2 moved every writer of a tour's served state
into one service (services/tour_lifecycle, ADR 0015 decision 2); the cascade itself (SQL, notifications, jobs,
the lifecycle record) is tested on Postgres in tests/integration/test_aa986_l2_tour_lifecycle.py. These unit tests
pin the routes' side of it: each route calls the right action with the caller as actor, maps a refusal to its HTTP
status and code, and runs the post-commit recompute with the state the action reports (the AA-986 L1 restore fix:
"active" when the Master came back, "inactive" otherwise).
"""
from contextlib import asynccontextmanager
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from api.routers import admin
from services import tour_lifecycle as tl
from services.notifications import EventType


@pytest.fixture(autouse=True)
def _admin_secret(monkeypatch):
    monkeypatch.setattr(admin, "ADMIN_SECRET", "test-secret")


class _Pool:
    @asynccontextmanager
    async def acquire(self):
        yield SimpleNamespace(name="conn")


def _req():
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=_Pool())))


def _outcome(action, **kw):
    out = tl.Outcome(action, "tour-1", "key-1", "Xi'an Highlights")
    for k, v in kw.items():
        setattr(out, k, v)
    return out


@pytest.mark.asyncio
async def test_trash_route_calls_retire_source_and_recomputes_after_commit():
    out = _outcome("retire_source", masters_off=[("tour-1", "source_deleted")],
                   recompute=[("tour-1", "inactive"), ("tour-1", "trashed")],
                   extra={"deleted_at": datetime(2026, 10, 7)})
    with patch.object(tl, "retire_source", AsyncMock(return_value=out)) as act, \
            patch.object(admin, "_recompute_after_status_change", new=AsyncMock()) as rec, \
            patch.object(admin, "_require_master_admin_if_published", new=AsyncMock()) as q8:
        res = await admin.trash_source_tour("tour-1", _req(), x_admin_secret="test-secret", x_admin_role="admin",
                                            x_admin_user_id="staff-9")
    q8.assert_awaited_once()
    assert act.await_args.kwargs["actor"] == "staff-9"
    assert [c.args[1:] for c in rec.await_args_list] == [("tour-1", "inactive"), ("tour-1", "trashed")]
    assert res["source_status"] == "trashed" and res["master_inactivated"] is True
    assert res["deleted_at"] == "2026-10-07T00:00:00"


@pytest.mark.asyncio
async def test_restore_route_recomputes_with_the_state_the_service_reports():
    out = _outcome("restore_source", masters_on=["tour-1"], recompute=[("tour-1", "active")])
    with patch.object(tl, "restore_source", AsyncMock(return_value=out)), \
            patch.object(admin, "_recompute_after_status_change", new=AsyncMock()) as rec:
        res = await admin.restore_source_tour("tour-1", _req(), x_admin_secret="test-secret", x_admin_user_id=None)
    assert rec.await_args.args[2] == "active"
    assert res["source_status"] == "active" and res["master_reactivated"] is True


@pytest.mark.asyncio
async def test_a_refusal_maps_to_its_status_and_code():
    err = tl.LifecycleError(409, "source_not_live", "cannot activate this Master: the source tour was deleted")
    with patch.object(tl, "activate_master", AsyncMock(side_effect=err)), \
            patch.object(admin, "_recompute_after_status_change", new=AsyncMock()) as rec:
        with pytest.raises(HTTPException) as e:
            await admin.activate_master_tour("tour-1", _req(), x_admin_secret="test-secret", x_admin_user_id=None)
    assert e.value.status_code == 409 and e.value.detail.startswith("source_not_live: ")
    assert e.value.headers == {"X-Error-Code": "source_not_live"}
    rec.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_404_keeps_its_plain_sentence():
    err = tl.LifecycleError(404, "not_found", "Tour not found or already trashed")
    with patch.object(tl, "retire_source", AsyncMock(side_effect=err)), \
            patch.object(admin, "_require_master_admin_if_published", new=AsyncMock()):
        with pytest.raises(HTTPException) as e:
            await admin.trash_source_tour("tour-1", _req(), x_admin_secret="test-secret", x_admin_role=None,
                                          x_admin_user_id=None)
    assert e.value.status_code == 404 and e.value.detail == "Tour not found or already trashed"


@pytest.mark.asyncio
@pytest.mark.parametrize("route, action", [
    ("trash_master_tour", "trash_master"), ("restore_master_tour", "restore_master"),
    ("activate_master_tour", "activate_master"), ("deactivate_master_tour", "pause_master"),
])
async def test_each_master_button_is_one_service_action(route, action):
    out = _outcome(action, extra={"deleted_at": datetime(2026, 10, 7)})
    with patch.object(tl, action, AsyncMock(return_value=out)) as act, \
            patch.object(admin, "_recompute_after_status_change", new=AsyncMock()):
        await getattr(admin, route)("tour-1", _req(), x_admin_secret="test-secret", x_admin_user_id=None)
    act.assert_awaited_once()
    assert act.await_args.kwargs["actor"] == "admin"


@pytest.mark.asyncio
async def test_q8_a_published_tour_needs_master_admin():
    with patch.object(tl, "key_was_published", AsyncMock(return_value=True)):
        with pytest.raises(HTTPException) as e:
            await admin._require_master_admin_if_published(_req(), "tour-1", "content")
        assert e.value.status_code == 403 and "master.admin" in e.value.detail
        await admin._require_master_admin_if_published(_req(), "tour-1", "admin")      # allowed
    with patch.object(tl, "key_was_published", AsyncMock(return_value=False)):
        await admin._require_master_admin_if_published(_req(), "tour-1", "content")    # S0/A0 only: allowed


def test_tenant_facing_events_target_the_tenant_role():
    from services.notifications import _DEFAULT_ROLES
    assert _DEFAULT_ROLES[EventType.TOUR_DISCONTINUED] == ["tenant"]
    assert _DEFAULT_ROLES[EventType.TOUR_REINSTATED] == ["tenant"]
