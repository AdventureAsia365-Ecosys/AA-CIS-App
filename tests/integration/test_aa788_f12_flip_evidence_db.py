"""AA-788 (S287) — the F12_scope flip on Platform › Settings against real Postgres: `block` for
cis.quality_modes.t10_f12_scope is refused (422 evidence_mismatch, nothing stored) unless its evidence is a passing
pilot measurement record's reference (scripts/aa788/pilot.py score); with one it is stored and, once approved, the
gate reads `block` at gate time; going back to `warn` takes any evidence. Every test runs in one transaction that is
rolled back. Invented values only."""
import os
import sys

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from api.services import settings_registry as reg  # noqa: E402
from services.acp_content_writing import quality_gates as qg  # noqa: E402
from services.quality import quality_settings, settings_keys  # noqa: E402
from shared import ops_alerts, settings  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
KEY = settings_keys.MODES["t10_f12_scope"]
REASON = "zz test: the pilot measurement record passed the bar"
A = "admin:zz-admin-a"
GOOD = f"{quality_settings.F12_PILOT_EVIDENCE_PREFIX}p202611220101abcd:0123456789ab"


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


async def _stored(pool) -> int:
    return await pool.conn.fetchval("SELECT count(*) AS n FROM shared.setting_value WHERE key = $1", KEY)


@pytest.mark.parametrize("evidence", ["replay run 42 showed 5 new holds",
                                      f"{quality_settings.F12_PILOT_EVIDENCE_PREFIX}x"])
@pytest.mark.asyncio
async def test_block_without_a_pilot_record_is_refused_before_it_is_stored(pool, evidence):
    with pytest.raises(reg.SettingsError) as err:
        await reg.propose(pool, KEY, value="block", reason=REASON, actor=A, base_version=0, evidence_ref=evidence,
                          apply=True, can_approve=True)
    assert err.value.status == 422 and err.value.code == "evidence_mismatch"
    assert await _stored(pool) == 0


@pytest.mark.asyncio
async def test_block_with_the_pilot_record_is_what_the_gate_reads(pool):
    out = await reg.propose(pool, KEY, value="block", reason=REASON, actor=A, base_version=0, evidence_ref=GOOD,
                            apply=True, can_approve=True)
    assert out["status"] == "approved" and out["evidence_ref"] == GOOD
    assert await settings.READER.refresh(force=True, pool=pool) is True
    assert qg.f12_scope_mode() == qg.BLOCK_MODE
    back = await reg.propose(pool, KEY, value="warn", reason=REASON, actor=A, base_version=out["version"],
                             evidence_ref="rollback: holds on light pieces", apply=True, can_approve=True)
    assert back["status"] == "approved"
    assert await settings.READER.refresh(force=True, pool=pool) is True
    assert qg.f12_scope_mode() == qg.WARN_MODE
