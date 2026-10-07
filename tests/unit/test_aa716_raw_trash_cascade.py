"""AA-716 — trashing a source tour cascades to its Master, atoms, review queue, and tenants.

AA-713 wired master_status -> atoms (deactivate a Master -> its atoms leave v_active_tour_atoms +
Score/Route). AA-716 adds the missing upstream link: trashing the SOURCE tour in raw_tours must
also inactivate its Master, dismiss its pending review rows, warn the adopting tenants, and trigger
the AA-713 recompute — all atomically, with restore reversing the chain.

These are behavioural unit tests over the two endpoints with a fake pool/conn; they assert the SQL
and notifications emitted inside the transaction and that the recompute runs after commit.
"""
import os
from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest

os.environ.setdefault("ADMIN_SECRET", "test-secret")

from api.routers import admin  # noqa: E402
from services.notifications import EventType  # noqa: E402


class _Txn:
    """async context manager for conn.transaction()."""
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc):
        return False


class FakeConn:
    """Fake asyncpg connection. fetchrow is set per-test by _make (the raw_tours pre-check and the
    UPDATE ... RETURNING both go through fetchrow). execute records every statement; notification
    INSERTs are also captured in `emitted`."""
    def __init__(self, raw_row, tenant_ids):
        self._raw_row = raw_row
        self._tenant_ids = tenant_ids
        self.execute = AsyncMock(side_effect=self._execute)
        self.executed = []          # (sql, args)
        self.emitted = []           # notification INSERTs (sql, args)

    async def _execute(self, sql, *args):
        self.executed.append((sql, args))
        if "INSERT INTO shared.notifications" in sql:
            self.emitted.append((sql, args))
            return "INSERT 0 1"
        if "published_tours" in sql:
            return "UPDATE 1"
        if "review_queue" in sql:
            return "UPDATE 2"
        return "UPDATE 1"

    async def fetch(self, sql, *args):
        if "tenant_tour_versions" in sql:
            return [{"tenant_id": t} for t in self._tenant_ids]
        return []

    def transaction(self):
        return _Txn()


class _Acquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class FakePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _Acquire(self._conn)


class FakeRequest:
    def __init__(self, pool):
        self.app = type("App", (), {"state": type("S", (), {"pool": pool})()})()


# fetchrow serves both the raw_tours pre-check and the UPDATE ... RETURNING, so this row satisfies
# both shapes (src_name, tenant_id, deleted_at).
RAW_ROW = {"src_name": "Xi'an Highlights", "tenant_id": "tenant-src",
           "tour_id": "tour-1", "source_status": "trashed",
           "deleted_at": datetime(2026, 10, 7)}


def _make(raw_row=None, tenants=("tenant-a", "tenant-b")):
    conn = FakeConn(raw_row or dict(RAW_ROW), list(tenants))

    # trash/restore call conn.fetchrow twice: pre-check row, then UPDATE ... RETURNING.
    # Both should return a row; drive it off a list.
    rows = [dict(RAW_ROW), dict(RAW_ROW)]

    async def fetchrow(sql, *args):
        if "raw_tours" in sql:
            return rows.pop(0) if rows else dict(RAW_ROW)
        return None
    conn.fetchrow = fetchrow
    return conn, FakeRequest(FakePool(conn))


def _sql_hits(conn, needle):
    return [e for e in conn.executed if needle in e[0]]


@pytest.mark.asyncio
async def test_trash_inactivates_master_dismisses_reviews_and_warns_tenants():
    conn, req = _make(tenants=("tenant-a", "tenant-b"))
    with patch.object(admin, "_recompute_after_status_change", new=AsyncMock()) as rec:
        out = await admin.trash_source_tour("tour-1", req, x_admin_secret="test-secret")

    # 1. Master inactivated
    master = _sql_hits(conn, "published_tours")
    assert len(master) == 1
    assert "master_status = 'inactive'" in master[0][0] and "master_status = 'active'" in master[0][0]

    # 2. Pending review rows dismissed, keyed on tour_id
    rev = _sql_hits(conn, "review_queue")
    assert len(rev) == 1
    assert "'dismissed'" in rev[0][0] and "review_status = 'pending'" in rev[0][0]

    # 3. Notifications: 1 admin SOURCE_TRASHED + 1 tenant TOUR_DISCONTINUED per adopting tenant
    events = [a[1][2] for a in conn.emitted]  # event_type is the 3rd INSERT param
    assert events.count(EventType.SOURCE_TRASHED.value) == 1
    assert events.count(EventType.TOUR_DISCONTINUED.value) == 2  # tenant-a, tenant-b

    # 4. AA-713 recompute runs after commit
    rec.assert_awaited_once()
    assert rec.await_args.args[2] == "trashed"

    assert out["source_status"] == "trashed" and out["master_inactivated"] is True


@pytest.mark.asyncio
async def test_trash_with_no_adopting_tenant_emits_only_admin_event():
    conn, req = _make(tenants=())
    with patch.object(admin, "_recompute_after_status_change", new=AsyncMock()):
        await admin.trash_source_tour("tour-1", req, x_admin_secret="test-secret")
    events = [a[1][2] for a in conn.emitted]
    assert events.count(EventType.SOURCE_TRASHED.value) == 1
    assert EventType.TOUR_DISCONTINUED.value not in events


@pytest.mark.asyncio
async def test_restore_reactivates_master_and_warns_tenants_then_recomputes():
    conn, req = _make(tenants=("tenant-a",))
    with patch.object(admin, "_recompute_after_status_change", new=AsyncMock()) as rec:
        out = await admin.restore_source_tour("tour-1", req, x_admin_secret="test-secret")

    master = _sql_hits(conn, "published_tours")
    assert len(master) == 1
    assert "master_status = 'active'" in master[0][0] and "master_status = 'inactive'" in master[0][0]

    # restore does NOT touch review_queue (dismissal is one-way)
    assert _sql_hits(conn, "review_queue") == []

    events = [a[1][2] for a in conn.emitted]
    assert events.count(EventType.SOURCE_RESTORED.value) == 1
    assert events.count(EventType.TOUR_REINSTATED.value) == 1

    rec.assert_awaited_once()
    assert rec.await_args.args[2] == "inactive"
    assert out["source_status"] == "active" and out["master_reactivated"] is True


def test_tenant_facing_events_target_the_tenant_role():
    from services.notifications import _DEFAULT_ROLES
    assert _DEFAULT_ROLES[EventType.TOUR_DISCONTINUED] == ["tenant"]
    assert _DEFAULT_ROLES[EventType.TOUR_REINSTATED] == ["tenant"]
