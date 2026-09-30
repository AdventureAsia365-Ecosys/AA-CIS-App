"""AA-702 — admin UI audit fixes (S207): notifications list, active-only S1 sources, one tenant key
for internal spend, dashboard model usage from llm_call_log, tenant LLM cost, NULL-safe job dedup.

Endpoints are called directly with a fake pool; verify_admin_secret is patched to a no-op.
"""
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from api.routers import admin, admin_llm_ops, admin_pipeline, jobs_repo

AA_INTERNAL = "00000000-0000-0000-0000-000000000001"


class _Conn:
    """Records every SQL string; answers fetch/fetchval/fetchrow from a list of (needle, value)."""

    def __init__(self, answers=()):
        self.sql = []
        self.args = []
        self.answers = list(answers)

    def _answer(self, sql, default):
        self.sql.append(sql)
        for needle, value in self.answers:
            if needle in sql:
                return value
        return default

    async def fetch(self, sql, *args):
        self.args.append(args)
        return self._answer(sql, [])

    async def fetchval(self, sql, *args):
        self.args.append(args)
        return self._answer(sql, 0)

    async def fetchrow(self, sql, *args):
        self.args.append(args)
        return self._answer(sql, None)


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class _Ctx:
            async def __aenter__(self_inner):
                return conn

            async def __aexit__(self_inner, *a):
                return False
        return _Ctx()


def _request(conn):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=_Pool(conn))))


# ── 1. notifications list: jsonb payload arrives as a string ────────────────────────────────────

@pytest.mark.asyncio
async def test_notifications_list_decodes_string_payload():
    now = datetime.now(timezone.utc)
    row = {
        "id": 43, "event_type": "spend_budget_alert", "entity_type": "spend_budget",
        "entity_id": "job:segment_research",
        "payload": json.dumps({"message": "dfs per-run budget reached"}),
        "target_roles": ["admin"], "is_read": False, "dispatched_at": now, "created_at": now,
    }
    conn = _Conn([("FROM shared.notifications", [row])])
    with patch("api.routers.admin.verify_admin_secret"):
        out = await admin.list_notifications(_request(conn), x_admin_secret="x", unread_only=False,
                                             limit=10, offset=0)
    assert out["total"] == 1
    item = out["items"][0]
    assert item["payload"] == {"message": "dfs per-run budget reached"}
    assert item["message"] == "dfs per-run budget reached"


@pytest.mark.asyncio
async def test_notifications_list_accepts_dict_and_null_payload():
    now = datetime.now(timezone.utc)
    base = {"entity_type": "x", "entity_id": "1", "target_roles": None, "is_read": True,
            "dispatched_at": now, "created_at": now}
    rows = [{**base, "id": 1, "event_type": "tour.pipeline.completed", "payload": {"tour_name": "Paro"}},
            {**base, "id": 2, "event_type": "tour.pipeline.failed", "payload": None}]
    conn = _Conn([("FROM shared.notifications", rows)])
    with patch("api.routers.admin.verify_admin_secret"):
        out = await admin.list_notifications(_request(conn), x_admin_secret="x", unread_only=False,
                                             limit=10, offset=0)
    assert [i["message"] for i in out["items"]] == ["Paro", ""]
    assert out["items"][1]["payload"] == {}


# ── 2. S0 "ready" and S1 list only offer active sources ────────────────────────────────────────

@pytest.mark.asyncio
async def test_tours_ready_and_s1_list_exclude_superseded():
    conn = _Conn()
    with patch("api.routers.admin_pipeline.verify_admin_secret"):
        await admin_pipeline.get_tours_ready(_request(conn), x_admin_secret="x")
        await admin_pipeline.get_all_tours(_request(conn), x_admin_secret="x")
    assert len(conn.sql) == 2
    for sql in conn.sql:
        assert "source_status = 'active'" in sql
        assert "'trashed'" not in sql


# ── 3. one tenant key for internal spend (NULL and the aa_internal UUID fold together) ──────────

def test_llm_tree_folds_internal_uuid_into_null_key():
    sql = admin_llm_ops._TREE_SQL
    assert f"NULLIF(l.tenant_id, '{AA_INTERNAL}'::uuid)::text AS tenant_id" in sql
    assert f"ON t.tenant_id = NULLIF(l.tenant_id, '{AA_INTERNAL}'::uuid)" in sql
    assert "GROUP BY 1, t.slug" in sql


def test_dfs_tree_uses_same_fold_and_slug_label():
    sql = admin_llm_ops._DFS_TREE_SQL
    assert f"NULLIF(d.tenant_id, '{AA_INTERNAL}'::uuid)::text AS tenant_id" in sql
    assert "COALESCE(t.slug, 'aa_internal')" in sql
    assert "'platform'" not in sql


# ── 4. dashboard model usage = real calls/cost from llm_call_log ────────────────────────────────

@pytest.mark.asyncio
async def test_dashboard_model_usage_from_llm_call_log():
    rows = [{"model": "claude-sonnet-5", "calls": 4, "total_cost": 0.2},
            {"model": "gpt-5.6-luna", "calls": 0, "total_cost": 0.0}]
    conn = _Conn([("FROM shared.llm_call_log", rows)])
    with patch("api.routers.admin_pipeline.verify_admin_secret"):
        out = await admin_pipeline.get_pipeline_metrics(_request(conn), days=7, x_admin_secret="x")
    assert out["model_usage"][0] == {"model": "claude-sonnet-5", "calls": 4, "total_cost": 0.2,
                                     "cost_per_call": 0.05}
    assert out["model_usage"][1]["cost_per_call"] == 0.0
    assert not any("pipeline_runs" in s and "llm_model" in s for s in conn.sql)


# ── 6/7. tenant detail LLM cost comes from llm_call_log ─────────────────────────────────────────

def test_tenant_detail_cost_query_reads_llm_call_log():
    import inspect
    src = inspect.getsource(admin.get_tenant_details)
    assert "FROM shared.llm_call_log" in src
    assert "WHERE tenant_id = $1 OR ($2 AND tenant_id IS NULL)" in src


# ── 8. job dedup matches a NULL model_tier (S1 "Settings default") ──────────────────────────────

@pytest.mark.asyncio
async def test_find_active_duplicate_is_null_safe_on_model_tier():
    seen = {}

    class _C:
        async def fetchval(self, sql, *args):
            seen["sql"], seen["args"] = sql, args
            return None

        async def close(self):
            pass

    async def _connect(*a, **k):
        return _C()

    with patch.dict("os.environ", {"DATABASE_URL": "postgres://x"}), \
         patch("api.routers.jobs_repo.asyncpg.connect", _connect):
        await jobs_repo.find_active_duplicate({"tour_id": "t1", "model_tier": None, "batch_id": None})
    assert "request->>'model_tier' IS NOT DISTINCT FROM $2" in seen["sql"]
    assert seen["args"] == ("t1", None, None)
