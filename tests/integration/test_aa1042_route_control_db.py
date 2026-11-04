"""AA-1042 (S268) — the admin route control against real Postgres (CIS migration 370 applied by conftest): the live
state read, a save as a version with its diff and audit row, a stale expected version (conflict), a revert, the
"Prefer Anthropic API" preset, the rule check of a chain, and the Routing tab's PATCH writing a version (with an
external change recorded first). api/services/llm_route_control.py, shared/llm_client/route_versions.py. Every row the
tests change goes back as it was at the end (the shared DB is read by later modules).
"""
import asyncio
import json
import os
import sys

import asyncpg
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"


# ── the route control (service + route_versions) ─────────────────────────────────────────────────────────────────

class _Pool:
    """The bits of an asyncpg pool the service uses, over one plain (no SSL) connection per acquire."""

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
    """The gateway's readers connect with ssl=require; the local test DB has no SSL: connect without it. Every row
    the tests change goes back as it was, updated_by / updated_at included (other modules select rows by them)."""
    real = asyncpg.connect

    async def plain(*args, **kwargs):
        kwargs.pop("ssl", None)
        return await real(DSN, **{k: v for k, v in kwargs.items() if k == "timeout"})
    monkeypatch.setattr(asyncpg, "connect", plain)
    monkeypatch.setenv("DATABASE_URL", DSN)
    monkeypatch.delenv("SECRET_DB_ARN", raising=False)
    monkeypatch.delenv("LLM_ROUTE_AWS_FIRST", raising=False)
    from shared.llm_client import catalog, connections, deployments, role_config, route_settings
    for mod in (catalog, connections, deployments, role_config, route_settings):
        mod.invalidate()
    pool = _Pool()

    async def snapshot():
        conn = await real(DSN)
        try:
            routes = [dict(r) for r in await conn.fetch(
                "SELECT stage, model_id, fallback_model_ids, updated_at, updated_by FROM shared.llm_role_config")]
            prios = [dict(r) for r in await conn.fetch(
                "SELECT model_key, connection_key, priority, updated_at, updated_by FROM shared.llm_model_deployment")]
            mode = await conn.fetchval("SELECT order_mode FROM shared.llm_route_settings")
            return routes, prios, mode
        finally:
            await conn.close()

    async def restore(saved):
        routes, prios, mode = saved
        conn = await real(DSN)
        try:
            async with conn.transaction():
                for r in routes:
                    await conn.execute("UPDATE shared.llm_role_config SET model_id = $2, fallback_model_ids = $3, "
                                       "updated_at = $4, updated_by = $5 WHERE stage = $1", r["stage"], r["model_id"],
                                       r["fallback_model_ids"], r["updated_at"], r["updated_by"])
                for p in prios:
                    await conn.execute("UPDATE shared.llm_model_deployment SET priority = $3, updated_at = $4, "
                                       "updated_by = $5 WHERE model_key = $1 AND connection_key = $2", p["model_key"],
                                       p["connection_key"], p["priority"], p["updated_at"], p["updated_by"])
                await conn.execute("UPDATE shared.llm_route_settings SET order_mode = $1", mode)
        finally:
            await conn.close()

    saved = asyncio.run(snapshot())
    try:
        yield pool
    finally:
        asyncio.run(restore(saved))
        for mod in (catalog, connections, deployments, role_config, route_settings):
            mod.invalidate()


def _version(pool) -> int:
    async def read():
        conn = await pool.connect(DSN)
        try:
            return await conn.fetchval("SELECT current_version FROM shared.llm_route_settings")
        finally:
            await conn.close()
    return asyncio.run(read())


def test_state_lists_stages_options_and_provider_order(world):
    from api.services import llm_route_control as rc
    state = asyncio.run(rc.read_state(world))
    assert state["ready"] is True and state["order_mode"] in ("model_first", "aws_first")
    assert state["version"] == _version(world)
    stage = next(s for s in state["stages"] if s["stage"] == "s1_judge")
    assert stage["chain"][0] and any(o["available"] for o in stage["options"])
    sonnet = next(p for p in state["provider_order"] if p["model_key"] == "sonnet-5-5")
    assert [d["connection_key"] for d in sonnet["deployments"]][0] == "acc3"
    assert {p["key"] for p in state["presets"]} == {"prefer_anthropic_api", "prefer_aws"}


def test_save_order_mode_is_a_version_with_a_diff_and_an_audit_row(world):
    from api.services import llm_route_control as rc
    from shared.llm_client import route_settings
    before = _version(world)
    out = asyncio.run(rc.apply(world, actor="admin:zz-aa1042", action="save", expected_version=before,
                               note="invented test change", order_mode="aws_first"))
    assert out["version"] == _version(world) > before
    assert out["diff"] == [{"kind": "order_mode", "before": "model_first", "after": "aws_first"}]
    route_settings.invalidate()
    assert route_settings.order_mode_sync() == "aws_first" and route_settings.order_mode_source() == "db"
    assert route_settings.current_version_sync() == out["version"]

    async def audit():
        conn = await world.connect(DSN)
        try:
            return await conn.fetchrow("SELECT actor, details FROM acp_shared.audit_log WHERE action = "
                                       "'llm_route_config_saved' AND resource_id = $1", str(out["version"]))
        finally:
            await conn.close()
    row = asyncio.run(audit())
    assert row["actor"] == "admin:zz-aa1042" and json.loads(row["details"])["diff"] == out["diff"]

    # a stale expected_version is a conflict; nothing is written
    with pytest.raises(rc.RouteControlConflict):
        asyncio.run(rc.apply(world, actor="admin:zz-aa1042", action="save", expected_version=before,
                             order_mode="model_first"))
    # revert to the version before: a new version that names it
    back = asyncio.run(rc.apply(world, actor="admin:zz-aa1042", action="revert", expected_version=out["version"],
                                revert_to=before))
    assert back["action"] == "revert" and back["reverted_from"] == before
    assert back["diff"] == [{"kind": "order_mode", "before": "aws_first", "after": "model_first"}]


def test_preset_puts_the_anthropic_api_first_for_claude_models(world):
    from api.services import llm_route_control as rc
    preview = asyncio.run(rc.preview(world, preset="prefer_anthropic_api"))
    moved = {c["model"] for c in preview["diff"] if c["kind"] == "provider_order"}
    assert "sonnet-5-5" in moved and not any(c["kind"] == "route" for c in preview["diff"])
    assert preview["valid"], preview["errors"]
    out = asyncio.run(rc.apply(world, actor="admin:zz-aa1042", action="preset", expected_version=_version(world),
                               preset="prefer_anthropic_api"))
    assert out["action"] == "preset" and out["preset"] == "prefer_anthropic_api"
    state = asyncio.run(rc.read_state(world))
    sonnet = next(p for p in state["provider_order"] if p["model_key"] == "sonnet-5-5")
    assert sonnet["deployments"][0]["connection_key"] == "anthropic-direct"
    # the same preset again changes nothing
    with pytest.raises(rc.RouteControlRefused, match="nothing to change"):
        asyncio.run(rc.apply(world, actor="admin:zz-aa1042", action="preset", expected_version=_version(world),
                             preset="prefer_anthropic_api"))


def test_chain_save_is_validated_and_a_judge_of_the_writers_family_is_refused(world):
    from api.services import llm_route_control as rc
    state = asyncio.run(rc.read_state(world))
    judge = next(s for s in state["stages"] if s["stage"] == "s1_judge")
    bad = asyncio.run(rc.preview(world, routes={"s1_judge": [judge["chain"][0], "sonnet-4-6"]}))
    assert not bad["valid"] and bad["needs_evidence"] and bad["evidence_stages"] == ["s1_judge"]
    assert any(e["stage"] == "s1_judge" and e["rule"] in ("not_selectable", "judge_writer_family")
               for e in bad["errors"])
    with pytest.raises(rc.RouteControlRefused):
        asyncio.run(rc.apply(world, actor="admin:zz-aa1042", action="save", expected_version=_version(world),
                             routes={"s1_judge": [judge["chain"][0], "sonnet-4-6"]}))


def test_a_routing_tab_patch_writes_a_version_and_a_migration_change_is_external(world):
    from shared.llm_client import role_config

    async def go():
        conn = await world.connect(DSN)
        try:
            # a change the app did not make (a migration)
            row = await conn.fetchrow("SELECT stage, fallback_model_ids FROM shared.llm_role_config "
                                      "WHERE stage = 'a3_place_days'")
            await conn.execute("UPDATE shared.llm_role_config SET fallback_model_ids = $2 WHERE stage = $1",
                               row["stage"], list(row["fallback_model_ids"])[::-1])
            v0 = await conn.fetchval("SELECT current_version FROM shared.llm_route_settings")
        finally:
            await conn.close()
        await role_config.set_stage_route("a3_place_days", fallback_model_ids=list(row["fallback_model_ids"]),
                                          shadow_model_id=None, shadow_sample_pct=0, updated_by="admin:zz-aa1042",
                                          audit_actor="admin:zz-aa1042")
        conn = await world.connect(DSN)
        try:
            return v0, [dict(r) for r in await conn.fetch(
                "SELECT version, action, created_by FROM shared.llm_route_config_version WHERE version > $1 "
                "ORDER BY version", v0)]
        finally:
            await conn.close()
    v0, rows = asyncio.run(go())
    assert [(r["action"], r["created_by"]) for r in rows] == [("external", "external"), ("save", "admin:zz-aa1042")]
    assert rows[-1]["version"] == v0 + 2
