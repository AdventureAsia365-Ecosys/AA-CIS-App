"""AA-1042 (S270) — the AI gateway Guide tab's live tables against real Postgres (every CIS migration applied by the
conftest): a route change saved through the route control shows up in GET /admin/ai-gateway/guide with the new chain
and version, with no doc edit; the matrix, register, caps and coverage read the registry; a deployment refused on record
is never the stage's "first". api/services/ai_gateway_guide.py. Every row the tests change goes back as it was.
"""
import asyncio
import json
import os
import sys
from datetime import datetime, timezone

import asyncpg
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"


class _Pool:
    """The bits of an asyncpg pool the services use, over one plain (no SSL) connection per acquire."""

    def acquire(self):
        pool = self

        class _Ctx:
            async def __aenter__(self):
                self.conn = await pool.connect(DSN)
                return self.conn

            async def __aexit__(self, *exc):
                await self.conn.close()
        return _Ctx()

    connect = staticmethod(asyncpg.connect)


@pytest.fixture
def world(monkeypatch):
    """As test_aa1042_route_control_db: readers connect without SSL; every changed row is put back."""
    real = asyncpg.connect

    async def plain(*args, **kwargs):
        kwargs.pop("ssl", None)
        return await real(DSN, **{k: v for k, v in kwargs.items() if k == "timeout"})
    monkeypatch.setattr(asyncpg, "connect", plain)
    monkeypatch.setenv("DATABASE_URL", DSN)
    monkeypatch.delenv("SECRET_DB_ARN", raising=False)
    monkeypatch.delenv("LLM_ROUTE_AWS_FIRST", raising=False)
    from shared.llm_client import catalog, connections, deployments, role_config, route_settings
    mods = (catalog, connections, deployments, role_config, route_settings)
    for mod in mods:
        mod.invalidate()

    async def snapshot():
        conn = await real(DSN)
        try:
            routes = [dict(r) for r in await conn.fetch(
                "SELECT stage, model_id, fallback_model_ids, updated_at, updated_by FROM shared.llm_role_config")]
            deps = [dict(r) for r in await conn.fetch(
                "SELECT model_key, connection_key, priority, access_state, last_probe_at, updated_at, updated_by "
                "FROM shared.llm_model_deployment")]
            mode = await conn.fetchval("SELECT order_mode FROM shared.llm_route_settings")
            return routes, deps, mode
        finally:
            await conn.close()

    async def restore(saved):
        routes, deps, mode = saved
        conn = await real(DSN)
        try:
            async with conn.transaction():
                for r in routes:
                    await conn.execute("UPDATE shared.llm_role_config SET model_id = $2, fallback_model_ids = $3, "
                                       "updated_at = $4, updated_by = $5 WHERE stage = $1", r["stage"], r["model_id"],
                                       r["fallback_model_ids"], r["updated_at"], r["updated_by"])
                for d in deps:
                    await conn.execute("UPDATE shared.llm_model_deployment SET priority = $3, access_state = $4, "
                                       "last_probe_at = $5, updated_at = $6, updated_by = $7 "
                                       "WHERE model_key = $1 AND connection_key = $2", d["model_key"],
                                       d["connection_key"], d["priority"], d["access_state"], d["last_probe_at"],
                                       d["updated_at"], d["updated_by"])
                await conn.execute("UPDATE shared.llm_route_settings SET order_mode = $1", mode)
        finally:
            await conn.close()

    saved = asyncio.run(snapshot())
    try:
        yield _Pool()
    finally:
        asyncio.run(restore(saved))
        for mod in mods:
            mod.invalidate()


def _live(pool):
    from api.services import ai_gateway_guide as guide
    for name in ("catalog", "connections", "deployments", "role_config", "route_settings"):
        __import__(f"shared.llm_client.{name}", fromlist=["invalidate"]).invalidate()
    return asyncio.run(guide.read_live(pool))


def test_live_tables_read_the_registry(world):
    live = _live(world)
    assert live["errors"] == [], live["errors"]
    routes = live["routes"]
    assert routes["version"] >= 1 and routes["order_mode"] in ("model_first", "aws_first")
    judge = next(s for s in routes["stages"] if s["stage"] == "s1_judge")
    assert judge["chain"] and judge["chain"][0]
    keys = {c["connection_key"] for c in live["register"]}
    assert {"acc3", "anthropic-direct", "openai-direct"} <= keys
    assert {c["connection_key"] for c in live["matrix"]["cells"]} <= keys
    assert all("account_id" not in r and "base_url" not in r and "role_name" not in r for r in live["register"])
    assert all("account_id" not in r for r in live["caps"])
    assert live["coverage"]["ready"] is True and isinstance(live["coverage"]["disabled"], list)
    json.dumps(live, default=str)


def test_a_saved_route_change_shows_in_the_guide_with_its_version(world):
    """Done-when (S267): editing a route in the admin changes the Guide tables with no doc edit."""
    from api.services import llm_route_control as rc
    before = _live(world)["routes"]
    state = asyncio.run(rc.read_state(world))
    live = asyncio.run(rc.preview(world))
    broken = {w.get("stage") for w in live["warnings"]}
    change = None
    for s in state["stages"]:
        if (s.get("is_active") and s["role"] not in ("writer", "judge") and len(s["chain"] or []) >= 3
                and s["stage"] not in broken):
            kw = {"routes": {s["stage"]: s["chain"][:-1]}}
            out = asyncio.run(rc.preview(world, **kw))
            if out["valid"] and not out["unchanged"] and not out["needs_evidence"]:
                change = kw
                break
    if change is None:
        pytest.fail("no valid evidence-free chain change in the migrated test DB")
    (stage, chain), = change["routes"].items()
    saved = asyncio.run(rc.apply(world, actor="admin:zz-aa1042-guide", action="save",
                                 expected_version=state["version"], note="invented guide test change", **change))
    after = _live(world)["routes"]
    assert after["version"] == saved["version"] > before["version"]
    row = next(s for s in after["stages"] if s["stage"] == stage)
    assert row["chain"] == chain
    assert next(s for s in before["stages"] if s["stage"] == stage)["chain"] != chain
    # put it back through the route control too (a revert is a new version)
    asyncio.run(rc.apply(world, actor="admin:zz-aa1042-guide", action="revert", expected_version=saved["version"],
                         revert_to=saved["version"] - 1))


def test_a_deployment_refused_on_record_is_not_first_in_the_guide(world):
    before = _live(world)["routes"]
    stage = next(s for s in before["stages"]
                 if s["first"] and "@" in s["first"] and "legacy" not in s["first"] and s["is_active"])
    model, conn_key = stage["first_model"], stage["first_connection"]

    async def no_answer_on_record():
        conn = await world.connect(DSN)
        try:
            return await conn.fetchval("SELECT last_ok_at FROM shared.llm_provider_health "
                                       "WHERE connection_key = $1 AND model_key = $2", conn_key, model)
        finally:
            await conn.close()

    async def refuse():
        conn = await world.connect(DSN)
        try:
            await conn.execute("UPDATE shared.llm_model_deployment SET access_state = 'access_denied', "
                               "last_probe_at = $3 WHERE model_key = $1 AND connection_key = $2",
                               model, conn_key, datetime.now(timezone.utc))
        finally:
            await conn.close()
    assert asyncio.run(no_answer_on_record()) is None
    asyncio.run(refuse())
    after = _live(world)
    row = next(s for s in after["routes"]["stages"] if s["stage"] == stage["stage"])
    assert row["first"] != stage["first"]
    assert {"model": model, "connection": conn_key, "reason": "refused (access_denied)"} in row["skipped"]
    cell = next(c for c in after["matrix"]["cells"] if c["model_key"] == model and c["connection_key"] == conn_key)
    assert cell["access_state"] == "access_denied" and cell["last_probe_at"] is not None
