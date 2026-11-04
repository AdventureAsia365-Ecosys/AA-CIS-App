"""AA-1026 (S267) — on real Postgres (one transaction, rolled back): the stage coverage report reads the live registry
tables with CIS 367's probe state, and the probe job writes that state as cis_app (its column UPDATE grant).

Invented keys only (zz1026-*); the window sits in 2099 so rows of other tests never count.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

from services.jobs import llm_probe_job as probe  # noqa: E402
from shared.llm_client import stage_coverage as sc  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
NOW = datetime(2099, 1, 1, 12, 0, tzinfo=timezone.utc)


@pytest_asyncio.fixture
async def conn():
    c = await asyncpg.connect(DSN)
    tr = c.transaction()
    await tr.start()
    try:
        yield c
    finally:
        await tr.rollback()
        await c.close()


async def _model(conn, key: str, connection: str, *, state: str = "unknown", probed=None):
    await conn.execute(
        "INSERT INTO shared.llm_model_catalog (model_key, label, vendor, provider, api_style, bedrock_profile_ids, "
        "callable_via, supports_temperature, price_in_per_mtok, price_out_per_mtok, enabled, updated_by) "
        "VALUES ($1, $1, 'openai', 'bedrock', 'converse', jsonb_build_object($2::text, 'wire-' || $1), "
        "ARRAY['llm_client'], false, 0.1, 0.5, true, 'test')", key, connection)
    await conn.execute(
        "INSERT INTO shared.llm_model_deployment (model_key, connection_key, wire_id, priority, enabled, origin, "
        "updated_by, access_state, last_probe_at) VALUES ($1, $2, 'wire-' || $1, 10, true, 'seed', 'test', $3, $4)",
        key, connection, state, probed)


async def _stage(conn, stage: str, model: str, fallbacks=()):
    await conn.execute(
        "INSERT INTO shared.llm_role_config (stage, role, provider, model_id, is_active, updated_at, updated_by, "
        "fallback_model_ids, shadow_sample_pct) VALUES ($1, 'validate', 'openai', $2, true, now(), 'test', $3, 0)",
        stage, model, list(fallbacks))


@pytest.mark.asyncio
async def test_read_reports_live_and_uncovered_stages_from_real_rows(conn):
    await conn.execute("UPDATE shared.llm_provider_connection SET access_state = 'ok' WHERE connection_key = 'acc2'")
    await _model(conn, "zz1026-live", "acc2", state="ok", probed=NOW - timedelta(hours=2))
    await _model(conn, "zz1026-refused", "acc2", state="access_denied", probed=NOW - timedelta(hours=1))
    await _model(conn, "zz1026-never", "acc2")
    await _stage(conn, "zz1026_covered", "zz1026-refused", ["zz1026-live"])
    await _stage(conn, "zz1026_uncovered", "zz1026-refused", ["zz1026-never"])
    rep = await sc.read(conn, now=NOW)
    assert rep["ready"] is True
    by = {s["stage"]: s for s in rep["stages"]}
    covered, uncovered = by["zz1026_covered"], by["zz1026_uncovered"]
    assert (covered["status"], covered["live_model"], covered["live_connection"]) == ("live", "zz1026-live", "acc2")
    assert uncovered["status"] == "none" and "zz1026_uncovered" in rep["uncovered"]
    assert [m["state"] for m in uncovered["models"]] == ["refused", "unproven"]


@pytest.mark.asyncio
async def test_the_probe_writes_the_deployment_state_as_cis_app(conn):
    if await conn.fetchval("SELECT count(*) FROM pg_roles WHERE rolname = 'cis_app'") == 0:
        pytest.skip("no cis_app role in this database")
    await _model(conn, "zz1026-probed", "acc2")
    await _model(conn, "zz1026-denied", "acc2")
    await conn.execute("GRANT USAGE ON SCHEMA shared TO cis_app")       # other modules' teardowns may revoke it
    await conn.execute("GRANT SELECT ON shared.llm_model_deployment TO cis_app")
    await conn.execute("SET LOCAL ROLE cis_app")

    class _Acquire:
        async def __aenter__(self):
            return conn

        async def __aexit__(self, *exc):
            return False

    class _Pool:
        def acquire(self):
            return _Acquire()

    def result(model: str, ok: bool, cls=None, error=None) -> dict:
        return dict(model_key=model, connection_key="acc2", ok=ok, error_class=cls, probe="text", error=error)

    written = await probe.record_deployment_states(_Pool(), [
        result("zz1026-probed", True),
        result("zz1026-denied", False, "access_denied", "AccessDeniedException: not available for this account")])
    assert written == 2
    await conn.execute("RESET ROLE")
    rows = {r["model_key"]: r for r in await conn.fetch(
        "SELECT model_key, access_state AS state, last_probe_at IS NOT NULL AS stamped, last_probe_error AS err "
        "FROM shared.llm_model_deployment WHERE model_key LIKE 'zz1026-%' AND connection_key = 'acc2'")}
    assert (rows["zz1026-probed"]["state"], rows["zz1026-probed"]["stamped"], rows["zz1026-probed"]["err"]) == (
        "ok", True, None)
    assert rows["zz1026-denied"]["state"] == "access_denied" and "not available" in rows["zz1026-denied"]["err"]
