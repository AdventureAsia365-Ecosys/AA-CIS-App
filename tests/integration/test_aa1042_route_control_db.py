"""AA-1042 (S268) — the admin route control against real Postgres (CIS migration 370 applied by conftest): the live
state read, a save as a version with its diff and audit row, a stale expected version (conflict), a revert, the
"Prefer Anthropic API" preset, the rule check of a chain, a revert of a chain and a provider-order change restoring the
exact routes and priorities, and the Routing tab's PATCH writing a version (with an external change recorded first).
api/services/llm_route_control.py, shared/llm_client/route_versions.py. Every row the tests change (shadow settings
included) goes back as it was at the end (the shared DB is read by later modules).
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
                "SELECT stage, model_id, fallback_model_ids, shadow_model_id, shadow_sample_pct, updated_at, "
                "updated_by FROM shared.llm_role_config")]
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
                                       "shadow_model_id = $4, shadow_sample_pct = $5, updated_at = $6, "
                                       "updated_by = $7 WHERE stage = $1", r["stage"], r["model_id"],
                                       r["fallback_model_ids"], r["shadow_model_id"], r["shadow_sample_pct"],
                                       r["updated_at"], r["updated_by"])
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
    # evidence exactly for the writer / judge stages the preset moves to another model@connection
    moved_stages = sorted(s["stage"] for s in preview["stages"]
                          if s["first_before"] != s["first"] and s["role"] in ("writer", "judge"))
    assert preview["evidence_stages"] == moved_stages and preview["needs_evidence"] is bool(moved_stages)
    if moved_stages:
        with pytest.raises(rc.RouteControlRefused, match="needs evidence"):
            asyncio.run(rc.apply(world, actor="admin:zz-aa1042", action="preset", expected_version=_version(world),
                                 preset="prefer_anthropic_api"))
    out = asyncio.run(rc.apply(world, actor="admin:zz-aa1042", action="preset", expected_version=_version(world),
                               preset="prefer_anthropic_api", eval_run_id=None,
                               break_glass_reason=_BREAK_GLASS if moved_stages else None))
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


_BREAK_GLASS = "invented integration test change, reverted by the fixture at the end"


async def _routes_and_priorities():
    conn = await asyncpg.connect(DSN)
    try:
        routes = {r["stage"]: (r["model_id"], list(r["fallback_model_ids"] or [])) for r in await conn.fetch(
            "SELECT stage, model_id, fallback_model_ids FROM shared.llm_role_config")}
        prios = {(r["model_key"], r["connection_key"]): r["priority"] for r in await conn.fetch(
            "SELECT model_key, connection_key, priority FROM shared.llm_model_deployment")}
        return routes, prios
    finally:
        await conn.close()


def _quiet_proposal(world, rc, candidates):
    """The first candidate proposal whose preview is valid and needs no eval evidence (no bell row left behind), on a
    stage that breaks no rule today: the revert puts its chain back, and a rule the live chain already breaks (e.g. the
    attempt cap after an earlier module's replay) would be NEW for the revert, which is then refused (by design)."""
    live = asyncio.run(rc.preview(world))                 # no change: the warnings are the rules broken today
    broken = {w.get("stage") for w in live["warnings"]}
    for kw in candidates:
        if broken & set(kw.get("routes") or ()):
            continue
        out = asyncio.run(rc.preview(world, **kw))
        if out["valid"] and not out["unchanged"] and not out["needs_evidence"]:
            return kw
    pytest.fail("no valid evidence-free candidate in the migrated test DB")


def test_revert_restores_the_exact_chains_and_provider_order(world):
    from api.services import llm_route_control as rc
    state = asyncio.run(rc.read_state(world))
    before_routes, before_prios = asyncio.run(_routes_and_priorities())
    v_before = _version(world)

    chain_kw = _quiet_proposal(world, rc, [
        {"routes": {s["stage"]: s["chain"][:-1]}} for s in state["stages"]
        if s.get("is_active") and s["role"] not in ("writer", "judge") and len(s["chain"] or []) >= 3])
    order_kw = _quiet_proposal(world, rc, [
        {"provider_order": {p["model_key"]: [d["connection_key"] for d in p["deployments"]][::-1]}}
        for p in state["provider_order"] if len(p["deployments"]) >= 2])
    (stage, chain), = chain_kw["routes"].items()
    (model, order), = order_kw["provider_order"].items()

    v1 = asyncio.run(rc.apply(world, actor="admin:zz-aa1042", action="save", expected_version=v_before,
                              note="invented chain change", **chain_kw))["version"]
    v2 = asyncio.run(rc.apply(world, actor="admin:zz-aa1042", action="save", expected_version=v1,
                              note="invented provider order change", **order_kw))["version"]
    mid_routes, mid_prios = asyncio.run(_routes_and_priorities())
    assert mid_routes[stage] == (chain[0], chain[1:]) != before_routes[stage]
    assert [c for (m, c), _ in sorted(((k, p) for k, p in mid_prios.items() if k[0] == model),
                                      key=lambda kp: (kp[1], kp[0][1]))] == order
    assert {k: v for k, v in mid_routes.items() if k != stage} == {k: v for k, v in before_routes.items()
                                                                   if k != stage}

    # the version right before the first save: v_before, or the "external" version begin() recorded first when an
    # earlier test's fixture put rows back by hand (its snapshot is the live rows this test started from)
    v_pre = v1 - 1
    assert v_pre >= v_before
    back = asyncio.run(rc.apply(world, actor="admin:zz-aa1042", action="revert", expected_version=v2,
                                revert_to=v_pre))
    assert back["action"] == "revert" and back["reverted_from"] == v_pre and back["version"] == v2 + 1
    assert {c["kind"] for c in back["diff"]} == {"route", "provider_order"}
    after_routes, after_prios = asyncio.run(_routes_and_priorities())
    assert after_routes == before_routes                       # every stage, primary and fallbacks
    assert after_prios == before_prios                         # every deployment's exact priority number
    snap_before = asyncio.run(rc.read_version(world, v_pre))["snapshot"]
    snap_after = asyncio.run(rc.read_version(world, back["version"]))["snapshot"]
    assert snap_after["routes"] == snap_before["routes"] and snap_after["priorities"] == snap_before["priorities"]


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


def test_a_deployment_refused_on_record_is_not_named_first(world):
    """AA-1042 (S268 follow-up): the probe state of CIS 367 (access_denied, newer than any answer) moves the preview's
    "first" to the deployment that would serve, and the refused one is listed in the stage's "skipped"."""
    from datetime import datetime, timezone

    from api.services import llm_route_control as rc
    before = asyncio.run(rc.preview(world))
    stage = next(s for s in before["stages"] if s["first"] and "@" in s["first"] and "legacy" not in s["first"])
    model, conn_key = stage["first"].split("@", 1)

    async def mark(state, probed):
        conn = await world.connect(DSN)
        try:
            return await conn.fetchrow(
                "UPDATE shared.llm_model_deployment d SET access_state = $3, last_probe_at = $4 "
                "FROM (SELECT access_state, last_probe_at FROM shared.llm_model_deployment "
                "      WHERE model_key = $1 AND connection_key = $2) old "
                "WHERE d.model_key = $1 AND d.connection_key = $2 "
                "RETURNING old.access_state AS was_state, old.last_probe_at AS was_probed",
                model, conn_key, state, probed)
        finally:
            await conn.close()

    async def no_answer_on_record():
        conn = await world.connect(DSN)
        try:
            return await conn.fetchval("SELECT last_ok_at FROM shared.llm_provider_health "
                                       "WHERE connection_key = $1 AND model_key = $2", conn_key, model)
        finally:
            await conn.close()
    assert asyncio.run(no_answer_on_record()) is None
    old = asyncio.run(mark("access_denied", datetime.now(timezone.utc)))
    try:
        after = asyncio.run(rc.preview(world))
        moved = next(s for s in after["stages"] if s["stage"] == stage["stage"])
        assert moved["first"] != stage["first"]
        assert {"model": model, "connection": conn_key, "reason": "refused (access_denied)"} in moved["skipped"]
        assert moved["first_before"] == moved["first"]               # now and the proposal read the same refusals
        assert after["unchanged"] is True and after["evidence_stages"] == []
    finally:
        asyncio.run(mark(old["was_state"], old["was_probed"]))
