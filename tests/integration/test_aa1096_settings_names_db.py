"""AA-1096 l23 — the settings registry list and history name the staff member behind a change (shared.admin_users:
display name, else e-mail, else username) next to the actor id the rows keep (admin:<uuid>, CIS 407). Needs CIS 407;
skipped before it. Invented staff rows only; one transaction, rolled back."""
import os
import sys
import uuid

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from api.services import settings_registry as reg  # noqa: E402
from shared import ops_alerts, settings  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
KEY = "cis.retention.job_succeeded_days"        # low impact, 7..365, default 30
REASON = "zz test: keep finished jobs a little longer"


class _OneConnPool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *a):
                return False
        return _Ctx()


@pytest_asyncio.fixture
async def pool(monkeypatch):
    c = await asyncpg.connect(DSN)
    if await c.fetchval("SELECT to_regclass('shared.setting_value') IS NULL AS ok"):
        await c.close()
        pytest.skip("CIS 407 is not on this branch")
    tr = c.transaction()
    await tr.start()

    async def no_mail(conn, *, details):
        return None
    monkeypatch.setattr(ops_alerts, "enqueue_setting_self_approved_email", no_mail)
    settings.READER.install(None)
    settings.READER.invalidate()
    try:
        yield _OneConnPool(c)
    finally:
        await tr.rollback()
        await c.close()
        settings.READER._snapshot = None
        settings.READER.invalidate()


async def _staff(conn, username, display_name=None):
    staff_id = str(uuid.uuid4())
    await conn.execute(
        "INSERT INTO shared.admin_users (id, username, role, is_active, display_name, is_owner, created_at, "
        "updated_at) VALUES ($1::uuid, $2, 'admin', true, $3, false, now(), now())", staff_id, username, display_name)
    return staff_id


@pytest.mark.asyncio
async def test_list_and_history_name_the_staff_member(pool):
    a = await _staff(pool.conn, "zz-maker", "Zz Maker")
    b = await _staff(pool.conn, "zz-checker")                 # no display name, no e-mail: the username
    p = await reg.propose(pool, KEY, value=40, reason=REASON, actor=f"admin:{a}", base_version=0)
    await reg.decide(pool, p["id"], actor=f"admin:{b}", approve=True)
    listing = await reg.list_settings(pool, app="cis")
    row = next(r for r in listing["settings"] if r["key"] == KEY)
    last = row["last_change"]
    assert (last["proposed_by"], last["proposed_by_name"]) == (f"admin:{a}", "Zz Maker")
    assert (last["decided_by"], last["decided_by_name"]) == (f"admin:{b}", "zz-checker")
    assert row["guide"] == settings.guide(KEY)
    h = await reg.history(pool, KEY)
    assert [(r["proposed_by_name"], r["decided_by_name"]) for r in h["history"]] == [("Zz Maker", "zz-checker")]


@pytest.mark.asyncio
async def test_an_actor_without_a_staff_row_keeps_its_id(pool):
    p = await reg.propose(pool, KEY, value=41, reason=REASON, actor="admin:zz-script-s1", base_version=0)
    listing = await reg.list_settings(pool, app="cis")
    row = next(r for r in listing["settings"] if r["key"] == KEY)
    assert row["proposals"][0]["id"] == p["id"] and row["proposals"][0]["proposed_by_name"] is None
