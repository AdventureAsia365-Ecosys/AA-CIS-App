"""AA-1039 R6 rest (root ADR 0019 d13, IA §6.2) — SEO Intelligence's Spend tab folded into the admin Spend page.

``GET /admin/seo-intelligence`` no longer reads ``shared.dfs_call_log`` nor sends ``spend``: DataForSEO spend is on
Spend › DataForSEO (``GET /admin/dfs-usage``, spend.read), and content_lead, who reads this route, holds no spend.read.
The four remaining tabs keep their shape. No DB: a fake connection answers each query by its SQL constant.
"""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from api.routers import admin_seo


class _Conn:
    def __init__(self):
        self.sql: list[str] = []

    async def fetchrow(self, sql, *args):
        self.sql.append(sql)
        if sql is admin_seo._COVERAGE_SQL:
            return {"total_tours": 4, "seo_covered": 2}
        if sql is admin_seo._FRESHNESS_SQL:
            return {"places_researched": 3, "places_expired": 1, "median_age_days": 12.34,
                    "last_researched": datetime(2026, 10, 1, tzinfo=timezone.utc)}
        raise AssertionError(f"unexpected fetchrow: {sql[:60]}")

    async def fetchval(self, sql, *args):
        self.sql.append(sql)
        return 1 if sql is admin_seo._WITH_VOLUME_SQL else 5

    async def fetch(self, sql, *args):
        self.sql.append(sql)
        if sql is admin_seo._PAA_SQL:
            return [{"market": "US", "people_also_ask": '["Is the river valley walk easy?"]'}]
        return []


def _request(conn):
    @asynccontextmanager
    async def acquire():
        yield conn

    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=SimpleNamespace(acquire=acquire))))


def test_seo_intelligence_sends_no_spend_and_reads_no_dfs_log():
    conn = _Conn()
    with patch.object(admin_seo, "verify_admin_secret", lambda _s: None):
        out = asyncio.run(admin_seo.get_seo_intelligence(_request(conn), days=30, x_admin_secret="x"))
    assert set(out) == {"overview", "keywords", "questions", "gaps"}
    assert not any("dfs_call_log" in s for s in conn.sql), "the Spend page owns the DataForSEO spend read"
    assert out["overview"]["coverage_pct"] == 50.0
    assert out["overview"]["freshness"]["median_age_days"] == 12.3
    assert out["questions"] == [{"question": "Is the river valley walk easy?", "markets": ["US"]}]


def test_router_module_keeps_no_dfs_rollup_import():
    src = open(admin_seo.__file__, encoding="utf-8").read()
    assert "_DFS_TREE_SQL" not in src and "_DFS_SUMMARY_SQL" not in src
