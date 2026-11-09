"""AA-642 A1 — the stage kill switch against a real Postgres (conftest applies every migration, 326 included).

- ``role_config.set_stage_enabled`` switches a stage off with a reason (audited) and on again (reason cleared); the
  stage read sees it (``enabled=False``, the stored route), the job queue's switch read lists it, the failure-domain
  report lists it as disabled and carries the declared degrade modes of 326.
- The gateway's 0 USD refusal row (status refused, error_class disabled) passes the ledger CHECK of 326.
The store and the ledger connect to the test database without TLS, like test_aa804_ledger_v2_db. A seeded stage is
switched and always switched back on; the ledger row uses an invented stage name.
"""
import os
import sys
import uuid

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from shared.jobs import queue  # noqa: E402
from shared.llm_client import call_log, invariant, role_config  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
STAGE = "t9_write"
LEDGER_STAGE = "zz_aa642_switch"
ACTOR = f"admin:zz-aa642-{uuid.uuid4().hex[:8]}"      # audit_log is append-only: one actor per run


@pytest_asyncio.fixture
async def pool(monkeypatch):
    real_connect = asyncpg.connect

    async def connect(*a, **k):                 # CI Postgres has no SSL; the code asks for it (as test_aa804)
        k.pop("ssl", None)
        return await real_connect(DSN, **{key: v for key, v in k.items() if key == "timeout"})
    monkeypatch.setattr(asyncpg, "connect", connect)
    monkeypatch.setattr(role_config, "get_database_url", lambda: DSN)
    monkeypatch.setattr(call_log, "get_database_url", lambda: DSN)
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
    yield p
    await p.execute("UPDATE shared.llm_role_config SET is_active = true, disabled_reason = NULL WHERE stage = $1",
                    STAGE)
    await p.execute("DELETE FROM shared.llm_call_log WHERE stage = $1", LEDGER_STAGE)
    await p.execute("DELETE FROM shared.llm_call_daily WHERE stage = $1", LEDGER_STAGE)
    role_config.invalidate()
    await p.close()


@pytest.mark.asyncio
async def test_switch_off_and_on_through_every_reader(pool):
    out = await role_config.set_stage_enabled(STAGE, enabled=False, reason="  zz drill ", updated_by=ACTOR,
                                              audit_actor=ACTOR)
    assert (out["is_active"], out["disabled_reason"], out["degrade_mode"]) == (False, "zz drill", "queue_retry")

    cfg = await role_config._fetch_one(STAGE)
    assert (cfg.enabled, cfg.disabled_reason, cfg.degrade_mode) == (False, "zz drill", "queue_retry")
    assert cfg.model_id                                            # the stored route, not the built-in one
    assert STAGE in await queue.disabled_stages(pool)
    async with pool.acquire() as conn:
        report = await invariant.read(conn, blocked={})
        audit = await conn.fetchval("SELECT count(*) AS ok FROM acp_shared.audit_log WHERE actor = $1 "
                                    "AND action = 'llm_stage_switched_off' AND resource_id = $2", ACTOR, STAGE)
    assert STAGE in report["disabled"] and all(s["stage"] != STAGE for s in report["stages"])
    modes = {s["stage"]: s["degrade_mode"] for s in report["stages"]}
    assert modes.get("t2_generate") == "queue_retry" and modes.get("tp_compose") == "basic_narration"
    assert audit == 1

    out = await role_config.set_stage_enabled(STAGE, enabled=True, reason="ignored", updated_by=ACTOR,
                                              audit_actor=ACTOR)
    assert (out["is_active"], out["disabled_reason"]) == (True, None)
    assert STAGE not in await queue.disabled_stages(pool)


@pytest.mark.asyncio
async def test_the_refusal_row_passes_the_ledger_check(pool):
    row = call_log.gateway_failure_row(stage=LEDGER_STAGE, role="writer", model="zz-model", status="refused",
                                       error_class="disabled", error="LLMStageDisabled: stage switched off",
                                       latency_ms=0)
    await call_log.record_calls([row])
    got = await pool.fetchrow("SELECT status, error_class, cost_usd::float8 AS cost FROM shared.llm_call_log "
                              "WHERE stage = $1", LEDGER_STAGE)
    assert got is not None, "the row was not written (see the llm_call_log_write_failed warning)"
    assert (got["status"], got["error_class"], got["cost"]) == ("refused", "disabled", 0.0)
